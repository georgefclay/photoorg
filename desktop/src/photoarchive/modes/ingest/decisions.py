"""Accept/reject flow for held pairings and rescans."""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

import psycopg

from ... import db
from ...config import Settings
from . import paths, staging

log = logging.getLogger(__name__)


# Fix-up 6: proposals whose DB decision committed but whose file move
# failed. The status bar surfaces the count; the integrity check tool
# lists the ids. In-memory — repopulated on demand by scanning the DB.
_NEEDS_FILE_REPAIR: set[int] = set()


def needs_file_repair_ids() -> set[int]:
    return set(_NEEDS_FILE_REPAIR)


def needs_file_repair_count() -> int:
    return len(_NEEDS_FILE_REPAIR)


def mark_file_repair_needed(pairing_id: int) -> None:
    _NEEDS_FILE_REPAIR.add(pairing_id)


def clear_file_repair(pairing_id: int) -> None:
    _NEEDS_FILE_REPAIR.discard(pairing_id)


class SourceFileMissing(RuntimeError):
    """Raised before any DB mutation when the proposal's source file
    can't be found. The UI surfaces this as a friendly banner, never a
    raw OSError."""

    def __init__(self, pairing_id: int, kind: str, path: Path) -> None:
        super().__init__(
            f"Pairing {pairing_id}: {kind} file not found at {path}. "
            f"Nothing was changed."
        )
        self.pairing_id = pairing_id
        self.kind = kind
        self.path = path


@dataclass(frozen=True)
class BackFiles:
    """Where a proposal's back file and thumb are RIGHT NOW.
    Not the DB-recorded staging paths — the resolver reads current state
    so that a photo that has since been junked, or a photo-as-back
    proposal whose file already lives at photos.working_path, both
    resolve to the file that actually exists on disk."""
    working: Path
    thumb: Path | None
    # Where the file was located from: 'staging', 'photo_working',
    # 'photo_quarantine'. Purely for logs and diagnostics.
    source: str


def resolve_back_files(
    conn: psycopg.Connection, pairing_id: int, settings: Settings,
) -> BackFiles:
    """Return the current on-disk location of a pending pairing's back
    file (and thumb). See BackFiles docstring for the resolution rules.

    Held-back proposal (back_photo_id null):
      working -> ingest_pairings.staging_working_path
      thumb   -> ingest_pairings.staging_thumb_path

    Photo-as-back proposal (back_photo_id set):
      working -> photos.working_path OR photos.quarantine_path
      thumb   -> THUMBS_DIR/{back_photo_id:08d}.jpg
    """
    row = conn.execute(
        """
        select ip.back_photo_id, ip.staging_working_path,
               ip.staging_thumb_path,
               p.working_path, p.quarantine_path
        from ingest_pairings ip
        left join photos p on p.id = ip.back_photo_id
        where ip.id = %s
        """,
        (pairing_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"pairing {pairing_id} not found")
    back_pid, staging_w, staging_t, photo_w, photo_q = row

    if back_pid is None:
        return BackFiles(
            working=Path(staging_w),
            thumb=Path(staging_t) if staging_t else None,
            source="staging",
        )

    thumb = settings.THUMBS_DIR / f"{back_pid:08d}.jpg"
    if photo_w:
        return BackFiles(working=Path(photo_w), thumb=thumb,
                         source="photo_working")
    if photo_q:
        return BackFiles(working=Path(photo_q), thumb=thumb,
                         source="photo_quarantine")
    # Fall back to the recorded staging path — this is what a rebuild
    # would have stored if the photo lost working_path somehow.
    return BackFiles(
        working=Path(staging_w) if staging_w else Path(""),
        thumb=thumb, source="staging_fallback",
    )


# ---------- Pairing ----------

def accept_pairing(settings: Settings, pairing_id: int) -> None:
    """Promote a proposed back to a photo_backs row.

    Two shapes of proposal:
      - **held back** (back_photo_id is null): the file has been sitting in
        `WORKING_DIR/_staging/` since ingest. Move staging → back working
        name, insert photo_backs.
      - **photo-as-back** (back_photo_id set): rebuild or the Triage B key
        proposed an already-committed photo as the back of another. The
        file lives at photos.working_path (or quarantine_path if the
        photo was junked meanwhile); move it to the back working name,
        insert photo_backs, then mark the demoted photos row is_deleted
        with `physical_ref_note='converted to back of photo <front_id>'`.

    Fix-up 6 order of operations:
      1. Verify source files exist (resolver, current on-disk state).
      2. DB transaction: photo_backs insert, demote photo, mark accepted,
         audit. Commit.
      3. Move file and thumb OUTSIDE the transaction. On move failure log
         it, keep the DB as decided, add the pairing to the "needs file
         repair" list surfaced by the status bar. The DB is the source of
         truth; files catch up.
    """
    _accept_pairing_impl(settings, pairing_id, orphan=False)


def accept_pairing_orphan(settings: Settings, pairing_id: int) -> int:
    """Accept the back but with no front (photo_id null). See
    accept_pairing for the order of operations. Returns the new
    photo_backs.id."""
    return _accept_pairing_impl(settings, pairing_id, orphan=True)


def _accept_pairing_impl(
    settings: Settings, pairing_id: int, *, orphan: bool,
) -> int:
    # Step 1: read the row + resolve source files. This is a read-only
    # snapshot; no locks yet.
    with db.connection() as conn:
        conn.autocommit = True
        row = conn.execute(
            """
            select front_photo_id, back_master_path, back_sha256,
                   back_source_folder, back_source_filename, back_scan_sequence,
                   back_score, status, back_photo_id
            from ingest_pairings where id = %s
            """,
            (pairing_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"pairing {pairing_id} not found")
        (front_id, back_master_path, sha, folder, name, seq,
         score, status, back_pid) = row
        if status != "pending":
            raise ValueError(f"pairing {pairing_id} is {status}, not pending")
        files = resolve_back_files(conn, pairing_id, settings)

    if not files.working.exists():
        raise SourceFileMissing(pairing_id, "back working", files.working)

    # Step 2: DB txn. Insert photo_backs (with the final working_path
    # already recorded), demote the photo row if photo-as-back, mark
    # accepted, audit. Commit.
    audit_new: dict = {"status": "accepted", "source": files.source}
    front_for_row = None if orphan else front_id
    with db.connection() as conn:
        conn.autocommit = False
        try:
            # SELECT ... FOR UPDATE re-checks status to guard against
            # a second reviewer racing us.
            recheck = conn.execute(
                "select status from ingest_pairings where id = %s for update",
                (pairing_id,),
            ).fetchone()
            if recheck is None or recheck[0] != "pending":
                raise ValueError(
                    f"pairing {pairing_id} is no longer pending"
                )

            new_row = conn.execute(
                """
                insert into photo_backs
                  (photo_id, master_path, sha256, source_folder,
                   source_filename, scan_sequence)
                values (%s, %s, %s, %s, %s, %s)
                returning id
                """,
                (front_for_row, back_master_path, sha, folder, name, seq),
            ).fetchone()
            back_id = new_row[0]
            audit_new["photo_back_id"] = back_id
            if orphan:
                audit_new["orphan"] = True
            else:
                audit_new["front_photo_id"] = front_id

            final_working = _final_back_working_path(
                settings, back_id, sha, str(files.working),
            )
            final_thumb = settings.THUMBS_DIR / f"back_{back_id:08d}.jpg"
            conn.execute(
                "update photo_backs set working_path = %s where id = %s",
                (str(final_working), back_id),
            )

            if back_pid is not None:
                note = (
                    "converted to orphan back" if orphan
                    else f"converted to back of photo {front_id}"
                )
                conn.execute(
                    """
                    update photos
                    set is_deleted = true,
                        deleted_at = now(),
                        physical_ref_note = %s,
                        working_path = null,
                        quarantine_path = null
                    where id = %s
                    """,
                    (note, back_pid),
                )
                audit_new["demoted_photo_id"] = back_pid

            conn.execute(
                "update ingest_pairings set status = 'accepted', "
                "decided_at = now() where id = %s",
                (pairing_id,),
            )
            db.audit(
                conn, actor="desktop",
                action="pairing.accept_orphan" if orphan else "pairing.accept",
                entity_type="ingest_pairing", entity_id=pairing_id,
                previous_value={"status": "pending"},
                new_value=audit_new,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # Step 3: move files. DB is committed; failures land in the repair
    # list and are surfaced in the status bar and integrity report.
    try:
        _move(files.working, final_working)
    except OSError as e:
        log.error(
            "pairing %s: DB committed but moving %s -> %s failed: %s. "
            "Marked for file repair.",
            pairing_id, files.working, final_working, e,
        )
        mark_file_repair_needed(pairing_id)
    if files.thumb and files.thumb.exists():
        try:
            _move(files.thumb, final_thumb)
        except OSError as e:
            log.error(
                "pairing %s: thumb move %s -> %s failed: %s.",
                pairing_id, files.thumb, final_thumb, e,
            )
            mark_file_repair_needed(pairing_id)

    return back_id


def change_pairing_front(
    settings: Settings, pairing_id: int, new_front_photo_id: int,
) -> None:
    """Re-point a pending pairing's front. Used by the review grid's
    filmstrip 'F' picker. Refuses if the pairing is already decided or
    the new front is deleted / not a scan-root photo."""
    with db.connection() as conn:
        conn.autocommit = False
        try:
            row = conn.execute(
                """
                select ip.status, ip.front_photo_id
                from ingest_pairings ip where id = %s for update
                """,
                (pairing_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"pairing {pairing_id} not found")
            status, old_front = row
            if status != "pending":
                raise ValueError(f"pairing {pairing_id} is {status}, not pending")
            new_front = conn.execute(
                """
                select id, is_deleted from photos where id = %s
                """,
                (new_front_photo_id,),
            ).fetchone()
            if new_front is None:
                raise ValueError(f"photo {new_front_photo_id} not found")
            if new_front[1]:
                raise ValueError(f"photo {new_front_photo_id} is deleted")
            conn.execute(
                "update ingest_pairings set front_photo_id = %s where id = %s",
                (new_front_photo_id, pairing_id),
            )
            db.audit(
                conn, actor="desktop", action="pairing.change_front",
                entity_type="ingest_pairing", entity_id=pairing_id,
                previous_value={"front_photo_id": old_front},
                new_value={"front_photo_id": new_front_photo_id, "reason": "filmstrip pick"},
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def reject_pairing(
    settings: Settings, pairing_id: int, *, job_run_id: int | None = None,
) -> int | None:
    """Reject a proposal.

    Held back (back_photo_id null): insert the staged file as a normal photo
    and return the new photo_id.

    Photo-as-back (back_photo_id set): just mark status='rejected'; the
    photo remains as it is. Rebuild uses the presence of a rejected pairing
    on that photo as the "never propose again" flag. Returns None.
    """
    with db.connection() as conn:
        conn.autocommit = False
        try:
            row = conn.execute(
                """
                select back_master_path, back_sha256, back_source_folder,
                       back_source_filename, back_scan_sequence,
                       staging_working_path, staging_thumb_path, status,
                       back_photo_id
                from ingest_pairings where id = %s
                for update
                """,
                (pairing_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"pairing {pairing_id} not found")
            (back_path, sha, folder, name, seq,
             staging_wpath, staging_tpath, status, back_pid) = row
            if status != "pending":
                raise ValueError(f"pairing {pairing_id} is {status}, not pending")

            if back_pid is not None:
                # Photo-as-back reject: leave photo alone, record decision.
                conn.execute(
                    "update ingest_pairings set status = 'rejected', decided_at = now() where id = %s",
                    (pairing_id,),
                )
                db.audit(conn, actor="desktop", action="pairing.reject",
                         entity_type="ingest_pairing", entity_id=pairing_id,
                         previous_value={"status": "pending"},
                         new_value={"status": "rejected", "back_photo_id": back_pid,
                                    "note": "photo preserved"})
                conn.commit()
                return None

            # Insert as a plain scan photo — we already have sha and metadata.
            # Read master details fresh (dimensions) via Pillow on the staging
            # copy so we don't touch the master again.
            from .image_io import mime_for_ext, open_image, probe_image
            from .hasher import perceptual_hashes
            ext = Path(back_path).suffix.lstrip(".").lower()
            with open_image(Path(staging_wpath)) as img:
                img.load()
                dims = probe_image(img)
                phash, dhash = perceptual_hashes(img)
            file_size = Path(back_path).stat().st_size
            mime = mime_for_ext(ext)
            # Batch = top-level of source_folder
            top = folder.split("/", 1)[0] if folder else None
            photo_id, _ = staging.insert_photo_and_master(
                conn, sha256_hex=sha, phash=phash, dhash=dhash,
                width=dims.display_width, height=dims.display_height,
                orientation=dims.orientation,
                master_width=dims.master_width, master_height=dims.master_height,
                mime=mime, file_size=file_size,
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

            # Point the rejected pairing at the newly-created photo so
            # future rebuilds see "this photo was already dealt with as a
            # back" via `back_photo_id`.
            conn.execute(
                """
                update ingest_pairings
                set status = 'rejected', decided_at = now(), back_photo_id = %s
                where id = %s
                """,
                (photo_id, pairing_id),
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
            from .image_io import open_image, probe_image
            with open_image(Path(staging_wpath)) as img:
                img.load()
                phash, dhash = perceptual_hashes(img)
                dims = probe_image(img)

            photo_id, _ = staging.insert_photo_and_master(
                conn, sha256_hex=sha, phash=phash, dhash=dhash,
                width=dims.display_width, height=dims.display_height,
                orientation=dims.orientation,
                master_width=dims.master_width, master_height=dims.master_height,
                mime=mime, file_size=size,
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
