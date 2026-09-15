"""Bulk group assign/unassign helpers, applied against TEST_DATABASE_URL."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import DB_AVAILABLE, TEST_DATABASE_URL, requires_db

pytestmark = requires_db


def _shared() -> Path:
    return Path(__file__).resolve().parents[2] / "shared"


@pytest.fixture(scope="module")
def clean_db() -> None:
    """Migrate down to zero then back up on TEST_DATABASE_URL."""
    if not DB_AVAILABLE:
        pytest.skip("no DB")
    env = {**_env(), "DATABASE_URL": TEST_DATABASE_URL}
    for step in (["down", "0"], ["up"]):
        r = subprocess.run(
            ["node", "node_modules/node-pg-migrate/bin/node-pg-migrate.js", *step],
            cwd=str(_shared()), env=env, capture_output=True, text=True,
        )
        if r.returncode != 0:
            pytest.skip(f"migrate {step} failed: {r.stderr[:200]}")


def _env() -> dict:
    import os
    return {**os.environ}


@pytest.fixture
def pool(clean_db):
    import psycopg
    conn = psycopg.connect(TEST_DATABASE_URL)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("""
                truncate table photo_groups, group_members, groups,
                               audit_log, magic_links, access_requests, "session",
                               contribution_files, contributions,
                               suggestions, likes, comments, faces, photo_backs,
                               album_photos, albums, photo_masters, photos, users
                  restart identity cascade
            """)
        yield conn
    finally:
        conn.close()


def _mk_photo(conn, sha, batch="B"):
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into photos (sha256, mime, source_root, source_folder, source_filename, scan_batch, triage_status)
            values (%s, 'image/jpeg', 'photos', '_2005-06', %s, %s, 'keep') returning id
            """,
            (sha, sha + ".jpg", batch),
        )
        return int(cur.fetchone()[0])


def _mk_group(conn, name):
    with conn.cursor() as cur:
        cur.execute("insert into groups (name) values (%s) returning id", (name,))
        return int(cur.fetchone()[0])


def test_resolve_and_bulk_apply_scan_batch(pool):
    # Import the desktop helpers with the test DB set up already.
    import psycopg
    from photoarchive.modes.sync import bulk

    p1 = _mk_photo(pool, "s1", "Batch 00012")
    p2 = _mk_photo(pool, "s2", "Batch 00012")
    p3 = _mk_photo(pool, "s3", "Batch 00099")
    gid = _mk_group(pool, "Clay")

    ids = bulk.resolve_photo_ids(pool, scan_batch="Batch 00012")
    assert set(ids) == {p1, p2}, ids

    with pool.transaction():
        stats = bulk.bulk_apply(pool, ids, add=[gid], remove=[])
    assert stats["photos"] == 2
    assert stats["added"] >= 2

    with pool.cursor() as cur:
        cur.execute("select photo_id from photo_groups where group_id = %s and is_deleted = false order by photo_id", (gid,))
        got = [int(r[0]) for r in cur.fetchall()]
    assert got == sorted([p1, p2])
    assert p3 not in got


def test_bulk_apply_remove_soft_deletes(pool):
    from photoarchive.modes.sync import bulk
    p = _mk_photo(pool, "s10")
    g = _mk_group(pool, "Clay2")
    with pool.cursor() as cur:
        cur.execute("insert into photo_groups (photo_id, group_id) values (%s, %s)", (p, g))
    with pool.transaction():
        stats = bulk.bulk_apply(pool, [p], add=[], remove=[g])
    assert stats["removed"] == 1
    with pool.cursor() as cur:
        cur.execute("select is_deleted from photo_groups where photo_id = %s and group_id = %s", (p, g))
        assert cur.fetchone()[0] is True


def test_unfiled_ids_excludes_grouped(pool):
    from photoarchive.modes.sync import bulk
    p1 = _mk_photo(pool, "u1")
    p2 = _mk_photo(pool, "u2")
    g = _mk_group(pool, "Clay3")
    with pool.cursor() as cur:
        cur.execute("insert into photo_groups (photo_id, group_id) values (%s, %s)", (p1, g))
    ids = bulk.unfiled_ids(pool)
    assert p2 in ids
    assert p1 not in ids
