"""Phase 6 fix-up 11 — bare working_path rows.

Covers:
  * the ONE resolver (absolute stays, bare joins WORKING_DIR by basename);
  * every uploader opens the WORKING_DIR-joined absolute path;
  * a missing file yields one failed job_items row and the rest of the
    batch still hands over; all-missing never starts the mini;
  * check_working_files rewrites bare photo + back rows to absolute paths
    with an audit row each, and the post-condition counts hit 0;
  * the push pre-flight refuses a web that shares our database.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from photoarchive import db as dbmod
from photoarchive.inference_client.base import BatchStartResult, BatchUploadResult
from photoarchive.jobs.base import (
    JobContext,
    SelectedItem,
    WorkingFileUploader,
    hand_over,
    last_skipped,
)
from photoarchive.jobs.classify import make_job as make_classify
from photoarchive.modes.ingest import paths as ingest_paths
from photoarchive.modes.sync.push import SharedDatabaseError, guard_not_shared_database
from photoarchive.tools import check_working_files

from .phase6_fixtures import (  # noqa: F401
    insert_back, insert_master, insert_photo, phase6, write_test_jpeg,
)


# ---- resolver ------------------------------------------------------------


def test_resolver_keeps_absolute_and_joins_bare(tmp_path):
    wd = tmp_path / "working"
    absolute = tmp_path / "elsewhere" / "00000001_abcdef01.jpg"
    assert ingest_paths.resolve_working_path(wd, str(absolute)) == absolute
    assert ingest_paths.resolve_working_path(wd, "00000001_abcdef01.jpg") == wd / "00000001_abcdef01.jpg"
    # A relative path with directories is reduced to its basename — never a traversal.
    assert ingest_paths.resolve_working_path(wd, "../../x/00000002_deadbeef.jpg") == wd / "00000002_deadbeef.jpg"
    assert ingest_paths.resolve_working_path(wd, None) is None
    assert ingest_paths.resolve_working_path(wd, "") is None
    assert ingest_paths.is_absolute_working_path(str(absolute))
    assert not ingest_paths.is_absolute_working_path("00000001_abcdef01.jpg")
    assert not ingest_paths.is_absolute_working_path(None)


# ---- uploader ------------------------------------------------------------


def test_uploader_opens_working_dir_joined_path(phase6):
    """A bare working_path resolves to WORKING_DIR/<name>; an absolute one
    is used verbatim. The uploader never builds a name from the scheme."""
    bare = "00000042_0badcafe.jpg"
    expected = Path(phase6.WORKING_DIR) / bare
    write_test_jpeg(expected)

    up = WorkingFileUploader()
    ref = up.prepare(SelectedItem(ref="42", photo_id=42, working_path=bare), 1024)
    assert ref.path == expected
    assert ref.path.is_absolute()

    ref2 = up.prepare(SelectedItem(ref="43", photo_id=43, working_path=str(expected)), 1024)
    assert ref2.path == expected

    with pytest.raises(FileNotFoundError):
        up.prepare(SelectedItem(ref="44", photo_id=44, working_path=""), 1024)


# ---- hand_over: skip, never abort ----------------------------------------


class RecordingClient:
    def __init__(self) -> None:
        self.uploads: list[list] = []
        self.starts: list[tuple[str, str]] = []

    def upload(self, job_name, images, *, endpoint_edge):
        imgs = list(images)
        self.uploads.append(imgs)
        return BatchUploadResult(accepted=len(imgs), rejected=[])

    def start_from_inbox(self, job_name, endpoint):
        self.starts.append((job_name, endpoint))
        return BatchStartResult(job_name=job_name, endpoint=endpoint, accepted=True,
                                already_running=False, raw={})

    def summary(self, job_name):
        raise RuntimeError("not used")


def _job_items(run_id: int) -> list[tuple]:
    with dbmod.connection() as conn:
        conn.autocommit = True
        return conn.execute(
            "select photo_id, status, error from job_items where job_run_id = %s order by photo_id",
            (run_id,),
        ).fetchall()


def test_missing_file_is_skipped_and_rest_hands_over(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        good_bare = "00000001_11111111.jpg"
        write_test_jpeg(Path(phase6.WORKING_DIR) / good_bare)
        p_good = insert_photo(conn, working_path=good_bare)          # bare, file present
        p_missing = insert_photo(conn, working_path="00000002_22222222.jpg")  # bare, no file
        p_garbage = insert_photo(conn, working_path=str(Path(phase6.WORKING_DIR) / "bad.jpg"))
        (Path(phase6.WORKING_DIR) / "bad.jpg").write_bytes(b"not a jpeg at all")
        conn.commit()

    client = RecordingClient()
    ctx = JobContext(client=client, model="m", prompt_version="v1")
    progress = []
    summary = hand_over(make_classify(), ctx, progress_cb=progress.append)

    assert summary.selected == 3
    assert summary.uploaded == 1
    assert summary.skipped == 2
    assert set(summary.skipped_refs) == {str(p_missing), str(p_garbage)}
    assert summary.started is True
    assert summary.error is None
    assert summary.report() == "1 uploaded, 2 skipped (missing file)"

    # Only the good photo went over the wire, with pre-downscaled bytes and
    # the WORKING_DIR-joined absolute path.
    assert len(client.uploads) == 1 and len(client.uploads[0]) == 1
    sent = client.uploads[0][0]
    assert sent.ref == str(p_good)
    assert sent.path == Path(phase6.WORKING_DIR) / good_bare
    assert sent.prepared_bytes and sent.prepared_bytes[:2] == b"\xff\xd8"
    assert client.starts == [("classify", "classify")]

    # One failed job_items row per skipped photo, with the error text.
    items = _job_items(summary.job_run_id)
    assert [(pid, st) for pid, st, _ in items] == [(p_missing, "failed"), (p_garbage, "failed")]
    assert "FileNotFoundError" in items[0][2]
    assert items[1][2]  # undecodable → some error text

    # The Jobs panel's button reads the same list back from job_runs.
    assert set(last_skipped()["classify"]) == {str(p_missing), str(p_garbage)}

    # Skipped photos never reached photo_job_status, so the next Run
    # re-selects them (the good one was uploaded but not yet collected).
    with dbmod.connection() as conn:
        conn.autocommit = True
        n = conn.execute("select count(*) from photo_job_status").fetchone()[0]
    assert n == 0


def test_all_missing_does_not_start_the_mini(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        p = insert_photo(conn, working_path="00000009_99999999.jpg")
        conn.commit()

    client = RecordingClient()
    ctx = JobContext(client=client, model="m", prompt_version="v1")
    summary = hand_over(make_classify(), ctx)
    assert summary.uploaded == 0 and summary.skipped == 1
    assert summary.started is False
    assert summary.error is None
    assert client.uploads == [] and client.starts == []
    assert _job_items(summary.job_run_id) == [(p, "failed", _job_items(summary.job_run_id)[0][2])]


# ---- check_working_files: bare rows → absolute ---------------------------


def _audit_rows(action_prefix: str) -> list[tuple]:
    with dbmod.connection() as conn:
        conn.autocommit = True
        return conn.execute(
            "select entity_type, entity_id, action, previous_value, new_value "
            "from audit_log where action like %s order by id",
            (action_prefix + "%",),
        ).fetchall()


def _sha(table: str, row_id: int) -> str:
    with dbmod.connection() as conn:
        conn.autocommit = True
        return conn.execute(f"select sha256 from {table} where id = %s", (row_id,)).fetchone()[0]


def test_check_repairs_bare_photo_and_back_rows(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = insert_photo(conn, working_path="placeholder")
        insert_master(conn, pid, master_path="C:\\masters\\x.jpg")
        bid = insert_back(conn, photo_id=pid, working_path="placeholder")
        conn.commit()

    psha, bsha = _sha("photos", pid), _sha("photo_backs", bid)
    photo_std = ingest_paths.working_path(phase6, pid, psha, "jpg")
    back_std = ingest_paths.back_working_path(phase6, bid, bsha, "jpg")
    write_test_jpeg(photo_std)
    write_test_jpeg(back_std)
    with dbmod.connection() as conn:
        conn.autocommit = True
        conn.execute("update photos set working_path = %s where id = %s", (photo_std.name, pid))
        conn.execute("update photo_backs set working_path = %s where id = %s", (back_std.name, bid))

    # Dry run: reports, changes nothing.
    dry = check_working_files.check(dry_run=True, limit=None)
    assert dry.bare_pointers_seen == 2
    assert dry.pointer_repaired_standard == 1 and dry.backs_pointer_repaired == 1
    assert dry.photos_still_bare == 1 and dry.backs_still_bare == 1
    assert _audit_rows("photo") == []

    counts = check_working_files.check(dry_run=False, limit=None)
    assert counts.pointer_repaired_standard == 1
    assert counts.backs_pointer_repaired == 1
    assert counts.truly_missing == 0 and counts.backs_missing == 0
    assert counts.photos_still_bare == 0 and counts.backs_still_bare == 0

    with dbmod.connection() as conn:
        conn.autocommit = True
        wp = conn.execute("select working_path from photos where id = %s", (pid,)).fetchone()[0]
        bwp = conn.execute("select working_path from photo_backs where id = %s", (bid,)).fetchone()[0]
        fv = conn.execute("select file_version from photos where id = %s", (pid,)).fetchone()[0]
    assert wp == str(photo_std) and Path(wp).is_absolute()
    assert bwp == str(back_std) and Path(bwp).is_absolute()
    assert fv == 1, "pointer-only repair must not bump file_version"

    rows = _audit_rows("photo")
    kinds = {(r[0], r[1], r[2]) for r in rows}
    assert ("photo", pid, "photo.working_path_repaired.pointer") in kinds
    assert ("photo_back", bid, "photo_back.working_path_repaired.pointer") in kinds
    for r in rows:
        assert r[3]["working_path"] in (photo_std.name, back_std.name)
        assert Path(r[4]["working_path"]).is_absolute()

    # Idempotent: a second run finds everything ok.
    again = check_working_files.check(dry_run=False, limit=None)
    assert again.already_ok == 1 and again.backs_already_ok == 1
    assert again.bare_pointers_seen == 0


def test_check_never_trusts_a_relative_path_via_cwd(phase6, tmp_path, monkeypatch):
    """A bare name that happens to exist in the CWD must still be repaired."""
    monkeypatch.chdir(tmp_path)
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = insert_photo(conn, working_path="placeholder")
        insert_master(conn, pid, master_path="C:\\masters\\y.jpg")
        conn.commit()
    sha = _sha("photos", pid)
    std = ingest_paths.working_path(phase6, pid, sha, "jpg")
    write_test_jpeg(std)
    write_test_jpeg(tmp_path / std.name)  # decoy in CWD
    with dbmod.connection() as conn:
        conn.autocommit = True
        conn.execute("update photos set working_path = %s where id = %s", (std.name, pid))

    counts = check_working_files.check(dry_run=False, limit=None)
    assert counts.already_ok == 0
    assert counts.pointer_repaired_standard == 1
    assert counts.photos_still_bare == 0


def test_check_repairs_deleted_bare_pointer_only_when_file_exists(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        p_ok = insert_photo(conn, working_path="placeholder", is_deleted=True, triage_status="untriaged")
        p_gone = insert_photo(conn, working_path="placeholder", is_deleted=True, triage_status="untriaged")
        conn.commit()
    std = ingest_paths.working_path(phase6, p_ok, _sha("photos", p_ok), "jpg")
    write_test_jpeg(std)
    with dbmod.connection() as conn:
        conn.autocommit = True
        conn.execute("update photos set working_path = %s where id = %s", (std.name, p_ok))
        conn.execute("update photos set working_path = %s where id = %s", ("00000000_gone.jpg", p_gone))

    counts = check_working_files.check(dry_run=False, limit=None)
    assert counts.photos_scanned == 0          # deleted rows are not "live"
    assert counts.deleted_pointer_repaired == 1
    assert counts.deleted_unresolved == 1
    with dbmod.connection() as conn:
        conn.autocommit = True
        assert conn.execute("select working_path from photos where id = %s", (p_ok,)).fetchone()[0] == str(std)
        assert conn.execute("select working_path from photos where id = %s", (p_gone,)).fetchone()[0] == "00000000_gone.jpg"


# ---- push pre-flight: never our own database -----------------------------


class _StatusClient:
    def __init__(self, db):
        self._db = db

    def status(self):
        return {"tables": [], "db": self._db}


def test_push_guard_refuses_same_database(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("select current_database(), system_identifier::text from pg_control_system()")
            name, ident = cur.fetchone()
        with pytest.raises(SharedDatabaseError):
            guard_not_shared_database(conn, _StatusClient({"name": name, "system_identifier": ident}))
        # Different database on the same cluster → fine.
        guard_not_shared_database(conn, _StatusClient({"name": name + "_web", "system_identifier": ident}))
        # Same name on a different cluster (the VM) → fine.
        guard_not_shared_database(conn, _StatusClient({"name": name, "system_identifier": "1"}))
        # Web that cannot report → allowed (logged).
        guard_not_shared_database(conn, _StatusClient(None))
        guard_not_shared_database(conn, _StatusClient({"name": None, "system_identifier": None}))
