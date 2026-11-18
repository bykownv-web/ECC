"""DiffusersGenerator logic tested against fake torch/diffusers modules (no GPU needed)."""

import sys
import types
from pathlib import Path

import pytest
from PIL import Image

from app.backends import BackendUnavailable, DiffusersGenerator, GenerateParams


class FakePipe:
    def __init__(self, source):
        self.source = source
        self.log = []

    def to(self, device):
        self.log.append(("to", device))
        return self

    def enable_attention_slicing(self):
        pass

    def load_lora_weights(self, directory, weight_name, adapter_name):
        if weight_name.startswith("bad"):
            raise ValueError("shape mismatch")
        self.log.append(("load_lora", weight_name, adapter_name))

    def set_adapters(self, names, adapter_weights):
        self.log.append(("set_adapters", tuple(names), tuple(adapter_weights)))

    def unload_lora_weights(self):
        self.log.append(("unload",))

    def __call__(self, **kwargs):
        self.log.append(("call", kwargs["num_inference_steps"]))
        return types.SimpleNamespace(images=[Image.new("RGB", (kwargs["width"], kwargs["height"]))])


@pytest.fixture
def fake_modules(monkeypatch):
    loads = []

    class SDXL:
        @staticmethod
        def from_single_file(path, torch_dtype):
            if "sd15" in path:
                raise ValueError("not an SDXL checkpoint")
            loads.append(("sdxl", Path(path).name))
            return FakePipe(("sdxl", path))

    class SD15:
        @staticmethod
        def from_single_file(path, torch_dtype):
            if "broken" in path:
                raise ValueError("unknown architecture")
            loads.append(("sd15", Path(path).name))
            return FakePipe(("sd15", path))

    class Auto:
        @staticmethod
        def from_pretrained(model_id, **kwargs):
            loads.append(("hf", model_id))
            return FakePipe(("hf", model_id))

    diffusers = types.ModuleType("diffusers")
    diffusers.StableDiffusionXLPipeline = SDXL
    diffusers.StableDiffusionPipeline = SD15
    diffusers.AutoPipelineForText2Image = Auto

    class Gen:
        def __init__(self, device):
            pass

        def manual_seed(self, seed):
            return self

    torch = types.ModuleType("torch")
    torch.float16, torch.float32 = "fp16", "fp32"
    torch.Generator = Gen
    torch.cuda = types.SimpleNamespace(empty_cache=lambda: None, is_available=lambda: False)

    monkeypatch.setitem(sys.modules, "diffusers", diffusers)
    monkeypatch.setitem(sys.modules, "torch", torch)
    return loads


def params(**kw):
    return GenerateParams(prompt="p", width=64, height=64, steps=2, seed=1, **kw)


def test_default_model_loaded_once(fake_modules):
    gen = DiffusersGenerator("org/model", "cpu")
    gen.generate(params())
    gen.generate(params())
    assert fake_modules == [("hf", "org/model")]


def test_single_file_sdxl_and_switching(fake_modules):
    gen = DiffusersGenerator("org/model", "cpu")
    gen.generate(params(checkpoint=Path("/m/juggernautXL.safetensors")))
    gen.generate(params(checkpoint=Path("/m/juggernautXL.safetensors")))
    gen.generate(params(checkpoint=Path("/m/sd15-model.safetensors")))
    assert fake_modules == [("sdxl", "juggernautXL.safetensors"), ("sd15", "sd15-model.safetensors")]


def test_unsupported_checkpoint(fake_modules):
    gen = DiffusersGenerator("org/model", "cpu")
    with pytest.raises(BackendUnavailable, match="broken-sd15.safetensors"):
        gen.generate(params(checkpoint=Path("/m/broken-sd15.safetensors")))


def test_loras_applied_then_unloaded(fake_modules):
    gen = DiffusersGenerator("org/model", "cpu")
    gen.generate(params(loras=((Path("/l/a.safetensors"), 0.7), (Path("/l/b.safetensors"), 1.0))))
    log = gen._pipe.log
    assert ("load_lora", "a.safetensors", "lora0") in log
    assert ("set_adapters", ("lora0", "lora1"), (0.7, 1.0)) in log
    assert log.index(("call", 2)) < log.index(("unload",))


def test_incompatible_lora_reports_and_cleans_up(fake_modules):
    gen = DiffusersGenerator("org/model", "cpu")
    with pytest.raises(BackendUnavailable, match="bad.safetensors"):
        gen.generate(params(loras=((Path("/l/bad.safetensors"), 1.0),)))
    assert ("unload",) in gen._pipe.log
    assert not any(e[0] == "call" for e in gen._pipe.log)
