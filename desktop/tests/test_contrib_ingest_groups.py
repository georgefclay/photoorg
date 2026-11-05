"""A contributed photo is filed into the groups it was approved for.

The first real contribution (2026-11-04) arrived with "Clay Family" ticked
and approved, and ingest dropped it: `manifest.json` was never read, the
photos came in unfiled, and the push sent no bytes for them. Drives the
real `run_ingest` over a contrib root laid out the way `pull_contributions`
leaves one, then the repair tool over photos ingested before the fix.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import psycopg
import pytest

from photoarchive.config import MasterRoot

from .conftest import DB_AVAILABLE, TEST_DATABASE_URL
from .test_ingest_date_guard import _exif_photo
from .test_ingest_idempotence import _init_pool_for_url, _reset_test_db, _test_settings

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="no TEST_DATABASE_URL")


def _batch(folder, cid, files):
    """files: {file_id: approved_group_ids or None (old manifest)}"""
    entries = []
    for i, (fid, approved) in enumerate(files.items()):
        _exif_photo(folder / f"{fid}.jpg", (40 * i + 20, 90, 160 - 30 * i), None)
        e = {"id": fid, "mime": "image/jpeg"}
        if approved is not None:
            e["approved_group_ids"] = approved
        entries.append(e)
    (folder / "manifest.json").write_text(json.dumps(
        {"contribution_id": cid, "group_ids": [1, 2], "files": entries}), "utf-8")


def _setup(tmp_path, monkeypatch):
    from photoarchive.modes.ingest import service as svc
    from photoarchive.modes.ingest.guard import GuardResult, RootGuardResult
    import photoarchive.config as cfg

    contrib = tmp_path / "contrib"
    root = MasterRoot(label="tst_contrib", path=contrib, kind="contrib")
    _reset_test_db(TEST_DATABASE_URL)
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("truncate photo_groups, groups restart identity cascade")
        conn.execute("insert into groups (id, name) values (1, 'Clay Family'), (2, 'Taylors'), (3, 'Gone')")
        conn.execute("update groups set is_deleted = true, deleted_at = now() where id = 3")
    _init_pool_for_url(TEST_DATABASE_URL)
    settings = _test_settings([root], tmp_path, TEST_DATABASE_URL)
    monkeypatch.setattr(svc, "run_masters_guard", lambda roots: GuardResult(
        checked_at=datetime.now(timezone.utc).isoformat(),
        per_root=[RootGuardResult(label=r.label, path=r.path, root_writable=False,
                                  sub_writable=False, sub_path=None) for r in roots]))
    monkeypatch.setattr(cfg.Settings, "master_roots", property(lambda self: [root]))
    return contrib, root, settings


def _groups():
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        rows = conn.execute(
            "select p.source_folder || '/' || p.source_filename, pg.group_id "
            "from photos p left join photo_groups pg on pg.photo_id = p.id and not pg.is_deleted "
            "order by 1, 2").fetchall()
        audits = conn.execute(
            "select count(*) from audit_log where action = 'photo_group.contribution'").fetchone()[0]
    out: dict[str, list[int]] = {}
    for name, gid in rows:
        out.setdefault(name, [])
        if gid is not None:
            out[name].append(gid)
    return out, audits


def test_ingest_files_each_upload_into_its_approved_groups(tmp_path, monkeypatch):
    from photoarchive.modes.ingest.service import run_ingest

    contrib, root, settings = _setup(tmp_path, monkeypatch)
    # File 1 approved for both; file 2 only for Clay Family (a Taylors
    # moderator rejected it there); file 3 for a group since removed;
    # file 4 from a manifest written before per-file approval.
    _batch(contrib / "aunt-may" / "7", 7, {1: [1, 2], 2: [1], 3: [3], 4: None})
    # A batch still staged by an interrupted pull: not committed, not ingested.
    _batch(contrib / "_incoming" / "8", 8, {1: [1]})

    summary = run_ingest(settings=settings, roots=[root])

    assert summary.totals.failed == 0
    groups, audits = _groups()
    assert groups == {
        "aunt-may/7/1.jpg": [1, 2],
        "aunt-may/7/2.jpg": [1],
        "aunt-may/7/3.jpg": [],
        "aunt-may/7/4.jpg": [1, 2],
    }
    assert audits == 3


def test_repair_tool_files_photos_ingested_before_the_fix(tmp_path, monkeypatch):
    from photoarchive.modes.ingest import contrib_groups as cg
    from photoarchive.modes.ingest.service import run_ingest
    from photoarchive.tools import contrib_groups as tool

    contrib, root, settings = _setup(tmp_path, monkeypatch)
    _batch(contrib / "george" / "1", 1, {1: [1], 2: [1]})
    # Ingest as it behaved before the fix: manifest ignored.
    monkeypatch.setattr(cg, "approved_groups", lambda *a: (None, []))
    run_ingest(settings=settings, roots=[root])
    monkeypatch.undo()  # the fixed ingest is back; the tool reads the roots itself
    import photoarchive.config as cfg
    monkeypatch.setattr(cfg.Settings, "master_roots", property(lambda self: [root]))
    assert _groups()[0] == {"george/1/1.jpg": [], "george/1/2.jpg": []}

    # A human removal is a decision: give #2 a deleted row and it stays out.
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        pid2 = conn.execute("select id from photos where source_filename = '2.jpg'").fetchone()[0]
        conn.execute("insert into photo_groups (photo_id, group_id, is_deleted, deleted_at) "
                     "values (%s, 1, true, now())", (pid2,))

    report = tool.run(settings, apply=False)
    assert [(r["approved"], r["added"]) for r in report] == [([1], [])]
    assert _groups()[0]["george/1/1.jpg"] == []          # report wrote nothing

    applied = tool.run(settings, apply=True)
    assert [(r["contribution_id"], r["added"]) for r in applied] == [(1, [1])]
    groups, audits = _groups()
    assert groups == {"george/1/1.jpg": [1], "george/1/2.jpg": []}
    assert audits == 1
    assert tool.run(settings, apply=True) == []          # idempotent
