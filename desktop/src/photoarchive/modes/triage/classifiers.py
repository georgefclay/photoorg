"""Pre-sort hint classifiers (no AI). Pure functions over an image and its
EXIF; no DB, no filesystem beyond what the caller opens.

Each classifier returns (matched: bool, confidence: float in [0,1], details: dict).
Order of application is decided by the caller (see presort.py).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

import cv2
import numpy as np
from PIL import Image, ImageOps

from ..ingest.back_detect import _haar

log = logging.getLogger(__name__)


# Exact pixel sizes we recognise as a screenshot without further evidence.
# Populated from common phones and desktop resolutions George might have
# taken screenshots on. Kept small and explicit — a JPG of a real photo
# with no EXIF must NOT be called a screenshot unless its dimensions land
# in this list exactly (either orientation).
SCREEN_SIZES: frozenset[tuple[int, int]] = frozenset({
    # iPhone family (portrait; landscape variants added below).
    (640, 960), (640, 1136), (750, 1334), (828, 1792),
    (1080, 1920), (1125, 2436), (1170, 2532), (1179, 2556),
    (1242, 2208), (1242, 2688), (1284, 2778), (1290, 2796),
    (1320, 2868), (1170, 2532),
    # Common Android portrait sizes.
    (720, 1280), (1080, 2160), (1080, 2340), (1080, 2400),
    (1440, 2560), (1440, 2960), (1440, 3120), (1440, 3200),
    # Desktop / laptop landscape.
    (1366, 768), (1440, 900), (1600, 900), (1680, 1050),
    (1920, 1080), (1920, 1200), (2160, 1440), (2560, 1440),
    (2560, 1600), (2880, 1800), (3000, 2000), (3200, 1800),
    (3440, 1440), (3840, 2160), (5120, 2880),
})


def _size_matches_screen(width: int | None, height: int | None) -> bool:
    if not width or not height:
        return False
    return (width, height) in SCREEN_SIZES or (height, width) in SCREEN_SIZES


# EXIF Software strings that betray an OS / editor / screenshot tool.
_SOFTWARE_MARKERS = (
    "screenshot", "snip", "greenshot", "sharex", "gimp", "photoshop",
    "paint.net", "windows", "microsoft", "android", "iphone os", "ios ",
    "mac os", "macos", "preview", "chrome", "firefox", "safari",
    "adobe", "picasa", "photos.app",
)


def is_screenshot(
    *, width: int | None, height: int | None, mime: str | None,
    exif_camera: str | None, exif_software: str | None,
) -> tuple[bool, float, dict]:
    """Soft rule (per answer 3): no camera make/model AND one of
      - PNG, or
      - EXIF Software names an OS / app, or
      - dimensions match SCREEN_SIZES exactly.
    A JPG of a real photo with no EXIF is NOT a screenshot unless it hits
    the size list."""
    if exif_camera:
        return False, 0.0, {"reason": "has camera"}
    is_png = (mime or "").lower() == "image/png"
    software = (exif_software or "").lower()
    software_hit = None
    if software:
        for marker in _SOFTWARE_MARKERS:
            if marker in software:
                software_hit = marker
                break
    size_hit = _size_matches_screen(width, height)

    if is_png:
        return True, 0.7 if size_hit else 0.55, {
            "reason": "png_no_camera", "size_match": size_hit,
            "software": software_hit,
        }
    if software_hit is not None:
        return True, 0.75, {
            "reason": "software", "software": software_hit,
            "size_match": size_hit,
        }
    if size_hit:
        return True, 0.7, {
            "reason": "size_match", "dimensions": [width, height],
        }
    return False, 0.0, {}


def is_tiny(*, width: int | None, height: int | None,
            long_edge_max: int = 599) -> tuple[bool, float, dict]:
    if not width or not height:
        return False, 0.0, {}
    long_edge = max(width, height)
    if long_edge <= long_edge_max:
        return True, 1.0, {"long_edge": long_edge}
    return False, 0.0, {"long_edge": long_edge}


@dataclass(frozen=True)
class _ToneStats:
    mean_L: float
    var_L: float
    mean_S: float
    mid_frac: float
    near_white_frac: float
    edge_density: float
    face_count: int
    width: int
    height: int


def _tone_stats(image: Image.Image) -> _ToneStats:
    """Downsample to 512 px, return the small bag of features every hint
    below re-uses."""
    img = ImageOps.exif_transpose(image).convert("RGB")
    img.thumbnail((512, 512), Image.LANCZOS)
    arr = np.asarray(img)
    h, w = arr.shape[:2]

    hls = cv2.cvtColor(arr, cv2.COLOR_RGB2HLS).astype(np.float32) / 255.0
    L = hls[..., 1]
    S = hls[..., 2]

    mean_L = float(L.mean())
    var_L = float(L.var())
    mean_S = float(S.mean())
    mid_frac = float(((L > 0.15) & (L < 0.85)).mean())
    near_white_frac = float((L > 0.85).mean())

    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 60, 180)
    edge_density = float((edges > 0).mean())

    face_count = 0
    try:
        faces = _haar().detectMultiScale(
            gray, scaleFactor=1.2, minNeighbors=4, minSize=(40, 40),
        )
        if faces is not None:
            face_count = len(faces)
    except cv2.error:
        pass

    return _ToneStats(
        mean_L=mean_L, var_L=var_L, mean_S=mean_S, mid_frac=mid_frac,
        near_white_frac=near_white_frac, edge_density=edge_density,
        face_count=face_count, width=w, height=h,
    )


def is_blank_or_dark(image: Image.Image, *, cached: _ToneStats | None = None
                    ) -> tuple[bool, float, dict]:
    """Mean L below 0.08 or above 0.95 with very low variance."""
    s = cached or _tone_stats(image)
    dark = s.mean_L < 0.08 and s.var_L < 0.005
    blank = s.mean_L > 0.95 and s.var_L < 0.005
    if dark or blank:
        return True, 1.0, {
            "kind": "dark" if dark else "blank",
            "mean_L": round(s.mean_L, 4), "var_L": round(s.var_L, 5),
        }
    return False, 0.0, {
        "mean_L": round(s.mean_L, 4), "var_L": round(s.var_L, 5),
    }


def is_document(image: Image.Image, *, cached: _ToneStats | None = None
               ) -> tuple[bool, float, dict]:
    """Receipts / labels / product keys / whiteboards.
    High near-white fraction, high edge density (text lines), low saturation,
    no faces."""
    s = cached or _tone_stats(image)
    if s.face_count > 0:
        return False, 0.0, {"reason": "faces"}
    hits = 0
    if s.near_white_frac > 0.55:
        hits += 1
    if s.mean_S < 0.15:
        hits += 1
    if s.edge_density > 0.06:
        hits += 1
    if s.mid_frac < 0.35:
        hits += 1
    # All four is a confident document; three of four is likely; less is not.
    if hits < 3:
        return False, 0.0, {
            "hits": hits,
            "near_white_frac": round(s.near_white_frac, 3),
            "mean_S": round(s.mean_S, 3),
            "edge_density": round(s.edge_density, 3),
            "mid_frac": round(s.mid_frac, 3),
        }
    conf = 0.6 + 0.1 * hits  # 0.9 for 3, 1.0 for 4
    return True, min(1.0, conf), {
        "hits": hits,
        "near_white_frac": round(s.near_white_frac, 3),
        "mean_S": round(s.mean_S, 3),
        "edge_density": round(s.edge_density, 3),
        "mid_frac": round(s.mid_frac, 3),
    }


def laplacian_sharpness(image: Image.Image) -> float:
    """Variance of the Laplacian — higher is sharper. Used to pick the
    keeper in a burst group."""
    img = ImageOps.exif_transpose(image).convert("L")
    img.thumbnail((512, 512), Image.LANCZOS)
    arr = np.asarray(img)
    return float(cv2.Laplacian(arr, cv2.CV_64F).var())


def analyse_tone(image: Image.Image) -> _ToneStats:
    """Public helper so the caller can compute once and pass to both
    is_blank_or_dark and is_document without re-decoding."""
    return _tone_stats(image)
