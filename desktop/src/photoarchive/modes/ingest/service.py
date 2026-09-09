"""Ingest orchestrator: guard → walk → hash/exif/thumb → dedup/rescan/back →
commit or stage. One transaction per file so Ctrl-C leaves the DB consistent.
"""
from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Sequence

from PIL import Image

from ... import db
from ...config import MasterRoot, Settings
from ...workers import CancelToken, Cancelled
from . import back_detect, paths, staging
from .exif import ExifData, read_exif
from .guard import GuardResult, remediation_message, run_masters_guard
from .hasher import perceptual_hashes, sha256_file
from .image_io import mime_for_ext, open_image
from .scanner import ScannedFile, is_batch_folder, parse_date_folder, walk_root
from .thumbs import write_thumb

log = logging.getLogger(__name__)


@dataclass
class IngestCounts:
    new: int = 0
    skipped_dupe: int = 0
    skipped_video: int = 0
    backs_proposed: int = 0
    rescans_proposed: int = 0
    failed: int = 0

    def as_dict(self) -> dict:
        return {
            "new": self.new,
            "skipped_dupe": self.skipped_dupe,
            "skipped_video": self.skipped_video,
            "backs_proposed": self.backs_proposed,
            "rescans_proposed": self.rescans_proposed,
            "failed": self.failed,
        }


@dataclass
class IngestSummary:
    started_at: float
    ended_at: float
    counts_by_root: dict[str, IngestCounts] = field(default_factory=dict)
    guard: dict | None = None
    cancelled: bool = False
    job_run_id: int | None = None

    @property
    def elapsed(self) -> float:
        return self.ended_at - self.started_at

    @property
    def totals(self) -> IngestCounts:
        t = IngestCounts()
        for c in self.counts_by_root.values():
            t.new += c.new
            t.skipped_dupe += c.skipped_dupe
            t.skipped_video += c.skipped_video
            t.backs_proposed += c.backs_proposed
            t.rescans_proposed += c.rescans_proposed
            t.failed += c.failed
        return t

    def as_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "elapsed": self.elapsed,
            "cancelled": self.cancelled,
            "job_run_id": self.job_run_id,
            "counts_by_root": {k: v.as_dict() for k, v in self.counts_by_root.items()},
            "totals": self.totals.as_dict(),
        }


class GuardRefused(RuntimeError):
    def __init__(self, guard: GuardResult):
        super().__init__(remediation_message(guard))
        self.guard = guard


def run_ingest(
    *,
    settings: Settings,
    roots: Sequence[MasterRoot],
    progress_cb: Callable[[dict], None] = lambda _: None,
    cancel_token: CancelToken | None = None,
) -> IngestSummary:
    """Full ingest pass over `roots`. See module docstring."""
    ctok = cancel_token or CancelToken()
    paths.ensure_dirs(settings)

    guard = run_masters_guard(roots)
    if not guard.all_read_only:
        raise GuardRefused(guard)

    summary = IngestSummary(started_at=time.time(), ended_at=0.0)
    summary.guard = guard.to_params()

    with db.connection() as conn:
        conn.autocommit = True
        job_run_id = db.start_job_run(
            conn, job_name="ingest",
            params={"guard": summary.guard,
                    "roots": [{"label": r.label, "path": str(r.path), "kind": r.kind}
                              for r in roots]},
        )
        db.audit(conn, actor="desktop", action="ingest.start",
                 entity_type="job_run", entity_id=job_run_id,
                 new_value={"roots": [r.label for r in roots]})
    summary.job_run_id = job_run_id

    try:
        for root in roots:
            counts = IngestCounts()
            summary.counts_by_root[root.label] = counts
            _ingest_root(
                settings=settings, root=root, counts=counts,
                job_run_id=job_run_id, progress_cb=progress_cb, cancel=ctok,
            )
            if ctok.is_set():
                summary.cancelled = True
                break
    finally:
        summary.ended_at = time.time()
        with db.connection() as conn:
            conn.autocommit = True
            db.finish_job_run(
                conn, job_run_id=job_run_id,
                status=("cancelled" if summary.cancelled else "ok"),
                stats=summary.as_dict(),
            )
            db.audit(conn, actor="desktop", action="ingest.finish",
                     entity_type="job_run", entity_id=job_run_id,
                     new_value=summary.as_dict())
    return summary


def _ingest_root(
    *,
    settings: Settings,
    root: MasterRoot,
    counts: IngestCounts,
    job_run_id: int,
    progress_cb: Callable[[dict], None],
    cancel: CancelToken,
) -> None:
    per_folder_prev: dict[str, dict] = {}  # {folder: {photo_id, aspect, was_back}}
    for f in walk_root(root):
        if cancel.is_set():
            raise Cancelled()
        try:
            _process_file(
                settings=settings, root=root, f=f, counts=counts,
                job_run_id=job_run_id, per_folder_prev=per_folder_prev,
            )
        except Exception as e:  # noqa: BLE001 — file-level failure, keep running
            log.exception("ingest failure on %s", f.master_path)
            counts.failed += 1
            with db.connection() as conn:
                conn.autocommit = True
                db.record_ingest_failure(
                    conn, job_run_id=job_run_id,
                    source_root=root.label, source_folder=f.source_folder,
                    source_filename=f.source_filename,
                    master_path=str(f.master_path), error=repr(e),
                )
        progress_cb({
            "kind": "counts",
            "root": root.label,
            "counts": counts.as_dict(),
            "last": str(f.master_path),
        })


def _process_file(
    *,
    settings: Settings,
    root: MasterRoot,
    f: ScannedFile,
    counts: IngestCounts,
    job_run_id: int,
    per_folder_prev: dict[str, dict],
) -> None:
    if f.is_video:
        counts.skipped_video += 1
        log.info("skip video (Phase 2 does not process video): %s", f.master_path)
        return

    sha = sha256_file(f.master_path)

    with db.connection() as conn:
        conn.autocommit = False
        try:
            already = staging.sha256_already_known(conn, sha)
            if already:
                counts.skipped_dupe += 1
                conn.commit()
                log.debug("skip dupe (%s): %s", already, f.master_path)
                return
        except Exception:
            conn.rollback()
            raise

    # Decode + features (outside DB txn; we hold no locks yet).
    from .image_io import probe_image
    with open_image(f.master_path) as img:
        img.load()
        phash, dhash = perceptual_hashes(img)
        dims = probe_image(img)
        back_feat = back_detect.analyse(img) if root.kind == "scan" else None
        # Re-open for thumb (some formats disallow re-use after transpose).
    # Fix-up 6: photos.width/height are DISPLAY dims (post EXIF-transpose)
    # so the face writer, preview, and per-photo view all read the same
    # coordinate frame. Master row keeps the raw file dims.
    width, height = dims.display_width, dims.display_height
    exif = read_exif(f.master_path)

    file_size = f.master_path.stat().st_size
    mime = mime_for_ext(f.ext)

    # Rescan detection (scan roots only)
    rescan_hit: tuple[int, int] | None = None
    if root.kind == "scan":
        with db.connection() as conn:
            conn.autocommit = True
            rescan_hit = staging.find_rescan_candidate(conn, phash, threshold=6)

    # Decide the path: rescan candidate, back candidate, or normal.
    # Fix-up 2: aspect is evidence, not a veto — at raw score >= 0.8 we
    # propose regardless of aspect and let the review grid tag it.
    # Fix-up 4: if the immediate predecessor is itself a probable back
    # (or the file is first in the folder), propose as an ORPHAN back
    # (front_photo_id null) instead of skipping — don't walk back.
    prev = per_folder_prev.get(f.source_folder)
    aspect_mismatch = False
    is_back_candidate = False
    orphan_front = False
    front_photo_id_for_back: int | None = None
    if (root.kind == "scan"
            and back_feat is not None
            and back_feat.score >= 0.6):
        if prev is None or prev.get("was_back", False):
            is_back_candidate = True
            orphan_front = True
        else:
            aspects_ok = back_detect.aspect_close(prev["aspect"], back_feat.aspect_ratio)
            aspect_mismatch = not aspects_ok
            is_back_candidate = aspects_ok or back_feat.score >= 0.8
            if is_back_candidate:
                front_photo_id_for_back = prev["photo_id"]

    if rescan_hit is not None:
        _handle_rescan(
            settings=settings, root=root, f=f, sha=sha,
            phash=phash, width=width, height=height,
            file_size=file_size, mime=mime, rescan_hit=rescan_hit,
            counts=counts, job_run_id=job_run_id, per_folder_prev=per_folder_prev,
            back_aspect=(back_feat.aspect_ratio if back_feat else (width / max(height, 1))),
        )
        return

    if is_back_candidate:
        _handle_back(
            settings=settings, root=root, f=f, sha=sha,
            width=width, height=height, back_feat=back_feat,  # type: ignore[arg-type]
            counts=counts, job_run_id=job_run_id, per_folder_prev=per_folder_prev,
            front_photo_id=front_photo_id_for_back,
            aspect_mismatch=aspect_mismatch,
        )
        return

    _commit_normal(
        settings=settings, root=root, f=f, sha=sha,
        phash=phash, dhash=dhash, width=width, height=height, dims=dims,
        file_size=file_size, mime=mime, exif=exif,
        back_aspect=(back_feat.aspect_ratio if back_feat else (width / max(height, 1))),
        counts=counts, job_run_id=job_run_id, per_folder_prev=per_folder_prev,
    )


def _commit_normal(
    *, settings, root, f, sha, phash, dhash, width, height, dims, file_size, mime, exif,
    back_aspect, counts, job_run_id, per_folder_prev,
) -> None:
    is_scan_flag = (root.kind == "scan") or (
        root.kind == "digital"
        and exif.taken_at is None
        and exif.camera is None
    )

    capture_date_val: date | None = None
    precision = "unknown"
    confirmed = False
    if exif.taken_at is not None:
        capture_date_val = exif.taken_at.date()
        precision = "exact"
        confirmed = True

    with db.connection() as conn:
        conn.autocommit = False
        try:
            photo_id, master_id = staging.insert_photo_and_master(
                conn,
                sha256_hex=sha, phash=phash, dhash=dhash,
                width=width, height=height,
                orientation=exif.orientation,
                master_width=dims.master_width, master_height=dims.master_height,
                mime=mime, file_size=file_size,
                is_scan=is_scan_flag,
                capture_date=capture_date_val,
                capture_date_precision=precision,
                capture_date_confirmed=confirmed,
                exif_taken_at=exif.taken_at,
                exif_camera=exif.camera,
                exif_gps_lat=exif.gps_lat, exif_gps_lon=exif.gps_lon,
                source_root=root.label,
                source_folder=f.source_folder,
                source_filename=f.source_filename,
                scan_batch=f.scan_batch, scan_sequence=f.scan_sequence,
                master_path=str(f.master_path),
            )

            # Folder hints → suggestions/albums
            _record_folder_hints(conn, root=root, f=f, photo_id=photo_id, has_exif=confirmed)

            # Copy master → working name (outside SQL, still inside txn).
            wpath = paths.working_path(settings, photo_id, sha, f.ext)
            _copy_master_to_working(f.master_path, wpath)
            try:
                staging.update_photo_working_path(conn, photo_id, str(wpath))
            except Exception:
                _safe_unlink(wpath)
                raise

            # Thumb outside txn on rollback; if it fails after commit, non-fatal.
            db.record_job_item(conn, job_run_id=job_run_id, photo_id=photo_id,
                               status="ok")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # Post-commit: thumb (best-effort).
    try:
        with open_image(f.master_path) as img:
            write_thumb(img, paths.thumb_path(settings, photo_id))
    except Exception as e:
        log.warning("thumb failed for %s: %s", f.master_path, e)

    counts.new += 1
    per_folder_prev[f.source_folder] = {
        "photo_id": photo_id, "aspect": back_aspect, "was_back": False,
    }


def _handle_back(
    *, settings, root, f, sha, width, height, back_feat,
    counts, job_run_id, per_folder_prev, front_photo_id: int | None,
    aspect_mismatch: bool = False,
) -> None:
    staging_wpath = paths.staging_working_path(settings, sha, f.ext)
    staging_tpath = paths.staging_thumb_path(settings, sha)
    _copy_master_to_working(f.master_path, staging_wpath)
    try:
        with open_image(f.master_path) as img:
            write_thumb(img, staging_tpath)
    except Exception as e:
        log.warning("staging thumb failed for %s: %s", f.master_path, e)

    with db.connection() as conn:
        conn.autocommit = False
        try:
            staging.stage_pairing(
                conn,
                front_photo_id=front_photo_id,
                back_master_path=str(f.master_path),
                back_sha256=sha,
                back_source_folder=f.source_folder,
                back_source_filename=f.source_filename,
                back_scan_sequence=f.scan_sequence,
                back_score=back_feat.score,
                staging_working_path=str(staging_wpath),
                staging_thumb_path=str(staging_tpath),
                back_aspect_mismatch=aspect_mismatch,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            _safe_unlink(staging_wpath)
            _safe_unlink(staging_tpath)
            raise

    counts.backs_proposed += 1
    per_folder_prev[f.source_folder] = {
        # After a back, the previous slot points at the back (was_back=True)
        # so we don't chain backs off backs.
        "photo_id": front_photo_id, "aspect": back_feat.aspect_ratio, "was_back": True,
    }


def _handle_rescan(
    *, settings, root, f, sha, phash, width, height, file_size, mime,
    rescan_hit, counts, job_run_id, per_folder_prev, back_aspect,
) -> None:
    existing_photo_id, distance = rescan_hit
    staging_wpath = paths.staging_working_path(settings, sha, f.ext)
    staging_tpath = paths.staging_thumb_path(settings, sha)
    _copy_master_to_working(f.master_path, staging_wpath)
    try:
        with open_image(f.master_path) as img:
            write_thumb(img, staging_tpath)
    except Exception as e:
        log.warning("staging thumb failed for %s: %s", f.master_path, e)

    with db.connection() as conn:
        conn.autocommit = False
        try:
            staging.stage_rescan(
                conn,
                existing_photo_id=existing_photo_id,
                new_master_path=str(f.master_path),
                new_sha256=sha,
                new_source_root=root.label,
                new_source_folder=f.source_folder,
                new_source_filename=f.source_filename,
                new_scan_batch=f.scan_batch,
                new_scan_sequence=f.scan_sequence,
                distance=distance,
                new_width=width, new_height=height,
                new_file_size=file_size, new_mime=mime,
                staging_working_path=str(staging_wpath),
                staging_thumb_path=str(staging_tpath),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            _safe_unlink(staging_wpath)
            _safe_unlink(staging_tpath)
            raise

    counts.rescans_proposed += 1
    # Advance previous-in-folder to this rescan candidate (not a back).
    per_folder_prev[f.source_folder] = {
        "photo_id": existing_photo_id, "aspect": back_aspect, "was_back": False,
    }


def _record_folder_hints(conn, *, root, f, photo_id, has_exif: bool) -> None:
    if root.kind == "digital":
        if not has_exif:
            hint = parse_date_folder(f.source_folder)
            if hint is not None:
                year, month = hint
                staging.insert_suggestion(
                    conn, kind="date", source="import", confidence=0.3,
                    photo_id=photo_id,
                    payload={
                        "date": f"{year:04d}-{month:02d}-01",
                        "precision": "month",
                        "evidence": f"export folder _{year:04d}-{month:02d}",
                    },
                )
    elif root.kind == "scan":
        top = f.source_folder.split("/", 1)[0] if f.source_folder else ""
        if top and not is_batch_folder(top):
            album_id = staging.ensure_album(conn, top)
            staging.add_photo_to_album(conn, album_id, photo_id)
            staging.insert_suggestion(
                conn, kind="description", source="import", confidence=0.5,
                photo_id=photo_id,
                payload={"text": top, "evidence": "scan folder name"},
            )


def _copy_master_to_working(master_path: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    # copy2 preserves mtime for the manifest cache; masters unchanged.
    shutil.copy2(master_path, dest)


def _safe_unlink(p: Path) -> None:
    try:
        if p.exists():
            p.unlink()
    except OSError:
        log.warning("could not remove %s", p)
