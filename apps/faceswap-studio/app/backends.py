"""Model backends: real local models (diffusers + InsightFace) and a mock for tests."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .config import Settings

log = logging.getLogger("faceswap.backends")


class NoFaceFound(Exception):
    """Raised when no face is detected in an input image."""


class BackendUnavailable(Exception):
    """Raised when model weights or dependencies are missing."""


@dataclass(frozen=True)
class GenerateParams:
    prompt: str
    negative_prompt: str = ""
    width: int = 1024
    height: int = 1024
    steps: int = 30
    guidance: float = 7.0
    seed: int | None = None


class Generator(Protocol):
    def generate(self, params: GenerateParams) -> tuple[Image.Image, int]: ...


class FaceSwapper(Protocol):
    def swap(
        self, source: Image.Image, target: Image.Image, all_faces: bool = False
    ) -> tuple[Image.Image, int]: ...


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
    """Text-to-image via any diffusers AutoPipeline-compatible model (SD 1.5, SDXL, ...)."""

    def __init__(self, model_id: str, device: str) -> None:
        self.model_id = model_id
        self.device = device
        self._pipe = None
        self._lock = threading.Lock()

    def _load(self):
        if self._pipe is not None:
            return self._pipe
        try:
            import torch
            from diffusers import AutoPipelineForText2Image
        except ImportError as exc:
            raise BackendUnavailable("Не установлены torch/diffusers. См. requirements-gpu.txt.") from exc
        dtype = torch.float16 if self.device in {"cuda", "mps"} else torch.float32
        log.info("Loading %s on %s (%s)", self.model_id, self.device, dtype)
        kwargs = {"torch_dtype": dtype}
        if dtype == torch.float16:
            kwargs["variant"] = "fp16"
        try:
            pipe = AutoPipelineForText2Image.from_pretrained(self.model_id, **kwargs)
        except (OSError, ValueError):
            kwargs.pop("variant", None)
            pipe = AutoPipelineForText2Image.from_pretrained(self.model_id, **kwargs)
        pipe = pipe.to(self.device)
        if self.device == "cuda":
            pipe.enable_attention_slicing()
        self._pipe = pipe
        return pipe

    def generate(self, params: GenerateParams) -> tuple[Image.Image, int]:
        import torch

        seed = params.seed if params.seed is not None else int(np.random.default_rng().integers(0, 2**31 - 1))
        with self._lock:
            pipe = self._load()
            gen_device = "cpu" if self.device == "mps" else self.device
            g = torch.Generator(device=gen_device).manual_seed(seed)
            result = pipe(
                prompt=params.prompt,
                negative_prompt=params.negative_prompt or None,
                width=params.width,
                height=params.height,
                num_inference_steps=params.steps,
                guidance_scale=params.guidance,
                generator=g,
            )
        return result.images[0], seed


# --------------------------------------------------------------------------- insightface


class InsightFaceSwapper:
    """Face swap with InsightFace detection + inswapper_128 ONNX model."""

    def __init__(self, settings: Settings, device: str) -> None:
        self.model_path = settings.swap_model_path
        self.pack = settings.face_analysis_pack
        self.device = device
        self._analyser = None
        self._swapper = None
        self._lock = threading.Lock()

    def _providers(self) -> list[str]:
        if self.device == "cuda":
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]
        return ["CPUExecutionProvider"]

    def _load(self):
        if self._swapper is not None:
            return self._analyser, self._swapper
        if not self.model_path.is_file():
            raise BackendUnavailable(f"Файл модели подмены лица не найден: {self.model_path}. См. README.")
        try:
            import insightface
            from insightface.app import FaceAnalysis
        except ImportError as exc:
            raise BackendUnavailable(
                "Не установлен insightface/onnxruntime. См. requirements-gpu.txt."
            ) from exc
        providers = self._providers()
        analyser = FaceAnalysis(name=self.pack, providers=providers)
        analyser.prepare(ctx_id=0 if self.device == "cuda" else -1, det_size=(640, 640))
        swapper = insightface.model_zoo.get_model(str(self.model_path), providers=providers)
        self._analyser, self._swapper = analyser, swapper
        return analyser, swapper

    @staticmethod
    def _to_bgr(img: Image.Image) -> np.ndarray:
        return np.asarray(img.convert("RGB"))[:, :, ::-1].copy()

    def swap(
        self, source: Image.Image, target: Image.Image, all_faces: bool = False
    ) -> tuple[Image.Image, int]:
        with self._lock:
            analyser, swapper = self._load()
            src = self._to_bgr(source)
            dst = self._to_bgr(target)
            src_faces = analyser.get(src)
            if not src_faces:
                raise NoFaceFound("На фото-источнике не найдено лицо.")
            # Use the largest face of the source photo
            src_face = max(src_faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
            dst_faces = analyser.get(dst)
            if not dst_faces:
                raise NoFaceFound("На целевом изображении не найдено лицо.")
            if not all_faces:
                dst_faces = [
                    max(
                        dst_faces,
                        key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]),
                    )
                ]
            out = dst
            for face in dst_faces:
                out = swapper.get(out, face, src_face, paste_back=True)
        return Image.fromarray(out[:, :, ::-1]), len(dst_faces)


# --------------------------------------------------------------------------- mock


class MockGenerator:
    """Deterministic gradient image; lets the UI and tests run without models."""

    def generate(self, params: GenerateParams) -> tuple[Image.Image, int]:
        seed = params.seed if params.seed is not None else 42
        rng = np.random.default_rng(seed)
        c1, c2 = rng.integers(0, 255, 3), rng.integers(0, 255, 3)
        t = np.linspace(0, 1, params.width)[None, :, None]
        row = (c1 * (1 - t) + c2 * t).astype(np.uint8)
        arr = np.repeat(row, params.height, axis=0)
        img = Image.fromarray(arr, "RGB")
        ImageDraw.Draw(img).text((16, 16), params.prompt[:80], fill=(255, 255, 255))
        return img, seed


class MockSwapper:
    """Pastes a blurred, resized centre crop of the source onto the target centre."""

    def swap(
        self, source: Image.Image, target: Image.Image, all_faces: bool = False
    ) -> tuple[Image.Image, int]:
        out = target.convert("RGB").copy()
        side = min(out.size) // 3
        if side < 8:
            raise NoFaceFound("Изображение слишком маленькое.")
        src = source.convert("RGB")
        s = min(src.size)
        box = ((src.width - s) // 2, (src.height - s) // 2)
        crop = src.crop((*box, box[0] + s, box[1] + s)).resize((side, side))
        mask = Image.new("L", (side, side), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, side, side), fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(side // 10))
        out.paste(crop, ((out.width - side) // 2, (out.height - side) // 2), mask)
        return out, 1


# --------------------------------------------------------------------------- factory


@dataclass
class Backends:
    name: str
    device: str
    generator: Generator
    swapper: FaceSwapper


def build_backends(settings: Settings) -> Backends:
    if settings.backend == "mock":
        return Backends("mock", "cpu", MockGenerator(), MockSwapper())
    if settings.backend == "diffusers":
        device = resolve_device(settings.device)
        return Backends(
            "diffusers",
            device,
            DiffusersGenerator(settings.model_id, device),
            InsightFaceSwapper(settings, device),
        )
    raise ValueError(f"Unknown FSS_BACKEND: {settings.backend!r}")
