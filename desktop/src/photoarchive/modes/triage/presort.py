"""Pre-sort job: compute one triage_hints row per untriaged photo.

Incremental: only photos with `triage_status = 'untriaged'` and no existing
triage_hints row are considered. Re-running is cheap.

Precedence when multiple hints match (highest wins):
  exact_dup_of → screenshot → blank_or_dark → tiny → document → burst

Losing hints are recorded in `details.also` so nothing is lost. `photo` is
the fallback when nothing matched.
"""
from __future__ import annotations

import json
import logging
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import psycopg
from PIL import Image

from ... import db
from ...workers import CancelToken, Cancelled
from ..ingest.image_io import open_image
from . import classifiers, burst

log = logging.getLogger(__name__)


HINT_PRECEDENCE = (
    "exact_dup_of", "screenshot", "blank_or_dark", "tiny", "document", "burst",
)


def _read_software(path: Path) -> str | None:
    """Best-effort read of the EXIF Software tag; None on any failure."""
    try:
        import exifread
        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False, stop_tag="Image Software")
        v = tags.get("Image Software")
        return str(v) if v is not None else None
    except Exception:
        return None


def _pick_hint(matches: dict[str, tuple[float, dict]]) -> tuple[str, float, dict]:
    """Apply precedence. `matches` maps hint→(confidence, details). Returns
    (chosen_hint, confidence, details_with_also)."""
    if not matches:
        return "photo", 1.0, {}
    order = [h for h in HINT_PRECEDENCE if h in matches]
    winner = order[0]
    conf, details = matches[winner]
    also = {h: matches[h][1] for h in order[1:]}
    if also:
        details = {**details, "also": also}
    return winner, conf, details


def _fetch_untriaged_without_hints(conn: psycopg.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select p.id, p.working_path, p.width, p.height, p.mime,
               p.exif_camera, p.exif_taken_at, p.phash, p.sha256,
               p.source_root, p.source_folder
        from photos p
        left join triage_hints h on h.photo_id = p.id
        where p.triage_status = 'untriaged'
          and not p.is_deleted
          and h.photo_id is null
        order by p.id
        """
    ).fetchall()
    return [{
        "id": r[0], "working_path": r[1],
        "width": r[2], "height": r[3], "mime": r[4],
        "exif_camera": r[5], "exif_taken_at": r[6],
        "phash": r[7], "sha256": r[8],
        "source_root": r[9], "source_folder": r[10],
    } for r in rows]


def _fetch_all_untriaged_for_burst(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Every untriaged photo — including those already with hints — so burst
    groups can span both. A photo already hinted `document` should still be
    reclassified as `burst` if its group qualifies (burst wins over document
    in precedence)."""
    rows = conn.execute(
        """
        select id, exif_taken_at, phash, source_root, source_folder, working_path
        from photos
        where triage_status = 'untriaged' and not is_deleted
          and exif_taken_at is not null and phash is not null
        order by source_root, source_folder, exif_taken_at, id
        """
    ).fetchall()
    return [{
        "id": r[0], "taken_at": r[1], "phash": r[2],
        "source_root": r[3], "source_folder": r[4], "working_path": r[5],
    } for r in rows]


def _count_sha_dups(conn: psycopg.Connection, sha: str, this_id: int) -> int:
    row = conn.execute(
        "select count(*) from photos where sha256 = %s and id <> %s and not is_deleted",
        (sha, this_id),
    ).fetchone()
    return int(row[0])


def _upsert_hint(
    conn: psycopg.Connection, *, photo_id: int, hint: str,
    confidence: float, details: dict,
) -> None:
    conn.execute(
        """
        insert into triage_hints (photo_id, hint, confidence, details, computed_at)
        values (%s, %s, %s, %s::jsonb, now())
        on conflict (photo_id) do update
          set hint = excluded.hint,
              confidence = excluded.confidence,
              details = excluded.details,
              computed_at = excluded.computed_at
        """,
        (photo_id, hint, float(confidence), json.dumps(details, default=str)),
    )


def _classify_one(row: dict[str, Any], conn: psycopg.Connection
                 ) -> tuple[dict[str, tuple[float, dict]], _ClassifyStats | None]:
    """Return the per-classifier matches for one photo, before precedence."""
    matches: dict[str, tuple[float, dict]] = {}
    stats: _ClassifyStats | None = None

    # exact_dup_of — safety net; ingest should have deduped already.
    dup_count = _count_sha_dups(conn, row["sha256"], row["id"])
    if dup_count > 0:
        matches["exact_dup_of"] = (1.0, {"dup_count": dup_count})

    # tiny
    matched, conf, det = classifiers.is_tiny(
        width=row["width"], height=row["height"],
    )
    if matched:
        matches["tiny"] = (conf, det)

    # screenshot — cheap; do before image decode
    software = None
    if row["working_path"]:
        software = _read_software(Path(row["working_path"]))
    matched, conf, det = classifiers.is_screenshot(
        width=row["width"], height=row["height"], mime=row["mime"],
        exif_camera=row["exif_camera"], exif_software=software,
    )
    if matched:
        matches["screenshot"] = (conf, det)

    # blank_or_dark and document need pixels.
    wp = row["working_path"]
    if wp and Path(wp).exists():
        try:
            with open_image(Path(wp)) as img:
                img.load()
                tone = classifiers.analyse_tone(img)
                stats = _ClassifyStats(tone=tone)
                matched, conf, det = classifiers.is_blank_or_dark(img, cached=tone)
                if matched:
                    matches["blank_or_dark"] = (conf, det)
                matched, conf, det = classifiers.is_document(img, cached=tone)
                if matched:
                    matches["document"] = (conf, det)
        except Exception as e:
            log.warning("classify decode failed for photo %s (%s): %s",
                        row["id"], wp, e)

    return matches, stats


class _ClassifyStats:
    def __init__(self, *, tone):
        self.tone = tone


def run_presort(
    *,
    progress_cb: Callable[[dict], None] = lambda _: None,
    cancel_token: CancelToken | None = None,
) -> dict[str, Any]:
    """Compute hints for every untriaged photo lacking one. Also recomputes
    burst membership across the current untriaged pool.

    Returns a summary dict with the hint distribution.
    """
    ctok = cancel_token or CancelToken()

    started = time.time()
    with db.connection() as conn:
        conn.autocommit = True
        job_run_id = db.start_job_run(
            conn, job_name="triage_presort", params={},
        )
        db.audit(conn, actor="desktop", action="triage_presort.start",
                 entity_type="job_run", entity_id=job_run_id, new_value={})

        needing = _fetch_untriaged_without_hints(conn)

    total = len(needing)
    progress_cb({"kind": "start", "total": total})

    # Per-photo pass (no burst yet).
    processed = 0
    per_hint_matches: dict[int, dict[str, tuple[float, dict]]] = {}
    for row in needing:
        if ctok.is_set():
            raise Cancelled()
        with db.connection() as conn:
            conn.autocommit = True
            try:
                matches, _stats = _classify_one(row, conn)
                per_hint_matches[row["id"]] = matches
                hint, conf, det = _pick_hint(matches)
                _upsert_hint(
                    conn, photo_id=row["id"],
                    hint=hint, confidence=conf, details=det,
                )
                db.record_job_item(conn, job_run_id=job_run_id,
                                   photo_id=row["id"], status="ok")
            except Exception as e:
                log.exception("presort failed for photo %s", row["id"])
                db.record_job_item(conn, job_run_id=job_run_id,
                                   photo_id=row["id"], status="error",
                                   error=repr(e))
        processed += 1
        if processed % 25 == 0 or processed == total:
            progress_cb({"kind": "progress", "done": processed, "total": total})

    # Burst pass: for every untriaged photo (with or without existing hint),
    # group by (source_root, source_folder) and cluster on time+phash.
    with db.connection() as conn:
        conn.autocommit = True
        all_untriaged = _fetch_all_untriaged_for_burst(conn)

    by_folder: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in all_untriaged:
        by_folder[(r["source_root"], r["source_folder"])].append(r)

    all_groups: list[list[burst.PhotoTime]] = []
    for (_root, _folder), photos in by_folder.items():
        if len(photos) < 3:
            continue
        ptimes = [burst.PhotoTime(
            photo_id=p["id"], taken_at=p["taken_at"], phash=p["phash"],
        ) for p in photos]
        all_groups.extend(burst.group_bursts(ptimes))

    # Compute sharpness only for burst-group members (expensive per file).
    sharpness: dict[int, float] = {}
    burst_member_ids = {p.photo_id for g in all_groups for p in g}
    path_by_id = {p["id"]: p["working_path"] for p in all_untriaged}
    for pid in burst_member_ids:
        wp = path_by_id.get(pid)
        if not wp or not Path(wp).exists():
            continue
        try:
            with open_image(Path(wp)) as img:
                img.load()
                sharpness[pid] = classifiers.laplacian_sharpness(img)
        except Exception as e:
            log.warning("sharpness failed for %s: %s", pid, e)

    extras = burst.burst_extras(all_groups, sharpness)
    burst_written = 0
    with db.connection() as conn:
        conn.autocommit = True
        # First: any existing burst rows for photos no longer in a burst extra
        # set → revert to their per-photo classification if we have it, else
        # 'photo'.
        current_extras = {e.photo_id for e in extras}
        stale = conn.execute(
            "select photo_id from triage_hints where hint = 'burst'"
        ).fetchall()
        for (pid,) in stale:
            if pid in current_extras:
                continue
            # Recompute this one from its previously stored matches. If we
            # didn't process it this pass (had a hint already), synthesise a
            # 'photo' fallback — a follow-up presort will re-classify it.
            matches = per_hint_matches.get(pid, {})
            hint, conf, det = _pick_hint(matches)
            _upsert_hint(conn, photo_id=pid, hint=hint,
                         confidence=conf, details=det)

        # Now write burst extras (overwrites existing hint per precedence).
        for e in extras:
            matches = per_hint_matches.get(e.photo_id, {}).copy()
            matches["burst"] = (0.9, {
                "sharpest_photo_id": e.sharpest_photo_id,
                "group_size": e.group_size,
                "time_span_s": e.time_span_s,
                "peers": e.peers,
            })
            hint, conf, det = _pick_hint(matches)
            _upsert_hint(conn, photo_id=e.photo_id,
                         hint=hint, confidence=conf, details=det)
            burst_written += 1

    # Distribution report.
    with db.connection() as conn:
        conn.autocommit = True
        rows = conn.execute(
            """
            select h.hint, count(*)
            from triage_hints h
            join photos p on p.id = h.photo_id
            where p.triage_status = 'untriaged' and not p.is_deleted
            group by h.hint
            order by count(*) desc
            """
        ).fetchall()
        distribution = {r[0]: int(r[1]) for r in rows}
        without = conn.execute(
            """
            select count(*) from photos p
            left join triage_hints h on h.photo_id = p.id
            where p.triage_status = 'untriaged' and not p.is_deleted
              and h.photo_id is null
            """
        ).fetchone()[0]

    ended = time.time()
    summary = {
        "processed": processed,
        "total": total,
        "elapsed_s": round(ended - started, 2),
        "burst_extras": burst_written,
        "distribution": distribution,
        "untriaged_without_hints_after": int(without),
        "job_run_id": job_run_id,
    }

    with db.connection() as conn:
        conn.autocommit = True
        db.finish_job_run(conn, job_run_id=job_run_id, status="ok", stats=summary)
        db.audit(conn, actor="desktop", action="triage_presort.finish",
                 entity_type="job_run", entity_id=job_run_id, new_value=summary)

    progress_cb({"kind": "done", "summary": summary})
    return summary


def count_photos_needing_hints() -> int:
    """Cheap query used by the Triage panel to label the presort button."""
    with db.connection() as conn:
        conn.autocommit = True
        return int(conn.execute(
            """
            select count(*) from photos p
            left join triage_hints h on h.photo_id = p.id
            where p.triage_status = 'untriaged' and not p.is_deleted
              and h.photo_id is null
            """
        ).fetchone()[0])
