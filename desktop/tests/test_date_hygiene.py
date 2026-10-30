"""Phase 13 fix-up 1 — date hygiene.

* the rules (pure): a scan, an unset camera clock, a folder that disagrees,
  an implausible date; and which EXIF dates ingest may still confirm;
* `unconfirm` clears the value, not just the flag, keeps the old value as a
  pending `date` suggestion (source by who set it), audits previous/new,
  adds the folder-month hint on a digital photo, and is idempotent;
* the tool: the report reads only ingest-sourced dates, `--apply` clears
  exactly the chosen rules' rows;
* the metadata writer afterwards: a cleared digital photo is due, a cleared
  scan is not (Q9's gate already kept its date out of the file);
* the pull of a web clear clears the fact without a local suggestion.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import psycopg
import pytest

from photoarchive import db as dbmod
from photoarchive.modes.dates import repo as date_repo, rules
from photoarchive.modes.dates.rules import DateRow
from photoarchive.modes.metadata import repo as meta_repo
from photoarchive.modes.sync import pull as pull_mod
from photoarchive.tools import date_hygiene as tool

from .conftest import TEST_DATABASE_URL
from .phase6_fixtures import _sha, phase6  # noqa: F401


# --------------------------------------------------------------------------
# Rules (pure)
# --------------------------------------------------------------------------

def _row(**kw) -> DateRow:
    base = dict(photo_id=1, capture_date=date(1999, 6, 1), precision="exact",
                exif_taken_at=datetime(1999, 6, 1, 14, 0), is_scan=False,
                source_root="photos", source_folder="_1999-06", scan_batch=None,
                ingested_at=datetime(2026, 9, 3, tzinfo=timezone.utc))
    base.update(kw)
    return DateRow(**base)


def test_a_good_camera_date_breaks_no_rule():
    assert rules.flag(_row()) == []


def test_rule_a_scan_by_flag_or_by_root():
    assert rules.flag(_row(is_scan=True)) == ["a"]
    assert rules.flag(_row(source_root="scans"), scan_roots={"scans"}) == ["a"]


def test_rule_b_january_first_before_one_minute_past_midnight():
    hit = _row(capture_date=date(2004, 1, 1), exif_taken_at=datetime(2004, 1, 1, 0, 0, 11),
               source_folder="_2004-01")
    assert rules.flag(hit) == ["b"]
    later = _row(capture_date=date(2004, 1, 1), exif_taken_at=datetime(2004, 1, 1, 0, 1, 0),
                 source_folder="_2004-01")
    assert rules.flag(later) == []


def test_rule_c_folder_more_than_three_months_away():
    assert rules.flag(_row(source_folder="_1999-09")) == []       # 3 months: agrees
    assert rules.flag(_row(source_folder="_1999-10")) == ["c"]    # 4 months
    assert rules.flag(_row(source_folder="Holiday")) == []        # no hint, no rule


def test_rule_d_after_ingest_or_before_photography():
    future = _row(capture_date=date(2027, 1, 5), exif_taken_at=datetime(2027, 1, 5, 9, 0),
                  source_folder=None)
    assert rules.flag(future) == ["d"]
    ancient = _row(capture_date=date(1800, 5, 5), exif_taken_at=None, source_folder=None)
    assert rules.flag(ancient) == ["d"]


def test_parse_rules_and_first_chosen_reason():
    assert rules.parse_rules("a, b,a") == ["a", "b"]
    with pytest.raises(ValueError):
        rules.parse_rules("a,e")
    assert rules.reason_for(["a", "b"], ["b"]) == ("b", "unset_camera_clock")
    assert rules.reason_for(["c"], ["a", "b"]) is None


def test_ingest_reason():
    mtime = datetime(2010, 6, 1, 12, 0)
    ok = datetime(2009, 7, 4, 15, 30)
    assert rules.ingest_reason(None, is_scan=True, master_mtime=mtime) is None
    assert rules.ingest_reason(ok, is_scan=False, master_mtime=mtime) is None
    assert rules.ingest_reason(ok, is_scan=True, master_mtime=mtime) == "scanner_clock"
    assert rules.ingest_reason(datetime(2004, 1, 1, 0, 0, 30), is_scan=False,
                               master_mtime=mtime) == "unset_camera_clock"
    # A day of slack for time zones, and not one second more.
    assert rules.ingest_reason(datetime(2010, 6, 2, 12, 0), is_scan=False,
                               master_mtime=mtime) is None
    assert rules.ingest_reason(datetime(2010, 6, 2, 12, 0, 1), is_scan=False,
                               master_mtime=mtime) == "implausible_date"
    assert rules.ingest_reason(datetime(1825, 12, 31, 12, 0), is_scan=False,
                               master_mtime=mtime) == "implausible_date"


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

def _q(sql, params=(), *, one=False):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        if cur.description is None:
            return None
        return cur.fetchone() if one else cur.fetchall()


def _photo(*, cd: str | None, taken: str | None = None, is_scan: bool = False,
           folder: str = "_1999-06", root: str = "m1", confirmed: bool = True,
           triage: str = "keep") -> int:
    sha = _sha()
    pid = _q("""
        insert into photos (sha256, mime, width, height, is_scan, source_root,
                            source_folder, source_filename, scan_batch, scan_sequence,
                            triage_status, file_version, capture_date,
                            capture_date_precision, capture_date_confirmed, exif_taken_at)
        values (%s, 'image/jpeg', 400, 300, %s, %s, %s, 'f.jpg', %s, %s, %s, 1,
                %s, %s, %s, %s)
        returning id
    """, (sha, is_scan, root, folder, folder if is_scan else None, 1 if is_scan else None,
          triage, cd, "exact" if cd else "unknown", confirmed and cd is not None, taken),
        one=True)[0]
    _q("insert into photo_masters (photo_id, master_path, sha256, mime, is_preferred) "
       "values (%s, %s, %s, 'image/jpeg', true)", (pid, f"M:\\{sha[:8]}.jpg", sha))
    return pid


def _set_by_human(pid: int, cd: str) -> None:
    _q("insert into audit_log (actor, action, entity_type, entity_id, new_value) "
       "values ('desktop', 'photo.capture_date.set', 'photo', %s, %s::jsonb)",
       (pid, f'{{"capture_date": "{cd}"}}'))


def _photo_row(pid):
    return _q("select capture_date, capture_date_precision::text, capture_date_confirmed "
              "from photos where id = %s", (pid,), one=True)


def _suggestions(pid):
    return _q("select source::text, status::text, confidence, payload from suggestions "
              "where photo_id = %s and kind = 'date' order by id", (pid,))


def _audits(pid, action="photo.capture_date.unconfirm"):
    return _q("select actor, previous_value, new_value from audit_log "
              "where entity_id = %s and action = %s order by id", (pid, action))


def _unconfirm(pid, **kw):
    kw.setdefault("reason", "cleared")
    kw.setdefault("via", "test")
    with dbmod.connection() as conn:
        conn.autocommit = False
        res = date_repo.unconfirm(conn, pid, **kw)
        conn.commit()
    return res


def test_unconfirm_clears_the_value_and_keeps_it_as_a_suggestion(phase6):
    pid = _photo(cd="2025-11-23", taken="2025-11-23 10:03:14", is_scan=True,
                 folder="Batch 00045")
    res = _unconfirm(pid, reason="scanner_clock", rule="a")

    assert _photo_row(pid) == (None, "unknown", False)
    [(source, status, conf, payload)] = _suggestions(pid)
    assert (source, status) == ("import", "pending")
    assert conf == pytest.approx(date_repo.CLEARED_CONFIDENCE)
    assert payload["date"] == "2025-11-23" and payload["precision"] == "exact"
    assert payload["reason"] == "scanner_clock" and payload["rule"] == "a"
    [(actor, prev, new)] = _audits(pid)
    assert actor == "desktop"
    assert prev == {"capture_date": "2025-11-23", "precision": "exact",
                    "confirmed": True, "source": "import"}
    assert new["capture_date"] is None and new["confirmed"] is False
    assert new["suggestion_id"] == res.suggestion_id
    # A scan gets no folder-month hint.
    assert res.folder_suggestion_id is None
    # Second call: nothing confirmed, nothing written.
    assert _unconfirm(pid) is None
    assert len(_audits(pid)) == 1 and len(_suggestions(pid)) == 1


def test_a_cleared_digital_photo_gains_its_folder_month_hint_once(phase6):
    pid = _photo(cd="2004-01-01", taken="2004-01-01 00:00:11", folder="_2004-01")
    res = _unconfirm(pid, reason="unset_camera_clock", rule="b")
    payloads = [s[3] for s in _suggestions(pid)]
    assert {"date": "2004-01-01", "precision": "month",
            "evidence": "export folder _2004-01"} in payloads
    assert res.folder_suggestion_id is not None
    # Re-confirm by hand and clear again: the hint is not added a second time.
    _q("update photos set capture_date = '2004-01-01', capture_date_confirmed = true "
       "where id = %s", (pid,))
    _unconfirm(pid)
    assert sum(1 for s in _suggestions(pid)
               if s[3].get("evidence", "").startswith("export folder")) == 1


def test_a_date_a_person_set_returns_as_a_human_suggestion(phase6):
    pid = _photo(cd="1962-03-01")
    _set_by_human(pid, "1962-03-01")
    _unconfirm(pid)
    [(source, *_rest)] = [s for s in _suggestions(pid) if s[3].get("reason") == "cleared"]
    assert source == "human"
    assert _audits(pid)[0][1]["source"] == "human"


def test_an_identical_pending_suggestion_is_reused_not_duplicated(phase6):
    pid = _photo(cd="1970-05-05", folder="Holiday")
    _q("insert into suggestions (photo_id, kind, source, confidence, payload) values "
       "(%s, 'date', 'ai', 0.6, '{\"date\": \"1970-05-05\", \"precision\": \"exact\"}')",
       (pid,))
    res = _unconfirm(pid)
    assert len(_suggestions(pid)) == 1
    assert res.suggestion_id == _q("select id from suggestions where photo_id = %s",
                                   (pid,), one=True)[0]


def test_report_reads_only_ingest_sourced_dates_and_apply_clears_the_chosen_rules(phase6):
    scan = _photo(cd="2025-11-07", taken="2025-11-07 12:07:57", is_scan=True,
                  folder="Batch 00033")
    clock = _photo(cd="2004-01-01", taken="2004-01-01 00:00:19", folder="_2004-01")
    far = _photo(cd="1999-01-15", taken="1999-01-15 10:00:00", folder="_1999-08")
    good = _photo(cd="1999-06-02", taken="1999-06-02 10:00:00", folder="_1999-06")
    human_scan = _photo(cd="1968-02-01", taken="1968-02-01 10:05:21", is_scan=True,
                        folder="Batch 00009")
    _set_by_human(human_scan, "1968-02-01")

    with dbmod.connection() as conn:
        rep = tool.build_report(conn, scan_roots=set())
    assert rep.total == 4                    # the human-set scan is not ingest-sourced
    flagged = {row.photo_id: hits for row, hits in rep.flagged}
    assert flagged == {scan: ["a"], clock: ["b"], far: ["c"]}
    text = tool.format_summary(rep)
    assert "Batch 00033" in text and str(clock) in text

    with dbmod.connection() as conn:
        conn.autocommit = False
        done = tool.apply(conn, rep, ["a", "b"])
        conn.commit()
    assert done["a"] == 1 and done["b"] == 1
    assert _photo_row(scan)[2] is False and _photo_row(clock)[2] is False
    assert _photo_row(far)[2] is True        # rule c not chosen
    assert _photo_row(good)[2] is True and _photo_row(human_scan)[2] is True
    assert _suggestions(clock)[0][3]["reason"] == "unset_camera_clock"
    assert _q("select count(*) from audit_log where action = 'date_hygiene.apply'",
              one=True)[0] == 1


def test_after_the_clear_only_the_digital_photo_is_due_for_metadata(phase6):
    """F3: Q9's gate already kept a scan's ingest date out of the file, so
    clearing it changes nothing there; a digital photo drops its date tags."""
    scan = _photo(cd="2025-11-07", taken="2025-11-07 12:07:57", is_scan=True,
                  folder="Batch 00033")
    clock = _photo(cd="2004-01-01", taken="2004-01-01 00:00:19", folder="_2004-01")
    with dbmod.connection() as conn:
        before = {c.photo_id: c.hash for c in meta_repo.load_candidates(conn)}
        assert next(c for c in meta_repo.load_candidates(conn)
                    if c.photo_id == clock).payload.get("date")
    _unconfirm(scan)
    _unconfirm(clock)
    with dbmod.connection() as conn:
        after = {c.photo_id: c for c in meta_repo.load_candidates(conn)}
    assert after[scan].hash == before[scan]
    assert after[clock].hash != before[clock]
    assert not after[clock].payload.get("date")
    assert not after[scan].payload.get("date")


class _PullClient:
    def __init__(self, audits):
        self._audits = audits

    def pull_confirmed(self, since):
        return {"cursor": "2026-10-30T00:00:00Z", "accepted_suggestions": [],
                "fact_audits": self._audits, "comments_summary": [], "likes_counts": []}


def test_a_web_clear_comes_down_without_a_local_suggestion(phase6, monkeypatch, tmp_path):
    pid = _photo(cd="2004-01-01", taken="2004-01-01 00:00:11", folder="_2004-01")
    monkeypatch.setattr(pull_mod, "pull_web_origin", lambda *a, **k: None)
    monkeypatch.setattr(pull_mod, "pull_web_edits", lambda *a, **k: None)
    client = _PullClient([{
        "id": 1, "action": "photo.capture_date.unconfirm", "entity_type": "photo",
        "entity_id": pid, "previous_value": {"capture_date": "2004-01-01"},
        "new_value": {"capture_date": None, "reason": "cleared", "via": "web.admin"},
    }])
    applied = pull_mod.pull_confirmed(client, tmp_path)
    assert applied == 1
    assert _photo_row(pid) == (None, "unknown", False)
    [(actor, _prev, new)] = _audits(pid)
    assert actor == "web" and new["via"] == "sync.pull.confirmed"
    # Only the folder-month hint is born here; the cleared value is the web's.
    assert [s[3]["evidence"] for s in _suggestions(pid)] == ["export folder _2004-01"]
    # Pulled twice (cursor replay): nothing more happens.
    assert pull_mod.pull_confirmed(client, tmp_path) == 0
