"""Resolve a dedupe group then undo it. Every side effect must round-trip:
  * loser file back in working/, keeper's is unchanged;
  * physical_ref_note appended on resolve and stripped on undo;
  * photo_backs re-pointed;
  * photo_masters re-parented and preferred flag preserved;
  * album memberships moved (both the "keeper already in album" skip
    and the "loser only" move);
  * suggestions moved (dedup by kind+source+payload);
  * is_private promoted then cleared;
  * exclusions from mark_group_not_duplicates suppress re-proposal.
"""
from __future__ import annotations

import json
from pathlib import Path

import psycopg
import pytest

from photoarchive import db
from photoarchive.config import Settings
from photoarchive.modes.dedupe import exclusions as excl_mod
from photoarchive.modes.dedupe import resolve as resolve_mod
from photoarchive.modes.dedupe import scan as scan_mod
from photoarchive.modes.dedupe import undo as undo_mod
from photoarchive.modes.ingest.paths import working_path

from .conftest import DB_AVAILABLE, TEST_DATABASE_URL


pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="no TEST_DATABASE_URL")


def _test_settings(tmp_path: Path, url: str) -> Settings:
    s = Settings(
        MASTER_ROOTS="dummy=Z:\\",
        WORKING_DIR=tmp_path / "working",
        QUARANTINE_DIR=tmp_path / "quarantine",
        MANUAL_FIX_DIR=tmp_path / "manual-fix",
        THUMBS_DIR=tmp_path / "thumbs",
        DATABASE_URL=url,
        INFERENCE_URL="http://x", INFERENCE_TOKEN="x",
        WEB_API_URL="http://x", WEB_API_TOKEN="x",
        DEDUPE_PHASH_MAX=10, DEDUPE_DHASH_MAX=10,
    )
    for d in (s.WORKING_DIR, s.QUARANTINE_DIR, s.MANUAL_FIX_DIR, s.THUMBS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    return s


def _init_pool(url: str, settings: Settings) -> None:
    if getattr(db, "_pool", None) is not None:
        db.close_pool()
    db.init_pool(settings)


def _reset(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("""
            truncate dedupe_exclusions, dedupe_members, dedupe_groups,
                     album_photos, albums, suggestions,
                     photo_backs, photo_masters,
                     audit_log, triage_hints, ingest_pairings, ingest_rescans,
                     ingest_failures, job_items, job_runs, photos
            restart identity cascade
        """)


def _mk_photo(
    url: str, settings: Settings, *,
    sha: str, is_scan: bool = False, mime: str = "image/jpeg",
    triage_status: str = "keep", is_private: bool = False,
    width: int = 1200, height: int = 900, file_size: int = 500_000,
    exif_camera: str | None = None,
    scan_batch: str | None = None, scan_sequence: int | None = None,
    source_folder: str = "", source_filename: str = "stub.jpg",
    physical_ref_note: str | None = None,
    phash: str | None = None, dhash: str | None = None,
) -> int:
    ext = "tif" if mime == "image/tiff" else "jpg"
    with psycopg.connect(url, autocommit=True) as conn:
        pid = conn.execute("""
            insert into photos
              (sha256, source_root, source_folder, source_filename,
               mime, width, height, file_size, is_scan,
               scan_batch, scan_sequence,
               triage_status, is_private, is_deleted, exif_camera,
               physical_ref_note, phash, dhash)
            values (%s, 'dummy', %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s,
                    %s, %s, false, %s,
                    %s, %s, %s)
            returning id
        """, (sha, source_folder, source_filename,
              mime, width, height, file_size, is_scan,
              scan_batch, scan_sequence,
              triage_status, is_private, exif_camera,
              physical_ref_note, phash, dhash)).fetchone()[0]
    wp = working_path(settings, pid, sha, ext)
    wp.parent.mkdir(parents=True, exist_ok=True)
    wp.write_bytes(b"stub-" + sha.encode())
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("update photos set working_path=%s where id=%s",
                     (str(wp), pid))
    return pid


def _make_group(url: str, keeper_id: int, loser_id: int) -> int:
    with psycopg.connect(url, autocommit=True) as conn:
        gid = conn.execute("""
            insert into dedupe_groups (status, size, min_distance)
            values ('pending', 2, 3) returning id
        """).fetchone()[0]
        conn.execute("""
            insert into dedupe_members
              (group_id, photo_id, is_keeper, phash_dist, dhash_dist,
               matched_by, transform, distance_to_keeper, keeper_reason)
            values (%s, %s, true, 0, 0, 'both', 'identity', 0, 'test'),
                   (%s, %s, false, 3, 3, 'both', 'identity', 3, null)
        """, (gid, keeper_id, gid, loser_id))
    return gid


def _photo_row(url: str, pid: int) -> dict:
    with psycopg.connect(url) as conn:
        r = conn.execute("""
            select triage_status, is_private, is_deleted, working_path,
                   quarantine_path, physical_ref_note
            from photos where id = %s
        """, (pid,)).fetchone()
    return {
        "triage_status": r[0], "is_private": r[1], "is_deleted": r[2],
        "working_path": r[3], "quarantine_path": r[4],
        "physical_ref_note": r[5],
    }


def test_resolve_and_undo_round_trip(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(TEST_DATABASE_URL, settings)
    _reset(TEST_DATABASE_URL)

    # Keeper is the digital (has EXIF); loser is a scan of the print.
    keeper = _mk_photo(
        TEST_DATABASE_URL, settings, sha="a" * 64,
        is_scan=False, mime="image/jpeg", exif_camera="Canon",
        source_filename="phone.jpg", physical_ref_note=None,
    )
    loser = _mk_photo(
        TEST_DATABASE_URL, settings, sha="b" * 64,
        is_scan=True, mime="image/tiff",
        scan_batch="Batch 00012", scan_sequence=17,
        source_folder="Batch 00012", source_filename="IMG017.tif",
        physical_ref_note=None,
    )

    # Attach data on the loser that must migrate to the keeper.
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("""
            insert into photo_backs (photo_id, master_path, sha256)
            values (%s, %s, %s)
        """, (loser, "/masters/loser-back.tif", "d" * 64))
        conn.execute("""
            insert into photo_masters
              (photo_id, master_path, sha256, mime, is_preferred)
            values (%s, %s, %s, 'image/tiff', true)
        """, (loser, "/masters/loser.tif", "e" * 64))
        conn.execute("""
            insert into albums (name, source) values ('Family', 'manual')
        """)
        album_id = conn.execute(
            "select id from albums where name='Family'"
        ).fetchone()[0]
        conn.execute("""
            insert into album_photos (album_id, photo_id, position)
            values (%s, %s, 1), (%s, %s, 2)
        """, (album_id, keeper, album_id, loser))
        # A suggestion identical to one already on the keeper (must NOT
        # duplicate on move) — plus one that only the loser has.
        payload_dup = json.dumps({"date": "2010-06-01", "precision": "month"})
        payload_uniq = json.dumps({"date": "2011-01-01", "precision": "exact"})
        for pid, payload in ((keeper, payload_dup), (loser, payload_dup),
                             (loser, payload_uniq)):
            conn.execute("""
                insert into suggestions (photo_id, kind, payload, source, status)
                values (%s, 'date', %s::jsonb, 'ai', 'pending')
            """, (pid, payload))

    gid = _make_group(TEST_DATABASE_URL, keeper, loser)

    # Resolve — reviewer accepts the pre-selected keeper.
    result = resolve_mod.resolve_group(
        settings, gid, chosen_keeper_id=keeper, actor="desktop",
    )

    # ------------------------------------------------------------------
    # Post-resolve state
    # ------------------------------------------------------------------
    ks = _photo_row(TEST_DATABASE_URL, keeper)
    ls = _photo_row(TEST_DATABASE_URL, loser)

    assert ks["triage_status"] == "keep"
    assert ks["is_deleted"] is False
    assert ks["working_path"] and Path(ks["working_path"]).exists()
    assert "also scanned: Batch 00012 #017" in (ks["physical_ref_note"] or "")

    assert ls["triage_status"] == "junk"
    assert ls["is_deleted"] is True
    assert ls["working_path"] is None
    assert ls["quarantine_path"] and Path(ls["quarantine_path"]).exists()

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        # photo_backs migrated
        (backs_on_keeper,) = conn.execute(
            "select count(*) from photo_backs where photo_id = %s", (keeper,),
        ).fetchone()
        assert backs_on_keeper == 1
        # photo_masters migrated and demoted to non-preferred
        row = conn.execute("""
            select is_preferred from photo_masters where photo_id = %s
        """, (keeper,)).fetchall()
        # Only the migrated one; keeper had no photo_masters row initially.
        assert row == [(False,)]
        # Album membership: keeper still there, loser gone (dedup skip).
        rows = conn.execute("""
            select photo_id from album_photos order by photo_id
        """).fetchall()
        assert rows == [(keeper,)]
        # Suggestions: two on keeper (its own + the unique one moved),
        # zero remaining on loser (the duplicate stayed, the unique moved).
        rows = conn.execute("""
            select photo_id from suggestions
        """).fetchall()
        photo_ids = sorted(r[0] for r in rows)
        assert photo_ids.count(keeper) == 2
        assert photo_ids.count(loser) == 1  # the dup we intentionally left
        # Group is resolved and audit rows exist.
        (status,) = conn.execute(
            "select status from dedupe_groups where id = %s", (gid,),
        ).fetchone()
        assert status == "resolved"
        (n_resolve,) = conn.execute("""
            select count(*) from audit_log
            where action='dedupe.resolve' and entity_id=%s
        """, (gid,)).fetchone()
        assert n_resolve == 1
        (n_triage,) = conn.execute("""
            select count(*) from audit_log
            where action='triage.decision' and entity_type='photo'
              and entity_id=%s
        """, (loser,)).fetchone()
        assert n_triage == 1

    # ------------------------------------------------------------------
    # Undo — everything reverses
    # ------------------------------------------------------------------
    undo_mod.undo_resolve(settings, gid, actor="desktop")

    ks2 = _photo_row(TEST_DATABASE_URL, keeper)
    ls2 = _photo_row(TEST_DATABASE_URL, loser)

    assert ls2["triage_status"] == "keep"
    assert ls2["is_deleted"] is False
    assert ls2["working_path"] and Path(ls2["working_path"]).exists()
    assert ls2["quarantine_path"] is None

    # Physical ref stripped
    assert (ks2["physical_ref_note"] or "") == ""

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        (backs_on_loser,) = conn.execute(
            "select count(*) from photo_backs where photo_id=%s", (loser,),
        ).fetchone()
        assert backs_on_loser == 1
        (backs_on_keeper,) = conn.execute(
            "select count(*) from photo_backs where photo_id=%s", (keeper,),
        ).fetchone()
        assert backs_on_keeper == 0

        (n_masters_on_loser,) = conn.execute(
            "select count(*) from photo_masters where photo_id=%s", (loser,),
        ).fetchone()
        assert n_masters_on_loser == 1
        (n_masters_on_keeper,) = conn.execute(
            "select count(*) from photo_masters where photo_id=%s", (keeper,),
        ).fetchone()
        assert n_masters_on_keeper == 0

        # Both album rows are back.
        rows = conn.execute(
            "select photo_id from album_photos order by photo_id"
        ).fetchall()
        assert sorted(r[0] for r in rows) == sorted([keeper, loser])

        # Group is pending again.
        (status,) = conn.execute(
            "select status from dedupe_groups where id=%s", (gid,),
        ).fetchone()
        assert status == "pending"

    db.close_pool()


def test_is_private_promotion_and_undo(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(TEST_DATABASE_URL, settings)
    _reset(TEST_DATABASE_URL)

    keeper = _mk_photo(
        TEST_DATABASE_URL, settings, sha="a" * 64,
        is_private=False, source_filename="keeper.jpg",
    )
    loser = _mk_photo(
        TEST_DATABASE_URL, settings, sha="b" * 64,
        is_private=True, triage_status="private",
        source_filename="loser.jpg",
    )
    gid = _make_group(TEST_DATABASE_URL, keeper, loser)

    resolve_mod.resolve_group(
        settings, gid, chosen_keeper_id=keeper, actor="desktop",
    )
    ks = _photo_row(TEST_DATABASE_URL, keeper)
    assert ks["is_private"] is True

    undo_mod.undo_resolve(settings, gid, actor="desktop")
    ks2 = _photo_row(TEST_DATABASE_URL, keeper)
    assert ks2["is_private"] is False

    db.close_pool()


def test_exclusions_suppress_regrouping(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(TEST_DATABASE_URL, settings)
    _reset(TEST_DATABASE_URL)

    # Two photos with identical hashes (would definitely group).
    same_phash = "00" * 32
    same_dhash = "11" * 32
    a = _mk_photo(
        TEST_DATABASE_URL, settings, sha="a" * 64,
        phash=same_phash, dhash=same_dhash, source_filename="a.jpg",
    )
    b = _mk_photo(
        TEST_DATABASE_URL, settings, sha="b" * 64,
        phash=same_phash, dhash=same_dhash, source_filename="b.jpg",
    )

    stats = scan_mod.run_dedupe_scan(settings)
    assert stats.groups_created == 1

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        (gid,) = conn.execute(
            "select id from dedupe_groups where status='pending' limit 1"
        ).fetchone()
    inserted = excl_mod.mark_group_not_duplicates(settings, gid)
    assert inserted == 1  # exactly one pair, exactly one exclusion

    # Rescan — the pair must not reappear.
    stats2 = scan_mod.run_dedupe_scan(settings)
    assert stats2.groups_created == 0

    db.close_pool()
