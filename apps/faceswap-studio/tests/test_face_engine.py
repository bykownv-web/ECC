"""onnxruntime face engine: pure numpy helpers, plus end-to-end runs on tiny fake ONNX models."""

import math
import zipfile
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

cv2 = pytest.importorskip("cv2")

from app import face_engine as fe  # noqa: E402
from app.backends import BackendUnavailable, InsightFaceSwapper, NoFaceFound  # noqa: E402
from app.config import APP_ROOT, Settings  # noqa: E402

# --------------------------------------------------------------------------- pure helpers


def test_distance2bbox_and_kps():
    points = np.array([[10.0, 20.0], [100.0, 50.0]])
    dist = np.array([[1.0, 2.0, 3.0, 4.0], [10.0, 10.0, 0.0, 5.0]])
    np.testing.assert_allclose(fe.distance2bbox(points, dist), [[9, 18, 13, 24], [90, 40, 100, 55]])
    kps = fe.distance2kps(points, np.array([[1.0, -1.0, 2.0, 3.0], [0.0, 0.0, -5.0, 5.0]]))
    np.testing.assert_allclose(kps, [[11, 19, 12, 23], [100, 50, 95, 55]])


def test_nms_suppresses_overlap_and_keeps_far_boxes():
    dets = np.array(
        [
            [0, 0, 100, 100, 0.8],
            [5, 5, 105, 105, 0.9],  # overlaps the first one, higher score
            [300, 300, 400, 400, 0.7],
            [0, 0, 100, 100, 0.6],  # duplicate of the first
        ],
        dtype=np.float32,
    )
    assert fe.nms(dets, 0.4) == [1, 2]
    assert fe.nms(dets[:0], 0.4) == []


def _similarity(points: np.ndarray, scale: float, angle_deg: float, shift) -> np.ndarray:
    a = math.radians(angle_deg)
    rot = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]]) * scale
    return (points @ rot.T + np.asarray(shift)).astype(np.float32)


@pytest.mark.parametrize("size", [112, 128])
def test_estimate_norm_maps_landmarks_onto_template(size):
    kps = _similarity(fe.ARCFACE_DST, 2.3, 17.0, (140.0, 75.0))
    m = fe.estimate_norm(kps, size)
    mapped = kps @ m[:, :2].T + m[:, 2]
    np.testing.assert_allclose(mapped, fe.arcface_template(size), atol=0.05)
    if size == 128:  # 128 template = 112 template shifted by 8 px on x
        np.testing.assert_allclose(fe.arcface_template(128), fe.ARCFACE_DST + [8.0, 0.0], atol=1e-5)


def test_norm_crop_moves_pixels_to_template():
    kps = _similarity(fe.ARCFACE_DST, 3.0, -12.0, (200.0, 120.0))
    img = np.zeros((600, 700, 3), np.uint8)
    x, y = np.round(kps[0]).astype(int)
    cv2.circle(img, (int(x), int(y)), 6, (255, 255, 255), -1)
    crop = fe.norm_crop(img, kps, 112)
    assert crop.shape == (112, 112, 3)
    ys, xs = np.where(crop[:, :, 0] > 128)
    assert abs(xs.mean() - fe.ARCFACE_DST[0, 0]) < 1.5
    assert abs(ys.mean() - fe.ARCFACE_DST[0, 1]) < 1.5


def test_paste_back_keeps_size_and_far_pixels():
    rng = np.random.default_rng(0)
    target = rng.integers(0, 255, (400, 500, 3), dtype=np.uint8)
    kps = _similarity(fe.arcface_template(128), 1.0, 5.0, (180.0, 120.0))
    aimg, m = fe.norm_crop2(target, kps, 128)
    fake = 255 - aimg
    out = fe.paste_back(target, fake, aimg, m)
    assert out.shape == target.shape and out.dtype == np.uint8
    np.testing.assert_array_equal(out[:60, :], target[:60, :])  # far above the face
    np.testing.assert_array_equal(out[:, 400:], target[:, 400:])  # far to the right
    centre = np.round(kps[2]).astype(int)  # nose: inside the eroded mask
    assert np.abs(out[centre[1], centre[0]].astype(int) - target[centre[1], centre[0]]).max() > 30


def test_paste_back_face_outside_image_is_noop():
    target = np.full((50, 50, 3), 7, np.uint8)
    m = np.array([[1.0, 0.0, 1000.0], [0.0, 1.0, 1000.0]])
    aimg = np.zeros((128, 128, 3), np.uint8)
    np.testing.assert_array_equal(fe.paste_back(target, aimg + 200, aimg, m), target)


def test_latent_from_embedding_is_unit_norm():
    rng = np.random.default_rng(1)
    emb = rng.normal(size=512).astype(np.float32)
    emb /= np.linalg.norm(emb)
    emap = rng.normal(size=(512, 512)).astype(np.float32)
    latent = fe.latent_from_embedding(emb, emap)
    assert latent.shape == (1, 512) and latent.dtype == np.float32
    assert np.linalg.norm(latent) == pytest.approx(1.0, abs=1e-5)
    expected = emb @ emap
    np.testing.assert_allclose(latent[0], expected / np.linalg.norm(expected), rtol=1e-4, atol=1e-6)


def test_select_providers():
    avail = ["CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"]
    assert fe.select_providers("cuda", avail) == avail
    assert fe.select_providers("cpu", avail) == ["CPUExecutionProvider"]
    assert fe.select_providers("cuda", ["CPUExecutionProvider"]) == ["CPUExecutionProvider"]
    assert fe.select_providers("auto", ["DmlExecutionProvider", "CPUExecutionProvider"]) == [
        "DmlExecutionProvider",
        "CPUExecutionProvider",
    ]


# --------------------------------------------------------------------------- buffalo_l files


@pytest.fixture
def no_legacy_pack(tmp_path, monkeypatch):
    monkeypatch.setattr(fe, "LEGACY_PACK_DIR", tmp_path / "no-home" / "buffalo_l")


def test_ensure_buffalo_l_uses_existing_and_legacy_dirs(tmp_path, monkeypatch):
    legacy = tmp_path / "home" / "buffalo_l"
    legacy.mkdir(parents=True)
    for name in fe.PACK_FILES:
        (legacy / name).write_bytes(b"x")
    monkeypatch.setattr(fe, "LEGACY_PACK_DIR", legacy)
    models = tmp_path / "models"
    assert fe.ensure_buffalo_l(models, url="http://127.0.0.1:9/never") == legacy
    local = models / "buffalo_l"
    local.mkdir(parents=True)
    for name in fe.PACK_FILES:
        (local / name).write_bytes(b"y")
    assert fe.ensure_buffalo_l(models, url="http://127.0.0.1:9/never") == local


def test_ensure_buffalo_l_extracts_only_needed_files(tmp_path, no_legacy_pack):
    archive = tmp_path / "pack.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("buffalo_l/det_10g.onnx", b"det")
        zf.writestr("w600k_r50.onnx", b"rec")
        zf.writestr("genderage.onnx", b"other")
        zf.writestr("../../evil/det_10g.onnx.txt", b"nope")
    models = tmp_path / "models"
    out = fe.ensure_buffalo_l(models, url=archive.as_uri())
    assert out == models / "buffalo_l"
    assert sorted(p.name for p in out.iterdir()) == ["det_10g.onnx", "w600k_r50.onnx"]
    assert (out / "det_10g.onnx").read_bytes() == b"det"


def test_ensure_buffalo_l_download_error_is_russian(tmp_path, no_legacy_pack):
    with pytest.raises(fe.FaceEngineError, match="Не удалось скачать"):
        fe.ensure_buffalo_l(tmp_path / "models", url=(tmp_path / "missing.zip").as_uri())
    assert not list((tmp_path / "models" / "buffalo_l").iterdir())  # no partial files left


# --------------------------------------------------------------------------- fake ONNX models

FACE_SCORE = 0.9
# One face at stride 32, grid cell (row 5, col 6): anchor centre (192, 160) in 640x640 input space
FACE_CELL, FACE_CENTRE = (5, 6), np.array([192.0, 160.0])
FACE_DIST = np.array([2.0, 2.0, 2.0, 2.0])  # in stride units -> 128x128 box
FACE_KPS = np.array([[-0.8, -0.6], [0.8, -0.6], [0.0, 0.2], [-0.6, 1.0], [0.6, 1.0]])  # stride units


def _onnx():
    return pytest.importorskip("onnx"), pytest.importorskip("onnxruntime")


def _save(model, path: Path) -> Path:
    onnx, _ = _onnx()
    model.opset_import[0].version = 13
    model.ir_version = 8
    onnx.checker.check_model(model)
    path.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, str(path))
    return path


def make_fake_detector(path: Path, with_face: bool = True) -> Path:
    """SCRFD-shaped model (9 outputs, non-batched) with constant predictions for a 640x640 input."""
    onnx, _ = _onnx()
    from onnx import TensorProto, helper, numpy_helper

    nodes, outputs = [], []
    groups = {"score": [], "bbox": [], "kps": []}
    for stride in (8, 16, 32):
        n = (640 // stride) ** 2 * 2
        scores = np.zeros((n, 1), np.float32)
        bbox = np.zeros((n, 4), np.float32)
        kps = np.zeros((n, 10), np.float32)
        scores[: n // 3] = 0.3  # below the 0.5 threshold everywhere
        if stride == 32 and with_face:
            idx = (FACE_CELL[0] * 20 + FACE_CELL[1]) * 2
            scores[idx], bbox[idx], kps[idx] = FACE_SCORE, FACE_DIST, FACE_KPS.ravel()
            # second anchor of the same cell: overlapping, lower score -> removed by NMS
            scores[idx + 1], bbox[idx + 1], kps[idx + 1] = 0.7, FACE_DIST * 1.1, FACE_KPS.ravel()
        for kind, arr in (("score", scores), ("bbox", bbox), ("kps", kps)):
            name = f"{kind}_{stride}"
            nodes.append(helper.make_node("Constant", [], [name], value=numpy_helper.from_array(arr)))
            groups[kind].append(helper.make_tensor_value_info(name, TensorProto.FLOAT, list(arr.shape)))
    outputs = groups["score"] + groups["bbox"] + groups["kps"]
    inp = helper.make_tensor_value_info("input.1", TensorProto.FLOAT, [1, 3, "h", "w"])
    graph = helper.make_graph(nodes, "fake_scrfd", [inp], outputs)
    return _save(helper.make_model(graph), path)


def make_fake_recognizer(path: Path) -> Path:
    onnx, _ = _onnx()
    from onnx import TensorProto, helper, numpy_helper

    rng = np.random.default_rng(3)
    w = numpy_helper.from_array(rng.normal(size=(3, 512)).astype(np.float32), "w")
    b = numpy_helper.from_array(rng.normal(size=(512,)).astype(np.float32), "b")
    nodes = [
        helper.make_node("GlobalAveragePool", ["input.1"], ["pooled"]),
        helper.make_node("Flatten", ["pooled"], ["flat"]),
        helper.make_node("Gemm", ["flat", "w", "b"], ["683"]),
    ]
    inp = helper.make_tensor_value_info("input.1", TensorProto.FLOAT, [1, 3, 112, 112])
    out = helper.make_tensor_value_info("683", TensorProto.FLOAT, [1, 512])
    return _save(helper.make_model(helper.make_graph(nodes, "fake_arcface", [inp], [out], [w, b])), path)


def make_fake_inswapper(path: Path) -> tuple[Path, np.ndarray]:
    """Same I/O names as inswapper_128 ('target', 'source' -> 'output'); output = 1 - target."""
    onnx, _ = _onnx()
    from onnx import TensorProto, helper, numpy_helper

    emap = np.random.default_rng(4).normal(size=(512, 512)).astype(np.float32)
    one = numpy_helper.from_array(np.ones((1,), np.float32), "one")
    nodes = [helper.make_node("Sub", ["one", "target"], ["output"])]
    inputs = [
        helper.make_tensor_value_info("target", TensorProto.FLOAT, [1, 3, 128, 128]),
        helper.make_tensor_value_info("source", TensorProto.FLOAT, [1, 512]),
    ]
    out = helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 3, 128, 128])
    # emap must be the LAST initializer, as in the real model
    inits = [one, numpy_helper.from_array(emap, "emap")]
    graph = helper.make_graph(nodes, "fake_inswapper", inputs, [out], inits)
    return _save(helper.make_model(graph), path), emap


@pytest.fixture
def fake_models(tmp_path, no_legacy_pack):
    models = tmp_path / "models"
    make_fake_detector(models / "buffalo_l" / fe.DET_FILE)
    make_fake_recognizer(models / "buffalo_l" / fe.REC_FILE)
    _, emap = make_fake_inswapper(models / "inswapper_128.onnx")
    return models, emap


def face_image(width=1280, height=960) -> np.ndarray:
    rng = np.random.default_rng(5)
    return rng.integers(0, 255, (height, width, 3), dtype=np.uint8)


def test_engine_detects_and_scales_back(fake_models):
    models, _ = fake_models
    engine = fe.FaceEngine(models, ["CPUExecutionProvider"], auto_download=False)
    faces = engine.detect(face_image(), with_embedding=True)  # 1280x960 -> det_scale 0.5
    assert len(faces) == 1
    face = faces[0]
    assert face.det_score == pytest.approx(FACE_SCORE)
    x1, y1 = FACE_CENTRE - FACE_DIST[:2] * 32
    x2, y2 = FACE_CENTRE + FACE_DIST[2:] * 32
    np.testing.assert_allclose(face.bbox, np.array([x1, y1, x2, y2]) / 0.5, atol=1e-3)
    np.testing.assert_allclose(face.kps, (FACE_CENTRE + FACE_KPS * 32) / 0.5, atol=1e-3)
    assert face.embedding.shape == (512,)
    assert np.linalg.norm(face.normed_embedding) == pytest.approx(1.0)


def test_engine_reads_emap_and_swaps(fake_models):
    models, emap = fake_models
    engine = fe.FaceEngine(models, ["CPUExecutionProvider"], auto_download=False)
    np.testing.assert_array_equal(engine.swapper.emap, emap)
    img = face_image()
    src = engine.embed(img, engine.detect(img)[0])
    target = engine.detect(img)[0]
    out = engine.swap(img, target, src)
    assert out.shape == img.shape
    np.testing.assert_array_equal(out[:100], img[:100])  # far from the face
    cx, cy = np.round(target.kps[2]).astype(int)
    assert not np.array_equal(out[cy - 5 : cy + 5, cx - 5 : cx + 5], img[cy - 5 : cy + 5, cx - 5 : cx + 5])
    with pytest.raises(ValueError):
        engine.swap(img, target, target)  # target has no embedding


def test_engine_without_pack_raises(tmp_path, no_legacy_pack):
    engine = fe.FaceEngine(tmp_path, auto_download=False)
    with pytest.raises(fe.FaceEngineError, match="Не найдены"):
        engine.detect(face_image(64, 64))


def test_insightface_swapper_end_to_end(fake_models):
    models, _ = fake_models
    settings = replace(Settings(), swap_model_path=models / "inswapper_128.onnx")
    swapper = InsightFaceSwapper(settings, "cpu")
    target = Image.fromarray(face_image()[:, :, ::-1])
    out, count = swapper.swap(target, target, all_faces=True)
    assert count == 1 and out.size == target.size and out.mode == "RGB"
    assert np.asarray(out).tobytes() != np.asarray(target).tobytes()


def test_insightface_swapper_no_face(tmp_path, no_legacy_pack):
    models = tmp_path / "models"
    make_fake_detector(models / "buffalo_l" / fe.DET_FILE, with_face=False)
    make_fake_recognizer(models / "buffalo_l" / fe.REC_FILE)
    make_fake_inswapper(models / "inswapper_128.onnx")
    swapper = InsightFaceSwapper(replace(Settings(), swap_model_path=models / "inswapper_128.onnx"), "cpu")
    img = Image.new("RGB", (320, 240))
    with pytest.raises(NoFaceFound, match="фото-источнике"):
        swapper.swap(img, img)


def test_insightface_swapper_missing_files(tmp_path, monkeypatch, no_legacy_pack):
    settings = replace(Settings(), swap_model_path=tmp_path / "inswapper_128.onnx")
    with pytest.raises(BackendUnavailable, match="не найден"):
        InsightFaceSwapper(settings, "cpu").swap(Image.new("RGB", (64, 64)), Image.new("RGB", (64, 64)))
    make_fake_inswapper(tmp_path / "inswapper_128.onnx")

    def offline(models_dir, url=fe.BUFFALO_L_URL):
        raise fe.FaceEngineError("Не удалось скачать модели (тест)")

    monkeypatch.setattr(fe, "ensure_buffalo_l", offline)
    with pytest.raises(BackendUnavailable, match="Не удалось скачать"):
        InsightFaceSwapper(settings, "cpu").swap(Image.new("RGB", (64, 64)), Image.new("RGB", (64, 64)))


# --------------------------------------------------------------------------- real weights


def _real_pack() -> Path | None:
    return fe.find_buffalo_l(APP_ROOT / "models")


@pytest.mark.integration
@pytest.mark.skipif(_real_pack() is None, reason="buffalo_l weights not present")
def test_real_buffalo_l_detector_and_recognizer():
    pytest.importorskip("onnxruntime")
    pack = _real_pack()
    engine = fe.FaceEngine(pack.parent, ["CPUExecutionProvider"], auto_download=False)
    assert engine.detect(np.zeros((480, 640, 3), np.uint8)) == []
    crop = np.random.default_rng(0).integers(0, 255, (112, 112, 3), dtype=np.uint8)
    emb = engine.recognizer.embed_crop(crop)
    assert emb.shape == (512,)
    assert np.linalg.norm(emb / np.linalg.norm(emb)) == pytest.approx(1.0)
