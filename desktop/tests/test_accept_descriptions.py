"""Phase 13 fix-up 2 — bulk-accept the AI descriptions.

* promote writes `description_ai`, marks the suggestion accepted, one
  `photo.description.set` audit row with previous and new, one batch row;
* the folder-name `import` descriptions are rejected with the note;
* two AI rows on one photo: the newest wins, the older is rejected;
* a photo whose description a person already set is left alone;
* junk photos are out of scope; a second run finds 0 due;
* the search index moves the text from weight C (pending) to A (the fact);
* the writer: due count equals the accepted count, the file carries the
  prefixed line, and a second write run is 0 due;
* a pulled web accept or admin edit marks the photo writer-due, and an
  emptied description leaves the file without the line.
"""
from __future__ import annotations

import json

import psycopg
import pytest

from photoarchive import db as dbmod
from photoarchive.modes.metadata import descriptions
from photoarchive.modes.sync import pull as pull_mod

from .conftest import TEST_DATABASE_URL
from .phase6_fixtures import phase6  # noqa: F401
from .test_metadata_writer import (  # noqa: F401
    EXIFTOOL, _due_ids, _photo, _q, _read, _write, _wp, mw,
)


def _sug(pid, text, *, source="ai", created="now()"):
    payload = {"text": text, "prompt_version": "describe.v1"} if source == "ai" \
        else {"text": text, "evidence": "scan folder name"}
    return _q(f"""insert into suggestions (photo_id, kind, payload, source, status, created_at)
                  values (%s, 'description', %s::jsonb, %s, 'pending', {created})
                  returning id""", (pid, json.dumps(payload), source), one=True)[0]


def _status(sid):
    return _q("select status::text, resolution_note from suggestions where id = %s", (sid,), one=True)


def _desc(pid):
    return _q("select description_ai from photos where id = %s", (pid,), one=True)[0]


def _run():
    with dbmod.connection() as conn:
        conn.autocommit = False
        p = descriptions.plan(conn)
        conn.rollback()
        res = descriptions.apply(conn, p, chunk=2)
    return p, res


def _plan():
    with dbmod.connection() as conn:
        p = descriptions.plan(conn)
        conn.rollback()
    return p


def test_bulk_accept_promotes_rejects_and_is_idempotent(mw):
    a = _photo(mw, seq=1)
    b = _photo(mw, seq=2)
    c = _photo(mw, seq=3)
    human = _photo(mw, seq=4, description_ai="Grandma's porch, 1962.")
    junk = _photo(mw, seq=5)
    _q("update photos set triage_status = 'junk', is_deleted = true where id = %s", (junk,))

    sa = _sug(a, "Three children on a porch.")
    old_b = _sug(b, "An older guess.", created="now() - interval '1 day'")
    sb = _sug(b, "A dog on a lawn.")
    sc = _sug(c, "A lake at dusk.")
    imp = _sug(c, "Summer 1992 - Canada", source="import")
    sh = _sug(human, "A house.")
    sj = _sug(junk, "Blur.")

    p, res = _run()
    assert res.counts == {"accepted": 3, "superseded": 1, "import_rejected": 1}
    assert len(p.conflict) == 1 and p.out_of_scope == 1

    assert _desc(a) == "Three children on a porch."
    assert _desc(b) == "A dog on a lawn."
    assert _status(sa)[0] == "accepted"
    assert _status(old_b) == ("rejected", descriptions.SUPERSEDED_NOTE)
    assert _status(imp) == ("rejected", descriptions.IMPORT_NOTE)
    # A person's words are never overwritten, and junk is out of scope.
    assert _desc(human) == "Grandma's porch, 1962." and _status(sh)[0] == "pending"
    assert _desc(junk) is None and _status(sj)[0] == "pending"

    [(prev, new)] = _q("""select previous_value, new_value from audit_log
                           where action = 'photo.description.set' and entity_id = %s""", (a,))
    assert prev == {"description_ai": None}
    assert new["description_ai"] == "Three children on a porch."
    assert new["suggestion_id"] == sa and new["batch_id"] == res.batch_id
    [(batch,)] = _q("select new_value from audit_log where action = 'description.bulk_accept.batch'")
    assert batch["batch_id"] == res.batch_id and batch["counts"]["accepted"] == 3
    assert _q("select count(*) from audit_log where action = 'suggestion.reject' "
              "and new_value->>'batch_id' = %s", (res.batch_id,), one=True)[0] == 2

    # Second run: nothing due, nothing written.
    assert _plan().due == 0
    _, res2 = _run()
    assert sum(res2.counts.values()) == 0


def test_search_moves_the_text_from_weight_c_to_a(mw):
    pid = _photo(mw)
    _sug(pid, "A canoe on the water.")

    def weight():
        tsv = _q("select tsv::text from photo_search where photo_id = %s", (pid,), one=True)[0]
        return [t for t in tsv.split() if t.startswith("'cano")]

    assert weight() and all(t.endswith("C") for t in weight()), weight()
    _run()
    assert weight() and all(t.endswith("A") for t in weight()), weight()


def test_writer_due_count_equals_accepted_and_file_carries_the_line(mw):
    ids = [_photo(mw, seq=i) for i in range(1, 4)]
    _write(mw)
    assert _due_ids() == []
    for pid in ids[:2]:
        _sug(pid, f"Photo {pid} at the lake.")
    _sug(ids[2], "Summer 1992 - Canada", source="import")
    _, res = _run()
    assert res.counts["accepted"] == 2
    assert sorted(_due_ids()) == sorted(ids[:2]), "exactly the accepted photos are due"
    _write(mw)
    assert _due_ids() == []
    assert _read(mw, _wp(ids[0]))["XMP-dc:Description"] == \
        f"AI-generated description: Photo {ids[0]} at the lake."
    assert "XMP-dc:Description" not in _read(mw, _wp(ids[2]))


class _PullClient:
    def __init__(self, audits):
        self._audits = audits

    def pull_confirmed(self, since):
        return {"cursor": "2026-11-05T00:00:00Z", "accepted_suggestions": [],
                "fact_audits": self._audits, "comments_summary": [], "likes_counts": []}


@pytest.mark.parametrize("action", ["photo.description.set", "photo.description.edit"])
def test_a_pulled_web_description_marks_the_photo_writer_due(mw, monkeypatch, tmp_path, action):
    pid = _photo(mw, description_ai="A porch.")
    _write(mw)
    assert _due_ids() == []
    monkeypatch.setattr(pull_mod, "pull_web_origin", lambda *a, **k: None)
    monkeypatch.setattr(pull_mod, "pull_web_edits", lambda *a, **k: None)

    def pull(text):
        return pull_mod.pull_confirmed(_PullClient([{
            "id": 1, "action": action, "entity_type": "photo", "entity_id": pid,
            "new_value": {"description_ai": text},
        }]), tmp_path)

    assert pull("A porch in Ohio.") == 1
    assert _desc(pid) == "A porch in Ohio."
    [(prev, new)] = _q("select previous_value, new_value from audit_log "
                       "where action = %s and actor = 'web'", (action,))
    assert prev == {"description_ai": "A porch."} and new["description_ai"] == "A porch in Ohio."
    assert _due_ids() == [pid]
    _write(mw)
    assert _read(mw, _wp(pid))["XMP-dc:Description"] == "AI-generated description: A porch in Ohio."

    # An emptied description removes the line from the file.
    pull("   ")
    assert _desc(pid) is None
    assert _due_ids() == [pid]
    _write(mw)
    assert "XMP-dc:Description" not in _read(mw, _wp(pid))
