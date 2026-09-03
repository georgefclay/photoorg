"""Synthetic-image tests for the back-detect scorer.

Four fixtures:
  - blank_white:  a plain light photo, no ink → not a strong back (missing "some ink")
  - back_with_ink: light background + small ink squiggle → back-like (≥ 0.6)
  - colour_photo: saturated gradient → not back (low light, low no-face weight)
  - dense_ink: mostly black → not back (fails "good_ink" band and light_bg)
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from photoarchive.modes.ingest.back_detect import analyse


def _make_blank_white() -> Image.Image:
    arr = np.full((400, 300, 3), 245, dtype=np.uint8)
    return Image.fromarray(arr)


def _make_back_with_ink() -> Image.Image:
    arr = np.full((400, 300, 3), 245, dtype=np.uint8)
    # Draw a few thin dark strokes (~1% of pixels).
    for y in (80, 150, 220):
        arr[y : y + 2, 40:260] = 20
    for x in (60, 120):
        arr[100:200, x : x + 2] = 20
    return Image.fromarray(arr)


def _make_colour_photo() -> Image.Image:
    r = np.tile(np.linspace(0, 255, 300, dtype=np.uint8), (400, 1))
    g = np.tile(np.linspace(255, 0, 300, dtype=np.uint8), (400, 1))
    b = np.tile(np.linspace(120, 200, 400, dtype=np.uint8).reshape(-1, 1), (1, 300))
    arr = np.stack([r, g, b], axis=-1).astype(np.uint8)
    return Image.fromarray(arr)


def _make_dense_ink() -> Image.Image:
    arr = np.full((400, 300, 3), 25, dtype=np.uint8)
    return Image.fromarray(arr)


def test_blank_white_is_not_strongly_back():
    feats = analyse(_make_blank_white())
    # No ink at all → good_ink component is 0 → score won't clear 0.6.
    assert feats.score < 0.6, feats


def test_back_with_ink_scores_high():
    feats = analyse(_make_back_with_ink())
    assert feats.score >= 0.6, feats
    assert feats.face_count == 0


def test_colour_photo_scores_low():
    feats = analyse(_make_colour_photo())
    assert feats.score < 0.6, feats


def test_dense_ink_scores_low():
    feats = analyse(_make_dense_ink())
    assert feats.score < 0.6, feats
