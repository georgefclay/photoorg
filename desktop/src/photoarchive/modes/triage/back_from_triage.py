"""Phase 3 fix-up 2: the B key in Triage.

George's blank_or_dark scan is really the back of a print — the back
detector missed it because there's barely any ink. B says "this is a
back". For scan-root photos, we insert a pending `ingest_pairings` row
that treats the photo as a back:

- `back_photo_id` = this photo (photo-as-back pattern from Phase 2)
- `front_photo_id` = the immediately preceding photo in the same folder
  by `scan_sequence` — but null if that predecessor is itself a back or
  a pending back (we don't chain backs off backs; George picks the front
  in the review grid).
- `back_score` = 1.0 (human-asserted)
- `details.source = 'triage'`
- `triage_status` on the photo becomes `keep` so it doesn't get junked
  before the review grid decision.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from ... import db
from ...config import Settings
from ..ingest import back_detect, paths

log = logging.getLogger(__name__)


class NotAScan(RuntimeError):
    """B was pressed on a digital-root photo. Not an error, just a
    signal for the UI to show 'not a scan' in the status bar."""


class AlreadyProposed(RuntimeError):
    """Photo already has a pending or rejected pairing as a back."""


@dataclass(frozen=True)
class BackProposalResult:
    photo_id: int
    pairing_id: int
    front_photo_id: int | None
    aspect_mismatch: bool
    reason: str  # 'ok' | 'orphan_no_predecessor' | 'orphan_predecessor_is_back'


def _load_photo(conn: psycopg.Connection, photo_id: int
               ) -> tuple[dict[str, Any], dict[str, Any]]:
    photo = conn.execute(
        """
        select p.id, p.source_root, p.source_folder, p.source_filename,
               p.scan_sequence, p.working_path, p.triage_status, p.is_deleted
        from photos p
        where p.id = %s
        """,
        (photo_id,),
    ).fetchone()
    if photo is None:
        raise ValueError(f"photo {photo_id} not found")
    master = conn.execute(
        """
        select master_path, sha256
        from photo_masters
        where photo_id = %s and is_preferred
        """,
        (photo_id,),
    ).fetchone()
    if master is None:
        raise ValueError(f"photo {photo_id} has no preferred master")
    return (
        {
            "id": photo[0], "source_root": photo[1],
            "source_folder": photo[2], "source_filename": photo[3],
            "scan_sequence": photo[4], "working_path": photo[5],
            "triage_status": photo[6], "is_deleted": photo[7],
        },
        {"master_path": master[0], "sha256": master[1]},
    )


def _immediate_predecessor(
    conn: psycopg.Connection, *, source_root: str,
    source_folder: str, scan_sequence: int | None,
) -> int | None:
    """Return the photo id of the immediately preceding non-deleted photo
    in the same folder by `scan_sequence`, or None."""
    if scan_sequence is None:
        return None
    row = conn.execute(
        """
        select id from photos
        where source_root = %s and source_folder = %s
          and scan_sequence is not null and scan_sequence < %s
          and not is_deleted
        order by scan_sequence desc, id desc
        limit 1
        """,
        (source_root, source_folder, scan_sequence),
    ).fetchone()
    return int(row[0]) if row else None


def _is_predecessor_a_back(conn: psycopg.Connection, pid: int) -> bool:
    """True if this photo is currently a back or pending back:
    - a pending ingest_pairings row with back_photo_id = pid, OR
    - a photo_backs row (a real back file, though those aren't in `photos`)
    A photo can be listed in photo_backs only if it was demoted; but
    demoted photos are is_deleted=true, so the earlier query already
    excludes them. So this reduces to "pending back proposal."
    """
    row = conn.execute(
        """
        select 1 from ingest_pairings
        where back_photo_id = %s and status = 'pending'
        limit 1
        """,
        (pid,),
    ).fetchone()
    return row is not None


def propose_back_from_triage(
    settings: Settings, photo_id: int,
    *, actor: str = "desktop",
) -> BackProposalResult:
    """B key from the Triage panel. Raises NotAScan if the photo is on a
    digital root, AlreadyProposed if it already has a pending or rejected
    pairing as a back."""
    scan_root_labels = {r.label for r in settings.master_roots
                        if r.kind == "scan"}

    with db.connection() as conn:
        conn.autocommit = False
        try:
            photo, master = _load_photo(conn, photo_id)
            if photo["source_root"] not in scan_root_labels:
                raise NotAScan(f"photo {photo_id} is on digital root "
                               f"{photo['source_root']}")

            already = conn.execute(
                """
                select id, status from ingest_pairings
                where back_photo_id = %s
                  and status in ('pending', 'rejected')
                limit 1
                """,
                (photo_id,),
            ).fetchone()
            if already:
                raise AlreadyProposed(
                    f"photo {photo_id} already has an ingest_pairings row "
                    f"(id {already[0]}, status {already[1]})"
                )

            pred_id = _immediate_predecessor(
                conn,
                source_root=photo["source_root"],
                source_folder=photo["source_folder"],
                scan_sequence=photo["scan_sequence"],
            )
            reason = "ok"
            front_id: int | None = None
            aspect_mismatch = False
            if pred_id is None:
                reason = "orphan_no_predecessor"
            elif _is_predecessor_a_back(conn, pred_id):
                reason = "orphan_predecessor_is_back"
            else:
                front_id = pred_id
                # Aspect mismatch tag if we can compute both aspects; when
                # working file is missing we skip and leave it False so
                # George reviews it as-is.
                try:
                    from PIL import Image
                    from ..ingest.image_io import open_image
                    def _ar(wp: str | None) -> float | None:
                        if not wp or not Path(wp).exists():
                            return None
                        with open_image(Path(wp)) as img:
                            img.load()
                            w, h = img.size
                            return w / max(h, 1)
                    pw = conn.execute(
                        "select working_path from photos where id = %s",
                        (pred_id,),
                    ).fetchone()[0]
                    a = _ar(pw)
                    b = _ar(photo["working_path"])
                    if a is not None and b is not None:
                        aspect_mismatch = not back_detect.aspect_close(a, b)
                except Exception as e:
                    log.warning("aspect check failed for triage-back %s: %s",
                                photo_id, e)

            staging_working = photo["working_path"]
            staging_thumb = str(paths.thumb_path(settings, photo_id))

            row = conn.execute(
                """
                insert into ingest_pairings
                  (front_photo_id, back_master_path, back_sha256,
                   back_source_folder, back_source_filename,
                   back_scan_sequence, back_score,
                   staging_working_path, staging_thumb_path,
                   back_photo_id, back_aspect_mismatch, details)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                returning id
                """,
                (
                    front_id, master["master_path"], master["sha256"],
                    photo["source_folder"], photo["source_filename"],
                    photo["scan_sequence"], 1.0,
                    staging_working, staging_thumb,
                    photo_id, aspect_mismatch,
                    json.dumps({"source": "triage", "reason": reason}),
                ),
            ).fetchone()
            pairing_id = int(row[0])

            # Set triage_status='keep' so the photo does not get junked
            # while it waits in the review grid.
            prev_status = photo["triage_status"]
            if prev_status != "keep":
                conn.execute(
                    """
                    update photos
                    set triage_status = 'keep',
                        is_private = false,
                        is_deleted = false,
                        deleted_at = null,
                        file_version = file_version + 1
                    where id = %s
                    """,
                    (photo_id,),
                )

            db.audit(
                conn, actor=actor, action="triage.propose_back",
                entity_type="photo", entity_id=photo_id,
                previous_value={"triage_status": prev_status},
                new_value={
                    "triage_status": "keep",
                    "pairing_id": pairing_id,
                    "front_photo_id": front_id,
                    "reason": reason,
                    "aspect_mismatch": aspect_mismatch,
                },
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return BackProposalResult(
        photo_id=photo_id, pairing_id=pairing_id, front_photo_id=front_id,
        aspect_mismatch=aspect_mismatch, reason=reason,
    )


def pending_pairings_count() -> int:
    """Cheap query for the Triage status bar."""
    with db.connection() as conn:
        conn.autocommit = True
        return int(conn.execute(
            "select count(*) from ingest_pairings where status = 'pending'"
        ).fetchone()[0])
