"""Diagnose why a face bounding box lands in the wrong place on a photo.

Prints, in one report:
  - EXIF orientation tag of the working copy
  - raw pixel dims (as stored on disk) and post-`exif_transpose` dims
  - `photos.width/height` (should be the display dims — fix-up 6)
  - `photos.orientation`
  - the endpoint edge and JPEG the client would send (transposed + downscaled)
  - every stored face on the photo with its bbox + short-edge + person label

Usage:
    python -m photoarchive.tools.diagnose_face_box PHOTO_ID
    python -m photoarchive.tools.diagnose_face_box PHOTO_ID --json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from PIL import Image, ImageOps

from .. import db as dbmod
from ..config import load as load_config
from ..inference_client import ENDPOINT_DETECT_FACES, ENDPOINT_MAX_EDGE
from ..logging_setup import configure_logging
from ..modes.ingest.image_io import open_image, probe_image

log = logging.getLogger(__name__)


def gather(photo_id: int) -> dict:
    with dbmod.connection() as conn:
        conn.autocommit = True
        photo = conn.execute(
            """
            select working_path, width, height, orientation,
                   source_root, source_folder, source_filename
            from photos where id = %s
            """,
            (photo_id,),
        ).fetchone()
        if photo is None:
            return {"photo_id": photo_id, "error": "not found"}
        master = conn.execute(
            """
            select master_path, width, height
            from photo_masters
            where photo_id = %s and is_preferred
            """,
            (photo_id,),
        ).fetchone()
        faces = conn.execute(
            """
            select f.id, f.bbox, f.person_id, pe.display_name, f.confidence,
                   f.is_disputed, f.is_deleted, f.embedding_model
            from faces f
            left join people pe on pe.id = f.person_id
            where f.photo_id = %s
            order by f.id
            """,
            (photo_id,),
        ).fetchall()

    working_path, db_width, db_height, db_orientation, src_root, src_folder, src_name = photo
    out: dict = {
        "photo_id": photo_id,
        "working_path": working_path,
        "db_dims": {"width": db_width, "height": db_height, "orientation": db_orientation},
        "source": {"root": src_root, "folder": src_folder, "filename": src_name},
        "master": (
            {"path": master[0], "width": master[1], "height": master[2]}
            if master else None
        ),
        "faces": [],
        "file_probe": None,
        "would_send": None,
    }

    if working_path and Path(working_path).exists():
        try:
            with open_image(Path(working_path)) as img:
                img.load()
                dims = probe_image(img)
                out["file_probe"] = {
                    "raw_dims": [dims.master_width, dims.master_height],
                    "display_dims": [dims.display_width, dims.display_height],
                    "orientation": dims.orientation,
                }
                transposed = ImageOps.exif_transpose(img)
                max_edge = ENDPOINT_MAX_EDGE[ENDPOINT_DETECT_FACES]
                w, h = transposed.size
                edge = max(w, h)
                if edge > max_edge:
                    scale = max_edge / edge
                    send_w, send_h = int(round(w * scale)), int(round(h * scale))
                else:
                    send_w, send_h = w, h
                out["would_send"] = {
                    "endpoint": ENDPOINT_DETECT_FACES,
                    "max_edge": max_edge,
                    "sent_dims": [send_w, send_h],
                }
        except Exception as e:
            out["file_probe"] = {"error": str(e)}
    else:
        out["file_probe"] = {"error": "working file missing"}

    for fid, bbox, person_id, display_name, confidence, is_disputed, is_deleted, model in faces:
        b = bbox if isinstance(bbox, dict) else json.loads(bbox)
        short_edge = None
        try:
            short_edge = float(min(float(b["w"]), float(b["h"])))
        except (KeyError, TypeError, ValueError):
            pass
        out["faces"].append({
            "face_id": int(fid),
            "bbox": b,
            "short_edge_px": short_edge,
            "person_id": person_id,
            "person_name": display_name,
            "confidence": confidence,
            "is_disputed": bool(is_disputed),
            "is_deleted": bool(is_deleted),
            "embedding_model": model,
        })

    # Verdict: does the DB agree with the file?
    verdict = []
    if out["file_probe"] and "display_dims" in out["file_probe"]:
        f_disp = tuple(out["file_probe"]["display_dims"])
        db_disp = (db_width, db_height)
        if f_disp != db_disp:
            verdict.append(
                f"MISMATCH: photos.width/height {db_disp} != display dims from file {f_disp}. "
                "Existing bboxes for this photo were likely scaled from raw dims and are wrong."
            )
        if db_orientation is None and out["file_probe"].get("orientation"):
            verdict.append(
                f"photos.orientation is NULL but file has EXIF orientation "
                f"{out['file_probe']['orientation']} — needs backfill."
            )
    if not verdict:
        verdict.append("DB dims agree with file display dims — no repair needed.")
    out["verdict"] = verdict
    return out


def print_report(report: dict) -> None:
    print(f"photo_id: {report['photo_id']}")
    if "error" in report:
        print(f"  ERROR: {report['error']}")
        return
    print(f"  working_path: {report['working_path']}")
    print(f"  source: {report['source']}")
    print(f"  db dims: {report['db_dims']}")
    print(f"  master: {report['master']}")
    print(f"  file_probe: {report['file_probe']}")
    print(f"  would_send: {report['would_send']}")
    print(f"  faces ({len(report['faces'])}):")
    for f in report["faces"]:
        print(f"    - {f}")
    print("  verdict:")
    for line in report["verdict"]:
        print(f"    · {line}")


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="photoarchive.tools.diagnose_face_box")
    parser.add_argument("photo_id", type=int)
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    args = parser.parse_args(argv)

    settings = load_config()
    dbmod.init_pool(settings)
    try:
        report = gather(args.photo_id)
    finally:
        dbmod.close_pool()

    if args.json:
        print(json.dumps(report, default=str, indent=2))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
