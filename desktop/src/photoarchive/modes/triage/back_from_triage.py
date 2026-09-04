"""Phase 3 fix-ups 2 & 3: the B key in Triage.

George's blank_or_dark scan is really the back of a print — the back
detector missed it because there's barely any ink. B says "this is a
back". For scan-root photos, we insert a pending `ingest_pairings` row
that treats the photo as a back, and always resolve existing rows
rather than refusing:

- No existing row:      insert new pending pairing (outcome='inserted').
- Existing pending:     leave it, return the front label (outcome='already_pending').
- Existing rejected:    reopen — status flips back to pending, front is
                        recomputed, back_score=1.0, details.reopened_from
                        records the prior status, audit row written
                        (outcome='reopened').
- Existing accepted:    the photo should already be a demoted back and
                        not in triage; warn and change nothing
                        (outcome='already_accepted').
- Multiple rows:        if any pending, treat as pending. Otherwise apply
                        the rule to the most recent (highest id).

Layout of every proposal (new or reopened):
- back_photo_id  = this photo (photo-as-back pattern from Phase 2)
- front_photo_id = the immediately preceding photo in the same folder
  by scan_sequence, unless that predecessor is itself a back or a
  pending back — orphan in that case (George picks the front in the
  review grid).
- back_score = 1.0 (human-asserted)
- details.source = 'triage'
- triage_status on the photo becomes 'keep' so it doesn't get junked
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


# Outcome discriminator; UI decides banner vs last-action from this.
OUTCOME_INSERTED = "inserted"
OUTCOME_REOPENED = "reopened"
OUTCOME_ALREADY_PENDING = "already_pending"
OUTCOME_ALREADY_ACCEPTED = "already_accepted"
OUTCOME_NOT_A_SCAN = "not_a_scan"


@dataclass(frozen=True)
class BackProposalResult:
    photo_id: int
    outcome: str
    pairing_id: int | None
    front_photo_id: int | None
    front_label: str | None       # "Batch 00012 #017" for the status message
    aspect_mismatch: bool
    reason: str                   # 'ok' | 'orphan_no_predecessor' | 'orphan_predecessor_is_back' | 'n/a'
    reopened_from: str | None     # e.g. 'rejected'

    @property
    def is_refusal(self) -> bool:
        """UI shows these as a coloured banner, not just a last-action line."""
        return self.outcome in (OUTCOME_NOT_A_SCAN, OUTCOME_ALREADY_ACCEPTED)


def _load_photo(conn: psycopg.Connection, photo_id: int
               ) -> tuple[dict[str, Any], dict[str, Any]]:
    photo = conn.execute(
        """
        select p.id, p.source_root, p.source_folder, p.source_filename,
               p.scan_sequence, p.scan_batch, p.working_path,
               p.triage_status, p.is_deleted
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
            "scan_sequence": photo[4], "scan_batch": photo[5],
            "working_path": photo[6], "triage_status": photo[7],
            "is_deleted": photo[8],
        },
        {"master_path": master[0], "sha256": master[1]},
    )


def _immediate_predecessor(
    conn: psycopg.Connection, *, source_root: str,
    source_folder: str, scan_sequence: int | None,
) -> int | None:
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
    row = conn.execute(
        """
        select 1 from ingest_pairings
        where back_photo_id = %s and status = 'pending'
        limit 1
        """,
        (pid,),
    ).fetchone()
    return row is not None


def _front_label(conn: psycopg.Connection, front_id: int | None) -> str | None:
    if front_id is None:
        return None
    row = conn.execute(
        "select scan_batch, scan_sequence from photos where id = %s",
        (front_id,),
    ).fetchone()
    if row is None:
        return f"photo {front_id}"
    batch, seq = row
    if batch and seq is not None:
        return f"{batch} #{seq}"
    if batch:
        return batch
    return f"photo {front_id}"


def _fetch_existing_pairings(
    conn: psycopg.Connection, back_photo_id: int,
) -> list[dict[str, Any]]:
    """Return every ingest_pairings row for this back_photo_id, most
    recent first."""
    rows = conn.execute(
        """
        select id, status, front_photo_id, back_aspect_mismatch, details
        from ingest_pairings
        where back_photo_id = %s
        order by id desc
        """,
        (back_photo_id,),
    ).fetchall()
    return [{
        "id": int(r[0]), "status": r[1],
        "front_photo_id": r[2],
        "back_aspect_mismatch": bool(r[3]),
        "details": r[4] if isinstance(r[4], dict) else {},
    } for r in rows]


def _pick_existing(
    rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Multi-row rule: if any is pending, treat as pending (that row).
    Otherwise return the most recent row (rows are id-desc). None if
    the list is empty."""
    if not rows:
        return None
    for r in rows:
        if r["status"] == "pending":
            return r
    return rows[0]


def _compute_front(
    conn: psycopg.Connection, photo: dict[str, Any], settings: Settings,
) -> tuple[int | None, bool, str]:
    """Return (front_photo_id, aspect_mismatch, reason).
    reason ∈ 'ok' | 'orphan_no_predecessor' | 'orphan_predecessor_is_back'."""
    pred_id = _immediate_predecessor(
        conn,
        source_root=photo["source_root"],
        source_folder=photo["source_folder"],
        scan_sequence=photo["scan_sequence"],
    )
    if pred_id is None:
        return None, False, "orphan_no_predecessor"
    if _is_predecessor_a_back(conn, pred_id):
        return None, False, "orphan_predecessor_is_back"

    aspect_mismatch = False
    try:
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
                    photo["id"], e)
    return pred_id, aspect_mismatch, "ok"


def _set_triage_keep(
    conn: psycopg.Connection, photo_id: int, prev_status: str, actor: str,
    action: str, new_value_extra: dict[str, Any],
) -> None:
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
        conn, actor=actor, action=action,
        entity_type="photo", entity_id=photo_id,
        previous_value={"triage_status": prev_status},
        new_value={"triage_status": "keep", **new_value_extra},
    )


def propose_back_from_triage(
    settings: Settings, photo_id: int,
    *, actor: str = "desktop",
) -> BackProposalResult:
    """B key from the Triage panel. Always returns a result; the outcome
    field discriminates. See module docstring for the resolution rules."""
    scan_root_labels = {r.label for r in settings.master_roots
                        if r.kind == "scan"}

    with db.connection() as conn:
        conn.autocommit = False
        try:
            photo, master = _load_photo(conn, photo_id)
            if photo["source_root"] not in scan_root_labels:
                conn.commit()
                return BackProposalResult(
                    photo_id=photo_id, outcome=OUTCOME_NOT_A_SCAN,
                    pairing_id=None, front_photo_id=None,
                    front_label=None, aspect_mismatch=False,
                    reason="n/a", reopened_from=None,
                )

            existing = _fetch_existing_pairings(conn, photo_id)
            selected = _pick_existing(existing)

            if selected is not None and selected["status"] == "accepted":
                log.warning(
                    "B on photo %s: pairing %s already accepted "
                    "(front photo %s). Photo should be a demoted back "
                    "already, not in triage.",
                    photo_id, selected["id"], selected["front_photo_id"],
                )
                conn.commit()
                return BackProposalResult(
                    photo_id=photo_id, outcome=OUTCOME_ALREADY_ACCEPTED,
                    pairing_id=selected["id"],
                    front_photo_id=selected["front_photo_id"],
                    front_label=_front_label(conn, selected["front_photo_id"]),
                    aspect_mismatch=selected["back_aspect_mismatch"],
                    reason="n/a", reopened_from=None,
                )

            if selected is not None and selected["status"] == "pending":
                # Leave it; keep triage_status='keep' so nothing junks it.
                _set_triage_keep(
                    conn, photo_id, photo["triage_status"], actor,
                    action="triage.propose_back.already_pending",
                    new_value_extra={
                        "pairing_id": selected["id"],
                        "front_photo_id": selected["front_photo_id"],
                    },
                )
                conn.commit()
                return BackProposalResult(
                    photo_id=photo_id, outcome=OUTCOME_ALREADY_PENDING,
                    pairing_id=selected["id"],
                    front_photo_id=selected["front_photo_id"],
                    front_label=_front_label(conn, selected["front_photo_id"]),
                    aspect_mismatch=selected["back_aspect_mismatch"],
                    reason="n/a", reopened_from=None,
                )

            # No selected row, OR the most recent is rejected → reopen /
            # insert. Recompute front regardless.
            front_id, aspect_mismatch, reason = _compute_front(
                conn, photo, settings,
            )

            if selected is not None and selected["status"] == "rejected":
                prior_details = selected["details"] if isinstance(
                    selected["details"], dict,
                ) else {}
                new_details = {
                    **prior_details,
                    "source": "triage",
                    "reopened_from": "rejected",
                    "reason": reason,
                }
                conn.execute(
                    """
                    update ingest_pairings
                    set status = 'pending',
                        decided_at = null,
                        front_photo_id = %s,
                        back_score = 1.0,
                        back_aspect_mismatch = %s,
                        details = %s::jsonb
                    where id = %s
                    """,
                    (front_id, aspect_mismatch,
                     json.dumps(new_details), selected["id"]),
                )
                db.audit(
                    conn, actor=actor, action="triage.reopen_back",
                    entity_type="ingest_pairing", entity_id=selected["id"],
                    previous_value={"status": "rejected",
                                    "front_photo_id": selected["front_photo_id"]},
                    new_value={"status": "pending",
                               "front_photo_id": front_id,
                               "reason": reason,
                               "aspect_mismatch": aspect_mismatch},
                )
                _set_triage_keep(
                    conn, photo_id, photo["triage_status"], actor,
                    action="triage.propose_back",
                    new_value_extra={
                        "pairing_id": selected["id"],
                        "front_photo_id": front_id,
                        "reopened_from": "rejected",
                        "reason": reason,
                    },
                )
                conn.commit()
                return BackProposalResult(
                    photo_id=photo_id, outcome=OUTCOME_REOPENED,
                    pairing_id=selected["id"],
                    front_photo_id=front_id,
                    front_label=_front_label(conn, front_id),
                    aspect_mismatch=aspect_mismatch,
                    reason=reason, reopened_from="rejected",
                )

            # No existing row (or the "most recent" filter left nothing
            # applicable) → fresh insert.
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
            _set_triage_keep(
                conn, photo_id, photo["triage_status"], actor,
                action="triage.propose_back",
                new_value_extra={
                    "pairing_id": pairing_id,
                    "front_photo_id": front_id,
                    "reason": reason,
                    "aspect_mismatch": aspect_mismatch,
                },
            )
            conn.commit()
            return BackProposalResult(
                photo_id=photo_id, outcome=OUTCOME_INSERTED,
                pairing_id=pairing_id, front_photo_id=front_id,
                front_label=_front_label(conn, front_id),
                aspect_mismatch=aspect_mismatch,
                reason=reason, reopened_from=None,
            )
        except Exception:
            conn.rollback()
            raise


def pending_pairings_count() -> int:
    """Cheap query for the Triage status bar."""
    with db.connection() as conn:
        conn.autocommit = True
        return int(conn.execute(
            "select count(*) from ingest_pairings where status = 'pending'"
        ).fetchone()[0])
