"""Phase 13 fix-up 1, item 4 / answer F5: ingest stops confirming dates it
has no business confirming. Drives the real `run_ingest` over a digital, a
scan and a contrib root (laid out the way `pull_contributions` leaves one:
`<uploader>/<contribution_id>/`), with real EXIF in the files.

* a scan's EXIF date → `date` suggestion, `reason='scanner_clock'`;
* 1 January before 00:01 → suggestion, `reason='unset_camera_clock'`, and
  the `_YYYY-MM` folder hint is recorded as for any undated photo;
* after the file's mtime (+1 day) or before 1826 → `implausible_date`, which
  is the path a contributed file would bring it back through;
* every other digital EXIF date stays confirmed exactly as before.
"""
from __future__ import annotations

import os
from datetime import datetime

import numpy as np
import psycopg
import pytest
from PIL import Image

from photoarchive.config import MasterRoot

from .conftest import DB_AVAILABLE, TEST_DATABASE_URL
from .test_ingest_idempotence import (
    _init_pool_for_url, _reset_test_db, _test_settings,
)

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="no TEST_DATABASE_URL")


def _exif_photo(path, colour, taken: str | None, *, mtime: datetime | None = None,
                camera: bool = True) -> None:
    rng = np.random.default_rng(seed=sum(colour))
    arr = np.clip(np.array(colour, dtype=np.int16)
                  + rng.integers(-8, 8, size=(96, 96, 3), dtype=np.int16), 0, 255)
    img = Image.fromarray(arr.astype(np.uint8))
    ex = Image.Exif()
    if camera:
        ex[0x010F] = "TestCam"          # Make
        ex[0x0110] = "Model 1"          # Model
    if taken:
        ex.get_ifd(0x8769)[0x9003] = taken   # DateTimeOriginal
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=92, exif=ex.tobytes())
    if mtime is not None:
        ts = mtime.timestamp()
        os.utime(path, (ts, ts))


def _run(tmp_path, monkeypatch, masters):
    from datetime import timezone
    from photoarchive.modes.ingest import service as svc
    from photoarchive.modes.ingest.guard import GuardResult, RootGuardResult
    from photoarchive.modes.ingest.service import run_ingest
    import photoarchive.config as cfg

    _reset_test_db(TEST_DATABASE_URL)
    _init_pool_for_url(TEST_DATABASE_URL)
    settings = _test_settings(masters, tmp_path, TEST_DATABASE_URL)
    monkeypatch.setattr(svc, "run_masters_guard", lambda roots: GuardResult(
        checked_at=datetime.now(timezone.utc).isoformat(),
        per_root=[RootGuardResult(label=r.label, path=r.path, root_writable=False,
                                  sub_writable=False, sub_path=None) for r in roots]))
    monkeypatch.setattr(cfg.Settings, "master_roots", property(lambda self: masters))
    summary = run_ingest(settings=settings, roots=masters)
    assert summary.totals.failed == 0
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        photos = {r[0]: r[1:] for r in conn.execute(
            "select source_filename, capture_date::text, capture_date_confirmed, id "
            "from photos").fetchall()}
        sugg = {}
        for fname, source, payload in conn.execute(
                "select p.source_filename, s.source::text, s.payload from suggestions s "
                "join photos p on p.id = s.photo_id where s.kind = 'date' order by s.id"):
            sugg.setdefault(fname, []).append((source, payload))
    return photos, sugg


def test_ingest_turns_untrusted_exif_dates_into_suggestions(tmp_path, monkeypatch):
    digital = tmp_path / "photos"
    scans = tmp_path / "scans"
    contrib = tmp_path / "contrib"
    mtime = datetime(2010, 6, 1, 12, 0)
    _exif_photo(digital / "_2009-07" / "good.jpg", (200, 30, 30), "2009:07:04 15:30:00", mtime=mtime)
    _exif_photo(digital / "_2004-01" / "clock.jpg", (30, 200, 30), "2004:01:01 00:00:11", mtime=mtime)
    _exif_photo(scans / "Batch 00045" / "scan0001.jpg", (30, 30, 200), "2025:11:23 10:03:14",
                camera=False)
    # A contributed file comes back through the same ingest.
    _exif_photo(contrib / "aunt-may" / "17" / "future.jpg", (200, 200, 30),
                "2031:05:05 09:00:00", mtime=mtime)
    _exif_photo(contrib / "aunt-may" / "17" / "ancient.jpg", (30, 200, 200),
                "1801:02:03 09:00:00", mtime=mtime)
    _exif_photo(contrib / "aunt-may" / "17" / "fine.jpg", (200, 30, 200),
                "1998:08:08 08:08:08", mtime=mtime)

    masters = [
        MasterRoot(label="tst_photos", path=digital, kind="digital"),
        MasterRoot(label="tst_scans", path=scans, kind="scan"),
        MasterRoot(label="tst_contrib", path=contrib, kind="contrib"),
    ]
    photos, sugg = _run(tmp_path, monkeypatch, masters)

    # Unchanged: a plausible camera date is a fact, on any non-scan root.
    assert photos["good.jpg"][:2] == ("2009-07-04", True)
    assert photos["fine.jpg"][:2] == ("1998-08-08", True)
    assert "good.jpg" not in sugg and "fine.jpg" not in sugg

    def only_reason(fname):
        assert photos[fname][:2] == (None, False), fname
        exif = [p for s, p in sugg[fname] if "reason" in p]
        assert len(exif) == 1 and exif[0]["precision"] == "exact"
        assert all(s == "import" for s, _ in sugg[fname])
        return exif[0]

    s = only_reason("scan0001.jpg")
    assert (s["reason"], s["date"]) == ("scanner_clock", "2025-11-23")
    c = only_reason("clock.jpg")
    assert (c["reason"], c["date"]) == ("unset_camera_clock", "2004-01-01")
    # …and the folder month is offered, as for any photo with no date.
    assert {"date": "2004-01-01", "precision": "month",
            "evidence": "export folder _2004-01"} in [p for _, p in sugg["clock.jpg"]]
    assert only_reason("future.jpg")["reason"] == "implausible_date"
    assert only_reason("ancient.jpg")["reason"] == "implausible_date"
