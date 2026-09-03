"""Score the probability that a scanned image is the back of a print.

Combine components multiplicatively with hard vetoes; do NOT average.
Averaging is how a portrait's dark-clothing "ink fraction" plus flesh-tone
"low saturation" combine into a false-positive 0.61 score.

A back has all of:
  - light background (fraction of pixels with HLS L > 0.85 is high)
  - near-grayscale (mean HLS S is very low)
  - sparse ink strokes (dark-pixel fraction in the ~0.5 %..15 % band)
  - no faces (Haar cascade)

Any failure zeros the score. All passing → score → 1.

The scorer works on a 512-px thumbnail; aspect_ratio is kept for the pairing
step (aspect_close), not for the score itself.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image, ImageOps

log = logging.getLogger(__name__)


LIGHT_L_THRESHOLD = 0.85
MIN_LIGHT_FRAC = 0.60
MAX_MEAN_SATURATION = 0.08
INK_LO = 0.005
INK_HI = 0.15

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
    light_pixel_fraction: float
    mean_saturation: float
    ink_fraction: float
    face_count: int
    aspect_ratio: float


def analyse(image: Image.Image) -> BackFeatures:
    """Feature extraction only; caller decides score vs. threshold and pairing."""
    img = ImageOps.exif_transpose(image).convert("RGB")
    img.thumbnail((512, 512), Image.LANCZOS)
    arr = np.asarray(img)
    h, w = arr.shape[:2]
    aspect = w / max(h, 1)

    # HLS: L is lightness (0..1 after divide), S is saturation (0..1).
    hls = cv2.cvtColor(arr, cv2.COLOR_RGB2HLS).astype(np.float32) / 255.0
    L = hls[..., 1]
    S = hls[..., 2]
    light_pixel_fraction = float((L > LIGHT_L_THRESHOLD).mean())
    mean_saturation = float(S.mean())

    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    thr = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 25, 10,
    )
    ink_fraction = float((thr > 0).mean())

    face_count = 0
    try:
        faces = _haar().detectMultiScale(
            gray, scaleFactor=1.2, minNeighbors=4, minSize=(40, 40)
        )
        if faces is not None:
            face_count = len(faces)
    except cv2.error:
        pass

    score = _score(light_pixel_fraction, mean_saturation, ink_fraction, face_count)
    return BackFeatures(
        score=score,
        light_pixel_fraction=light_pixel_fraction,
        mean_saturation=mean_saturation,
        ink_fraction=ink_fraction,
        face_count=face_count,
        aspect_ratio=aspect,
    )


def _score(light_pixel_fraction: float, mean_saturation: float,
           ink_fraction: float, face_count: int) -> float:
    # Hard veto: any human face → not a back.
    if face_count > 0:
        return 0.0
    # Hard veto: no light background at all → not a back.
    if light_pixel_fraction < MIN_LIGHT_FRAC / 2:
        return 0.0
    # Hard veto: too saturated to be paper.
    if mean_saturation > 2 * MAX_MEAN_SATURATION:
        return 0.0
    # Hard veto: outside the ink band (blank paper OR densely dark).
    if ink_fraction < INK_LO or ink_fraction > INK_HI:
        return 0.0
    # Otherwise combine multiplicatively. Each factor peaks at 1.0 in its
    # ideal region, ramps to 0 at the veto boundary.
    light_factor = _ramp_up(light_pixel_fraction, MIN_LIGHT_FRAC / 2, MIN_LIGHT_FRAC)
    sat_factor = _ramp_down(mean_saturation, MAX_MEAN_SATURATION, 2 * MAX_MEAN_SATURATION)
    # Ink: 1 across the plateau [INK_LO, 0.12], falling off at 0.005 and
    # again above 0.12 to the hard veto at INK_HI. Spec: 0.5-15 % is a back.
    ink_factor = _plateau(ink_fraction, INK_LO * 0.5, INK_LO, 0.12, INK_HI)
    return float(max(0.0, min(1.0, light_factor * sat_factor * ink_factor)))


def _ramp_up(x: float, lo: float, hi: float) -> float:
    if x <= lo:
        return 0.0
    if x >= hi:
        return 1.0
    return (x - lo) / (hi - lo)


def _ramp_down(x: float, lo: float, hi: float) -> float:
    if x <= lo:
        return 1.0
    if x >= hi:
        return 0.0
    return (hi - x) / (hi - lo)


def _plateau(x: float, lo1: float, lo2: float, hi1: float, hi2: float) -> float:
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
