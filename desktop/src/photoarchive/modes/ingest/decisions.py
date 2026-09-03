"""Accept/reject flow for held pairings and rescans."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

import psycopg

from ... import db
from ...config import Settings
from . import paths, staging

log = logging.getLogger(__name__)


# ---------- Pairing ----------

def accept_pairing(settings: Settings, pairing_id: int) -> None:
    """Promote a staged back to a photo_backs row and rename staging → final."""
    with db.connection() as conn:
        conn.autocommit = False
        try:
            row = conn.execute(
                """
                select front_photo_id, back_master_path, back_sha256,
                       back_source_folder, back_source_filename, back_scan_sequence,
                       back_score, staging_working_path, staging_thumb_path, status
                from ingest_pairings where id = %s
                for update
                """,
                (pairing_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"pairing {pairing_id} not found")
            (front_id, back_path, sha, folder, name, seq,
             score, staging_wpath, staging_tpath, status) = row
            if status != "pending":
                raise ValueError(f"pairing {pairing_id} is {status}, not pending")

            new_row = conn.execute(
                """
                insert into photo_backs
                  (photo_id, master_path, sha256, source_folder,
                   source_filename, scan_sequence)
                values (%s, %s, %s, %s, %s, %s)
                returning id
                """,
                (front_id, back_path, sha, folder, name, seq),
            ).fetchone()
            back_id = new_row[0]

            # Rename staging → final working path for the back.
            final_wpath = _final_back_working_path(settings, back_id, sha, staging_wpath)
            _move(Path(staging_wpath), final_wpath)
            conn.execute(
                "update photo_backs set working_path = %s where id = %s",
                (str(final_wpath), back_id),
            )
            if staging_tpath:
                dest_thumb = settings.THUMBS_DIR / f"back_{back_id:08d}.jpg"
                _move(Path(staging_tpath), dest_thumb)

            conn.execute(
                "update ingest_pairings set status = 'accepted', decided_at = now() where id = %s",
                (pairing_id,),
            )
            db.audit(conn, actor="desktop", action="pairing.accept",
                     entity_type="ingest_pairing", entity_id=pairing_id,
                     previous_value={"status": "pending"},
                     new_value={"status": "accepted", "photo_back_id": back_id,
                                "front_photo_id": front_id})
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def reject_pairing(
    settings: Settings, pairing_id: int, *, job_run_id: int | None = None,
) -> int:
    """Reject a staged back → insert it as a normal new photo.
    Returns the new photo_id."""
    with db.connection() as conn:
        conn.autocommit = False
        try:
            row = conn.execute(
                """
                select back_master_path, back_sha256, back_source_folder,
                       back_source_filename, back_scan_sequence,
                       staging_working_path, staging_thumb_path, status
                from ingest_pairings where id = %s
                for update
                """,
                (pairing_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"pairing {pairing_id} not found")
            (back_path, sha, folder, name, seq,
             staging_wpath, staging_tpath, status) = row
            if status != "pending":
                raise ValueError(f"pairing {pairing_id} is {status}, not pending")

            # Insert as a plain scan photo — we already have sha and metadata.
            # Read master details fresh (dimensions) via Pillow on the staging
            # copy so we don't touch the master again.
            from .image_io import mime_for_ext, open_image
            from .hasher import perceptual_hashes
            ext = Path(back_path).suffix.lstrip(".").lower()
            with open_image(Path(staging_wpath)) as img:
                img.load()
                width, height = img.size
                phash, dhash = perceptual_hashes(img)
            file_size = Path(back_path).stat().st_size
            mime = mime_for_ext(ext)
            # Batch = top-level of source_folder
            top = folder.split("/", 1)[0] if folder else None
            photo_id, _ = staging.insert_photo_and_master(
                conn, sha256_hex=sha, phash=phash, dhash=dhash,
                width=width, height=height, mime=mime, file_size=file_size,
                is_scan=True,
                capture_date=None, capture_date_precision="unknown",
                capture_date_confirmed=False,
                exif_taken_at=None, exif_camera=None,
                exif_gps_lat=None, exif_gps_lon=None,
                source_root=_source_root_for_path(conn, back_path),
                source_folder=folder, source_filename=name,
                scan_batch=top, scan_sequence=seq,
                master_path=back_path,
            )

            final_wpath = paths.working_path(settings, photo_id, sha, ext)
            _move(Path(staging_wpath), final_wpath)
            staging.update_photo_working_path(conn, photo_id, str(final_wpath))
            if staging_tpath:
                final_tpath = paths.thumb_path(settings, photo_id)
                _move(Path(staging_tpath), final_tpath)

            conn.execute(
                "update ingest_pairings set status = 'rejected', decided_at = now() where id = %s",
                (pairing_id,),
            )
            db.audit(conn, actor="desktop", action="pairing.reject",
                     entity_type="ingest_pairing", entity_id=pairing_id,
                     previous_value={"status": "pending"},
                     new_value={"status": "rejected", "new_photo_id": photo_id})
            if job_run_id is not None:
                db.record_job_item(conn, job_run_id=job_run_id, photo_id=photo_id,
                                   status="ok")
            conn.commit()
            return photo_id
        except Exception:
            conn.rollback()
            raise


# ---------- Rescan ----------

def accept_rescan(settings: Settings, rescan_id: int) -> None:
    """Add a new photo_masters row for existing_photo_id; mark it preferred if
    it has more pixels than the current preferred. Update photos.sha256 /
    working_path / file_version accordingly."""
    with db.connection() as conn:
        conn.autocommit = False
        try:
            row = conn.execute(
                """
                select existing_photo_id, new_master_path, new_sha256,
                       new_width, new_height, new_file_size, new_mime,
                       staging_working_path, staging_thumb_path, status
                from ingest_rescans where id = %s
                for update
                """,
                (rescan_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"rescan {rescan_id} not found")
            (photo_id, new_master_path, sha, w, h, size, mime,
             staging_wpath, staging_tpath, status) = row
            if status != "pending":
                raise ValueError(f"rescan {rescan_id} is {status}, not pending")

            # Compare pixel counts against the current preferred master.
            prev = conn.execute(
                """
                select id, width, height from photo_masters
                where photo_id = %s and is_preferred
                """,
                (photo_id,),
            ).fetchone()
            prev_pixels = (prev[1] or 0) * (prev[2] or 0) if prev else 0
            new_pixels = (w or 0) * (h or 0)
            make_preferred = new_pixels > prev_pixels

            # Insert new master row.
            new_master = conn.execute(
                """
                insert into photo_masters
                  (photo_id, master_path, sha256, width, height, mime, file_size, is_preferred)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (photo_id, new_master_path, sha, w, h, mime, size, False),
            ).fetchone()
            new_master_id = new_master[0]

            if make_preferred:
                # Demote old, promote new, update photos row.
                if prev:
                    conn.execute(
                        "update photo_masters set is_preferred = false where id = %s",
                        (prev[0],),
                    )
                conn.execute(
                    "update photo_masters set is_preferred = true where id = %s",
                    (new_master_id,),
                )
                ext = Path(new_master_path).suffix.lstrip(".").lower()
                final_wpath = paths.working_path(settings, photo_id, sha, ext)
                _move(Path(staging_wpath), final_wpath)
                if staging_tpath:
                    _move(Path(staging_tpath), paths.thumb_path(settings, photo_id))
                conn.execute(
                    """
                    update photos
                    set sha256 = %s,
                        working_path = %s,
                        width = %s,
                        height = %s,
                        mime = %s,
                        file_size = %s,
                        file_version = file_version + 1
                    where id = %s
                    """,
                    (sha, str(final_wpath), w, h, mime, size, photo_id),
                )
                db.audit(
                    conn, actor="desktop", action="photo_masters.preferred_change",
                    entity_type="photo", entity_id=photo_id,
                    previous_value={"master_id": prev[0] if prev else None},
                    new_value={"master_id": new_master_id, "reason": "rescan-accept"},
                )
            else:
                # Not preferred; drop staging files, we have no working name for it.
                _safe_unlink(Path(staging_wpath))
                if staging_tpath:
                    _safe_unlink(Path(staging_tpath))

            conn.execute(
                "update ingest_rescans set status = 'accepted', decided_at = now() where id = %s",
                (rescan_id,),
            )
            db.audit(
                conn, actor="desktop", action="rescan.accept",
                entity_type="ingest_rescan", entity_id=rescan_id,
                previous_value={"status": "pending"},
                new_value={"status": "accepted", "photo_id": photo_id,
                           "new_master_id": new_master_id,
                           "became_preferred": make_preferred},
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def reject_rescan(
    settings: Settings, rescan_id: int, *, job_run_id: int | None = None,
) -> int:
    """Reject → insert as a normal new photo. Returns new photo_id."""
    with db.connection() as conn:
        conn.autocommit = False
        try:
            row = conn.execute(
                """
                select existing_photo_id, new_master_path, new_sha256,
                       new_source_root, new_source_folder, new_source_filename,
                       new_scan_batch, new_scan_sequence,
                       new_width, new_height, new_file_size, new_mime,
                       staging_working_path, staging_thumb_path, status
                from ingest_rescans where id = %s
                for update
                """,
                (rescan_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"rescan {rescan_id} not found")
            (_existing, new_master_path, sha,
             src_root, src_folder, src_name, batch, seq,
             w, h, size, mime,
             staging_wpath, staging_tpath, status) = row
            if status != "pending":
                raise ValueError(f"rescan {rescan_id} is {status}, not pending")

            from .hasher import perceptual_hashes
            from .image_io import open_image
            with open_image(Path(staging_wpath)) as img:
                img.load()
                phash, dhash = perceptual_hashes(img)

            photo_id, _ = staging.insert_photo_and_master(
                conn, sha256_hex=sha, phash=phash, dhash=dhash,
                width=w, height=h, mime=mime, file_size=size,
                is_scan=True,
                capture_date=None, capture_date_precision="unknown",
                capture_date_confirmed=False,
                exif_taken_at=None, exif_camera=None,
                exif_gps_lat=None, exif_gps_lon=None,
                source_root=src_root, source_folder=src_folder,
                source_filename=src_name,
                scan_batch=batch, scan_sequence=seq,
                master_path=new_master_path,
            )
            ext = Path(new_master_path).suffix.lstrip(".").lower()
            final_wpath = paths.working_path(settings, photo_id, sha, ext)
            _move(Path(staging_wpath), final_wpath)
            staging.update_photo_working_path(conn, photo_id, str(final_wpath))
            if staging_tpath:
                _move(Path(staging_tpath), paths.thumb_path(settings, photo_id))

            conn.execute(
                "update ingest_rescans set status = 'rejected', decided_at = now() where id = %s",
                (rescan_id,),
            )
            db.audit(
                conn, actor="desktop", action="rescan.reject",
                entity_type="ingest_rescan", entity_id=rescan_id,
                previous_value={"status": "pending"},
                new_value={"status": "rejected", "new_photo_id": photo_id},
            )
            if job_run_id is not None:
                db.record_job_item(conn, job_run_id=job_run_id, photo_id=photo_id,
                                   status="ok")
            conn.commit()
            return photo_id
        except Exception:
            conn.rollback()
            raise


# ---------- helpers ----------

def _final_back_working_path(
    settings: Settings, back_id: int, sha: str, staging_wpath: str,
) -> Path:
    ext = Path(staging_wpath).suffix.lstrip(".").lower()
    name = f"back_{back_id:08d}_{sha[:8]}.{ext}"
    return settings.WORKING_DIR / name


def _source_root_for_path(conn: psycopg.Connection, master_path: str) -> str:
    """Best-effort: find another photos row that already sits under this path
    root; fall back to the leftmost path component. Used only by
    reject_pairing since ingest_pairings doesn't carry source_root."""
    # We rely on the ingest_pairings row's back_master_path always being a
    # child of one of the configured roots. Match against known master roots
    # by prefix — cheap.
    from ...config import load as load_config
    for r in load_config().master_roots:
        try:
            Path(master_path).relative_to(r.path)
            return r.label
        except ValueError:
            continue
    return "unknown"


def _move(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    shutil.move(str(src), str(dest))


def _safe_unlink(p: Path) -> None:
    try:
        if p.exists():
            p.unlink()
    except OSError:
        log.warning("could not remove %s", p)
