"""Push tombstones: a photo the web already holds that has since gone away.

Phase 7 answer 3. The ordinary photo selector excludes junk and private, so
without this a dedupe loser, a triage-to-junk or a split parent would stay
visible on the VM forever. The tombstone goes across once, with
`is_deleted = true`, and `tombstoned_at` stops it coming back.
"""
from __future__ import annotations

import psycopg
import pytest

# `modes.sync.__init__` re-exports the `push` function, which shadows the
# submodule of the same name — `import ... .push as x` would bind the function.
import importlib

push_mod = importlib.import_module("photoarchive.modes.sync.push")

from .conftest import DB_AVAILABLE, TEST_DATABASE_URL
from .test_cleanup_accept import _init_pool, _reset, _test_settings

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="no TEST_DATABASE_URL")


def _mk_photo(
    url: str, *, sha: str, triage_status="keep", is_private=False,
    is_deleted=False, synced=True,
) -> int:
    with psycopg.connect(url, autocommit=True) as conn:
        pid = conn.execute("""
            insert into photos
              (sha256, source_root, source_folder, source_filename, mime,
               width, height, is_scan, triage_status, is_private, is_deleted,
               working_path, synced_at, synced_file_version, file_version)
            values (%s, 'masters', '', %s, 'image/jpeg', 100, 80, true,
                    %s, %s, %s, 'w.jpg', %s, 1, 1)
            returning id
        """, (sha, f"{sha[:6]}.jpg", triage_status, is_private, is_deleted,
              "now()" if synced else None)).fetchone()[0]
        if synced:
            conn.execute("update photos set synced_at = now() where id = %s", (pid,))
        else:
            conn.execute("update photos set synced_at = null where id = %s", (pid,))
    return pid


def _selected(url: str) -> list[int]:
    with psycopg.connect(url) as conn:
        return [r["id"] for r in push_mod._select_tombstones(conn, 200)]


def test_only_previously_synced_gone_photos_are_selected(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)

    live = _mk_photo(TEST_DATABASE_URL, sha="a" * 64)
    junk = _mk_photo(TEST_DATABASE_URL, sha="b" * 64, triage_status="junk",
                     is_deleted=True)
    private = _mk_photo(TEST_DATABASE_URL, sha="c" * 64, triage_status="private",
                        is_private=True)
    soft = _mk_photo(TEST_DATABASE_URL, sha="d" * 64, is_deleted=True)
    never_synced = _mk_photo(TEST_DATABASE_URL, sha="e" * 64,
                             triage_status="junk", is_deleted=True, synced=False)

    assert _selected(TEST_DATABASE_URL) == sorted([junk, private, soft])
    assert live not in _selected(TEST_DATABASE_URL)
    # A photo the web never had needs no tombstone.
    assert never_synced not in _selected(TEST_DATABASE_URL)


def test_a_tombstone_is_sent_with_is_deleted_and_never_private(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    _mk_photo(TEST_DATABASE_URL, sha="f" * 64, triage_status="private",
              is_private=True, is_deleted=False)

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        rows = push_mod._select_tombstones(conn, 200)
    payload = push_mod._serialise_tombstone(rows[0])
    assert payload["is_deleted"] is True
    # The web rejects is_private=true with a 400, and we never reveal it.
    assert payload["is_private"] is False
    assert payload["triage_status"] == "private"


def test_marking_tombstoned_stops_it_coming_back(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    junk = _mk_photo(TEST_DATABASE_URL, sha="1" * 64, triage_status="junk",
                     is_deleted=True)
    assert _selected(TEST_DATABASE_URL) == [junk]

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        push_mod._mark_tombstoned(conn, [junk])
    # `set_updated_at` fires on that write, so a synced_at/updated_at
    # comparison could never have served as the marker.
    assert _selected(TEST_DATABASE_URL) == []
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        stamped, updated = conn.execute(
            "select tombstoned_at, updated_at from photos where id = %s",
            (junk,)).fetchone()
    assert stamped is not None
    assert stamped <= updated


def test_the_full_push_sends_tombstones_and_counts_them(tmp_path):
    """Drive `push()` against a fake client so the stage really runs."""
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    live = _mk_photo(TEST_DATABASE_URL, sha="2" * 64)
    junk = _mk_photo(TEST_DATABASE_URL, sha="3" * 64, triage_status="junk",
                     is_deleted=True)

    client = _FakeClient()
    stats = push_mod.push(
        client,
        working_dir=settings.WORKING_DIR,
        thumbs_dir=settings.THUMBS_DIR,
        state_dir=tmp_path / "state",
        allow_shared_db=True,
    )
    assert stats.tombstones_pushed == 1
    sent = [p for batch in client.photo_batches for p in batch]
    by_id = {p["id"]: p for p in sent}
    assert by_id[junk]["is_deleted"] is True
    assert by_id[live]["is_deleted"] is False
    # Second push: nothing to tombstone.
    client2 = _FakeClient()
    stats2 = push_mod.push(
        client2,
        working_dir=settings.WORKING_DIR,
        thumbs_dir=settings.THUMBS_DIR,
        state_dir=tmp_path / "state",
        allow_shared_db=True,
    )
    assert stats2.tombstones_pushed == 0


class _FakeClient:
    """The slice of WebSyncClient a push touches, with no network."""

    def __init__(self) -> None:
        self.photo_batches: list[list[dict]] = []

    # pull side
    def pull_groups(self, *a, **k):
        return {}

    def status(self):
        return {"db": {}}

    # push side
    def push_photos(self, batch):
        self.photo_batches.append(batch)
        return {"upserted": len(batch), "need_files": []}

    def push_photo_file(self, *a, **k):
        raise AssertionError("no file should be uploaded in this test")

    def push_table(self, table, rows):
        return {"upserted": len(rows)}

    def push_faces(self, rows):
        return {"upserted": len(rows)}

    def push_photo_backs(self, rows):
        return {"upserted": len(rows), "need_files": []}

    def push_back_file(self, *a, **k):
        raise AssertionError("no back file should be uploaded in this test")

    def push_face_crop(self, *a, **k):
        raise AssertionError("no crop should be uploaded in this test")

    def __getattr__(self, name):
        # Anything else a stage reaches for is a no-op upsert.
        def _noop(*a, **k):
            return {"upserted": 0, "need_files": []}
        return _noop


@pytest.fixture(autouse=True)
def _no_pull(monkeypatch):
    """`push()` pulls first, always — stub the pulls out; they have their own
    tests and would need a live web."""
    monkeypatch.setattr(push_mod, "pull_groups", lambda *a, **k: {})
    monkeypatch.setattr(push_mod, "pull_confirmed", lambda *a, **k: 0)
