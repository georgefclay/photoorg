"""Merge two people round-trip: faces move, variants move (deduped),
loser is soft-deleted, audit rows are written for both directions."""

from __future__ import annotations

import pytest

from photoarchive import db as dbmod
from photoarchive.modes.faces import repo
from photoarchive.modes.faces.merge import merge_people

from .phase6_fixtures import insert_master, insert_photo, phase6  # noqa: F401


def test_merge_moves_faces_and_dedupes_variants_and_audits(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        winner_id = repo.create_person(
            conn, given_name="Margaret", middle_name=None, surname="Clay",
            maiden_name="Schmidt", nickname="Peggy",
            birth_year=1935, death_year=None, notes=None,
        )
        loser_id = repo.create_person(
            conn, given_name="Peggy", middle_name=None, surname="Clay",
            maiden_name=None, nickname=None,
            birth_year=None, death_year=None, notes=None,
        )
        # Some variants — a shared "Peggy" should not double-insert.
        conn.execute(
            """
            insert into person_name_variants (person_id, variant, kind)
            values (%s, 'Peggy', 'nickname')
            """,
            (winner_id,),
        )
        conn.execute(
            """
            insert into person_name_variants (person_id, variant, kind)
            values (%s, 'Peggy', 'nickname')
            """,
            (loser_id,),
        )
        conn.execute(
            """
            insert into person_name_variants (person_id, variant, kind)
            values (%s, 'Peg', 'nickname')
            """,
            (loser_id,),
        )
        # Faces on the loser
        photos = []
        for _ in range(3):
            pid = insert_photo(conn)
            insert_master(conn, pid)
            photos.append(pid)
            conn.execute(
                """
                insert into faces
                  (photo_id, person_id, bbox, embedding, embedding_model,
                   confidence, source, is_disputed, is_deleted)
                values (%s, %s, '{"x":0,"y":0,"w":10,"h":10}'::jsonb,
                        %s, 'buffalo_l', 0.9, 'human', false, false)
                """,
                (pid, loser_id, [0.1] * 512),
            )
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = False
        out = merge_people(conn, winner_id=winner_id, loser_id=loser_id,
                            reason="test")
        conn.commit()
    assert out["faces_moved"] == 3

    with dbmod.connection() as conn:
        conn.autocommit = True
        # Faces are now on the winner.
        n_win = conn.execute(
            "select count(*) from faces where person_id = %s", (winner_id,)
        ).fetchone()[0]
        assert n_win == 3
        n_lose = conn.execute(
            "select count(*) from faces where person_id = %s", (loser_id,)
        ).fetchone()[0]
        assert n_lose == 0

        # Loser is soft-deleted.
        assert conn.execute(
            "select is_deleted from people where id = %s", (loser_id,)
        ).fetchone()[0] is True

        # Variants: winner has {Peggy, Peg} (no dup Peggy).
        variants = {r[0] for r in conn.execute(
            "select variant from person_name_variants where person_id = %s",
            (winner_id,),
        ).fetchall()}
        assert variants == {"Peggy", "Peg"}
        # Loser has no variants.
        n_lv = conn.execute(
            "select count(*) from person_name_variants where person_id = %s",
            (loser_id,),
        ).fetchone()[0]
        assert n_lv == 0

        # Audit rows both directions.
        actions = {r[0] for r in conn.execute(
            "select action from audit_log where entity_type='person'"
        ).fetchall()}
        assert {"person.create", "person.merge", "person.merged_into"} <= actions


@pytest.mark.parametrize("kwargs,expected", [
    (dict(given_name="Margaret", surname="Clay", maiden_name="Schmidt",
          nickname="Peggy"), 'Margaret "Peggy" Clay (née Schmidt)'),
    (dict(given_name="George", surname="Clay", suffix="Jr."), "George Clay Jr."),
    (dict(given_name="John", surname="Smith", nickname="Jack", suffix="III"),
     'John "Jack" Smith III'),
    (dict(given_name="John", surname="Smith", nickname="Jack", suffix="III",
          maiden_name="Doe"), 'John "Jack" Smith III (née Doe)'),
    (dict(given_name="Anon"), "Anon"),
])
def test_display_name_trigger_includes_suffix(phase6, kwargs, expected):
    """Fix-up 4: the trigger-maintained display_name interpolates
    suffix between surname and maiden."""
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = repo.create_person(
            conn,
            given_name=kwargs.get("given_name"),
            middle_name=kwargs.get("middle_name"),
            surname=kwargs.get("surname"),
            maiden_name=kwargs.get("maiden_name"),
            nickname=kwargs.get("nickname"),
            birth_year=None, death_year=None, notes=None,
            suffix=kwargs.get("suffix"),
        )
        conn.commit()
    with dbmod.connection() as conn:
        conn.autocommit = True
        person = repo.get_person(conn, pid)
    assert person is not None
    assert person.display_name == expected


def test_person_prototypes_falls_back_to_mean_for_small_sample(phase6):
    """Fix-up 3: a person with fewer than 3 non-disputed embeddings gets
    a single-prototype fallback (their mean) rather than a k-means
    partition."""
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = repo.create_person(
            conn, given_name="Solo", middle_name=None, surname="Face",
            maiden_name=None, nickname=None,
            birth_year=None, death_year=None, notes=None,
        )
        photo = insert_photo(conn)
        conn.execute(
            """
            insert into faces
              (photo_id, person_id, bbox, embedding, embedding_model,
               confidence, source, is_disputed, is_deleted)
            values (%s, %s, '{"x":0,"y":0,"w":80,"h":80}'::jsonb,
                    %s, 'buffalo_l', 0.9, 'human', false, false)
            """,
            (photo, pid, [0.1] * 512),
        )
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = True
        protos = repo.person_prototypes(conn)
    assert pid in protos
    assert len(protos[pid]) == 1  # not enough to k-means; one mean


def test_person_prototypes_honours_quality_gate(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = repo.create_person(
            conn, given_name="Gated", middle_name=None, surname="X",
            maiden_name=None, nickname=None,
            birth_year=None, death_year=None, notes=None,
        )
        photo = insert_photo(conn)
        # One good face, one below the score threshold, one below the
        # short-edge threshold. Only the good one should feed the mean.
        conn.execute(
            """
            insert into faces
              (photo_id, person_id, bbox, embedding, embedding_model,
               confidence, source, is_disputed, is_deleted)
            values
              (%s, %s, '{"x":0,"y":0,"w":80,"h":80}'::jsonb,
                %s, 'buffalo_l', 0.95, 'human', false, false),
              (%s, %s, '{"x":0,"y":0,"w":80,"h":80}'::jsonb,
                %s, 'buffalo_l', 0.30, 'human', false, false),
              (%s, %s, '{"x":0,"y":0,"w":20,"h":20}'::jsonb,
                %s, 'buffalo_l', 0.95, 'human', false, false)
            """,
            (
                photo, pid, [1.0] * 512,
                photo, pid, [-1.0] * 512,
                photo, pid, [-1.0] * 512,
            ),
        )
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = True
        protos = repo.person_prototypes(
            conn, min_score=0.7, min_short_edge_px=40,
        )
    assert pid in protos
    # Only the +1 face survives the gate; the mean is dominated by +1.
    assert (protos[pid][0][:8] > 0.5).all()


def test_load_photo_context_returns_faces_and_back_transcription(phase6):
    """Fix-up 5: the preview needs photo dims, faces + person names,
    year/batch context, and back transcription in one call."""
    with dbmod.connection() as conn:
        conn.autocommit = False
        # Two people: one labelled, one for the second face.
        p_a = repo.create_person(
            conn, given_name="Adam", middle_name=None, surname="A",
            maiden_name=None, nickname=None, suffix=None,
            birth_year=None, death_year=None, notes=None,
        )
        photo = insert_photo(
            conn, width=1200, height=800, source_folder="Batch 0007",
            scan_sequence=42,
        )
        conn.execute(
            """
            update photos set capture_date = '1962-06-01',
                              capture_date_confirmed = true,
                              scan_batch = 'Batch 0007'
            where id = %s
            """,
            (photo,),
        )
        # Two faces on the same photo: labelled + unlabelled.
        conn.execute(
            """
            insert into faces
              (photo_id, person_id, bbox, embedding, embedding_model,
               confidence, source, is_disputed, is_deleted)
            values
              (%s, %s, '{"x":100,"y":100,"w":80,"h":80}'::jsonb,
                %s, 'buffalo_l', 0.95, 'human', false, false),
              (%s, NULL, '{"x":300,"y":200,"w":90,"h":90}'::jsonb,
                %s, 'buffalo_l', 0.90, 'ai', false, false)
            """,
            (photo, p_a, [0.1] * 512, photo, [0.2] * 512),
        )
        # One back with transcription.
        conn.execute(
            """
            insert into photo_backs
              (photo_id, master_path, sha256, working_path,
               transcribed_text, transcription_confidence)
            values (%s, %s, %s, null, 'Summer 1962 · beach', 0.9)
            """,
            (photo, f"backs/{photo}.jpg", "b" * 64),
        )
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = True
        context = repo.load_photo_context(conn, photo)
    assert context is not None
    assert context.width == 1200 and context.height == 800
    assert context.capture_year == 1962
    assert context.scan_batch == "Batch 0007"
    assert context.scan_sequence == 42
    assert context.back_transcription == "Summer 1962 · beach"
    assert len(context.faces) == 2
    labelled = [f for f in context.faces if f.person_id is not None]
    assert len(labelled) == 1
    assert labelled[0].person_name == "Adam A"


def test_load_photo_context_returns_none_for_missing_photo(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = True
        context = repo.load_photo_context(conn, 999999)
    assert context is None


def test_person_reference_means_excludes_disputed(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = repo.create_person(
            conn, given_name="X", middle_name=None, surname="Y",
            maiden_name=None, nickname=None,
            birth_year=None, death_year=None, notes=None,
        )
        photo = insert_photo(conn)
        # One good face + one disputed face
        conn.execute(
            """
            insert into faces
              (photo_id, person_id, bbox, embedding, embedding_model,
               confidence, source, is_disputed, is_deleted)
            values
              (%s, %s, '{"x":0,"y":0,"w":10,"h":10}'::jsonb,
                %s, 'buffalo_l', 0.9, 'human', false, false),
              (%s, %s, '{"x":0,"y":0,"w":10,"h":10}'::jsonb,
                %s, 'buffalo_l', 0.9, 'human', true, false)
            """,
            (photo, pid, [1.0] * 8 + [0.0] * 504,
             photo, pid, [-1.0] * 8 + [0.0] * 504),
        )
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = True
        means = repo.person_reference_means(conn)
    # Only the non-disputed face should contribute; mean's first eight values
    # should be +1 (i.e. no cancellation from the disputed opposite vector).
    assert pid in means
    m = means[pid]
    assert (m[:8] > 0.5).all()
