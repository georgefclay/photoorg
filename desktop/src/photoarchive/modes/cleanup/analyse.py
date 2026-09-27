"""Measure one scan: where the print is, how skewed, how cast, how faded.

Runs on a downscale (`CLEANUP_ANALYSE_EDGE`, default 2000 px long edge) and
records everything in the *full-resolution display frame*, so the numbers can
be applied to the real pixels later without re-analysing. One image in memory
at a time — the laptop is memory-tight.

Nothing here writes to the database or to a file. `analyse_photo` returns an
`Analysis`; `job.py` stores it and `render.py` applies it.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageOps

from ...config import Settings
from ..ingest.image_io import open_image
from . import ops
from .geometry import Rect, Transform, inset_px_for_dpi, normalise_angle, transform_for

log = logging.getLogger(__name__)


# Bed detection: how far from the bed's own tone a pixel must be to count as
# print, and how saturated a pixel must be to count as print regardless.
BED_DELTA = 25
BED_SAT_MIN = 50
# A saturated pixel only counts as ink if it is also bright enough to be ink.
# HSV saturation is (max-min)/max, so a near-black pixel with a few levels of
# scanner noise reads as *highly* saturated — without this floor a black bed
# masks in as one giant print covering the whole scan.
BED_SAT_MIN_VALUE = 48
BED_WHITE_MIN = 200
BED_BLACK_MAX = 55
# The border ring we sample to guess the bed's tone.
BED_RING_FRAC = 0.02
# The print's interior, inset this much on each side, is what we measure the
# tone from — the very edge carries the paper border and the bed's bleed.
TONE_INSET_FRAC = 0.08


@dataclass
class Analysis:
    photo_id: int
    status: str                       # 'pending' | 'clean'
    operations: dict[str, Any] = field(default_factory=dict)
    transform: Transform | None = None
    split_regions: list[dict[str, Any]] | None = None
    needs_manual: bool = False
    manual_reason: str | None = None
    analysis_ms: int = 0
    src_w: int = 0
    src_h: int = 0

    @property
    def op_names(self) -> list[str]:
        return sorted((self.operations.get("ops") or {}).keys())

    @property
    def is_geometric_only(self) -> bool:
        names = set(self.op_names)
        return bool(names) and names <= {"deskew", "crop"}


@dataclass
class BedInfo:
    kind: str          # 'white' | 'black'
    grey: float
    score: float
    mask: np.ndarray = field(repr=False, default_factory=lambda: np.zeros((1, 1), bool))


# --------------------------------------------------------------------------
# Bed / print detection
# --------------------------------------------------------------------------

def _border_ring(grey: np.ndarray) -> np.ndarray:
    h, w = grey.shape[:2]
    t = max(1, int(round(min(h, w) * BED_RING_FRAC)))
    ring = np.concatenate([
        grey[:t, :].ravel(), grey[-t:, :].ravel(),
        grey[:, :t].ravel(), grey[:, -t:].ravel(),
    ])
    return ring


def _print_mask(rgb: np.ndarray, bed_kind: str, bed_grey: float) -> np.ndarray:
    grey = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    sat, value = hsv[..., 1], hsv[..., 2]
    if bed_kind == "white":
        mask = (grey < (bed_grey - BED_DELTA))
    else:
        mask = (grey > (bed_grey + BED_DELTA))
    # Colour is print even where luminance matches the bed — but only above
    # the brightness floor (see BED_SAT_MIN_VALUE).
    mask = mask | ((sat > BED_SAT_MIN) & (value > BED_SAT_MIN_VALUE))
    mask = mask.astype(np.uint8) * 255

    # Close gaps inside a print (a sky, a white shirt), then drop specks.
    long_edge = max(rgb.shape[0], rgb.shape[1])
    k = max(3, int(round(long_edge * 0.01)) | 1)
    close_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close_kernel)
    k2 = max(3, int(round(long_edge * 0.004)) | 1)
    open_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k2, k2))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, open_kernel)
    return mask


@dataclass
class Component:
    rect: Rect
    area_frac: float           # component pixels / image pixels
    rectangularity: float      # component pixels / minAreaRect area
    mask: np.ndarray = field(repr=False, default_factory=lambda: np.zeros((1, 1), bool))


def _components(mask: np.ndarray, min_frac: float) -> list[Component]:
    img_area = float(mask.shape[0] * mask.shape[1])
    n, labels, stats, _ = cv2.connectedComponentsWithStats(
        (mask > 0).astype(np.uint8), connectivity=8,
    )
    out: list[Component] = []
    for label in range(1, n):
        area = float(stats[label, cv2.CC_STAT_AREA])
        if area / img_area < min_frac:
            continue
        comp = (labels == label)
        pts = cv2.findNonZero(comp.astype(np.uint8))
        if pts is None:
            continue
        (cx, cy), (w, h), angle = cv2.minAreaRect(pts)
        angle, w, h = normalise_angle(angle, w, h)
        rect = Rect(cx=float(cx), cy=float(cy), w=float(w), h=float(h),
                    angle=float(angle))
        rect_area = rect.area or 1.0
        out.append(Component(
            rect=rect, area_frac=area / img_area,
            rectangularity=min(1.0, area / rect_area), mask=comp,
        ))
    out.sort(key=lambda c: c.area_frac, reverse=True)
    return out


def detect_bed_and_prints(
    rgb: np.ndarray, settings: Settings,
) -> tuple[BedInfo, list[Component]]:
    """Try a white bed and a black bed; keep whichever explains the scan
    better. "Better" = the largest component covers a plausible fraction of
    the scan and looks rectangular.
    """
    grey = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    ring = _border_ring(grey)
    ring_med = float(np.median(ring))

    if ring_med >= BED_WHITE_MIN:
        candidates = [("white", ring_med)]
    elif ring_med <= BED_BLACK_MAX:
        candidates = [("black", ring_med)]
    else:
        # Ambiguous — a print that runs to the edge, or a grey bed. Try both
        # against the extremes rather than the ring median.
        candidates = [("white", max(ring_med, 235.0)), ("black", min(ring_med, 20.0))]

    best: tuple[BedInfo, list[Component]] | None = None
    for kind, bed_grey in candidates:
        mask = _print_mask(rgb, kind, bed_grey)
        comps = _components(mask, settings.CLEANUP_SPLIT_MIN_FRAC * 0.5)
        if comps:
            top = comps[0]
            # Reward a big, rectangular component; penalise one that fills
            # the whole scan (that means the mask caught the bed too).
            fill = top.area_frac
            score = top.rectangularity * (1.0 - abs(fill - 0.75))
        else:
            score = 0.0
        info = BedInfo(kind=kind, grey=bed_grey, score=round(score, 4), mask=mask > 0)
        if best is None or score > best[0].score:
            best = (info, comps)
    assert best is not None
    return best


# --------------------------------------------------------------------------
# Tone measurement
# --------------------------------------------------------------------------

def _interior_mask(rect: Rect, shape: tuple[int, int]) -> np.ndarray:
    """Boolean mask of the print's interior — the rect pulled in by
    TONE_INSET_FRAC so the paper border and the bed's bleed stay out of the
    tone statistics.
    """
    h, w = shape[:2]
    inset_w = abs(rect.w) * TONE_INSET_FRAC
    inset_h = abs(rect.h) * TONE_INSET_FRAC
    inner = Rect(cx=rect.cx, cy=rect.cy,
                 w=max(1.0, abs(rect.w) - 2 * inset_w),
                 h=max(1.0, abs(rect.h) - 2 * inset_h),
                 angle=rect.angle)
    mask = np.zeros((h, w), dtype=np.uint8)
    pts = np.array([[int(round(x)), int(round(y))] for x, y in inner.corners()],
                   dtype=np.int32)
    cv2.fillConvexPoly(mask, pts, 1)
    return mask.astype(bool)


def measure_tone(
    rgb: np.ndarray, rect: Rect, settings: Settings,
) -> dict[str, Any]:
    """Colour-cast and levels measurements, plus the mono/sepia verdict that
    vetoes the colour op."""
    mask = _interior_mask(rect, rgb.shape[:2])
    if int(mask.sum()) < 500:
        mask = np.ones(rgb.shape[:2], dtype=bool)

    chroma = ops.measure_chroma(rgb, mask)
    is_mono = chroma["p95_chroma"] < settings.CLEANUP_MONO_CHROMA_MAX
    is_sepia = (
        not is_mono
        and chroma["hue_variance"] <= settings.CLEANUP_SEPIA_HUE_VAR_MAX
    )
    chroma["is_mono"] = bool(is_mono)
    chroma["is_sepia"] = bool(is_sepia)

    cast = ops.measure_cast(rgb, mask,
                            neutral_pct=settings.CLEANUP_CAST_NEUTRAL_PCT)
    # Kept for comparison: plain grey-world over every mid-tone, the estimator
    # this replaced. The gap between the two is how much of the "cast" was
    # really the scene's own colour.
    cast["grey_world_magnitude"] = ops.measure_cast(
        rgb, mask, neutral_pct=100.0)["magnitude"]
    levels = ops.measure_levels(rgb, mask)
    return {"chroma": chroma, "cast": cast, "levels": levels,
            "mask_px": int(mask.sum())}


# --------------------------------------------------------------------------
# The analyser
# --------------------------------------------------------------------------

def load_for_analysis(
    path: Path, edge: int,
) -> tuple[np.ndarray, int, int, float]:
    """Open the working copy, put it in the display frame, and downscale.

    Returns (rgb_small, src_w, src_h, scale) where `scale` multiplies a
    coordinate in the small frame to get the full-resolution one.
    """
    with open_image(path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode != "RGB":
            im = im.convert("RGB")
        src_w, src_h = im.size
        long_edge = max(src_w, src_h)
        if long_edge > edge:
            factor = edge / float(long_edge)
            small = im.resize(
                (max(1, int(round(src_w * factor))),
                 max(1, int(round(src_h * factor)))),
                Image.LANCZOS,
            )
        else:
            small = im.copy()
        arr = np.asarray(small)
    scale = src_w / float(arr.shape[1])
    return arr, src_w, src_h, scale


def analyse_photo(
    settings: Settings,
    *,
    photo_id: int,
    working_path: Path,
    dpi: int | None = None,
    has_back: bool = False,
) -> Analysis:
    """Measure one scan and decide which ops it needs.

    `has_back` forces `needs_manual`: which child of a split owns the
    scanned back is not something the analyser may guess (Phase 7 answer 2).
    """
    started = time.perf_counter()
    rgb, src_w, src_h, scale = load_for_analysis(
        working_path, settings.CLEANUP_ANALYSE_EDGE,
    )
    result = Analysis(photo_id=photo_id, status="pending",
                      src_w=src_w, src_h=src_h)

    bed, comps = detect_bed_and_prints(rgb, settings)
    measured: dict[str, Any] = {
        "bed": {"kind": bed.kind, "grey": round(bed.grey, 1), "score": bed.score},
        "analysis": {"edge": int(max(rgb.shape[0], rgb.shape[1])),
                     "scale": round(scale, 5),
                     "src_w": src_w, "src_h": src_h},
        "ops": {},
    }
    inset = inset_px_for_dpi(dpi, settings.CLEANUP_CROP_INSET_PX_AT_300)
    measured["analysis"]["dpi"] = dpi
    measured["analysis"]["inset_px"] = round(inset, 2)

    if not comps:
        result.needs_manual = True
        result.manual_reason = "no_print_found"
        result.operations = measured
        result.analysis_ms = int((time.perf_counter() - started) * 1000)
        return result

    # --- the print, in full-resolution display coordinates ---------------
    top = comps[0]
    rect_full = top.rect.scaled(scale)
    measured["print_rect"] = rect_full.to_json()
    measured["print_frac"] = round(top.area_frac, 4)
    measured["rectangularity"] = round(top.rectangularity, 4)

    # --- multi-print split ----------------------------------------------
    # Found first, because it changes how the size gate below is read: on a
    # scan of three prints the *largest* one covers only a third of the bed,
    # which is not the analyser failing to find a print.
    prints = [
        c for c in comps
        if c.area_frac >= settings.CLEANUP_SPLIT_MIN_FRAC
        and c.rectangularity >= settings.CLEANUP_SPLIT_RECTANGULARITY
    ]
    is_multi = len(prints) >= 2 and not has_back
    if len(prints) >= 2:
        measured["print_frac_all"] = round(sum(c.area_frac for c in prints), 4)

    if is_multi:
        # The per-region gates (CLEANUP_SPLIT_MIN_FRAC + rectangularity) have
        # already judged every print, so the whole-scan size gate does not
        # apply. The aspect and skew checks still do, per region.
        worst = max(prints, key=lambda c: c.rect.scaled(scale).aspect)
        if worst.rect.scaled(scale).aspect > settings.CLEANUP_MAX_ASPECT:
            result.needs_manual = True
            result.manual_reason = "implausible_aspect"
        elif any(abs(c.rect.scaled(scale).angle) > settings.CLEANUP_MAX_DESKEW_DEG
                 for c in prints):
            result.needs_manual = True
            result.manual_reason = "skew_too_large"
    elif top.area_frac < settings.CLEANUP_MIN_PRINT_FRAC:
        result.needs_manual = True
        result.manual_reason = "print_too_small"
    elif rect_full.aspect > settings.CLEANUP_MAX_ASPECT:
        result.needs_manual = True
        result.manual_reason = "implausible_aspect"
    elif abs(rect_full.angle) > settings.CLEANUP_MAX_DESKEW_DEG:
        result.needs_manual = True
        result.manual_reason = "skew_too_large"

    if len(prints) >= 2:
        if has_back:
            result.needs_manual = True
            result.manual_reason = "has_back"
            measured["split_candidates"] = len(prints)
        else:
            regions = []
            for i, c in enumerate(prints, start=1):
                r_full = c.rect.scaled(scale)
                t = transform_for(r_full, src_w=src_w, src_h=src_h,
                                  deskew=abs(r_full.angle) >= settings.CLEANUP_DESKEW_MIN_DEG,
                                  crop=True, inset_px=inset)
                regions.append({
                    "index": i,
                    "rect": r_full.to_json(),
                    "area_frac": round(c.area_frac, 4),
                    "rectangularity": round(c.rectangularity, 4),
                    "transform": t.to_json(),
                })
            result.split_regions = regions
            measured["ops"]["split"] = {
                "regions": len(regions),
                "area_fracs": [r["area_frac"] for r in regions],
            }

    # --- geometry ---------------------------------------------------------
    deskew_wanted = (
        not result.needs_manual
        and abs(rect_full.angle) >= settings.CLEANUP_DESKEW_MIN_DEG
    )
    if deskew_wanted:
        measured["ops"]["deskew"] = {"angle_deg": round(rect_full.angle, 3)}

    crop_wanted = False
    if not result.needs_manual:
        candidate = transform_for(
            rect_full, src_w=src_w, src_h=src_h,
            deskew=deskew_wanted, crop=True, inset_px=inset,
        )
        src_area = float(src_w * src_h)
        removed = 1.0 - (candidate.out_w * candidate.out_h) / src_area
        if removed >= settings.CLEANUP_CROP_MIN_FRAC:
            crop_wanted = True
            measured["ops"]["crop"] = {
                "rect": [round(v, 2) for v in (candidate.crop or (0, 0, 0, 0))],
                "out_w": candidate.out_w, "out_h": candidate.out_h,
                "inset_px": round(inset, 2),
                "removed_frac": round(removed, 4),
            }

    # --- tone -------------------------------------------------------------
    tone = measure_tone(rgb, top.rect, settings)
    measured["tone"] = tone
    chroma = tone["chroma"]
    cast = tone["cast"]
    levels = tone["levels"]

    if chroma["is_mono"]:
        measured["colour_skipped"] = "mono"
    elif chroma["is_sepia"]:
        measured["colour_skipped"] = "sepia"
    elif cast["magnitude"] >= settings.CLEANUP_CAST_MIN:
        interior = _interior_mask(top.rect, rgb.shape[:2])
        gains = ops.cast_gains(cast, rgb, interior,
                               neutral_pct=settings.CLEANUP_CAST_NEUTRAL_PCT)
        if any(abs(gains[k] - 1.0) > 0.005 for k in ("r", "g", "b")):
            measured["ops"]["colour"] = {
                "cast_a": cast["a"], "cast_b": cast["b"],
                "magnitude": cast["magnitude"],
                "grey_world_magnitude": cast.get("grey_world_magnitude"),
                "neutral_pct": settings.CLEANUP_CAST_NEUTRAL_PCT,
                "sample_px": cast.get("n"),
                "gains": gains,
                "rgb_shift": ops.rgb_shift_from_gains(gains),
            }
    else:
        measured["colour_skipped"] = "below_threshold"

    if levels["contrast"] < settings.CLEANUP_CONTRAST_LOW:
        interior = _interior_mask(top.rect, rgb.shape[:2])
        after = ops.measure_contrast_after(
            rgb, lo=levels["lo"], hi=levels["hi"],
            s_curve=settings.CLEANUP_SCURVE, mask=interior,
        )
        measured["ops"]["levels"] = {
            "lo": levels["lo"], "hi": levels["hi"],
            "s_curve": settings.CLEANUP_SCURVE,
            "contrast_before": levels["contrast"],
            "contrast_after": round(after, 4),
        }

    # --- verdict ----------------------------------------------------------
    result.operations = measured
    if measured["ops"]:
        result.transform = transform_for(
            rect_full, src_w=src_w, src_h=src_h,
            deskew=deskew_wanted, crop=crop_wanted, inset_px=inset,
        )
        result.status = "pending"
    elif result.needs_manual:
        # No op we can name, but we couldn't read the scan either — George
        # still gets to look at it.
        result.transform = None
        result.status = "pending"
    else:
        result.status = "clean"
        result.transform = None

    result.analysis_ms = int((time.perf_counter() - started) * 1000)
    return result


def caption_for(operations: dict[str, Any]) -> str:
    """The one-line summary the review pane shows — measured numbers, not
    adjectives."""
    o = (operations or {}).get("ops") or {}
    bits: list[str] = []
    if "deskew" in o:
        bits.append(f"skew {o['deskew']['angle_deg']:+.1f}°")
    if "crop" in o:
        bits.append(f"crop {o['crop']['removed_frac'] * 100:.0f}%")
    if "split" in o:
        bits.append(f"split {o['split']['regions']} prints")
    if "colour" in o:
        s = o["colour"]["rgb_shift"]
        parts = [f"{ch.upper()}{s[ch]:+d}" for ch in ("r", "g", "b") if s[ch]]
        bits.append("cast " + " ".join(parts) if parts else "cast")
    if "levels" in o:
        lv = o["levels"]
        bits.append(
            f"levels {lv['contrast_before']:.2f}→{lv['contrast_after']:.2f}"
        )
    if "remote_enhance" in o:
        bits.append("remote enhance")
    skipped = (operations or {}).get("colour_skipped")
    if skipped in ("mono", "sepia"):
        bits.append(f"colour skipped ({skipped})")
    return ", ".join(bits) if bits else "nothing to change"
