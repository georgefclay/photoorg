"""Score the probability that a scanned image is the back of a print.

Signals (higher score → more back-like):
  - mostly light background (mean value high)
  - low saturation (near-grayscale)
  - some ink-like dark strokes covering a small fraction (not zero, not much)
  - no face-like regions (cheap Haar cascade)
  - aspect ratio close to the previous file's (paired scans)

The scorer returns 0..1. A file is a *proposed back* when its score is >= 0.6
AND the previous file in the same folder is not itself a proposed back.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image, ImageOps

log = logging.getLogger(__name__)

_HAAR: cv2.CascadeClassifier | None = None


def _haar() -> cv2.CascadeClassifier:
    global _HAAR
    if _HAAR is None:
        path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        _HAAR = cv2.CascadeClassifier(path)
    return _HAAR


@dataclass(frozen=True)
class BackFeatures:
    score: float
    mean_v: float
    mean_s: float
    ink_fraction: float
    face_count: int
    aspect_ratio: float


def analyse(image: Image.Image) -> BackFeatures:
    """Feature extraction only; caller does the score-vs-threshold decision."""
    img = ImageOps.exif_transpose(image).convert("RGB")
    # Downscale for speed; features are all intensity-based.
    img.thumbnail((512, 512), Image.LANCZOS)
    arr = np.asarray(img)
    h, w = arr.shape[:2]
    aspect = w / max(h, 1)

    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
    mean_v = float(hsv[..., 2].mean()) / 255.0
    mean_s = float(hsv[..., 1].mean()) / 255.0

    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    # Adaptive threshold catches pen/pencil strokes even when the paper isn't
    # perfectly uniform; ink pixels become the minority.
    thr = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 25, 10,
    )
    ink_fraction = float((thr > 0).mean())

    try:
        faces = _haar().detectMultiScale(
            gray, scaleFactor=1.2, minNeighbors=4, minSize=(40, 40)
        )
        face_count = 0 if faces is None else len(faces)
    except cv2.error:
        face_count = 0

    score = _score(mean_v, mean_s, ink_fraction, face_count)
    return BackFeatures(score=score, mean_v=mean_v, mean_s=mean_s,
                        ink_fraction=ink_fraction, face_count=face_count,
                        aspect_ratio=aspect)


def _score(mean_v: float, mean_s: float,
           ink_fraction: float, face_count: int) -> float:
    # Each component 0..1. Ink is weighted heavily so a truly blank sheet
    # (no writing) is NOT scored as a back — the whole point of the pass is
    # to catch scanned backs of prints that have writing on them.
    light_bg = _bump(mean_v, 0.75, 1.00)
    low_sat = 1.0 - _bump(mean_s, 0.20, 0.60)
    good_ink = _band(ink_fraction, 0.005, 0.02, 0.12, 0.30)
    no_faces = 0.0 if face_count > 0 else 1.0
    raw = 0.20 * light_bg + 0.15 * low_sat + 0.50 * good_ink + 0.15 * no_faces
    return float(max(0.0, min(1.0, raw)))


def _bump(x: float, lo: float, hi: float) -> float:
    if x <= lo:
        return 0.0
    if x >= hi:
        return 1.0
    return (x - lo) / (hi - lo)


def _band(x: float, lo1: float, lo2: float, hi1: float, hi2: float) -> float:
    """Trapezoid: 0 below lo1, 1 in [lo2..hi1], 0 above hi2."""
    if x <= lo1 or x >= hi2:
        return 0.0
    if lo2 <= x <= hi1:
        return 1.0
    if x < lo2:
        return (x - lo1) / (lo2 - lo1)
    return (hi2 - x) / (hi2 - hi1)


def aspect_close(a: float, b: float, tol: float = 0.15) -> bool:
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) / max(a, b) <= tol
