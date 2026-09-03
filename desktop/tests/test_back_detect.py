"""Synthetic-image regression tests for the back-detect scorer.

Requirements (fix-up 1):
  - White with dark scribbles          -> >= 0.8
  - Dark image with face-sized light oval -> <= 0.2
  - Grey mid-tone image                -> <= 0.3

Plus the earlier back-with-ink-strokes fixture at >= 0.6 (kept for spec
compatibility with the original Phase 2 prompt).
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from photoarchive.modes.ingest.back_detect import analyse


def _make_white_with_scribbles() -> Image.Image:
    arr = np.full((400, 300, 3), 245, dtype=np.uint8)
    img = Image.fromarray(arr)
    d = ImageDraw.Draw(img)
    # ~1-2 % of pixels as ink strokes.
    d.line([(40, 80), (260, 88)], fill=(20, 20, 20), width=2)
    d.line([(50, 150), (250, 145)], fill=(20, 20, 20), width=2)
    d.line([(60, 220), (240, 232)], fill=(20, 20, 20), width=2)
    d.line([(60, 100), (60, 200)], fill=(20, 20, 20), width=2)
    d.line([(120, 100), (120, 200)], fill=(20, 20, 20), width=2)
    return img


def _make_dark_with_light_oval() -> Image.Image:
    arr = np.full((400, 300, 3), 25, dtype=np.uint8)
    img = Image.fromarray(arr)
    d = ImageDraw.Draw(img)
    d.ellipse([(100, 130), (200, 270)], fill=(230, 210, 190))  # face-sized
    return img


def _make_grey_midtone() -> Image.Image:
    arr = np.full((400, 300, 3), 128, dtype=np.uint8)
    return Image.fromarray(arr)


def _make_back_with_strokes() -> Image.Image:
    """Kept from Phase 2 initial spec — sanity that the strong-back
    signal still fires."""
    arr = np.full((400, 300, 3), 245, dtype=np.uint8)
    for y in (80, 150, 220):
        arr[y : y + 2, 40:260] = 20
    for x in (60, 120):
        arr[100:200, x : x + 2] = 20
    return Image.fromarray(arr)


def test_white_with_dark_scribbles_scores_high():
    feats = analyse(_make_white_with_scribbles())
    assert feats.score >= 0.8, feats


def test_dark_with_face_oval_scores_low():
    feats = analyse(_make_dark_with_light_oval())
    assert feats.score <= 0.2, feats


def test_grey_midtone_scores_low():
    feats = analyse(_make_grey_midtone())
    assert feats.score <= 0.3, feats


def test_back_with_strokes_still_qualifies():
    feats = analyse(_make_back_with_strokes())
    assert feats.score >= 0.6, feats
    assert feats.face_count == 0
