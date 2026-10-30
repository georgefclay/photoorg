"""Date hygiene report and audited un-confirm (Phase 13 fix-up 1).

Every confirmed date with no `photo.capture_date.set` audit row was confirmed
by ingest from EXIF, never by a person. Each is checked against four rules:

  a  the photo is a scan (`is_scan`, or on a `|scan` root) — the EXIF date
     is the scanner's clock
  b  1 January with a time of day before 00:01 — a camera never set
  c  more than 3 months from its `_YYYY-MM` folder
  d  later than the photo's own ingest date, or before 1826

Usage:
    python -m photoarchive.tools.date_hygiene --report
    python -m photoarchive.tools.date_hygiene --apply --rules a,b

`--report` writes nothing to the database: a summary and a CSV land under
`WORKING_DIR/_report/date-hygiene/<stamp>/`. `--apply` clears each flagged
date through `dates.repo.unconfirm` — the old value becomes a pending
`date` suggestion (`source='import'`, `payload.reason`), one
`photo.capture_date.unconfirm` audit row per photo, completeness refreshed —
in one transaction. Never SQL.
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .. import db as dbmod
from ..config import load as load_config
from ..logging_setup import configure_logging
from ..modes.dates import repo, rules
from ..modes.dates.rules import DateRow

log = logging.getLogger(__name__)

IN_SCOPE = ("keep", "private")


def batch_of(row: DateRow) -> str:
    if row.scan_batch:
        return row.scan_batch
    folder = (row.source_folder or "").split("/", 1)[0]
    return f"{row.source_root or '?'}:{folder or '(root)'}"


@dataclass
class Report:
    total: int = 0
    flagged: list[tuple[DateRow, list[str]]] = field(default_factory=list)
    per_rule: Counter = field(default_factory=Counter)
    per_rule_in_scope: Counter = field(default_factory=Counter)
    per_batch: dict[str, Counter] = field(default_factory=dict)

    def chosen(self, chosen: list[str]) -> list[tuple[DateRow, str, str]]:
        out = []
        for row, hits in self.flagged:
            hit = rules.reason_for(hits, chosen)
            if hit is not None:
                out.append((row, *hit))
        return out


def build_report(conn, *, scan_roots: set[str]) -> Report:
    rep = Report()
    for row in repo.load_ingest_confirmed(conn):
        rep.total += 1
        hits = rules.flag(row, scan_roots=scan_roots)
        if not hits:
            continue
        rep.flagged.append((row, hits))
        b = rep.per_batch.setdefault(batch_of(row), Counter())
        for h in hits:
            rep.per_rule[h] += 1
            b[h] += 1
            if row.triage_status in IN_SCOPE and not row.is_deleted:
                rep.per_rule_in_scope[h] += 1
    return rep


def _row_cells(row: DateRow, hits: list[str]) -> list[str]:
    return [
        str(row.photo_id), row.capture_date.isoformat(), row.precision,
        row.exif_taken_at.isoformat(sep=" ") if row.exif_taken_at else "",
        "scan" if row.is_scan else "digital", row.source_root or "",
        batch_of(row), row.source_folder or "", row.ingested_at.date().isoformat(),
        row.triage_status + (" (deleted)" if row.is_deleted else ""), ",".join(hits),
    ]


HEADER = ["photo_id", "capture_date", "precision", "exif_taken_at", "kind",
          "source_root", "batch", "source_folder", "ingested", "triage", "rules"]


def format_summary(rep: Report, *, samples: int = 10) -> str:
    lines = [
        f"Ingest-sourced confirmed dates: {rep.total}",
        f"Flagged by at least one rule:   {len(rep.flagged)}",
        "",
        "Per rule (all photos / in scope keep+private, not deleted):",
    ]
    for r in rules.RULES:
        lines.append(f"  {r}  {rep.per_rule[r]:>6} / {rep.per_rule_in_scope[r]:<6} {rules.RULE_TEXT[r]}")
    combos = Counter(",".join(h) for _, h in rep.flagged)
    lines += ["", "Rule combinations:"]
    lines += [f"  {k:<8} {v}" for k, v in combos.most_common()]
    lines += ["", "Per batch (a / b / c / d):"]
    for name in sorted(rep.per_batch):
        c = rep.per_batch[name]
        lines.append(f"  {name:<40} {c['a']:>4} {c['b']:>4} {c['c']:>4} {c['d']:>4}")
    if rep.flagged:
        lines += ["", f"Samples (first {samples} of each rule):", "  " + " | ".join(HEADER)]
        shown: set[int] = set()
        for r in rules.RULES:
            n = 0
            for row, hits in rep.flagged:
                if r in hits and row.photo_id not in shown and n < samples:
                    lines.append("  " + " | ".join(_row_cells(row, hits)))
                    shown.add(row.photo_id)
                    n += 1
    return "\n".join(lines)


def write_report(rep: Report, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "flagged.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        for row, hits in rep.flagged:
            w.writerow(_row_cells(row, hits))
    (out_dir / "summary.md").write_text(format_summary(rep) + "\n", encoding="utf-8")
    return out_dir


def apply(conn, rep: Report, chosen: list[str]) -> Counter:
    """Un-confirm every row a chosen rule flags. Caller commits."""
    done: Counter = Counter()
    for row, rule, reason in rep.chosen(chosen):
        res = repo.unconfirm(
            conn, row.photo_id, reason=reason, rule=rule, via="date_hygiene",
            evidence=f"EXIF date, un-confirmed by date hygiene rule {rule}",
        )
        if res is not None:
            done[rule] += 1
            if res.folder_suggestion_id is not None:
                done["folder_suggestions"] += 1
    dbmod.audit(conn, actor="desktop", action="date_hygiene.apply", entity_type="photo",
                entity_id=None, new_value={"rules": chosen, "counts": dict(done)})
    return done


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(prog="photoarchive.tools.date_hygiene")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--report", action="store_true", help="report only; writes nothing to the DB")
    mode.add_argument("--apply", action="store_true", help="un-confirm the rows the --rules flag")
    p.add_argument("--rules", default="a,b", help="with --apply: comma list of a,b,c,d (default a,b)")
    p.add_argument("--samples", type=int, default=10)
    args = p.parse_args(argv)

    settings = load_config()
    scan_roots = {r.label for r in settings.master_roots if r.kind == "scan"}
    chosen = rules.parse_rules(args.rules) if args.apply else []
    dbmod.init_pool(settings)
    try:
        with dbmod.connection() as conn:
            conn.autocommit = False
            try:
                rep = build_report(conn, scan_roots=scan_roots)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                out = write_report(rep, Path(settings.WORKING_DIR) / "_report" / "date-hygiene"
                                   / (stamp + ("-apply" if args.apply else "")))
                print(format_summary(rep, samples=args.samples))
                print(f"\nReport: {out}")
                if not args.apply:
                    conn.rollback()
                    return 0
                before = rep.total
                done = apply(conn, rep, chosen)
                after = len(repo.load_ingest_confirmed(conn))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    finally:
        dbmod.close_pool()
    print(f"\nApplied rules {','.join(chosen)}: un-confirmed "
          + ", ".join(f"{r}={done[r]}" for r in chosen)
          + f" (total {sum(done[r] for r in chosen)}); "
          f"folder-month suggestions added {done['folder_suggestions']}")
    print(f"Ingest-sourced confirmed dates: before {before}, after {after}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
