"""The one affine transform every geometric cleanup op is expressed as.

Invariant 2 of Phase 7: face boxes travel with the pixels. Deskew, crop and
split-region offset compose into a single 2x3 affine `M` mapping a point in
the source's *display* frame (EXIF-transposed, full resolution — the frame
`faces.bbox` lives in, see CLAUDE.md "Face coordinate frame") to a point in
the rendered output's frame. `Transform.apply_bbox` maps a face box through
it; `Transform.invert()` gives the transform back.

Why boxes keep their width and height
-------------------------------------
A rotated axis-aligned box is no longer axis-aligned. The two usual choices
are (a) take the axis-aligned bounding box of the rotated corners, which
*grows* the box by w·|sin θ| and is not invertible, or (b) map the centre
exactly and keep w/h. We take (b):

  * it is exactly invertible, so `invert()` round-trips to the pixel;
  * the face itself rotates with the image, so its true box in the new frame
    is the same size, only re-centred — (a) would make the box too big;
  * cleanup rotations are small by construction (`CLEANUP_MAX_DESKEW_DEG`,
    default 15°; anything larger is `needs_manual`), so the O(w·sin θ)
    error — 3.5 % of the box at 2° — is well inside the 15 % padding the
    face crops already carry.

Undo does not rely on this: it restores each box verbatim from the
`cleanup.accept` audit row. `invert()` is the fallback and the thing the
round-trip test exercises.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

# A box is considered lost when less than this fraction of its area survives
# inside the new frame. Invariant 2: it is soft-deleted, never dropped.
MIN_BOX_OVERLAP = 0.5


@dataclass(frozen=True)
class Rect:
    """A rotated rectangle in some pixel frame — OpenCV's minAreaRect, but
    with the angle normalised into (-45, 45] so `w`/`h` keep their meaning.
    """
    cx: float
    cy: float
    w: float
    h: float
    angle: float  # degrees, positive = the rect is rotated clockwise

    def to_json(self) -> dict[str, float]:
        return {"cx": self.cx, "cy": self.cy, "w": self.w,
                "h": self.h, "angle": self.angle}

    @staticmethod
    def from_json(d: dict[str, Any]) -> "Rect":
        return Rect(cx=float(d["cx"]), cy=float(d["cy"]), w=float(d["w"]),
                    h=float(d["h"]), angle=float(d["angle"]))

    @property
    def area(self) -> float:
        return abs(self.w) * abs(self.h)

    @property
    def aspect(self) -> float:
        lo, hi = sorted((abs(self.w), abs(self.h)))
        return (hi / lo) if lo > 0 else float("inf")

    def scaled(self, factor: float) -> "Rect":
        """Same rectangle in a frame `factor` times larger (analysis →
        full resolution)."""
        return Rect(cx=self.cx * factor, cy=self.cy * factor,
                    w=self.w * factor, h=self.h * factor, angle=self.angle)

    def corners(self) -> list[tuple[float, float]]:
        t = math.radians(self.angle)
        cos_t, sin_t = math.cos(t), math.sin(t)
        hw, hh = self.w / 2.0, self.h / 2.0
        out = []
        for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
            out.append((self.cx + dx * cos_t - dy * sin_t,
                        self.cy + dx * sin_t + dy * cos_t))
        return out

    def axis_aligned_bounds(self) -> tuple[float, float, float, float]:
        """(x, y, w, h) of the axis-aligned box that contains this rect."""
        xs = [p[0] for p in self.corners()]
        ys = [p[1] for p in self.corners()]
        return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


def normalise_angle(angle: float, w: float, h: float) -> tuple[float, float, float]:
    """Bring an OpenCV minAreaRect (angle in [0, 90)) into (-45, 45],
    swapping w/h when the rect is closer to its other diagonal.

    Returns (angle, w, h).
    """
    a = float(angle)
    while a <= -45.0:
        a += 90.0
        w, h = h, w
    while a > 45.0:
        a -= 90.0
        w, h = h, w
    return a, w, h


@dataclass(frozen=True)
class Transform:
    """A 2x3 affine plus the output frame it maps into.

    `m` is (a, b, tx, c, d, ty): x' = a·x + b·y + tx, y' = c·x + d·y + ty.
    The crop offset is already folded in, so `apply_point` lands directly in
    the output frame [0, out_w) x [0, out_h).
    """
    m: tuple[float, float, float, float, float, float]
    src_w: int
    src_h: int
    out_w: int
    out_h: int
    angle_deg: float = 0.0
    # Bookkeeping for the caption / report; not used by the maths.
    crop: tuple[float, float, float, float] | None = None
    notes: dict[str, Any] = field(default_factory=dict)

    # -- construction ---------------------------------------------------

    @staticmethod
    def identity(src_w: int, src_h: int) -> "Transform":
        return Transform(m=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0),
                         src_w=src_w, src_h=src_h, out_w=src_w, out_h=src_h)

    @staticmethod
    def build(
        *,
        src_w: int,
        src_h: int,
        angle_deg: float = 0.0,
        crop: Sequence[float] | None = None,
    ) -> "Transform":
        """Rotate about the source centre by `-angle_deg` (deskewing a rect
        that sits at `+angle_deg`), expanding the canvas so nothing is lost,
        then crop to `crop` = (x, y, w, h) in the rotated canvas.

        With `angle_deg == 0` this is a pure crop; with `crop is None` the
        full rotated canvas is kept.
        """
        theta = math.radians(-float(angle_deg))
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        cx, cy = src_w / 2.0, src_h / 2.0

        # Canvas that holds the rotated source (cv2.warpAffine convention).
        rot_w = int(math.ceil(abs(src_w * cos_t) + abs(src_h * sin_t)))
        rot_h = int(math.ceil(abs(src_w * sin_t) + abs(src_h * cos_t)))
        # Rotation about (cx, cy) followed by the centring translation.
        tx = rot_w / 2.0 - (cx * cos_t - cy * sin_t)
        ty = rot_h / 2.0 - (cx * sin_t + cy * cos_t)

        if crop is None:
            cx0, cy0, cw, ch = 0.0, 0.0, float(rot_w), float(rot_h)
        else:
            cx0, cy0, cw, ch = (float(v) for v in crop)

        return Transform(
            m=(cos_t, -sin_t, tx - cx0, sin_t, cos_t, ty - cy0),
            src_w=int(src_w), src_h=int(src_h),
            out_w=max(1, int(round(cw))), out_h=max(1, int(round(ch))),
            angle_deg=float(angle_deg),
            crop=(cx0, cy0, cw, ch),
            notes={"rot_w": rot_w, "rot_h": rot_h},
        )

    # -- serialisation --------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        return {
            "m": list(self.m),
            "src_w": self.src_w, "src_h": self.src_h,
            "out_w": self.out_w, "out_h": self.out_h,
            "angle_deg": self.angle_deg,
            "crop": list(self.crop) if self.crop else None,
            "notes": self.notes,
        }

    @staticmethod
    def from_json(d: dict[str, Any]) -> "Transform":
        m = tuple(float(v) for v in d["m"])  # type: ignore[assignment]
        crop = d.get("crop")
        return Transform(
            m=m,  # type: ignore[arg-type]
            src_w=int(d["src_w"]), src_h=int(d["src_h"]),
            out_w=int(d["out_w"]), out_h=int(d["out_h"]),
            angle_deg=float(d.get("angle_deg") or 0.0),
            crop=tuple(float(v) for v in crop) if crop else None,  # type: ignore[arg-type]
            notes=dict(d.get("notes") or {}),
        )

    # -- maths ----------------------------------------------------------

    @property
    def is_identity(self) -> bool:
        a, b, tx, c, d, ty = self.m
        return (
            abs(a - 1.0) < 1e-12 and abs(b) < 1e-12 and abs(tx) < 1e-9
            and abs(c) < 1e-12 and abs(d - 1.0) < 1e-12 and abs(ty) < 1e-9
            and self.out_w == self.src_w and self.out_h == self.src_h
        )

    def apply_point(self, x: float, y: float) -> tuple[float, float]:
        a, b, tx, c, d, ty = self.m
        return a * x + b * y + tx, c * x + d * y + ty

    def scaled_by(self, factor: float) -> "Transform":
        """The same transform in a coordinate system `factor` times smaller.

        Lets a preview warp a downscaled source instead of the full-resolution
        one: the linear part is scale-invariant, only the translation and the
        output size move. Used by `render.render_preview`, which must not put a
        93 MP array through a full-resolution warp just to show a thumbnail.
        """
        a, b, tx, c, d, ty = self.m
        return Transform(
            m=(a, b, tx * factor, c, d, ty * factor),
            src_w=max(1, int(round(self.src_w * factor))),
            src_h=max(1, int(round(self.src_h * factor))),
            out_w=max(1, int(round(self.out_w * factor))),
            out_h=max(1, int(round(self.out_h * factor))),
            angle_deg=self.angle_deg,
            crop=None,
            notes={"scaled_from": {"out_w": self.out_w, "out_h": self.out_h},
                   "factor": factor},
        )

    def invert(self) -> "Transform":
        """The transform that maps the output frame back to the source."""
        a, b, tx, c, d, ty = self.m
        det = a * d - b * c
        if abs(det) < 1e-12:
            raise ValueError("transform is not invertible")
        ia, ib = d / det, -b / det
        ic, id_ = -c / det, a / det
        itx = -(ia * tx + ib * ty)
        ity = -(ic * tx + id_ * ty)
        return Transform(
            m=(ia, ib, itx, ic, id_, ity),
            src_w=self.out_w, src_h=self.out_h,
            out_w=self.src_w, out_h=self.src_h,
            angle_deg=-self.angle_deg,
            crop=None,
            notes={"inverse_of": self.notes or {}},
        )

    def apply_bbox(self, bbox: dict[str, Any]) -> dict[str, Any] | None:
        """Map a face box. Returns the new box, or None when the box no
        longer belongs in the frame (caller soft-deletes it).

        The centre is mapped exactly; w/h are preserved (see the module
        docstring). The result is clamped to the output frame, and rejected
        when less than `MIN_BOX_OVERLAP` of its area survives.
        """
        x = float(bbox["x"])
        y = float(bbox["y"])
        w = float(bbox["w"])
        h = float(bbox["h"])
        if w <= 0 or h <= 0:
            return None

        ncx, ncy = self.apply_point(x + w / 2.0, y + h / 2.0)
        nx, ny = ncx - w / 2.0, ncy - h / 2.0

        # Overlap with the output frame, before clamping.
        ix0, iy0 = max(0.0, nx), max(0.0, ny)
        ix1 = min(float(self.out_w), nx + w)
        iy1 = min(float(self.out_h), ny + h)
        if ix1 <= ix0 or iy1 <= iy0:
            return None
        if ((ix1 - ix0) * (iy1 - iy0)) / (w * h) < MIN_BOX_OVERLAP:
            return None

        out = {"x": ix0, "y": iy0, "w": ix1 - ix0, "h": iy1 - iy0}
        # Carry any extra keys (landmarks are not stored, but be safe).
        for k, v in bbox.items():
            if k not in out:
                out[k] = v
        return out

    def bbox_fully_inside(self, bbox: dict[str, Any]) -> bool:
        x = float(bbox["x"]); y = float(bbox["y"])
        w = float(bbox["w"]); h = float(bbox["h"])
        cx, cy = self.apply_point(x + w / 2.0, y + h / 2.0)
        return (0 <= cx - w / 2.0 and 0 <= cy - h / 2.0
                and cx + w / 2.0 <= self.out_w and cy + h / 2.0 <= self.out_h)


def bbox_centre(bbox: dict[str, Any]) -> tuple[float, float]:
    return (float(bbox["x"]) + float(bbox["w"]) / 2.0,
            float(bbox["y"]) + float(bbox["h"]) / 2.0)


def bbox_overlap_frac(bbox: dict[str, Any], rect: Rect) -> float:
    """Fraction of `bbox`'s area inside `rect`'s axis-aligned bounds.

    Used to decide which split region owns a face, and to spot a face that
    straddles two prints.
    """
    rx, ry, rw, rh = rect.axis_aligned_bounds()
    x = float(bbox["x"]); y = float(bbox["y"])
    w = float(bbox["w"]); h = float(bbox["h"])
    if w <= 0 or h <= 0:
        return 0.0
    ix0, iy0 = max(x, rx), max(y, ry)
    ix1, iy1 = min(x + w, rx + rw), min(y + h, ry + rh)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    return ((ix1 - ix0) * (iy1 - iy0)) / (w * h)


def transform_for(
    rect: Rect,
    *,
    src_w: int,
    src_h: int,
    deskew: bool,
    crop: bool,
    inset_px: float = 0.0,
) -> Transform:
    """Compose the ticked geometric ops for a print at `rect` into one
    transform. This is the single place the review UI's checkboxes become
    pixels, so unticking "deskew" really does change the crop.

      deskew + crop : rotate flat, then crop to the print (the usual case)
      deskew only   : rotate flat, keep the whole expanded canvas
      crop only     : no rotation; crop to the print's axis-aligned bounds
      neither       : identity
    """
    if not deskew and not crop:
        return Transform.identity(src_w, src_h)

    if not deskew:
        x, y, w, h = rect.axis_aligned_bounds()
        x += inset_px
        y += inset_px
        w = max(1.0, w - 2.0 * inset_px)
        h = max(1.0, h - 2.0 * inset_px)
        x = max(0.0, min(x, src_w - 1.0))
        y = max(0.0, min(y, src_h - 1.0))
        w = min(w, src_w - x)
        h = min(h, src_h - y)
        return Transform.build(src_w=src_w, src_h=src_h, angle_deg=0.0,
                               crop=(x, y, w, h))

    rotation = Transform.build(src_w=src_w, src_h=src_h, angle_deg=rect.angle)
    if not crop:
        return rotation

    # After the rotation the print is axis-aligned, centred on wherever its
    # centre landed. Crop that box, inset on every side.
    ccx, ccy = rotation.apply_point(rect.cx, rect.cy)
    w = max(1.0, abs(rect.w) - 2.0 * inset_px)
    h = max(1.0, abs(rect.h) - 2.0 * inset_px)
    return Transform.build(
        src_w=src_w, src_h=src_h, angle_deg=rect.angle,
        crop=(ccx - w / 2.0, ccy - h / 2.0, w, h),
    )


def inset_px_for_dpi(dpi: int | None, at_300: float) -> float:
    """The crop inset scales with resolution: `at_300` px at 300 DPI, so
    4 px at 300 becomes 16 px at 1200. Unknown DPI keeps the base value.
    """
    if not dpi or dpi <= 0:
        return float(at_300)
    return float(at_300) * (float(dpi) / 300.0)
