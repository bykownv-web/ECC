import io
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.main import create_app
from app.safety import ContentBlocked, check_prompt


@pytest.fixture
def settings(tmp_path):
    return replace(Settings(), backend="mock", output_dir=tmp_path)


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings))


def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["backend"] == "mock"


def test_index_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "Image Studio" in res.text


def test_generate_returns_watermarked_png_with_metadata(client):
    res = client.post(
        "/api/generate", json={"prompt": "a red fox in snow", "width": 300, "height": 260, "seed": 7}
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["seed"] == 7
    img_res = client.get(data["url"])
    assert img_res.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(img_res.content))
    # sizes snapped to multiples of 8
    assert img.size == (296, 256)
    assert img.info["ai_generated"] == "true"
    assert img.info["prompt"] == "a red fox in snow"


def test_generate_is_deterministic_for_seed(client):
    a = client.post("/api/generate", json={"prompt": "x", "width": 256, "height": 256, "seed": 1}).json()
    b = client.post("/api/generate", json={"prompt": "x", "width": 256, "height": 256, "seed": 1}).json()
    assert client.get(a["url"]).content == client.get(b["url"]).content


@pytest.mark.parametrize("prompt", ["nude woman on beach", "голая девушка", "a child in underwear"])
def test_generate_blocks_disallowed_prompts(client, prompt):
    res = client.post("/api/generate", json={"prompt": prompt})
    assert res.status_code == 422


@pytest.mark.parametrize("prompt", ["детализированный портрет", "sussex countryside", "essex landscape"])
def test_prompt_filter_allows_benign(prompt):
    check_prompt(prompt)


def test_prompt_filter_extra_terms():
    with pytest.raises(ContentBlocked):
        check_prompt("photo of Some Celebrity", extra_terms=["some celebrity"])


@pytest.mark.parametrize("bad_id", ["../secret", "abc", "0" * 31 + "g"])
def test_image_id_validation(client, bad_id):
    assert client.get(f"/api/images/{bad_id}").status_code == 404


def test_delete_image(client):
    url = client.post("/api/generate", json={"prompt": "tree", "width": 256, "height": 256}).json()["url"]
    assert client.delete(url).status_code == 204
    assert client.get(url).status_code == 404


# ---------------------------------------------------------------- local models / LoRA


@pytest.fixture
def models_client(tmp_path):
    ckpt, loras = tmp_path / "checkpoints", tmp_path / "loras"
    ckpt.mkdir()
    loras.mkdir()
    (ckpt / "juggernautXL_ragnarok.safetensors").write_bytes(b"x" * 1024)
    (ckpt / "notes.txt").write_text("ignored")
    (loras / "edgCrystalButterflyv1.safetensors").write_bytes(b"x")
    (tmp_path / "outside.safetensors").write_bytes(b"x")
    s = replace(
        Settings(), backend="mock", output_dir=tmp_path / "out", checkpoints_dir=ckpt, loras_dir=loras
    )
    app = create_app(s)
    calls = []
    original = app.state.backends.generator.generate

    def spy(params):
        calls.append(params)
        return original(params)

    app.state.backends.generator.generate = spy
    return TestClient(app), calls


def test_models_listing(models_client):
    client, _ = models_client
    body = client.get("/api/models").json()
    assert [c["name"] for c in body["checkpoints"]] == ["juggernautXL_ragnarok.safetensors"]
    assert [lo["name"] for lo in body["loras"]] == ["edgCrystalButterflyv1.safetensors"]


def test_models_listing_without_dirs(client):
    body = client.get("/api/models").json()
    assert body["checkpoints"] == [] and body["loras"] == []


def test_generate_with_checkpoint_and_lora(models_client):
    client, calls = models_client
    res = client.post(
        "/api/generate",
        json={
            "prompt": "crystal butterfly",
            "width": 256,
            "height": 256,
            "model": "juggernautXL_ragnarok.safetensors",
            "loras": [{"name": "edgCrystalButterflyv1.safetensors", "weight": 0.6}],
        },
    )
    assert res.status_code == 200, res.text
    params = calls[-1]
    assert params.checkpoint.name == "juggernautXL_ragnarok.safetensors"
    assert [(p.name, w) for p, w in params.loras] == [("edgCrystalButterflyv1.safetensors", 0.6)]


@pytest.mark.parametrize(
    "model", ["missing.safetensors", "../outside.safetensors", "notes.txt", "/etc/passwd"]
)
def test_generate_rejects_unknown_or_unsafe_model(models_client, model):
    client, _ = models_client
    res = client.post("/api/generate", json={"prompt": "x", "model": model})
    assert res.status_code == 400


def test_generate_rejects_unknown_lora_and_too_many(models_client):
    client, _ = models_client
    bad = client.post("/api/generate", json={"prompt": "x", "loras": [{"name": "nope.safetensors"}]})
    assert bad.status_code == 400
    many = [{"name": "edgCrystalButterflyv1.safetensors"}] * 6
    assert client.post("/api/generate", json={"prompt": "x", "loras": many}).status_code == 400


@pytest.mark.parametrize(
    "path", ["/api/swap", "/api/generate-swap", "/api/jobs/swap", "/api/jobs/generate-swap"]
)
def test_face_swap_endpoints_removed(client, path):
    assert client.post(path).status_code in (404, 405)


def test_health_has_no_face_swap_fields(client):
    body = client.get("/api/health").json()
    assert not any("swap" in key for key in body)
