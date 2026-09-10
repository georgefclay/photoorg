"""Fix-ups 9 and 10: bbox editing, name variants, assign-dialog search,
back transcription confirm/edit, accept-date promotion, ✎-badge lookup.
"""

from __future__ import annotations

import json

import pytest

from photoarchive import db as dbmod
from photoarchive.modes.faces import repo

from .phase6_fixtures import insert_master, insert_photo, phase6  # noqa: F401


def _insert_face(
    conn, *, photo_id: int, person_id: int | None = None,
    embedding: list[float] | None = None,
    bbox: dict | None = None,
) -> int:
    source = "human" if person_id is not None else "ai"
    row = conn.execute(
        """
        insert into faces
          (photo_id, person_id, bbox, embedding, embedding_model,
           confidence, source, is_disputed, is_deleted, review_status)
        values (%s, %s, %s::jsonb, %s, 'test', 0.9, %s, false, false, 'pending')
        returning id
        """,
        (photo_id, person_id,
         json.dumps(bbox or {"x": 0, "y": 0, "w": 40, "h": 40}),
         embedding or [0.1] * 512, source),
    ).fetchone()
    return int(row[0])


# --- fix-up 9: bbox edit --------------------------------------------------


def test_update_face_bbox_writes_audit_and_sets_source_human(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = insert_photo(conn)
        fid = _insert_face(conn, photo_id=pid, bbox={"x": 10, "y": 20, "w": 30, "h": 40})
        conn.commit()
    new_bbox = {"x": 100, "y": 120, "w": 30, "h": 40}
    with dbmod.connection() as conn:
        conn.autocommit = False
        prev = repo.update_face_bbox(conn, face_id=fid, new_bbox=new_bbox)
        conn.commit()
    assert prev == {"x": 10, "y": 20, "w": 30, "h": 40}
    with dbmod.connection() as conn:
        conn.autocommit = True
        row = conn.execute("select bbox, source::text from faces where id = %s", (fid,)).fetchone()
        assert row[1] == "human"
        stored = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        assert stored == new_bbox
        n_audit = conn.execute(
            """
            select count(*) from audit_log
            where entity_type='face' and entity_id=%s
              and action='face.bbox_edit'
            """,
            (fid,),
        ).fetchone()[0]
        assert n_audit == 1


def test_mark_and_refresh_embedding_stale(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = insert_photo(conn)
        fid = _insert_face(conn, photo_id=pid)
        conn.commit()
    with dbmod.connection() as conn:
        conn.autocommit = False
        repo.mark_face_embedding_stale(conn, face_id=fid, stale=True)
        conn.commit()
    with dbmod.connection() as conn:
        conn.autocommit = True
        stale = conn.execute(
            "select embedding_stale from faces where id = %s", (fid,)
        ).fetchone()[0]
        assert stale is True
        listing = repo.stale_faces(conn)
        assert any(f.id == fid for f in listing)

    with dbmod.connection() as conn:
        conn.autocommit = False
        repo.refresh_face_embedding(
            conn, face_id=fid, embedding=[0.5] * 512, embedding_model="buffalo_l",
        )
        conn.commit()
    with dbmod.connection() as conn:
        conn.autocommit = True
        row = conn.execute(
            "select embedding_stale, embedding_model from faces where id = %s", (fid,)
        ).fetchone()
        assert row[0] is False
        assert row[1] == "buffalo_l"


# --- fix-up 9: people editing / variants / search -------------------------


def test_add_and_remove_name_variant_writes_audit(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = repo.create_person(
            conn, given_name="Margaret", middle_name=None, surname="Clay",
            maiden_name="Schmidt", nickname="Peggy", suffix=None,
            birth_year=1935, death_year=None, notes=None,
        )
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = False
        vid = repo.add_name_variant(conn, person_id=pid, variant="Peg", kind="nickname")
        conn.commit()
    assert vid is not None

    # Duplicate should return None (case-insensitive).
    with dbmod.connection() as conn:
        conn.autocommit = False
        dup = repo.add_name_variant(conn, person_id=pid, variant="peg", kind="nickname")
        conn.commit()
    assert dup is None

    with dbmod.connection() as conn:
        conn.autocommit = True
        variants = repo.list_name_variants(conn, pid)
    assert len(variants) == 1
    variant_id, variant_text, kind = variants[0]
    assert variant_text == "Peg"

    with dbmod.connection() as conn:
        conn.autocommit = False
        repo.remove_name_variant(conn, variant_id)
        conn.commit()
    with dbmod.connection() as conn:
        conn.autocommit = True
        assert repo.list_name_variants(conn, pid) == []
        actions = {r[0] for r in conn.execute(
            "select action from audit_log where entity_type='person' and entity_id=%s",
            (pid,),
        ).fetchall()}
        assert {"person.create", "person.variant.add", "person.variant.remove"} <= actions


def test_search_people_prefix_and_variant(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        # Two people; one with a variant.
        p_meg = repo.create_person(
            conn, given_name="Margaret", middle_name=None, surname="Clay",
            maiden_name="Schmidt", nickname="Peggy", suffix=None,
            birth_year=None, death_year=None, notes=None,
        )
        p_other = repo.create_person(
            conn, given_name="George", middle_name=None, surname="Clay",
            maiden_name=None, nickname=None, suffix="Jr.",
            birth_year=None, death_year=None, notes=None,
        )
        repo.add_name_variant(conn, person_id=p_meg, variant="Peg", kind="nickname")
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = True
        # Prefix on given name hits Margaret.
        peg = repo.search_people(conn, "marg")
    assert peg[0].id == p_meg
    # Prefix on variant should also surface her.
    with dbmod.connection() as conn:
        conn.autocommit = True
        pegvar = repo.search_people(conn, "peg")
    assert peg[0].id in {r.id for r in pegvar}
    # Empty query returns all people.
    with dbmod.connection() as conn:
        conn.autocommit = True
        all_p = repo.search_people(conn, "")
    assert {p.id for p in all_p} >= {p_meg, p_other}


def test_photos_for_person_and_face_count(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = repo.create_person(
            conn, given_name="X", middle_name=None, surname="Y",
            maiden_name=None, nickname=None, suffix=None,
            birth_year=None, death_year=None, notes=None,
        )
        ph1 = insert_photo(conn)
        ph2 = insert_photo(conn)
        _insert_face(conn, photo_id=ph1, person_id=pid)
        _insert_face(conn, photo_id=ph1, person_id=pid)
        _insert_face(conn, photo_id=ph2, person_id=pid)
        conn.commit()
    with dbmod.connection() as conn:
        conn.autocommit = True
        ids = repo.photos_for_person(conn, pid)
        n = repo.face_count_for_person(conn, pid)
    assert set(ids) == {ph1, ph2}
    assert n == 3


# --- fix-up 10: back transcription confirm / edit -------------------------


def _insert_back(conn, *, photo_id: int, text: str = "sample text",
                 confidence: float = 0.8, confirmed: bool = False) -> int:
    import os
    row = conn.execute(
        """
        insert into photo_backs
          (photo_id, master_path, sha256, transcribed_text,
           transcription_confidence, transcription_confirmed)
        values (%s, %s, %s, %s, %s, %s)
        returning id
        """,
        (photo_id, f"back/{os.urandom(8).hex()}.jpg",
         os.urandom(32).hex(), text, confidence, confirmed),
    ).fetchone()
    return int(row[0])


def test_confirm_back_transcription_without_edit(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = insert_photo(conn)
        bid = _insert_back(conn, photo_id=pid, text="Mar 1962")
        conn.commit()
    with dbmod.connection() as conn:
        conn.autocommit = False
        repo.confirm_back_transcription(conn, photo_back_id=bid, edited_text=None)
        conn.commit()
    with dbmod.connection() as conn:
        conn.autocommit = True
        text, confirmed = conn.execute(
            "select transcribed_text, transcription_confirmed from photo_backs where id = %s",
            (bid,),
        ).fetchone()
        assert text == "Mar 1962"
        assert confirmed is True
        actions = [r[0] for r in conn.execute(
            "select action from audit_log where entity_type='photo_back' and entity_id=%s order by id",
            (bid,),
        ).fetchall()]
        assert "back.transcription_confirm" in actions


def test_confirm_back_transcription_with_edit(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = insert_photo(conn)
        bid = _insert_back(conn, photo_id=pid, text="Mar 1962")
        conn.commit()
    with dbmod.connection() as conn:
        conn.autocommit = False
        repo.confirm_back_transcription(
            conn, photo_back_id=bid, edited_text="March 1962 · Peggy at the porch",
        )
        conn.commit()
    with dbmod.connection() as conn:
        conn.autocommit = True
        text, confirmed = conn.execute(
            "select transcribed_text, transcription_confirmed from photo_backs where id = %s",
            (bid,),
        ).fetchone()
        assert text == "March 1962 · Peggy at the porch"
        assert confirmed is True
        actions = {r[0] for r in conn.execute(
            "select action from audit_log where entity_type='photo_back' and entity_id=%s",
            (bid,),
        ).fetchall()}
        assert "back.transcription_edit" in actions


# --- fix-up 10: accept-date promotion + 409 conflict ---------------------


def _insert_date_suggestion(conn, *, photo_id: int, date: str, precision: str,
                              confidence: float | None = 0.4) -> int:
    row = conn.execute(
        """
        insert into suggestions
          (photo_id, kind, payload, confidence, source, status)
        values (%s, 'date', %s::jsonb, %s, 'ai', 'pending')
        returning id
        """,
        (photo_id,
         json.dumps({"date": date, "precision": precision,
                     "evidence": "unit test"}),
         confidence),
    ).fetchone()
    return int(row[0])


def test_promote_date_suggestion_sets_capture_date(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = insert_photo(conn)
        sid = _insert_date_suggestion(conn, photo_id=pid, date="1962-03-01",
                                        precision="month")
        conn.commit()
    with dbmod.connection() as conn:
        conn.autocommit = False
        out = repo.promote_date_suggestion(conn, suggestion_id=sid)
        conn.commit()
    assert out.accepted is True
    assert out.conflict is False
    with dbmod.connection() as conn:
        conn.autocommit = True
        row = conn.execute(
            """
            select capture_date::text, capture_date_precision::text,
                   capture_date_confirmed
            from photos where id = %s
            """,
            (pid,),
        ).fetchone()
        assert row == ("1962-03-01", "month", True)
        status = conn.execute(
            "select status::text from suggestions where id = %s", (sid,),
        ).fetchone()[0]
        assert status == "accepted"
        # Audit row present.
        n = conn.execute(
            """
            select count(*) from audit_log
            where entity_type='photo' and entity_id=%s
              and action='photo.capture_date.set'
            """,
            (pid,),
        ).fetchone()[0]
        assert n == 1


def test_promote_date_suggestion_flags_409_when_confirmed_date_exists(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = insert_photo(conn, capture_date_confirmed=True)
        conn.execute(
            """
            update photos
            set capture_date = '1955-05-05'::date,
                capture_date_precision = 'exact'
            where id = %s
            """,
            (pid,),
        )
        sid = _insert_date_suggestion(conn, photo_id=pid, date="1962-03-01",
                                        precision="month")
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = False
        out = repo.promote_date_suggestion(conn, suggestion_id=sid)
        conn.commit()
    assert out.accepted is False
    assert out.conflict is True
    assert out.existing_date == "1955-05-05"
    assert out.new_date == "1962-03-01"

    # With allow_overwrite the promotion goes through.
    with dbmod.connection() as conn:
        conn.autocommit = False
        out2 = repo.promote_date_suggestion(
            conn, suggestion_id=sid, allow_overwrite=True,
        )
        conn.commit()
    assert out2.accepted is True
    with dbmod.connection() as conn:
        conn.autocommit = True
        date_row = conn.execute(
            "select capture_date::text from photos where id = %s", (pid,)
        ).fetchone()[0]
        assert date_row == "1962-03-01"


def test_photo_ids_with_backs_returns_only_those_with_backs(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        with_back = insert_photo(conn)
        without = insert_photo(conn)
        _insert_back(conn, photo_id=with_back)
        conn.commit()
    with dbmod.connection() as conn:
        conn.autocommit = True
        found = repo.photo_ids_with_backs(conn, [with_back, without])
    assert found == {with_back}


def test_load_photo_context_populates_backs_dates_and_description(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = insert_photo(conn)
        _insert_back(conn, photo_id=pid, text="August 1975", confidence=0.9)
        _insert_date_suggestion(conn, photo_id=pid, date="1975-08-01",
                                  precision="month", confidence=0.4)
        conn.execute(
            """
            insert into suggestions
              (photo_id, kind, payload, source, status)
            values (%s, 'description', '{"text":"beach trip"}'::jsonb,
                    'ai', 'pending')
            """,
            (pid,),
        )
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = True
        ctx = repo.load_photo_context(conn, pid)
    assert ctx is not None
    assert len(ctx.backs) == 1
    assert ctx.backs[0].transcribed_text == "August 1975"
    assert ctx.date_suggestions
    assert ctx.date_suggestions[0].date == "1975-08-01"
    assert ctx.description_suggestion == "beach trip"
