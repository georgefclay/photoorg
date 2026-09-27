"""The pixel operations, dtype-agnostic (uint8 and 16-bit TIFF both occur).

Everything here takes and returns an HxWx3 (or HxW) numpy array in the
image's own dtype, so a 16-bit scan stays 16-bit end to end — invariant 3,
format follows the source.

Memory. The laptop is memory-tight and the biggest scan in the archive is
93.7 MP, which is 281 MB as uint8 RGB — one float32 copy of that is 1.1 GB, and
the naive "convert to float, scale, convert back" chain makes several. Both
tonal ops are *pointwise per channel*, so `tone_luts` composes them into one
lookup table per channel (256 entries for uint8, 65536 for uint16) and
`apply_tone_luts` maps the image in a single pass with no intermediate. The
result is identical arithmetic, done once per possible value instead of once
per pixel.
"""
from __future__ import annotations

import logging

import cv2
import numpy as np

from .geometry import Transform

log = logging.getLogger(__name__)


def dtype_max(arr: np.ndarray) -> float:
    if arr.dtype == np.uint8:
        return 255.0
    if arr.dtype == np.uint16:
        return 65535.0
    if np.issubdtype(arr.dtype, np.floating):
        return 1.0
    return float(np.iinfo(arr.dtype).max)


def to_float01(arr: np.ndarray) -> np.ndarray:
    return arr.astype(np.float32) / dtype_max(arr)


def from_float01(f: np.ndarray, like: np.ndarray) -> np.ndarray:
    mx = dtype_max(like)
    if np.issubdtype(like.dtype, np.floating):
        return np.clip(f, 0.0, 1.0).astype(like.dtype)
    return np.clip(f * mx + 0.5, 0, mx).astype(like.dtype)


def as_uint8(arr: np.ndarray) -> np.ndarray:
    """A uint8 view for the OpenCV routines that only speak 8-bit (Lab,
    thresholding, connected components). Measurement only — never written
    back to the output."""
    if arr.dtype == np.uint8:
        return arr
    return (to_float01(arr) * 255.0 + 0.5).clip(0, 255).astype(np.uint8)


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------

def warp(
    arr: np.ndarray, transform: Transform, *, border_value: float | None = None,
) -> np.ndarray:
    """Apply a Transform's affine and crop in one warpAffine.

    `border_value` fills anything the rotation exposes outside the source —
    the scanner bed's own tone, so an un-cropped deskew looks deliberate
    rather than black-cornered.
    """
    if transform.is_identity:
        return arr
    a, b, tx, c, d, ty = transform.m
    m = np.array([[a, b, tx], [c, d, ty]], dtype=np.float64)
    bv = dtype_max(arr) if border_value is None else float(border_value)
    channels = 1 if arr.ndim == 2 else arr.shape[2]
    return cv2.warpAffine(
        arr, m, (transform.out_w, transform.out_h),
        flags=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(bv,) * channels,
    )


# --------------------------------------------------------------------------
# Colour cast
# --------------------------------------------------------------------------

def midtone_mask(arr: np.ndarray) -> np.ndarray:
    """Boolean mask of the mid-tones (L in [25, 75] of full scale). Age casts
    show there; the paper white and the shadows lie."""
    u8 = as_uint8(arr)
    grey = u8 if u8.ndim == 2 else cv2.cvtColor(u8, cv2.COLOR_RGB2GRAY)
    return (grey >= 64) & (grey <= 191)


# The least-colourful this fraction of the print's mid-tones is what a cast is
# measured on. See `neutral_midtones` and CLEANUP_CAST_NEUTRAL_PCT.
DEFAULT_NEUTRAL_PCT = 40.0
MIN_CAST_SAMPLE = 100


def neutral_midtones(
    arr: np.ndarray,
    mask: np.ndarray | None = None,
    *,
    neutral_pct: float = DEFAULT_NEUTRAL_PCT,
) -> np.ndarray:
    """The pixels a colour cast is judged on: the print's mid-tones, narrowed
    to the least-colourful `neutral_pct` per cent of them.

    Plain grey-world — every mid-tone pixel — assumes the average scene is
    grey, which is exactly where it fails: a lawn, a sunset or a warm indoor
    shot reads as a cast and gets "corrected" into the opposite one. An age
    cast, by contrast, shifts the paper itself, so it is just as visible in the
    pixels that are already nearly grey. Measuring there keeps the defect and
    drops the scene.

    `neutral_pct = 100` restores plain grey-world.
    """
    mid = midtone_mask(arr)
    if mask is not None:
        mid = mid & mask
    if arr.ndim == 2 or neutral_pct >= 100.0 or int(mid.sum()) < MIN_CAST_SAMPLE:
        return mid
    u8 = as_uint8(arr)
    lab = cv2.cvtColor(u8, cv2.COLOR_RGB2LAB)
    chroma = np.hypot(lab[..., 1].astype(np.float32) - 128.0,
                      lab[..., 2].astype(np.float32) - 128.0)
    cut = float(np.percentile(chroma[mid], neutral_pct))
    narrowed = mid & (chroma <= cut)
    # If the narrowing leaves too little to average, keep the wider set rather
    # than measure noise.
    return narrowed if int(narrowed.sum()) >= MIN_CAST_SAMPLE else mid


def measure_cast(
    arr: np.ndarray,
    mask: np.ndarray | None = None,
    *,
    neutral_pct: float = DEFAULT_NEUTRAL_PCT,
) -> dict:
    """Mean Lab a/b of the print's near-neutral mid-tones, as a distance from
    neutral.

    OpenCV's 8-bit Lab centres a and b on 128, so we report the signed
    offsets. Returns `{a, b, magnitude, n, neutral_pct}`; magnitude 0 is
    neutral.
    """
    u8 = as_uint8(arr)
    if u8.ndim == 2:
        return {"a": 0.0, "b": 0.0, "magnitude": 0.0, "n": 0,
                "neutral_pct": neutral_pct}
    sel = neutral_midtones(arr, mask, neutral_pct=neutral_pct)
    n = int(sel.sum())
    if n < MIN_CAST_SAMPLE:
        return {"a": 0.0, "b": 0.0, "magnitude": 0.0, "n": n,
                "neutral_pct": neutral_pct}
    lab = cv2.cvtColor(u8, cv2.COLOR_RGB2LAB)
    a = float(lab[..., 1][sel].mean()) - 128.0
    b = float(lab[..., 2][sel].mean()) - 128.0
    return {"a": round(a, 3), "b": round(b, 3),
            "magnitude": round(float(np.hypot(a, b)), 3), "n": n,
            "neutral_pct": neutral_pct}


def measure_chroma(arr: np.ndarray, mask: np.ndarray | None = None) -> dict:
    """p95 Lab chroma and the circular variance of hue — how we tell a B&W
    or sepia print (a deliberate look) from an age cast (a defect)."""
    u8 = as_uint8(arr)
    if u8.ndim == 2:
        return {"p95_chroma": 0.0, "mean_chroma": 0.0, "hue_variance": 0.0,
                "is_mono": True, "is_sepia": False}
    lab = cv2.cvtColor(u8, cv2.COLOR_RGB2LAB)
    a = lab[..., 1].astype(np.float32) - 128.0
    b = lab[..., 2].astype(np.float32) - 128.0
    if mask is not None and mask.any():
        a, b = a[mask], b[mask]
    else:
        a, b = a.ravel(), b.ravel()
    chroma = np.hypot(a, b)
    if chroma.size == 0:
        return {"p95_chroma": 0.0, "mean_chroma": 0.0, "hue_variance": 0.0,
                "is_mono": True, "is_sepia": False}
    p95 = float(np.percentile(chroma, 95))
    mean_c = float(chroma.mean())
    # Circular variance of hue, weighted by chroma so near-grey pixels (whose
    # hue is noise) don't dominate.
    w = chroma
    total = float(w.sum())
    if total <= 0:
        hue_var = 0.0
    else:
        hue = np.arctan2(b, a)
        cbar = float((w * np.cos(hue)).sum()) / total
        sbar = float((w * np.sin(hue)).sum()) / total
        hue_var = max(0.0, 1.0 - float(np.hypot(cbar, sbar)))
    return {"p95_chroma": round(p95, 3), "mean_chroma": round(mean_c, 3),
            "hue_variance": round(hue_var, 4),
            "is_mono": False, "is_sepia": False}


def cast_gains(
    cast: dict,
    arr: np.ndarray,
    mask: np.ndarray | None = None,
    *,
    neutral_pct: float = DEFAULT_NEUTRAL_PCT,
) -> dict:
    """Per-channel gains that pull the print's near-neutral mid-tones back to
    grey.

    Measured on exactly the pixels `measure_cast` judged (see
    `neutral_midtones`) — if the gains came from a wider set than the
    measurement, the correction would not be the one the caption promised.
    Each channel's mean over that set is scaled to the mean of the three.
    Clamped so a strong cast can't blow a channel.
    """
    u8 = as_uint8(arr)
    if u8.ndim == 2:
        return {"r": 1.0, "g": 1.0, "b": 1.0}
    mid = neutral_midtones(arr, mask, neutral_pct=neutral_pct)
    if int(mid.sum()) < MIN_CAST_SAMPLE:
        return {"r": 1.0, "g": 1.0, "b": 1.0}
    means = [float(u8[..., i][mid].mean()) for i in range(3)]
    target = float(np.mean(means))
    gains = []
    for mval in means:
        g = target / mval if mval > 1.0 else 1.0
        gains.append(float(min(1.35, max(0.74, g))))
    return {"r": round(gains[0], 4), "g": round(gains[1], 4),
            "b": round(gains[2], 4)}


def apply_cast_gains(arr: np.ndarray, gains: dict) -> np.ndarray:
    if arr.ndim == 2:
        return arr
    f = to_float01(arr)
    for i, key in enumerate(("r", "g", "b")):
        f[..., i] = f[..., i] * float(gains.get(key, 1.0))
    return from_float01(f, arr)


def rgb_shift_from_gains(gains: dict) -> dict:
    """The caption wants "cast R+9 B-7" — the 0..255 move a mid-grey makes."""
    mid = 128.0
    return {
        "r": int(round(mid * (float(gains.get("r", 1.0)) - 1.0))),
        "g": int(round(mid * (float(gains.get("g", 1.0)) - 1.0))),
        "b": int(round(mid * (float(gains.get("b", 1.0)) - 1.0))),
    }


# --------------------------------------------------------------------------
# Levels / fade
# --------------------------------------------------------------------------

LEVELS_LO_PCT = 0.5
LEVELS_HI_PCT = 99.5


def measure_levels(arr: np.ndarray, mask: np.ndarray | None = None) -> dict:
    """Percentile range of luminance, as a fraction of full scale.

    `contrast` near 1.0 means the print already uses the whole range; a faded
    print sits well below it.
    """
    u8 = as_uint8(arr)
    grey = u8 if u8.ndim == 2 else cv2.cvtColor(u8, cv2.COLOR_RGB2GRAY)
    vals = grey[mask] if (mask is not None and mask.any()) else grey.ravel()
    if vals.size == 0:
        return {"lo": 0.0, "hi": 255.0, "contrast": 1.0}
    lo = float(np.percentile(vals, LEVELS_LO_PCT))
    hi = float(np.percentile(vals, LEVELS_HI_PCT))
    return {"lo": round(lo, 2), "hi": round(hi, 2),
            "contrast": round(max(0.0, (hi - lo) / 255.0), 4)}


def apply_levels(
    arr: np.ndarray, *, lo: float, hi: float, s_curve: float = 0.0,
) -> np.ndarray:
    """Stretch [lo, hi] (given on a 0..255 scale) to the full range, then a
    mild S-curve. Applied equally to every channel so hues survive.
    """
    if hi <= lo:
        return arr
    f = to_float01(arr)
    lo01, hi01 = lo / 255.0, hi / 255.0
    f = (f - lo01) / (hi01 - lo01)
    f = np.clip(f, 0.0, 1.0)
    if s_curve > 0:
        # smoothstep blended in by `s_curve`; 0 = pure linear.
        f = (1.0 - s_curve) * f + s_curve * (f * f * (3.0 - 2.0 * f))
    return from_float01(f, arr)


def measure_contrast_after(
    arr: np.ndarray, *, lo: float, hi: float, s_curve: float,
    mask: np.ndarray | None = None,
) -> float:
    """What `contrast` becomes once the stretch is applied — for the caption,
    measured rather than assumed."""
    out = apply_levels(arr, lo=lo, hi=hi, s_curve=s_curve)
    return measure_levels(out, mask)["contrast"]


# --------------------------------------------------------------------------
# Both tonal ops as one lookup table (see the module docstring on memory)
# --------------------------------------------------------------------------

def tone_luts(
    dtype: np.dtype,
    *,
    gains: dict | None = None,
    levels: dict | None = None,
    channels: int = 3,
) -> np.ndarray | None:
    """One LUT per channel composing the colour gains and the levels stretch.

    Returns an array of shape (channels, N) in `dtype`, or None when there is
    nothing to do. The arithmetic is exactly `apply_cast_gains` followed by
    `apply_levels`, evaluated once per possible input value.
    """
    if not gains and not levels:
        return None
    if dtype == np.uint8:
        n = 256
    elif dtype == np.uint16:
        n = 65536
    else:
        return None                      # float paths keep the direct route

    mx = float(n - 1)
    values = np.arange(n, dtype=np.float32) / mx
    out = np.empty((channels, n), dtype=dtype)
    keys = ("r", "g", "b")
    for ch in range(channels):
        v = values
        if gains:
            key = keys[ch] if ch < len(keys) else "g"
            v = v * float(gains.get(key, 1.0))
        if levels:
            lo01 = float(levels["lo"]) / 255.0
            hi01 = float(levels["hi"]) / 255.0
            if hi01 > lo01:
                v = np.clip((v - lo01) / (hi01 - lo01), 0.0, 1.0)
                sc = float(levels.get("s_curve") or 0.0)
                if sc > 0:
                    v = (1.0 - sc) * v + sc * (v * v * (3.0 - 2.0 * v))
        out[ch] = np.clip(v * mx + 0.5, 0, mx).astype(dtype)
    return out


def apply_tone_luts(
    arr: np.ndarray, luts: np.ndarray | None, *, in_place: bool = False,
) -> np.ndarray:
    """Map an image through per-channel LUTs in one pass.

    `in_place=True` writes back into `arr` — safe because a LUT is pointwise,
    and worth 281 MB on the 93.7 MP scan at accept time, where the array it
    would otherwise duplicate is the warp result nothing else holds.
    """
    if luts is None:
        return arr
    if arr.ndim == 2:
        mapped = luts[0][arr]
        if in_place:
            arr[...] = mapped
            return arr
        return mapped
    out = arr if in_place else np.empty_like(arr)
    for ch in range(arr.shape[2]):
        lut = luts[min(ch, luts.shape[0] - 1)]
        if arr.dtype == np.uint8:
            # cv2.LUT is the fastest route for 8-bit and allocates only the
            # plane it writes.
            out[..., ch] = cv2.LUT(arr[..., ch], lut)
        else:
            out[..., ch] = lut[arr[..., ch]]
    return out
