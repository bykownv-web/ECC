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
from .model_arch import (
    BUNDLE_PREFIXES,
    LABELS,
    SD15,
    SDXL,
    UNKNOWN,
    UNSUPPORTED,
    ZIMAGE,
    architecture_from_class_name,
    guess_repo_architecture,
    inspect_checkpoint,
)

log = logging.getLogger("image_studio.backends")


class BackendUnavailable(Exception):
    """Raised when model weights or dependencies are missing."""


OOM_HINT = "Не хватило видеопамяти: уменьшите размер до 768×768 или включите FSS_LOWVRAM=on"
STAGE_LOADING = "Загрузка модели…"
# auto low-VRAM: on when the GPU has less memory than this (compared in bytes; a "16 GB" card
# reports ~15.9-16.0 GiB, a "24 GB" card ~23.6 GiB). SDXL / SD 1.5 fit in 10 GB; Z-Image needs
# ~12 GB (bf16 DiT) + ~8 GB (Qwen3-4B text encoder) at once (~21 GB), so below 22 GiB it runs
# with model CPU offload (peak ~13 GB) and 24 GB cards keep everything on the GPU.
LOWVRAM_THRESHOLD_BYTES = 10 * 2**30
LOWVRAM_THRESHOLDS = {SDXL: LOWVRAM_THRESHOLD_BYTES, SD15: LOWVRAM_THRESHOLD_BYTES, ZIMAGE: 22 * 2**30}
# Below this even the Z-Image transformer alone does not fit: offload it block by block instead
ZIMAGE_GROUP_OFFLOAD_BELOW = 14 * 2**30
ZIMAGE_DIFFUSERS_HINT = (
    "Для Z-Image Turbo нужен diffusers 0.39 или новее и transformers 4.51 или новее: "
    "запустите install.bat ещё раз (или pip install -U -r requirements-gpu.txt)."
)
ZIMAGE_FP16_BLACK = (
    "Z-Image в float16 выдал пустое (чёрное) изображение: веса модели рассчитаны на bfloat16. "
    "Уберите FSS_ZIMAGE_DTYPE=fp16 (или задайте bf16) и перезапустите приложение."
)


def unsupported_message(name: str, arch: str) -> str:
    return f"{name}: это модель {LABELS[arch].split(' (')[0]}, такие модели приложение не поддерживает."


def _is_blank(image: Image.Image) -> bool:
    """All-black output: what NaNs (fp16 overflow) turn into after VAE post-processing."""
    return int(np.asarray(image.convert("L")).max()) == 0


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
    # Detected architecture of `checkpoint` (model_arch); None = detect when loading
    architecture: str | None = None


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

    Local checkpoints are classified by their .safetensors header (app/model_arch.py): SDXL and
    SD 1.5 are loaded as full pipelines; Z-Image Turbo files usually hold only the transformer, so
    the transformer is loaded from the file and the text encoder / VAE / tokenizer / scheduler come
    from `zimage_base` (downloaded once). Switching between Z-Image checkpoints keeps those parts.

    `lowvram`: "on" | "off" | "auto" (auto = on when the CUDA GPU has less memory than
    LOWVRAM_THRESHOLDS[architecture]). Low-VRAM mode uses model CPU offload plus VAE slicing/tiling
    (Z-Image on < 14 GB: block-level group offloading of the transformer and text encoder)."""

    def __init__(
        self,
        model_id: str,
        device: str,
        lowvram: str = "auto",
        zimage_base: str = "Tongyi-MAI/Z-Image-Turbo",
        zimage_dtype: str = "auto",
    ) -> None:
        self.model_id = model_id
        self.device = device
        self.lowvram = lowvram
        self.zimage_base = zimage_base
        self.zimage_dtype = zimage_dtype  # "auto" (= bf16) | "bf16" | "fp16"
        self._fp16_zimage = False  # the loaded Z-Image pipeline runs in float16
        self._gpu_total: int | None = None
        self._gpu_queried = False
        self._lowvram_active: bool | None = None
        self._offload: str | None = None  # "none" / "model" / "group" of the loaded pipeline
        self._pipe = None
        self._pipe_key: str | None = None
        self._zimage_repo: str | None = None  # repo the loaded Z-Image base components came from
        self.architecture: str | None = None  # "sdxl" / "sd15" / "zimage" / None, of the loaded pipeline
        self._lock = threading.Lock()

    def _imports(self):
        try:
            import diffusers
            import torch
        except ImportError as exc:
            raise BackendUnavailable("Не установлены torch/diffusers. См. requirements-gpu.txt.") from exc
        return torch, diffusers

    # ------------------------------------------------------------------ low VRAM

    def _gpu_memory(self, torch) -> int | None:
        if not self._gpu_queried:
            self._gpu_queried = True
            try:
                self._gpu_total = int(torch.cuda.get_device_properties(0).total_memory)
                log.info("GPU memory %.2f GiB", self._gpu_total / 2**30)
            except Exception as exc:  # noqa: BLE001 - driver quirks must not break loading
                log.warning("Could not query GPU memory (%s); low-VRAM auto mode off", exc)
        return self._gpu_total

    def _offload_plan(self, torch, arch: str | None) -> str:
        """ "none" (everything on the device), "model" (model CPU offload) or "group"."""
        mode = (self.lowvram or "auto").strip().lower()
        if self.device != "cuda" or mode == "off":
            return "none"
        total = self._gpu_memory(torch)
        if mode != "on":
            threshold = LOWVRAM_THRESHOLDS.get(arch or SDXL, LOWVRAM_THRESHOLD_BYTES)
            if total is None or total >= threshold:
                return "none"
        if arch == ZIMAGE and total is not None and total < ZIMAGE_GROUP_OFFLOAD_BELOW:
            return "group"
        return "model"

    def _resolve_lowvram(self, torch, arch: str | None = None) -> bool:
        return self._offload_plan(torch, arch) != "none"

    @property
    def lowvram_active(self) -> bool | None:
        """Low-VRAM state of the loaded pipeline, or the expected one for the default model;
        None while unknown (torch not imported yet)."""
        if self._lowvram_active is None and "torch" in sys.modules:
            try:
                return self._resolve_lowvram(sys.modules["torch"], guess_repo_architecture(self.model_id))
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

    def _target_architecture(self, checkpoint: Path | None, hint: str | None) -> str | None:
        if checkpoint is None:
            return guess_repo_architecture(self.model_id)
        return hint or inspect_checkpoint(checkpoint).architecture

    def _release(self, torch, keep_zimage_repo: str | None) -> dict:
        """Drop the loaded pipeline. Returns its Z-Image base components (all but the transformer)
        when the next pipeline is built from the same base repo, so they need not be reloaded."""
        old, old_arch, old_repo, old_offload = self._pipe, self.architecture, self._zimage_repo, self._offload
        self._pipe, self._pipe_key, self.architecture, self._zimage_repo = None, None, None, None
        self._lowvram_active, self._offload, self._fp16_zimage = None, None, False
        reuse: dict = {}
        # group offloading hooks cannot be re-applied to the same modules: reload those instead
        if old is not None and old_arch == ZIMAGE and keep_zimage_repo and old_repo == keep_zimage_repo:
            if old_offload != "group":
                if old_offload == "model" and callable(getattr(old, "remove_all_hooks", None)):
                    old.remove_all_hooks()
                reuse = {k: v for k, v in dict(old.components).items() if k != "transformer"}
        del old
        self._free_memory(torch)
        return reuse

    def _load(self, checkpoint: Path | None, arch_hint: str | None = None):
        key = self._key(checkpoint)
        if not self.needs_load(checkpoint):
            return self._pipe
        torch, diffusers = self._imports()
        arch = self._target_architecture(checkpoint, arch_hint)
        if arch in UNSUPPORTED:
            raise BackendUnavailable(unsupported_message(checkpoint.name if checkpoint else key, arch))
        zimage_repo = None
        if arch == ZIMAGE:
            zimage_repo = self.zimage_base if checkpoint is not None else self.model_id
            if checkpoint is not None and inspect_checkpoint(checkpoint).fp8_scaled:
                raise BackendUnavailable(
                    f"{checkpoint.name}: «scaled fp8» версии Z-Image не поддерживаются. "
                    "Скачайте вариант bf16 или fp16 этой модели."
                )
        # Free the previous pipeline before loading the next one (SDXL needs ~7 GB VRAM)
        reuse = self._release(torch, zimage_repo)
        plan = self._offload_plan(torch, arch)
        if arch == ZIMAGE:
            dtype = self._zimage_dtype(torch)
            log.info("Loading Z-Image %s on %s (%s, offload %s)", key, self.device, dtype, plan)
            pipe = self._load_zimage(torch, diffusers, checkpoint, zimage_repo, dtype, reuse)
        else:
            dtype = torch.float16 if self.device in {"cuda", "mps"} else torch.float32
            log.info("Loading %s on %s (%s, offload %s)", key, self.device, dtype, plan)
            if checkpoint is not None:
                pipe, arch = self._load_single_file(diffusers, checkpoint, dtype, arch)
            else:
                pipe = self._load_pretrained(torch, diffusers, dtype)
                arch = self._detect_architecture(pipe)
                if arch == ZIMAGE:
                    # Z-Image repo the name heuristics missed: AutoPipeline loaded it in float16;
                    # reload in the Z-Image dtype so reused components never mix dtypes
                    log.info("%s is a Z-Image repo: reloading in the Z-Image dtype", key)
                    del pipe
                    self._free_memory(torch)
                    zimage_repo = self.model_id
                    plan = self._offload_plan(torch, arch)
                    dtype = self._zimage_dtype(torch)
                    pipe = self._load_zimage(torch, diffusers, None, zimage_repo, dtype, {})
        pipe = self._place(torch, pipe, arch, plan)
        self._pipe, self._pipe_key, self.architecture = pipe, key, arch
        self._zimage_repo, self._offload, self._lowvram_active = zimage_repo, plan, plan != "none"
        self._fp16_zimage = arch == ZIMAGE and dtype == torch.float16
        return pipe

    def _load_pretrained(self, torch, diffusers, dtype):
        kwargs = {"torch_dtype": dtype}
        if dtype == torch.float16:
            kwargs["variant"] = "fp16"
        try:
            return diffusers.AutoPipelineForText2Image.from_pretrained(self.model_id, **kwargs)
        except (OSError, ValueError):
            kwargs.pop("variant", None)
            return diffusers.AutoPipelineForText2Image.from_pretrained(self.model_id, **kwargs)

    def _place(self, torch, pipe, arch: str | None, plan: str):
        if plan == "group":
            self._group_offload(torch, pipe)
        elif plan == "model":
            # Keeps only the active sub-model on the GPU; requires `accelerate`
            pipe.enable_model_cpu_offload()
        else:
            pipe = pipe.to(self.device)
        if plan != "none":
            self._vae_memory_savers(pipe)
        # Z-Image uses its own attention processor; sliced attention would replace it
        if self.device == "cuda" and arch != ZIMAGE:
            pipe.enable_attention_slicing()
        return pipe

    def _group_offload(self, torch, pipe) -> None:
        """Stream transformer / text encoder blocks to the GPU one at a time; VAE stays on it."""
        if callable(getattr(pipe, "enable_group_offload", None)):
            pipe.enable_group_offload(
                onload_device=torch.device(self.device),
                offload_device=torch.device("cpu"),
                offload_type="block_level",
                num_blocks_per_group=1,
                use_stream=True,
                low_cpu_mem_usage=True,
                exclude_modules=["vae"],
            )
        else:  # older diffusers: slowest, but fits anything
            pipe.enable_sequential_cpu_offload()

    @staticmethod
    def _vae_memory_savers(pipe) -> None:
        vae = getattr(pipe, "vae", None)
        pairs = (("enable_vae_slicing", "enable_slicing"), ("enable_vae_tiling", "enable_tiling"))
        for pipe_name, vae_name in pairs:
            fn = getattr(vae, vae_name, None) if vae is not None else None
            if not callable(fn):  # pipeline-level helpers are deprecated in newer diffusers
                fn = getattr(pipe, pipe_name, None)
            if callable(fn):
                fn()

    @staticmethod
    def _detect_architecture(pipe) -> str | None:
        return architecture_from_class_name(type(pipe).__name__)

    @staticmethod
    def _load_single_file(diffusers, checkpoint: Path, dtype, arch: str | None = None):
        """Civitai-style .safetensors with a known architecture; unknown: try SDXL, then SD 1.5."""
        candidates = (("StableDiffusionXLPipeline", SDXL), ("StableDiffusionPipeline", SD15))
        if arch in (SDXL, SD15):
            candidates = tuple(c for c in candidates if c[1] == arch)
        errors = []
        for cls_name, cls_arch in candidates:
            cls = getattr(diffusers, cls_name)
            try:
                return cls.from_single_file(str(checkpoint), torch_dtype=dtype), cls_arch
            except Exception as exc:  # noqa: BLE001 - diffusers raises many types here
                errors.append(f"{cls_name}: {exc}")
        if arch in (SDXL, SD15):
            hint = f"файл похож на {LABELS[arch]}, но не загрузился"
        else:
            hint = "поддерживаются SDXL, SD 1.5 и Z-Image Turbo"
        detail = "; ".join(errors)[:500]
        raise BackendUnavailable(f"Не удалось загрузить чекпоинт {checkpoint.name}: {hint}. {detail}")

    # ------------------------------------------------------------------ Z-Image

    def _zimage_dtype(self, torch):
        """bfloat16, the dtype Z-Image is published in, also on GPUs older than Ampere (slower
        there, but correct). float16 only on request (FSS_ZIMAGE_DTYPE=fp16): the transformer has
        no fp16 overflow guards, so it may produce black images."""
        if (self.zimage_dtype or "auto").strip().lower() == "fp16":
            return torch.float16
        return torch.bfloat16

    def _load_zimage(self, torch, diffusers, checkpoint: Path | None, repo: str, dtype, reuse: dict):
        pipe_cls = getattr(diffusers, "ZImagePipeline", None)
        if pipe_cls is None:
            raise BackendUnavailable(ZIMAGE_DIFFUSERS_HINT)
        kwargs = dict(reuse)
        if checkpoint is not None:
            kwargs["transformer"] = self._load_zimage_transformer(torch, diffusers, checkpoint, dtype)
        try:
            return pipe_cls.from_pretrained(repo, torch_dtype=dtype, **kwargs)
        except (OSError, ValueError) as exc:
            # the transformer folder is not downloaded when a local transformer is passed
            size = "около 8 ГБ" if checkpoint is not None else "около 20 ГБ"
            raise BackendUnavailable(
                f"Не удалось загрузить базовую модель Z-Image «{repo}» (при первом запуске "
                f"скачивается с Hugging Face, {size}; нужен интернет): {exc}"[:600]
            ) from exc

    def _load_zimage_transformer(self, torch, diffusers, checkpoint: Path, dtype):
        cls = getattr(diffusers, "ZImageTransformer2DModel", None)
        if cls is None or not callable(getattr(cls, "from_single_file", None)):
            raise BackendUnavailable(ZIMAGE_DIFFUSERS_HINT)
        info = inspect_checkpoint(checkpoint)
        source = str(checkpoint)
        if info.layout == "bundle":
            # all-in-one file: read only the diffusion model tensors (saves ~8 GB of RAM)
            source = self._read_transformer_tensors(checkpoint, info.prefix)
        try:
            # config from the base repo: works for original, ComfyUI and diffusers key layouts
            return cls.from_single_file(
                source, config=self.zimage_base, subfolder="transformer", torch_dtype=dtype
            )
        except self._oom_types(torch):
            raise
        except Exception as exc:  # noqa: BLE001 - diffusers raises many types here
            raise BackendUnavailable(f"Не удалось загрузить Z-Image {checkpoint.name}: {exc}"[:600]) from exc

    @staticmethod
    def _read_transformer_tensors(checkpoint: Path, prefix: str) -> dict:
        from safetensors import safe_open

        with safe_open(str(checkpoint), framework="pt") as fh:
            return {
                k: fh.get_tensor(k)
                for k in fh.keys()
                if k.startswith(prefix) and not k.startswith(BUNDLE_PREFIXES)
            }

    # ------------------------------------------------------------------ LoRA

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
                base = LABELS.get(self.architecture or UNKNOWN, "SDXL, SD 1.5 или Z-Image")
                raise BackendUnavailable(
                    f"LoRA {path.name} не подходит к выбранной модели (нужна LoRA для той же базы: {base})."
                ) from exc
            names.append(name)
            weights.append(weight)
        try:
            pipe.set_adapters(names, adapter_weights=weights)
        except Exception as exc:  # noqa: BLE001 - e.g. ValueError: no adapter created (keys did not match)
            pipe.unload_lora_weights()
            base = LABELS.get(self.architecture or UNKNOWN, "SDXL, SD 1.5 или Z-Image")
            files = ", ".join(path.name for path, _ in loras)
            raise BackendUnavailable(
                f"LoRA не подходит к выбранной модели ({files}; нужна LoRA для той же базы: {base})."
            ) from exc

    # ------------------------------------------------------------------ generation

    def _call_kwargs(self, params: GenerateParams, steps: int, generator, on_step_end) -> dict:
        kwargs = {
            "prompt": params.prompt,
            "negative_prompt": params.negative_prompt or None,
            "width": params.width,
            "height": params.height,
            "num_inference_steps": steps,
            "guidance_scale": params.guidance,
            "generator": generator,
            "callback_on_step_end": on_step_end,
        }
        if self.architecture == ZIMAGE:
            # ZImagePipeline: sizes must be multiples of 16; the negative prompt only matters
            # with CFG (guidance_scale > 0) - Turbo runs without it
            kwargs["width"] = max(16, params.width // 16 * 16)
            kwargs["height"] = max(16, params.height // 16 * 16)
            if params.guidance <= 0:
                kwargs["negative_prompt"] = None
            # only tensors listed in ZImagePipeline._callback_tensor_inputs may be requested
            kwargs["callback_on_step_end_tensor_inputs"] = ["latents"]
        return kwargs

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
                pipe = self._load(params.checkpoint, params.architecture)
                self._apply_loras(pipe, params.loras)
                try:
                    _report(progress, f"Генерация: шаг 0/{steps}", 0.0)
                    gen_device = "cpu" if self.device == "mps" else self.device
                    g = torch.Generator(device=gen_device).manual_seed(seed)
                    result = pipe(**self._call_kwargs(params, steps, g, on_step_end))
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
        image = result.images[0]
        if self._fp16_zimage and _is_blank(image):
            raise BackendUnavailable(ZIMAGE_FP16_BLACK)
        return image, seed


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
            DiffusersGenerator(
                settings.model_id,
                device,
                lowvram=settings.lowvram,
                zimage_base=settings.zimage_base,
                zimage_dtype=settings.zimage_dtype,
            ),
        )
    raise ValueError(f"Unknown FSS_BACKEND: {settings.backend!r}")
