"""Backfill `photos.orientation` and repair face bounding boxes stored
under the pre-fix-up-6 (raw-dims) semantics.

For every photo:
  - Read the working copy's EXIF orientation (or the master's if the
    working copy has been re-encoded). Set `photos.orientation` if
    NULL.
  - If orientation ∈ {5,6,7,8} and `photos.width/height` still match
    the RAW file dims, swap them to DISPLAY dims (matching what
    `ImageOps.exif_transpose(img).size` returns).
  - For every face on such a photo, recompute the bbox from the
    raw-dims scaling to the display-dims scaling: x/w × (H_raw/W_raw),
    y/h × (W_raw/H_raw). Regenerate the face crop thumbnail.
  - If the master file has EXIF orientation but the working copy does
    not (Pillow may strip it on some ingest paths), fall back to the
    master's orientation.

`--dry-run` prints what would change without touching the DB or disk.
`--limit N` caps iteration for a smoke run.

Usage:
    python -m photoarchive.tools.repair_face_boxes --dry-run
    python -m photoarchive.tools.repair_face_boxes --limit 50
    python -m photoarchive.tools.repair_face_boxes
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageOps

from .. import db as dbmod
from ..config import load as load_config
from ..jobs.detect_faces import _write_face_crops
from ..logging_setup import configure_logging
from ..modes.ingest.image_io import open_image, probe_image

log = logging.getLogger(__name__)


@dataclass
class RepairRow:
    """One photo that needs repairing, as the dry run reports it."""
    photo_id: int
    raw_w: int
    raw_h: int
    exif_tag: int | None
    source: str
    faces: int
    labelled: int

    def line(self) -> str:
        return (f"  #{self.photo_id:<6} raw {self.raw_w}x{self.raw_h:<10} "
                f"exif={self.exif_tag:<3} via {self.source:<7} "
                f"boxes={self.faces:<3} labelled={self.labelled}")


@dataclass
class RepairCounts:
    photos_scanned: int = 0
    orientation_backfilled: int = 0
    dims_swapped: int = 0
    faces_repaired: int = 0
    face_crops_regenerated: int = 0
    photos_missing_working_file: int = 0
    photos_missing_master_file: int = 0
    photos_unreadable: int = 0
    rows: list = field(default_factory=list)

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "rows"}
        d["photos_needing_repair"] = len(self.rows)
        return d


def _load_dims_via(path: Path) -> tuple[int, int, int | None] | None:
    """Return (raw_w, raw_h, orientation) or None if the file can't be read.

    Header only: `probe_image` wants the size and one EXIF tag, and decoding
    12,686 images to read them is what made a whole-archive run unaffordable
    on this laptop.
    """
    try:
        with open_image(path) as img:
            dims = probe_image(img)
            return dims.master_width, dims.master_height, dims.orientation
    except Exception as e:
        log.warning("could not read %s: %s", path, e)
        return None


def repair(*, dry_run: bool, limit: int | None) -> RepairCounts:
    write_face_crops = _write_face_crops
    settings = load_config()
    thumbs_dir = settings.THUMBS_DIR / "faces"
    if not dry_run:
        thumbs_dir.mkdir(parents=True, exist_ok=True)

    counts = RepairCounts()

    with dbmod.connection() as conn:
        conn.autocommit = True
        sql = """
            select id, working_path, width, height, orientation
            from photos
            where not is_deleted
            order by id
        """
        rows = conn.execute(sql).fetchall()

    if limit:
        rows = rows[:limit]

    for row in rows:
        counts.photos_scanned += 1
        photo_id, working_path, db_w, db_h, db_orientation = row

        # Prefer the working copy for EXIF read, fall back to the master.
        probe = None
        source = None
        if working_path and Path(working_path).exists():
            probe = _load_dims_via(Path(working_path))
            source = "working"
        if probe is None:
            counts.photos_missing_working_file += 1
            with dbmod.connection() as conn:
                conn.autocommit = True
                master = conn.execute(
                    """
                    select master_path from photo_masters
                    where photo_id = %s and is_preferred
                    """,
                    (photo_id,),
                ).fetchone()
            if master and Path(master[0]).exists():
                probe = _load_dims_via(Path(master[0]))
                source = "master"
            else:
                counts.photos_missing_master_file += 1
                continue
        if probe is None:
            counts.photos_unreadable += 1
            continue

        raw_w, raw_h, orientation = probe
        new_orientation = db_orientation
        new_w, new_h = db_w, db_h

        if db_orientation is None and orientation is not None:
            new_orientation = orientation
            counts.orientation_backfilled += 1

        # If we now know the orientation and it rotates dims, the display
        # values are (raw_h, raw_w). If the DB still holds (raw_w, raw_h)
        # then the old writer stored raw dims and every face bbox is
        # scaled wrong.
        needs_dim_swap = False
        if new_orientation in (5, 6, 7, 8):
            disp_w, disp_h = raw_h, raw_w
            if (db_w, db_h) == (raw_w, raw_h):
                new_w, new_h = disp_w, disp_h
                needs_dim_swap = True
        else:
            disp_w, disp_h = raw_w, raw_h

        # Fetch faces regardless — we may need to regenerate crops even
        # when dims already match (e.g. dims were swapped by a prior run
        # but crops weren't regenerated).
        with dbmod.connection() as conn:
            conn.autocommit = True
            face_rows = conn.execute(
                """
                select id, bbox from faces
                where photo_id = %s and not is_deleted
                """,
                (photo_id,),
            ).fetchall()
            _labelled = {
                int(r[0]): bool(r[1]) for r in conn.execute(
                    """
                    select id, person_id is not null from faces
                    where photo_id = %s and not is_deleted
                    """,
                    (photo_id,),
                ).fetchall()
            }

        face_fixups: list[tuple[int, dict, dict]] = []
        if needs_dim_swap and face_rows:
            # x/w scaled by (disp_w / raw_w) = (raw_h / raw_w).
            # y/h scaled by (disp_h / raw_h) = (raw_w / raw_h).
            sx = raw_h / max(raw_w, 1)
            sy = raw_w / max(raw_h, 1)
            for fid, bbox in face_rows:
                b = bbox if isinstance(bbox, dict) else json.loads(bbox)
                new_bbox = {
                    "x": float(b.get("x", 0)) * sx,
                    "y": float(b.get("y", 0)) * sy,
                    "w": float(b.get("w", 0)) * sx,
                    "h": float(b.get("h", 0)) * sy,
                }
                face_fixups.append((int(fid), b, new_bbox))

        if needs_dim_swap:
            counts.rows.append(RepairRow(
                photo_id=photo_id, raw_w=raw_w, raw_h=raw_h,
                exif_tag=new_orientation, source=source or "?",
                faces=len(face_rows),
                labelled=sum(1 for _fid, _b in face_rows
                             if _labelled.get(int(_fid))),
            ))

        # Apply changes.
        if not dry_run:
            if (new_orientation != db_orientation) or (new_w, new_h) != (db_w, db_h):
                with dbmod.connection() as conn:
                    conn.autocommit = True
                    conn.execute(
                        """
                        update photos
                        set orientation = %s,
                            width = %s,
                            height = %s
                        where id = %s
                        """,
                        (new_orientation, new_w, new_h, photo_id),
                    )
                    # Every state change leaves a row. The absence of one is
                    # how nobody could tell this tool had never been run.
                    dbmod.audit(
                        conn, actor="desktop",
                        action="photo.orientation_repaired",
                        entity_type="photo", entity_id=photo_id,
                        previous_value={"orientation": db_orientation,
                                        "width": db_w, "height": db_h,
                                        # Every box as it was, so this is
                                        # reversible from the audit row alone.
                                        "faces": [{"id": fid, "bbox": old_b}
                                                  for fid, old_b, _new in
                                                  face_fixups]},
                        new_value={"orientation": new_orientation,
                                   "width": new_w, "height": new_h,
                                   "raw": [raw_w, raw_h], "source": source,
                                   "faces_rescaled": len(face_fixups)},
                    )
            for fid, old_bbox, new_bbox in face_fixups:
                with dbmod.connection() as conn:
                    conn.autocommit = True
                    conn.execute(
                        "update faces set bbox = %s where id = %s",
                        (json.dumps(new_bbox), fid),
                    )
                counts.faces_repaired += 1
            # Regenerate face crops for repaired photos.
            if face_fixups and working_path and Path(working_path).exists():
                with dbmod.connection() as conn:
                    conn.autocommit = True
                    fresh_faces = conn.execute(
                        """
                        select id, bbox from faces
                        where photo_id = %s and not is_deleted
                        """,
                        (photo_id,),
                    ).fetchall()
                inserted = []
                for fid, bbox in fresh_faces:
                    b = bbox if isinstance(bbox, dict) else json.loads(bbox)
                    inserted.append((int(fid), b))
                write_face_crops(Path(working_path), inserted, thumbs_dir)
                counts.face_crops_regenerated += len(inserted)
        else:
            counts.faces_repaired += len(face_fixups)

        if needs_dim_swap:
            counts.dims_swapped += 1
            log.info(
                "photo %d (%s): orientation=%s, raw=(%d,%d) → display=(%d,%d), "
                "%d face(s) rebuilt",
                photo_id, source, new_orientation, raw_w, raw_h, new_w, new_h,
                len(face_fixups),
            )

    return counts


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="photoarchive.tools.repair_face_boxes")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None,
                         help="Stop after N photos (smoke test)")
    args = parser.parse_args(argv)

    settings = load_config()
    dbmod.init_pool(settings)
    try:
        counts = repair(dry_run=args.dry_run, limit=args.limit)
    finally:
        dbmod.close_pool()

    if args.dry_run and counts.rows:
        print(f"{len(counts.rows)} photos need repairing:")
        for r in counts.rows:
            print(r.line())
        print(f"  total boxes {sum(r.faces for r in counts.rows)}, "
              f"labelled {sum(r.labelled for r in counts.rows)}")
    print(json.dumps(counts.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
