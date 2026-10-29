"""The whole-archive pass: chunks in fresh processes, and the report.

CLAUDE.md, Cleanup (c): any whole-archive pass runs as resumable chunks in
fresh processes from the first attempt. The orchestrator selects what is due,
hands each chunk of `METADATA_CHUNK` ids to a new
`python -m photoarchive.tools.write_metadata --chunk-worker` process, and
collects the chunk's result file. A killed chunk costs that chunk; the next
run re-selects by hash, so it simply continues.

The report lands in `WORKING_DIR/_report/metadata/<stamp>-<dry|write>/`:
`summary.md` (counts by tag, skips, failures, what re-pushes),
`changes.txt` (per file, every tag that changes), `samples.json` (10 sample
payloads), `foreign_values.csv` (pre-existing values in tags we own that we
did not write — answer 4a), `date_mismatch.csv` (confirmed date ≠ the file's
`DateTimeOriginal` — answer 4b) and `skipped.csv`.
"""
from __future__ import annotations

import csv
import json
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from ... import db
from ...config import Settings
from . import exiftool as et_mod
from . import repo
from .writer import check_masters, env_for_children

JOB_NAME = "metadata_write"
REPORT_SUBDIR = Path("_report") / "metadata"
SAMPLE_COUNT = 10


@dataclass
class RunSummary:
    dry_run: bool
    scope: int = 0
    due: int = 0
    written: int = 0
    would_write: int = 0
    skipped: int = 0
    failed: int = 0
    in_group: int = 0
    chunks: int = 0
    failed_chunks: list[int] = field(default_factory=list)
    by_tag: Counter = field(default_factory=Counter)
    reasons: Counter = field(default_factory=Counter)
    foreign_files: int = 0
    foreign_by_tag: Counter = field(default_factory=Counter)
    foreign_region_files: int = 0
    date_mismatches: int = 0
    elapsed: float = 0.0
    report_dir: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run, "scope": self.scope, "due": self.due,
            "written": self.written, "would_write": self.would_write,
            "skipped": self.skipped, "failed": self.failed,
            "in_group": self.in_group, "chunks": self.chunks,
            "failed_chunks": self.failed_chunks,
            "by_tag": dict(self.by_tag), "reasons": dict(self.reasons),
            "foreign_files": self.foreign_files,
            "foreign_by_tag": dict(self.foreign_by_tag),
            "foreign_region_files": self.foreign_region_files,
            "date_mismatches": self.date_mismatches,
            "elapsed_seconds": round(self.elapsed, 1),
            "report_dir": self.report_dir,
        }

    def report(self) -> str:
        done = self.would_write if self.dry_run else self.written
        verb = "would write" if self.dry_run else "wrote"
        lines = [
            f"{'Dry run' if self.dry_run else 'Run'}: {self.scope} photos in scope, "
            f"{self.due} due; {verb} {done}, skipped {self.skipped}, failed {self.failed} "
            f"in {self.elapsed:.0f} s ({self.chunks} chunks).",
        ]
        if self.reasons:
            lines.append("Why due: " + ", ".join(f"{k} {v}" for k, v in self.reasons.most_common()))
        if done:
            lines.append(
                f"{done} files {'would get' if self.dry_run else 'got'} a new file_version: the next "
                f"push re-sends {self.in_group} of them now (in a group); the rest go "
                "when their group is assigned."
            )
        if self.failed_chunks:
            lines.append(f"Chunks that did not finish: {self.failed_chunks} — run again to continue.")
        if self.report_dir:
            lines.append(f"Report: {self.report_dir}")
        return "\n".join(lines)


def _spawn(cmd: list[str]) -> subprocess.Popen[str]:
    """A chunk's fresh process, output merged so the caller can stream it."""
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env_for_children(), text=True, encoding="utf-8",
                            errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _chunks(ids: Sequence[int], n: int) -> list[list[int]]:
    return [list(ids[i:i + n]) for i in range(0, len(ids), n)]


def pick_samples(cands: Sequence[repo.Candidate], n: int = SAMPLE_COUNT) -> list[repo.Candidate]:
    """A spread, not the first ten: people + back, people, back, dated scan,
    digital — evenly spaced through each group, round-robin."""
    def kind(c: repo.Candidate) -> int:
        f = c.facts
        if f.people and f.backs:
            return 0
        if f.people:
            return 1
        if f.backs:
            return 2
        if f.is_scan:
            return 3
        return 4
    groups: dict[int, list[repo.Candidate]] = {}
    for c in cands:
        groups.setdefault(kind(c), []).append(c)
    spread: dict[int, list[repo.Candidate]] = {}
    for k, g in groups.items():
        step = max(1, len(g) // n)
        spread[k] = g[::step]
    out: list[repo.Candidate] = []
    while len(out) < n and any(spread.values()):
        for k in sorted(spread):
            if spread[k] and len(out) < n:
                out.append(spread[k].pop(0))
    return out


def _short(v: Any, n: int = 160) -> str:
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    s = s.replace("\n", " ⏎ ")
    return s if len(s) <= n else s[: n - 1] + "…"


def format_changes(r: dict[str, Any]) -> list[str]:
    lines = [f"photo {r['photo_id']} ({r['reason']}): {len(r['changes'])} tags"]
    for tag, (old, new) in sorted(r["changes"].items()):
        lines.append(f"    {tag}: {_short(old)} → {_short(new)}")
    for note in r.get("notes") or []:
        lines.append(f"    note: {note}")
    return lines


def run(
    settings: Settings, *, dry_run: bool, photo_ids: Sequence[int] | None = None,
    batches: Sequence[str] | None = None, limit: int | None = None,
    chunk_size: int | None = None, out: Callable[[str], None] = print,
    python: str | None = None,
) -> RunSummary:
    started = time.time()
    check_masters(settings)
    et_mod.find_exiftool(settings.EXIFTOOL_PATH)

    with db.connection() as conn:
        conn.autocommit = True
        cands = repo.load_candidates(conn, photo_ids=photo_ids, batches=batches)
    due = repo.due(cands)
    if limit:
        due = due[:limit]
    s = RunSummary(dry_run=dry_run, scope=len(cands), due=len(due))
    s.reasons.update(c.reason for c in due)
    out(f"{len(cands)} photos in scope; {len(due)} due "
        f"({', '.join(f'{k} {v}' for k, v in s.reasons.most_common()) or 'none'}).")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rdir = Path(settings.WORKING_DIR) / REPORT_SUBDIR / f"{stamp}-{'dry' if dry_run else 'write'}"
    rdir.mkdir(parents=True, exist_ok=True)
    s.report_dir = str(rdir)

    with db.connection() as conn:
        conn.autocommit = True
        run_id = db.start_job_run(conn, job_name=JOB_NAME, params={
            "dry_run": dry_run, "photo_ids": list(photo_ids or []),
            "batches": list(batches or []), "limit": limit, "due": len(due),
            "report_dir": str(rdir),
        })

    by_id = {c.photo_id: c for c in due}
    chunks = _chunks([c.photo_id for c in due], chunk_size or settings.METADATA_CHUNK)
    s.chunks = len(chunks)
    results: list[dict[str, Any]] = []
    done = 0
    for i, ids in enumerate(chunks, 1):
        ids_file = rdir / f"chunk_{i:04d}.ids.json"
        res_file = rdir / f"chunk_{i:04d}.json"
        ids_file.write_text(json.dumps(ids), encoding="utf-8")
        out(f"Chunk {i}/{len(chunks)}: {len(ids)} photos (#{ids[0]}–#{ids[-1]})")
        cmd = [python or sys.executable, "-m", "photoarchive.tools.write_metadata",
               "--chunk-worker", str(ids_file), "--out", str(res_file)]
        if dry_run:
            cmd.append("--dry-run")
        proc = _spawn(cmd)
        assert proc.stdout is not None
        for line in proc.stdout:
            out(line.rstrip("\n"))
        code = proc.wait()
        if code != 0 or not res_file.exists():
            s.failed_chunks.append(i)
            out(f"Chunk {i} exited with code {code}; its photos stay due.")
            continue
        chunk_results = json.loads(res_file.read_text(encoding="utf-8"))
        results.extend(chunk_results)
        done += len(ids)
        out(f"progress: {done}/{len(due)}")

    _aggregate(s, results, by_id)
    s.elapsed = time.time() - started
    _write_report(s, results, by_id, rdir)

    with db.connection() as conn:
        conn.autocommit = True
        db.finish_job_run(conn, job_run_id=run_id,
                          status="failed" if s.failed_chunks else "done",
                          stats=s.to_dict())
    return s


def _aggregate(s: RunSummary, results: list[dict[str, Any]],
               by_id: dict[int, repo.Candidate]) -> None:
    for r in results:
        st = r["status"]
        if st == "written":
            s.written += 1
        elif st == "would_write":
            s.would_write += 1
        elif st == "failed":
            s.failed += 1
        else:
            s.skipped += 1
        if st in ("written", "would_write"):
            s.by_tag.update(r["changes"].keys())
            if r.get("in_group"):
                s.in_group += 1
        if r.get("foreign"):
            s.foreign_files += 1
            s.foreign_by_tag.update(r["foreign"].keys())
        if r.get("foreign_region_names"):
            s.foreign_region_files += 1
        if r.get("date_mismatch"):
            s.date_mismatches += 1


def _write_report(s: RunSummary, results: list[dict[str, Any]],
                  by_id: dict[int, repo.Candidate], rdir: Path) -> None:
    with (rdir / "changes.txt").open("w", encoding="utf-8") as fh:
        for r in results:
            if r["status"] in ("written", "would_write"):
                fh.write("\n".join(format_changes(r)) + "\n")

    samples = pick_samples([by_id[r["photo_id"]] for r in results
                            if r["status"] in ("written", "would_write")
                            and r["photo_id"] in by_id])
    (rdir / "samples.json").write_text(json.dumps(
        [{"photo_id": c.photo_id, "hash": c.hash, "payload": c.payload} for c in samples],
        indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    foreign_values: Counter = Counter()
    with (rdir / "foreign_values.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["photo_id", "tag", "value"])
        for r in results:
            for tag, v in (r.get("foreign") or {}).items():
                text = _short(v, 400)
                w.writerow([r["photo_id"], tag, text])
                foreign_values[(tag, _short(v, 60))] += 1
            for name in r.get("foreign_region_names") or []:
                w.writerow([r["photo_id"], "region name", name])

    with (rdir / "date_mismatch.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["photo_id", "confirmed_date", "file_DateTimeOriginal"])
        for r in results:
            if r.get("date_mismatch"):
                w.writerow([r["photo_id"], r["date_mismatch"]["db"], r["date_mismatch"]["file"]])

    with (rdir / "skipped.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["photo_id", "status", "reason", "error"])
        for r in results:
            if r["status"] in ("skipped", "failed"):
                w.writerow([r["photo_id"], r["status"], r.get("reason"), r.get("error") or ""])

    md = [f"# Metadata {'dry run' if s.dry_run else 'write'} — {rdir.name}", "",
          s.report(), "", "## Files changing, by tag", ""]
    md += [f"- `{t}`: {n}" for t, n in s.by_tag.most_common()] or ["- none"]
    md += ["", "## Pre-existing values in tags the archive owns (not ours)", "",
           f"{s.foreign_files} files carry at least one; "
           f"{s.foreign_region_files} carry **named face regions**. "
           "Full list in `foreign_values.csv`.", ""]
    md += [f"- `{t}`: {n} files" for t, n in s.foreign_by_tag.most_common()]
    md += ["", "Most common values:", ""]
    md += [f"- `{t}` = {v!r}: {n}" for (t, v), n in foreign_values.most_common(15)]
    md += ["", "## Confirmed date ≠ the file's DateTimeOriginal", "",
           f"{s.date_mismatches} files (viewers show the camera's date for these; "
           "ours is in XMP). List in `date_mismatch.csv`.", "",
           "## Samples", "", "Ten payloads in `samples.json`: "
           + ", ".join(f"#{c.photo_id}" for c in samples) + "."]
    (rdir / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
