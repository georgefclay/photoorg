"""Contact sheet HTML for pending back proposals.

Writes %LOCALAPPDATA%\\PhotoArchive\\reports\\backs-<ts>.html with every
pending back at ~200 px. Clicking a thumbnail toggles a "not a back"
mark. "Save marked" writes the ingest_pairings.id list to a JSON file.
Use `python -m photoarchive.tools.reject_from_contact <json>` to bulk-
reject the marked proposals.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .. import db
from ..config import load as load_config
from ..logging_setup import configure_logging

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProposalRow:
    pair_id: int
    batch: str
    back_seq: int | None
    score: float
    thumb_path: Path | None
    orphan_front: bool
    aspect_mismatch: bool
    photo_as_back: bool


def reports_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    d = Path(base) / "PhotoArchive" / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _rows(settings) -> list[ProposalRow]:
    out: list[ProposalRow] = []
    with db.connection() as conn:
        rows = conn.execute(
            """
            select ip.id, ip.staging_thumb_path, ip.back_score,
                   ip.back_source_folder, ip.back_scan_sequence,
                   ip.front_photo_id, ip.back_photo_id,
                   ip.back_aspect_mismatch,
                   coalesce(pf.scan_batch, pb.scan_batch) as batch
            from ingest_pairings ip
            left join photos pf on pf.id = ip.front_photo_id
            left join photos pb on pb.id = ip.back_photo_id
            where ip.status = 'pending'
            order by coalesce(pf.scan_batch, pb.scan_batch) nulls last,
                     ip.back_scan_sequence nulls last, ip.id
            """
        ).fetchall()
    for r in rows:
        (pid, tpath, score, folder, back_seq,
         front_pid, back_pid, aspect_mismatch, batch) = r
        # Prefer photo thumb over staging thumb when a photo-as-back
        # proposal has one; falls back to staging path.
        thumb: Path | None = None
        if back_pid is not None:
            thumb = settings.THUMBS_DIR / f"{back_pid:08d}.jpg"
            if not thumb.exists():
                thumb = None
        if thumb is None and tpath:
            thumb = Path(tpath)
        out.append(ProposalRow(
            pair_id=int(pid),
            batch=batch or (folder.split('/', 1)[0] if folder else ""),
            back_seq=(int(back_seq) if back_seq is not None else None),
            score=float(score),
            thumb_path=thumb,
            orphan_front=(front_pid is None),
            aspect_mismatch=bool(aspect_mismatch),
            photo_as_back=(back_pid is not None),
        ))
    return out


def _thumb_uri(p: Path | None) -> str:
    if p is None or not p.exists():
        return ""
    s = str(p.resolve()).replace("\\", "/")
    if not s.startswith("/"):
        s = "/" + s
    return "file://" + s


_HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Proposed backs contact sheet</title>
<style>
body { font-family: system-ui, sans-serif; background: #1e1e1e; color: #ddd;
       margin: 0; padding: 12px 16px 100px; }
h1 { margin: 4px 0 8px; font-size: 16pt; }
.toolbar { position: sticky; top: 0; background: #1e1e1e; padding: 8px 0;
           border-bottom: 1px solid #333; z-index: 10; }
button { font-size: 11pt; padding: 6px 12px; background: #2a2a2a;
         color: #ddd; border: 1px solid #555; border-radius: 4px;
         cursor: pointer; }
button:hover { background: #333; }
#marked-count { margin-left: 12px; opacity: 0.7; }
.batch-h { margin-top: 20px; padding: 4px 0; border-bottom: 1px solid #444;
           font-weight: bold; color: #aaddff; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
        gap: 8px; margin-top: 8px; }
.cell { background: #2a2a2a; border: 2px solid transparent; border-radius: 4px;
        padding: 4px; cursor: pointer; user-select: none; }
.cell:hover { border-color: #666; }
.cell.marked { border-color: #e44; background: #3a1a1a; }
.cell.marked::after { content: "NOT A BACK"; display: block; color: #f88;
                      font-weight: bold; text-align: center; margin-top: 2px; }
.cell img { display: block; width: 100%; height: 200px; object-fit: contain;
            background: #111; }
.cap { font-size: 9pt; margin-top: 4px; color: #ccc; }
.tag { display: inline-block; font-size: 8pt; background: #444; color: #ddd;
       padding: 1px 5px; border-radius: 3px; margin-right: 4px; }
</style></head><body>
"""


def render_html(rows: list[ProposalRow], out_path: Path) -> None:
    parts: list[str] = [_HTML_HEAD]
    parts.append(f'<h1>Proposed backs — {len(rows)} pending</h1>')
    parts.append('<div class="toolbar">')
    parts.append('<button onclick="saveMarked()">Save marked as JSON</button>')
    parts.append('<button onclick="clearMarked()">Clear marks</button>')
    parts.append('<span id="marked-count">0 marked</span>')
    parts.append('</div>')

    last_batch: str | None = None
    for r in rows:
        if r.batch != last_batch:
            parts.append('</div>' if last_batch is not None else '')
            parts.append(f'<div class="batch-h">{html.escape(r.batch or "(no batch)")}</div>')
            parts.append('<div class="grid">')
            last_batch = r.batch
        uri = _thumb_uri(r.thumb_path)
        tags = []
        if r.orphan_front:
            tags.append("no front")
        if r.photo_as_back:
            tags.append("photo-as-back")
        if r.aspect_mismatch:
            tags.append("aspect differs")
        tag_html = "".join(f'<span class="tag">{html.escape(t)}</span>' for t in tags)
        parts.append(
            f'<div class="cell" data-id="{r.pair_id}" onclick="toggle(this)">'
            f'<img src="{html.escape(uri)}" loading="lazy" alt="">'
            f'<div class="cap">#{r.back_seq or "?"} · '
            f'score {r.score:.2f}<br>{tag_html}</div>'
            f'</div>'
        )
    if last_batch is not None:
        parts.append('</div>')

    parts.append('''
<script>
function toggle(el) {
  el.classList.toggle("marked");
  updateCount();
}
function updateCount() {
  const n = document.querySelectorAll(".cell.marked").length;
  document.getElementById("marked-count").textContent = n + " marked";
}
function clearMarked() {
  document.querySelectorAll(".cell.marked").forEach(c => c.classList.remove("marked"));
  updateCount();
}
function saveMarked() {
  const ids = Array.from(document.querySelectorAll(".cell.marked"))
    .map(c => parseInt(c.dataset.id, 10));
  const blob = new Blob([JSON.stringify({rejected_pairing_ids: ids}, null, 2)],
                        {type: "application/json"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "backs-marked-" + new Date().toISOString().replace(/[:.]/g, "-") + ".json";
  a.click();
}
</script>
</body></html>
''')
    out_path.write_text("".join(parts), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    ap = argparse.ArgumentParser(prog="photoarchive.tools.contact_sheet")
    ap.add_argument("--open", action="store_true", default=False,
                    help="open the generated HTML in the default browser")
    ap.add_argument("--out", help="explicit output path (defaults to timestamped)")
    args = ap.parse_args(argv)

    settings = load_config()
    db.init_pool(settings)
    try:
        rows = _rows(settings)
    finally:
        db.close_pool()

    if args.out:
        out_path = Path(args.out)
    else:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = reports_dir() / f"backs-{ts}.html"
    render_html(rows, out_path)
    log.info("wrote contact sheet with %d rows to %s", len(rows), out_path)
    print(out_path)
    if args.open:
        webbrowser.open(out_path.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
