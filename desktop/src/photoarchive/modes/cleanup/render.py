"""Turn a proposal's measurements plus the reviewer's ticked ops into pixels.

Analysis stores a *plan*, not a file: the full-resolution derivative is cut on
demand (when the review pane zooms to 1:1) and again at Accept with exactly
the ops that are still ticked. That is the Phase 7 answer to writing 8–10 GB
of derivatives most of which would be re-rendered anyway.

Invariant 3: format follows the source. JPEG in → JPEG q95 out, TIFF in →
TIFF (LZW) out, and a 16-bit *greyscale* TIFF stays 16-bit. Pillow has no
16-bit RGB mode, so colour is processed at 8 bits — see `load_display_array`.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image, ImageOps

from ...config import Settings
from ..ingest.hasher import perceptual_hashes
from ..ingest.image_io import open_image
from ..ingest.thumbs import THUMB_LONG_EDGE
from . import ops
from .geometry import Rect, Transform, inset_px_for_dpi, transform_for

log = logging.getLogger(__name__)

# Every op the reviewer can tick, in the order they are applied.
ALL_OPS = ("deskew", "crop", "colour", "levels")
JPEG_QUALITY = 95


@dataclass
class Plan:
    """What to do to the pixels, for one ticked set."""
    transform: Transform
    colour_gains: dict[str, float] | None = None
    levels: dict[str, float] | None = None
    ticked: tuple[str, ...] = ()
    bed_value: float | None = None

    @property
    def is_noop(self) -> bool:
        return (self.transform.is_identity
                and self.colour_gains is None
                and self.levels is None)


@dataclass
class RenderResult:
    path: Path
    width: int
    height: int
    file_size: int
    phash: str | None = None
    dhash: str | None = None
    thumb_bytes: bytes | None = None
    notes: dict[str, Any] = field(default_factory=dict)


def _bed_value_for(operations: dict[str, Any], dtype_max: float) -> float:
    bed = (operations or {}).get("bed") or {}
    grey = bed.get("grey")
    if grey is None:
        return dtype_max
    return float(grey) / 255.0 * dtype_max


def plan_from(
    operations: dict[str, Any],
    ticked: Iterable[str],
    *,
    settings: Settings,
    src_w: int | None = None,
    src_h: int | None = None,
    region_rect: dict[str, Any] | None = None,
) -> Plan:
    """Build the plan for the currently ticked ops.

    Unticking `deskew` while `crop` stays on crops to the print's
    axis-aligned bounds instead; unticking `crop` while `deskew` stays on
    keeps the whole rotated canvas, bed-filled at the corners. Both are
    honest, so a checkbox always does something visible.
    """
    want = {t for t in ticked if t in ALL_OPS and op_enabled(t, settings)}
    o = (operations or {}).get("ops") or {}
    analysis = (operations or {}).get("analysis") or {}
    width = int(src_w or analysis.get("src_w") or 0)
    height = int(src_h or analysis.get("src_h") or 0)
    if width <= 0 or height <= 0:
        raise ValueError("plan_from needs the source dimensions")

    rect_json = region_rect or (operations or {}).get("print_rect")
    # Fix-up 3: the analyser cropped with `inset - safety`, so rebuilding the
    # transform has to use the same number or the preview and the accepted
    # file would disagree. The crop op records exactly what was used.
    crop_op = ((operations or {}).get("ops") or {}).get("crop") or {}
    if "inset_px" in crop_op:
        inset = float(crop_op["inset_px"])
    else:
        base = float(analysis.get("inset_px")
                     or inset_px_for_dpi(analysis.get("dpi"),
                                         settings.CLEANUP_CROP_INSET_PX_AT_300))
        safety = float(analysis.get("safety_px") or 0.0)
        inset = max(0.0, base - safety)

    deskew = "deskew" in want and "deskew" in o
    crop = "crop" in want and ("crop" in o or region_rect is not None)
    if rect_json is None or not (deskew or crop):
        transform = Transform.identity(width, height)
    else:
        transform = transform_for(
            Rect.from_json(rect_json), src_w=width, src_h=height,
            deskew=deskew, crop=crop, inset_px=inset,
        )

    gains = None
    if "colour" in want and "colour" in o:
        gains = dict(o["colour"].get("gains") or {})
        if not gains:
            gains = None

    levels = None
    if "levels" in want and "levels" in o:
        lv = o["levels"]
        levels = {"lo": float(lv["lo"]), "hi": float(lv["hi"]),
                  "s_curve": float(lv.get("s_curve")
                                   or settings.CLEANUP_SCURVE)}

    return Plan(transform=transform, colour_gains=gains, levels=levels,
                ticked=tuple(sorted(want)))


# Fix-up 2: which ops a setting can switch off wholesale.
OP_ENABLED_SETTING = {
    "colour": "CLEANUP_COLOUR_ENABLED",
    "levels": "CLEANUP_LEVELS_ENABLED",
}


def op_enabled(name: str, settings: Settings | None) -> bool:
    """Is this op switched on? Geometry always is."""
    setting = OP_ENABLED_SETTING.get(name)
    if setting is None or settings is None:
        return True
    return bool(getattr(settings, setting, True))


def default_ticked(
    operations: dict[str, Any], settings: Settings | None = None,
) -> tuple[str, ...]:
    """Every op the analyser proposed starts ticked — unless a setting has
    since switched it off.

    Passing `settings` is how a proposal analysed before fix-up 2 opens with
    its tonal ops unticked instead of needing the row rewritten: the
    measurements stay on the proposal, they simply are not applied.
    """
    o = (operations or {}).get("ops") or {}
    return tuple(name for name in ALL_OPS
                 if name in o and op_enabled(name, settings))


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _save(img_arr: np.ndarray, out_path: Path, *, mime: str, mode: str) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.fromarray(img_arr, mode=mode)
    if mime == "image/tiff":
        im.save(out_path, format="TIFF", compression="tiff_lzw")
    else:
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.save(out_path, format="JPEG", quality=JPEG_QUALITY,
                subsampling=0, optimize=True)


def _pil_mode_for(arr: np.ndarray) -> str:
    """Pillow carries 16 bits per channel only for a *single* channel (`I;16`).

    There is no 16-bit RGB mode, so `load_display_array` reduces multi-channel
    high-bit-depth input to 8-bit on the way in and this only ever sees uint8
    for colour. See the invariant-3 note in that function.
    """
    if arr.ndim == 2:
        return "I;16" if arr.dtype == np.uint16 else "L"
    return "RGB"


def apply_plan_to_array(arr: np.ndarray, plan: Plan) -> np.ndarray:
    """Geometry first, then tone — the order the measurements assume.

    The two tonal ops go through one composed lookup table rather than a chain
    of float32 copies: at 93 MP a single float32 copy is 1.1 GB, and the
    laptop is memory-tight (see `ops.tone_luts`).
    """
    out = arr
    if not plan.transform.is_identity:
        bed = plan.bed_value
        out = ops.warp(out, plan.transform,
                       border_value=bed if bed is not None else ops.dtype_max(out))
    luts = ops.tone_luts(
        out.dtype, gains=plan.colour_gains, levels=plan.levels,
        channels=1 if out.ndim == 2 else out.shape[2],
    )
    if luts is not None:
        # When `out` is the warp result nobody else holds, map it in place
        # rather than allocating a second full-resolution array.
        out = ops.apply_tone_luts(out, luts, in_place=out is not arr)
    elif plan.colour_gains or plan.levels:
        # Non-integer dtype: fall back to the direct route.
        if plan.colour_gains:
            out = ops.apply_cast_gains(out, plan.colour_gains)
        if plan.levels:
            out = ops.apply_levels(out, lo=plan.levels["lo"],
                                   hi=plan.levels["hi"],
                                   s_curve=plan.levels.get("s_curve", 0.0))
    return out


def load_display_array(path: Path) -> tuple[np.ndarray, str]:
    """The working copy as a numpy array in the display frame, in its own bit
    depth. Returns (array, mime-ish hint from the file format).

    Invariant 3 keeps the *format*, and the bit depth with it wherever Pillow
    can: a 16-bit greyscale TIFF stays `I;16` end to end. Pillow has no 16-bit
    RGB mode, though — it cannot even be constructed, let alone saved — so a
    multi-channel high-bit-depth array is reduced to 8 bits here, once, rather
    than blowing up at save time. Pillow's TIFF reader already hands 16-bit RGB
    over as 8-bit RGB, so in practice this is a guard rather than a step.
    """
    with open_image(path) as im:
        fmt = (im.format or "").upper()
        im = ImageOps.exif_transpose(im)
        if im.mode in ("P", "CMYK", "RGBA", "LA"):
            im = im.convert("RGB")
        arr = np.asarray(im)
    if arr.ndim == 3 and arr.dtype != np.uint8:
        log.info("cleanup: %s is %d-channel %s; Pillow has no 16-bit RGB mode, "
                 "so it is processed at 8 bits", path.name, arr.shape[2], arr.dtype)
        arr = ops.as_uint8(arr)
    mime = "image/tiff" if fmt in ("TIFF", "TIF") else "image/jpeg"
    return arr, mime


def render_full(
    src_path: Path,
    plan: Plan,
    out_path: Path,
    *,
    mime: str | None = None,
    operations: dict[str, Any] | None = None,
    want_hashes: bool = True,
) -> RenderResult:
    """Render at full resolution. One image in memory at a time.

    Also returns the new pHash/dHash and the thumbnail bytes, computed from
    the rendered pixels while they are still in hand — Accept then needs no
    second decode, and the hash update rides along in the same statement.
    """
    arr, src_mime = load_display_array(src_path)
    out_mime = mime or src_mime
    if plan.bed_value is None and operations:
        plan = Plan(transform=plan.transform, colour_gains=plan.colour_gains,
                    levels=plan.levels, ticked=plan.ticked,
                    bed_value=_bed_value_for(operations, ops.dtype_max(arr)))
    out = apply_plan_to_array(arr, plan)
    if out is not arr:
        del arr

    mode = _pil_mode_for(out)
    _save(out, out_path, mime=out_mime, mode=mode)

    height, width = out.shape[0], out.shape[1]
    phash = dhash = None
    thumb_bytes = None
    if want_hashes:
        preview = Image.fromarray(ops.as_uint8(out),
                                  mode="L" if out.ndim == 2 else "RGB")
        try:
            phash, dhash = perceptual_hashes(preview)
        except Exception as e:  # pragma: no cover — imagehash is robust
            log.warning("cleanup: perceptual hashes failed for %s: %s", out_path, e)
        thumb = preview.convert("RGB")
        thumb.thumbnail((THUMB_LONG_EDGE, THUMB_LONG_EDGE), Image.LANCZOS)
        buf = io.BytesIO()
        thumb.save(buf, "JPEG", quality=85, optimize=True)
        thumb_bytes = buf.getvalue()
    del out

    return RenderResult(
        path=out_path, width=int(width), height=int(height),
        file_size=out_path.stat().st_size,
        phash=phash, dhash=dhash, thumb_bytes=thumb_bytes,
    )


def render_preview(
    src_path: Path,
    plan: Plan,
    out_path: Path,
    *,
    edge: int,
    operations: dict[str, Any] | None = None,
) -> RenderResult:
    """A downscaled JPEG of the same plan — what the review pane shows until
    the reviewer zooms in. Always JPEG, whatever the source is.

    The source is reduced to `edge` *before* the warp and the transform scaled
    to match, so a 93 MP scan never goes through a full-resolution rotation
    just to produce a thumbnail. Pillow's `draft()` does the reduction in the
    JPEG decoder, so for most scans the full image is never materialised.
    """
    arr, scale = _load_reduced(src_path, edge)
    small_plan = Plan(
        transform=plan.transform.scaled_by(scale) if scale < 1.0 else plan.transform,
        colour_gains=plan.colour_gains, levels=plan.levels, ticked=plan.ticked,
        bed_value=(plan.bed_value if plan.bed_value is not None
                   else (_bed_value_for(operations, ops.dtype_max(arr))
                         if operations else None)),
    )
    out = apply_plan_to_array(arr, small_plan)
    del arr
    u8 = ops.as_uint8(out)
    if u8 is not out:
        del out
    im = Image.fromarray(u8, mode="L" if u8.ndim == 2 else "RGB").convert("RGB")
    if max(im.size) > edge:
        im.thumbnail((edge, edge), Image.LANCZOS)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_path, "JPEG", quality=88, optimize=True)
    return RenderResult(path=out_path, width=im.width, height=im.height,
                        file_size=out_path.stat().st_size)


def _load_reduced(src_path: Path, edge: int) -> tuple[np.ndarray, float]:
    """The display-frame array, reduced so its long edge is near `edge`.

    Returns (array, scale) where `scale` multiplies a full-resolution
    coordinate to get one in the reduced frame.
    """
    with open_image(src_path) as im:
        full_w, full_h = im.size
        if im.mode in ("P", "CMYK", "RGBA", "LA"):
            im = im.convert("RGB")
        else:
            # JPEG: ask the decoder for a reduced image — the full-size one is
            # then never allocated at all.
            try:
                im.draft(im.mode, (edge, edge))
            except Exception:
                pass
        im = ImageOps.exif_transpose(im)
        if max(im.size) > edge:
            im.thumbnail((edge, edge), Image.LANCZOS)
        arr = np.asarray(im)
        reduced_long = max(im.size)
    if arr.ndim == 3 and arr.dtype != np.uint8:
        arr = ops.as_uint8(arr)
    # `full_w/full_h` are raw dims; the display long edge is the same number.
    scale = reduced_long / float(max(full_w, full_h))
    return arr, min(1.0, scale)


def crop_at_full_res(
    src_path: Path,
    plan: Plan,
    *,
    box: tuple[int, int, int, int],
    operations: dict[str, Any] | None = None,
) -> Image.Image:
    """A true 1:1 window of the rendered result, for the review pane at full
    zoom (answer 5 — viewport-sized levels, real pixels when zoomed in).

    `box` is (x, y, w, h) in the *output* frame. We warp only the source
    region that feeds it, so a 93 MP TIFF never lands in memory whole… as
    far as the warp is concerned; the decode itself is still one image.
    """
    arr, _ = load_display_array(src_path)
    if plan.bed_value is None and operations:
        plan = Plan(transform=plan.transform, colour_gains=plan.colour_gains,
                    levels=plan.levels, ticked=plan.ticked,
                    bed_value=_bed_value_for(operations, ops.dtype_max(arr)))
    x, y, w, h = (int(v) for v in box)
    x = max(0, min(x, max(0, plan.transform.out_w - 1)))
    y = max(0, min(y, max(0, plan.transform.out_h - 1)))
    w = max(1, min(w, plan.transform.out_w - x))
    h = max(1, min(h, plan.transform.out_h - y))
    # Shift the transform so the requested window becomes the whole output.
    a, b, tx, c, d, ty = plan.transform.m
    windowed = Transform(
        m=(a, b, tx - x, c, d, ty - y),
        src_w=plan.transform.src_w, src_h=plan.transform.src_h,
        out_w=w, out_h=h, angle_deg=plan.transform.angle_deg,
        crop=None, notes={"window": [x, y, w, h]},
    )
    shifted = Plan(transform=windowed, colour_gains=plan.colour_gains,
                   levels=plan.levels, ticked=plan.ticked,
                   bed_value=plan.bed_value)
    out = apply_plan_to_array(arr, shifted)
    del arr
    u8 = ops.as_uint8(out)
    del out
    return Image.fromarray(u8, mode="L" if u8.ndim == 2 else "RGB").convert("RGB")
