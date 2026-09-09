"""Phase 6 fix-up 6: face boxes on portrait phone photos landed on the
wrong pixel. The mini sees the exif-transposed image, so the bbox it
returns is in DISPLAY coordinates; the writer must scale back using
DISPLAY dims (post-transpose), and the DB must carry those display
dims + the orientation code.

This test exercises the whole chain with a synthetic EXIF-orientation-6
JPEG: raw file is 40 wide × 20 tall, displayed should be 20 wide × 40
tall. A red pixel sits at raw (5, 15) — after transpose it displays at
(4, 5) — the writer must store the DB row's face bbox in the display
coordinate frame so `image = ImageOps.exif_transpose(open(working));
image.crop(bbox)` lands on red pixels.
"""

from __future__ import annotations

import io
from pathlib import Path

import piexif
import pytest
from PIL import Image, ImageOps

from photoarchive import db as dbmod
from photoarchive.inference_client import ResultLine
from photoarchive.jobs.base import JobContext
from photoarchive.jobs.detect_faces import FacesWriter
from photoarchive.modes.ingest.image_io import probe_image
from photoarchive.tools import repair_face_boxes

from .phase6_fixtures import insert_photo, phase6  # noqa: F401


ORIENTATION_ROTATE_90_CW = 6


def _write_portrait_jpeg_with_exif_orientation(
    path: Path,
    raw_w: int = 40,
    raw_h: int = 20,
    orientation: int = ORIENTATION_ROTATE_90_CW,
) -> None:
    """Raw file is landscape; EXIF says rotate 90° for display so it
    should render as portrait. We paint an easily-identified red patch
    on the raw canvas so we can look for it after transpose."""
    im = Image.new("RGB", (raw_w, raw_h), (30, 30, 30))
    # Put a red 4x4 block at raw (5, 15..19) — bottom-left in landscape.
    for y in range(15, min(raw_h, 20)):
        for x in range(5, min(raw_w, 9)):
            im.putpixel((x, y), (255, 0, 0))
    exif_dict = {"0th": {piexif.ImageIFD.Orientation: orientation}}
    exif_bytes = piexif.dump(exif_dict)
    im.save(path, format="JPEG", exif=exif_bytes, quality=95)


def _ctx() -> JobContext:
    return JobContext(client=None, model="test-vlm", prompt_version="v1")


def _line(ref: str, result: dict) -> ResultLine:
    return ResultLine(
        line_no=1, ref=ref, ok=True, model="test-vlm",
        prompt_version="v1", elapsed_ms=1, result=result, error=None,
        raw={"ref": ref, "result": result},
    )


def test_probe_image_swaps_dims_when_exif_orientation_is_rotated(tmp_path):
    """The building block: probe_image reads EXIF orientation 6 and
    reports display dims as (raw_h, raw_w)."""
    p = tmp_path / "portrait.jpg"
    _write_portrait_jpeg_with_exif_orientation(p, raw_w=40, raw_h=20)
    with Image.open(p) as im:
        im.load()
        dims = probe_image(im)
    assert dims.master_width == 40
    assert dims.master_height == 20
    assert dims.orientation == 6
    assert dims.display_width == 20
    assert dims.display_height == 40


def _bbox_of_red_pixels(im: Image.Image) -> tuple[int, int, int, int]:
    """Return the (x, y, w, h) enclosing rectangle of red-ish pixels in
    a small RGB image, in that image's own coordinate frame."""
    px = im.load()
    xs, ys = [], []
    for y in range(im.height):
        for x in range(im.width):
            r, g, b = px[x, y][:3]
            if r > 200 and g < 40:
                xs.append(x)
                ys.append(y)
    assert xs, "no red pixels in the image"
    return min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1


def test_face_writer_stores_bbox_in_display_frame_for_portrait_phone_photo(phase6):
    """Regression for fix-up 6. With `photos.width/height` stored as
    DISPLAY dims and the mini's `image_w/h` reported in the same frame,
    the writer's scale-back is a no-op — bbox lands where the red patch
    displays after transposing the working copy."""
    wp = phase6.WORKING_DIR / "portrait.jpg"
    _write_portrait_jpeg_with_exif_orientation(wp)

    # Establish ground truth: where does the red patch land after the
    # same exif_transpose the client / preview apply? We derive the
    # "mini's report" coords from what the mini would actually see.
    with Image.open(wp) as _im:
        transposed = ImageOps.exif_transpose(_im).convert("RGB")
    red_x, red_y, red_w, red_h = _bbox_of_red_pixels(transposed)
    disp_w, disp_h = transposed.size
    assert (disp_w, disp_h) == (20, 40)  # orientation 6 swapped dims

    with dbmod.connection() as conn:
        conn.autocommit = False
        # Ingest-shaped insert: photos.width/height are display dims
        # (post-transpose) since fix-up 6.
        pid = insert_photo(
            conn,
            width=disp_w, height=disp_h,
            working_path=str(wp),
        )
        conn.execute(
            "update photos set orientation = 6 where id = %s", (pid,),
        )
        conn.commit()

    # Fake mini response: it saw the transposed image (20 × 40) and
    # reported the red block at the display coords it observed.
    line = _line(str(pid), {
        "image_w": disp_w, "image_h": disp_h,
        "faces": [{
            "bbox": {"x": float(red_x), "y": float(red_y),
                      "w": float(red_w), "h": float(red_h)},
            "det_score": 0.99,
            "embedding": [0.1] * 512,
            "landmarks": [[1.0, 2.0]] * 5,
        }],
    })
    with dbmod.connection() as conn:
        conn.autocommit = False
        FacesWriter().apply(conn, line, _ctx())
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = True
        row = conn.execute(
            "select bbox from faces where photo_id = %s", (pid,)
        ).fetchone()
    import json as _json
    bbox = row[0] if isinstance(row[0], dict) else _json.loads(row[0])
    # No scale (image dims == display dims). Stored bbox matches the
    # display-frame report.
    assert abs(bbox["x"] - red_x) < 0.01
    assert abs(bbox["y"] - red_y) < 0.01
    assert abs(bbox["w"] - red_w) < 0.01
    assert abs(bbox["h"] - red_h) < 0.01

    # Cross-check against the actual pixels: crop the transposed working
    # copy at the stored bbox and assert every pixel in the crop is red.
    crop = transposed.crop((
        int(bbox["x"]), int(bbox["y"]),
        int(bbox["x"] + bbox["w"]), int(bbox["y"] + bbox["h"]),
    ))
    red_pixels = 0
    total = 0
    for pixel in crop.getdata():
        total += 1
        if pixel[0] > 200 and pixel[1] < 40:
            red_pixels += 1
    assert red_pixels == total, (
        f"bbox does not land squarely on red patch: "
        f"{red_pixels}/{total} red pixels"
    )


def test_repair_tool_swaps_dims_and_rescales_bboxes_for_pre_fixup_rows(phase6):
    """Simulate a pre-fix-up-6 row: photos.width/height stored as RAW
    file dims + face bbox scaled from raw. Run the repair tool and
    assert both are corrected."""
    wp = phase6.WORKING_DIR / "portrait-old.jpg"
    _write_portrait_jpeg_with_exif_orientation(wp, raw_w=40, raw_h=20)

    with dbmod.connection() as conn:
        conn.autocommit = False
        # Old row: photos.width/height are the RAW dims, orientation NULL.
        pid = insert_photo(
            conn,
            width=40, height=20,   # RAW dims (pre-fix-up 6 semantics)
            working_path=str(wp),
        )
        # Old-writer bbox: pretend the mini sent image_w=20, image_h=40
        # (transposed at edge 40) and reported face at display (4, 5, 4, 4).
        # Old writer scaled by (40/20, 20/40) = (2, 0.5) → stored (8, 2.5, 8, 2).
        conn.execute(
            """
            insert into faces
              (photo_id, person_id, bbox, embedding, embedding_model,
               confidence, source, is_disputed, is_deleted)
            values (%s, NULL, %s::jsonb, %s, 'test', 0.9, 'ai', false, false)
            """,
            (pid,
             '{"x": 8.0, "y": 2.5, "w": 8.0, "h": 2.0}',
             [0.1] * 512),
        )
        conn.commit()

    counts = repair_face_boxes.repair(dry_run=False, limit=None)
    assert counts.dims_swapped >= 1
    assert counts.orientation_backfilled >= 1
    assert counts.faces_repaired >= 1

    with dbmod.connection() as conn:
        conn.autocommit = True
        row = conn.execute(
            "select width, height, orientation from photos where id = %s",
            (pid,),
        ).fetchone()
        assert row == (20, 40, 6)   # swapped + orientation filled
        face_row = conn.execute(
            "select bbox from faces where photo_id = %s", (pid,)
        ).fetchone()
    import json as _json
    bbox = face_row[0] if isinstance(face_row[0], dict) else _json.loads(face_row[0])
    # Repair math: x/w × (raw_h / raw_w) = ×0.5; y/h × (raw_w / raw_h) = ×2.
    # 8 * 0.5 = 4 (x), 2.5 * 2 = 5 (y), 8 * 0.5 = 4 (w), 2.0 * 2 = 4 (h).
    assert abs(bbox["x"] - 4.0) < 0.01
    assert abs(bbox["y"] - 5.0) < 0.01
    assert abs(bbox["w"] - 4.0) < 0.01
    assert abs(bbox["h"] - 4.0) < 0.01
