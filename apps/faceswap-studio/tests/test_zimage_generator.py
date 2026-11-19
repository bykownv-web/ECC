"""Z-Image Turbo loading in DiffusersGenerator, against fake torch/diffusers modules (no GPU).

The fakes mirror the diffusers API read from the source: ZImageTransformer2DModel.from_single_file
(FromOriginalModelMixin), ZImagePipeline.from_pretrained(repo, transformer=...), pipeline.components,
remove_all_hooks, enable_group_offload, callback_on_step_end + _interrupt."""

import sys
import types
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.backends import (
    ZIMAGE_DIFFUSERS_HINT,
    ZIMAGE_FP16_BLACK,
    Backends,
    BackendUnavailable,
    DiffusersGenerator,
    GenerateParams,
)
from app.config import Settings
from app.jobs import JobCancelled
from app.main import create_app
from tests.safetensors_fakes import (
    SDXL_KEYS,
    ZIMAGE_BUNDLE_KEYS,
    ZIMAGE_TRANSFORMER_KEYS,
    write_safetensors,
)
from tests.test_diffusers_generator import FakePipe, fake_modules  # noqa: F401 - fixture

BASE = "Tongyi-MAI/Z-Image-Turbo"
GPU_16GB = 17163091968  # what torch reports for a 16 GB card (16368 MiB)
GPU_24GB = 25393692672  # what torch reports for a 24 GB card (RTX 3090 / 4090), ~23.6 GiB


class FakeModule:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"<{self.name}>"


class FakeVae(FakeModule):
    def __init__(self, log):
        super().__init__("vae")
        self.log = log

    def enable_slicing(self):
        self.log.append(("vae.slicing",))

    def enable_tiling(self):
        self.log.append(("vae.tiling",))


class ZImagePipeline(FakePipe):
    """Named like the diffusers class: architecture detection looks at the class name."""

    def __init__(self, source, components):
        super().__init__(source)
        self._components = components
        self.vae = components["vae"]
        self.vae.log = self.log  # a reused VAE reports into the pipeline that owns it now

    @property
    def components(self):
        return dict(self._components)

    def remove_all_hooks(self):
        self.log.append(("remove_hooks",))

    def enable_group_offload(self, **kwargs):
        self.log.append(("group_offload", kwargs["offload_type"], tuple(kwargs["exclude_modules"])))


@pytest.fixture
def zi(fake_modules, monkeypatch):  # noqa: F811 - pytest fixture
    loads = fake_modules
    diffusers = sys.modules["diffusers"]
    state = types.SimpleNamespace(loads=loads, pretrained=[], transformers=[], pipes=[])

    class ZImageTransformer2DModel:
        @staticmethod
        def from_single_file(source, config, subfolder, torch_dtype):
            name = Path(source).name if isinstance(source, str) else f"dict:{len(source)}"
            state.transformers.append(
                {"source": source, "config": config, "subfolder": subfolder, "dtype": torch_dtype}
            )
            loads.append(("zimage-transformer", name))
            return FakeModule(f"transformer:{name}")

    class Pipeline:
        @staticmethod
        def from_pretrained(repo, torch_dtype, **components):
            state.pretrained.append({"repo": repo, "dtype": torch_dtype, **components})
            loads.append(("zimage-pipeline", repo))
            full = {
                "text_encoder": components.get("text_encoder") or FakeModule(f"te:{len(state.pretrained)}"),
                "tokenizer": components.get("tokenizer") or "tokenizer",
                "scheduler": components.get("scheduler") or "scheduler",
                "vae": components.get("vae") or FakeVae(None),
                "transformer": components.get("transformer") or FakeModule(f"transformer:{repo}"),
            }
            pipe = ZImagePipeline(("zimage", repo), full)
            state.pipes.append(pipe)
            return pipe

    diffusers.ZImagePipeline = Pipeline
    diffusers.ZImageTransformer2DModel = ZImageTransformer2DModel

    class SafeOpen:
        def __init__(self, path, framework):
            from app.model_arch import read_header

            self.keys_ = [k for k in read_header(Path(path)) if k != "__metadata__"]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def keys(self):
            return self.keys_

        def get_tensor(self, key):
            return f"tensor:{key}"

    safetensors = types.ModuleType("safetensors")
    safetensors.safe_open = SafeOpen
    monkeypatch.setitem(sys.modules, "safetensors", safetensors)
    return state


def zparams(path, **kw):
    return GenerateParams(
        **{
            "prompt": "p",
            "width": 1000,
            "height": 1000,
            "steps": 3,
            "seed": 1,
            "guidance": 0.0,
            "checkpoint": path,
            **kw,
        }
    )


@pytest.fixture
def zit(tmp_path):
    return write_safetensors(tmp_path / "zitFinetuneA.safetensors", ZIMAGE_TRANSFORMER_KEYS)


@pytest.fixture
def zit_b(tmp_path):
    comfy = [f"model.diffusion_model.{k}" for k in ZIMAGE_TRANSFORMER_KEYS]
    return write_safetensors(tmp_path / "zitFinetuneB.safetensors", comfy)


@pytest.fixture
def sdxl(tmp_path):
    return write_safetensors(tmp_path / "juggernautXL.safetensors", SDXL_KEYS)


# ---------------------------------------------------------------- loading


def test_transformer_only_checkpoint_uses_base_repo(zi, zit):
    sys.modules["torch"].vram_bytes = 32 * 2**30
    gen = DiffusersGenerator("org/model", "cuda", zimage_base=BASE)
    gen.generate(zparams(zit))
    assert zi.transformers == [
        {"source": str(zit), "config": BASE, "subfolder": "transformer", "dtype": "bf16"}
    ]
    (call,) = zi.pretrained
    assert call["repo"] == BASE and call["dtype"] == "bf16"
    assert repr(call["transformer"]) == "<transformer:zitFinetuneA.safetensors>"
    assert gen.architecture == "zimage"
    assert ("to", "cuda") in gen._pipe.log  # 32 GB: no offload


def test_zimage_bf16_even_without_native_bf16(zi, zit):
    sys.modules["torch"].bf16 = False  # pre-Ampere GPU: bf16 is emulated, slower but correct
    gen = DiffusersGenerator("org/model", "cuda", zimage_base=BASE)
    gen.generate(zparams(zit))  # FakePipe returns a black image: no fp16 check in bf16
    assert zi.transformers[0]["dtype"] == "bf16" and zi.pretrained[0]["dtype"] == "bf16"


def test_zimage_fp16_only_on_request_and_black_output_detected(zi, zit, monkeypatch):
    gen = DiffusersGenerator("org/model", "cuda", zimage_base=BASE, zimage_dtype="fp16")
    with pytest.raises(BackendUnavailable) as info:
        gen.generate(zparams(zit))  # FakePipe images are all black, as NaN output would be
    assert str(info.value) == ZIMAGE_FP16_BLACK and "FSS_ZIMAGE_DTYPE" in ZIMAGE_FP16_BLACK
    assert zi.transformers[0]["dtype"] == "fp16" and zi.pretrained[0]["dtype"] == "fp16"
    original = FakePipe.__call__

    def white(self, **kwargs):
        out = original(self, **kwargs)
        out.images[0].paste((255, 255, 255), (0, 0, 8, 8))
        return out

    monkeypatch.setattr(FakePipe, "__call__", white)
    image, _ = gen.generate(zparams(zit))
    assert image.getpixel((0, 0)) == (255, 255, 255)


def test_zimage_dtype_setting(monkeypatch):
    monkeypatch.setenv("FSS_ZIMAGE_DTYPE", "FP16")
    assert Settings.from_env().zimage_dtype == "fp16"
    monkeypatch.setenv("FSS_ZIMAGE_DTYPE", "bfloat16")
    assert Settings.from_env().zimage_dtype == "bf16"
    monkeypatch.setenv("FSS_ZIMAGE_DTYPE", "fp8")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_autopipeline_zimage_repo_reloaded_in_zimage_dtype(zi, zit, monkeypatch):
    def auto(model_id, **kwargs):
        zi.loads.append(("hf", model_id, kwargs["torch_dtype"]))
        return ZImagePipeline(("hf", model_id), {"vae": FakeVae(None), "text_encoder": FakeModule("fp16-te")})

    monkeypatch.setattr(sys.modules["diffusers"].AutoPipelineForText2Image, "from_pretrained", auto)
    gen = DiffusersGenerator("someone/turbo-mirror", "cuda", zimage_base="someone/turbo-mirror")
    gen.generate(zparams(None))
    assert zi.loads[0] == ("hf", "someone/turbo-mirror", "fp16")  # name did not reveal Z-Image
    assert zi.pretrained == [{"repo": "someone/turbo-mirror", "dtype": "bf16"}]  # reloaded in bf16
    assert gen.architecture == "zimage" and gen._pipe.source == ("zimage", "someone/turbo-mirror")
    gen.generate(zparams(zit))  # reuse now hands over bf16 components, not the fp16 ones
    assert repr(zi.pretrained[1]["text_encoder"]) != "<fp16-te>"


def test_zimage_bf16_on_cpu(zi, zit):
    gen = DiffusersGenerator("org/model", "cpu", zimage_base=BASE)
    gen.generate(zparams(zit))
    assert zi.transformers[0]["dtype"] == "bf16"


def test_base_components_reused_when_switching_finetunes(zi, zit, zit_b):
    sys.modules["torch"].vram_bytes = GPU_16GB
    gen = DiffusersGenerator("org/model", "cuda", zimage_base=BASE)
    gen.generate(zparams(zit))
    first = zi.pipes[0]
    gen.generate(zparams(zit_b))
    assert ("remove_hooks",) in first.log  # offload hooks dropped before the components move
    second = zi.pretrained[1]
    assert second["text_encoder"] is first.components["text_encoder"]
    assert second["vae"] is first.components["vae"]
    assert second["tokenizer"] == "tokenizer" and second["scheduler"] == "scheduler"
    assert repr(second["transformer"]) == "<transformer:zitFinetuneB.safetensors>"
    assert ("cpu_offload",) in gen._pipe.log


def test_base_not_reused_after_other_architecture(zi, zit, zit_b, sdxl):
    gen = DiffusersGenerator("org/model", "cpu", zimage_base=BASE)
    gen.generate(zparams(zit))
    gen.generate(zparams(sdxl, guidance=7.0))
    assert gen.architecture == "sdxl"
    gen.generate(zparams(zit_b))
    assert "text_encoder" not in zi.pretrained[1]
    assert [e[0] for e in zi.loads] == [
        "zimage-transformer",
        "zimage-pipeline",
        "sdxl",
        "zimage-transformer",
        "zimage-pipeline",
    ]


def test_default_model_can_be_zimage_repo(zi, zit):
    gen = DiffusersGenerator(BASE, "cpu", zimage_base=BASE)
    gen.generate(zparams(None))
    assert zi.pretrained[0] == {"repo": BASE, "dtype": "bf16"}  # no transformer override
    assert gen.architecture == "zimage"
    assert ("hf", BASE) not in zi.loads  # ZImagePipeline, not AutoPipeline + fp16 variant
    gen.generate(zparams(zit))  # fine-tune on top of the same base: keeps text encoder / VAE
    assert "text_encoder" in zi.pretrained[1]


def test_bundle_reads_only_transformer_tensors(zi, tmp_path):
    path = write_safetensors(tmp_path / "zitAllInOne.safetensors", ZIMAGE_BUNDLE_KEYS)
    gen = DiffusersGenerator("org/model", "cpu", zimage_base=BASE)
    gen.generate(zparams(path))
    source = zi.transformers[0]["source"]
    assert isinstance(source, dict)
    assert sorted(source) == sorted(f"model.diffusion_model.{k}" for k in ZIMAGE_TRANSFORMER_KEYS)


def test_scaled_fp8_rejected(zi, tmp_path):
    path = write_safetensors(tmp_path / "zitFp8.safetensors", [*ZIMAGE_TRANSFORMER_KEYS, "scaled_fp8"])
    gen = DiffusersGenerator("org/model", "cpu", zimage_base=BASE)
    with pytest.raises(BackendUnavailable, match="scaled fp8"):
        gen.generate(zparams(path))
    assert zi.loads == []


def test_old_diffusers_without_zimage(zi, zit):
    del sys.modules["diffusers"].ZImagePipeline
    gen = DiffusersGenerator("org/model", "cpu", zimage_base=BASE)
    with pytest.raises(BackendUnavailable) as info:
        gen.generate(zparams(zit))
    assert str(info.value) == ZIMAGE_DIFFUSERS_HINT


def test_transformer_load_error_is_readable(zi, zit, monkeypatch):
    def boom(*args, **kwargs):
        raise KeyError("x_embedder.weight")

    monkeypatch.setattr(sys.modules["diffusers"].ZImageTransformer2DModel, "from_single_file", boom)
    gen = DiffusersGenerator("org/model", "cpu", zimage_base=BASE)
    with pytest.raises(BackendUnavailable, match="zitFinetuneA.safetensors"):
        gen.generate(zparams(zit))


def test_base_download_error_is_readable(zi, zit, monkeypatch):
    def offline(*args, **kwargs):
        raise OSError("couldn't connect to huggingface.co")

    monkeypatch.setattr(sys.modules["diffusers"].ZImagePipeline, "from_pretrained", offline)
    gen = DiffusersGenerator("org/model", "cpu", zimage_base=BASE)
    with pytest.raises(BackendUnavailable, match="Tongyi-MAI/Z-Image-Turbo"):
        gen.generate(zparams(zit))


# ---------------------------------------------------------------- low VRAM per architecture


def test_16gb_card_sdxl_on_gpu_zimage_offloaded(zi, zit, sdxl):
    sys.modules["torch"].vram_bytes = GPU_16GB
    gen = DiffusersGenerator("org/model", "cuda", lowvram="auto", zimage_base=BASE)
    gen.generate(zparams(sdxl, guidance=7.0))
    assert gen.lowvram_active is False
    assert ("to", "cuda") in gen._pipe.log and ("cpu_offload",) not in gen._pipe.log
    gen.generate(zparams(zit))
    assert gen.lowvram_active is True
    log = gen._pipe.log
    assert ("cpu_offload",) in log and ("vae.slicing",) in log and ("vae.tiling",) in log
    assert not any(e[0] == "to" for e in log)


@pytest.mark.parametrize(
    "mode, vram, expect",
    [
        ("auto", 25 * 2**30, "none"),
        ("auto", GPU_24GB, "none"),  # ~21 GB of weights fit on a 24 GB card
        ("auto", 22 * 2**30 - 1, "model"),
        ("auto", GPU_16GB, "model"),
        ("auto", 12 * 2**30, "group"),
        ("on", 32 * 2**30, "model"),
        ("on", 8 * 2**30, "group"),
        ("off", 8 * 2**30, "none"),
    ],
)
def test_zimage_offload_plan(zi, zit, mode, vram, expect):
    sys.modules["torch"].vram_bytes = int(vram)
    gen = DiffusersGenerator("org/model", "cuda", lowvram=mode, zimage_base=BASE)
    gen.generate(zparams(zit))
    log = gen._pipe.log
    assert gen._offload == expect
    assert (("cpu_offload",) in log) == (expect == "model")
    assert (("group_offload", "block_level", ("vae",)) in log) == (expect == "group")
    assert (("to", "cuda") in log) == (expect == "none")


def test_group_offload_reloads_base(zi, zit, zit_b):
    sys.modules["torch"].vram_bytes = 12 * 2**30
    gen = DiffusersGenerator("org/model", "cuda", zimage_base=BASE)
    gen.generate(zparams(zit))
    gen.generate(zparams(zit_b))
    assert "text_encoder" not in zi.pretrained[1]  # hooked modules are not handed over


def test_health_reports_zimage(zi, zit, tmp_path):
    sys.modules["torch"].vram_bytes = GPU_16GB
    gen = DiffusersGenerator("org/model", "cuda", zimage_base=BASE)
    ckpt = zit.parent
    settings = replace(
        Settings(), backend="diffusers", nsfw_model_id="", output_dir=tmp_path / "out", checkpoints_dir=ckpt
    )
    client = TestClient(create_app(settings, Backends("diffusers", "cuda", gen)))
    assert client.get("/api/health").json()["lowvram_active"] is False  # SDXL-sized default model
    res = client.post("/api/generate", json={"prompt": "x", "model": zit.name, "width": 1000, "height": 776})
    assert res.status_code == 200, res.text
    health = client.get("/api/health").json()
    assert health["architecture"] == "zimage" and health["lowvram_active"] is True
    kwargs = gen._pipe.last_kwargs
    assert kwargs["num_inference_steps"] == 8 and kwargs["guidance_scale"] == 0.0
    assert (kwargs["width"], kwargs["height"]) == (992, 768)


# ---------------------------------------------------------------- generation call


def test_zimage_call_kwargs(zi, zit):
    gen = DiffusersGenerator("org/model", "cpu", zimage_base=BASE)
    gen.generate(zparams(zit, negative_prompt="blurry", width=1000, height=1000))
    kwargs = gen._pipe.last_kwargs
    assert kwargs["negative_prompt"] is None  # CFG 0: Turbo ignores it, so it is not sent
    assert (kwargs["width"], kwargs["height"]) == (992, 992)  # multiples of 16
    assert kwargs["callback_on_step_end_tensor_inputs"] == ["latents"]
    gen.generate(zparams(zit, negative_prompt="blurry", guidance=2.0))
    assert gen._pipe.last_kwargs["negative_prompt"] == "blurry"


def test_zimage_progress_and_cancel(zi, zit):
    gen = DiffusersGenerator("org/model", "cpu", zimage_base=BASE)
    events = []
    gen.generate(zparams(zit, steps=3), progress=lambda s, f: events.append((s, f)))
    assert events[0] == ("Загрузка модели…", 0.0)
    assert events[-1] == ("Генерация: шаг 3/3", 1.0)

    def cancel(stage, frac):
        if stage == "Генерация: шаг 1/4":
            raise JobCancelled("stop")

    with pytest.raises(JobCancelled):
        gen.generate(zparams(zit, steps=4), progress=cancel)
    assert gen._pipe._interrupt is True
    assert [e for e in gen._pipe.log if e[0] == "step"][-1] == ("step", 0)


def test_zimage_lora_supported(zi, zit):
    gen = DiffusersGenerator("org/model", "cpu", zimage_base=BASE)
    gen.generate(zparams(zit, loras=((Path("/l/zitStyle.safetensors"), 0.8),)))
    assert ("load_lora", "zitStyle.safetensors", "lora0") in gen._pipe.log
    assert ("unload",) in gen._pipe.log


def test_set_adapters_failure_unloads_and_next_generation_works(zi, zit, monkeypatch):
    # peft only warns when no LoRA key matches, then set_adapters raises ValueError
    def set_adapters(self, names, adapter_weights):
        if any(n in self._loaded for n in names):
            raise ValueError("Adapter name lora0 already in use")  # what a leftover would cause
        if self.fail_adapters:
            raise ValueError("Adapter name(s) {'lora1'} not in the list of present adapters")
        self.log.append(("set_adapters", tuple(names), tuple(adapter_weights)))

    def load(self, directory, weight_name, adapter_name):
        self.log.append(("load_lora", weight_name, adapter_name))

    def unload(self):
        self.log.append(("unload",))
        self._loaded = set()

    monkeypatch.setattr(FakePipe, "_loaded", set(), raising=False)
    monkeypatch.setattr(FakePipe, "fail_adapters", True, raising=False)
    monkeypatch.setattr(FakePipe, "set_adapters", set_adapters)
    monkeypatch.setattr(FakePipe, "load_lora_weights", load)
    monkeypatch.setattr(FakePipe, "unload_lora_weights", unload)
    gen = DiffusersGenerator("org/model", "cpu", zimage_base=BASE)
    loras = ((Path("/l/a.safetensors"), 1.0), (Path("/l/b.safetensors"), 0.5))
    with pytest.raises(BackendUnavailable) as info:
        gen.generate(zparams(zit, loras=loras))
    assert str(info.value).startswith("LoRA не подходит к выбранной модели (a.safetensors, b.safetensors")
    assert ("unload",) in gen._pipe.log
    assert not any(e[0] == "call" for e in gen._pipe.log)
    monkeypatch.setattr(FakePipe, "fail_adapters", False, raising=False)
    gen.generate(zparams(zit, loras=loras))
    assert ("set_adapters", ("lora0", "lora1"), (1.0, 0.5)) in gen._pipe.log
    gen.generate(zparams(zit))  # and without LoRA


def test_zimage_lora_mismatch_message(zi, zit):
    gen = DiffusersGenerator("org/model", "cpu", zimage_base=BASE)
    with pytest.raises(BackendUnavailable, match="Z-Image Turbo"):
        gen.generate(zparams(zit, loras=((Path("/l/bad.safetensors"), 1.0),)))


def test_flux_checkpoint_rejected_by_backend(zi, tmp_path):
    from tests.safetensors_fakes import FLUX_KEYS

    path = write_safetensors(tmp_path / "flux1-dev.safetensors", FLUX_KEYS)
    gen = DiffusersGenerator("org/model", "cpu", zimage_base=BASE)
    with pytest.raises(BackendUnavailable, match="FLUX, такие модели приложение не поддерживает"):
        gen.generate(zparams(path))
    assert zi.loads == []
