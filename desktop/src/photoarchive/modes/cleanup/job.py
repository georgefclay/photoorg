"""The `cleanup_analyse` batch job, plus bulk accept.

Resumable: without "Re-analyse" a photo that already has a proposal is
skipped, so killing the run at N and starting again continues at N. The
masters guard probe runs first, exactly as ingest does — invariant 4.

Worker-thread contract (the Phase 3 lesson): nothing in here touches a
widget. Progress goes out as plain dicts through `progress_cb`, which the
panel marshals onto the GUI thread with a QueuedConnection.
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ... import db
from ...config import Settings
from ...workers import CancelToken, Cancelled
from ..ingest.guard import remediation_message, run_masters_guard
from . import analyse as analyse_mod
from . import paths as cpaths
from . import render as render_mod
from . import repo
from .accept import AcceptResult, accept_proposal

log = logging.getLogger(__name__)


class MastersWritable(RuntimeError):
    """The guard found a writable master root. Invariant 4: refuse to run."""


@dataclass
class AnalyseStats:
    total: int = 0
    analysed: int = 0
    clean: int = 0
    pending: int = 0
    needs_manual: int = 0
    split: int = 0
    failed: int = 0
    skipped_missing: int = 0
    elapsed_seconds: float = 0.0
    ms_per_photo: list[int] = field(default_factory=list)
    ops: Counter = field(default_factory=Counter)
    manual_reasons: Counter = field(default_factory=Counter)
    failures: list[dict[str, Any]] = field(default_factory=list)

    @property
    def median_ms(self) -> int:
        if not self.ms_per_photo:
            return 0
        s = sorted(self.ms_per_photo)
        return s[len(s) // 2]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total, "analysed": self.analysed,
            "clean": self.clean, "pending": self.pending,
            "needs_manual": self.needs_manual, "split": self.split,
            "failed": self.failed, "skipped_missing": self.skipped_missing,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "median_ms": self.median_ms,
            "mean_ms": (int(sum(self.ms_per_photo) / len(self.ms_per_photo))
                        if self.ms_per_photo else 0),
            "max_ms": max(self.ms_per_photo) if self.ms_per_photo else 0,
            "ops": dict(self.ops),
            "manual_reasons": dict(self.manual_reasons),
            "failures": self.failures[:50],
        }

    def report(self) -> str:
        d = self.to_dict()
        lines = [
            f"Analysed {d['analysed']} of {d['total']} scans in "
            f"{d['elapsed_seconds']:.1f} s "
            f"({d['median_ms']} ms median, {d['max_ms']} ms worst).",
            f"  clean {d['clean']}  pending {d['pending']}  "
            f"needs_manual {d['needs_manual']}  split {d['split']}",
        ]
        if d["ops"]:
            lines.append("  ops: " + ", ".join(
                f"{k} {v}" for k, v in sorted(d["ops"].items(),
                                              key=lambda kv: -kv[1])))
        if d["manual_reasons"]:
            lines.append("  needs_manual: " + ", ".join(
                f"{k} {v}" for k, v in sorted(d["manual_reasons"].items(),
                                              key=lambda kv: -kv[1])))
        if d["skipped_missing"]:
            lines.append(f"  skipped (file missing): {d['skipped_missing']}")
        if d["failed"]:
            lines.append(f"  failed: {d['failed']} (see the log)")
        return "\n".join(lines)


def run_cleanup_analyse(
    settings: Settings,
    *,
    reanalyse: bool = False,
    batches: Sequence[str] | None = None,
    photo_ids: Sequence[int] | None = None,
    limit: int | None = None,
    progress_cb: Callable[[dict], None] = lambda _: None,
    cancel_token: CancelToken | None = None,
    write_previews: bool = True,
) -> AnalyseStats:
    ctok = cancel_token or CancelToken()
    cpaths.ensure_dirs(settings)

    guard = run_masters_guard(settings.master_roots)
    if not guard.all_read_only:
        raise MastersWritable(remediation_message(guard))

    stats = AnalyseStats()
    started = time.time()

    with db.connection() as conn:
        conn.autocommit = True
        job_run_id = db.start_job_run(
            conn, job_name="cleanup_analyse",
            params={"reanalyse": reanalyse, "batches": list(batches or []),
                    "photo_ids": [int(i) for i in (photo_ids or [])],
                    "limit": limit, "guard": guard.to_params()},
        )
        db.audit(conn, actor="desktop", action="cleanup_analyse.start",
                 entity_type="job_run", entity_id=job_run_id,
                 new_value={"reanalyse": reanalyse,
                            "batches": list(batches or [])})
        rows = repo.select_scope(conn, reanalyse=reanalyse, batches=batches,
                                 photo_ids=photo_ids, limit=limit)

    stats.total = len(rows)
    progress_cb({"kind": "start", "total": stats.total})

    try:
        for i, row in enumerate(rows, start=1):
            if ctok.is_set():
                raise Cancelled()
            path = row.resolved_path(settings)
            if path is None or not path.exists():
                stats.skipped_missing += 1
                log.warning("cleanup_analyse: working file missing for photo %s (%s)",
                            row.photo_id, row.working_path)
                progress_cb({"kind": "item", "done": i, "total": stats.total,
                             "photo_id": row.photo_id, "status": "missing"})
                continue
            try:
                result = analyse_mod.analyse_photo(
                    settings, photo_id=row.photo_id, working_path=path,
                    dpi=row.dpi, has_back=row.has_back,
                    ai_label=row.ai_label,
                )
            except Exception as e:
                stats.failed += 1
                stats.failures.append({"photo_id": row.photo_id, "error": str(e)})
                log.exception("cleanup_analyse: photo %s failed", row.photo_id)
                progress_cb({"kind": "item", "done": i, "total": stats.total,
                             "photo_id": row.photo_id, "status": "failed"})
                continue

            stats.analysed += 1
            stats.ms_per_photo.append(result.analysis_ms)
            for name in result.op_names:
                stats.ops[name] += 1
            if result.manual_reason:
                stats.manual_reasons[result.manual_reason] += 1

            proposal_id = _store(settings, result)
            if result.status == "clean":
                stats.clean += 1
            else:
                stats.pending += 1
                if result.split_regions:
                    stats.split += 1
                if result.needs_manual:
                    stats.needs_manual += 1
                if write_previews and proposal_id:
                    _write_preview(settings, result, proposal_id, path)

            progress_cb({"kind": "item", "done": i, "total": stats.total,
                         "photo_id": row.photo_id, "status": result.status,
                         "ms": result.analysis_ms,
                         "caption": analyse_mod.caption_for(result.operations)})
    except Cancelled:
        stats.elapsed_seconds = time.time() - started
        with db.connection() as conn:
            conn.autocommit = True
            db.finish_job_run(conn, job_run_id=job_run_id, status="cancelled",
                              stats=stats.to_dict())
        raise
    except Exception:
        stats.elapsed_seconds = time.time() - started
        with db.connection() as conn:
            conn.autocommit = True
            db.finish_job_run(conn, job_run_id=job_run_id, status="failed",
                              stats=stats.to_dict())
        raise

    stats.elapsed_seconds = time.time() - started
    with db.connection() as conn:
        conn.autocommit = True
        db.finish_job_run(conn, job_run_id=job_run_id, status="succeeded",
                          stats=stats.to_dict())
    progress_cb({"kind": "done", **stats.to_dict()})
    return stats


def _store(settings: Settings, result: analyse_mod.Analysis) -> int | None:
    with db.connection() as conn:
        conn.autocommit = False
        try:
            repo.supersede_pending(conn, result.photo_id)
            proposal_id = repo.insert_proposal(
                conn,
                photo_id=result.photo_id,
                status=result.status,
                operations=result.operations,
                transform=result.transform.to_json() if result.transform else None,
                split_regions=result.split_regions,
                needs_manual=result.needs_manual,
                manual_reason=result.manual_reason,
                analysis_ms=result.analysis_ms,
            )
            db.audit(conn, actor="desktop", action="cleanup.propose",
                     entity_type="photo", entity_id=result.photo_id,
                     new_value={"proposal_id": proposal_id,
                                "status": result.status,
                                "ops": result.op_names,
                                "needs_manual": result.needs_manual,
                                "manual_reason": result.manual_reason,
                                "regions": len(result.split_regions or []),
                                "analysis_ms": result.analysis_ms})
            conn.commit()
            return proposal_id
        except Exception:
            conn.rollback()
            raise


def _write_preview(
    settings: Settings, result: analyse_mod.Analysis, proposal_id: int, src,
) -> None:
    """A ~2000 px JPEG of the proposed result, so the review queue opens
    instantly. The full-resolution render waits for a zoom or an Accept."""
    try:
        if result.split_regions:
            from .geometry import Transform
            for region in result.split_regions:
                plan = render_mod.Plan(
                    transform=Transform.from_json(region["transform"]))
                out = cpaths.region_preview_path(
                    settings, result.photo_id, proposal_id, int(region["index"]))
                render_mod.render_preview(
                    src, plan, out, edge=settings.CLEANUP_ANALYSE_EDGE,
                    operations=result.operations)
        plan = render_mod.plan_from(
            result.operations,
            render_mod.default_ticked(result.operations, settings),
            settings=settings, src_w=result.src_w, src_h=result.src_h,
        )
        out = cpaths.preview_path(settings, result.photo_id, proposal_id)
        render_mod.render_preview(src, plan, out,
                                  edge=settings.CLEANUP_ANALYSE_EDGE,
                                  operations=result.operations)
        with db.connection() as conn:
            conn.autocommit = True
            repo.set_derived_path(conn, proposal_id, str(out))
    except Exception as e:
        log.warning("cleanup: preview render failed for photo %s: %s",
                    result.photo_id, e)


# --------------------------------------------------------------------------
# Bulk accept — the boring geometric majority
# --------------------------------------------------------------------------

@dataclass
class BulkStats:
    considered: int = 0
    accepted: int = 0
    failed: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    results: list[AcceptResult] = field(default_factory=list)

    def report(self) -> str:
        line = f"Accepted {self.accepted} of {self.considered} geometric-only proposals."
        if self.failed:
            line += f" {self.failed} failed — see the log."
        return line


def geometric_only_ids(
    settings: Settings, *, batches: Sequence[str] | None = None,
) -> list[int]:
    """Pending proposals whose only ops are deskew and/or crop, with no
    split and no needs_manual. Tonal ops always go through the eye; splits
    are never bulk-accepted (answers 7 and 8)."""
    out: list[int] = []
    with db.connection() as conn:
        conn.autocommit = True
        for pid in repo.pending_ids(conn, batches=batches):
            p = repo.load_proposal(conn, pid)
            if p is None or p.needs_manual or p.is_split:
                continue
            if p.is_geometric_only:
                out.append(pid)
    return out


def bulk_accept_geometric(
    settings: Settings,
    proposal_ids: Sequence[int],
    *,
    actor: str = "desktop",
    progress_cb: Callable[[dict], None] = lambda _: None,
    cancel_token: CancelToken | None = None,
) -> BulkStats:
    ctok = cancel_token or CancelToken()
    stats = BulkStats(considered=len(proposal_ids))
    progress_cb({"kind": "start", "total": len(proposal_ids)})
    for i, pid in enumerate(proposal_ids, start=1):
        if ctok.is_set():
            raise Cancelled()
        try:
            res = accept_proposal(settings, pid, actor=actor)
            stats.accepted += 1
            stats.results.append(res)
            progress_cb({"kind": "item", "done": i, "total": len(proposal_ids),
                         "photo_id": res.photo_id, "status": "accepted"})
        except Exception as e:
            stats.failed += 1
            stats.errors.append({"proposal_id": pid, "error": str(e)})
            log.exception("cleanup: bulk accept failed for proposal %s", pid)
            progress_cb({"kind": "item", "done": i, "total": len(proposal_ids),
                         "proposal_id": pid, "status": "failed"})
    progress_cb({"kind": "done", "accepted": stats.accepted,
                 "failed": stats.failed})
    return stats
