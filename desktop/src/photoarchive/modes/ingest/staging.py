"""DB helpers for ingest: dedupe lookups, insert helpers, staging rows,
folder-hint suggestions, album creation, and rescan promotion.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

import psycopg

log = logging.getLogger(__name__)


def sha256_already_known(conn: psycopg.Connection, sha256_hex: str) -> str | None:
    """Return a short reason string if this sha256 has been seen before.
    None if it's new."""
    row = conn.execute("select 1 from photo_masters where sha256 = %s", (sha256_hex,)).fetchone()
    if row:
        return "photo_masters"
    row = conn.execute("select 1 from photo_backs where sha256 = %s", (sha256_hex,)).fetchone()
    if row:
        return "photo_backs"
    row = conn.execute(
        "select 1 from ingest_pairings where back_sha256 = %s", (sha256_hex,)
    ).fetchone()
    if row:
        return "ingest_pairings"
    row = conn.execute(
        "select 1 from ingest_rescans where new_sha256 = %s", (sha256_hex,)
    ).fetchone()
    if row:
        return "ingest_rescans"
    return None


def insert_photo_and_master(
    conn: psycopg.Connection,
    *,
    sha256_hex: str,
    phash: str | None,
    dhash: str | None,
    width: int | None,
    height: int | None,
    mime: str,
    file_size: int | None,
    is_scan: bool,
    capture_date: date | None,
    capture_date_precision: str,
    capture_date_confirmed: bool,
    exif_taken_at,
    exif_camera: str | None,
    exif_gps_lat: float | None,
    exif_gps_lon: float | None,
    source_root: str,
    source_folder: str,
    source_filename: str,
    scan_batch: str | None,
    scan_sequence: int | None,
    master_path: str,
    dpi: int | None = None,
) -> tuple[int, int]:
    """Insert photos + photo_masters (preferred=true). working_path is set
    later by update_photo_working_path once the file has been copied.
    Returns (photo_id, photo_master_id)."""
    row = conn.execute(
        """
        insert into photos
          (working_path, sha256, phash, dhash, width, height, mime, file_size,
           is_scan, capture_date, capture_date_precision, capture_date_confirmed,
           exif_taken_at, exif_camera, exif_gps_lat, exif_gps_lon,
           source_root, source_folder, source_filename, scan_batch, scan_sequence)
        values (%s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s)
        returning id
        """,
        (
            None, sha256_hex, phash, dhash, width, height, mime, file_size,
            is_scan, capture_date, capture_date_precision, capture_date_confirmed,
            exif_taken_at, exif_camera, exif_gps_lat, exif_gps_lon,
            source_root, source_folder, source_filename, scan_batch, scan_sequence,
        ),
    ).fetchone()
    photo_id: int = row[0]

    row = conn.execute(
        """
        insert into photo_masters
          (photo_id, master_path, sha256, width, height, dpi, mime, file_size, is_preferred)
        values (%s, %s, %s, %s, %s, %s, %s, %s, true)
        returning id
        """,
        (photo_id, master_path, sha256_hex, width, height, dpi, mime, file_size),
    ).fetchone()
    master_id: int = row[0]
    return photo_id, master_id


def update_photo_working_path(
    conn: psycopg.Connection, photo_id: int, working_path: str
) -> None:
    conn.execute(
        "update photos set working_path = %s where id = %s",
        (working_path, photo_id),
    )


def ensure_album(
    conn: psycopg.Connection, name: str
) -> int:
    """Return the id of an import album with this name; create if missing."""
    row = conn.execute(
        "select id from albums where lower(name) = lower(%s) and source = 'import'",
        (name,),
    ).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "insert into albums (name, source) values (%s, 'import') returning id",
        (name,),
    ).fetchone()
    return row[0]


def add_photo_to_album(conn: psycopg.Connection, album_id: int, photo_id: int) -> None:
    conn.execute(
        """
        insert into album_photos (album_id, photo_id)
        values (%s, %s) on conflict do nothing
        """,
        (album_id, photo_id),
    )


def insert_suggestion(
    conn: psycopg.Connection,
    *,
    kind: str,
    source: str,
    confidence: float,
    payload: dict[str, Any],
    photo_id: int | None,
    model: str | None = None,
) -> int:
    row = conn.execute(
        """
        insert into suggestions
          (photo_id, kind, source, model, confidence, payload, status)
        values (%s, %s, %s, %s, %s, %s::jsonb, 'pending')
        returning id
        """,
        (photo_id, kind, source, model, confidence, json.dumps(payload, default=str)),
    ).fetchone()
    return row[0]


def stage_pairing(
    conn: psycopg.Connection,
    *,
    front_photo_id: int,
    back_master_path: str,
    back_sha256: str,
    back_source_folder: str,
    back_source_filename: str,
    back_scan_sequence: int | None,
    back_score: float,
    staging_working_path: str,
    staging_thumb_path: str | None,
    back_aspect_mismatch: bool = False,
) -> int:
    row = conn.execute(
        """
        insert into ingest_pairings
          (front_photo_id, back_master_path, back_sha256,
           back_source_folder, back_source_filename, back_scan_sequence,
           back_score, staging_working_path, staging_thumb_path,
           back_aspect_mismatch)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        returning id
        """,
        (front_photo_id, back_master_path, back_sha256,
         back_source_folder, back_source_filename, back_scan_sequence,
         back_score, staging_working_path, staging_thumb_path,
         back_aspect_mismatch),
    ).fetchone()
    return row[0]


def stage_rescan(
    conn: psycopg.Connection,
    *,
    existing_photo_id: int,
    new_master_path: str,
    new_sha256: str,
    new_source_root: str,
    new_source_folder: str,
    new_source_filename: str,
    new_scan_batch: str | None,
    new_scan_sequence: int | None,
    distance: int,
    new_width: int | None,
    new_height: int | None,
    new_file_size: int | None,
    new_mime: str,
    staging_working_path: str,
    staging_thumb_path: str | None,
) -> int:
    row = conn.execute(
        """
        insert into ingest_rescans
          (existing_photo_id, new_master_path, new_sha256,
           new_source_root, new_source_folder, new_source_filename,
           new_scan_batch, new_scan_sequence, distance,
           new_width, new_height, new_file_size, new_mime,
           staging_working_path, staging_thumb_path)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s)
        returning id
        """,
        (existing_photo_id, new_master_path, new_sha256,
         new_source_root, new_source_folder, new_source_filename,
         new_scan_batch, new_scan_sequence, distance,
         new_width, new_height, new_file_size, new_mime,
         staging_working_path, staging_thumb_path),
    ).fetchone()
    return row[0]


def find_rescan_candidate(
    conn: psycopg.Connection, phash_hex: str, threshold: int = 6
) -> tuple[int, int] | None:
    """Return (photo_id, hamming) for the nearest existing scan photo whose
    pHash is within threshold, or None. pHash is stored as a hex string in
    photos.phash; we compare as unsigned bigints via `bit_count`. For a
    17k-row set a full scan is fine.
    """
    row = conn.execute(
        """
        with cand as (
          select id, phash,
                 -- Python-side XOR is easier than pushing bit_count into SQL for
                 -- 256-bit hex strings on Postgres 15. Fetch and check in Python.
                 (phash is not null and is_scan and not is_deleted) as ok
          from photos
          where is_scan and not is_deleted and phash is not null
        )
        select id, phash from cand where ok
        """
    ).fetchall()
    from .hasher import hamming
    best: tuple[int, int] | None = None
    for pid, ph in row:
        try:
            d = hamming(ph, phash_hex)
        except ValueError:
            continue
        if d <= threshold and (best is None or d < best[1]):
            best = (pid, d)
    return best


def previous_non_back_in_folder(
    conn: psycopg.Connection,
    *,
    source_root: str,
    source_folder: str,
    max_seq_exclusive: int,
) -> tuple[int, int | None] | None:
    """Return (photo_id, scan_sequence) of the previous non-back photo in
    this folder (scan_sequence < max_seq_exclusive), or None."""
    row = conn.execute(
        """
        select id, scan_sequence
        from photos
        where source_root = %s and source_folder = %s
          and (scan_sequence is null or scan_sequence < %s)
          and not is_deleted
        order by scan_sequence desc nulls last
        limit 1
        """,
        (source_root, source_folder, max_seq_exclusive),
    ).fetchone()
    if not row:
        return None
    return (row[0], row[1])


def accept_pairing(
    conn: psycopg.Connection,
    *,
    pairing_id: int,
) -> tuple[int, str]:
    """Promote a pending ingest_pairings row to a photo_backs row.
    Returns (photo_back_id, staging_working_path) so the caller can move
    the staging files into place."""
    row = conn.execute(
        """
        select front_photo_id, back_master_path, back_sha256,
               back_source_folder, back_source_filename, back_scan_sequence,
               staging_working_path, staging_thumb_path, status
        from ingest_pairings where id = %s
        """,
        (pairing_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"pairing {pairing_id} not found")
    (front_photo_id, back_master_path, back_sha256,
     src_folder, src_filename, src_seq,
     staging_working, staging_thumb, status) = row
    if status != "pending":
        raise ValueError(f"pairing {pairing_id} is {status}, not pending")
    back_row = conn.execute(
        """
        insert into photo_backs
          (photo_id, master_path, sha256, source_folder, source_filename, scan_sequence)
        values (%s, %s, %s, %s, %s, %s)
        returning id
        """,
        (front_photo_id, back_master_path, back_sha256,
         src_folder, src_filename, src_seq),
    ).fetchone()
    conn.execute(
        "update ingest_pairings set status = 'accepted', decided_at = now() where id = %s",
        (pairing_id,),
    )
    return back_row[0], staging_working
