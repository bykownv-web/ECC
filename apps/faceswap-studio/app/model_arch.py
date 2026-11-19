"""Architecture detection for local .safetensors files without loading the weights.

A .safetensors file starts with an 8-byte little-endian header length followed by a JSON object
that maps tensor names to dtype/shape/offsets. The tensor names are enough to tell SDXL, SD 1.5 and
Z-Image apart, so only the header is read (a few KB to a few MB), never the multi-GB tensor data.

Key names follow the diffusers single-file loaders (diffusers/loaders/single_file_utils.py,
CHECKPOINT_KEY_NAMES and convert_z_image_transformer_checkpoint_to_diffusers).
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

SDXL, SD15, ZIMAGE, UNKNOWN = "sdxl", "sd15", "zimage", "unknown"
# Recognised only to give a clear "not supported" message
FLUX = "flux"
ARCHITECTURES = (SDXL, SD15, ZIMAGE)
UNSUPPORTED = (FLUX,)
LABELS = {SDXL: "SDXL", SD15: "SD 1.5", ZIMAGE: "Z-Image Turbo", FLUX: "FLUX (не поддерживается)"}

MAX_HEADER_BYTES = 100 * 2**20

# Z-Image transformer marker: original / ComfyUI layout ("model.diffusion_model." prefix) and
# the diffusers layout share it. Lumina 2 uses adaLN_modulation.1, so it does not match.
ZIMAGE_MARKER = "layers.0.adaLN_modulation.0.weight"
ZIMAGE_PREFIXES = ("", "model.diffusion_model.")
# Parts of an all-in-one (ComfyUI "checkpoint") file besides the diffusion model
BUNDLE_PREFIXES = ("first_stage_model.", "vae.", "text_encoders.", "text_encoder.", "cond_stage_model.")

# Per-architecture generation defaults. Z-Image Turbo is guidance-distilled: 8 steps, CFG 0
# (diffusers ZImagePipeline docstring example: num_inference_steps=8, guidance_scale=0.0).
ARCH_DEFAULTS = {
    SDXL: {"steps": 30, "guidance": 7.0, "width": 1024, "height": 1024},
    SD15: {"steps": 30, "guidance": 7.0, "width": 512, "height": 512},
    ZIMAGE: {"steps": 8, "guidance": 0.0, "width": 1024, "height": 1024},
}
# ZImagePipeline requires width/height divisible by vae_scale_factor * 2 = 16
SIZE_MULTIPLE = {ZIMAGE: 16}


class InvalidSafetensors(ValueError):
    """The file is not a readable .safetensors file (bad or oversized header)."""


@dataclass(frozen=True)
class CheckpointInfo:
    architecture: str
    # zimage only: "transformer" (DiT weights only) or "bundle" (also text encoder / VAE)
    layout: str | None = None
    # zimage only: key prefix of the transformer weights ("" or "model.diffusion_model.")
    prefix: str = ""
    # ComfyUI "scaled fp8" weights need per-tensor scales that diffusers does not apply
    fp8_scaled: bool = False


def read_header(path: Path, max_bytes: int = MAX_HEADER_BYTES) -> dict:
    """Return the JSON header of a .safetensors file. Raises InvalidSafetensors."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(0)
            raw_len = fh.read(8)
            if len(raw_len) != 8:
                raise InvalidSafetensors("файл слишком короткий")
            (length,) = struct.unpack("<Q", raw_len)
            if length < 2 or length > max_bytes or length > size - 8:
                raise InvalidSafetensors(f"недопустимая длина заголовка: {length}")
            raw = fh.read(length)
            if not raw.startswith(b"{"):
                raise InvalidSafetensors("заголовок не начинается с «{»")
    except OSError as exc:
        raise InvalidSafetensors(str(exc)) from exc
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise InvalidSafetensors("заголовок не является JSON") from exc
    if not isinstance(header, dict):
        raise InvalidSafetensors("заголовок не является JSON-объектом")
    return header


def _split(header: dict) -> tuple[list[str], dict]:
    meta = header.get("__metadata__")
    keys = [k for k in header if k != "__metadata__"]
    return keys, meta if isinstance(meta, dict) else {}


def classify_checkpoint_keys(keys: list[str] | set[str]) -> CheckpointInfo:
    keyset = set(keys)
    for prefix in ZIMAGE_PREFIXES:
        if prefix + ZIMAGE_MARKER in keyset:
            bundle = any(k.startswith(BUNDLE_PREFIXES) for k in keyset)
            fp8_scaled = "scaled_fp8" in keyset or any(k.endswith(".scale_weight") for k in keyset)
            return CheckpointInfo(ZIMAGE, "bundle" if bundle else "transformer", prefix, fp8_scaled)
    if any(k.startswith("conditioner.embedders.") for k in keyset) or (
        "model.diffusion_model.label_emb.0.0.weight" in keyset
    ):
        return CheckpointInfo(SDXL)
    if any(k.startswith("cond_stage_model.") for k in keyset) or (
        "model.diffusion_model.output_blocks.11.0.skip_connection.weight" in keyset
    ):
        return CheckpointInfo(SD15)
    if any(_FLUX.search(k) for k in keyset):
        return CheckpointInfo(FLUX)
    return CheckpointInfo(UNKNOWN)


@lru_cache(maxsize=256)
def _inspect_cached(path: str, _mtime_ns: int, _size: int) -> CheckpointInfo:
    keys, _ = _split(read_header(Path(path)))
    return classify_checkpoint_keys(keys)


def inspect_checkpoint(path: Path) -> CheckpointInfo:
    """Classify a checkpoint by its tensor names; unreadable files are "unknown"."""
    try:
        st = Path(path).stat()
        return _inspect_cached(str(path), st.st_mtime_ns, st.st_size)
    except (OSError, InvalidSafetensors):
        return CheckpointInfo(UNKNOWN)


def detect_architecture(path: Path) -> str:
    """ "sdxl" | "sd15" | "zimage" | "unknown" for a single-file checkpoint."""
    return inspect_checkpoint(path).architecture


# FLUX DiT blocks (original / ComfyUI / diffusers / kohya key styles)
_FLUX = re.compile(r"(?:^|[._])(?:double_blocks|single_blocks|single_transformer_blocks)[._]\d")

# ------------------------------------------------------------------ LoRA

_ZIMAGE_LORA = re.compile(
    r"(?:^|[._])(?:layers|noise_refiner|context_refiner)[._]\d+[._](?:attention|feed_forward|adaLN)"
)
# SDXL UNet attention has up to 10 transformer blocks per layer, SD 1.5 only one; DiT models
# (FLUX, SD3) also use "transformer_blocks", so require a UNet block before it
_SDXL_LORA = re.compile(
    r"lora_te2_|text_encoder_2\.|(?:down|up|mid|input|output|middle)_blocks?(?:[._]\d+)?[._].*"
    r"transformer_blocks[._][1-9]"
)
_UNET_LORA = re.compile(r"(?:down|up|mid)_blocks?[._]|(?:input|output|middle)_blocks?[._]")
_SD15_LORA = re.compile(r"lora_te_text_model|down_blocks[._]0[._]attentions|input_blocks[._][12][._]1[._]")


def classify_lora_keys(keys: list[str] | set[str], metadata: dict | None = None) -> str:
    keys = list(keys)
    if any(_ZIMAGE_LORA.search(k) for k in keys) and not any(_UNET_LORA.search(k) for k in keys):
        return ZIMAGE
    if any(_FLUX.search(k) for k in keys) and not any(_UNET_LORA.search(k) for k in keys):
        return FLUX
    if any(_SDXL_LORA.search(k) for k in keys):
        return SDXL
    if any(_SD15_LORA.search(k) for k in keys):
        return SD15
    base = str((metadata or {}).get("ss_base_model_version", "")).lower()
    if base.startswith("sdxl"):
        return SDXL
    if base.startswith(("sd_v1", "sd1")):
        return SD15
    if "z_image" in base or "zimage" in base or "z-image" in base:
        return ZIMAGE
    return UNKNOWN


@lru_cache(maxsize=256)
def _lora_cached(path: str, _mtime_ns: int, _size: int) -> str:
    keys, meta = _split(read_header(Path(path)))
    return classify_lora_keys(keys, meta)


def detect_lora_architecture(path: Path) -> str:
    try:
        st = Path(path).stat()
        return _lora_cached(str(path), st.st_mtime_ns, st.st_size)
    except (OSError, InvalidSafetensors):
        return UNKNOWN


# ------------------------------------------------------------------ Hugging Face / diffusers repos


def architecture_from_class_name(name: str | None) -> str | None:
    if not name:
        return None
    if name.startswith("ZImage"):
        return ZIMAGE
    if "XL" in name:
        return SDXL
    if name.startswith("StableDiffusion"):
        return SD15
    return None


def guess_repo_architecture(model_id: str) -> str | None:
    """Best guess for a diffusers repo id / local folder without network access:
    local model_index.json, then the Hugging Face cache, then the repo name."""
    candidates = [Path(model_id) / "model_index.json"]
    try:
        from huggingface_hub import try_to_load_from_cache

        cached = try_to_load_from_cache(model_id, "model_index.json")
        if isinstance(cached, str):
            candidates.append(Path(cached))
    except Exception:  # noqa: BLE001 - optional dependency, invalid repo ids
        pass
    for candidate in candidates:
        try:
            if candidate.is_file():
                data = json.loads(candidate.read_text(encoding="utf-8"))
                arch = architecture_from_class_name(data.get("_class_name"))
                if arch:
                    return arch
        except (OSError, ValueError, AttributeError):
            continue
    # only the last path segment ("org/repo" -> "repo", "C:\\Users\\alexl\\m" -> "m"), whole tokens
    name = re.split(r"[/\\]", model_id.rstrip("/\\"))[-1].lower().replace("_", "-")
    if re.search(r"(?:^|[-.])z-?image(?:$|[-.])", name):
        return ZIMAGE
    if re.search(r"(?:^|[-.])(?:sdxl|xl)(?:$|[-.])", name):
        return SDXL
    if re.search(r"(?:^|[-.])(?:sd15|sd-1-5|stable-diffusion-v1-5|v1-5)(?:$|[-.])", name):
        return SD15
    return None


def defaults_for(architecture: str | None, default_steps: int = 30) -> dict:
    """Generation defaults for the UI and for requests that omit them."""
    if architecture == ZIMAGE:
        return dict(ARCH_DEFAULTS[ZIMAGE])
    base = dict(ARCH_DEFAULTS.get(architecture or "", ARCH_DEFAULTS[SDXL]))
    base["steps"] = default_steps
    return base
