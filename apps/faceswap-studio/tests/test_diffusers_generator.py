"""DiffusersGenerator logic tested against fake torch/diffusers modules (no GPU needed)."""

import sys
import types
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.backends import (
    OOM_HINT,
    Backends,
    BackendUnavailable,
    DiffusersGenerator,
    GenerateParams,
)
from app.config import Settings
from app.jobs import JobCancelled
from app.main import create_app


class FakeOOM(RuntimeError):
    pass


class FakePipe:
    # set by tests: raise FakeOOM on call
    oom = False

    def __init__(self, source):
        self.source = source
        self.log = []
        self._interrupt = False

    def to(self, device):
        self.log.append(("to", device))
        return self

    def enable_model_cpu_offload(self):
        self.log.append(("cpu_offload",))

    def enable_vae_slicing(self):
        self.log.append(("vae_slicing",))

    def enable_vae_tiling(self):
        self.log.append(("vae_tiling",))

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
        steps = kwargs["num_inference_steps"]
        self.log.append(("call", steps))
        if FakePipe.oom:
            raise FakeOOM("CUDA out of memory")
        # mimics the diffusers denoising loop: callback after each step, honour _interrupt
        self._interrupt = False
        callback = kwargs.get("callback_on_step_end")
        for i in range(steps):
            if self._interrupt:
                break
            self.log.append(("step", i))
            if callback is not None:
                out = callback(self, i, 1000 - i, {"latents": "z"})
                assert out == {"latents": "z"}
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
    cache_clears = []
    torch.vram_bytes = 24 * 2**30
    torch.cache_clears = cache_clears
    torch.cuda = types.SimpleNamespace(
        empty_cache=lambda: cache_clears.append(1),
        is_available=lambda: False,
        get_device_properties=lambda index: types.SimpleNamespace(total_memory=torch.vram_bytes),
        OutOfMemoryError=FakeOOM,
    )

    monkeypatch.setitem(sys.modules, "diffusers", diffusers)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setattr(FakePipe, "oom", False)
    return loads


def params(**kw):
    return GenerateParams(**{"prompt": "p", "width": 64, "height": 64, "steps": 2, "seed": 1, **kw})


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


# ---------------------------------------------------------------- progress / cancel


def test_progress_reports_loading_then_steps(fake_modules):
    gen = DiffusersGenerator("org/model", "cpu")
    events = []
    gen.generate(params(steps=3), progress=lambda stage, frac: events.append((stage, frac)))
    assert events[0] == ("Загрузка модели…", 0.0)
    assert events[-3:] == [
        ("Генерация: шаг 1/3", 1 / 3),
        ("Генерация: шаг 2/3", 2 / 3),
        ("Генерация: шаг 3/3", 1.0),
    ]
    # already loaded: no loading stage the second time
    events.clear()
    gen.generate(params(steps=1), progress=lambda stage, frac: events.append((stage, frac)))
    assert "Загрузка модели…" not in [e[0] for e in events]


def test_cancel_sets_interrupt_and_raises(fake_modules):
    gen = DiffusersGenerator("org/model", "cpu")

    def progress(stage, frac):
        if stage == "Генерация: шаг 2/5":
            raise JobCancelled("stop")

    with pytest.raises(JobCancelled):
        gen.generate(params(steps=5), progress=progress)
    steps_run = [e for e in gen._pipe.log if e[0] == "step"]
    assert steps_run == [("step", 0), ("step", 1)]  # loop interrupted after the cancelling step
    assert gen._pipe._interrupt is True


def test_cancel_before_load_skips_loading(fake_modules):
    gen = DiffusersGenerator("org/model", "cpu")

    def progress(stage, frac):
        raise JobCancelled("stop")

    with pytest.raises(JobCancelled):
        gen.generate(params(), progress=progress)
    assert fake_modules == []


# ---------------------------------------------------------------- low VRAM / OOM / architecture


@pytest.mark.parametrize(
    "mode, vram_gb, expect_offload",
    [("auto", 8, True), ("auto", 12, False), ("on", 24, True), ("off", 6, False)],
)
def test_lowvram_selection(fake_modules, mode, vram_gb, expect_offload):
    sys.modules["torch"].vram_bytes = vram_gb * 2**30
    gen = DiffusersGenerator("org/model", "cuda", lowvram=mode)
    gen.generate(params())
    log = gen._pipe.log
    assert gen.lowvram_active is expect_offload
    if expect_offload:
        assert ("cpu_offload",) in log and ("vae_slicing",) in log and ("vae_tiling",) in log
        assert not any(e[0] == "to" for e in log)
    else:
        assert ("to", "cuda") in log and ("cpu_offload",) not in log


def test_lowvram_never_on_cpu(fake_modules):
    gen = DiffusersGenerator("org/model", "cpu", lowvram="on")
    gen.generate(params())
    assert gen.lowvram_active is False
    assert ("to", "cpu") in gen._pipe.log


def test_lowvram_without_vae_helpers(fake_modules, monkeypatch):
    monkeypatch.delattr(FakePipe, "enable_vae_tiling")
    gen = DiffusersGenerator("org/model", "cuda", lowvram="on")
    gen.generate(params())
    assert ("vae_slicing",) in gen._pipe.log
    assert ("vae_tiling",) not in gen._pipe.log


def test_oom_maps_to_backend_unavailable(fake_modules, monkeypatch):
    gen = DiffusersGenerator("org/model", "cuda", lowvram="off")
    gen.generate(params())
    monkeypatch.setattr(FakePipe, "oom", True)
    clears_before = len(sys.modules["torch"].cache_clears)
    with pytest.raises(BackendUnavailable) as info:
        gen.generate(params(loras=((Path("/l/a.safetensors"), 1.0),)))
    assert str(info.value) == OOM_HINT
    assert "768×768" in OOM_HINT and "FSS_LOWVRAM=on" in OOM_HINT
    assert len(sys.modules["torch"].cache_clears) > clears_before
    assert ("unload",) in gen._pipe.log  # LoRA cleaned up even on OOM


def test_oom_guard_without_attribute(fake_modules, monkeypatch):
    del sys.modules["torch"].cuda.OutOfMemoryError
    monkeypatch.setattr(FakePipe, "oom", True)
    gen = DiffusersGenerator("org/model", "cpu")
    with pytest.raises(FakeOOM):
        gen.generate(params())


def test_architecture_reported(fake_modules):
    gen = DiffusersGenerator("org/model", "cpu")
    assert gen.architecture is None
    gen.generate(params(checkpoint=Path("/m/juggernautXL.safetensors")))
    assert gen.architecture == "sdxl"
    gen.generate(params(checkpoint=Path("/m/sd15-model.safetensors")))
    assert gen.architecture == "sd15"
    gen.generate(params())
    assert gen.architecture is None  # FakePipe's class name says nothing about the architecture


def test_health_and_jobs_with_diffusers_generator(fake_modules, tmp_path):
    sys.modules["torch"].vram_bytes = 8 * 2**30
    gen = DiffusersGenerator("org/model", "cuda")
    settings = replace(Settings(), backend="diffusers", nsfw_model_id="", output_dir=tmp_path)
    client = TestClient(create_app(settings, Backends("diffusers", "cuda", gen)))
    assert client.get("/api/health").json()["lowvram_active"] is True
    job_id = client.post("/api/jobs/generate", json={"prompt": "x", "width": 256, "height": 256, "steps": 2})
    job_id = job_id.json()["job_id"]
    assert client.app.state.jobs.get(job_id).wait(10)
    body = client.get(f"/api/jobs/{job_id}").json()
    assert body["status"] == "done", body
    assert body["stage"] == "Готово"
    health = client.get("/api/health").json()
    assert health["lowvram"] == "auto" and health["architecture"] is None


def test_oom_job_reports_503_hint(fake_modules, tmp_path, monkeypatch):
    monkeypatch.setattr(FakePipe, "oom", True)
    gen = DiffusersGenerator("org/model", "cuda", lowvram="off")
    settings = replace(Settings(), backend="diffusers", nsfw_model_id="", output_dir=tmp_path)
    client = TestClient(create_app(settings, Backends("diffusers", "cuda", gen)))
    job_id = client.post("/api/jobs/generate", json={"prompt": "x", "width": 256, "height": 256}).json()[
        "job_id"
    ]
    assert client.app.state.jobs.get(job_id).wait(10)
    assert client.get(f"/api/jobs/{job_id}").json()["error"] == {"code": 503, "detail": OOM_HINT}
    res = client.post("/api/generate", json={"prompt": "x", "width": 256, "height": 256})
    assert res.status_code == 503 and res.json()["detail"] == OOM_HINT
