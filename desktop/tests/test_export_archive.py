"""Phase 12 — the full export: tree, backs, private, masters, db/, resumability.

The tree itself is pinned by ``test_export_tree.py`` against the fixtures
the web's zip shares; this checks the tool puts real files where the tree
says, produces the SQL and CSVs, survives the website being down, and skips
what is already in place on a second run.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from photoarchive import db as dbmod
from photoarchive.modes.export import archive

from .phase6_fixtures import insert_back, insert_master, insert_photo, phase6  # noqa: F401


class FakeWeb:
    def __init__(self, tables: dict[str, list[dict]] | None = None, fail: bool = False):
        self.tables = tables or {}
        self.fail = fail

    def export_web_only(self, table, offset=0, limit=1000):
        if self.fail:
            raise ConnectionError("site down")
        rows = self.tables.get(table, [])
        page = rows[offset:offset + limit]
        return {"rows": page, "next_offset": offset + limit if offset + limit < len(rows) else None}


def _seed(settings) -> dict:
    wd = Path(settings.WORKING_DIR)
    ids: dict[str, int] = {}
    with dbmod.connection() as conn:
        conn.autocommit = True

        def photo(name, *, date=None, precision=None, confirmed=False, private=False,
                  triage="keep", ext="jpg", write=True, seq=None):
            pid = insert_photo(conn, triage_status="private" if private else triage, scan_sequence=seq)
            fname = f"{pid:08d}_abcdef12.{ext}"
            if write:
                (wd / fname).write_bytes(f"pixels-{name}".encode())
            conn.execute(
                """update photos set working_path = %s, capture_date = %s, capture_date_precision = coalesce(%s, 'unknown')::date_precision,
                          capture_date_confirmed = %s, is_private = %s, scan_batch = 'Batch 00012'
                    where id = %s""",
                (str(wd / fname), date, precision, confirmed, private, pid))
            ids[name] = pid
            return pid

        photo("exact", date="1962-07-04", precision="exact", confirmed=True, seq=17)
        photo("year", date="1975-01-01", precision="year", confirmed=True)
        photo("decade", date="1964-01-01", precision="decade", confirmed=True, ext="tif")
        photo("pending", date="1981-03-01", precision="exact", confirmed=False)
        photo("private", date="1990-05-01", precision="month", confirmed=True, private=True)
        photo("junk", triage="junk")
        photo("nofile", write=False)
        pe = conn.execute(
            "insert into people (given_name, nickname, surname) values ('Margaret', 'Peggy', 'Clay') returning id"
        ).fetchone()[0]
        conn.execute("""insert into faces (photo_id, person_id, bbox, source)
                        values (%s, %s, '{"x":1,"y":1,"w":9,"h":9}', 'human')""", (ids["exact"], pe))
        aid = conn.execute("insert into albums (name) values ('Album 6') returning id").fetchone()[0]
        conn.execute("insert into album_photos (album_id, photo_id, position) values (%s, %s, 1)", (aid, ids["exact"]))
        (wd / "back_a.jpg").write_bytes(b"back-a")
        (wd / "back_b.jpg").write_bytes(b"back-b")
        (wd / "back_o.jpg").write_bytes(b"back-o")
        (wd / "back_p.jpg").write_bytes(b"back-p")
        insert_back(conn, photo_id=ids["exact"], working_path=str(wd / "back_a.jpg"))
        insert_back(conn, photo_id=ids["exact"], working_path=str(wd / "back_b.jpg"))
        ids["orphan_back"] = insert_back(conn, photo_id=None, working_path=str(wd / "back_o.jpg"))
        insert_back(conn, photo_id=ids["private"], working_path=str(wd / "back_p.jpg"))
        masters_root = Path(settings.master_roots[0].path)
        (masters_root / "Batch 00012").mkdir(parents=True, exist_ok=True)
        (masters_root / "Batch 00012" / "0017.jpg").write_bytes(b"master-exact")
        insert_master(conn, ids["exact"], master_path=str(masters_root / "Batch 00012" / "0017.jpg"))
        (masters_root / "junk.jpg").write_bytes(b"master-junk")
        insert_master(conn, ids["junk"], master_path=str(masters_root / "junk.jpg"))
    return ids


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and ".export" not in p.parts}


def test_export_tree_backs_private_db_and_resume(phase6, tmp_path):
    settings = phase6
    ids = _seed(settings)
    to = tmp_path / "export"
    web = FakeWeb({"comments": [{"id": 1, "photo_id": ids["exact"], "body": "That's Peggy, 1962", "is_hidden": False}],
                   "users": [{"id": 1, "email": "a@example.com", "role": "admin"}]})

    s = archive.run(settings, to, web_client=web, out=lambda _m: None)  # real chunk processes
    f = _files(to)
    p8 = lambda k: f"{ids[k]:08d}"  # noqa: E731
    assert f"images/1962/07/1962-07-04_{p8('exact')}.jpg" in f
    assert f"images/1975/1975_{p8('year')}.jpg" in f, "year-only: top of the year folder"
    assert f"images/1960s/1960s_{p8('decade')}.tif" in f
    assert f"images/Unsorted/undated_{p8('pending')}.jpg" in f, "an unconfirmed date is not a date"
    assert f"images/Private/1990/05/1990-05_{p8('private')}.jpg" in f
    assert not any(p8("junk") in x for x in f if x.startswith("images/")), "junk stays out of images/"
    assert f"backs/{p8('exact')}_back1.jpg" in f and f"backs/{p8('exact')}_back2.jpg" in f
    assert f"backs/orphan_{ids['orphan_back']:08d}.jpg" in f
    assert f"images/Private/backs/{p8('private')}_back1.jpg" in f, "a private photo's back stays private"
    assert not any(x.startswith("masters/") for x in f), "masters only on request"
    assert (to / f"images/1962/07/1962-07-04_{p8('exact')}.jpg").read_bytes() == b"pixels-exact"

    missing = (to / "images" / "MISSING.txt").read_text(encoding="utf-8")
    assert f"#{ids['nofile']}" in missing
    manifest = list(csv.DictReader(io.StringIO((to / "images" / "manifest.csv").read_text(encoding="utf-8"))))
    row = next(r for r in manifest if r["photo_id"] == str(ids["exact"]))
    assert row["people"] == "Peggy Clay" and row["albums"] == "Album 6"
    assert row["scan_locator"] == "Batch 00012 #017" and row["capture_date"] == "1962-07-04"
    assert next(r for r in manifest if r["photo_id"] == str(ids["private"]))["private"] == "yes"

    sql = (to / "db" / "photoorg.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE public.photos" in sql and "COPY public.photos" in sql
    for t in archive.CSV_TABLES:
        assert (to / "db" / "csv" / f"{t}.csv").exists(), t
    photos_csv = list(csv.DictReader(open(to / "db" / "csv" / "photos.csv", encoding="utf-8")))
    assert len(photos_csv) == 7, "the CSVs hold every row, junk included"
    comments = list(csv.DictReader(open(to / "db" / "web" / "comments.csv", encoding="utf-8")))
    assert comments[0]["body"] == "That's Peggy, 1962"
    assert not (to / "db" / "web" / "UNAVAILABLE.txt").exists()

    readme = (to / "README.txt").read_text(encoding="utf-8")
    assert "Schema version:" in readme and "phase-12-download-export" in readme
    assert "deleting images/Private/" in readme and "Never give anyone db/" in readme
    assert s.copied == len([x for x in f if x.startswith(("images/", "backs/")) and x.endswith((".jpg", ".tif"))])
    assert s.failed == 0 and not s.failed_chunks

    # Resumable: a second run copies nothing; a removed file comes back.
    (to / f"images/1975/1975_{p8('year')}.jpg").unlink()
    s2 = archive.run(settings, to, web_client=web, out=lambda _m: None, in_process=True)
    assert s2.copied == 1 and s2.skipped == s.copied - 1
    assert not list(to.rglob("*.part"))


def test_masters_are_copied_under_their_root_label(phase6, tmp_path, monkeypatch):
    settings = phase6
    _seed(settings)
    monkeypatch.setattr(archive, "check_masters", lambda _s: None)  # tmp roots are writable
    to = tmp_path / "export"
    archive.run(settings, to, masters=True, web_client=FakeWeb(), out=lambda _m: None, in_process=True)
    assert (to / "masters" / "m1" / "Batch 00012" / "0017.jpg").read_bytes() == b"master-exact"
    assert (to / "masters" / "m1" / "junk.jpg").exists(), "masters are masters, junk's included"
    master = Path(settings.master_roots[0].path) / "Batch 00012" / "0017.jpg"
    assert master.read_bytes() == b"master-exact", "the master is untouched"


def test_masters_guard_refuses_a_writable_root(phase6, tmp_path):
    with pytest.raises(archive.MastersWritable):
        archive.run(phase6, tmp_path / "export", masters=True, web_client=FakeWeb(), out=lambda _m: None)


def test_the_site_being_down_never_fails_the_export(phase6, tmp_path):
    _seed(phase6)
    to = tmp_path / "export"
    s = archive.run(phase6, to, web_client=FakeWeb(fail=True), out=lambda _m: None, in_process=True)
    note = (to / "db" / "web" / "UNAVAILABLE.txt").read_text(encoding="utf-8")
    assert "site down" in note
    assert (to / "db" / "photoorg.sql").exists() and (to / "README.txt").exists()
    assert s.web["ok"] is False
    assert "website could not be read" in (to / "README.txt").read_text(encoding="utf-8")


def test_master_dest_outside_every_root(phase6):
    assert archive.master_dest(phase6, r"Q:\Old Scans\a.jpg") == "masters/_other/Q/Old Scans/a.jpg"
