"""Database access for cleanup: the scope selector, proposals, and the
facts the review pane needs about one photo.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import psycopg
from psycopg.rows import dict_row

from ...config import Settings
from ..ingest.paths import resolve_working_path

log = logging.getLogger(__name__)


# Scope (Phase 7 answer 6): every scan kept or private, minus the
# back-shaped ones. `is_scan` rather than the root kind, so the 19 scans
# that live under the digital root are included. Back-shaped = a
# `possible_back` triage hint or any `ingest_pairings.back_photo_id` row,
# exactly the rule Dedupe uses; backs get their own pass later.
SCOPE_SQL = """
    from photos p
   where p.is_scan
     and not p.is_deleted
     and p.triage_status in ('keep', 'private')
     and p.working_path is not null
     and not exists (
           select 1 from triage_hints h
            where h.photo_id = p.id and h.hint = 'possible_back')
     and not exists (
           select 1 from ingest_pairings ip
            where ip.back_photo_id = p.id)
"""


@dataclass
class ScopeRow:
    photo_id: int
    working_path: str
    mime: str
    file_version: int
    sha256: str
    width: int | None
    height: int | None
    scan_batch: str | None
    scan_sequence: int | None
    source_filename: str = ""
    dpi: int | None = None
    has_back: bool = False
    #: The newest AI classification label, if the classify job has run.
    #: A newspaper's columns read as separate prints (fix-up 5, photo #3839).
    ai_label: str | None = None
    proposal_status: str | None = None

    def resolved_path(self, settings: Settings) -> Path | None:
        return resolve_working_path(settings.WORKING_DIR, self.working_path)


@dataclass
class Proposal:
    id: int
    photo_id: int
    status: str
    operations: dict[str, Any]
    transform: dict[str, Any] | None
    derived_path: str | None
    needs_manual: bool
    manual_reason: str | None
    split_regions: list[dict[str, Any]] | None
    analysis_ms: int | None = None
    # Joined from photos, so the review pane needs one query.
    working_path: str | None = None
    mime: str | None = None
    file_version: int = 1
    sha256: str = ""
    width: int | None = None
    height: int | None = None
    scan_batch: str | None = None
    scan_sequence: int | None = None
    source_filename: str = ""
    physical_ref_note: str | None = None
    face_count: int = 0
    labelled_face_count: int = 0

    @property
    def op_names(self) -> list[str]:
        return sorted((self.operations.get("ops") or {}).keys())

    @property
    def is_geometric_only(self) -> bool:
        names = set(self.op_names)
        return bool(names) and names <= {"deskew", "crop"}

    @property
    def is_split(self) -> bool:
        return bool(self.split_regions)

    def resolved_path(self, settings: Settings) -> Path | None:
        return resolve_working_path(settings.WORKING_DIR, self.working_path)


def _as_dict(v: Any) -> dict[str, Any]:
    if v is None:
        return {}
    if isinstance(v, str):
        return json.loads(v)
    return dict(v)


def _as_list(v: Any) -> list[dict[str, Any]] | None:
    if v is None:
        return None
    if isinstance(v, str):
        v = json.loads(v)
    return list(v) if v else None


# --------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------

def scope_count(conn: psycopg.Connection) -> int:
    return int(conn.execute("select count(*) " + SCOPE_SQL).fetchone()[0])


def select_scope(
    conn: psycopg.Connection,
    *,
    reanalyse: bool = False,
    batches: Sequence[str] | None = None,
    photo_ids: Sequence[int] | None = None,
    limit: int | None = None,
) -> list[ScopeRow]:
    """Photos to analyse, in batch / sequence order.

    Without `reanalyse` a photo that already has a proposal in any state is
    skipped — the analyser is resumable and cheap to re-run.

    `photo_ids` restricts the pass to a named list. A whole-archive sweep that
    predicts a change on nine photos should be applied to those nine, not to
    3,446, and certainly not by re-analysing a batch around them.
    """
    # The lateral join for the newest proposal has to sit inside the FROM
    # clause, so this spells the scope out rather than reusing SCOPE_SQL.
    sql = f"""
        select p.id, p.working_path, p.mime, p.file_version, p.sha256,
               p.width, p.height, p.scan_batch, p.scan_sequence,
               p.source_filename,
               (select max(pm.dpi) from photo_masters pm where pm.photo_id = p.id) as dpi,
               exists (select 1 from photo_backs b where b.photo_id = p.id) as has_back,
               (select s.payload ->> 'label'
                  from suggestions s
                 where s.photo_id = p.id and s.kind = 'classification'
                   and s.status <> 'rejected'
                 order by s.id desc limit 1) as ai_label,
               cp.status::text as proposal_status
          from photos p
          left join lateral (
                 select status from cleanup_proposals c
                  where c.photo_id = p.id
                  order by c.id desc limit 1
               ) cp on true
         where p.is_scan
           and not p.is_deleted
           and p.triage_status in ('keep', 'private')
           and p.working_path is not null
           and not exists (
                 select 1 from triage_hints h
                  where h.photo_id = p.id and h.hint = 'possible_back')
           and not exists (
                 select 1 from ingest_pairings ip
                  where ip.back_photo_id = p.id)
           {'and p.scan_batch = any(%s)' if batches else ''}
           {'and p.id = any(%s)' if photo_ids else ''}
           {'' if reanalyse else 'and cp.status is null'}
         order by p.scan_batch nulls last, p.scan_sequence nulls last, p.id
         {'limit %s' if limit else ''}
    """
    args: list[Any] = []
    if batches:
        args.append(list(batches))
    if photo_ids:
        args.append([int(i) for i in photo_ids])
    if limit:
        args.append(limit)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()
    return [ScopeRow(
        photo_id=r["id"], working_path=r["working_path"], mime=r["mime"],
        file_version=r["file_version"], sha256=r["sha256"],
        width=r["width"], height=r["height"],
        scan_batch=r["scan_batch"], scan_sequence=r["scan_sequence"],
        source_filename=r["source_filename"],
        dpi=r["dpi"], has_back=bool(r["has_back"]),
        ai_label=r["ai_label"],
        proposal_status=r["proposal_status"],
    ) for r in rows]


def batch_labels(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute(
        "select distinct p.scan_batch " + SCOPE_SQL
        + " and p.scan_batch is not null order by 1"
    ).fetchall()
    return [r[0] for r in rows]


# --------------------------------------------------------------------------
# Proposals
# --------------------------------------------------------------------------

def supersede_pending(conn: psycopg.Connection, photo_id: int) -> int:
    """A re-analysis replaces the live proposal; the old row is kept as
    `superseded` so the history stays readable.

    `clean` goes too, not just `pending`: a clean photo has no decision to
    preserve, and leaving the old row behind made `status_counts` report two
    `clean` rows per photo after one re-analysis. Decisions —
    `accepted`, `rejected`, `manual` — are never superseded.
    """
    cur = conn.execute(
        """
        update cleanup_proposals
           set status = 'superseded', decided_at = now(), decided_by = 'cleanup_analyse'
         where photo_id = %s and status in ('pending', 'clean')
        """,
        (photo_id,),
    )
    return cur.rowcount or 0


def insert_proposal(
    conn: psycopg.Connection,
    *,
    photo_id: int,
    status: str,
    operations: dict[str, Any],
    transform: dict[str, Any] | None,
    split_regions: list[dict[str, Any]] | None,
    needs_manual: bool,
    manual_reason: str | None,
    analysis_ms: int | None,
    derived_path: str | None = None,
) -> int:
    row = conn.execute(
        """
        insert into cleanup_proposals
          (photo_id, status, operations, transform, split_regions,
           needs_manual, manual_reason, analysis_ms, derived_path, analysed_at)
        values (%s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, %s, now())
        returning id
        """,
        (photo_id, status, json.dumps(operations, default=str),
         json.dumps(transform, default=str) if transform is not None else None,
         json.dumps(split_regions, default=str) if split_regions is not None else None,
         needs_manual, manual_reason, analysis_ms, derived_path),
    ).fetchone()
    return int(row[0])


def set_derived_path(conn: psycopg.Connection, proposal_id: int, path: str | None) -> None:
    conn.execute(
        "update cleanup_proposals set derived_path = %s where id = %s",
        (path, proposal_id),
    )


def update_split_regions(
    conn: psycopg.Connection, proposal_id: int,
    regions: list[dict[str, Any]], *, actor: str,
) -> None:
    """Replace a proposal's split regions with the ones George drew.

    `operations.ops.split` is kept in step so the caption and the report do
    not go on claiming a region count that is no longer true.
    """
    conn.execute(
        """
        update cleanup_proposals
           set split_regions = %s::jsonb,
               operations = jsonb_set(
                   jsonb_set(operations, '{ops,split}',
                             coalesce(operations -> 'ops' -> 'split', '{}'::jsonb)
                             || jsonb_build_object(
                                    'regions', %s::int,
                                    'edited_by', %s::text)),
                   '{split_edited}', 'true'::jsonb),
               updated_at = now()
         where id = %s
        """,
        (json.dumps(regions, default=str), len(regions), actor,
         proposal_id),
    )


def decide(
    conn: psycopg.Connection, proposal_id: int, status: str, *, actor: str,
) -> None:
    conn.execute(
        """
        update cleanup_proposals
           set status = %s, decided_at = now(), decided_by = %s
         where id = %s
        """,
        (status, actor, proposal_id),
    )


_PROPOSAL_SELECT = """
    select cp.id, cp.photo_id, cp.status::text as status, cp.operations,
           cp.transform, cp.derived_path, cp.needs_manual, cp.manual_reason,
           cp.split_regions, cp.analysis_ms,
           p.working_path, p.mime, p.file_version, p.sha256,
           p.width, p.height, p.scan_batch, p.scan_sequence,
           p.source_filename, p.physical_ref_note,
           (select count(*) from faces f
             where f.photo_id = p.id and not f.is_deleted) as face_count,
           (select count(*) from faces f
             where f.photo_id = p.id and not f.is_deleted
               and f.person_id is not null) as labelled_face_count
      from cleanup_proposals cp
      join photos p on p.id = cp.photo_id
"""


def _to_proposal(r: dict[str, Any]) -> Proposal:
    return Proposal(
        id=r["id"], photo_id=r["photo_id"], status=r["status"],
        operations=_as_dict(r["operations"]),
        transform=_as_dict(r["transform"]) or None,
        derived_path=r["derived_path"], needs_manual=bool(r["needs_manual"]),
        manual_reason=r["manual_reason"],
        split_regions=_as_list(r["split_regions"]),
        analysis_ms=r["analysis_ms"],
        working_path=r["working_path"], mime=r["mime"],
        file_version=r["file_version"], sha256=r["sha256"],
        width=r["width"], height=r["height"],
        scan_batch=r["scan_batch"], scan_sequence=r["scan_sequence"],
        source_filename=r["source_filename"],
        physical_ref_note=r["physical_ref_note"],
        face_count=int(r["face_count"]), labelled_face_count=int(r["labelled_face_count"]),
    )


#: The review queue's `Show:` filter. Keys are stored in QSettings, so they
#: are part of the on-disk contract — add to this, never rename.
QUEUE_FILTERS: tuple[tuple[str, str], ...] = (
    ("all", "All pending"),
    ("splits", "Splits"),
    ("manual", "Needs manual"),
    ("geometric", "Geometric-only"),
)

#: Each filter's `where` clause. "geometric" is `Proposal.is_geometric_only`
#: written in SQL — at least one op, and nothing outside deskew/crop — with
#: the same exclusions bulk accept applies, so what the filter shows is
#: exactly what the bulk button would take.
_QUEUE_FILTER_SQL: dict[str, str] = {
    "all": "",
    "splits": " and cp.split_regions is not null",
    "manual": " and cp.needs_manual",
    "geometric": """
        and cp.split_regions is null
        and not cp.needs_manual
        and coalesce(cp.operations -> 'ops', '{}'::jsonb) ?| array['deskew', 'crop']
        and not exists (
            select 1
              from jsonb_object_keys(
                       coalesce(cp.operations -> 'ops', '{}'::jsonb)) as k
             where k not in ('deskew', 'crop'))
    """,
}


def pending_ids(
    conn: psycopg.Connection,
    *,
    batches: Sequence[str] | None = None,
    queue_filter: str = "all",
) -> list[int]:
    sql = """
        select cp.id
          from cleanup_proposals cp
          join photos p on p.id = cp.photo_id
         where cp.status = 'pending' and not p.is_deleted
    """
    args: list[Any] = []
    if batches:
        sql += " and p.scan_batch = any(%s)"
        args.append(list(batches))
    sql += _QUEUE_FILTER_SQL.get(queue_filter, "")
    sql += " order by p.scan_batch nulls last, p.scan_sequence nulls last, p.id"
    return [r[0] for r in conn.execute(sql, args).fetchall()]


def pending_counts_by_filter(
    conn: psycopg.Connection, *, batches: Sequence[str] | None = None,
) -> dict[str, int]:
    """How many pending proposals each filter would show, for the combo's
    labels — George picked *Splits* because it was 17, so the number belongs
    next to the choice, not behind it."""
    return {key: len(pending_ids(conn, batches=batches, queue_filter=key))
            for key, _label in QUEUE_FILTERS}


def load_proposal(conn: psycopg.Connection, proposal_id: int) -> Proposal | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_PROPOSAL_SELECT + " where cp.id = %s", (proposal_id,))
        r = cur.fetchone()
    return _to_proposal(r) if r else None


def load_pending_for_photo(conn: psycopg.Connection, photo_id: int) -> Proposal | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _PROPOSAL_SELECT + " where cp.photo_id = %s and cp.status = 'pending'",
            (photo_id,),
        )
        r = cur.fetchone()
    return _to_proposal(r) if r else None


def manual_queue(conn: psycopg.Connection) -> list[Proposal]:
    """Photos parked for hand-fixing — minus any that have come back.

    A decision is never superseded, so a photo rejected with R keeps its
    `manual` row forever. When the analyser is fixed and that photo is
    re-analysed it gains a live `pending` proposal, and listing it in both
    queues shows the same scan twice with the stale answer in one of them.
    The newer proposal is the real one; the `manual` row stays as the record
    that George rejected what came before it.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _PROPOSAL_SELECT
            + """ where cp.status = 'manual'
                    and not exists (
                          select 1 from cleanup_proposals live
                           where live.photo_id = cp.photo_id
                             and live.status = 'pending')
                  order by p.scan_batch nulls last, p.scan_sequence nulls last, p.id"""
        )
        rows = cur.fetchall()
    return [_to_proposal(r) for r in rows]


def status_counts(conn: psycopg.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        select cp.status::text, count(*)
          from cleanup_proposals cp
         where cp.status <> 'superseded'
         group by 1
        """
    ).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def op_frequency(conn: psycopg.Connection) -> dict[str, int]:
    """How often each op appears across live proposals — the report's
    "op frequency" line."""
    rows = conn.execute(
        """
        select key, count(*)
          from cleanup_proposals cp,
               jsonb_each(coalesce(cp.operations -> 'ops', '{}'::jsonb))
         where cp.status <> 'superseded'
         group by key order by 2 desc
        """
    ).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def manual_reason_counts(conn: psycopg.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        select coalesce(manual_reason, 'unknown'), count(*)
          from cleanup_proposals
         where needs_manual and status <> 'superseded'
         group by 1 order by 2 desc
        """
    ).fetchall()
    return {r[0]: int(r[1]) for r in rows}


# --------------------------------------------------------------------------
# Faces on a photo
# --------------------------------------------------------------------------

@dataclass
class FaceRow:
    id: int
    bbox: dict[str, Any]
    person_id: int | None
    person_name: str | None = None


def faces_for(conn: psycopg.Connection, photo_id: int) -> list[FaceRow]:
    rows = conn.execute(
        """
        select f.id, f.bbox, f.person_id, pe.display_name
          from faces f
          left join people pe on pe.id = f.person_id
         where f.photo_id = %s and not f.is_deleted
         order by f.id
        """,
        (photo_id,),
    ).fetchall()
    out = []
    for fid, bbox, person_id, name in rows:
        out.append(FaceRow(
            id=fid,
            bbox=bbox if isinstance(bbox, dict) else json.loads(bbox),
            person_id=person_id, person_name=name,
        ))
    return out


def spend_total(conn: psycopg.Connection, provider: str | None = None) -> float:
    sql = ("select coalesce(sum(coalesce(actual_cost_usd, cost_estimate_usd)), 0)"
           " from cleanup_spend where status <> 'failed'")
    args: list[Any] = []
    if provider:
        sql += " and provider = %s"
        args.append(provider)
    return float(conn.execute(sql, args).fetchone()[0])


def record_spend(
    conn: psycopg.Connection,
    *,
    provider: str,
    photo_id: int | None,
    proposal_id: int | None,
    job_ref: str | None,
    cost_estimate_usd: float | None,
    actual_cost_usd: float | None = None,
    status: str = "submitted",
) -> int:
    row = conn.execute(
        """
        insert into cleanup_spend
          (provider, photo_id, proposal_id, job_ref,
           cost_estimate_usd, actual_cost_usd, status)
        values (%s, %s, %s, %s, %s, %s, %s)
        returning id
        """,
        (provider, photo_id, proposal_id, job_ref,
         cost_estimate_usd, actual_cost_usd, status),
    ).fetchone()
    return int(row[0])
