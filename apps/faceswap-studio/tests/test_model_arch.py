"""Architecture detection from .safetensors headers (no torch, no weights)."""

import json
import struct

import pytest

from app.model_arch import (
    InvalidSafetensors,
    defaults_for,
    detect_architecture,
    detect_lora_architecture,
    guess_repo_architecture,
    inspect_checkpoint,
    read_header,
)
from tests.safetensors_fakes import (
    LUMINA2_KEYS,
    SD15_KEYS,
    SD15_LORA_KEYS,
    SDXL_KEYS,
    SDXL_LORA_KEYS,
    ZIMAGE_BUNDLE_KEYS,
    ZIMAGE_LORA_KEYS,
    ZIMAGE_TRANSFORMER_KEYS,
    write_safetensors,
)


@pytest.mark.parametrize(
    "keys, arch",
    [
        (SDXL_KEYS, "sdxl"),
        (SD15_KEYS, "sd15"),
        (ZIMAGE_TRANSFORMER_KEYS, "zimage"),
        (ZIMAGE_BUNDLE_KEYS, "zimage"),
        (LUMINA2_KEYS, "unknown"),
        (["some.random.weight"], "unknown"),
    ],
)
def test_detect_architecture(tmp_path, keys, arch):
    assert detect_architecture(write_safetensors(tmp_path / "m.safetensors", keys)) == arch


def test_zimage_layouts(tmp_path):
    transformer = inspect_checkpoint(write_safetensors(tmp_path / "t.safetensors", ZIMAGE_TRANSFORMER_KEYS))
    assert (transformer.layout, transformer.prefix, transformer.fp8_scaled) == ("transformer", "", False)
    comfy = [f"model.diffusion_model.{k}" for k in ZIMAGE_TRANSFORMER_KEYS]
    comfy_info = inspect_checkpoint(write_safetensors(tmp_path / "c.safetensors", comfy))
    assert (comfy_info.layout, comfy_info.prefix) == ("transformer", "model.diffusion_model.")
    bundle = inspect_checkpoint(write_safetensors(tmp_path / "b.safetensors", ZIMAGE_BUNDLE_KEYS))
    assert (bundle.architecture, bundle.layout) == ("zimage", "bundle")
    assert bundle.prefix == "model.diffusion_model."
    scaled = inspect_checkpoint(
        write_safetensors(tmp_path / "s.safetensors", [*ZIMAGE_TRANSFORMER_KEYS, "scaled_fp8"])
    )
    assert scaled.fp8_scaled is True


def test_header_metadata_is_not_a_key(tmp_path):
    path = write_safetensors(tmp_path / "m.safetensors", SDXL_KEYS, metadata={"format": "pt"})
    assert read_header(path)["__metadata__"] == {"format": "pt"}
    assert detect_architecture(path) == "sdxl"


def _short(path):
    path.write_bytes(b"\x01\x02")
    return path


def _raw(path, length: int, body: bytes):
    path.write_bytes(struct.pack("<Q", length) + body)
    return path


@pytest.mark.parametrize(
    "make",
    [
        lambda p: _short(p),  # shorter than the length field
        lambda p: _raw(p, 200 * 2**20, b"{}"),  # header length above the cap
        lambda p: _raw(p, 1000, b"{}"),  # length beyond the end of the file
        lambda p: _raw(p, 0, b""),  # empty header
        lambda p: _raw(p, 9, b"not json!"),
        lambda p: _raw(p, 7, b"[1,2,3]"),  # JSON, but not an object
        lambda p: _raw(p, 4, b"\xff\xfe\xfd\xfc"),  # not UTF-8
    ],
)
def test_corrupted_headers(tmp_path, make):
    path = make(tmp_path / "bad.safetensors")
    with pytest.raises(InvalidSafetensors):
        read_header(path)
    assert detect_architecture(path) == "unknown"
    assert detect_lora_architecture(path) == "unknown"


def test_header_cap_is_configurable(tmp_path):
    body = json.dumps({"a": {"dtype": "F32", "shape": [], "data_offsets": [0, 0]}}).encode()
    path = _raw(tmp_path / "m.safetensors", len(body), body)
    assert "a" in read_header(path)
    with pytest.raises(InvalidSafetensors):
        read_header(path, max_bytes=10)


def test_missing_file_is_unknown(tmp_path):
    assert detect_architecture(tmp_path / "missing.safetensors") == "unknown"


def test_detection_cache_follows_file_changes(tmp_path):
    path = write_safetensors(tmp_path / "m.safetensors", SDXL_KEYS)
    assert detect_architecture(path) == "sdxl"
    write_safetensors(path, [*ZIMAGE_TRANSFORMER_KEYS, "extra.weight"])
    assert detect_architecture(path) == "zimage"


@pytest.mark.parametrize(
    "keys, metadata, arch",
    [
        (SDXL_LORA_KEYS, None, "sdxl"),
        (SD15_LORA_KEYS, None, "sd15"),
        (ZIMAGE_LORA_KEYS, None, "zimage"),
        (["lora_unet_layers_0_attention_to_q.lora_down.weight"], None, "zimage"),
        (["something.lora_down.weight"], {"ss_base_model_version": "sdxl_base_v1-0"}, "sdxl"),
        (["something.lora_down.weight"], {"ss_base_model_version": "sd_v1"}, "sd15"),
        (["something.lora_down.weight"], None, "unknown"),
    ],
)
def test_detect_lora_architecture(tmp_path, keys, metadata, arch):
    path = write_safetensors(tmp_path / "l.safetensors", keys, metadata=metadata)
    assert detect_lora_architecture(path) == arch


def test_guess_repo_architecture(tmp_path):
    # no network: name heuristics and local model_index.json only
    assert guess_repo_architecture("Tongyi-MAI/Z-Image-Turbo") == "zimage"
    assert guess_repo_architecture("stabilityai/stable-diffusion-xl-base-1.0") == "sdxl"
    assert guess_repo_architecture("org/model") is None
    local = tmp_path / "my-model"
    local.mkdir()
    (local / "model_index.json").write_text(json.dumps({"_class_name": "ZImagePipeline"}))
    assert guess_repo_architecture(str(local)) == "zimage"


def test_defaults_per_architecture():
    assert defaults_for("zimage", 30) == {"steps": 8, "guidance": 0.0, "width": 1024, "height": 1024}
    assert defaults_for("sdxl", 25) == {"steps": 25, "guidance": 7.0, "width": 1024, "height": 1024}
    assert defaults_for("sd15", 30)["width"] == 512
    assert defaults_for(None, 30) == defaults_for("sdxl", 30)
