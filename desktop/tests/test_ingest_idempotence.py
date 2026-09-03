"""Ingest end-to-end against TEST_DATABASE_URL. Two runs over the same
synthetic tree must produce the same row counts (idempotent)."""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import psycopg
import pytest
from PIL import Image

from photoarchive import db
from photoarchive.config import MasterRoot, Settings
from photoarchive.modes.ingest.service import run_ingest

from .conftest import DB_AVAILABLE, TEST_DATABASE_URL


pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="no TEST_DATABASE_URL")


def _make_photo(path: Path, colour: tuple[int, int, int]) -> None:
    """Highly saturated, distinct colour so nothing looks back-like."""
    rng = np.random.default_rng(seed=sum(colour))
    base = np.array(colour, dtype=np.uint8)
    noise = rng.integers(-8, 8, size=(96, 96, 3), dtype=np.int16)
    arr = np.clip(base + noise, 0, 255).astype(np.uint8)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr).save(path, "PNG")


def _make_photos_root(root: Path) -> None:
    _make_photo(root / "_2005-04" / "f001.jpg", colour=(200, 30, 30))
    _make_photo(root / "_2005-04" / "f002.jpg", colour=(30, 200, 30))
    _make_photo(root / "_2006-01" / "f050.jpg", colour=(30, 30, 200))


def _make_scans_root(root: Path) -> None:
    _make_photo(root / "Batch 00001" / "IMG_0001.JPG", colour=(180, 40, 120))
    _make_photo(root / "Batch 00001" / "IMG_0002.JPG", colour=(40, 180, 120))
    _make_photo(root / "Chuck and Lola Wedding" / "IMG_0100.JPG", colour=(120, 80, 200))


def _reset_test_db(url: str) -> None:
    """Truncate rows we care about. Migrations must already have been run
    against this DB (Phase 1 setup + migration 12)."""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("""
            truncate
              ingest_failures,
              ingest_rescans,
              ingest_pairings,
              album_photos,
              albums,
              suggestions,
              photo_backs,
              photo_masters,
              photos,
              job_items,
              job_runs,
              audit_log
            restart identity cascade
        """)


def _init_pool_for_url(url: str) -> None:
    if getattr(db, "_pool", None) is not None:
        db.close_pool()
    from photoarchive.config import Settings as S

    settings = S(
        MASTER_ROOTS="dummy=Z:\\",  # never used in this test
        WORKING_DIR=Path("."), QUARANTINE_DIR=Path("."),
        MANUAL_FIX_DIR=Path("."), THUMBS_DIR=Path("."),
        DATABASE_URL=url,
        INFERENCE_URL="http://x", INFERENCE_TOKEN="x",
        WEB_API_URL="http://x", WEB_API_TOKEN="x",
    )
    db.init_pool(settings)


def _test_settings(masters: list[MasterRoot], working: Path, url: str) -> Settings:
    s = Settings(
        MASTER_ROOTS="dummy=Z:\\",
        WORKING_DIR=working / "working",
        QUARANTINE_DIR=working / "quarantine",
        MANUAL_FIX_DIR=working / "manual-fix",
        THUMBS_DIR=working / "thumbs",
        DATABASE_URL=url,
        INFERENCE_URL="http://x", INFERENCE_TOKEN="x",
        WEB_API_URL="http://x", WEB_API_TOKEN="x",
    )
    # Bypass MASTER_ROOTS string parsing — inject the list directly.
    s.__dict__["_master_roots_override"] = masters
    return s


def _row_counts(url: str) -> dict[str, int]:
    with psycopg.connect(url) as conn:
        return {
            t: conn.execute(f"select count(*) from {t}").fetchone()[0]
            for t in ("photos", "photo_masters", "suggestions", "albums",
                      "ingest_pairings", "ingest_rescans", "ingest_failures",
                      "job_runs", "job_items")
        }


def test_ingest_is_idempotent(tmp_path, monkeypatch):
    """Full ingest twice should yield the same counts and zero failures.
    The masters guard is stubbed out because pytest's tmp dirs are always
    writable; guard behaviour is exercised by test_guard.py."""
    photos_root = tmp_path / "photos"
    scans_root = tmp_path / "scans"
    _make_photos_root(photos_root)
    _make_scans_root(scans_root)

    _reset_test_db(TEST_DATABASE_URL)  # type: ignore[arg-type]
    _init_pool_for_url(TEST_DATABASE_URL)  # type: ignore[arg-type]

    masters = [
        MasterRoot(label="tst_photos", path=photos_root, kind="digital"),
        MasterRoot(label="tst_scans", path=scans_root, kind="scan"),
    ]
    settings = _test_settings(masters, tmp_path, TEST_DATABASE_URL)  # type: ignore[arg-type]

    # Stub the guard so writable temp dirs don't refuse the run.
    from datetime import datetime, timezone
    from photoarchive.modes.ingest import service as svc
    from photoarchive.modes.ingest.guard import GuardResult, RootGuardResult

    def fake_guard(roots):
        return GuardResult(
            checked_at=datetime.now(timezone.utc).isoformat(),
            per_root=[
                RootGuardResult(label=r.label, path=r.path,
                                root_writable=False, sub_writable=False,
                                sub_path=None)
                for r in roots
            ],
        )

    monkeypatch.setattr(svc, "run_masters_guard", fake_guard)

    # Also monkey-patch settings.master_roots so run_ingest sees ours.
    import photoarchive.config as cfg
    orig_prop = cfg.Settings.master_roots
    try:
        cfg.Settings.master_roots = property(  # type: ignore[assignment]
            lambda self: masters
        )
        s1 = run_ingest(settings=settings, roots=masters)
        counts_after_first = _row_counts(TEST_DATABASE_URL)  # type: ignore[arg-type]

        s2 = run_ingest(settings=settings, roots=masters)
        counts_after_second = _row_counts(TEST_DATABASE_URL)  # type: ignore[arg-type]
    finally:
        cfg.Settings.master_roots = orig_prop  # type: ignore[assignment]

    assert s1.totals.new == 6
    assert s1.totals.failed == 0
    # Second run adds nothing new: everything is dupe.
    assert s2.totals.new == 0
    assert s2.totals.failed == 0
    assert s2.totals.skipped_dupe == 6
    # Row deltas: same photos/masters/suggestions/albums; only job_runs+items grow.
    for t in ("photos", "photo_masters", "suggestions", "albums",
              "ingest_pairings", "ingest_rescans", "ingest_failures"):
        assert counts_after_first[t] == counts_after_second[t], (t, counts_after_first[t], counts_after_second[t])
    assert counts_after_second["job_runs"] == counts_after_first["job_runs"] + 1

    db.close_pool()
