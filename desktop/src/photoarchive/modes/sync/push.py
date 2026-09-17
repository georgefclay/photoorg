"""Push: send the laptop's authoritative rows to the web.

Order:
  photos (metadata, ≤200 per batch) → files (per photo, ≤N in parallel later)
  → photo_masters → people → person_name_variants → relationships
  → places → photo_places → albums → album_photos
  → faces (embeddings gated by SYNC_FACE_EMBEDDINGS) → photo_backs → suggestions
  → photo_groups

The photo metadata call returns `need_files` — the file upload step only
walks that list. Resumable via `photos.synced_at` and
`photos.synced_file_version` on the laptop: the selector for the next
run skips rows whose synced_file_version == file_version AND synced_at
> photos.updated_at.

Private and junk photos are excluded at the selector.

Pull first, always (Phase 9 fix-up 1): every push starts with
pull_groups + pull_confirmed (which copies web-born rows down first), so
a web change — a rescan flag, an accepted suggestion, a moderator's
group removal — is on the laptop before the laptop's rows go back up
and can't be clobbered by a stale push.

Web-origin rows (id >= WEB_ID_FLOOR in the tables of
shared/id-ranges.json) are never pushed; the web would refuse them.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from psycopg.rows import dict_row

from ... import db
from ...id_ranges import WEB_ID_FLOOR
from ..ingest.paths import resolve_working_path
from .client import WebSyncClient, WebSyncError
from .pull import pull_confirmed, pull_groups

log = logging.getLogger(__name__)


PHOTO_BATCH = 200
META_BATCH = 500


@dataclass
class PushProgress:
    stage: str
    done: int
    total: int
    detail: str = ""


@dataclass
class PushStats:
    photos_upserted: int = 0
    files_uploaded: int = 0
    bytes_uploaded: int = 0
    tables: dict[str, int] = field(default_factory=dict)


def _select_photos(conn, offset: int, limit: int) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            select id, sha256, phash, dhash, width, height, mime, file_size,
                   is_scan, has_no_people,
                   capture_date, capture_date_precision, capture_date_confirmed,
                   exif_taken_at, exif_camera, exif_gps_lat, exif_gps_lon,
                   source_root, source_folder, source_filename,
                   scan_batch, scan_sequence, physical_ref_note,
                   rescan_wanted, triage_status, is_private, is_deleted, deleted_at,
                   orientation, description_ai, completeness_score, file_version,
                   working_path, synced_at, synced_file_version, updated_at
              from photos
             where is_private = false
               and triage_status <> 'junk'
             order by id asc
            offset %s limit %s
            """,
            (offset, limit),
        )
        return cur.fetchall()


def _serialise_photo(row: dict) -> dict:
    # Server takes ids on the wire; convert paths to bare basenames so
    # the web doesn't learn about D: paths.
    working = row.get("working_path")
    working_basename = os.path.basename(working) if working else None
    return {
        "id": row["id"], "sha256": row["sha256"], "phash": row["phash"], "dhash": row["dhash"],
        "width": row["width"], "height": row["height"], "mime": row["mime"], "file_size": row["file_size"],
        "is_scan": row["is_scan"], "has_no_people": row["has_no_people"],
        "capture_date": row["capture_date"].isoformat() if row["capture_date"] else None,
        "capture_date_precision": row["capture_date_precision"],
        "capture_date_confirmed": row["capture_date_confirmed"],
        "exif_taken_at": row["exif_taken_at"].isoformat() if row["exif_taken_at"] else None,
        "exif_camera": row["exif_camera"],
        "exif_gps_lat": row["exif_gps_lat"], "exif_gps_lon": row["exif_gps_lon"],
        "source_root": row["source_root"], "source_folder": row["source_folder"],
        "source_filename": row["source_filename"], "scan_batch": row["scan_batch"],
        "scan_sequence": row["scan_sequence"], "physical_ref_note": row["physical_ref_note"],
        "rescan_wanted": row["rescan_wanted"], "triage_status": row["triage_status"],
        "is_private": False,  # never send private
        "is_deleted": row["is_deleted"], "deleted_at": row["deleted_at"].isoformat() if row["deleted_at"] else None,
        "orientation": row["orientation"], "description_ai": row["description_ai"],
        "completeness_score": row["completeness_score"],
        "file_version": row["file_version"],
        "working_path": working_basename,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


def _photos_total(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "select count(*) from photos where is_private = false and triage_status <> 'junk'"
        )
        return cur.fetchone()[0]


def _fetch_ids_needing_upload(conn, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            select id, working_path, file_version, file_size
              from photos where id = any(%s::bigint[]) and working_path is not null
            """,
            (ids,),
        )
        return cur.fetchall()


def _filter_to_grouped(conn, ids: list[int]) -> list[int]:
    """Restrict a photo id list to those that live in at least one live
    photo_groups row. Used when files_only_for_grouped=True — we still
    push every photo's metadata (harmless, keeps the web catalogue
    complete) but only upload the file bytes for photos a real user
    can see.
    """
    if not ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            select distinct pg.photo_id
              from photo_groups pg
             where pg.is_deleted = false
               and pg.photo_id = any(%s::bigint[])
            """,
            (ids,),
        )
        return [int(r[0]) for r in cur.fetchall()]


class SharedDatabaseError(WebSyncError):
    """The web server we are about to push to is writing to the SAME
    Postgres database this desktop reads. Pushing would overwrite the
    desktop's own working_path rows with basenames (fix-up 11)."""


def local_db_identity(conn) -> tuple[str | None, str | None]:
    with conn.cursor() as cur:
        cur.execute(
            "select current_database(), system_identifier::text from pg_control_system()"
        )
        row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


def guard_not_shared_database(conn, client: WebSyncClient) -> None:
    """Raise SharedDatabaseError if the web's /sync/status reports the same
    (database name, cluster system_identifier) as our own connection.
    A web that cannot report its identity is logged and allowed."""
    try:
        remote = (client.status() or {}).get("db") or {}
    except WebSyncError as e:
        log.warning("push pre-flight: /sync/status unavailable (%s); cannot verify DB isolation", e)
        return
    r_name, r_id = remote.get("name"), remote.get("system_identifier")
    if not r_name or not r_id:
        log.warning("push pre-flight: web did not report a DB identity; cannot verify DB isolation")
        return
    l_name, l_id = local_db_identity(conn)
    if (l_name, l_id) == (r_name, str(r_id)):
        raise SharedDatabaseError(
            f"refusing to push: the web at this URL writes to database {r_name!r} on the "
            f"same Postgres cluster this desktop uses. Desktop and web must never share a "
            f"database on one machine — point web/.env DATABASE_URL at photoorg_web (see GC.md)."
        )


def _mark_synced(conn, ids: list[int]) -> None:
    if not ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            "update photos set synced_at = now() where id = any(%s::bigint[])",
            (ids,),
        )


def push(
    client: WebSyncClient,
    *,
    working_dir: Path,
    thumbs_dir: Path,
    state_dir: Path,
    send_face_embeddings: bool = False,
    files_only_for_grouped: bool = True,
    progress: Callable[[PushProgress], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    allow_shared_db: bool = False,
) -> PushStats:
    """Run one full push. Blocking. `progress` receives updates; return
    True from `should_stop` to break at the next batch boundary.

    files_only_for_grouped (default True): send file bytes only for
    photos that live in at least one live `photo_groups` row. Metadata
    for every non-private/non-junk photo still goes across so the web
    catalogue is complete and lists/search behave. The full-file push
    (`files_only_for_grouped=False`) is scheduled once cleanup and
    inference jobs on the laptop stop bumping `file_version`; passing
    False casually would burn hours re-pushing bytes that are about to
    change.

    state_dir holds the pull cursors (sync_state.json); the pre-push pull
    uses and advances them exactly like the Pull tab does.
    """
    stats = PushStats()

    with db.connection() as conn:
        conn.autocommit = True
        # Fix-up 11 pre-flight: never push into our own database.
        # `allow_shared_db=True` is an explicit test-only opt-in.
        if not allow_shared_db:
            guard_not_shared_database(conn, client)

        # Pull first, always — see module docstring.
        if progress:
            progress(PushProgress(stage="pull", done=0, total=2, detail="groups"))
        pull_groups(client, state_dir)
        if progress:
            progress(PushProgress(stage="pull", done=1, total=2, detail="confirmed + web-origin rows"))
        stats.tables["pulled_facts"] = pull_confirmed(
            client, state_dir, working_dir=working_dir, thumbs_dir=thumbs_dir,
        )
        total_photos = _photos_total(conn)

        # -------- photos + files --------------------------------------
        offset = 0
        while True:
            if should_stop and should_stop():
                return stats
            rows = _select_photos(conn, offset, PHOTO_BATCH)
            if not rows:
                break
            batch = [_serialise_photo(r) for r in rows]
            resp = client.push_photos(batch)
            stats.photos_upserted += resp.get("upserted", 0)

            # Upload files the web says it needs.
            need_ids = list(resp.get("need_files", []))
            if files_only_for_grouped:
                need_ids = _filter_to_grouped(conn, need_ids)
            need_meta = _fetch_ids_needing_upload(conn, need_ids)
            for i, m in enumerate(need_meta):
                if should_stop and should_stop():
                    return stats
                wpath = resolve_working_path(working_dir, m["working_path"])
                if not wpath.exists():
                    log.warning("push: missing working file for photo %s (%s)", m["id"], wpath)
                    continue
                client.push_photo_file(m["id"], wpath)
                stats.files_uploaded += 1
                stats.bytes_uploaded += m.get("file_size") or wpath.stat().st_size
                if progress:
                    progress(PushProgress(
                        stage="photo_files",
                        done=i + 1,
                        total=len(need_meta),
                        detail=f"id={m['id']} sent_total={stats.files_uploaded}",
                    ))

            _mark_synced(conn, [r["id"] for r in rows])
            offset += len(rows)
            if progress:
                progress(PushProgress(
                    stage="photos", done=offset, total=total_photos,
                    detail=f"batch of {len(rows)}",
                ))
            if len(rows) < PHOTO_BATCH:
                break

        # -------- metadata tables -------------------------------------
        for stage, sql, marshaller in _META_STAGES:
            n_pushed = _push_meta_stage(conn, client, stage, sql, marshaller, progress, should_stop)
            stats.tables[stage] = n_pushed

        # -------- faces (embeddings gated) ----------------------------
        n_faces = _push_faces(conn, client, send_face_embeddings, progress, should_stop)
        stats.tables["faces"] = n_faces

        # -------- photo_backs (metadata) + back files -----------------
        # The web answers each batch with `need_files`: backs whose image
        # isn't on its disk yet. Those are uploaded after the metadata.
        back_need: list[int] = []
        n_backs = _push_meta_stage(conn, client, "photo_backs",
            """
            select id, photo_id, master_path, sha256, working_path,
                   source_folder, source_filename, scan_sequence,
                   transcribed_text, transcription_confidence, transcription_confirmed,
                   created_at
              from photo_backs
             where photo_id in (
                 select id from photos where is_private = false and triage_status <> 'junk'
             )
            """,
            lambda r: {
                "id": r["id"], "photo_id": r["photo_id"], "master_path": r["master_path"],
                "sha256": r["sha256"], "working_path": os.path.basename(r["working_path"]) if r["working_path"] else None,
                "source_folder": r["source_folder"], "source_filename": r["source_filename"],
                "scan_sequence": r["scan_sequence"],
                "transcribed_text": r["transcribed_text"],
                "transcription_confidence": r["transcription_confidence"],
                "transcription_confirmed": r["transcription_confirmed"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }, progress, should_stop,
            on_response=lambda resp: back_need.extend(resp.get("need_files") or []),
        )
        stats.tables["photo_backs"] = n_backs
        stats.tables["back_files"] = _push_back_files(
            conn, client, working_dir, back_need, files_only_for_grouped, progress, should_stop,
        )

        # -------- suggestions -----------------------------------------
        stats.tables["suggestions"] = _push_meta_stage(conn, client, "suggestions",
            """
            select s.id, s.photo_id, s.user_id, s.kind, s.payload::text as payload_text,
                   s.confidence, s.status, s.source, s.model,
                   s.resolved_by, s.resolved_at, s.resolution_note, s.created_at
              from suggestions s
             left join photos p on p.id = s.photo_id
             where s.source in ('ai', 'import')
               and s.id < %(web_id_floor)s
               and (p.id is null or (p.is_private = false and p.triage_status <> 'junk'))
            """,
            lambda r: {
                "id": r["id"], "photo_id": r["photo_id"], "user_id": r["user_id"],
                "kind": r["kind"], "payload": r["payload_text"],
                "confidence": r["confidence"], "status": r["status"], "source": r["source"],
                "model": r["model"], "resolved_by": r["resolved_by"],
                "resolved_at": r["resolved_at"].isoformat() if r["resolved_at"] else None,
                "resolution_note": r["resolution_note"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }, progress, should_stop,
        )

        # -------- photo_groups (adds AND soft-removes) ----------------
        stats.tables["photo_groups"] = _push_meta_stage(conn, client, "photo_groups",
            """
            select pg.photo_id, pg.group_id, pg.added_by, pg.added_at,
                   pg.is_deleted, pg.deleted_at, pg.deleted_by, pg.updated_at
              from photo_groups pg
              join photos p on p.id = pg.photo_id
             where p.is_private = false
            """,
            lambda r: {
                "photo_id": r["photo_id"], "group_id": r["group_id"],
                "added_by": r["added_by"], "added_at": r["added_at"].isoformat() if r["added_at"] else None,
                "is_deleted": r["is_deleted"],
                "deleted_at": r["deleted_at"].isoformat() if r["deleted_at"] else None,
                "deleted_by": r["deleted_by"],
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }, progress, should_stop,
        )

    return stats


def _push_meta_stage(
    conn, client: WebSyncClient, stage: str, sql: str, marshaller,
    progress, should_stop, on_response=None,
) -> int:
    with conn.cursor(row_factory=dict_row) as cur:
        # Stage queries may filter `id < %(web_id_floor)s` (web-origin rows stay put).
        cur.execute(sql, {"web_id_floor": WEB_ID_FLOOR})
        rows = cur.fetchall()
    n = 0
    for i in range(0, len(rows), META_BATCH):
        if should_stop and should_stop():
            return n
        chunk = [marshaller(r) for r in rows[i:i + META_BATCH]]
        chunk = [c for c in chunk if c is not None]
        if not chunk:
            continue
        resp = client.push_batch(stage, chunk)
        n += resp.get("upserted", 0)
        if on_response:
            on_response(resp)
        if progress:
            progress(PushProgress(stage=stage, done=min(i + META_BATCH, len(rows)), total=len(rows)))
    return n


def _push_back_files(
    conn, client: WebSyncClient, working_dir: Path, back_ids: list[int],
    files_only_for_grouped: bool, progress, should_stop,
) -> int:
    """Upload the back images the web reported missing. Same file scope as
    photos: with files_only_for_grouped, only backs of photos in a live
    group. A missing or undecodable file logs and is skipped (the next push
    asks for it again)."""
    if not back_ids:
        return 0
    grouped_clause = """
               and exists (select 1 from photo_groups pg
                            where pg.photo_id = pb.photo_id and pg.is_deleted = false)
    """ if files_only_for_grouped else ""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            select pb.id, pb.working_path
              from photo_backs pb
             where pb.id = any(%s::bigint[])
               and pb.working_path is not null
               {grouped_clause}
             order by pb.id
            """,
            (list(back_ids),),
        )
        rows = cur.fetchall()
    sent = 0
    for i, r in enumerate(rows):
        if should_stop and should_stop():
            break
        path = resolve_working_path(working_dir, r["working_path"])
        if not path.exists():
            log.warning("push: missing back file for back %s (%s)", r["id"], path)
            continue
        try:
            client.push_back_file(r["id"], path)
            sent += 1
        except WebSyncError as e:
            log.warning("push: back %s upload failed: %s", r["id"], e)
        if progress:
            progress(PushProgress(stage="back_files", done=i + 1, total=len(rows)))
    return sent


def _push_faces(
    conn, client: WebSyncClient, send_embeddings: bool,
    progress, should_stop,
) -> int:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            select f.id, f.photo_id, f.person_id, f.bbox::text as bbox_text, f.embedding,
                   f.embedding_model, f.embedding_stale,
                   f.source, f.is_disputed, f.disputed_by, f.dispute_note, f.created_by,
                   f.review_status, f.review_note, f.reviewed_at,
                   f.is_deleted, f.deleted_at, f.delete_reason, f.created_at
              from faces f
              join photos p on p.id = f.photo_id
             where p.is_private = false
               and p.triage_status <> 'junk'
               and f.id < %s
             order by f.id asc
            """,
            (WEB_ID_FLOOR,),
        )
        rows = cur.fetchall()
    n = 0
    for i in range(0, len(rows), META_BATCH):
        if should_stop and should_stop():
            return n
        chunk = []
        for r in rows[i:i + META_BATCH]:
            item = {
                "id": r["id"], "photo_id": r["photo_id"], "person_id": r["person_id"],
                "bbox": r["bbox_text"],
                "embedding_model": r["embedding_model"],
                "embedding_stale": r["embedding_stale"],
                "source": r["source"], "is_disputed": r["is_disputed"],
                "disputed_by": r["disputed_by"], "dispute_note": r["dispute_note"],
                "created_by": r["created_by"],
                "review_status": r["review_status"], "review_note": r["review_note"],
                "reviewed_at": r["reviewed_at"].isoformat() if r["reviewed_at"] else None,
                "is_deleted": r["is_deleted"],
                "deleted_at": r["deleted_at"].isoformat() if r["deleted_at"] else None,
                "delete_reason": r["delete_reason"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            if send_embeddings and r["embedding"] is not None:
                item["embedding"] = list(r["embedding"])
            chunk.append(item)
        resp = client.push_batch("faces", chunk)
        n += resp.get("upserted", 0)
        if progress:
            progress(PushProgress(stage="faces", done=min(i + META_BATCH, len(rows)), total=len(rows)))
    return n


# Ordered list of simple meta stages driven by SELECT + marshaller.
_META_STAGES = [
    (
        "photo_masters",
        """
        select pm.id, pm.photo_id, pm.master_path, pm.sha256, pm.width, pm.height,
               pm.dpi, pm.mime, pm.file_size, pm.is_preferred, pm.ingested_at
          from photo_masters pm
          join photos p on p.id = pm.photo_id
         where p.is_private = false and p.triage_status <> 'junk'
        """,
        lambda r: {
            "id": r["id"], "photo_id": r["photo_id"], "master_path": r["master_path"],
            "sha256": r["sha256"], "width": r["width"], "height": r["height"],
            "dpi": r["dpi"], "mime": r["mime"], "file_size": r["file_size"],
            "is_preferred": r["is_preferred"],
            "ingested_at": r["ingested_at"].isoformat() if r["ingested_at"] else None,
        },
    ),
    (
        "people",
        """
        select id, given_name, middle_name, surname, maiden_name, nickname, suffix,
               birth_year, death_year, notes, is_deleted, created_at
          from people
         where id < %(web_id_floor)s
        """,
        lambda r: {
            "id": r["id"], "given_name": r["given_name"], "middle_name": r["middle_name"],
            "surname": r["surname"], "maiden_name": r["maiden_name"],
            "nickname": r["nickname"], "suffix": r["suffix"],
            "birth_year": r["birth_year"], "death_year": r["death_year"],
            "notes": r["notes"], "is_deleted": r["is_deleted"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        },
    ),
    (
        "person_name_variants",
        """
        select id, person_id, variant, kind, created_at
          from person_name_variants
         where id < %(web_id_floor)s
        """,
        lambda r: {
            "id": r["id"], "person_id": r["person_id"], "variant": r["variant"],
            "kind": r["kind"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        },
    ),
    (
        "relationships",
        """
        select id, person_a_id, person_b_id, type, confirmed, created_by, created_at
          from relationships
         where id < %(web_id_floor)s
        """,
        lambda r: {
            "id": r["id"], "person_a_id": r["person_a_id"], "person_b_id": r["person_b_id"],
            "type": r["type"], "confirmed": r["confirmed"], "created_by": r["created_by"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        },
    ),
    (
        "places",
        """
        select id, name, latitude, longitude, notes, is_deleted, created_at
          from places
         where id < %(web_id_floor)s
        """,
        lambda r: {
            "id": r["id"], "name": r["name"], "latitude": r["latitude"],
            "longitude": r["longitude"], "notes": r["notes"], "is_deleted": r["is_deleted"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        },
    ),
    (
        "photo_places",
        """
        select pp.photo_id, pp.place_id, pp.confirmed
          from photo_places pp
          join photos p on p.id = pp.photo_id
         where p.is_private = false and p.triage_status <> 'junk'
        """,
        lambda r: {"photo_id": r["photo_id"], "place_id": r["place_id"], "confirmed": r["confirmed"]},
    ),
    (
        "albums",
        """
        select id, name, description, source, created_by, is_deleted, created_at
          from albums
         where id < %(web_id_floor)s
        """,
        lambda r: {
            "id": r["id"], "name": r["name"], "description": r["description"],
            "source": r["source"], "created_by": r["created_by"], "is_deleted": r["is_deleted"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        },
    ),
    (
        "album_photos",
        """
        select ap.album_id, ap.photo_id, ap.position
          from album_photos ap
          join photos p on p.id = ap.photo_id
         where p.is_private = false and p.triage_status <> 'junk'
        """,
        lambda r: {"album_id": r["album_id"], "photo_id": r["photo_id"], "position": r["position"]},
    ),
]
