"""Face detection, recognition and swapping on plain onnxruntime + numpy + OpenCV.

Re-implements the parts of the ``insightface`` package the app needs (SCRFD detector,
ArcFace embedding, inswapper_128 paste-back) so that no C++ build tools are required:
onnxruntime, onnx and opencv-python-headless all ship Windows wheels.
"""

from __future__ import annotations

import logging
import shutil
import threading
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger("faceswap.face_engine")

BUFFALO_L_URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
DET_FILE = "det_10g.onnx"
REC_FILE = "w600k_r50.onnx"
PACK_FILES = (DET_FILE, REC_FILE)
LEGACY_PACK_DIR = Path.home() / ".insightface" / "models" / "buffalo_l"

# Standard ArcFace 5-point template (eyes, nose, mouth corners) for a 112x112 crop
ARCFACE_DST = np.array(
    [
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ],
    dtype=np.float32,
)


class FaceEngineError(Exception):
    """Model files or runtime dependencies are missing (message is user-facing, Russian)."""


@dataclass
class Face:
    bbox: np.ndarray  # (4,) x1, y1, x2, y2
    kps: np.ndarray  # (5, 2) landmarks
    det_score: float
    embedding: np.ndarray | None = None  # (512,)

    @property
    def area(self) -> float:
        return float(max(0.0, self.bbox[2] - self.bbox[0]) * max(0.0, self.bbox[3] - self.bbox[1]))

    @property
    def normed_embedding(self) -> np.ndarray:
        if self.embedding is None:
            raise ValueError("face has no embedding")
        return self.embedding / np.linalg.norm(self.embedding)


# --------------------------------------------------------------------------- pure numpy helpers


def distance2bbox(points: np.ndarray, distance: np.ndarray) -> np.ndarray:
    """Anchor centres (N, 2) + (left, top, right, bottom) distances (N, 4) -> boxes (N, 4)."""
    x1 = points[:, 0] - distance[:, 0]
    y1 = points[:, 1] - distance[:, 1]
    x2 = points[:, 0] + distance[:, 2]
    y2 = points[:, 1] + distance[:, 3]
    return np.stack([x1, y1, x2, y2], axis=-1)


def distance2kps(points: np.ndarray, distance: np.ndarray) -> np.ndarray:
    """Anchor centres (N, 2) + keypoint offsets (N, 2K) -> keypoints (N, 2K)."""
    preds = []
    for i in range(0, distance.shape[1], 2):
        preds.append(points[:, i % 2] + distance[:, i])
        preds.append(points[:, i % 2 + 1] + distance[:, i + 1])
    return np.stack(preds, axis=-1)


def nms(dets: np.ndarray, thresh: float = 0.4) -> list[int]:
    """Greedy NMS over rows (x1, y1, x2, y2, score); returns kept row indices."""
    x1, y1, x2, y2, scores = (dets[:, i] for i in range(5))
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0.0, xx2 - xx1 + 1) * np.maximum(0.0, yy2 - yy1 + 1)
        ovr = inter / (areas[i] + areas[order[1:]] - inter)
        order = order[np.where(ovr <= thresh)[0] + 1]
    return keep


def arcface_template(image_size: int) -> np.ndarray:
    """Template for a square crop: 112*k scales the 112 template, 128*k also shifts x by 8*k."""
    if image_size % 112 == 0:
        ratio, diff_x = image_size / 112.0, 0.0
    elif image_size % 128 == 0:
        ratio = image_size / 128.0
        diff_x = 8.0 * ratio
    else:
        raise ValueError(f"unsupported crop size {image_size}")
    dst = ARCFACE_DST * ratio
    dst[:, 0] += diff_x
    return dst


def estimate_norm(kps: np.ndarray, image_size: int = 112) -> np.ndarray:
    """2x3 similarity transform mapping the 5 landmarks onto the ArcFace template."""
    import cv2

    dst = arcface_template(image_size)
    m, _ = cv2.estimateAffinePartial2D(np.asarray(kps, dtype=np.float32).reshape(5, 2), dst, method=cv2.LMEDS)
    if m is None:
        raise ValueError("could not estimate face alignment")
    return m


def norm_crop2(img: np.ndarray, kps: np.ndarray, image_size: int = 112) -> tuple[np.ndarray, np.ndarray]:
    import cv2

    m = estimate_norm(kps, image_size)
    warped = cv2.warpAffine(img, m, (image_size, image_size), borderValue=0.0)
    return warped, m


def norm_crop(img: np.ndarray, kps: np.ndarray, image_size: int = 112) -> np.ndarray:
    return norm_crop2(img, kps, image_size)[0]


def latent_from_embedding(normed_embedding: np.ndarray, emap: np.ndarray) -> np.ndarray:
    latent = np.asarray(normed_embedding, dtype=np.float32).reshape((1, -1)) @ emap
    return (latent / np.linalg.norm(latent)).astype(np.float32)


def paste_back(target_img: np.ndarray, bgr_fake: np.ndarray, aimg: np.ndarray, m: np.ndarray) -> np.ndarray:
    """Blend the swapped crop into the full image with an eroded + blurred mask (as insightface does)."""
    import cv2

    h, w = target_img.shape[:2]
    fake_diff = np.abs(bgr_fake.astype(np.float32) - aimg.astype(np.float32)).mean(axis=2)
    fake_diff[:2, :] = 0
    fake_diff[-2:, :] = 0
    fake_diff[:, :2] = 0
    fake_diff[:, -2:] = 0
    im = cv2.invertAffineTransform(m)
    img_white = np.full(aimg.shape[:2], 255, dtype=np.float32)
    bgr_fake = cv2.warpAffine(bgr_fake, im, (w, h), borderValue=0.0)
    img_white = cv2.warpAffine(img_white, im, (w, h), borderValue=0.0)
    fake_diff = cv2.warpAffine(fake_diff, im, (w, h), borderValue=0.0)
    img_white[img_white > 20] = 255
    fake_diff[fake_diff < 10] = 0
    fake_diff[fake_diff >= 10] = 255
    img_mask = img_white
    mask_h_inds, mask_w_inds = np.where(img_mask == 255)
    if mask_h_inds.size == 0:  # face crop lies entirely outside the image
        return target_img.copy()
    mask_h = np.max(mask_h_inds) - np.min(mask_h_inds)
    mask_w = np.max(mask_w_inds) - np.min(mask_w_inds)
    mask_size = int(np.sqrt(mask_h * mask_w))
    k = max(mask_size // 10, 10)
    img_mask = cv2.erode(img_mask, np.ones((k, k), np.uint8), iterations=1)
    k = max(mask_size // 20, 5)
    img_mask = cv2.GaussianBlur(img_mask, (2 * k + 1, 2 * k + 1), 0)
    img_mask = (img_mask / 255)[:, :, None]
    merged = img_mask * bgr_fake + (1 - img_mask) * target_img.astype(np.float32)
    return merged.astype(np.uint8)


# --------------------------------------------------------------------------- model files


def _pack_complete(directory: Path) -> bool:
    return all((directory / name).is_file() for name in PACK_FILES)


def find_buffalo_l(models_dir: Path) -> Path | None:
    for candidate in (models_dir / "buffalo_l", LEGACY_PACK_DIR):
        if _pack_complete(candidate):
            return candidate
    return None


def ensure_buffalo_l(models_dir: Path, url: str = BUFFALO_L_URL) -> Path:
    """Return a folder with det_10g.onnx + w600k_r50.onnx, downloading the official pack if needed."""
    found = find_buffalo_l(models_dir)
    if found is not None:
        return found
    target = models_dir / "buffalo_l"
    target.mkdir(parents=True, exist_ok=True)
    archive = target / "buffalo_l.zip.part"
    log.info("Downloading face detection models from %s", url)
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, open(archive, "wb") as fh:
            shutil.copyfileobj(resp, fh, 1 << 20)
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                name = Path(info.filename).name  # basename only: never trust paths inside the zip
                if name in PACK_FILES:
                    with zf.open(info) as src, open(target / f"{name}.part", "wb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
                    (target / f"{name}.part").replace(target / name)
    except (OSError, zipfile.BadZipFile) as exc:
        raise FaceEngineError(
            f"Не удалось скачать модели распознавания лиц buffalo_l ({exc}). Скачайте вручную {url}, "
            f"распакуйте {DET_FILE} и {REC_FILE} в папку {target} и перезапустите."
        ) from exc
    finally:
        archive.unlink(missing_ok=True)
    if not _pack_complete(target):
        raise FaceEngineError(f"В архиве {url} нет файлов {DET_FILE} / {REC_FILE}.")
    return target


# --------------------------------------------------------------------------- onnxruntime


def select_providers(device: str, available: list[str] | None = None) -> list[str]:
    if available is None:
        import onnxruntime as ort

        available = ort.get_available_providers()
    providers = []
    if device == "cuda" and "CUDAExecutionProvider" in available:
        providers.append("CUDAExecutionProvider")
    if device != "cpu" and "DmlExecutionProvider" in available:
        providers.append("DmlExecutionProvider")
    providers.append("CPUExecutionProvider")
    return providers


def _imports():
    try:
        import cv2
        import onnxruntime as ort
    except ImportError as exc:
        raise FaceEngineError(
            "Не установлены onnxruntime/opencv-python-headless. См. requirements-gpu.txt."
        ) from exc
    return cv2, ort


def create_session(path: Path, providers: list[str]):
    _, ort = _imports()
    if "CUDAExecutionProvider" in providers:
        try:  # on Windows torch puts the CUDA/cuDNN DLLs on the search path for onnxruntime-gpu
            import torch  # noqa: F401
        except ImportError:
            pass
    opts = ort.SessionOptions()
    if "DmlExecutionProvider" in providers:
        opts.enable_mem_pattern = False
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    session = ort.InferenceSession(str(path), sess_options=opts, providers=providers)
    log.info("Loaded %s with %s", path.name, session.get_providers())
    return session


class Detector:
    """SCRFD (det_10g) face detector."""

    def __init__(self, session, input_size: tuple[int, int] = (640, 640), thresh: float = 0.5) -> None:
        self.session = session
        self.input_size = input_size
        self.thresh = thresh
        self.nms_thresh = 0.4
        self.input_mean, self.input_std = 127.5, 128.0
        self.input_name = session.get_inputs()[0].name
        outputs = session.get_outputs()
        self.output_names = [o.name for o in outputs]
        self.batched = len(outputs[0].shape) == 3
        if len(outputs) not in (6, 9):
            raise FaceEngineError(f"Неподдерживаемый детектор лиц: {len(outputs)} выходов.")
        self.fmc, self.strides, self.num_anchors = 3, (8, 16, 32), 2
        self.use_kps = len(outputs) == 9
        if not self.use_kps:
            raise FaceEngineError("Детектор лиц без ключевых точек не подходит для подмены лица.")
        self._centers: dict[tuple[int, int, int], np.ndarray] = {}

    def _anchor_centers(self, height: int, width: int, stride: int) -> np.ndarray:
        key = (height, width, stride)
        if key not in self._centers:
            centers = np.stack(np.mgrid[:height, :width][::-1], axis=-1).astype(np.float32)
            centers = (centers * stride).reshape((-1, 2))
            if self.num_anchors > 1:
                centers = np.stack([centers] * self.num_anchors, axis=1).reshape((-1, 2))
            self._centers[key] = centers
        return self._centers[key]

    def _forward(self, det_img: np.ndarray):
        cv2, _ = _imports()
        blob = cv2.dnn.blobFromImage(
            det_img, 1.0 / self.input_std, self.input_size, (self.input_mean,) * 3, swapRB=True
        )
        outs = self.session.run(self.output_names, {self.input_name: blob})
        in_h, in_w = blob.shape[2], blob.shape[3]
        scores_l, bboxes_l, kps_l = [], [], []
        for idx, stride in enumerate(self.strides):
            scores = outs[idx]
            bbox_preds = outs[idx + self.fmc]
            kps_preds = outs[idx + self.fmc * 2]
            if self.batched:
                scores, bbox_preds, kps_preds = scores[0], bbox_preds[0], kps_preds[0]
            bbox_preds = bbox_preds * stride
            kps_preds = kps_preds * stride
            centers = self._anchor_centers(in_h // stride, in_w // stride, stride)
            pos = np.where(scores.reshape(-1) >= self.thresh)[0]
            scores_l.append(scores.reshape(-1, 1)[pos])
            bboxes_l.append(distance2bbox(centers, bbox_preds)[pos])
            kps = distance2kps(centers, kps_preds)
            kps_l.append(kps.reshape((kps.shape[0], -1, 2))[pos])
        return scores_l, bboxes_l, kps_l

    def detect(self, img: np.ndarray) -> list[Face]:
        cv2, _ = _imports()
        in_w, in_h = self.input_size
        im_ratio = img.shape[0] / img.shape[1]
        if im_ratio > in_h / in_w:
            new_h = in_h
            new_w = int(new_h / im_ratio)
        else:
            new_w = in_w
            new_h = int(new_w * im_ratio)
        det_scale = new_h / img.shape[0]
        det_img = np.zeros((in_h, in_w, 3), dtype=np.uint8)
        det_img[:new_h, :new_w, :] = cv2.resize(img, (new_w, new_h))
        scores_l, bboxes_l, kps_l = self._forward(det_img)
        scores = np.vstack(scores_l).ravel()
        if scores.size == 0:
            return []
        order = scores.argsort()[::-1]
        bboxes = np.vstack(bboxes_l) / det_scale
        kpss = np.vstack(kps_l) / det_scale
        pre_det = np.hstack((bboxes, scores[:, None])).astype(np.float32)[order]
        kpss = kpss[order]
        keep = nms(pre_det, self.nms_thresh)
        return [
            Face(bbox=pre_det[i, :4].copy(), kps=kpss[i].astype(np.float32), det_score=float(pre_det[i, 4]))
            for i in keep
        ]


class Recognizer:
    """ArcFace (w600k_r50) 512-d identity embedding."""

    def __init__(self, session) -> None:
        self.session = session
        inp = session.get_inputs()[0]
        self.input_name = inp.name
        self.input_size = int(inp.shape[2]) if isinstance(inp.shape[2], int) else 112
        self.output_name = session.get_outputs()[0].name

    def embed_crop(self, aimg: np.ndarray) -> np.ndarray:
        cv2, _ = _imports()
        size = (self.input_size, self.input_size)
        blob = cv2.dnn.blobFromImage(aimg, 1.0 / 127.5, size, (127.5,) * 3, swapRB=True)
        return self.session.run([self.output_name], {self.input_name: blob})[0].flatten()

    def embed(self, img: np.ndarray, face: Face) -> np.ndarray:
        return self.embed_crop(norm_crop(img, face.kps, self.input_size))


class Swapper:
    """inswapper_128: 128x128 aligned target crop + source identity latent -> swapped crop."""

    def __init__(self, session, emap: np.ndarray) -> None:
        self.session = session
        self.emap = emap
        inputs = session.get_inputs()
        names = [i.name for i in inputs]
        self.target_name = "target" if "target" in names else names[0]
        self.source_name = "source" if "source" in names else names[1]
        shape = next(i.shape for i in inputs if i.name == self.target_name)
        self.input_size = int(shape[2]) if isinstance(shape[2], int) else 128
        self.output_name = session.get_outputs()[0].name

    @staticmethod
    def load_emap(path: Path) -> np.ndarray:
        try:
            import onnx
            from onnx import numpy_helper
        except ImportError as exc:
            raise FaceEngineError("Не установлен пакет onnx. См. requirements-gpu.txt.") from exc
        model = onnx.load(str(path))
        return numpy_helper.to_array(model.graph.initializer[-1]).astype(np.float32)

    def swap(self, img: np.ndarray, target_face: Face, source_face: Face) -> np.ndarray:
        cv2, _ = _imports()
        aimg, m = norm_crop2(img, target_face.kps, self.input_size)
        size = (self.input_size, self.input_size)
        blob = cv2.dnn.blobFromImage(aimg, 1.0 / 255.0, size, (0.0, 0.0, 0.0), swapRB=True)
        latent = latent_from_embedding(source_face.normed_embedding, self.emap)
        pred = self.session.run([self.output_name], {self.target_name: blob, self.source_name: latent})[0]
        bgr_fake = np.clip(255 * pred.transpose((0, 2, 3, 1))[0], 0, 255).astype(np.uint8)[:, :, ::-1]
        return paste_back(img, bgr_fake, aimg, m)


class FaceEngine:
    """Detect, embed and swap faces. ONNX sessions are created lazily on first use.

    ``models_dir`` holds ``inswapper_128.onnx`` and ``buffalo_l/{det_10g,w600k_r50}.onnx``; the
    buffalo_l pack is downloaded automatically (or taken from ``~/.insightface``) when missing.
    """

    def __init__(
        self,
        models_dir: Path,
        providers: list[str] | None = None,
        swap_model_path: Path | None = None,
        auto_download: bool = True,
    ) -> None:
        self.models_dir = Path(models_dir)
        self.providers = providers or ["CPUExecutionProvider"]
        self.swap_model_path = swap_model_path or self.models_dir / "inswapper_128.onnx"
        self.auto_download = auto_download
        self._detector: Detector | None = None
        self._recognizer: Recognizer | None = None
        self._swapper: Swapper | None = None
        self._lock = threading.RLock()

    def _pack_dir(self) -> Path:
        if self.auto_download:
            return ensure_buffalo_l(self.models_dir)
        found = find_buffalo_l(self.models_dir)
        if found is None:
            raise FaceEngineError(
                f"Не найдены модели {DET_FILE} / {REC_FILE} в {self.models_dir / 'buffalo_l'}."
            )
        return found

    @property
    def detector(self) -> Detector:
        with self._lock:
            if self._detector is None:
                self._detector = Detector(create_session(self._pack_dir() / DET_FILE, self.providers))
            return self._detector

    @property
    def recognizer(self) -> Recognizer:
        with self._lock:
            if self._recognizer is None:
                self._recognizer = Recognizer(create_session(self._pack_dir() / REC_FILE, self.providers))
            return self._recognizer

    @property
    def swapper(self) -> Swapper:
        with self._lock:
            if self._swapper is None:
                path = self.swap_model_path
                if not path.is_file():
                    raise FaceEngineError(f"Файл модели подмены лица не найден: {path}. См. README.")
                self._swapper = Swapper(create_session(path, self.providers), Swapper.load_emap(path))
            return self._swapper

    def detect(self, img_bgr: np.ndarray, with_embedding: bool = False) -> list[Face]:
        faces = self.detector.detect(img_bgr)
        if with_embedding:
            for face in faces:
                face.embedding = self.recognizer.embed(img_bgr, face)
        return faces

    def embed(self, img_bgr: np.ndarray, face: Face) -> Face:
        face.embedding = self.recognizer.embed(img_bgr, face)
        return face

    def swap(self, img_bgr: np.ndarray, target_face: Face, source_face: Face) -> np.ndarray:
        if source_face.embedding is None:
            raise ValueError("source_face needs an embedding (FaceEngine.embed)")
        return self.swapper.swap(img_bgr, target_face, source_face)
