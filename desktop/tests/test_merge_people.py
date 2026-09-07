"""Merge two people round-trip: faces move, variants move (deduped),
loser is soft-deleted, audit rows are written for both directions."""

from __future__ import annotations

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
