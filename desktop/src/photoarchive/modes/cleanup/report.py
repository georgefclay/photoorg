"""The run report: counts, timings, op frequency, and before/after pairs.

Written under `CLEANUP_DIR/_report/<stamp>/` as `report.md` (readable in the
terminal or a viewer), `summary.json` (machine-readable), and
`contact-sheet.html` plus the pair JPEGs George looks at before authorising
the full run.
"""
from __future__ import annotations

import html
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from PIL import Image

from ... import db
from ...config import Settings
from . import analyse as analyse_mod
from . import paths as cpaths
from . import render as render_mod
from . import repo

log = logging.getLogger(__name__)

PAIR_EDGE = 900
DEFAULT_SAMPLES = 10


@dataclass
class ReportPaths:
    directory: Path
    markdown: Path
    summary: Path
    contact_sheet: Path


def write_report(
    settings: Settings,
    *,
    stats: dict[str, Any] | None = None,
    samples: int = DEFAULT_SAMPLES,
    batches: Sequence[str] | None = None,
    title: str = "Cleanup analysis",
) -> ReportPaths:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = cpaths.report_dir(settings, stamp)
    out_dir.mkdir(parents=True, exist_ok=True)

    with db.connection() as conn:
        conn.autocommit = True
        counts = repo.status_counts(conn)
        ops = repo.op_frequency(conn)
        manual = repo.manual_reason_counts(conn)
        scope_total = repo.scope_count(conn)
        pending = repo.pending_ids(conn, batches=batches)
        proposals = [repo.load_proposal(conn, pid) for pid in pending]
        proposals = [p for p in proposals if p is not None]
        spend = repo.spend_total(conn)

    sample_rows = _pick_samples(proposals, samples)
    pairs = _render_pairs(settings, sample_rows, out_dir)

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "title": title,
        "batches": list(batches or []),
        "scope_total": scope_total,
        "status_counts": counts,
        "op_frequency": ops,
        "manual_reasons": manual,
        "pending_in_queue": len(pending),
        "split_proposals": sum(1 for p in proposals if p.is_split),
        "geometric_only": sum(1 for p in proposals
                              if p.is_geometric_only and not p.needs_manual),
        "remote_spend_usd": round(spend, 4),
        "run_stats": stats or {},
        "samples": [asdict(p) for p in pairs],
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str),
                            encoding="utf-8")

    md_path = out_dir / "report.md"
    md_path.write_text(_markdown(summary, proposals), encoding="utf-8")

    sheet_path = out_dir / "contact-sheet.html"
    sheet_path.write_text(_contact_sheet(summary, pairs), encoding="utf-8")

    log.info("cleanup: report written to %s", out_dir)
    return ReportPaths(directory=out_dir, markdown=md_path,
                       summary=summary_path, contact_sheet=sheet_path)


# --------------------------------------------------------------------------
# Before / after pairs
# --------------------------------------------------------------------------

@dataclass
class Pair:
    photo_id: int
    proposal_id: int
    caption: str
    before: str
    after: str
    ops: list[str]
    scan_batch: str | None = None
    scan_sequence: int | None = None


def _pick_samples(
    proposals: Sequence[repo.Proposal], n: int,
) -> list[repo.Proposal]:
    """Spread the samples across the op kinds rather than taking the first N —
    one boring deskew tells George nothing about the colour work."""
    if n <= 0 or not proposals:
        return []
    buckets: dict[str, list[repo.Proposal]] = {}
    for p in proposals:
        key = "split" if p.is_split else ",".join(p.op_names) or "none"
        buckets.setdefault(key, []).append(p)
    picked: list[repo.Proposal] = []
    while len(picked) < n and any(buckets.values()):
        for key in sorted(buckets):
            if not buckets[key]:
                continue
            picked.append(buckets[key].pop(0))
            if len(picked) >= n:
                break
    return picked


def _render_pairs(
    settings: Settings, proposals: Sequence[repo.Proposal], out_dir: Path,
) -> list[Pair]:
    pairs: list[Pair] = []
    for p in proposals:
        src = p.resolved_path(settings)
        if src is None or not src.exists():
            continue
        try:
            before = out_dir / f"{p.photo_id:08d}_before.jpg"
            _write_scaled(src, before, PAIR_EDGE)

            after = out_dir / f"{p.photo_id:08d}_after.jpg"
            if p.is_split and p.split_regions:
                from .geometry import Transform
                region = p.split_regions[0]
                plan = render_mod.Plan(
                    transform=Transform.from_json(region["transform"]))
            else:
                plan = render_mod.plan_from(
                    p.operations,
                    render_mod.default_ticked(p.operations, settings),
                    settings=settings,
                )
            render_mod.render_preview(src, plan, after, edge=PAIR_EDGE,
                                      operations=p.operations)
            pairs.append(Pair(
                photo_id=p.photo_id, proposal_id=p.id,
                caption=analyse_mod.caption_for(p.operations),
                before=before.name, after=after.name, ops=p.op_names,
                scan_batch=p.scan_batch, scan_sequence=p.scan_sequence,
            ))
        except Exception as e:
            log.warning("cleanup report: pair render failed for photo %s: %s",
                        p.photo_id, e)
    return pairs


def _write_scaled(src: Path, out: Path, edge: int) -> None:
    with Image.open(src) as im:
        from PIL import ImageOps
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((edge, edge), Image.LANCZOS)
        im.save(out, "JPEG", quality=88, optimize=True)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def _markdown(summary: dict[str, Any], proposals: Sequence[repo.Proposal]) -> str:
    s = summary
    lines = [
        f"# {s['title']}",
        "",
        f"Generated {s['generated_at']}.",
        "",
        f"- Scope (scans, keep/private, not back-shaped): **{s['scope_total']}**",
        f"- Pending in the review queue: **{s['pending_in_queue']}**",
        f"- Split proposals: **{s['split_proposals']}**",
        f"- Geometric-only (bulk-acceptable): **{s['geometric_only']}**",
        f"- Remote-enhance spend to date: **${s['remote_spend_usd']:.2f}**",
        "",
        "## Counts by status",
        "",
        "| status | count |",
        "| --- | --- |",
    ]
    for k, v in sorted(s["status_counts"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| {k} | {v} |")
    lines += ["", "## Op frequency", "", "| op | proposals |", "| --- | --- |"]
    for k, v in sorted(s["op_frequency"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| {k} | {v} |")
    if s["manual_reasons"]:
        lines += ["", "## needs_manual reasons", "",
                  "| reason | count |", "| --- | --- |"]
        for k, v in sorted(s["manual_reasons"].items(), key=lambda kv: -kv[1]):
            lines.append(f"| {k} | {v} |")

    run = s.get("run_stats") or {}
    if run:
        lines += [
            "", "## Timing", "",
            f"- Analysed {run.get('analysed', 0)} of {run.get('total', 0)} "
            f"in {run.get('elapsed_seconds', 0)} s",
            f"- Per photo: median {run.get('median_ms', 0)} ms, "
            f"mean {run.get('mean_ms', 0)} ms, worst {run.get('max_ms', 0)} ms",
        ]
        if run.get("skipped_missing"):
            lines.append(f"- Skipped (working file missing): {run['skipped_missing']}")
        if run.get("failed"):
            lines.append(f"- Failed: {run['failed']}")

    if s["samples"]:
        lines += ["", "## Samples", "",
                  "Open `contact-sheet.html` for the before/after pairs.", ""]
        for p in s["samples"]:
            lines.append(f"- #{p['photo_id']} — {p['caption']}")
    return "\n".join(lines) + "\n"


def _contact_sheet(summary: dict[str, Any], pairs: Sequence[Pair]) -> str:
    rows = []
    for p in pairs:
        rows.append(f"""
      <figure>
        <figcaption>#{p.photo_id} &mdash; {html.escape(p.caption)}
          <small>{html.escape(str(p.scan_batch or ''))}
            #{p.scan_sequence if p.scan_sequence is not None else '?'}</small>
        </figcaption>
        <div class="pair">
          <div><span>before</span><img src="{html.escape(p.before)}" alt="before"></div>
          <div><span>after</span><img src="{html.escape(p.after)}" alt="after"></div>
        </div>
      </figure>""")
    return f"""<!doctype html>
<meta charset="utf-8">
<title>{html.escape(summary['title'])} &mdash; {summary['generated_at']}</title>
<style>
  body {{ font: 14px/1.5 system-ui, sans-serif; margin: 0; padding: 24px;
          background: #14161a; color: #e6e6e6; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .meta {{ color: #9aa3ad; margin-bottom: 24px; }}
  figure {{ margin: 0 0 32px; }}
  figcaption {{ font-weight: 600; margin-bottom: 6px; }}
  figcaption small {{ font-weight: 400; color: #9aa3ad; margin-left: 8px; }}
  .pair {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
  .pair span {{ display: block; color: #9aa3ad; font-size: 12px;
                text-transform: uppercase; letter-spacing: .04em; }}
  img {{ width: 100%; height: auto; background: #000; border-radius: 4px; }}
  @media (max-width: 700px) {{ .pair {{ grid-template-columns: 1fr; }} }}
</style>
<h1>{html.escape(summary['title'])}</h1>
<p class="meta">{summary['generated_at']} &middot;
  scope {summary['scope_total']} &middot;
  pending {summary['pending_in_queue']} &middot;
  splits {summary['split_proposals']} &middot;
  geometric-only {summary['geometric_only']}</p>
{''.join(rows) if rows else '<p>No samples rendered.</p>'}
"""
