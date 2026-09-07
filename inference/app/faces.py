"""InsightFace (ArcFace) detection and embeddings. Never the VLM — identity is
a dedicated model's job, and its output is still only a suggestion.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import numpy as np
from PIL import Image

from .config import get_settings
from .images import LoadedImage
from .logging_conf import log_event


class FacesUnavailable(RuntimeError):
    """The face model crashed or could not be loaded."""


class FaceEngine:
    """Lazy-loaded and thread-safe. Face work may run while the VLM is busy."""

    def __init__(self) -> None:
        self._app = None
        self._model_name: str | None = None
        self._det_size: int | None = None
        self._load_lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._app is not None

    @property
    def model_name(self) -> str:
        return self._model_name or get_settings().face_model

    def unload(self) -> None:
        with self._load_lock:
            self._app = None
            self._model_name = None

    def _load_blocking(self) -> None:
        with self._load_lock:
            if self._app is not None:
                return
            from insightface.app import FaceAnalysis

            settings = get_settings()
            started = time.perf_counter()
            app = FaceAnalysis(
                name=settings.face_model,
                allowed_modules=["detection", "recognition"],
                providers=["CPUExecutionProvider"],
            )
            app.prepare(ctx_id=-1, det_size=(settings.face_det_size, settings.face_det_size))
            self._app = app
            self._model_name = settings.face_model
            self._det_size = settings.face_det_size
            log_event(
                "faces.loaded",
                model=settings.face_model,
                det_size=settings.face_det_size,
                load_ms=int((time.perf_counter() - started) * 1000),
            )

    def _detect_blocking(self, loaded: LoadedImage) -> list[dict[str, Any]]:
        self._load_blocking()
        # InsightFace expects BGR, like OpenCV.
        rgb = np.asarray(loaded.image, dtype=np.uint8)
        bgr = rgb[:, :, ::-1]
        faces = self._app.get(bgr)

        out = [
            scale_detection(
                bbox_xyxy=face.bbox,
                kps=getattr(face, "kps", None),
                det_score=face.det_score,
                embedding=face.normed_embedding,
                scale=loaded.scale,
            )
            for face in faces
        ]
        out.sort(key=lambda f: f["det_score"], reverse=True)
        return out

    async def detect(self, loaded: LoadedImage) -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(self._detect_blocking, loaded)
        except Exception as exc:  # noqa: BLE001
            self.unload()
            log_event("faces.detect_failed", error=str(exc))
            raise FacesUnavailable(str(exc)) from exc


def scale_detection(
    *,
    bbox_xyxy: Any,
    kps: Any,
    det_score: Any,
    embedding: Any,
    scale: float,
) -> dict[str, Any]:
    """One InsightFace detection, in pixels of the ORIGINAL image.

    Detection runs on the downscaled copy, so every coordinate is multiplied by
    `scale` on the way out. The caller stores boxes against the full-size file.
    """
    x1, y1, x2, y2 = (float(v) for v in bbox_xyxy)
    landmarks = None
    if kps is not None:
        landmarks = [
            [round(float(px) * scale, 2), round(float(py) * scale, 2)]
            for px, py in np.asarray(kps, dtype=np.float32)
        ]
    return {
        "bbox": {
            "x": round(x1 * scale, 2),
            "y": round(y1 * scale, 2),
            "w": round((x2 - x1) * scale, 2),
            "h": round((y2 - y1) * scale, 2),
        },
        "det_score": round(float(det_score), 4),
        "embedding": [round(float(v), 6) for v in np.asarray(embedding, dtype=np.float32)],
        "landmarks": landmarks,
    }


def cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    """1 - cosine similarity. References are normalised here, not trusted to be."""
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 1.0
    distance = 1.0 - float(np.dot(a, b)) / (na * nb)
    # Float error can push an identical pair to -0.0 or a bit past 2.0.
    return float(min(2.0, max(0.0, distance)))


face_engine = FaceEngine()
