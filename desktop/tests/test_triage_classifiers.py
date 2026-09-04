"""Hint classifier tests. Synthetic images exercise each rule without
touching the DB or filesystem."""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from photoarchive.modes.triage import classifiers


def _blank(width: int, height: int, colour: int = 255) -> Image.Image:
    return Image.fromarray(
        np.full((height, width, 3), colour, dtype=np.uint8),
    )


def test_is_tiny_by_long_edge():
    matched, conf, _ = classifiers.is_tiny(width=400, height=300)
    assert matched and conf == 1.0
    matched, _, _ = classifiers.is_tiny(width=800, height=200)
    assert not matched  # long edge 800 → not tiny


def test_screenshot_soft_rule_size_match():
    matched, _, det = classifiers.is_screenshot(
        width=1920, height=1080, mime="image/jpeg",
        exif_camera=None, exif_software=None,
    )
    assert matched
    assert det["reason"] == "size_match"


def test_screenshot_png_without_camera():
    matched, _, det = classifiers.is_screenshot(
        width=1234, height=5678, mime="image/png",
        exif_camera=None, exif_software=None,
    )
    assert matched
    assert det["reason"] == "png_no_camera"


def test_screenshot_rejected_when_camera_present():
    matched, _, _ = classifiers.is_screenshot(
        width=1920, height=1080, mime="image/jpeg",
        exif_camera="Canon EOS 5D", exif_software=None,
    )
    assert not matched


def test_screenshot_jpg_no_exif_arbitrary_size_is_not_screenshot():
    matched, _, _ = classifiers.is_screenshot(
        width=1234, height=567, mime="image/jpeg",
        exif_camera=None, exif_software=None,
    )
    assert not matched


def test_screenshot_software_marker():
    matched, _, det = classifiers.is_screenshot(
        width=800, height=600, mime="image/jpeg",
        exif_camera=None, exif_software="Snipping Tool 11",
    )
    assert matched
    assert det["reason"] == "software"


def test_blank_or_dark_black_frame():
    img = _blank(300, 200, colour=5)
    matched, _, det = classifiers.is_blank_or_dark(img)
    assert matched
    assert det["kind"] == "dark"


def test_blank_or_dark_white_frame():
    img = _blank(300, 200, colour=252)
    matched, _, det = classifiers.is_blank_or_dark(img)
    assert matched
    assert det["kind"] == "blank"


def test_blank_or_dark_photo_is_not():
    rng = np.random.default_rng(seed=1)
    arr = rng.integers(20, 220, size=(200, 300, 3), dtype=np.uint8)
    matched, _, _ = classifiers.is_blank_or_dark(Image.fromarray(arr))
    assert not matched


def test_document_with_text_lines():
    # White page with horizontal "text" lines: high near-white fraction,
    # high edge density, low saturation.
    arr = np.full((600, 800, 3), 250, dtype=np.uint8)
    img = Image.fromarray(arr)
    d = ImageDraw.Draw(img)
    for y in range(60, 540, 30):
        d.line([(60, y), (740, y)], fill=(20, 20, 20), width=2)
    matched, conf, det = classifiers.is_document(img)
    assert matched, det
    assert conf >= 0.9


def test_document_rejects_saturated_photo():
    rng = np.random.default_rng(seed=2)
    arr = rng.integers(0, 255, size=(400, 400, 3), dtype=np.uint8)
    matched, _, _ = classifiers.is_document(Image.fromarray(arr))
    assert not matched


def test_ink_fraction_high_on_page_with_scribble():
    """A near-blank scan with a hand-scribbled date should have ink well
    above the 0.001 threshold used to reclassify blank_or_dark →
    possible_back. Using rectangles instead of text avoids PIL's tiny
    default font — real handwriting is many pixels tall."""
    arr = np.full((600, 800, 3), 250, dtype=np.uint8)
    img = Image.fromarray(arr)
    d = ImageDraw.Draw(img)
    # A handwritten date takes up maybe 200×20 px of ink strokes.
    for x in (200, 260, 320, 380, 440):
        d.line([(x, 260), (x, 320)], fill=(20, 20, 20), width=3)
    d.line([(200, 260), (460, 262)], fill=(20, 20, 20), width=3)
    d.line([(200, 320), (460, 322)], fill=(20, 20, 20), width=3)
    ink = classifiers.ink_fraction(img)
    assert ink > 0.001, ink


def test_ink_fraction_low_on_truly_blank_page():
    """A completely blank near-white scan should not trigger possible_back."""
    img = _blank(600, 400, colour=252)
    ink = classifiers.ink_fraction(img)
    assert ink <= 0.001, ink


def test_laplacian_sharpness_prefers_edges():
    # A flat image is not sharp; add an edge to bump variance.
    flat = _blank(300, 300, colour=128)
    edge = Image.fromarray(np.tile(
        np.concatenate([
            np.full((300, 150, 3), 30, dtype=np.uint8),
            np.full((300, 150, 3), 220, dtype=np.uint8),
        ], axis=1), (1, 1, 1),
    ))
    assert classifiers.laplacian_sharpness(edge) > classifiers.laplacian_sharpness(flat)
