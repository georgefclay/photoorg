"""Accept, reject and undo one cleanup proposal.

Invariant 1: cleanup writes a new derived file and never overwrites. Accept
moves the current working copy into `WORKING_DIR/_versions/` (kept forever),
puts the render at the standard working name, bumps `file_version` so sync
re-pushes, and updates the dims, pHash/dHash and thumbnail.

Invariant 2: the same transform that moved the pixels moves every
`faces.bbox`. A box that no longer belongs in the frame is soft-deleted with
`delete_reason='cleanup_out_of_frame'`, never dropped.

A note on ordering. Triage's rule is "file moves happen AFTER the DB commit"
because a failed move there leaves a recoverable mismatch. Cleanup is the
other way round: a committed `file_version` bump whose bytes never arrived is
unrecoverable. So the moves happen inside the transaction, immediately before
commit, with an explicit compensation that puts the previous working file
back if the second move fails.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import psycopg

from ... import db
from ...config import Settings
from ..ingest.paths import resolve_working_path, thumb_path, working_path
from . import paths as cpaths
from . import render as render_mod
from . import repo
from .geometry import Transform

log = logging.getLogger(__name__)


class CleanupError(RuntimeError):
    pass


class StaleProposal(CleanupError):
    """The working file changed since the proposal was analysed."""


@dataclass
class AcceptResult:
    photo_id: int
    proposal_id: int
    ticked: tuple[str, ...]
    no_op: bool = False
    previous_version: int = 0
    new_version: int = 0
    width: int = 0
    height: int = 0
    faces_moved: int = 0
    faces_lost: list[int] = field(default_factory=list)
    version_file: str | None = None
    working_file: str | None = None

    def summary(self) -> str:
        if self.no_op:
            return f"photo {self.photo_id}: nothing ticked, no change"
        bits = [f"photo {self.photo_id}: v{self.previous_version} → v{self.new_version}",
                f"{self.width}×{self.height}",
                f"ops {', '.join(self.ticked) or 'none'}"]
        if self.faces_moved:
            bits.append(f"{self.faces_moved} face boxes moved")
        if self.faces_lost:
            bits.append(f"{len(self.faces_lost)} out of frame")
        return "; ".join(bits)


# --------------------------------------------------------------------------
# Accept
# --------------------------------------------------------------------------

def accept_proposal(
    settings: Settings,
    proposal_id: int,
    *,
    ticked: Iterable[str] | None = None,
    actor: str = "desktop",
) -> AcceptResult:
    """Apply the ticked ops to the working copy. Splits go through
    `split.accept_split` instead — they create photo rows."""
    with db.connection() as conn:
        conn.autocommit = True
        proposal = repo.load_proposal(conn, proposal_id)
    if proposal is None:
        raise CleanupError(f"proposal {proposal_id} not found")
    if proposal.status != "pending":
        raise CleanupError(
            f"proposal {proposal_id} is {proposal.status}, not pending"
        )
    if proposal.is_split:
        raise CleanupError(
            "split proposals are accepted through split.accept_split"
        )
    if "remote_enhance" in ((proposal.operations or {}).get("ops") or {}):
        from .remote_enhance import accept_remote
        return accept_remote(settings, proposal, actor=actor)

    ticked_set = tuple(sorted(
        ticked if ticked is not None
        else render_mod.default_ticked(proposal.operations)
    ))

    src = proposal.resolved_path(settings)
    if src is None or not src.exists():
        raise CleanupError(f"working file missing for photo {proposal.photo_id}: {src}")

    _check_source_dims(src, proposal)
    plan = render_mod.plan_from(
        proposal.operations, ticked_set, settings=settings,
    )
    if plan.is_noop:
        with db.connection() as conn:
            conn.autocommit = False
            try:
                repo.decide(conn, proposal_id, "accepted", actor=actor)
                db.audit(conn, actor=actor, action="cleanup.accept",
                         entity_type="photo", entity_id=proposal.photo_id,
                         previous_value=None,
                         new_value={"proposal_id": proposal_id,
                                    "ticked": list(ticked_set), "no_op": True})
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return AcceptResult(photo_id=proposal.photo_id, proposal_id=proposal_id,
                            ticked=ticked_set, no_op=True,
                            previous_version=proposal.file_version,
                            new_version=proposal.file_version)

    ext = cpaths.ext_for(proposal.mime, fallback=src.suffix.lstrip(".") or "jpg")
    new_version = int(proposal.file_version) + 1
    cpaths.ensure_dirs(settings)
    derived = cpaths.derived_path(settings, proposal.photo_id, new_version, ext)

    rendered = render_mod.render_full(
        src, plan, derived, mime=proposal.mime, operations=proposal.operations,
    )
    return install_new_version(
        settings, proposal, rendered,
        transform=plan.transform, ticked=ticked_set, src=src, ext=ext,
        actor=actor,
    )


def install_new_version(
    settings: Settings,
    proposal: repo.Proposal,
    rendered: render_mod.RenderResult,
    *,
    transform: Transform,
    ticked: tuple[str, ...],
    src: Path,
    ext: str,
    actor: str = "desktop",
) -> AcceptResult:
    """Put an already-rendered derivative in place as the new working copy.

    Shared by the ordinary accept path and the remote-enhance one: both end up
    with a file on disk plus the transform that produced it, and from there
    the version swap, the face boxes and the audit row are identical.
    """
    proposal_id = proposal.id
    new_version = int(proposal.file_version) + 1

    with db.connection() as conn:
        conn.autocommit = True
        faces = repo.faces_for(conn, proposal.photo_id)

    moved: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    lost: list[tuple[int, dict[str, Any]]] = []
    for f in faces:
        new_bbox = transform.apply_bbox(f.bbox)
        if new_bbox is None:
            lost.append((f.id, f.bbox))
        elif transform.is_identity:
            continue
        else:
            moved.append((f.id, f.bbox, new_bbox))

    derived = rendered.path
    plan_transform = transform
    version_target = cpaths.version_path(
        settings, proposal.photo_id, proposal.file_version, ext,
    )
    working_target = working_path(settings, proposal.photo_id, proposal.sha256, ext)

    previous = {
        "file_version": proposal.file_version,
        "width": proposal.width, "height": proposal.height,
        "working_path": str(src),
        "faces": [{"id": fid, "bbox": bbox} for fid, bbox, _ in moved]
                 + [{"id": fid, "bbox": bbox, "lost": True} for fid, bbox in lost],
    }
    new_value = {
        "proposal_id": proposal_id,
        "ticked": list(ticked),
        "file_version": new_version,
        "width": rendered.width, "height": rendered.height,
        "working_path": str(working_target),
        "version_file": str(version_target),
        "transform": plan_transform.to_json(),
        "faces_moved": [{"id": fid, "bbox": nb} for fid, _, nb in moved],
        "faces_lost": [fid for fid, _ in lost],
        "phash": rendered.phash, "dhash": rendered.dhash,
    }

    with db.connection() as conn:
        conn.autocommit = False
        try:
            prev_hashes = conn.execute(
                "select phash, dhash, file_size from photos where id = %s",
                (proposal.photo_id,),
            ).fetchone()
            previous["phash"] = prev_hashes[0] if prev_hashes else None
            previous["dhash"] = prev_hashes[1] if prev_hashes else None
            previous["file_size"] = prev_hashes[2] if prev_hashes else None

            conn.execute(
                """
                update photos
                   set width = %s, height = %s, file_size = %s,
                       phash = coalesce(%s, phash), dhash = coalesce(%s, dhash),
                       file_version = %s, working_path = %s
                 where id = %s
                """,
                (rendered.width, rendered.height, rendered.file_size,
                 rendered.phash, rendered.dhash, new_version,
                 str(working_target), proposal.photo_id),
            )
            for fid, _prev, nb in moved:
                conn.execute(
                    "update faces set bbox = %s::jsonb where id = %s",
                    (json.dumps(nb), fid),
                )
            for fid, _prev in lost:
                _soft_delete_face(conn, fid, reason="cleanup_out_of_frame",
                                  actor=actor)
            repo.decide(conn, proposal_id, "accepted", actor=actor)
            db.audit(conn, actor=actor, action="cleanup.accept",
                     entity_type="photo", entity_id=proposal.photo_id,
                     previous_value=previous, new_value=new_value)

            _swap_in_new_version(src, version_target, derived, working_target)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    _post_commit(settings, proposal.photo_id, rendered, moved,
                 working_target=working_target)

    return AcceptResult(
        photo_id=proposal.photo_id, proposal_id=proposal_id, ticked=ticked,
        previous_version=proposal.file_version, new_version=new_version,
        width=rendered.width, height=rendered.height,
        faces_moved=len(moved), faces_lost=[fid for fid, _ in lost],
        version_file=str(version_target), working_file=str(working_target),
    )


def _check_source_dims(src: Path, proposal: repo.Proposal) -> None:
    """The proposal's numbers are only valid for the file it was measured on.

    Cheap: PIL reads the header, not the pixels.
    """
    analysis = (proposal.operations or {}).get("analysis") or {}
    want_w, want_h = analysis.get("src_w"), analysis.get("src_h")
    if not want_w or not want_h:
        return
    from PIL import Image
    with Image.open(src) as im:
        w, h = im.size
        if (im.getexif().get(0x0112) or 1) in (5, 6, 7, 8):
            w, h = h, w
    if int(want_w) != w or int(want_h) != h:
        raise StaleProposal(
            f"photo {proposal.photo_id}: proposal measured {want_w}×{want_h}, "
            f"the working file is now {w}×{h}. Re-analyse it."
        )


def _swap_in_new_version(
    current: Path, version_target: Path, derived: Path, working_target: Path,
) -> None:
    """Move the current working copy aside, then the render into place.

    If the second move fails the first is undone, so the transaction can roll
    back to a consistent world. `current` and `working_target` are usually the
    same path (the sha in the name is the master's, which cleanup never
    changes) — the sequence handles both.
    """
    version_target.parent.mkdir(parents=True, exist_ok=True)
    working_target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(current, version_target)
    try:
        os.replace(derived, working_target)
    except OSError:
        try:
            os.replace(version_target, current)
        except OSError:
            log.exception(
                "cleanup: could not restore %s from %s after a failed swap; "
                "the previous version is still at the _versions path",
                current, version_target,
            )
        raise


def _soft_delete_face(
    conn: psycopg.Connection, face_id: int, *, reason: str, actor: str,
) -> None:
    prev = conn.execute(
        "select bbox, person_id from faces where id = %s", (face_id,),
    ).fetchone()
    conn.execute(
        """
        update faces
           set is_deleted = true, deleted_at = now(), delete_reason = %s
         where id = %s
        """,
        (reason, face_id),
    )
    db.audit(conn, actor=actor, action="face.delete",
             entity_type="face", entity_id=face_id,
             previous_value={"bbox": prev[0] if prev else None,
                             "person_id": prev[1] if prev else None},
             new_value={"is_deleted": True, "delete_reason": reason})


def _post_commit(
    settings: Settings,
    photo_id: int,
    rendered: render_mod.RenderResult,
    moved: Sequence[tuple[int, dict[str, Any], dict[str, Any]]],
    *,
    working_target: Path,
) -> None:
    """Thumbnail and face crops. Cosmetic — a failure here is logged, never
    fatal: the DB and the working file already agree."""
    try:
        if rendered.thumb_bytes:
            tp = thumb_path(settings, photo_id)
            tp.parent.mkdir(parents=True, exist_ok=True)
            tp.write_bytes(rendered.thumb_bytes)
    except OSError as e:
        log.warning("cleanup: thumbnail write failed for photo %s: %s", photo_id, e)

    if not moved:
        return
    try:
        from ...jobs.detect_faces import _write_face_crops
        crops_dir = settings.THUMBS_DIR / "faces"
        crops_dir.mkdir(parents=True, exist_ok=True)
        _write_face_crops(
            working_target, [(fid, nb) for fid, _prev, nb in moved], crops_dir,
        )
    except Exception as e:  # pragma: no cover — crops are best effort
        log.warning("cleanup: face-crop regeneration failed for photo %s: %s",
                    photo_id, e)


# --------------------------------------------------------------------------
# Reject → manual fix
# --------------------------------------------------------------------------

@dataclass
class RejectResult:
    photo_id: int
    proposal_id: int
    manual_path: str


def reject_proposal(
    settings: Settings, proposal_id: int, *, actor: str = "desktop",
    reason: str | None = None,
) -> RejectResult:
    """R: the automatic result is wrong. Copy the *current* working file to
    MANUAL_FIX_DIR and park the proposal as `manual`, so it leaves the queue
    without blocking it. The working copy is untouched."""
    with db.connection() as conn:
        conn.autocommit = True
        proposal = repo.load_proposal(conn, proposal_id)
    if proposal is None:
        raise CleanupError(f"proposal {proposal_id} not found")
    if proposal.status != "pending":
        raise CleanupError(f"proposal {proposal_id} is {proposal.status}, not pending")

    src = proposal.resolved_path(settings)
    if src is None or not src.exists():
        raise CleanupError(f"working file missing for photo {proposal.photo_id}")

    ext = cpaths.ext_for(proposal.mime, fallback=src.suffix.lstrip(".") or "jpg")
    target = cpaths.manual_fix_path(settings, proposal.photo_id, proposal.sha256, ext)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Copy, never move: the working copy stays the working copy.
    import shutil
    shutil.copy2(src, target)

    with db.connection() as conn:
        conn.autocommit = False
        try:
            repo.decide(conn, proposal_id, "manual", actor=actor)
            db.audit(conn, actor=actor, action="cleanup.reject",
                     entity_type="photo", entity_id=proposal.photo_id,
                     previous_value={"status": "pending"},
                     new_value={"proposal_id": proposal_id, "status": "manual",
                                "manual_path": str(target), "reason": reason})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return RejectResult(photo_id=proposal.photo_id, proposal_id=proposal_id,
                        manual_path=str(target))


# --------------------------------------------------------------------------
# Undo (session-scoped; the recipe for a cross-restart undo is in GC.md)
# --------------------------------------------------------------------------

@dataclass
class UndoResult:
    photo_id: int
    proposal_id: int
    restored_version: int
    new_version: int
    faces_restored: int
    faces_undeleted: int


def undo_accept(
    settings: Settings, photo_id: int, *, actor: str = "desktop",
) -> UndoResult:
    """Reverse the most recent `cleanup.accept` on this photo.

    The cleaned file is kept — it moves into `_versions/` under its own
    version number — and the previous one comes back to the working name with
    `file_version` bumped again, so sync sees a change and history is never
    rewritten. Face boxes are restored verbatim from the audit row (exact),
    which is better than re-deriving them through the inverse transform.
    """
    with db.connection() as conn:
        conn.autocommit = True
        row = conn.execute(
            """
            select id, previous_value, new_value from audit_log
             where action = 'cleanup.accept' and entity_type = 'photo'
               and entity_id = %s
             order by id desc limit 1
            """,
            (photo_id,),
        ).fetchone()
        if row is None:
            raise CleanupError(f"no cleanup.accept audit row for photo {photo_id}")
        audit_id, prev, new = row
        prev = _json(prev) or {}
        new = _json(new) or {}
        photo = conn.execute(
            "select file_version, working_path, mime, sha256 from photos where id = %s",
            (photo_id,),
        ).fetchone()
    if photo is None:
        raise CleanupError(f"photo {photo_id} not found")
    if new.get("no_op"):
        # Nothing to reverse but the decision itself.
        return _undo_no_op(settings, photo_id, new, actor=actor)

    current_version = int(photo[0])
    current_path = resolve_working_path(settings.WORKING_DIR, photo[1])
    mime = photo[2]
    sha = photo[3]
    ext = cpaths.ext_for(mime, fallback=(current_path.suffix.lstrip(".")
                                         if current_path else "jpg"))

    restore_from = Path(new.get("version_file") or "")
    if not restore_from.exists():
        restore_from = cpaths.version_path(
            settings, photo_id, int(prev.get("file_version") or 1), ext,
        )
    if not restore_from.exists():
        raise CleanupError(
            f"cannot undo photo {photo_id}: previous version file missing "
            f"({restore_from})"
        )

    undo_version = current_version + 1
    keep_cleaned = cpaths.version_path(settings, photo_id, current_version, ext)
    target = working_path(settings, photo_id, sha, ext)

    with db.connection() as conn:
        conn.autocommit = False
        try:
            conn.execute(
                """
                update photos
                   set width = %s, height = %s, file_size = %s,
                       phash = %s, dhash = %s,
                       file_version = %s, working_path = %s
                 where id = %s
                """,
                (prev.get("width"), prev.get("height"), prev.get("file_size"),
                 prev.get("phash"), prev.get("dhash"), undo_version,
                 str(target), photo_id),
            )
            restored = 0
            undeleted = 0
            for entry in prev.get("faces") or []:
                fid = entry.get("id")
                bbox = entry.get("bbox")
                if fid is None or bbox is None:
                    continue
                conn.execute(
                    "update faces set bbox = %s::jsonb where id = %s",
                    (json.dumps(bbox), fid),
                )
                restored += 1
                if entry.get("lost"):
                    conn.execute(
                        """
                        update faces
                           set is_deleted = false, deleted_at = null,
                               delete_reason = null
                         where id = %s and delete_reason = 'cleanup_out_of_frame'
                        """,
                        (fid,),
                    )
                    undeleted += 1

            proposal_id = int(new.get("proposal_id") or 0)
            if proposal_id:
                repo.decide(conn, proposal_id, "superseded", actor=actor)
            db.audit(conn, actor=actor, action="cleanup.undo",
                     entity_type="photo", entity_id=photo_id,
                     previous_value={"file_version": current_version,
                                     "reversed_audit_id": audit_id},
                     new_value={"file_version": undo_version,
                                "restored_from": str(restore_from),
                                "cleaned_kept_at": str(keep_cleaned),
                                "faces_restored": restored,
                                "faces_undeleted": undeleted,
                                "proposal_id": proposal_id or None})

            if current_path and current_path.exists():
                keep_cleaned.parent.mkdir(parents=True, exist_ok=True)
                os.replace(current_path, keep_cleaned)
            try:
                os.replace(restore_from, target)
            except OSError:
                if keep_cleaned.exists() and current_path:
                    os.replace(keep_cleaned, current_path)
                raise
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    _regenerate_after_undo(settings, photo_id, target,
                           prev.get("faces") or [])
    return UndoResult(photo_id=photo_id, proposal_id=int(new.get("proposal_id") or 0),
                      restored_version=int(prev.get("file_version") or 0),
                      new_version=undo_version,
                      faces_restored=restored, faces_undeleted=undeleted)


def _undo_no_op(
    settings: Settings, photo_id: int, new: dict[str, Any], *, actor: str,
) -> UndoResult:
    proposal_id = int(new.get("proposal_id") or 0)
    with db.connection() as conn:
        conn.autocommit = False
        try:
            if proposal_id:
                repo.decide(conn, proposal_id, "superseded", actor=actor)
            db.audit(conn, actor=actor, action="cleanup.undo",
                     entity_type="photo", entity_id=photo_id,
                     previous_value={"no_op": True},
                     new_value={"proposal_id": proposal_id or None,
                                "no_op": True})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return UndoResult(photo_id=photo_id, proposal_id=proposal_id,
                      restored_version=0, new_version=0,
                      faces_restored=0, faces_undeleted=0)


def _regenerate_after_undo(
    settings: Settings, photo_id: int, target: Path,
    faces: Sequence[dict[str, Any]],
) -> None:
    try:
        from PIL import Image
        from ..ingest.thumbs import write_thumb
        with Image.open(target) as im:
            write_thumb(im, thumb_path(settings, photo_id))
    except Exception as e:
        log.warning("cleanup: thumbnail regeneration failed after undo on %s: %s",
                    photo_id, e)
    try:
        from ...jobs.detect_faces import _write_face_crops
        pairs = [(int(e["id"]), e["bbox"]) for e in faces
                 if e.get("id") is not None and e.get("bbox")]
        if pairs:
            crops_dir = settings.THUMBS_DIR / "faces"
            crops_dir.mkdir(parents=True, exist_ok=True)
            _write_face_crops(target, pairs, crops_dir)
    except Exception as e:  # pragma: no cover
        log.warning("cleanup: face-crop regeneration failed after undo on %s: %s",
                    photo_id, e)


def _json(v: Any) -> dict[str, Any] | None:
    if v is None:
        return None
    if isinstance(v, str):
        return json.loads(v)
    return dict(v)
