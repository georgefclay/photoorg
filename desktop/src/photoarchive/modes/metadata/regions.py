"""Face regions: from the archive's display frame to the file's stored pixels.

`faces.bbox` is in the display frame — the working copy after
`ImageOps.exif_transpose` (Phase 6 fix-up 6). XMP regions (MWG-RS and
Microsoft's `MP:RegionInfo`) are relative to the pixels as *stored*, with
`AppliedToDimensions` naming those stored dimensions. So every box goes
through the inverse of the file's EXIF orientation before it is written.
Skipping this step puts the boxes on the wrong part of the picture.

Pure: no I/O. The dimensions and orientation come from the file being
written, never from the DB (a cleaned scan's working file is not the
master's size).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

# Display-normalised (u, v) → stored-normalised (x, y), per EXIF orientation.
# Derived from what `exif_transpose` does to a stored pixel, e.g. for 6
# (rotate 90° CW to display) stored (x, y) lands at display (H - y, x).
_INVERSE = {
    1: lambda u, v: (u, v),
    2: lambda u, v: (1 - u, v),
    3: lambda u, v: (1 - u, 1 - v),
    4: lambda u, v: (u, 1 - v),
    5: lambda u, v: (v, u),
    6: lambda u, v: (v, 1 - u),
    7: lambda u, v: (1 - v, 1 - u),
    8: lambda u, v: (1 - v, u),
}

# Display dims may differ from what the DB recorded by a pixel of rounding;
# anything more means the boxes were measured on a different file.
DIM_TOLERANCE_PX = 1


@dataclass(frozen=True)
class StoredRegion:
    name: str
    x: float  # top-left, normalised to stored pixels
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


def display_dims(stored_w: int, stored_h: int, orientation: int | None) -> tuple[int, int]:
    if (orientation or 1) in (5, 6, 7, 8):
        return stored_h, stored_w
    return stored_w, stored_h


def dims_match(db_w: int | None, db_h: int | None, stored_w: int, stored_h: int,
               orientation: int | None) -> bool:
    if not db_w or not db_h:
        return False
    dw, dh = display_dims(stored_w, stored_h, orientation)
    return (abs(dw - int(db_w)) <= DIM_TOLERANCE_PX
            and abs(dh - int(db_h)) <= DIM_TOLERANCE_PX)


def _clamp(v: float) -> float:
    return min(1.0, max(0.0, v))


def to_stored(regions: Sequence[dict[str, Any]], orientation: int | None) -> list[StoredRegion]:
    """Map payload regions (display-normalised top-left boxes) to stored."""
    inv = _INVERSE.get(int(orientation or 1), _INVERSE[1])
    out: list[StoredRegion] = []
    for r in regions:
        u0, v0 = float(r["x"]), float(r["y"])
        u1, v1 = u0 + float(r["w"]), v0 + float(r["h"])
        xa, ya = inv(u0, v0)
        xb, yb = inv(u1, v1)
        x0, x1 = sorted((_clamp(xa), _clamp(xb)))
        y0, y1 = sorted((_clamp(ya), _clamp(yb)))
        if x1 <= x0 or y1 <= y0:
            continue
        out.append(StoredRegion(name=str(r["name"]), x=round(x0, 6), y=round(y0, 6),
                                w=round(x1 - x0, 6), h=round(y1 - y0, 6)))
    return out


def mwg_struct(regions: Sequence[StoredRegion], stored_w: int, stored_h: int) -> dict[str, Any]:
    """`XMP-mwg-rs:RegionInfo` — MWG areas are centre-based."""
    return {
        "AppliedToDimensions": {"W": int(stored_w), "H": int(stored_h), "Unit": "pixel"},
        "RegionList": [
            {"Area": {"X": round(r.cx, 6), "Y": round(r.cy, 6),
                      "W": r.w, "H": r.h, "Unit": "normalized"},
             "Name": r.name, "Type": "Face"}
            for r in regions
        ],
    }


def mp_struct(regions: Sequence[StoredRegion]) -> dict[str, Any]:
    """`XMP-MP:RegionInfoMP` — Windows' people tags; top-left rectangles."""
    return {
        "Regions": [
            {"PersonDisplayName": r.name,
             "Rectangle": f"{r.x:.6f}, {r.y:.6f}, {r.w:.6f}, {r.h:.6f}"}
            for r in regions
        ],
    }
