"""Model backends: real local models (diffusers) and a mock for tests."""

from __future__ import annotations

import gc
import logging
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
from PIL import Image, ImageDraw

from .config import Settings
from .jobs import JobCancelled, ProgressFn

log = logging.getLogger("image_studio.backends")


class BackendUnavailable(Exception):
    """Raised when model weights or dependencies are missing."""


OOM_HINT = "Не хватило видеопамяти: уменьшите размер до 768×768 или включите FSS_LOWVRAM=on"
STAGE_LOADING = "Загрузка модели…"
LOWVRAM_THRESHOLD_BYTES = 10 * 2**30


def _report(progress: ProgressFn | None, stage: str, fraction: float) -> None:
    if progress is not None:
        progress(stage, fraction)


@dataclass(frozen=True)
class GenerateParams:
    prompt: str
    negative_prompt: str = ""
    width: int = 1024
    height: int = 1024
    steps: int = 30
    guidance: float = 7.0
    seed: int | None = None
    # Local .safetensors checkpoint; None = the default model (FSS_MODEL_ID)
    checkpoint: Path | None = None
    # (LoRA file, weight) pairs
    loras: tuple[tuple[Path, float], ...] = ()


class Generator(Protocol):
    def generate(self, params: GenerateParams, progress: ProgressFn | None = None) -> tuple[Image.Image, int]:
        """`progress(stage, fraction)` is optional; it may raise JobCancelled to abort."""
        ...


def resolve_device(requested: str) -> str:
    if requested != "auto":
        return requested
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
    except ImportError:
        pass
    return "cpu"


# --------------------------------------------------------------------------- diffusers


class DiffusersGenerator:
    """Text-to-image via diffusers: a Hugging Face model or a local single-file checkpoint,
    optionally with LoRA adapters. One pipeline is kept in memory; switching checkpoints reloads it.

    `lowvram`: "on" | "off" | "auto" (auto = on when the CUDA GPU has < 10 GB). Low-VRAM mode uses
    model CPU offload plus VAE slicing/tiling, so SDXL fits into 6-8 GB at the cost of speed."""

    def __init__(self, model_id: str, device: str, lowvram: str = "auto") -> None:
        self.model_id = model_id
        self.device = device
        self.lowvram = lowvram
        self._lowvram_active: bool | None = None
        self._pipe = None
        self._pipe_key: str | None = None
        self.architecture: str | None = None  # "sdxl" / "sd15" / None, of the loaded pipeline
        self._lock = threading.Lock()

    def _imports(self):
        try:
            import diffusers
            import torch
        except ImportError as exc:
            raise BackendUnavailable("Не установлены torch/diffusers. См. requirements-gpu.txt.") from exc
        return torch, diffusers

    # ------------------------------------------------------------------ low VRAM

    def _resolve_lowvram(self, torch) -> bool:
        if self._lowvram_active is not None:
            return self._lowvram_active
        mode = (self.lowvram or "auto").strip().lower()
        if self.device != "cuda" or mode == "off":
            active = False
        elif mode == "on":
            active = True
        else:
            try:
                total = torch.cuda.get_device_properties(0).total_memory
                active = total < LOWVRAM_THRESHOLD_BYTES
                log.info("GPU memory %.1f GB -> low-VRAM mode %s", total / 2**30, "on" if active else "off")
            except Exception as exc:  # noqa: BLE001 - driver quirks must not break loading
                log.warning("Could not query GPU memory (%s); low-VRAM mode off", exc)
                active = False
        self._lowvram_active = active
        return active

    @property
    def lowvram_active(self) -> bool | None:
        """Resolved low-VRAM state; None while unknown (torch not imported yet)."""
        if self._lowvram_active is None and "torch" in sys.modules:
            try:
                return self._resolve_lowvram(sys.modules["torch"])
            except Exception:  # noqa: BLE001
                return None
        return self._lowvram_active

    @staticmethod
    def _oom_types(torch) -> tuple[type[BaseException], ...]:
        oom = getattr(getattr(torch, "cuda", None), "OutOfMemoryError", None)
        return (oom,) if isinstance(oom, type) and issubclass(oom, BaseException) else ()

    def _free_memory(self, torch) -> None:
        gc.collect()
        if self.device == "cuda":
            try:
                torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ loading

    def _key(self, checkpoint: Path | None) -> str:
        return str(checkpoint) if checkpoint else self.model_id

    def needs_load(self, checkpoint: Path | None) -> bool:
        return self._pipe is None or self._pipe_key != self._key(checkpoint)

    def _load(self, checkpoint: Path | None):
        key = self._key(checkpoint)
        if not self.needs_load(checkpoint):
            return self._pipe
        torch, diffusers = self._imports()
        # Free the previous pipeline before loading the next one (SDXL needs ~7 GB VRAM)
        self._pipe, self._pipe_key, self.architecture = None, None, None
        self._free_memory(torch)
        dtype = torch.float16 if self.device in {"cuda", "mps"} else torch.float32
        lowvram = self._resolve_lowvram(torch)
        log.info("Loading %s on %s (%s, low-VRAM %s)", key, self.device, dtype, lowvram)
        if checkpoint is not None:
            pipe, arch = self._load_single_file(diffusers, checkpoint, dtype)
        else:
            kwargs = {"torch_dtype": dtype}
            if dtype == torch.float16:
                kwargs["variant"] = "fp16"
            try:
                pipe = diffusers.AutoPipelineForText2Image.from_pretrained(self.model_id, **kwargs)
            except (OSError, ValueError):
                kwargs.pop("variant", None)
                pipe = diffusers.AutoPipelineForText2Image.from_pretrained(self.model_id, **kwargs)
            arch = self._detect_architecture(pipe)
        if lowvram:
            # Keeps only the active sub-model on the GPU; requires `accelerate`
            pipe.enable_model_cpu_offload()
            for name in ("enable_vae_slicing", "enable_vae_tiling"):
                if callable(getattr(pipe, name, None)):
                    getattr(pipe, name)()
        else:
            pipe = pipe.to(self.device)
        if self.device == "cuda":
            pipe.enable_attention_slicing()
        self._pipe, self._pipe_key, self.architecture = pipe, key, arch
        return pipe

    @staticmethod
    def _detect_architecture(pipe) -> str | None:
        name = type(pipe).__name__
        if "XL" in name:
            return "sdxl"
        if name.startswith("StableDiffusion"):
            return "sd15"
        return None

    @staticmethod
    def _load_single_file(diffusers, checkpoint: Path, dtype):
        """Civitai-style .safetensors: try SDXL (Juggernaut, Illustrious, Pony...), then SD 1.5."""
        errors = []
        for cls_name, arch in (("StableDiffusionXLPipeline", "sdxl"), ("StableDiffusionPipeline", "sd15")):
            cls = getattr(diffusers, cls_name)
            try:
                return cls.from_single_file(str(checkpoint), torch_dtype=dtype), arch
            except Exception as exc:  # noqa: BLE001 - diffusers raises many types here
                errors.append(f"{cls_name}: {exc}")
        raise BackendUnavailable(
            f"Не удалось загрузить чекпоинт {checkpoint.name}: поддерживаются SDXL и SD 1.5. "
            + "; ".join(errors)[:500]
        )

    def _apply_loras(self, pipe, loras: tuple[tuple[Path, float], ...]) -> None:
        if not loras:
            return
        names, weights = [], []
        for i, (path, weight) in enumerate(loras):
            name = f"lora{i}"
            try:
                pipe.load_lora_weights(str(path.parent), weight_name=path.name, adapter_name=name)
            except Exception as exc:  # noqa: BLE001
                pipe.unload_lora_weights()
                raise BackendUnavailable(
                    f"LoRA {path.name} не подходит к выбранной модели (нужна та же база: SDXL или SD 1.5)."
                ) from exc
            names.append(name)
            weights.append(weight)
        pipe.set_adapters(names, adapter_weights=weights)

    # ------------------------------------------------------------------ generation

    def generate(self, params: GenerateParams, progress: ProgressFn | None = None) -> tuple[Image.Image, int]:
        torch, _ = self._imports()
        oom_types = self._oom_types(torch)
        seed = params.seed if params.seed is not None else int(np.random.default_rng().integers(0, 2**31 - 1))
        steps = max(1, params.steps)
        cancelled = False

        def on_step_end(pipe, step, timestep, callback_kwargs):
            nonlocal cancelled
            if progress is not None and not cancelled:
                done = min(step + 1, steps)
                try:
                    progress(f"Генерация: шаг {done}/{steps}", done / steps)
                except JobCancelled:
                    cancelled = True
                    pipe._interrupt = True  # diffusers stops the denoising loop after this step
            return callback_kwargs

        with self._lock:
            try:
                if self.needs_load(params.checkpoint):
                    _report(progress, STAGE_LOADING, 0.0)
                pipe = self._load(params.checkpoint)
                self._apply_loras(pipe, params.loras)
                try:
                    _report(progress, f"Генерация: шаг 0/{steps}", 0.0)
                    gen_device = "cpu" if self.device == "mps" else self.device
                    g = torch.Generator(device=gen_device).manual_seed(seed)
                    result = pipe(
                        prompt=params.prompt,
                        negative_prompt=params.negative_prompt or None,
                        width=params.width,
                        height=params.height,
                        num_inference_steps=steps,
                        guidance_scale=params.guidance,
                        generator=g,
                        callback_on_step_end=on_step_end,
                    )
                finally:
                    if params.loras:
                        pipe.unload_lora_weights()
            except oom_types as exc:
                log.warning(
                    "CUDA out of memory (%dx%d, low-VRAM %s)",
                    params.width,
                    params.height,
                    self._lowvram_active,
                )
                self._free_memory(torch)
                raise BackendUnavailable(OOM_HINT) from exc
        if cancelled:
            raise JobCancelled("Генерация отменена.")
        return result.images[0], seed


# --------------------------------------------------------------------------- mock


class MockGenerator:
    """Deterministic gradient image; lets the UI and tests run without models.

    `step_delay` (seconds per fake step, FSS_MOCK_DELAY) only exists to demo the progress bar."""

    fake_steps = 5

    def __init__(self, step_delay: float = 0.0) -> None:
        self.step_delay = step_delay
        self.architecture: str | None = None
        self.lowvram_active: bool | None = None

    def generate(self, params: GenerateParams, progress: ProgressFn | None = None) -> tuple[Image.Image, int]:
        steps = min(params.steps, self.fake_steps)
        for i in range(steps):
            _report(progress, f"Генерация: шаг {i + 1}/{steps}", (i + 1) / steps)
            if self.step_delay:
                time.sleep(self.step_delay)
        seed = params.seed if params.seed is not None else 42
        rng = np.random.default_rng(seed)
        c1, c2 = rng.integers(0, 255, 3), rng.integers(0, 255, 3)
        t = np.linspace(0, 1, params.width)[None, :, None]
        row = (c1 * (1 - t) + c2 * t).astype(np.uint8)
        arr = np.repeat(row, params.height, axis=0)
        img = Image.fromarray(arr, "RGB")
        draw = ImageDraw.Draw(img)
        draw.text((16, 16), params.prompt[:80], fill=(255, 255, 255))
        model = params.checkpoint.name if params.checkpoint else "default"
        loras = ", ".join(f"{p.name}:{w:g}" for p, w in params.loras)
        draw.text((16, 36), f"model: {model}" + (f" | lora: {loras}" if loras else ""), fill=(255, 255, 255))
        return img, seed


# --------------------------------------------------------------------------- factory


@dataclass
class Backends:
    name: str
    device: str
    generator: Generator


def build_backends(settings: Settings) -> Backends:
    if settings.backend == "mock":
        return Backends("mock", "cpu", MockGenerator(settings.mock_delay))
    if settings.backend == "diffusers":
        device = resolve_device(settings.device)
        return Backends(
            "diffusers",
            device,
            DiffusersGenerator(settings.model_id, device, lowvram=settings.lowvram),
        )
    raise ValueError(f"Unknown FSS_BACKEND: {settings.backend!r}")
