"""Phase 15 fix-up 1 — the last two real deletes.

Phase 15 gave `album_photos` and `photo_places` soft-delete because a
*removal* could not otherwise reach the VM: the push only upserts, so a
hard-deleted row simply stopped being sent and the web kept what it had.
Two tables were left doing hard deletes and both have the same fault:

* `person_name_variants` — a nickname removed here went on showing on the
  web forever;
* `place_aliases` — "no real deletes, ever" has no exception for text
  that happens to be its own key.

The cases below are the ones that decide whether the fix actually works:
the row survives, the audit row carries what was there before, re-adding
brings it back rather than failing on the key, a merge does not resurrect
a name somebody removed, and `person_search` stops matching it.
"""

from __future__ import annotations

import pytest

from photoarchive import db as dbmod
from photoarchive.id_ranges import WEB_ID_FLOOR
from photoarchive.modes.corrections import places as places_repo
from photoarchive.modes.corrections import repo as corrections_repo
from photoarchive.modes.faces import repo as faces_repo
from photoarchive.modes.faces.merge import merge_people
from photoarchive.modes.sync.push import _META_STAGES

from .phase6_fixtures import insert_master, insert_photo, phase6  # noqa: F401


def _stage(name):
    for stage, sql, marshal in _META_STAGES:
        if stage == name:
            return sql, marshal
    raise AssertionError(f"no push stage named {name}")


def _rows(conn, sql, params=None):
    from psycopg.rows import dict_row
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# ---------------------------------------------------------------------------
# person_name_variants
# ---------------------------------------------------------------------------

def test_removing_a_variant_keeps_the_row_and_audits_the_old_name(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = faces_repo.create_person(
            conn, given_name="Margaret", middle_name=None, surname="Clay",
            maiden_name=None, nickname="Peggy",
            birth_year=None, death_year=None, notes=None,
        )
        vid = faces_repo.add_name_variant(conn, person_id=pid, variant="Peg")
        conn.commit()
        assert vid is not None

        faces_repo.remove_name_variant(conn, vid)
        conn.commit()

        row = conn.execute(
            """
            select is_deleted, deleted_at, edited_on_desktop_at, variant
              from person_name_variants where id = %s
            """,
            (vid,),
        ).fetchone()
        assert row is not None, "the row must survive - no real deletes, ever"
        assert row[0] is True
        assert row[1] is not None
        # The human-edit stamp is what lets `sync_web_edit_wins` settle the
        # removal against a web edit of the same row.
        assert row[2] is not None
        assert row[3] == "Peg", "the name itself is kept, for the audit and a restore"

        # Gone from the live list.
        assert "Peg" not in [v for _, v, _ in faces_repo.list_name_variants(conn, pid)]

        prev, new = conn.execute(
            """
            select previous_value, new_value from audit_log
             where action = 'person.variant_remove' and entity_id = %s
            """,
            (pid,),
        ).fetchone()
        assert prev["variant"] == "Peg" and prev["is_deleted"] is False
        assert new["is_deleted"] is True


def test_re_adding_a_removed_variant_brings_back_the_same_row(phase6):
    """A plain insert would hit `person_name_variants_uq` and the name
    would be un-re-addable, because the row it collides with is the one
    that was removed."""
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = faces_repo.create_person(
            conn, given_name="Margaret", middle_name=None, surname="Clay",
            maiden_name=None, nickname=None,
            birth_year=None, death_year=None, notes=None,
        )
        vid = faces_repo.add_name_variant(conn, person_id=pid, variant="Peg")
        faces_repo.remove_name_variant(conn, vid)
        conn.commit()

        again = faces_repo.add_name_variant(conn, person_id=pid, variant="Peg")
        conn.commit()
        assert again == vid, "the same row comes back, so the web's copy matches"
        assert conn.execute(
            "select is_deleted from person_name_variants where id = %s", (vid,)
        ).fetchone()[0] is False
        assert "Peg" in [v for _, v, _ in faces_repo.list_name_variants(conn, pid)]

        # A case-differing re-add matches too (the unique is on lower()).
        faces_repo.remove_name_variant(conn, vid)
        conn.commit()
        third = faces_repo.add_name_variant(conn, person_id=pid, variant="PEG")
        conn.commit()
        assert third == vid
        assert conn.execute(
            "select variant from person_name_variants where id = %s", (vid,)
        ).fetchone()[0] == "PEG", "the caller's spelling wins"

        # Adding one that is already live is still a no-op.
        assert faces_repo.add_name_variant(conn, person_id=pid, variant="peg") is None


def test_person_search_stops_matching_a_removed_variant(phase6):
    """The whole point of a nickname is that search finds the person by
    it, so the index has to drop it — and come back when it does."""
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = faces_repo.create_person(
            conn, given_name="Margaret", middle_name=None, surname="Clay",
            maiden_name=None, nickname=None,
            birth_year=None, death_year=None, notes=None,
        )
        vid = faces_repo.add_name_variant(conn, person_id=pid, variant="Pegleg")
        conn.commit()

        def tokens() -> set[str]:
            return {
                r[0] for r in conn.execute(
                    "select token from person_search where person_id = %s", (pid,)
                ).fetchall()
            }

        assert "pegleg" in tokens()

        faces_repo.remove_name_variant(conn, vid)
        conn.commit()
        assert "pegleg" not in tokens(), \
            "a removed nickname must stop finding the person"
        assert "margaret" in tokens(), "the real names are untouched"

        faces_repo.add_name_variant(conn, person_id=pid, variant="Pegleg")
        conn.commit()
        assert "pegleg" in tokens()


def test_a_merge_does_not_resurrect_a_removed_variant(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        winner = faces_repo.create_person(
            conn, given_name="Margaret", middle_name=None, surname="Clay",
            maiden_name=None, nickname=None,
            birth_year=None, death_year=None, notes=None,
        )
        loser = faces_repo.create_person(
            conn, given_name="Peggy", middle_name=None, surname="Clay",
            maiden_name=None, nickname=None,
            birth_year=None, death_year=None, notes=None,
        )
        keep = faces_repo.add_name_variant(conn, person_id=loser, variant="Peg")
        gone = faces_repo.add_name_variant(conn, person_id=loser, variant="Pegster")
        faces_repo.remove_name_variant(conn, gone)
        conn.commit()

        merge_people(conn, winner_id=winner, loser_id=loser)
        conn.commit()

        live = {v for _, v, _ in faces_repo.list_name_variants(conn, winner)}
        assert live == {"Peg"}, "only live variants move"
        # The removed one stays removed, and stays on the loser rather than
        # silently becoming the winner's.
        row = conn.execute(
            "select person_id, is_deleted from person_name_variants where id = %s",
            (gone,),
        ).fetchone()
        assert row[0] == loser and row[1] is True
        assert conn.execute(
            "select person_id from person_name_variants where id = %s", (keep,)
        ).fetchone()[0] == winner


def test_a_merge_soft_deletes_the_colliding_leftovers(phase6):
    """Variants the winner already has under another id used to be hard
    deleted. Those rows are pushed, so the web would have been left
    showing a nickname attached to a person who no longer exists."""
    with dbmod.connection() as conn:
        conn.autocommit = False
        winner = faces_repo.create_person(
            conn, given_name="Margaret", middle_name=None, surname="Clay",
            maiden_name=None, nickname=None,
            birth_year=None, death_year=None, notes=None,
        )
        loser = faces_repo.create_person(
            conn, given_name="Peggy", middle_name=None, surname="Clay",
            maiden_name=None, nickname=None,
            birth_year=None, death_year=None, notes=None,
        )
        faces_repo.add_name_variant(conn, person_id=winner, variant="Peg")
        collides = faces_repo.add_name_variant(conn, person_id=loser, variant="Peg")
        conn.commit()

        merge_people(conn, winner_id=winner, loser_id=loser)
        conn.commit()

        row = conn.execute(
            "select person_id, is_deleted from person_name_variants where id = %s",
            (collides,),
        ).fetchone()
        assert row is not None, "the row must survive so the removal can travel"
        assert row[0] == loser and row[1] is True


def test_the_push_carries_the_variant_flag(phase6):
    sql, marshal = _stage("person_name_variants")
    with dbmod.connection() as conn:
        conn.autocommit = False
        conn.execute(
            "insert into people (id, given_name, surname) values (1, 'Ann', 'B')")
        conn.execute(
            """
            insert into person_name_variants (id, person_id, variant, kind, is_deleted, deleted_at)
            values (1, 1, 'Annie', 'nickname', true, now()),
                   (2, 1, 'Nan', 'nickname', false, null)
            """
        )
        conn.commit()
        items = {i["variant"]: i for i in
                 (marshal(r) for r in _rows(conn, sql, {"web_id_floor": WEB_ID_FLOOR}))}

    # Both travel. A removed variant that stopped being sent is exactly how
    # the web ended up keeping it forever.
    assert items["Annie"]["is_deleted"] is True
    assert items["Annie"]["deleted_at"]
    assert items["Nan"]["is_deleted"] is False


# ---------------------------------------------------------------------------
# place_aliases
# ---------------------------------------------------------------------------

def test_removing_an_alias_keeps_the_row_and_audits_it(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        place = conn.execute(
            "insert into places (name) values ('Banff') returning id"
        ).fetchone()[0]
        places_repo.add_alias(conn, place, "Banff Springs")
        conn.commit()

        assert places_repo.remove_alias(conn, place, "Banff Springs")
        conn.commit()

        row = conn.execute(
            """
            select alias, is_deleted, deleted_at from place_aliases
             where place_id = %s
            """,
            (place,),
        ).fetchone()
        assert row is not None, "the row must survive - no real deletes, ever"
        assert row[0] == "Banff Springs" and row[1] is True and row[2] is not None
        assert places_repo.list_aliases(conn, place) == []

        prev, new = conn.execute(
            """
            select previous_value, new_value from audit_log
             where action = 'place.alias_remove' and entity_id = %s
            """,
            (place,),
        ).fetchone()
        assert prev["alias"] == "Banff Springs" and prev["is_deleted"] is False
        assert new["is_deleted"] is True

        # Removing it twice is a no-op, not a second audit row.
        assert places_repo.remove_alias(conn, place, "Banff Springs") is False


def test_re_adding_a_removed_alias_brings_it_back(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        place = conn.execute(
            "insert into places (name) values ('Banff') returning id"
        ).fetchone()[0]
        places_repo.add_alias(conn, place, "Banff Springs")
        places_repo.remove_alias(conn, place, "Banff Springs")
        conn.commit()

        assert places_repo.add_alias(conn, place, "Banff Springs")
        conn.commit()
        assert places_repo.list_aliases(conn, place) == [("Banff Springs", "alias")]
        # Still one row, not two.
        assert conn.execute(
            "select count(*) from place_aliases where place_id = %s", (place,)
        ).fetchone()[0] == 1

        # And a case-differing re-add matches the same row, because the
        # unique is on lower(alias).
        places_repo.remove_alias(conn, place, "Banff Springs")
        conn.commit()
        assert places_repo.add_alias(conn, place, "banff springs")
        conn.commit()
        assert conn.execute(
            "select count(*) from place_aliases where place_id = %s", (place,)
        ).fetchone()[0] == 1
        assert places_repo.list_aliases(conn, place) == [("banff springs", "alias")]


def test_the_push_sends_live_aliases_only(phase6):
    """The route makes the web match the set it is sent, so a removed
    alias must be absent from it — that absence is the removal."""
    sql, marshal = _stage("place_aliases")
    with dbmod.connection() as conn:
        conn.autocommit = False
        place = conn.execute(
            "insert into places (id, name) values (1, 'Banff') returning id"
        ).fetchone()[0]
        places_repo.add_alias(conn, place, "Banff Springs")
        places_repo.add_alias(conn, place, "Banff AB")
        places_repo.remove_alias(conn, place, "Banff AB")
        conn.commit()

        rows = {r["place_id"]: marshal(r)
                for r in _rows(conn, sql, {"web_id_floor": WEB_ID_FLOOR})}

    assert [a["alias"] for a in rows[place]["aliases"]] == ["Banff Springs"]


# ---------------------------------------------------------------------------
# Corrections must not offer a removed name
# ---------------------------------------------------------------------------

def test_find_and_replace_skips_removed_variants_and_aliases(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = faces_repo.create_person(
            conn, given_name="Ann", middle_name=None, surname="Smith",
            maiden_name=None, nickname=None,
            birth_year=None, death_year=None, notes=None,
        )
        live_v = faces_repo.add_name_variant(conn, person_id=pid, variant="Ann Canaca")
        dead_v = faces_repo.add_name_variant(conn, person_id=pid, variant="Annie Canaca")
        faces_repo.remove_name_variant(conn, dead_v)

        place = conn.execute(
            "insert into places (name) values ('Somewhere') returning id"
        ).fetchone()[0]
        places_repo.add_alias(conn, place, "Canaca west")
        places_repo.add_alias(conn, place, "Canaca east")
        places_repo.remove_alias(conn, place, "Canaca east")
        conn.commit()

        groups = {g.target.key: g for g in corrections_repo.search(conn, "Canaca", "Canada")}

    variant_ids = {r.key["id"] for r in groups["person_variant"].rows}
    assert variant_ids == {live_v}, "a removed name is not text anybody can see"
    alias_names = {r.key["alias"] for r in groups["place_alias"].rows}
    assert alias_names == {"Canaca west"}


def test_a_correction_cannot_rename_an_alias_onto_a_removed_one(phase6):
    """A soft-deleted row still holds `(place_id, alias)`, so it still
    collides. The collision check deliberately ignores `is_deleted`."""
    with dbmod.connection() as conn:
        conn.autocommit = False
        place = conn.execute(
            "insert into places (name) values ('Somewhere') returning id"
        ).fetchone()[0]
        places_repo.add_alias(conn, place, "Canaca west")
        places_repo.add_alias(conn, place, "Canada west")
        places_repo.remove_alias(conn, place, "Canada west")
        conn.commit()

        groups = corrections_repo.search(conn, "Canaca", "Canada")
        rows = [r for g in groups if g.target.key == "place_alias" for r in g.rows]
        result = corrections_repo.apply_correction(
            conn, needle="Canaca", replacement="Canada", match_case=True, rows=rows,
        )
        conn.commit()

    assert result.changed == 0
    assert any("already exists" in s for s in result.skipped)


# ---------------------------------------------------------------------------
# Fix-up 1 item 4 — the two checks, on the laptop
# ---------------------------------------------------------------------------

def test_re_adding_a_photo_to_an_album_or_place_flips_the_flag_back(phase6):
    from photoarchive.modes.corrections import albums as albums_repo

    with dbmod.connection() as conn:
        conn.autocommit = False
        photo = insert_photo(conn)
        insert_master(conn, photo)
        album = conn.execute(
            "insert into albums (name, source) values ('A', 'import') returning id"
        ).fetchone()[0]
        place = conn.execute(
            "insert into places (name) values ('P') returning id"
        ).fetchone()[0]
        conn.execute(
            "insert into album_photos (album_id, photo_id) values (%s, %s)",
            (album, photo),
        )
        conn.execute(
            "insert into photo_places (photo_id, place_id) values (%s, %s)",
            (photo, place),
        )
        conn.commit()

        albums_repo.set_photo_in_album(conn, album, photo, False)
        conn.commit()
        assert albums_repo.album_photos(conn, album) == []

        # Re-adding must flip the flag, not fail on the composite key.
        assert albums_repo.set_photo_in_album(conn, album, photo, True)
        conn.commit()
        assert [r.photo_id for r in albums_repo.album_photos(conn, album)] == [photo]
        assert conn.execute(
            "select count(*) from album_photos where album_id = %s", (album,)
        ).fetchone()[0] == 1, "one row throughout, never a second"

        # photo_places has no desktop editor yet; prove the shape directly.
        conn.execute(
            """
            update photo_places set is_deleted = true, deleted_at = now(), updated_at = now()
             where photo_id = %s and place_id = %s
            """,
            (photo, place),
        )
        conn.commit()
        conn.execute(
            """
            insert into photo_places (photo_id, place_id) values (%s, %s)
            on conflict (photo_id, place_id) do update set
              is_deleted = false, deleted_at = null, updated_at = now()
            """,
            (photo, place),
        )
        conn.commit()
        assert conn.execute(
            """
            select is_deleted from photo_places
             where photo_id = %s and place_id = %s
            """,
            (photo, place),
        ).fetchone()[0] is False


@pytest.mark.parametrize("table", ["album_photos", "photo_places"])
def test_the_soft_delete_update_refreshes_photo_search_on_the_laptop(phase6, table):
    """The web test proves the web side. The Phase 11 statement trigger on
    these tables has to cover UPDATE too, or the laptop's own index would
    go on claiming the photo is in an album it was removed from."""
    from photoarchive.modes.corrections import albums as albums_repo

    with dbmod.connection() as conn:
        conn.autocommit = False
        photo = insert_photo(conn)
        insert_master(conn, photo)
        if table == "album_photos":
            parent = conn.execute(
                """
                insert into albums (name, source)
                values ('Zanzibar Holiday', 'import') returning id
                """
            ).fetchone()[0]
            conn.execute(
                "insert into album_photos (album_id, photo_id) values (%s, %s)",
                (parent, photo),
            )
        else:
            parent = conn.execute(
                "insert into places (name) values ('Zanzibar') returning id"
            ).fetchone()[0]
            conn.execute(
                "insert into photo_places (photo_id, place_id) values (%s, %s)",
                (photo, parent),
            )
        conn.commit()

        def matches() -> bool:
            row = conn.execute(
                """
                select 1 from photo_search
                 where photo_id = %s and tsv @@ plainto_tsquery('english', 'zanzibar')
                """,
                (photo,),
            ).fetchone()
            return row is not None

        assert matches(), "the insert trigger should have indexed it"

        if table == "album_photos":
            albums_repo.set_photo_in_album(conn, parent, photo, False)
        else:
            conn.execute(
                """
                update photo_places
                   set is_deleted = true, deleted_at = now(), updated_at = now()
                 where photo_id = %s and place_id = %s
                """,
                (photo, parent),
            )
        conn.commit()

        assert not matches(), \
            f"the {table} soft-delete must refresh photo_search on the laptop"
