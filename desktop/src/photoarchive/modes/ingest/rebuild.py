"""Rebuild back proposals under the corrected scorer.

Re-scores every file in scan-kind roots — both currently-committed `photos`
rows AND the backs already held in `ingest_pairings`. Never touches
accepted/rejected pairings or rescan rows.

Outcomes:
  1. Pending pairing whose staged back no longer qualifies:
     - drop the pairing (mark rejected with a synthetic note) and insert the
       held file as a normal `photos` row via the existing reject-path.
  2. Pending pairing whose staged back still qualifies:
     - update `back_score` in place; also re-verify the front is still a
       non-back (predecessor with new_score < 0.6).
  3. Committed scan photo that now qualifies as a back and was not previously
     rejected as a back:
     - insert a new pending pairing with `back_photo_id` set. The proposal's
       "back file" is the photo's existing working copy — no _staging copy is
       written, so `staging_working_path`/`staging_thumb_path` point at the
       real working paths.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from PIL import Image

from ... import db
from ...config import MasterRoot, Settings
from ...workers import CancelToken, Cancelled
from . import back_detect, paths
from .image_io import open_image
from .scanner import ScannedFile, walk_root

log = logging.getLogger(__name__)

BACK_SCORE_THRESHOLD = 0.6


@dataclass
class RebuildSummary:
    scan_files_scored: int = 0
    pending_kept: int = 0
    pending_updated_front: int = 0
    pending_dropped: int = 0
    photo_as_back_proposed: int = 0
    photo_as_back_skipped_rejected: int = 0
    photo_as_back_skipped_no_predecessor: int = 0
    photo_as_back_skipped_predecessor_is_back: int = 0
    photo_as_back_skipped_aspect: int = 0
    total_pending_after: int = 0
    histogram: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            **{k: v for k, v in self.__dict__.items() if k != "histogram"},
            "histogram": self.histogram,
        }


@dataclass
class _Scored:
    photo_id: int
    score: float
    aspect: float
    working_path: str
    source_folder: str
    scan_sequence: int | None


def rebuild_back_proposals(
    *,
    settings: Settings,
    roots: Sequence[MasterRoot],
    progress_cb: Callable[[dict], None] = lambda _: None,
    cancel_token: CancelToken | None = None,
) -> RebuildSummary:
    ctok = cancel_token or CancelToken()
    summary = RebuildSummary()

    scan_roots = [r for r in roots if r.kind == "scan"]
    if not scan_roots:
        return summary

    # --- Step 1: score every committed scan photo -----------------------
    scored_by_id: dict[int, _Scored] = {}
    with db.connection() as conn:
        conn.autocommit = True
        rows = conn.execute(
            """
            select id, working_path, source_root, source_folder,
                   source_filename, scan_sequence
            from photos
            where source_root = ANY(%s)
              and not is_deleted
              and working_path is not null
            order by source_root, source_folder, scan_sequence nulls last, id
            """,
            ([r.label for r in scan_roots],),
        ).fetchall()

    for row in rows:
        if ctok.is_set():
            raise Cancelled()
        pid, wpath, sroot, sfolder, sname, sseq = row
        p = Path(wpath) if wpath else None
        if p is None or not p.exists():
            log.warning("rebuild: working file missing for photo %d: %s", pid, wpath)
            continue
        try:
            with open_image(p) as img:
                img.load()
                feats = back_detect.analyse(img)
        except Exception as e:
            log.warning("rebuild: analyse failed for photo %d (%s): %s", pid, p, e)
            continue
        scored_by_id[pid] = _Scored(
            photo_id=pid, score=feats.score, aspect=feats.aspect_ratio,
            working_path=wpath, source_folder=sfolder, scan_sequence=sseq,
        )
        summary.scan_files_scored += 1
        if summary.scan_files_scored % 200 == 0:
            progress_cb({"kind": "scored", "n": summary.scan_files_scored})

    # Index by (source_folder, scan_sequence) so we can look up predecessors.
    by_folder: dict[str, list[_Scored]] = defaultdict(list)
    for s in scored_by_id.values():
        by_folder[s.source_folder].append(s)
    for folder in by_folder:
        by_folder[folder].sort(key=lambda s: (s.scan_sequence or 0, s.photo_id))

    def predecessor(s: _Scored) -> _Scored | None:
        lst = by_folder.get(s.source_folder, [])
        for i, item in enumerate(lst):
            if item.photo_id == s.photo_id:
                return lst[i - 1] if i > 0 else None
        return None

    # --- Step 2: handle pending pairings ---------------------------------
    from . import decisions  # local import to avoid circular init

    with db.connection() as conn:
        conn.autocommit = True
        pending_rows = conn.execute(
            """
            select id, front_photo_id, back_master_path, back_sha256,
                   back_source_folder, back_source_filename, back_scan_sequence,
                   staging_working_path, back_photo_id
            from ingest_pairings
            where status = 'pending'
            order by id
            """
        ).fetchall()

    for row in pending_rows:
        if ctok.is_set():
            raise Cancelled()
        (pair_id, front_id, back_master, back_sha, folder, name, seq,
         staging_wpath, back_pid) = row

        # Re-score the back. For held backs, that's the staging file.
        # For photo-as-back pairings (already the result of a previous
        # rebuild), that's the photo's current working copy.
        if back_pid is not None and back_pid in scored_by_id:
            new_score = scored_by_id[back_pid].score
            new_aspect = scored_by_id[back_pid].aspect
        else:
            p = Path(staging_wpath)
            if not p.exists():
                log.warning("rebuild: staged back missing at %s", p)
                continue
            try:
                with open_image(p) as img:
                    img.load()
                    feats = back_detect.analyse(img)
            except Exception as e:
                log.warning("rebuild: analyse failed for pairing %d: %s", pair_id, e)
                continue
            new_score = feats.score
            new_aspect = feats.aspect_ratio

        if new_score < BACK_SCORE_THRESHOLD:
            # Drop: reject the held back via the existing normal-photo path.
            # For photo-as-back pairings, "reject" only marks status; the
            # photo stays as it was.
            try:
                if back_pid is None:
                    decisions.reject_pairing(settings, pair_id)
                else:
                    with db.connection() as conn:
                        conn.autocommit = True
                        conn.execute(
                            """
                            update ingest_pairings
                            set status = 'rejected', decided_at = now()
                            where id = %s
                            """,
                            (pair_id,),
                        )
                        db.audit(
                            conn, actor="desktop", action="rebuild.pairing.dropped",
                            entity_type="ingest_pairing", entity_id=pair_id,
                            new_value={"new_score": new_score},
                        )
                summary.pending_dropped += 1
            except Exception as e:
                log.exception("rebuild: could not drop pairing %d: %s", pair_id, e)
            continue

        # Still qualifies. Confirm front is still a non-back and aspect_close.
        front_scored = scored_by_id.get(front_id)
        if front_scored is None:
            log.warning("rebuild: pairing %d front photo %d not in scored set", pair_id, front_id)
            continue

        # For held backs the front's predecessor logic doesn't apply — the
        # front is a committed photo. We just want the front NOT to be a
        # back itself, and aspects reasonably close.
        aspects_ok = back_detect.aspect_close(front_scored.aspect, new_aspect)
        if front_scored.score >= BACK_SCORE_THRESHOLD or not aspects_ok:
            # Front is now a probable back OR aspect mismatch → recover the
            # back through the normal path (held staging) or drop the
            # proposal (photo-as-back).
            try:
                if back_pid is None:
                    decisions.reject_pairing(settings, pair_id)
                else:
                    with db.connection() as conn:
                        conn.autocommit = True
                        conn.execute(
                            """
                            update ingest_pairings
                            set status = 'rejected', decided_at = now()
                            where id = %s
                            """,
                            (pair_id,),
                        )
                summary.pending_dropped += 1
            except Exception as e:
                log.exception("rebuild: could not drop pairing %d: %s", pair_id, e)
            continue

        # Just refresh the score.
        try:
            with db.connection() as conn:
                conn.autocommit = True
                conn.execute(
                    "update ingest_pairings set back_score = %s where id = %s",
                    (new_score, pair_id),
                )
            summary.pending_kept += 1
        except Exception as e:
            log.exception("rebuild: could not update pairing %d: %s", pair_id, e)

    # --- Step 3: propose new photo-as-back pairings ---------------------
    # A committed photo qualifies now if:
    #   - its new_score >= 0.6
    #   - it's not the front of an ACCEPTED pairing (a real front has an
    #     accepted back — we're not going to demote it)
    #   - it's not already deleted
    #   - there's no rejected pairing whose back_photo_id equals this photo
    #     (that would be re-proposing something George already said no to)
    #   - there's no pending pairing already for it as a back (unique index
    #     enforces this too, but check to avoid a wasted insert)
    #   - it has a predecessor in the same folder whose new_score < 0.6
    #   - aspect_close(predecessor, this)
    with db.connection() as conn:
        conn.autocommit = True
        accepted_fronts = {
            row[0] for row in conn.execute(
                "select distinct front_photo_id from ingest_pairings where status = 'accepted'"
            ).fetchall()
        }
        already_pending_as_back = {
            row[0] for row in conn.execute(
                "select back_photo_id from ingest_pairings where status = 'pending' and back_photo_id is not null"
            ).fetchall()
        }
        already_rejected_as_back = {
            row[0] for row in conn.execute(
                "select back_photo_id from ingest_pairings where status = 'rejected' and back_photo_id is not null"
            ).fetchall()
        }
        # Photo metadata we need to insert new pairings.
        meta_rows = conn.execute(
            """
            select p.id, p.source_folder, p.source_filename, p.scan_sequence,
                   pm.master_path, pm.sha256, p.working_path
            from photos p
            join photo_masters pm on pm.photo_id = p.id and pm.is_preferred
            where p.id = ANY(%s)
            """,
            (list(scored_by_id.keys()),),
        ).fetchall()
        meta_by_id = {r[0]: r for r in meta_rows}

    inserts: list[tuple] = []
    for s in scored_by_id.values():
        if ctok.is_set():
            raise Cancelled()
        if s.score < BACK_SCORE_THRESHOLD:
            continue
        if s.photo_id in accepted_fronts:
            continue
        if s.photo_id in already_pending_as_back:
            continue
        if s.photo_id in already_rejected_as_back:
            summary.photo_as_back_skipped_rejected += 1
            continue
        pred = predecessor(s)
        if pred is None:
            summary.photo_as_back_skipped_no_predecessor += 1
            continue
        if pred.score >= BACK_SCORE_THRESHOLD:
            summary.photo_as_back_skipped_predecessor_is_back += 1
            continue
        if not back_detect.aspect_close(pred.aspect, s.aspect):
            summary.photo_as_back_skipped_aspect += 1
            continue
        meta = meta_by_id.get(s.photo_id)
        if meta is None:
            log.warning("rebuild: no photo_masters row for photo %d", s.photo_id)
            continue
        _, folder, name, seq, master_path, sha256, working_path = meta
        thumb_path = str(paths.thumb_path(settings, s.photo_id))
        inserts.append((
            pred.photo_id, master_path, sha256,
            folder, name, seq,
            s.score, working_path, thumb_path,
            s.photo_id,
        ))

    if inserts:
        with db.connection() as conn:
            conn.autocommit = False
            try:
                for tup in inserts:
                    conn.execute(
                        """
                        insert into ingest_pairings
                          (front_photo_id, back_master_path, back_sha256,
                           back_source_folder, back_source_filename,
                           back_scan_sequence, back_score,
                           staging_working_path, staging_thumb_path,
                           back_photo_id)
                        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        tup,
                    )
                    summary.photo_as_back_proposed += 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # --- Step 4: summary + histogram ------------------------------------
    with db.connection() as conn:
        conn.autocommit = True
        summary.total_pending_after = conn.execute(
            "select count(*) from ingest_pairings where status = 'pending'"
        ).fetchone()[0]

    # Histogram over ALL scored files (not just proposals) so George can see
    # the distribution.
    buckets: dict[str, int] = {}
    for s in scored_by_id.values():
        lo = int(s.score * 10) / 10
        key = f"{lo:.1f}"
        buckets[key] = buckets.get(key, 0) + 1
    summary.histogram = dict(sorted(buckets.items()))

    return summary
