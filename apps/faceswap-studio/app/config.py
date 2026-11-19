"""Runtime configuration, read from environment variables (prefix FSS_)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    # "diffusers" = real local models, "mock" = no models (tests / UI development)
    backend: str = "diffusers"
    # Hugging Face id or local path of a text-to-image model
    model_id: str = "stabilityai/stable-diffusion-xl-base-1.0"
    # Local single-file models (.safetensors from Civitai etc.)
    checkpoints_dir: Path = APP_ROOT / "models" / "checkpoints"
    loras_dir: Path = APP_ROOT / "models" / "loras"
    max_loras: int = 5
    # "auto" picks cuda > mps > cpu
    device: str = "auto"
    # inswapper_128 weights (must be placed manually, see README). The detector/recognizer
    # (buffalo_l: det_10g.onnx, w600k_r50.onnx) live in <this file's folder>/buffalo_l
    swap_model_path: Path = APP_ROOT / "models" / "inswapper_128.onnx"
    # Kept for backwards compatibility; the onnxruntime face engine always uses buffalo_l
    face_analysis_pack: str = "buffalo_l"
    # Optional NSFW image classifier (transformers); empty string disables it
    nsfw_model_id: str = "Falconsai/nsfw_image_detection"
    nsfw_threshold: float = 0.7
    watermark: bool = True
    output_dir: Path = APP_ROOT / "outputs"
    max_upload_mb: int = 10
    max_side: int = 1536
    default_steps: int = 30
    max_steps: int = 80
    extra_blocked_terms: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_env(cls) -> Settings:
        extra = os.environ.get("FSS_BLOCKED_TERMS", "")
        return cls(
            backend=os.environ.get("FSS_BACKEND", cls.backend).strip().lower(),
            model_id=os.environ.get("FSS_MODEL_ID", cls.model_id),
            device=os.environ.get("FSS_DEVICE", cls.device),
            checkpoints_dir=Path(os.environ.get("FSS_CHECKPOINTS_DIR", str(cls.checkpoints_dir))),
            loras_dir=Path(os.environ.get("FSS_LORAS_DIR", str(cls.loras_dir))),
            swap_model_path=Path(os.environ.get("FSS_SWAP_MODEL_PATH", str(cls.swap_model_path))),
            face_analysis_pack=os.environ.get("FSS_FACE_PACK", cls.face_analysis_pack),
            nsfw_model_id=os.environ.get("FSS_NSFW_MODEL", cls.nsfw_model_id),
            nsfw_threshold=float(os.environ.get("FSS_NSFW_THRESHOLD", cls.nsfw_threshold)),
            watermark=_bool("FSS_WATERMARK", cls.watermark),
            output_dir=Path(os.environ.get("FSS_OUTPUT_DIR", str(cls.output_dir))),
            max_upload_mb=_int("FSS_MAX_UPLOAD_MB", cls.max_upload_mb),
            max_side=_int("FSS_MAX_SIDE", cls.max_side),
            default_steps=_int("FSS_DEFAULT_STEPS", cls.default_steps),
            max_steps=_int("FSS_MAX_STEPS", cls.max_steps),
            extra_blocked_terms=tuple(t.strip().lower() for t in extra.split(",") if t.strip()),
        )
