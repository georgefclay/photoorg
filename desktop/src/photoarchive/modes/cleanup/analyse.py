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


def _local_std(grey: np.ndarray, win: int) -> np.ndarray:
    """Per-pixel standard deviation over a `win` x `win` window."""
    g = grey.astype(np.float32)
    mean = cv2.blur(g, (win, win))
    sq = cv2.blur(g * g, (win, win))
    return np.sqrt(np.maximum(0.0, sq - mean * mean))


def _bed_reachable_from_border(bed_mask: np.ndarray) -> np.ndarray:
    """Restrict a bed mask to the part the scan's border can reach.

    A large smooth pale area *inside* the print — an overexposed sky, a white
    wall — is bed-coloured and calm, but it is enclosed by photograph. Only
    bed that touches the outside of the scan is really bed.
    """
    h, w = bed_mask.shape
    filled = bed_mask.copy()
    ff = np.zeros((h + 2, w + 2), np.uint8)
    step_x = max(1, w // 64)
    step_y = max(1, h // 64)
    seeds = ([(x, 0) for x in range(0, w, step_x)]
             + [(x, h - 1) for x in range(0, w, step_x)]
             + [(0, y) for y in range(0, h, step_y)]
             + [(w - 1, y) for y in range(0, h, step_y)])
    for x, y in seeds:
        if filled[y, x] == 1:
            cv2.floodFill(filled, ff, (x, y), 2)
    return (filled == 2).astype(np.uint8)


def _print_mask_calm(
    rgb: np.ndarray, bed: "BedInfo", settings: Settings,
) -> np.ndarray:
    """Bed is near the bed tone, locally flat, and reachable from the border.

    Fix-up 3. The brightness mask below calls a pixel bed when it is *darker
    than the bed*, which is why photo #15's white cardigan and white curtain
    became bed and the crop took a person's arm with them. Scanner bed is calm
    in a way no photograph is — #15's bed reads std 1.2-1.6 — so calmness
    separates them where brightness cannot.

    It is not sufficient on its own: #15's curtain is locally smooth at a 7 px
    window even though a full strip across it reads 10.5, so the guard in
    `guard_crop_edges` remains the thing that actually protects the picture.
    What this buys is a much better starting rectangle — measured over batches
    1-5, the guard's p90 residual push fell from 2.14 % to 0.35 %.
    """
    grey = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    noise = bed_noise(rgb, bed, settings)
    allowed = min(settings.CLEANUP_BED_MAX_STD,
                  max(settings.CLEANUP_BED_MIN_STD,
                      settings.CLEANUP_BED_NOISE_FACTOR * noise))
    long_edge = max(rgb.shape[0], rgb.shape[1])
    win = max(settings.CLEANUP_MASK_STD_WINDOW_MIN,
              int(round(long_edge * settings.CLEANUP_MASK_STD_WINDOW_FRAC))) | 1
    near = np.abs(grey.astype(np.float32) - bed.grey) <= settings.CLEANUP_BED_TOL
    calm = _local_std(grey, win) <= allowed
    bed_mask = _bed_reachable_from_border((near & calm).astype(np.uint8))
    mask = ((1 - bed_mask) * 255).astype(np.uint8)
    return _clean_mask(mask, rgb)


def _clean_mask(mask: np.ndarray, rgb: np.ndarray) -> np.ndarray:
    """Close gaps inside a print (a sky, a white shirt), then drop specks."""
    long_edge = max(rgb.shape[0], rgb.shape[1])
    k = max(3, int(round(long_edge * 0.01)) | 1)
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    k2 = max(3, int(round(long_edge * 0.004)) | 1)
    return cv2.morphologyEx(
        mask, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k2, k2)))


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
    return _clean_mask(mask.astype(np.uint8) * 255, rgb)


# Fix-up 2: how much of the print's outline has to agree on an orientation
# before we believe it, and how close counts as agreeing.
EDGE_TOL_DEG = 2.0
EDGE_MIN_CONFIDENCE = 0.55
# Contour simplification, as a fraction of the perimeter. Big enough to turn a
# jagged threshold boundary into straight runs, small enough to keep a real
# corner.
EDGE_APPROX_FRAC = 0.004


@dataclass
class EdgeOrientation:
    """How tilted the print's *edges* are, and how much of the outline says so."""
    angle: float
    confidence: float
    total_length: float
    agreeing_length: float

    def to_json(self) -> dict[str, float]:
        return {"angle": round(self.angle, 3),
                "confidence": round(self.confidence, 4),
                "total_length": round(self.total_length, 1),
                "agreeing_length": round(self.agreeing_length, 1)}


def _fold_angle(deg: float) -> float:
    """Fold an edge direction into [-45, 45).

    A rectangle's four edges point four different ways but share one
    orientation, so 0, 90, 180 and 270 are the same tilt.
    """
    return ((deg + 45.0) % 90.0) - 45.0


def edge_orientation(
    mask: np.ndarray,
    *,
    tol_deg: float = EDGE_TOL_DEG,
    approx_frac: float = EDGE_APPROX_FRAC,
) -> EdgeOrientation | None:
    """The tilt of the print, measured from its outline.

    `minAreaRect` gives the *minimum enclosing* rectangle, whose angle is
    pinned by whatever sticks out furthest — a torn corner, a spur of bed, the
    print running off the edge of the scan. On a print that is not a clean
    rectangle that angle has nothing to do with the print's edges, and
    deskewing by it tilts a photograph that was straight (photo #306).

    So measure the edges themselves: simplify the outline to straight runs,
    fold each run's direction into [-45, 45), and take the length-weighted
    consensus. Four long straight edges outvote a torn corner, and when
    nothing wins, `confidence` says so and the caller leaves the photo alone.
    """
    cnts, _ = cv2.findContours((mask > 0).astype(np.uint8),
                               cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    contour = max(cnts, key=cv2.contourArea)
    perimeter = cv2.arcLength(contour, True)
    if perimeter <= 0:
        return None
    poly = cv2.approxPolyDP(contour, approx_frac * perimeter, True)
    pts = poly.reshape(-1, 2).astype(np.float64)
    if len(pts) < 2:
        return None

    segments: list[tuple[float, float]] = []   # (folded angle, length)
    total = 0.0
    for i in range(len(pts)):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % len(pts)]
        length = float(np.hypot(x1 - x0, y1 - y0))
        if length <= 0:
            continue
        segments.append((_fold_angle(float(np.degrees(np.arctan2(y1 - y0,
                                                                x1 - x0)))),
                         length))
        total += length
    if total <= 0 or not segments:
        return None

    # Length-weighted vote. Doubling the angle makes the fold continuous, so
    # -44.9 and +44.9 (nearly the same tilt) average correctly instead of
    # cancelling.
    best_angle, best_weight = 0.0, -1.0
    for centre, _ in segments:
        weight = sum(l for a, l in segments
                     if abs(_fold_angle(a - centre)) <= tol_deg)
        if weight > best_weight:
            best_angle, best_weight = centre, weight
    agreeing = [(a, l) for a, l in segments
                if abs(_fold_angle(a - best_angle)) <= tol_deg]
    weight = sum(l for _, l in agreeing)
    if weight <= 0:
        return None
    sin_sum = sum(l * np.sin(np.radians(2 * _fold_angle(a - best_angle)))
                  for a, l in agreeing)
    cos_sum = sum(l * np.cos(np.radians(2 * _fold_angle(a - best_angle)))
                  for a, l in agreeing)
    refined = best_angle + np.degrees(np.arctan2(sin_sum, cos_sum)) / 2.0
    return EdgeOrientation(angle=_fold_angle(float(refined)),
                           confidence=weight / total,
                           total_length=total, agreeing_length=weight)


@dataclass
class Component:
    rect: Rect
    area_frac: float           # component pixels / image pixels
    rectangularity: float      # component pixels / minAreaRect area
    edges: "EdgeOrientation | None" = None
    mask: np.ndarray = field(repr=False, default_factory=lambda: np.zeros((1, 1), bool))


def _extent_at_angle(comp: np.ndarray, angle: float) -> Rect:
    """The component's extent measured in the frame `angle` straightens.

    Rotating the component's points by -angle and taking their axis-aligned
    bounds gives the print's real width and height, without the minimum
    enclosing rectangle's sensitivity to whatever sticks out furthest.
    """
    pts = cv2.findNonZero(comp.astype(np.uint8)).reshape(-1, 2).astype(np.float64)
    t = np.radians(-angle)
    cos_t, sin_t = np.cos(t), np.sin(t)
    xs = pts[:, 0] * cos_t - pts[:, 1] * sin_t
    ys = pts[:, 0] * sin_t + pts[:, 1] * cos_t
    x0, x1 = float(xs.min()), float(xs.max())
    y0, y1 = float(ys.min()), float(ys.max())
    # Centre back in the original frame.
    ccx, ccy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    back = np.radians(angle)
    cb, sb = np.cos(back), np.sin(back)
    cx = ccx * cb - ccy * sb
    cy = ccx * sb + ccy * cb
    return Rect(cx=float(cx), cy=float(cy),
                w=float(x1 - x0), h=float(y1 - y0), angle=float(angle))


def _component_from_mask(comp: np.ndarray, img_area: float) -> Component | None:
    """Measure one connected blob: its extent, tilt and rectangularity."""
    pts = cv2.findNonZero(comp.astype(np.uint8))
    if pts is None:
        return None
    (cx, cy), (w, h), angle = cv2.minAreaRect(pts)
    angle, w, h = normalise_angle(angle, w, h)

    # Fix-up 2: the tilt comes from the print's edges, not from the minimum
    # enclosing rectangle — see `edge_orientation`. The enclosing rectangle
    # still gives the extent, measured in the frame the edges say is straight.
    edges = edge_orientation(comp)
    if edges is not None and edges.confidence >= EDGE_MIN_CONFIDENCE:
        rect = _extent_at_angle(comp, edges.angle)
    else:
        rect = Rect(cx=float(cx), cy=float(cy), w=float(w), h=float(h),
                    angle=float(angle))
    rect_area = rect.area or 1.0
    area = float(np.count_nonzero(comp))
    return Component(
        rect=rect, area_frac=area / img_area,
        rectangularity=min(1.0, area / rect_area),
        edges=edges, mask=comp,
    )


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
        built = _component_from_mask(labels == label, img_area)
        if built is not None:
            out.append(built)
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
        probe = BedInfo(kind=kind, grey=bed_grey, score=0.0)
        if settings.CLEANUP_MASK_MODE == "calm":
            mask = _print_mask_calm(rgb, probe, settings)
        else:
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
# The content guard (fix-up 3)
# --------------------------------------------------------------------------

@dataclass
class EdgeVerdict:
    """What the guard found on one side of the proposed crop."""
    side: str
    original: float          # the edge the detector proposed
    resolved: float          # where it ended up
    found_bed: bool          # did we reach genuine bed?
    hit_boundary: bool       # or did we run off the scan first?
    steps: int
    outside_grey: float = 0.0
    outside_std: float = 0.0
    inside_edge_energy: float = 0.0
    interior_edge_energy: float = 0.0

    @property
    def moved_px(self) -> float:
        return abs(self.resolved - self.original)

    # How far the guard had to move this edge, as a fraction of the print's
    # short side. Filled in by `guard_crop_edges`.
    moved_frac: float = 0.0
    unclear: bool = False

    @property
    def runs_off_scan(self) -> bool:
        """The print reaches the edge of the scan on this side, so there is
        nothing to crop there. Common and harmless — not a failure."""
        return self.hit_boundary and not self.found_bed

    def to_json(self) -> dict[str, Any]:
        return {"side": self.side, "original": round(self.original, 1),
                "resolved": round(self.resolved, 1),
                "moved_px": round(self.moved_px, 1),
                "moved_frac": round(self.moved_frac, 4),
                "unclear": self.unclear,
                "runs_off_scan": self.runs_off_scan,
                "found_bed": self.found_bed, "hit_boundary": self.hit_boundary,
                "steps": self.steps,
                "outside_grey": round(self.outside_grey, 1),
                "outside_std": round(self.outside_std, 2),
                "inside_edge_energy": round(self.inside_edge_energy, 2),
                "interior_edge_energy": round(self.interior_edge_energy, 2)}


def _strip_stats(rgb: np.ndarray, x0: float, y0: float, x1: float, y1: float):
    """Mean grey, variance and edge energy of an axis-aligned strip."""
    h, w = rgb.shape[:2]
    xa, xb = max(0, int(round(x0))), min(w, int(round(x1)))
    ya, yb = max(0, int(round(y0))), min(h, int(round(y1)))
    if xb <= xa or yb <= ya:
        return None
    patch = rgb[ya:yb, xa:xb]
    grey = cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY) if patch.ndim == 3 else patch
    gx = cv2.Sobel(grey, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(grey, cv2.CV_32F, 0, 1, ksize=3)
    return {"grey": float(grey.mean()), "std": float(grey.std()),
            "edge": float(np.hypot(gx, gy).mean())}


def bed_noise(rgb: np.ndarray, bed: BedInfo, settings: Settings) -> float:
    """How textured *this scan's* bed is.

    A fixed calmness threshold cannot separate bed from picture: across the
    archive, genuine bed strips run from std 1.2 on a clean scan to 10 on a
    noisy one, and photo #15's white curtain sits at 10.5 — right in the
    middle. But within one scan the gap is decisive: #15's own bed reads 1.2
    to 1.6 against that curtain's 10.5.

    So measure the bed on the scan in front of us, from the corner patches,
    which are bed on anything that is not edge-to-edge print. Patches whose
    tone does not match the bed are dropped, so a corner covered by the print
    does not inflate the figure.
    """
    grey = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    h, w = grey.shape
    side = max(6, int(round(min(h, w) * 0.03)))
    corners = [grey[:side, :side], grey[:side, -side:],
               grey[-side:, :side], grey[-side:, -side:]]
    stds = [float(c.std()) for c in corners
            if abs(float(c.mean()) - bed.grey) <= settings.CLEANUP_BED_TOL]
    if not stds:
        return float(settings.CLEANUP_BED_MAX_STD)
    return float(np.median(stds))


def _looks_like_bed(
    stats: dict | None, bed: BedInfo, settings: Settings, *, noise: float,
) -> bool:
    """Is this strip scanner bed — the right tone, and no busier than the bed
    on this scan actually is?

    Both halves matter. Tone alone calls a white curtain or a white cardigan
    bed (photo #15); calmness alone calls a blank sky bed.
    """
    if stats is None:
        return False
    allowed = min(settings.CLEANUP_BED_MAX_STD,
                  max(settings.CLEANUP_BED_MIN_STD,
                      settings.CLEANUP_BED_NOISE_FACTOR * noise))
    return (abs(stats["grey"] - bed.grey) <= settings.CLEANUP_BED_TOL
            and stats["std"] <= allowed)


# --------------------------------------------------------------------------
# Prints that touch (fix-up 5)
# --------------------------------------------------------------------------

#: A gutter is bed all the way across the component, not merely in places.
GUTTER_MIN_BED = 0.90


def bed_by_tone(
    rgb: np.ndarray, bed: "BedInfo", settings: Settings,
) -> np.ndarray:
    """Pixels at the bed's tone that the outside of the scan can reach.

    The same three tests as `_print_mask_calm` — tone, flatness, reachability
    — but with a flatness window a fifth the size, and that difference is the
    whole point. A gutter between two prints is only as wide as the gap
    between them, and at 1.3 % of the long edge the mask's window is 26 px at
    analysis scale: wider than many gutters, so every pixel in one reads as
    busy because of the prints on either side of it. That is exactly why those
    prints arrive as one component in the first place. A small window fits
    inside the gutter and sees that it is flat.

    The other two tests still earn their place. Tone alone would call a pale
    band of picture a gutter; reachability answers that, because a gutter runs
    out to the edge of the scan and an overexposed sky is enclosed by
    photograph. Flatness alone would call that same sky a gutter; scanner bed
    is flat at every scale, while photographic paper carries grain and scanner
    noise even where it is nearly white.
    """
    grey = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    noise = bed_noise(rgb, bed, settings)
    allowed = min(settings.CLEANUP_BED_MAX_STD,
                  max(settings.CLEANUP_BED_MIN_STD,
                      settings.CLEANUP_BED_NOISE_FACTOR * noise))
    long_edge = max(rgb.shape[0], rgb.shape[1])
    win = max(3, int(round(long_edge * settings.CLEANUP_GUTTER_STD_WINDOW_FRAC))) | 1
    near = np.abs(grey.astype(np.float32) - bed.grey) <= settings.CLEANUP_BED_TOL
    calm = _local_std(grey, win) <= allowed
    return _bed_reachable_from_border((near & calm).astype(np.uint8)).astype(bool)


def _gutter_cuts(
    rgb: np.ndarray, comp_mask: np.ndarray, bed: BedInfo, settings: Settings,
    *, gutter: np.ndarray, axis: int, y0: int, y1: int, x0: int, x1: int,
) -> list[int]:
    """Where, along `axis`, this blob is cut in two by scanner bed.

    Photo #708 is three prints stacked on one bed; the lower two touch closely
    enough that one connected component covers both, so the analyser saw two
    prints and proposed a two-way split. The giveaway is a 62 px band of bed
    running straight across that component.

    The evidence is the bed map from `bed_by_tone`, not the absence of the
    print mask. On a scan whose prints have pale backgrounds the mask is full
    of holes that are not gutters (photo #1398); on a scan whose prints sit
    close together the mask has already bridged the gutter that is there,
    which is why they arrived as one component. A band counts when it is bed
    nearly all the way across the component.
    """
    band_map = gutter[y0:y1 + 1, x0:x1 + 1]
    profile = band_map.mean(axis=1 - axis)
    span = len(profile)
    min_gutter = max(3, int(round(0.004 * max(rgb.shape[0], rgb.shape[1]))))
    if span < 4 * min_gutter:
        return []

    cuts: list[int] = []
    start: int | None = None
    for i in range(span + 1):
        is_bed = i < span and profile[i] >= GUTTER_MIN_BED
        if is_bed:
            start = i if start is None else start
            continue
        if start is None:
            continue
        run_start, run_end = start, i - 1
        start = None
        # Interior only: the bed beyond either end of the blob is margin,
        # not a gutter.
        if run_start < min_gutter or run_end > span - 1 - min_gutter:
            continue
        if run_end - run_start + 1 < min_gutter:
            continue
        cuts.append((run_start + run_end) // 2)
    return cuts


def split_touching_prints(
    rgb: np.ndarray, comp: Component, bed: BedInfo, settings: Settings,
    *, gutter: np.ndarray,
) -> list[Component]:
    """Cut one component into the prints it is really made of.

    Returns `[comp]` unchanged unless the cut produces at least two pieces
    that each look like a print in their own right — a subdivision that
    produces one real print and a sliver is the detector being wrong twice,
    not a multi-print scan.
    """
    ys, xs = np.nonzero(comp.mask)
    if ys.size == 0:
        return [comp]
    # A component that fills the scan is one print scanned edge to edge, and
    # the bands inside it are picture, not bed. Photo #1033 is a single
    # photograph covering 98 % of the frame; cutting it in two took a quarter
    # of it away. Several prints on a bed always leave bed around them.
    if comp.area_frac > settings.CLEANUP_SPLIT_MAX_FILL:
        return [comp]
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())

    row_cuts = _gutter_cuts(rgb, comp.mask, bed, settings, gutter=gutter,
                            axis=0, y0=y0, y1=y1, x0=x0, x1=x1)
    col_cuts = _gutter_cuts(rgb, comp.mask, bed, settings, gutter=gutter,
                            axis=1, y0=y0, y1=y1, x0=x0, x1=x1)
    if not row_cuts and not col_cuts:
        return [comp]

    row_edges = [0] + [c + 1 for c in row_cuts] + [y1 - y0 + 1]
    col_edges = [0] + [c + 1 for c in col_cuts] + [x1 - x0 + 1]
    img_area = float(rgb.shape[0] * rgb.shape[1])

    pieces: list[Component] = []
    for ra, rb in zip(row_edges, row_edges[1:]):
        for ca, cb in zip(col_edges, col_edges[1:]):
            cell = np.zeros_like(comp.mask)
            cell[y0 + ra:y0 + rb, x0 + ca:x0 + cb] =                 comp.mask[y0 + ra:y0 + rb, x0 + ca:x0 + cb]
            if not cell.any():
                continue
            built = _component_from_mask(cell, img_area)
            if built is not None:
                pieces.append(built)

    good = [c for c in pieces
            if c.area_frac >= settings.CLEANUP_SPLIT_MIN_FRAC * 0.5
            and c.rectangularity >= settings.CLEANUP_SPLIT_RECTANGULARITY]
    if len(good) < 2:
        return [comp]
    good.sort(key=lambda c: c.area_frac, reverse=True)
    return good


def split_merged_components(
    rgb: np.ndarray, comps: list[Component], bed: BedInfo, settings: Settings,
) -> list[Component]:
    """Apply `split_touching_prints` to every component, largest first."""
    if not settings.CLEANUP_SPLIT_GUTTERS or not comps:
        return comps
    gutter = bed_by_tone(rgb, bed, settings)
    out: list[Component] = []
    for c in comps:
        out.extend(split_touching_prints(rgb, c, bed, settings, gutter=gutter))
    out.sort(key=lambda c: c.area_frac, reverse=True)
    return out


def guard_crop_edges(
    rgb: np.ndarray, rect: Rect, bed: BedInfo, settings: Settings,
) -> tuple[Rect, list[EdgeVerdict]]:
    """Push every crop edge outward until what lies beyond it is really bed.

    Invariant: **a crop removes scanner bed, never pixels of the photograph.**
    The print mask keys on "darker than the bed", so light picture content —
    a pale sky, a white curtain, a white cardigan — is indistinguishable from
    bed and falls outside the detected rectangle. On photo #15 that cut a
    person in half.

    The test that catches it is what lies *outside* the edge: if that is not
    bed, there is more print out there, so the edge moves out. (Fix-up 3
    proposed also requiring the inside strip to be high-detail, but #15's
    inside strip is smooth white fabric — calm, not busy — so an `and` of the
    two would have let it through. The inside energy is measured and reported
    as evidence rather than used as a veto.)

    An edge that reaches the scan boundary without finding bed is left at the
    boundary and marked unclear: the print runs off the scan there, so there
    is nothing to crop on that side.
    """
    h, w = rgb.shape[:2]
    x0 = rect.cx - abs(rect.w) / 2.0
    x1 = rect.cx + abs(rect.w) / 2.0
    y0 = rect.cy - abs(rect.h) / 2.0
    y1 = rect.cy + abs(rect.h) / 2.0
    short = max(4.0, min(abs(rect.w), abs(rect.h)))
    depth = max(3.0, settings.CLEANUP_EDGE_STRIP_FRAC * short)

    interior = _strip_stats(rgb, x0 + 3 * depth, y0 + 3 * depth,
                            x1 - 3 * depth, y1 - 3 * depth)
    interior_edge = interior["edge"] if interior else 0.0
    noise = bed_noise(rgb, bed, settings)

    limits = {"left": 0.0, "right": float(w), "top": 0.0, "bottom": float(h)}
    verdicts: list[EdgeVerdict] = []
    resolved = {"left": x0, "right": x1, "top": y0, "bottom": y1}

    for side in ("left", "right", "top", "bottom"):
        start = resolved[side]
        pos = start
        steps = 0
        found = False
        hit_boundary = False
        outside = None
        inside = None
        # Walk outward a strip at a time until the far side is bed.
        # The strip being judged starts one depth beyond the edge. A strip
        # flush against it straddles the print's own boundary — a few pixels
        # of antialiased print in an otherwise clean strip raise its variance
        # and it never reads as bed, so every edge would creep outward by a
        # strip or two on a perfectly good scan.
        while True:
            if side == "left":
                outside = _strip_stats(rgb, pos - 2 * depth, y0, pos - depth, y1)
                inside = _strip_stats(rgb, pos, y0, pos + depth, y1)
                at_limit = pos - 2 * depth <= limits[side]
            elif side == "right":
                outside = _strip_stats(rgb, pos + depth, y0, pos + 2 * depth, y1)
                inside = _strip_stats(rgb, pos - depth, y0, pos, y1)
                at_limit = pos + 2 * depth >= limits[side]
            elif side == "top":
                outside = _strip_stats(rgb, x0, pos - 2 * depth, x1, pos - depth)
                inside = _strip_stats(rgb, x0, pos, x1, pos + depth)
                at_limit = pos - 2 * depth <= limits[side]
            else:
                outside = _strip_stats(rgb, x0, pos + depth, x1, pos + 2 * depth)
                inside = _strip_stats(rgb, x0, pos - depth, x1, pos)
                at_limit = pos + 2 * depth >= limits[side]

            if _looks_like_bed(outside, bed, settings, noise=noise):
                found = True
                break
            if at_limit:
                hit_boundary = True
                pos = limits[side]
                break
            pos += -depth if side in ("left", "top") else depth
            steps += 1
            if steps > 200:                      # cannot happen; belt and braces
                hit_boundary = True
                break

        resolved[side] = pos
        verdict = EdgeVerdict(
            side=side, original=start, resolved=pos, found_bed=found,
            hit_boundary=hit_boundary, steps=steps,
            outside_grey=(outside or {}).get("grey", 0.0),
            outside_std=(outside or {}).get("std", 0.0),
            inside_edge_energy=(inside or {}).get("edge", 0.0),
            interior_edge_energy=interior_edge,
        )
        verdict.moved_frac = verdict.moved_px / short
        # An edge the guard had to drag a long way means the detected
        # rectangle was materially wrong there — the mask missed light print
        # content, which is how #15 cut a person in half. Even the guarded
        # edge does not deserve trust, so that side is not cropped at all.
        verdict.unclear = verdict.moved_frac > settings.CLEANUP_EDGE_MAX_MOVE_FRAC
        if verdict.unclear:
            resolved[side] = limits[side]
        verdicts.append(verdict)

    guarded = Rect(
        cx=(resolved["left"] + resolved["right"]) / 2.0,
        cy=(resolved["top"] + resolved["bottom"]) / 2.0,
        w=max(1.0, resolved["right"] - resolved["left"]),
        h=max(1.0, resolved["bottom"] - resolved["top"]),
        angle=rect.angle,
    )
    return guarded, verdicts


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
    # Fix-up 1: the paper white. An age cast stains it; a scene colour does
    # not, so this is the second opinion the correction has to satisfy.
    highlight = ops.measure_highlight_cast(
        rgb, mask, pct=settings.CLEANUP_CAST_HIGHLIGHT_PCT)
    levels = ops.measure_levels(rgb, mask)
    return {"chroma": chroma, "cast": cast, "highlight": highlight,
            "levels": levels, "mask_px": int(mask.sum())}


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
        # Raw dimensions first: `draft` below changes `size`, and everything
        # downstream is in the full-resolution display frame.
        raw_w, raw_h = im.size

        # Decode at a reduced scale where the format allows it. A JPEG can be
        # decoded straight out of the DCT at 1/2, 1/4 or 1/8, so a 38 MP scan
        # costs ~5 MB instead of the 114 MB a full decode would take — and the
        # result is downscaled to `edge` regardless. On this archive the
        # biggest scan is 93.7 MP, and the laptop has under 2 GB free.
        #
        # The requested box must match the image's aspect ratio. Pillow picks
        # the scale as min(w // box_w, h // box_h), so a square (edge, edge)
        # box is governed by the SHORT edge: a 4400x3000 scan asking for
        # 2000x2000 gets min(2, 1) = 1 and is decoded in full. Asking in
        # proportion gives min(2, 2) = 2, and still guarantees the reduced long
        # edge is >= `edge`, so the resize below never upscales.
        try:
            raw_long = max(raw_w, raw_h)
            if raw_long > edge:
                f = edge / float(raw_long)
                im.draft("RGB", (max(1, int(raw_w * f)),
                                 max(1, int(raw_h * f))))
        except Exception:
            pass                      # TIFF and friends: no-op, decode in full

        # In place: `exif_transpose` returns `image.copy()` when there is no
        # orientation to apply, which on the 93.7 MP TIFF is a 281 MB copy to
        # produce an identical image. Almost every scan is orientation 1.
        pre = im.size
        ImageOps.exif_transpose(im, in_place=True)
        # Whether the display frame swaps axes is read off what the transpose
        # actually did, not off EXIF tag 0x0112: `exif_transpose` also honours
        # an XMP orientation, and the two must never disagree about which way
        # round `src_w`/`src_h` are. (A square image cannot be told apart here,
        # and does not need to be.)
        if im.size == (pre[1], pre[0]) and pre[0] != pre[1]:
            src_w, src_h = raw_h, raw_w
        else:
            src_w, src_h = raw_w, raw_h

        long_edge = max(im.size)
        target = None
        if long_edge > edge:
            factor = edge / float(long_edge)
            target = (max(1, int(round(im.width * factor))),
                      max(1, int(round(im.height * factor))))

        # Downscale before converting, where the two commute. Twelve of the
        # archive's scans are greyscale TIFFs; the largest is 93.7 MP, which
        # decodes to 94 MB but costs another 281 MB the moment it becomes RGB
        # — for a picture that is about to be thrown away at 2000 px.
        # Replicating one channel into three commutes exactly with a linear
        # resample, so the order is free to change. Palette, 16-bit and alpha
        # modes either do not resample faithfully or do not commute; they keep
        # the old order.
        if target is not None and im.mode in ("L", "RGB"):
            small = im.resize(target, Image.LANCZOS)
        else:
            if im.mode != "RGB":
                im = im.convert("RGB")
            small = im.resize(target, Image.LANCZOS) if target else im.copy()
        if small.mode != "RGB":
            small = small.convert("RGB")
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
    ai_label: str | None = None,
) -> Analysis:
    """Measure one scan and decide which ops it needs.

    `has_back` forces `needs_manual`: which child of a split owns the
    scanned back is not something the analyser may guess (Phase 7 answer 2).

    `ai_label` is the classifier's verdict, when the classify job has run.
    A `document` never splits (fix-up 5): photo #3839 is a newspaper cutting
    whose columns of text, separated by white gutters, read as two prints.
    """
    started = time.perf_counter()
    rgb, src_w, src_h, scale = load_for_analysis(
        working_path, settings.CLEANUP_ANALYSE_EDGE,
    )
    result = Analysis(photo_id=photo_id, status="pending",
                      src_w=src_w, src_h=src_h)

    bed, whole_comps = detect_bed_and_prints(rgb, settings)
    # Prints that touch arrive as one component, so they are cut apart before
    # the split gates judge anything (fix-up 5, photo #708). The cut decides
    # the *split* only: which component is "the print" for cropping still
    # comes from the uncut list unless a split is actually proposed. A cut
    # that does not become a split is a fragment of one photograph, and
    # letting the crop follow it removed a third of a clean scan.
    cut_comps = split_merged_components(rgb, whole_comps, bed, settings)
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

    if not whole_comps:
        result.needs_manual = True
        result.manual_reason = "no_print_found"
        result.operations = measured
        result.analysis_ms = int((time.perf_counter() - started) * 1000)
        return result

    # --- multi-print split ----------------------------------------------
    # Decided first, because it changes how the size gate below is read: on a
    # scan of three prints the *largest* one covers only a third of the bed,
    # which is not the analyser failing to find a print. It also decides which
    # component list the crop works from.
    #
    # A region counts as a print if it is big in absolute terms OR big
    # relative to the largest region (fix-up 5). Eight prints on one bed are
    # ~10 % of the scan each and every one failed the absolute gate, but what
    # makes them prints is that they are all the same size as each other.
    def _split_candidates(components: list[Component]) -> list[Component]:
        biggest = max((c.area_frac for c in components), default=0.0)
        rel_floor = biggest * settings.CLEANUP_SPLIT_REL_MIN
        return [
            c for c in components
            if (c.area_frac >= settings.CLEANUP_SPLIT_MIN_FRAC
                or (c.area_frac >= rel_floor
                    and c.area_frac >= settings.CLEANUP_SPLIT_MIN_FRAC * 0.5))
            and c.rectangularity >= settings.CLEANUP_SPLIT_RECTANGULARITY
        ]

    # Try the cut pieces, and fall back to the uncut components when cutting
    # did not help. On photo #3833 the cut turned four prints into seven
    # pieces that no longer looked like prints, and judging only the cut list
    # lost a four-way split that was already correct. Cutting may find a
    # split; it may never destroy one.
    prints = _split_candidates(cut_comps)
    used_cut = True
    if len(prints) < 2 and cut_comps is not whole_comps:
        fallback = _split_candidates(whole_comps)
        if len(fallback) >= 2:
            prints, used_cut = fallback, False

    skip_labels = {x.strip().lower()
                   for x in (settings.CLEANUP_SPLIT_SKIP_LABELS or "").split(",")
                   if x.strip()}
    label_vetoes = bool(ai_label) and ai_label.strip().lower() in skip_labels
    is_multi = len(prints) >= 2 and not has_back and not label_vetoes

    # Only a scan that really is several prints is measured from the cut
    # pieces. Everywhere else the crop follows the whole component, because a
    # piece of one photograph is not a smaller photograph.
    comps = cut_comps if (is_multi and used_cut) else whole_comps
    if len(prints) >= 2:
        measured["print_frac_all"] = round(sum(c.area_frac for c in prints), 4)
    if len(cut_comps) != len(whole_comps):
        measured["gutter_cuts"] = {
            "components_before": len(whole_comps),
            "components_after": len(cut_comps),
            "used": is_multi and used_cut,
        }
    if label_vetoes:
        measured["split_vetoed_by_label"] = ai_label

    # --- the print, in full-resolution display coordinates ---------------
    top = comps[0]
    rect_full = top.rect.scaled(scale)
    measured["print_rect"] = rect_full.to_json()
    measured["print_frac"] = round(top.area_frac, 4)
    measured["rectangularity"] = round(top.rectangularity, 4)
    measured["edges"] = top.edges.to_json() if top.edges else None

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

    if len(prints) >= 2 and label_vetoes:
        # Offer the crop, never the split: the regions are columns of text.
        measured["split_candidates"] = len(prints)
    elif len(prints) >= 2:
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
    # Fix-up 2: only deskew when the print's own edges agree on a tilt. An
    # unconfident reading means the outline is not a rectangle — a torn
    # corner, a print running off the scan — and its angle is noise, which is
    # how photo #306 came out tilted when it had been straight.
    edges_ok = (top.edges is not None
                and top.edges.confidence >= EDGE_MIN_CONFIDENCE)
    deskew_wanted = (
        not result.needs_manual
        and edges_ok
        and abs(rect_full.angle) >= settings.CLEANUP_DESKEW_MIN_DEG
    )
    if deskew_wanted:
        measured["ops"]["deskew"] = {
            "angle_deg": round(rect_full.angle, 3),
            "edge_confidence": round(top.edges.confidence, 4),
            "edge_length_px": round(top.edges.agreeing_length, 1),
        }
    elif (not result.needs_manual and not edges_ok
          and abs(rect_full.angle) >= settings.CLEANUP_DESKEW_MIN_DEG):
        measured["deskew_skipped"] = "edges_disagree"
        measured["deskew_skipped_detail"] = (
            top.edges.to_json() if top.edges else None)

    # Fix-up 3: a crop removes scanner bed, never pixels of the photograph.
    # Before proposing one, push every edge outward until what lies beyond it
    # is genuinely bed — the print mask keys on "darker than the bed", so a
    # pale sky, a white curtain or a white cardigan sits outside it and the
    # raw rectangle cuts into the picture (photo #15).
    crop_rect_full = rect_full
    crop_wanted = False
    if not result.needs_manual:
        guarded_small, verdicts = guard_crop_edges(rgb, top.rect, bed, settings)
        measured["crop_guard"] = [v.to_json() for v in verdicts]
        unclear = [v.side for v in verdicts if v.unclear]
        off_scan = [v.side for v in verdicts if v.runs_off_scan]
        if off_scan:
            measured["crop_runs_off_scan"] = off_scan
        moved = {v.side: round(v.moved_px * scale, 1)
                 for v in verdicts if v.moved_px > 0.5}
        if moved:
            measured["crop_guard_moved_px"] = moved

        if len(unclear) >= 2:
            # Two or more edges we could not find: this is not a print on a
            # bed, it is a scan George needs to look at.
            result.needs_manual = True
            result.manual_reason = "print_edge_unclear"
            measured["crop_unclear_sides"] = unclear
        else:
            if unclear:
                measured["crop_unclear_sides"] = unclear
            crop_rect_full = guarded_small.scaled(scale)
            # Prefer under-cropping: give the found edges a margin back.
            safety = inset_px_for_dpi(
                dpi, settings.CLEANUP_CROP_SAFETY_PX_AT_300)
            measured["crop_safety_px"] = round(safety, 2)
            candidate = transform_for(
                crop_rect_full, src_w=src_w, src_h=src_h,
                deskew=deskew_wanted, crop=True,
                inset_px=max(0.0, inset - safety),
            )
            src_area = float(src_w * src_h)
            removed = 1.0 - (candidate.out_w * candidate.out_h) / src_area
            if removed >= settings.CLEANUP_CROP_MIN_FRAC:
                crop_wanted = True
                measured["ops"]["crop"] = {
                    "rect": [round(v, 2) for v in (candidate.crop or (0, 0, 0, 0))],
                    "out_w": candidate.out_w, "out_h": candidate.out_h,
                    "inset_px": round(max(0.0, inset - safety), 2),
                    "safety_px": round(safety, 2),
                    "removed_frac": round(removed, 4),
                    "guard_moved_px": moved or None,
                    "unclear_sides": unclear or None,
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
        highlight = tone["highlight"]

        # Rule 1: the paper white has to tell the same story as the mid-tones.
        # An age cast stains the whole print; a lawn, a warm lamp or a beige
        # wall shifts the mid-tones and leaves the paper alone.
        agree, why, ratio = ops.casts_agree(
            cast, highlight,
            min_ratio=settings.CLEANUP_CAST_HIGHLIGHT_AGREE,
        )
        measured["cast_agreement"] = {
            "agree": bool(agree), "why": why, "ratio": round(ratio, 4),
            "mid": {"a": cast["a"], "b": cast["b"],
                    "magnitude": cast["magnitude"]},
            "highlight": {"a": highlight["a"], "b": highlight["b"],
                          "magnitude": highlight["magnitude"],
                          "n": highlight["n"], "rgb": highlight["rgb"]},
        }
        if not agree:
            measured["colour_skipped"] = "scene_colour"
            measured["colour_skipped_why"] = why
        else:
            # Rule 1, second half: correct by the *smaller* of the two
            # readings, so the more cautious measurement wins.
            use_highlight = highlight["magnitude"] < cast["magnitude"]
            if use_highlight:
                gains = ops.cast_gains(
                    highlight, rgb,
                    ops.highlight_pixels(
                        rgb, interior,
                        pct=settings.CLEANUP_CAST_HIGHLIGHT_PCT),
                    neutral_pct=100.0)
            else:
                gains = ops.cast_gains(
                    cast, rgb, interior,
                    neutral_pct=settings.CLEANUP_CAST_NEUTRAL_PCT)
            raw_gains = dict(gains)

            # Rule 2: under-correct on purpose.
            gains = ops.blend_gains(gains, settings.CLEANUP_CAST_STRENGTH)
            blended_gains = dict(gains)

            # Rule 3: never push the paper white further from neutral.
            gains, guard = ops.guard_white_point(gains, highlight["rgb"])

            if any(abs(gains[k] - 1.0) > 0.005 for k in ("r", "g", "b")):
                # Fix-up 2: measured either way, proposed only when enabled.
                target = (measured["ops"] if settings.CLEANUP_COLOUR_ENABLED
                          else measured.setdefault("ops_disabled", {}))
                target["colour"] = {
                    "cast_a": cast["a"], "cast_b": cast["b"],
                    "magnitude": cast["magnitude"],
                    "grey_world_magnitude": cast.get("grey_world_magnitude"),
                    "highlight_magnitude": highlight["magnitude"],
                    "highlight_ratio": round(ratio, 4),
                    "measured_on": "highlight" if use_highlight else "mid-tone",
                    "neutral_pct": settings.CLEANUP_CAST_NEUTRAL_PCT,
                    "sample_px": cast.get("n"),
                    "gains_raw": raw_gains,
                    "strength": settings.CLEANUP_CAST_STRENGTH,
                    "gains_blended": blended_gains,
                    "white_point_guard": guard,
                    "gains": gains,
                    "rgb_shift": ops.rgb_shift_from_gains(gains),
                }
            else:
                measured["colour_skipped"] = "correction_too_small"
    else:
        measured["colour_skipped"] = "below_threshold"

    if levels["contrast"] < settings.CLEANUP_CONTRAST_LOW:
        interior = _interior_mask(top.rect, rgb.shape[:2])
        after = ops.measure_contrast_after(
            rgb, lo=levels["lo"], hi=levels["hi"],
            s_curve=settings.CLEANUP_SCURVE, mask=interior,
        )
        # Fix-up 2: measured either way, proposed only when enabled.
        target = (measured["ops"] if settings.CLEANUP_LEVELS_ENABLED
                  else measured.setdefault("ops_disabled", {}))
        target["levels"] = {
            "lo": levels["lo"], "hi": levels["hi"],
            "s_curve": settings.CLEANUP_SCURVE,
            "contrast_before": levels["contrast"],
            "contrast_after": round(after, 4),
        }

    # --- verdict ----------------------------------------------------------
    # `render.plan_from` rebuilds the transform from `print_rect`, so the
    # guarded rectangle is the one that has to be stored.
    measured["print_rect"] = crop_rect_full.to_json()
    measured["print_rect_raw"] = rect_full.to_json()
    result.operations = measured
    if measured["ops"]:
        safety = inset_px_for_dpi(dpi, settings.CLEANUP_CROP_SAFETY_PX_AT_300)
        result.transform = transform_for(
            crop_rect_full, src_w=src_w, src_h=src_h,
            deskew=deskew_wanted, crop=crop_wanted,
            inset_px=max(0.0, inset - safety),
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
    if (operations or {}).get("deskew_skipped") == "edges_disagree":
        bits.append("deskew skipped (print edges disagree)")
    unclear = (operations or {}).get("crop_unclear_sides")
    if unclear:
        bits.append("crop skipped on " + ", ".join(unclear)
                    + ": print edge unclear")
    skipped = (operations or {}).get("colour_skipped")
    if skipped in ("mono", "sepia"):
        bits.append(f"colour skipped ({skipped})")
    elif skipped == "scene_colour":
        why = (operations or {}).get("colour_skipped_why")
        bits.append("scene colour, not a cast"
                    + (f" ({why})" if why else ""))
    return ", ".join(bits) if bits else "nothing to change"
