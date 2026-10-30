"""Phase 12 — take the entire archive out of this system, in open formats.

    <to>/
      README.txt          what this is, the schema version, the date, counts
      images/             every kept photo, in the download tree (export_tree.py)
        Private/          private photos, same tree — delete before handing on
        Private/backs/    backs of private photos
        manifest.csv      one line per photo (same columns as a download zip)
        MISSING.txt       photos whose working file could not be found
      backs/              {front_id:08d}_back{n}.{ext}; orphan_{back_id:08d}.{ext}
      masters/            (--masters) the original files, <root label>/<path in root>
      db/photoorg.sql     pg_dump --format=plain of photoorg
      db/csv/<table>.csv  the main tables, one CSV each
      db/web/<table>.csv  what only the website holds (comment bodies, likes,
                          users, groups, contributions, web-born suggestions);
                          UNAVAILABLE.txt instead when the site cannot be reached

Why a desktop tool and not a web page: `photoorg` is authoritative and
WORKING_DIR holds every working copy, while the VM holds files only for
grouped photos (PROJECT-PLAN Phase 12).

The rules it keeps:

* **Same tree as a download.** Placement comes from ``export_tree``, the
  Python port of ``web/services/zip-tree.js``; both are tested against
  ``shared/zip-tree-fixtures.json``.
* **Scope** is ``triage_status in ('keep','private')`` and not deleted. Junk
  stays out of ``images/`` (it is in the dump and the CSVs); ``--masters``
  still copies junk's masters, because masters are masters.
* **Masters are read-only.** The masters guard runs first and in every chunk
  that copies one; a master is only ever opened for reading.
* **Resumable, chunked, memory-safe** (CLAUDE.md, Cleanup (c)): the copy runs
  as chunks of ``EXPORT_CHUNK`` files in fresh processes; a file already at
  its destination with the same size is skipped, so a killed run continues.
  Each copy goes through ``<dest>.part`` and a rename, so a half-copied file
  never looks finished.
* **The site being down never fails the export**: the local part finishes and
  ``db/web/UNAVAILABLE.txt`` says why.
"""

from __future__ import annotations

import csv
import glob
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Iterable

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from ... import db
from ... import export_tree as tree
from ...config import Settings, _norm
from ..ingest.guard import remediation_message, run_masters_guard
from ..ingest.paths import resolve_working_path

log = logging.getLogger(__name__)

#: db/csv/ — the tables named in the Phase 12 prompt, in a stable order.
CSV_TABLES: dict[str, str] = {
    "photos": "id",
    "photo_masters": "id",
    "people": "id",
    "person_name_variants": "id",
    "faces": "id",
    "albums": "id",
    "album_photos": "album_id, photo_id",
    "places": "id",
    "photo_places": "photo_id, place_id",
    "photo_backs": "id",
    "comments": "id",
    "suggestions": "id",
    "audit_log": "id",
}

#: db/web/ — served by GET /sync/export/web_only.
WEB_TABLES = ("comments", "likes", "groups", "group_members", "users",
              "contributions", "contribution_files", "suggestions")

STATE_DIR = ".export"


class MastersWritable(RuntimeError):
    """The guard found a writable master root. Refuse to run."""


def check_masters(settings: Settings) -> None:
    guard = run_masters_guard(settings.master_roots)
    if not guard.all_read_only:
        raise MastersWritable(remediation_message(guard))


# ---------------------------------------------------------------------------
# The plan: every file to copy, as (kind, source, destination)
# ---------------------------------------------------------------------------

@dataclass
class Item:
    kind: str          # image | back | master
    src: str
    dest: str          # relative to <to>, forward slashes
    photo_id: int | None = None


@dataclass
class Plan:
    items: list[Item] = field(default_factory=list)
    photos: list[dict[str, Any]] = field(default_factory=list)
    missing: list[tuple[dict[str, Any], str]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


def _photo_rows(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            select p.id, to_char(p.capture_date, 'YYYY-MM-DD') as capture_date,
                   p.capture_date_precision as precision, p.capture_date_confirmed as confirmed,
                   p.sha256, p.mime, p.working_path, p.is_private, p.scan_batch, p.scan_sequence,
                   p.physical_ref_note, p.source_filename
              from photos p
             where p.triage_status in ('keep', 'private') and p.is_deleted = false
             order by p.capture_date nulls last, p.id
            """
        )
        rows = cur.fetchall()
        by_id = {r["id"]: r for r in rows}
        for r in rows:
            r.update(people=[], albums=[], places=[])
        cur.execute(
            """
            select distinct f.photo_id, pe.id, pe.given_name, pe.nickname, pe.surname, pe.suffix,
                   pe.display_name, pe.birth_year
              from faces f join people pe on pe.id = f.person_id
             where f.is_deleted = false and f.is_disputed = false
               and coalesce(f.review_status, 'pending') <> 'ignore' and pe.is_deleted = false
             order by f.photo_id, pe.id
            """
        )
        people: dict[int, dict[str, Any]] = {}
        for f in cur.fetchall():
            if f["photo_id"] in by_id:
                people.setdefault(f["id"], dict(f))
                by_id[f["photo_id"]]["people"].append(f["id"])
        cur.execute(
            """
            select ap.photo_id, a.name from album_photos ap join albums a on a.id = ap.album_id
             where ap.is_deleted = false and a.is_deleted = false order by ap.photo_id, lower(a.name)
            """
        )
        for a in cur.fetchall():
            if a["photo_id"] in by_id:
                by_id[a["photo_id"]]["albums"].append(a["name"])
        cur.execute(
            """
            select pp.photo_id, pl.name from photo_places pp join places pl on pl.id = pp.place_id
             where pp.is_deleted = false and pl.is_deleted = false order by pp.photo_id, lower(pl.name)
            """
        )
        for pl in cur.fetchall():
            if pl["photo_id"] in by_id:
                by_id[pl["photo_id"]]["places"].append(pl["name"])
    folders = tree.people_folders(people.values())
    for r in rows:
        r["people_names"] = [tree.common_name(people[pid]) for pid in r["people"]]
        r["people_folders"] = [folders[pid] for pid in r["people"]]
    return rows


def _ext(path: str | None, mime: str | None) -> str:
    suffix = Path(path).suffix if path else ""
    return tree.norm_ext(suffix or {"image/tiff": "tif", "image/png": "png"}.get(mime or "", "jpg"))


def master_dest(settings: Settings, master_path: str) -> str:
    """masters/<root label>/<path inside the root>; anything outside every
    configured root keeps its drive and path under masters/_other/."""
    mp = _norm(Path(master_path))
    for root in settings.master_roots:
        rp = _norm(root.path)
        try:
            rel = mp.relative_to(rp)
        except ValueError:
            continue
        return "masters/" + "/".join((root.label, *rel.parts))
    parts = PureWindowsPath(master_path).parts
    clean = [p.replace(":", "").strip("\\/") for p in parts if p.strip("\\/")]
    return "masters/_other/" + "/".join(clean)


def build_plan(settings: Settings, *, masters: bool = False, people_tree: bool = False) -> Plan:
    plan = Plan()
    wd = Path(settings.WORKING_DIR)
    with db.connection() as conn:
        conn.autocommit = True
        rows = _photo_rows(conn)
        for r in rows:
            r["ext"] = _ext(r["working_path"], r["mime"])
            src = resolve_working_path(wd, r["working_path"])
            if src is None or not src.is_file():
                plan.missing.append((r, "working file not found"))
                continue
            prefix = "images/Private/" if r["is_private"] else "images/"
            r["export_paths"] = [prefix + tree.photo_path(r)]
            if people_tree:
                r["export_paths"] += [prefix + tree.people_path(r, f) for f in r["people_folders"]]
            for d in r["export_paths"]:
                plan.items.append(Item("image", str(src), d, r["id"]))
        plan.photos = rows

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                select b.id, b.photo_id, b.working_path, p.is_private
                  from photo_backs b
                  left join photos p on p.id = b.photo_id
                 where b.photo_id is null
                    or (p.triage_status in ('keep', 'private') and p.is_deleted = false)
                 order by b.photo_id nulls last, b.id
                """
            )
            backs = cur.fetchall()
        n_on_front: dict[int, int] = {}
        backs_done = backs_missing = 0
        for b in backs:
            src = resolve_working_path(wd, b["working_path"])
            if src is None or not src.is_file():
                backs_missing += 1
                continue
            ext = _ext(str(src), None)
            if b["photo_id"] is None:
                dest = f"backs/orphan_{b['id']:08d}.{ext}"
            else:
                n = n_on_front[b["photo_id"]] = n_on_front.get(b["photo_id"], 0) + 1
                base = "images/Private/backs/" if b["is_private"] else "backs/"
                dest = f"{base}{b['photo_id']:08d}_back{n}.{ext}"
            plan.items.append(Item("back", str(src), dest, b["photo_id"]))
            backs_done += 1

        n_masters = 0
        if masters:
            with conn.cursor() as cur:
                cur.execute("select distinct master_path from photo_masters order by master_path")
                for (mp,) in cur.fetchall():
                    plan.items.append(Item("master", mp, master_dest(settings, mp)))
                    n_masters += 1

    plan.counts = {
        "photos": len(rows),
        "private": sum(1 for r in rows if r["is_private"]),
        "images": sum(1 for i in plan.items if i.kind == "image"),
        "missing": len(plan.missing),
        "backs": backs_done,
        "backs_missing": backs_missing,
        "masters": n_masters,
    }
    return plan


# ---------------------------------------------------------------------------
# Copying (inside a chunk process)
# ---------------------------------------------------------------------------

def copy_one(src: Path, dest: Path) -> str:
    """'copied' | 'skipped' (same size already there) | 'missing'.

    Reads `src` only. The bytes go to `<dest>.part` and are renamed into
    place, so an interrupted copy never looks finished."""
    try:
        size = src.stat().st_size
    except FileNotFoundError:
        return "missing"
    try:
        if dest.stat().st_size == size:
            return "skipped"
    except FileNotFoundError:
        pass
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    with open(src, "rb") as fin, open(part, "wb") as fout:
        shutil.copyfileobj(fin, fout, 4 * 1024 * 1024)
    shutil.copystat(src, part)
    os.replace(part, dest)
    return "copied"


def run_chunk(settings: Settings, to: Path, items: list[dict[str, Any]],
              out: Callable[[str], None] = print) -> dict[str, int]:
    if any(i["kind"] == "master" for i in items):
        check_masters(settings)
    counts = {"copied": 0, "skipped": 0, "missing": 0, "failed": 0, "bytes": 0}
    for it in items:
        dest = to / it["dest"]
        try:
            r = copy_one(Path(it["src"]), dest)
        except OSError as e:
            counts["failed"] += 1
            out(f"  failed: {it['src']} -> {it['dest']}: {e}")
            continue
        counts[r] += 1
        if r == "copied":
            counts["bytes"] += dest.stat().st_size
        elif r == "missing":
            out(f"  missing: {it['src']}")
    return counts


# ---------------------------------------------------------------------------
# The database: pg_dump, CSVs, the web-only tables
# ---------------------------------------------------------------------------

def find_pg_dump(settings: Settings) -> str:
    if settings.PG_DUMP_PATH:
        return settings.PG_DUMP_PATH
    found = shutil.which("pg_dump")
    if found:
        return found
    cands = sorted(glob.glob(r"C:\Program Files\PostgreSQL\*\bin\pg_dump.exe"),
                   key=lambda p: int(Path(p).parts[-3]) if Path(p).parts[-3].isdigit() else 0)
    if cands:
        return cands[-1]
    raise FileNotFoundError("pg_dump not found: set PG_DUMP_PATH in desktop/.env")


def dump_sql(settings: Settings, dest: Path) -> None:
    """`pg_dump --format=plain`. The password travels in the environment,
    never on the command line."""
    info = conninfo_to_dict(settings.DATABASE_URL)
    env = dict(os.environ)
    if info.get("password"):
        env["PGPASSWORD"] = str(info["password"])
    cmd = [find_pg_dump(settings), "--format=plain", "--no-owner", "--no-privileges",
           "--encoding=UTF8", "-f", str(dest.with_name(dest.name + ".part"))]
    for k, flag in (("host", "-h"), ("port", "-p"), ("user", "-U")):
        if info.get(k):
            cmd += [flag, str(info[k])]
    cmd.append(str(info.get("dbname") or "photoorg"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(cmd, env=env, capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {r.stderr.strip()[-400:]}")
    os.replace(dest.with_name(dest.name + ".part"), dest)


def dump_csvs(dest_dir: Path) -> dict[str, int]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, int] = {}
    with db.connection() as conn:
        conn.autocommit = True
        for table, order in CSV_TABLES.items():
            part = dest_dir / f"{table}.csv.part"
            with conn.cursor() as cur, open(part, "wb") as f:
                with cur.copy(
                    f"copy (select * from {table} order by {order}) to stdout with (format csv, header)"
                ) as cp:
                    for chunk in cp:
                        f.write(chunk)
            os.replace(part, dest_dir / f"{table}.csv")
            with conn.cursor() as cur:
                cur.execute(f"select count(*) from {table}")
                out[table] = int(cur.fetchone()[0])
    return out


def _cell(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v


def dump_web(settings: Settings, dest_dir: Path, client=None) -> dict[str, Any]:
    """db/web/<table>.csv from /sync/export/web_only. An unreachable or
    refusing site writes UNAVAILABLE.txt and never fails the export."""
    from ..sync.client import WebSyncClient
    dest_dir.mkdir(parents=True, exist_ok=True)
    unavailable = dest_dir / "UNAVAILABLE.txt"
    try:
        client = client or WebSyncClient(settings.WEB_API_URL, settings.WEB_API_TOKEN)
        counts: dict[str, int] = {}
        for table in WEB_TABLES:
            rows: list[dict[str, Any]] = []
            offset: int | None = 0
            while offset is not None:
                page = client.export_web_only(table, offset=offset, limit=1000)
                rows.extend(page.get("rows") or [])
                offset = page.get("next_offset")
            part = dest_dir / f"{table}.csv.part"
            with open(part, "w", encoding="utf-8", newline="") as f:
                cols: list[str] = []
                for r in rows:
                    cols += [k for k in r if k not in cols]
                w = csv.DictWriter(f, fieldnames=cols)
                if cols:
                    w.writeheader()
                for r in rows:
                    w.writerow({k: _cell(v) for k, v in r.items()})
            os.replace(part, dest_dir / f"{table}.csv")
            counts[table] = len(rows)
        if unavailable.exists():
            unavailable.unlink()
        return {"ok": True, "counts": counts}
    except Exception as e:  # the site being down must not fail the export
        reason = f"{type(e).__name__}: {e}"
        unavailable.write_text(
            "The website could not be read when this export was made, so the\n"
            "tables only it holds (comment bodies, likes, users, groups,\n"
            "contributions, web-born suggestions) are not in this folder.\n"
            f"When: {datetime.now().isoformat(timespec='seconds')}\n"
            f"Why:  {reason}\n"
            "Run the export again when the site is up; it skips every file it already has.\n",
            encoding="utf-8",
        )
        return {"ok": False, "reason": reason}


def schema_version() -> str:
    with db.connection() as conn:
        conn.autocommit = True
        row = conn.execute("select name from pgmigrations order by run_on desc, id desc limit 1").fetchone()
    return row[0] if row else "unknown"


# ---------------------------------------------------------------------------
# README, manifest, MISSING
# ---------------------------------------------------------------------------

def scan_locator(r: dict[str, Any]) -> str:
    if not r.get("scan_batch") and r.get("scan_sequence") is None:
        return ""
    seq = f" #{int(r['scan_sequence']):03d}" if r.get("scan_sequence") is not None else ""
    return f"{r.get('scan_batch') or ''}{seq}".strip()


def _confirmed_date(r: dict[str, Any]) -> str:
    ok = r.get("confirmed") and r.get("capture_date") and r.get("precision") not in (None, "unknown")
    return r["capture_date"] if ok else ""


def write_manifest(to: Path, plan: Plan) -> None:
    dest = to / "images" / "manifest.csv"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\r\n")
        w.writerow(["photo_id", "path", "master_sha256", "scan_locator", "capture_date",
                    "date_precision", "people", "albums", "places", "private", "in_export"])
        missing_ids = {r["id"] for r, _ in plan.missing}
        for r in sorted(plan.photos, key=lambda x: x["id"]):
            paths = [p.removeprefix("images/") for p in r.get("export_paths", [])]
            d = _confirmed_date(r)
            w.writerow([
                r["id"], " ; ".join(paths), r["sha256"],
                " | ".join(x for x in (scan_locator(r), r.get("physical_ref_note")) if x),
                d, r["precision"] if d else "", "; ".join(r["people_names"]),
                "; ".join(r["albums"]), "; ".join(r["places"]),
                "yes" if r["is_private"] else "", "missing" if r["id"] in missing_ids else "yes",
            ])
    miss = to / "images" / "MISSING.txt"
    if plan.missing:
        lines = ["Photos in the archive whose working file was not found when this export was",
                 "made. Run `python -m photoarchive.tools.check_working_files`, then export again.", ""]
        for r, why in plan.missing:
            lines.append(f"#{r['id']}  {_confirmed_date(r) or 'undated'}  "
                         f"{scan_locator(r) or r.get('source_filename') or ''}  ({why})")
        miss.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    elif miss.exists():
        miss.unlink()


def write_readme(to: Path, plan: Plan, summary: "ExportSummary") -> None:
    c = plan.counts
    lines = [
        "Family photo archive — full export",
        "===================================",
        "",
        f"Made:            {datetime.now().strftime('%Y-%m-%d %H:%M')} (laptop time)",
        f"Schema version:  {summary.schema_version} (the last migration in shared/migrations)",
        f"Database:        photoorg on the laptop (the authoritative copy)",
        "",
        "Counts",
        f"  Photos (kept and private):  {c.get('photos', 0)}  ({c.get('private', 0)} private)",
        f"  Image files in images/:     {c.get('images', 0)}",
        f"  Listed in images/MISSING.txt: {c.get('missing', 0)}",
        f"  Backs of prints:            {c.get('backs', 0)}",
        f"  Masters copied:             {c.get('masters', 0) if summary.masters else 'not requested (--masters)'}",
        "",
        "Layout",
        "  images/      every kept photo, filed by its confirmed date:",
        "                 YYYY/MM/   the day or the month is known",
        "                 YYYY/      only the year is known (no month is invented)",
        "                 1960s/     only the decade is known",
        "                 Unsorted/  no confirmed date",
        "               File names are <date>_<photo id>.<ext>. Every image carries its",
        "               own metadata (people, date, places, albums, the back's text, the",
        "               print's batch and number). images/manifest.csv lists every photo.",
        "  images/Private/  private photos, same tree (and their backs).",
        "  backs/       scans of the backs of prints: <front id>_back<n>, orphan_<back id>.",
        "  masters/     (only with --masters) the original files, by master root.",
        "  db/photoorg.sql  the whole database (pg_dump --format=plain). Restore with",
        "                   createdb <name> && psql -d <name> -f db/photoorg.sql",
        "  db/csv/      the main tables as CSV, readable in any spreadsheet.",
        "  db/web/      what only the website holds: comment bodies, likes, users,",
        "               groups, contributions, family suggestions made on the site.",
        "",
        "HANDING A COPY TO A RELATIVE",
        "  Give them images/ — after deleting images/Private/.",
        "  Never give anyone db/: it holds personal data (email addresses) and is",
        "  George's own backup of the system.",
        "",
    ]
    if summary.web and not summary.web.get("ok"):
        lines += ["NOTE: the website could not be read; see db/web/UNAVAILABLE.txt.", ""]
    (to / "README.txt").write_text("\r\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# The whole run
# ---------------------------------------------------------------------------

@dataclass
class ExportSummary:
    to: Path
    masters: bool = False
    schema_version: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    copied: int = 0
    skipped: int = 0
    missing: int = 0
    failed: int = 0
    bytes_copied: int = 0
    chunks: int = 0
    failed_chunks: list[int] = field(default_factory=list)
    csv_rows: dict[str, int] = field(default_factory=dict)
    web: dict[str, Any] = field(default_factory=dict)
    elapsed: float = 0.0
    total_bytes: int = 0

    def report(self) -> str:
        gb = self.total_bytes / 1024 ** 3
        lines = [
            f"Export to {self.to}: {self.counts.get('photos', 0)} photos, "
            f"{self.counts.get('images', 0)} image files, {self.counts.get('backs', 0)} backs"
            + (f", {self.counts.get('masters', 0)} masters" if self.masters else "") + ".",
            f"Copied {self.copied}, already there {self.skipped}, missing {self.missing}, "
            f"failed {self.failed} — in {self.elapsed:.0f} s ({self.chunks} chunks).",
            f"Folder size {gb:.1f} GB. Schema {self.schema_version}.",
            "Database: db/photoorg.sql + " + ", ".join(f"{k} {v}" for k, v in self.csv_rows.items()),
            ("Website tables: " + ", ".join(f"{k} {v}" for k, v in (self.web.get("counts") or {}).items()))
            if self.web.get("ok") else f"Website tables NOT exported: {self.web.get('reason')}",
        ]
        if self.counts.get("missing"):
            lines.append(f"{self.counts['missing']} photos had no working file: images/MISSING.txt")
        if self.failed_chunks:
            lines.append(f"Chunks that did not finish: {self.failed_chunks} — run again to continue.")
        return "\n".join(lines)


def _spawn(cmd: list[str]) -> subprocess.Popen[str]:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def folder_bytes(root: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


def run(settings: Settings, to: Path, *, masters: bool = False, people_tree: bool = False,
        chunk_size: int | None = None, out: Callable[[str], None] = print,
        web_client=None, in_process: bool = False) -> ExportSummary:
    """The whole export. `in_process` copies in this process (tests only)."""
    started = time.monotonic()
    to = Path(to)
    to.mkdir(parents=True, exist_ok=True)
    s = ExportSummary(to=to, masters=masters)
    if masters:
        check_masters(settings)

    out("Planning…")
    plan = build_plan(settings, masters=masters, people_tree=people_tree)
    s.counts = plan.counts
    out(f"{plan.counts['photos']} photos, {len(plan.items)} files to place"
        f" ({plan.counts['missing']} photos without a working file).")

    state = to / STATE_DIR
    state.mkdir(exist_ok=True)
    n = chunk_size or settings.EXPORT_CHUNK
    chunks = [plan.items[i:i + n] for i in range(0, len(plan.items), n)]
    s.chunks = len(chunks)
    for i, items in enumerate(chunks, 1):
        ids_file = state / f"chunk_{i:04d}.items.json"
        res_file = state / f"chunk_{i:04d}.result.json"
        ids_file.write_text(json.dumps([it.__dict__ for it in items], ensure_ascii=False), encoding="utf-8")
        if res_file.exists():
            res_file.unlink()
        if in_process:
            res = run_chunk(settings, to, [it.__dict__ for it in items], out=out)
        else:
            cmd = [sys.executable, "-m", "photoarchive.tools.export_archive",
                   "--to", str(to), "--chunk-worker", str(ids_file), "--out", str(res_file)]
            proc = _spawn(cmd)
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    out(line.rstrip())
            except BaseException:
                # Stopped (the app closing raises from `out`): end the chunk
                # too. Its half-copied file is a .part; the next run redoes it.
                proc.kill()
                proc.wait()
                raise
            if proc.wait() != 0 or not res_file.exists():
                s.failed_chunks.append(i)
                out(f"Chunk {i}/{len(chunks)} did not finish (exit {proc.returncode}).")
                continue
            res = json.loads(res_file.read_text(encoding="utf-8"))
        s.copied += res["copied"]
        s.skipped += res["skipped"]
        s.missing += res["missing"]
        s.failed += res["failed"]
        s.bytes_copied += res["bytes"]
        done = sum(len(c) for c in chunks[:i])
        out(f"Chunk {i}/{len(chunks)}: {done}/{len(plan.items)} files — copied {s.copied}, "
            f"already there {s.skipped}, missing {s.missing}, failed {s.failed}")

    out("Dumping the database (pg_dump)…")
    dump_sql(settings, to / "db" / "photoorg.sql")
    out("Writing db/csv/…")
    s.csv_rows = dump_csvs(to / "db" / "csv")
    out("Reading the website's own tables…")
    s.web = dump_web(settings, to / "db" / "web", client=web_client)
    s.schema_version = schema_version()
    write_manifest(to, plan)
    write_readme(to, plan, s)
    s.elapsed = time.monotonic() - started
    s.total_bytes = folder_bytes(to)
    return s
