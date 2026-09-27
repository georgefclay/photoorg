"""Split one flatbed scan of several prints into one photo row per print.

Each region becomes its own `photos` row with its own `photo_masters` row
pointing at the *same* master file, carrying the `region` it came from. The
parent goes through the normal triage transition to `junk` with hint
`split_parent` — quarantined and restorable, never deleted.

Child identity (SCHEMA.md): `photos.sha256` =
sha256(master_sha256 + ':' + region_key). It is an identity key, not a file
hash — the child has no master file of its own. The parent keeps the real
`source_filename`, so re-ingesting the master stays a no-op; children take
`<parent filename>#pN`.

Splits are never bulk-accepted (Phase 7 answer 8): they create rows and junk
a photo, so every one goes through the eye.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from ... import db
from ...config import Settings
from ..ingest.paths import thumb_path, working_path
from ..triage import decisions as triage
from . import paths as cpaths
from . import render as render_mod
from . import repo
from .accept import CleanupError, _soft_delete_face
from .geometry import Rect, Transform, bbox_overlap_frac

log = logging.getLogger(__name__)

# A face must sit this much inside a region to belong to it…
REGION_CLAIM_MIN = 0.5
# …and if a second region also holds this much of it, it straddles two prints
# and is soft-deleted rather than guessed at.
REGION_STRADDLE_MIN = 0.2


@dataclass
class ChildResult:
    index: int
    photo_id: int
    width: int
    height: int
    working_path: str
    faces: list[int] = field(default_factory=list)


@dataclass
class SplitResult:
    parent_photo_id: int
    proposal_id: int
    children: list[ChildResult] = field(default_factory=list)
    faces_lost: list[int] = field(default_factory=list)
    albums_copied: int = 0
    groups_copied: int = 0
    parent_status: str = "junk"

    def summary(self) -> str:
        kids = ", ".join(f"#{c.photo_id} ({c.width}×{c.height})"
                         for c in self.children)
        bits = [f"photo {self.parent_photo_id} split into {len(self.children)}: {kids}"]
        if self.faces_lost:
            bits.append(f"{len(self.faces_lost)} face boxes straddled a cut")
        return "; ".join(bits)


def child_region_key(rect: Rect) -> str:
    """'x,y,w,h' of the region's axis-aligned bounds, in the photo's display
    frame at full resolution. Part of the `photo_masters` uniqueness key."""
    x, y, w, h = rect.axis_aligned_bounds()
    return f"{int(round(x))},{int(round(y))},{int(round(w))},{int(round(h))}"


def child_sha256(master_sha: str, region_key: str) -> str:
    return hashlib.sha256(f"{master_sha}:{region_key}".encode()).hexdigest()


def _assign_faces(
    faces: list[repo.FaceRow], regions: list[dict[str, Any]],
) -> tuple[dict[int, list[tuple[repo.FaceRow, dict[str, Any]]]], list[int]]:
    """Which region owns which face, and which faces are lost to a cut."""
    by_region: dict[int, list[tuple[repo.FaceRow, dict[str, Any]]]] = {
        int(r["index"]): [] for r in regions
    }
    lost: list[int] = []
    rects = {int(r["index"]): Rect.from_json(r["rect"]) for r in regions}
    transforms = {int(r["index"]): Transform.from_json(r["transform"])
                  for r in regions}

    for f in faces:
        overlaps = sorted(
            ((bbox_overlap_frac(f.bbox, rect), idx) for idx, rect in rects.items()),
            reverse=True,
        )
        best_frac, best_idx = overlaps[0]
        second_frac = overlaps[1][0] if len(overlaps) > 1 else 0.0
        if best_frac < REGION_CLAIM_MIN or second_frac >= REGION_STRADDLE_MIN:
            lost.append(f.id)
            continue
        new_bbox = transforms[best_idx].apply_bbox(f.bbox)
        if new_bbox is None:
            lost.append(f.id)
            continue
        by_region[best_idx].append((f, new_bbox))
    return by_region, lost


def accept_split(
    settings: Settings, proposal_id: int, *, actor: str = "desktop",
) -> SplitResult:
    with db.connection() as conn:
        conn.autocommit = True
        proposal = repo.load_proposal(conn, proposal_id)
        if proposal is None:
            raise CleanupError(f"proposal {proposal_id} not found")
        if proposal.status != "pending":
            raise CleanupError(f"proposal {proposal_id} is {proposal.status}")
        if not proposal.split_regions:
            raise CleanupError(f"proposal {proposal_id} is not a split")
        parent = _load_parent(conn, proposal.photo_id)
        master = _preferred_master(conn, proposal.photo_id)
        faces = repo.faces_for(conn, proposal.photo_id)
        had_detect_faces = _job_done(conn, proposal.photo_id, "detect_faces")

    if master is None:
        raise CleanupError(
            f"photo {proposal.photo_id} has no preferred photo_masters row"
        )
    src = proposal.resolved_path(settings)
    if src is None or not src.exists():
        raise CleanupError(f"working file missing for photo {proposal.photo_id}")

    regions = sorted(proposal.split_regions, key=lambda r: int(r["index"]))
    by_region, lost = _assign_faces(faces, regions)

    ext = cpaths.ext_for(proposal.mime, fallback=src.suffix.lstrip(".") or "jpg")
    cpaths.ensure_dirs(settings)
    staging = settings.CLEANUP_DIR / "_split"
    staging.mkdir(parents=True, exist_ok=True)

    # Render every region before touching the database, one at a time.
    renders: dict[int, render_mod.RenderResult] = {}
    for r in regions:
        idx = int(r["index"])
        plan = render_mod.Plan(transform=Transform.from_json(r["transform"]))
        out = staging / f"{proposal.photo_id:08d}_r{idx}.{ext}"
        renders[idx] = render_mod.render_full(
            src, plan, out, mime=proposal.mime, operations=proposal.operations,
        )

    result = SplitResult(parent_photo_id=proposal.photo_id, proposal_id=proposal_id,
                         faces_lost=lost)
    total = len(regions)
    moves: list[tuple[Path, Path]] = []

    with db.connection() as conn:
        conn.autocommit = False
        try:
            for r in regions:
                idx = int(r["index"])
                rect = Rect.from_json(r["rect"])
                rendered = renders[idx]
                region_key = child_region_key(rect)
                sha = child_sha256(master["sha256"], region_key)
                child_id = _insert_child(
                    conn, parent=parent, index=idx, total=total,
                    sha256=sha, rendered=rendered,
                )
                target = working_path(settings, child_id, sha, ext)
                conn.execute(
                    "update photos set working_path = %s where id = %s",
                    (str(target), child_id),
                )
                _insert_child_master(
                    conn, child_id=child_id, master=master, rect=rect,
                    region_key=region_key, sha256=sha, rendered=rendered,
                )
                result.albums_copied += _copy_albums(conn, parent["id"], child_id)
                result.groups_copied += _copy_groups(conn, parent["id"], child_id)
                if had_detect_faces and by_region[idx]:
                    # Answer 10: the faces came across with the pixels, so the
                    # next jobs run must not detect them a second time.
                    _mark_job_done(conn, child_id, "detect_faces")

                face_ids = []
                for face, new_bbox in by_region[idx]:
                    conn.execute(
                        """
                        update faces set photo_id = %s, bbox = %s::jsonb
                         where id = %s
                        """,
                        (child_id, json.dumps(new_bbox), face.id),
                    )
                    face_ids.append(face.id)
                conn.execute("select refresh_completeness(%s)", (child_id,))

                db.audit(conn, actor=actor, action="cleanup.split",
                         entity_type="photo", entity_id=child_id,
                         previous_value={"parent_photo_id": parent["id"]},
                         new_value={
                             "proposal_id": proposal_id,
                             "index": idx, "of": total,
                             "region": {"frame": "display", "key": region_key,
                                        **rect.to_json()},
                             "sha256": sha,
                             "width": rendered.width, "height": rendered.height,
                             "faces": [{"id": f.id, "bbox": bb}
                                       for f, bb in by_region[idx]],
                             "faces_previous": [{"id": f.id, "bbox": f.bbox}
                                                for f, _ in by_region[idx]],
                             "working_path": str(target),
                         })
                result.children.append(ChildResult(
                    index=idx, photo_id=child_id,
                    width=rendered.width, height=rendered.height,
                    working_path=str(target), faces=face_ids,
                ))
                moves.append((rendered.path, target))

            for fid in lost:
                _soft_delete_face(conn, fid, reason="cleanup_out_of_frame",
                                  actor=actor)

            repo.decide(conn, proposal_id, "accepted", actor=actor)
            db.audit(conn, actor=actor, action="cleanup.split",
                     entity_type="photo", entity_id=parent["id"],
                     previous_value={"triage_status": parent["triage_status"],
                                     "faces": [{"id": f.id, "bbox": f.bbox}
                                               for f in faces]},
                     new_value={"proposal_id": proposal_id,
                                "children": [c.photo_id for c in result.children],
                                "faces_lost": lost,
                                "albums_copied": result.albums_copied,
                                "groups_copied": result.groups_copied})

            _move_all(moves)
            conn.commit()
        except Exception:
            conn.rollback()
            _undo_moves(moves)
            raise

    # The parent leaves through the front door: a normal triage transition, so
    # it lands in quarantine with an audit row and can be restored.
    try:
        triage.apply_decision(settings, parent["id"], "junk",
                              hint="split_parent", actor=actor)
    except Exception as e:
        result.parent_status = f"junk failed: {e}"
        log.exception(
            "cleanup: split children created for photo %s but junking the "
            "parent failed; reconcile in the quarantine browser", parent["id"],
        )

    _write_child_thumbs(settings, result, renders)
    return result


# --------------------------------------------------------------------------
# Undo
# --------------------------------------------------------------------------

@dataclass
class SplitUndoResult:
    parent_photo_id: int
    children: list[int] = field(default_factory=list)
    faces_returned: int = 0
    parent_status: str = ""


def undo_split(
    settings: Settings, parent_photo_id: int, *, actor: str = "desktop",
) -> SplitUndoResult:
    """Reverse a split: faces go back to the parent with their original
    boxes, the children are soft-deleted (never removed — no real deletes),
    and the parent comes back out of quarantine.
    """
    with db.connection() as conn:
        conn.autocommit = True
        rows = conn.execute(
            """
            select id, previous_value, new_value from audit_log
             where action = 'cleanup.split' and entity_type = 'photo'
               and entity_id = %s
             order by id desc limit 1
            """,
            (parent_photo_id,),
        ).fetchall()
        if not rows:
            raise CleanupError(f"no cleanup.split audit row for photo {parent_photo_id}")
        _aid, prev, new = rows[0]
        prev = _json(prev) or {}
        new = _json(new) or {}
        child_audits = []
        for cid in new.get("children") or []:
            r = conn.execute(
                """
                select new_value from audit_log
                 where action = 'cleanup.split' and entity_type = 'photo'
                   and entity_id = %s
                 order by id desc limit 1
                """,
                (cid,),
            ).fetchone()
            if r:
                child_audits.append((cid, _json(r[0]) or {}))

    result = SplitUndoResult(parent_photo_id=parent_photo_id,
                             children=[c for c, _ in child_audits])
    prior_status = prev.get("triage_status") or "keep"

    with db.connection() as conn:
        conn.autocommit = False
        try:
            for cid, payload in child_audits:
                for entry in payload.get("faces_previous") or []:
                    fid, bbox = entry.get("id"), entry.get("bbox")
                    if fid is None or bbox is None:
                        continue
                    conn.execute(
                        """
                        update faces
                           set photo_id = %s, bbox = %s::jsonb
                         where id = %s
                        """,
                        (parent_photo_id, json.dumps(bbox), fid),
                    )
                    result.faces_returned += 1
                conn.execute(
                    """
                    update photos
                       set is_deleted = true, deleted_at = now(),
                           triage_status = 'junk', working_path = null
                     where id = %s
                    """,
                    (cid,),
                )
                db.audit(conn, actor=actor, action="cleanup.undo",
                         entity_type="photo", entity_id=cid,
                         previous_value={"parent_photo_id": parent_photo_id},
                         new_value={"reversed": "split", "is_deleted": True,
                                    "delete_reason": "cleanup_split_undo"})
            for fid in new.get("faces_lost") or []:
                conn.execute(
                    """
                    update faces
                       set is_deleted = false, deleted_at = null, delete_reason = null
                     where id = %s and delete_reason = 'cleanup_out_of_frame'
                    """,
                    (fid,),
                )
            proposal_id = new.get("proposal_id")
            if proposal_id:
                repo.decide(conn, int(proposal_id), "superseded", actor=actor)
            conn.execute("select refresh_completeness(%s)", (parent_photo_id,))
            db.audit(conn, actor=actor, action="cleanup.undo",
                     entity_type="photo", entity_id=parent_photo_id,
                     previous_value={"children": result.children},
                     new_value={"reversed": "split",
                               "faces_returned": result.faces_returned,
                               "restored_status": prior_status})
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    try:
        triage.apply_decision(settings, parent_photo_id, prior_status,
                              hint="split_parent_undo", actor=actor)
        result.parent_status = prior_status
    except Exception as e:
        result.parent_status = f"restore failed: {e}"
        log.exception("cleanup: could not restore split parent %s", parent_photo_id)
    return result


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _load_parent(conn: psycopg.Connection, photo_id: int) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            select id, sha256, mime, source_root, source_folder, source_filename,
                   scan_batch, scan_sequence, physical_ref_note,
                   triage_status::text as triage_status, is_private, is_scan,
                   has_no_people, capture_date, capture_date_precision::text
                     as capture_date_precision, capture_date_confirmed,
                   exif_taken_at, exif_camera, exif_gps_lat, exif_gps_lon,
                   rescan_wanted
              from photos where id = %s
            """,
            (photo_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise CleanupError(f"photo {photo_id} not found")
    return row


def _preferred_master(conn: psycopg.Connection, photo_id: int) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            select id, master_path, sha256, width, height, dpi, mime, file_size
              from photo_masters
             where photo_id = %s
             order by is_preferred desc, id
             limit 1
            """,
            (photo_id,),
        )
        return cur.fetchone()


def _insert_child(
    conn: psycopg.Connection,
    *,
    parent: dict[str, Any],
    index: int,
    total: int,
    sha256: str,
    rendered: render_mod.RenderResult,
) -> int:
    note_parts = [f"print {index} of {total} on this scan"]
    if parent.get("physical_ref_note"):
        note_parts.append(str(parent["physical_ref_note"]))
    row = conn.execute(
        """
        insert into photos
          (sha256, phash, dhash, width, height, mime, file_size,
           is_scan, has_no_people,
           capture_date, capture_date_precision, capture_date_confirmed,
           exif_taken_at, exif_camera, exif_gps_lat, exif_gps_lon,
           source_root, source_folder, source_filename,
           scan_batch, scan_sequence, physical_ref_note,
           rescan_wanted, triage_status, is_private, parent_photo_id,
           file_version)
        values
          (%s, %s, %s, %s, %s, %s, %s,
           true, %s,
           %s, %s, %s,
           %s, %s, %s, %s,
           %s, %s, %s,
           %s, %s, %s,
           %s, %s, %s, %s,
           1)
        returning id
        """,
        (sha256, rendered.phash, rendered.dhash, rendered.width, rendered.height,
         parent["mime"], rendered.file_size,
         parent["has_no_people"],
         parent["capture_date"], parent["capture_date_precision"],
         parent["capture_date_confirmed"],
         parent["exif_taken_at"], parent["exif_camera"],
         parent["exif_gps_lat"], parent["exif_gps_lon"],
         parent["source_root"], parent["source_folder"],
         f"{parent['source_filename']}#p{index}",
         parent["scan_batch"], parent["scan_sequence"], " | ".join(note_parts),
         parent["rescan_wanted"], parent["triage_status"], parent["is_private"],
         parent["id"]),
    ).fetchone()
    return int(row[0])


def _insert_child_master(
    conn: psycopg.Connection,
    *,
    child_id: int,
    master: dict[str, Any],
    rect: Rect,
    region_key: str,
    sha256: str,
    rendered: render_mod.RenderResult,
) -> None:
    region = {"frame": "display", "key": region_key, **rect.to_json()}
    conn.execute(
        """
        insert into photo_masters
          (photo_id, master_path, sha256, width, height, dpi, mime,
           file_size, is_preferred, region, region_key)
        values (%s, %s, %s, %s, %s, %s, %s, null, true, %s::jsonb, %s)
        """,
        (child_id, master["master_path"], sha256,
         rendered.width, rendered.height, master["dpi"], master["mime"],
         json.dumps(region), region_key),
    )


def _copy_albums(conn: psycopg.Connection, parent_id: int, child_id: int) -> int:
    cur = conn.execute(
        """
        insert into album_photos (album_id, photo_id, position)
        select album_id, %s, position from album_photos where photo_id = %s
        on conflict do nothing
        """,
        (child_id, parent_id),
    )
    return cur.rowcount or 0


def _copy_groups(conn: psycopg.Connection, parent_id: int, child_id: int) -> int:
    """Live group memberships come across too. Group membership is what makes
    a photo visible on the web, so dropping it would quietly hide the print
    from the family the parent was shared with."""
    cur = conn.execute(
        """
        insert into photo_groups (photo_id, group_id, added_by)
        select %s, group_id, added_by
          from photo_groups where photo_id = %s and not is_deleted
        on conflict do nothing
        """,
        (child_id, parent_id),
    )
    return cur.rowcount or 0


def _job_done(conn: psycopg.Connection, photo_id: int, job_name: str) -> bool:
    row = conn.execute(
        """
        select 1 from photo_job_status
         where photo_id = %s and job_name = %s and status = 'done'
        """,
        (photo_id, job_name),
    ).fetchone()
    return row is not None


def _mark_job_done(conn: psycopg.Connection, photo_id: int, job_name: str) -> None:
    conn.execute(
        """
        insert into photo_job_status (photo_id, job_name, status, completed_at)
        values (%s, %s, 'done', now())
        on conflict (photo_id, job_name) do update
          set status = 'done', completed_at = now()
        """,
        (photo_id, job_name),
    )


def _move_all(moves: list[tuple[Path, Path]]) -> None:
    done: list[tuple[Path, Path]] = []
    for src, dst in moves:
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(src, dst)
        except OSError:
            for s, d in reversed(done):
                try:
                    os.replace(d, s)
                except OSError:
                    log.exception("cleanup: could not roll back split move %s", d)
            raise
        done.append((src, dst))


def _undo_moves(moves: list[tuple[Path, Path]]) -> None:
    for src, dst in reversed(moves):
        if dst.exists() and not src.exists():
            try:
                os.replace(dst, src)
            except OSError:
                log.exception("cleanup: could not undo split move %s", dst)


def _write_child_thumbs(
    settings: Settings, result: SplitResult,
    renders: dict[int, render_mod.RenderResult],
) -> None:
    for child in result.children:
        rendered = renders.get(child.index)
        if rendered is None or not rendered.thumb_bytes:
            continue
        try:
            tp = thumb_path(settings, child.photo_id)
            tp.parent.mkdir(parents=True, exist_ok=True)
            tp.write_bytes(rendered.thumb_bytes)
        except OSError as e:
            log.warning("cleanup: thumbnail write failed for split child %s: %s",
                        child.photo_id, e)
    try:
        from ...jobs.detect_faces import _write_face_crops
        crops_dir = settings.THUMBS_DIR / "faces"
        crops_dir.mkdir(parents=True, exist_ok=True)
        with db.connection() as conn:
            conn.autocommit = True
            for child in result.children:
                if not child.faces:
                    continue
                faces = repo.faces_for(conn, child.photo_id)
                _write_face_crops(
                    Path(child.working_path),
                    [(f.id, f.bbox) for f in faces], crops_dir,
                )
    except Exception as e:  # pragma: no cover
        log.warning("cleanup: split face-crop regeneration failed: %s", e)


def _json(v: Any) -> dict[str, Any] | None:
    if v is None:
        return None
    if isinstance(v, str):
        return json.loads(v)
    return dict(v)
