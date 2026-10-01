"""Split regions George can edit by hand (fix-up 5).

The analyser is right about most multi-print scans and wrong about some, and
when it is wrong there was nothing to do but reject the whole proposal. Photo
#708 is three prints and it found two; #3817 is a ten-picture proof sheet
whose prints touch, with no bed between them for any detector to find.

So the regions became editable. Everything here is pure — no Qt, no database —
because this is where the rules live: regions may not overlap, may not leave
the scan, and must be big enough to be a print. The dialog in
`region_editor.py` is a way of calling these functions with a mouse.

A region is stored exactly as the analyser writes it, so an edited proposal and
a measured one are the same shape to every reader:

    {"index": 1, "rect": {...}, "area_frac": 0.31, "transform": {...},
     "edited_by": "human"}
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from ...config import Settings
from .geometry import Rect, inset_px_for_dpi, transform_for

#: A region smaller than this fraction of the scan is a slip of the mouse.
MIN_REGION_FRAC = 0.005
#: Two regions may share this fraction of the smaller one before it counts as
#: an overlap — adjacent prints on a proof sheet touch, and a pixel of shared
#: edge is not an error worth refusing.
OVERLAP_TOLERANCE = 0.02


@dataclass(frozen=True)
class Box:
    """An axis-aligned region in full-resolution display coordinates.

    The editor works in axis-aligned boxes because that is what a mouse can
    draw. A per-region tilt is carried separately, from whatever the analyser
    measured for the print that region covers.
    """
    x: float
    y: float
    w: float
    h: float
    angle: float = 0.0

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0

    def clamped(self, src_w: int, src_h: int) -> "Box":
        x = min(max(0.0, self.x), max(0.0, float(src_w) - 1.0))
        y = min(max(0.0, self.y), max(0.0, float(src_h) - 1.0))
        return Box(x=x, y=y,
                   w=max(1.0, min(self.w, float(src_w) - x)),
                   h=max(1.0, min(self.h, float(src_h) - y)),
                   angle=self.angle)

    def to_rect(self) -> Rect:
        return Rect(cx=self.cx, cy=self.cy, w=self.w, h=self.h,
                    angle=self.angle)

    @staticmethod
    def from_rect(rect: Rect) -> "Box":
        """The axis-aligned bounds of a measured print rectangle.

        A tilted print's bounds are wider than the print; the tilt is kept so
        the accepted child is still deskewed by the angle that was measured.
        """
        x, y, w, h = rect.axis_aligned_bounds()
        return Box(x=x, y=y, w=w, h=h, angle=rect.angle)


def boxes_from_regions(regions: Sequence[dict[str, Any]]) -> list[Box]:
    return [Box.from_rect(Rect.from_json(r["rect"])) for r in regions]


def overlap_area(a: Box, b: Box) -> float:
    dx = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
    dy = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
    return dx * dy if dx > 0 and dy > 0 else 0.0


def problems(
    boxes: Sequence[Box], *, src_w: int, src_h: int,
    min_frac: float = MIN_REGION_FRAC,
) -> list[str]:
    """Everything wrong with this set of regions, in words George can act on.

    Returned rather than raised: the editor shows them and keeps the dialog
    open, so a half-finished layout is never thrown away.
    """
    out: list[str] = []
    if len(boxes) < 2:
        out.append("A split needs at least two regions.")
    scan_area = float(src_w) * float(src_h) or 1.0
    for i, b in enumerate(boxes, start=1):
        if b.w < 1.0 or b.h < 1.0:
            out.append(f"Region {i} has no area.")
            continue
        if b.area / scan_area < min_frac:
            out.append(
                f"Region {i} covers {100 * b.area / scan_area:.1f}% of the "
                f"scan — too small to be a print.")
        if (b.x < -0.5 or b.y < -0.5
                or b.x + b.w > src_w + 0.5 or b.y + b.h > src_h + 0.5):
            out.append(f"Region {i} runs outside the scan.")
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            shared = overlap_area(boxes[i], boxes[j])
            smaller = min(boxes[i].area, boxes[j].area) or 1.0
            if shared / smaller > OVERLAP_TOLERANCE:
                out.append(
                    f"Regions {i + 1} and {j + 1} overlap by "
                    f"{100 * shared / smaller:.0f}%.")
    return out


def grid_boxes(
    frame: Box, rows: int, cols: int, *, gutter_pct: float = 0.0,
) -> list[Box]:
    """Lay `rows` x `cols` equal regions over `frame`.

    The proof sheet (#3817) is ten pictures in a grid with no bed between
    them, which no detector is going to find. Typing "3 x 4" and nudging the
    result is quicker than drawing ten boxes, and quicker than George cutting
    them by hand in another program.

    `frame` is the *sheet*, not the regions already found. Laying the grid
    over the union of the measured regions is what made the boxes drift: on
    #3817 those two regions were narrower than the sheet, so every cell was
    narrow and the error accumulated across the columns until the rightmost
    box sat well left of its print. The caller passes the sheet and can drag
    it; this function only divides what it is given.

    `gutter_pct` shrinks each cell about its own centre, for a sheet with
    white space between the pictures. It leaves `gutter_pct` of a cell between
    neighbours and half that at the frame edge, which is how a printed sheet
    is usually laid out.
    """
    rows = max(1, int(rows))
    cols = max(1, int(cols))
    shrink = min(max(float(gutter_pct), 0.0), 0.9)
    cell_w = frame.w / cols
    cell_h = frame.h / rows
    out: list[Box] = []
    for r in range(rows):
        for c in range(cols):
            cx = frame.x + (c + 0.5) * cell_w
            cy = frame.y + (r + 0.5) * cell_h
            w = max(1.0, cell_w * (1.0 - shrink))
            h = max(1.0, cell_h * (1.0 - shrink))
            out.append(Box(x=cx - w / 2.0, y=cy - h / 2.0, w=w, h=h,
                           angle=frame.angle))
    return out


def same_column(boxes: Sequence[Box], index: int) -> list[int]:
    """Indices of the boxes sharing a column with `boxes[index]`.

    Membership is by centre proximity rather than by remembering the grid,
    so it still works after the boxes have been nudged about one at a time.
    """
    if not boxes or not (0 <= index < len(boxes)):
        return []
    sel = boxes[index]
    tol = max(1.0, sel.w * 0.5)
    return [i for i, b in enumerate(boxes) if abs(b.cx - sel.cx) <= tol]


def same_row(boxes: Sequence[Box], index: int) -> list[int]:
    if not boxes or not (0 <= index < len(boxes)):
        return []
    sel = boxes[index]
    tol = max(1.0, sel.h * 0.5)
    return [i for i, b in enumerate(boxes) if abs(b.cy - sel.cy) <= tol]


def moved(box: Box, dx: float, dy: float) -> Box:
    return Box(x=box.x + dx, y=box.y + dy, w=box.w, h=box.h, angle=box.angle)


def bounds_of(boxes: Iterable[Box]) -> Box | None:
    boxes = list(boxes)
    if not boxes:
        return None
    x0 = min(b.x for b in boxes)
    y0 = min(b.y for b in boxes)
    x1 = max(b.x + b.w for b in boxes)
    y1 = max(b.y + b.h for b in boxes)
    return Box(x=x0, y=y0, w=x1 - x0, h=y1 - y0,
               angle=boxes[0].angle if boxes else 0.0)


def to_regions(
    boxes: Sequence[Box], *, settings: Settings, src_w: int, src_h: int,
    dpi: int | None = None, inset_px: float | None = None,
    edited: bool = True,
) -> list[dict[str, Any]]:
    """Turn edited boxes into stored regions, transforms and all.

    The transform is built exactly as the analyser builds it, by the same
    function — an edited region and a measured one must be indistinguishable
    to `accept_split`, which renders from `region["transform"]` and assigns
    faces by containment against `region["rect"]`.
    """
    if inset_px is None:
        inset_px = inset_px_for_dpi(dpi, settings.CLEANUP_CROP_INSET_PX_AT_300)
    scan_area = float(src_w) * float(src_h) or 1.0
    out: list[dict[str, Any]] = []
    for i, raw in enumerate(boxes, start=1):
        b = raw.clamped(src_w, src_h)
        rect = b.to_rect()
        t = transform_for(
            rect, src_w=src_w, src_h=src_h,
            deskew=abs(rect.angle) >= settings.CLEANUP_DESKEW_MIN_DEG,
            crop=True, inset_px=inset_px,
        )
        region: dict[str, Any] = {
            "index": i,
            "rect": rect.to_json(),
            "area_frac": round(b.area / scan_area, 4),
            "rectangularity": 1.0,
            "transform": t.to_json(),
        }
        if edited:
            region["edited_by"] = "human"
        out.append(region)
    return out


def reading_order(boxes: Sequence[Box]) -> list[Box]:
    """Top to bottom, left to right, in bands.

    Region 1 should be the top-left print, not whichever blob happened to be
    biggest — the children are numbered `#p1`, `#p2` in this order and George
    will look for them in the order they sit on the bed.
    """
    if not boxes:
        return []
    band = max(b.h for b in boxes) * 0.5
    return sorted(boxes, key=lambda b: (round(b.cy / band) if band else 0, b.cx))
