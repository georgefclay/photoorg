"""Phase 6 fix-up 8: `unknown` / `ignore` face review statuses.

- `repo.set_review_status` writes the new status + one audit row per
  face and returns the previous statuses (so the UI can undo).
- `repo.restore_review_status` reverts a single face (Z key).
- `unlabelled_faces_with_embeddings` excludes `ignore` faces but keeps
  `unknown` ones (so their clusters can later match against a labelled
  person's prototype).
- Reference queries (`person_prototypes`, `person_reference_means`)
  never take `ignore` faces — belt-and-braces since those are always
  unassigned anyway.
"""

from __future__ import annotations

import json

import pytest

from photoarchive import db as dbmod
from photoarchive.modes.faces import repo

from .phase6_fixtures import insert_master, insert_photo, phase6  # noqa: F401


def _insert_face(
    conn,
    *,
    photo_id: int,
    person_id: int | None = None,
    embedding: list[float] | None = None,
    review_status: str = "pending",
    is_disputed: bool = False,
    is_deleted: bool = False,
) -> int:
    source = "human" if person_id is not None else "ai"
    row = conn.execute(
        """
        insert into faces
          (photo_id, person_id, bbox, embedding, embedding_model,
           confidence, source, is_disputed, is_deleted, review_status)
        values (%s, %s, '{"x":0,"y":0,"w":80,"h":80}'::jsonb,
                %s, 'test', 0.95, %s, %s, %s, %s)
        returning id
        """,
        (photo_id, person_id, embedding or [0.1] * 512,
         source, is_disputed, is_deleted, review_status),
    ).fetchone()
    return int(row[0])


def _status(conn, face_id: int) -> str:
    return conn.execute(
        "select review_status from faces where id = %s", (face_id,)
    ).fetchone()[0]


def test_set_review_status_marks_faces_and_writes_audit(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        photo = insert_photo(conn)
        insert_master(conn, photo)
        f1 = _insert_face(conn, photo_id=photo)
        f2 = _insert_face(conn, photo_id=photo)
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = False
        prior = repo.set_review_status(
            conn, face_ids=[f1, f2], new_status="unknown", note="test",
        )
        conn.commit()
    # Prior statuses returned so the caller can undo.
    assert sorted(prior) == sorted([(f1, "pending"), (f2, "pending")])

    with dbmod.connection() as conn:
        conn.autocommit = True
        assert _status(conn, f1) == "unknown"
        assert _status(conn, f2) == "unknown"
        n_audit = conn.execute(
            """
            select count(*) from audit_log
            where entity_type='face' and action='face.review'
              and entity_id in (%s, %s)
            """,
            (f1, f2),
        ).fetchone()[0]
        assert n_audit == 2


def test_set_review_status_rejects_invalid_value(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        photo = insert_photo(conn)
        f1 = _insert_face(conn, photo_id=photo)
        conn.commit()
    with dbmod.connection() as conn:
        conn.autocommit = False
        with pytest.raises(ValueError):
            repo.set_review_status(conn, face_ids=[f1], new_status="bogus")


def test_restore_review_status_reverts_the_face(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        photo = insert_photo(conn)
        f = _insert_face(conn, photo_id=photo)
        conn.commit()
    with dbmod.connection() as conn:
        conn.autocommit = False
        repo.set_review_status(conn, face_ids=[f], new_status="ignore")
        conn.commit()
    with dbmod.connection() as conn:
        conn.autocommit = True
        assert _status(conn, f) == "ignore"
    with dbmod.connection() as conn:
        conn.autocommit = False
        repo.restore_review_status(conn, face_id=f, status="pending")
        conn.commit()
    with dbmod.connection() as conn:
        conn.autocommit = True
        assert _status(conn, f) == "pending"
        # Undo audit row present.
        n = conn.execute(
            """
            select count(*) from audit_log
            where entity_type='face' and action='face.review.undo'
              and entity_id = %s
            """,
            (f,),
        ).fetchone()[0]
        assert n == 1


def test_unlabelled_faces_excludes_ignore_keeps_unknown(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        photo = insert_photo(conn)
        pending = _insert_face(conn, photo_id=photo, review_status="pending")
        unknown = _insert_face(conn, photo_id=photo, review_status="unknown")
        ignored = _insert_face(conn, photo_id=photo, review_status="ignore")
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = True
        faces = repo.unlabelled_faces_with_embeddings(conn)
    ids = {f.id for f in faces}
    assert pending in ids
    assert unknown in ids
    assert ignored not in ids
    # Status carried on the row.
    by_id = {f.id: f for f in faces}
    assert by_id[pending].review_status == "pending"
    assert by_id[unknown].review_status == "unknown"


def test_person_prototypes_and_means_skip_ignore(phase6):
    """Assigned faces never carry 'ignore' in practice, but the
    reference queries guard against it defensively (spec item 3)."""
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = repo.create_person(
            conn, given_name="Guarded", middle_name=None, surname="Ref",
            maiden_name=None, nickname=None, suffix=None,
            birth_year=None, death_year=None, notes=None,
        )
        photo = insert_photo(conn)
        # A labelled face + a mistakenly-ignored labelled face — the
        # reference set must only take the labelled one.
        _insert_face(
            conn, photo_id=photo, person_id=pid,
            embedding=[1.0] * 512, review_status="pending",
        )
        _insert_face(
            conn, photo_id=photo, person_id=pid,
            embedding=[-1.0] * 512, review_status="ignore",
        )
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = True
        means = repo.person_reference_means(conn)
        protos = repo.person_prototypes(conn)
    # Mean is dominated by the +1 face; ignore face didn't cancel it out.
    assert pid in means
    assert (means[pid][:8] > 0.5).all()
    assert pid in protos
    assert (protos[pid][0][:8] > 0.5).all()


def test_set_review_status_empty_input_is_a_noop(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        assert repo.set_review_status(conn, face_ids=[], new_status="unknown") == []
