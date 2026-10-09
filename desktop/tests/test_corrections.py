"""Phase 15 — the Corrections tool.

George's folder was named "Canaca" instead of "Canada". That typo reached
an album name and 139 pending description suggestions, and putting it
right took an evening of hand-written SQL that landed in the wrong
database. The rule these tests hold: every piece of human-visible text is
correctable from the app that owns it, with an audit row, and the
correction reaches every copy on its own.

So the cases here are the ones that bit:
  * all three row shapes (a plain column, a jsonb path, a composite key)
    are found, previewed, written and audited;
  * the master-derived columns are *found and counted* but refused —
    they mirror a read-only disk and keep the original spelling on
    purpose;
  * undo works from the audit log, not from session state;
  * undo skips a row somebody edited after the correction instead of
    clobbering it.
"""

from __future__ import annotations

import json

import pytest

from photoarchive import db as dbmod
from photoarchive.modes.corrections import albums as albums_repo
from photoarchive.modes.corrections import places as places_repo
from photoarchive.modes.corrections import repo
from photoarchive.modes.corrections.targets import BY_KEY

from .phase6_fixtures import insert_master, insert_photo, phase6  # noqa: F401


TYPO = "Canaca"
FIXED = "Canada"


def _seed(conn) -> dict:
    """One row of every shape, each carrying the typo."""
    ids: dict = {}

    album_id = conn.execute(
        "insert into albums (name, description, source) values (%s, %s, 'import') returning id",
        (f"Summer 1992 - {TYPO}", f"scanned in {TYPO}"),
    ).fetchone()[0]
    ids["album"] = album_id

    # A photo whose source_folder and scan_batch mirror the master disk —
    # the read-only group.
    photo_id = insert_photo(conn, source_folder=f"Summer 1992 - {TYPO}")
    insert_master(conn, photo_id)
    conn.execute(
        "update photos set scan_batch = %s, physical_ref_note = %s where id = %s",
        (f"Batch {TYPO}", f"box 3, {TYPO} trip", photo_id),
    )
    conn.execute(
        "update photo_masters set master_path = %s where photo_id = %s",
        (f"D:\\Scanned Photos\\Summer 1992 - {TYPO}\\001.jpg", photo_id),
    )
    ids["photo"] = photo_id

    conn.execute(
        "insert into album_photos (album_id, photo_id, position) values (%s, %s, 1)",
        (album_id, photo_id),
    )

    sug_id = conn.execute(
        """
        insert into suggestions (photo_id, kind, payload, status, source)
        values (%s, 'description', %s::jsonb, 'pending', 'ai')
        returning id
        """,
        (photo_id, json.dumps({
            "text": f"A lake in {TYPO}.",
            "tags": ["lake", "trees"],
            "prompt_version": "describe.v1",
        })),
    ).fetchone()[0]
    ids["suggestion"] = sug_id

    # A resolved suggestion with the same typo: must never be offered.
    ids["resolved_suggestion"] = conn.execute(
        """
        insert into suggestions (photo_id, kind, payload, status, source, resolved_at)
        values (%s, 'description', %s::jsonb, 'accepted', 'ai', now())
        returning id
        """,
        (photo_id, json.dumps({"text": f"Another shot of {TYPO}."})),
    ).fetchone()[0]

    person_id = conn.execute(
        "insert into people (given_name, surname) values (%s, %s) returning id",
        ("Ann", TYPO),
    ).fetchone()[0]
    ids["person"] = person_id
    ids["variant"] = conn.execute(
        """
        insert into person_name_variants (person_id, variant, kind)
        values (%s, %s, 'misspelling') returning id
        """,
        (person_id, f"Ann {TYPO}"),
    ).fetchone()[0]

    place_id = conn.execute(
        "insert into places (name, notes) values (%s, %s) returning id",
        (TYPO, f"the {TYPO} house"),
    ).fetchone()[0]
    ids["place"] = place_id
    conn.execute(
        "insert into place_aliases (place_id, alias, kind) values (%s, %s, 'alias')",
        (place_id, f"{TYPO} (west)"),
    )
    conn.execute(
        "insert into photo_places (photo_id, place_id) values (%s, %s)",
        (photo_id, place_id),
    )

    back_id = conn.execute(
        """
        insert into photo_backs (photo_id, master_path, sha256, transcribed_text)
        values (%s, 'D:\\x\\back.jpg', 'sha-back-1', %s)
        returning id
        """,
        (photo_id, f"written on back: {TYPO} 1992"),
    ).fetchone()[0]
    ids["back"] = back_id

    conn.commit()
    return ids


# ---------------------------------------------------------------------------
# Search and preview
# ---------------------------------------------------------------------------

def test_search_finds_every_shape_and_separates_the_master_mirrors(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        _seed(conn)
        groups = repo.search(conn, TYPO, FIXED)

    by_key = {g.target.key: g for g in groups}

    # One of each shape: a plain column, a jsonb path, a composite key.
    assert by_key["album_name"].count == 1
    assert by_key["suggestion_description"].count == 1
    assert by_key["place_alias"].count == 1
    # And the rest of the editable surface.
    for key in ("album_description", "person_surname", "person_variant",
                "place_name", "place_notes", "back_transcription",
                "physical_ref_note"):
        assert key in by_key, f"{key} should have matched"
        assert by_key[key].target.editable

    # The master mirrors are found and counted, with no edit path.
    for key in ("master_source_folder", "master_scan_batch", "master_path"):
        assert by_key[key].count == 1
        assert by_key[key].target.editable is False

    # The preview is the literal string that will be stored.
    row = by_key["suggestion_description"].rows[0]
    assert row.value == f"A lake in {TYPO}."
    assert row.new_value == f"A lake in {FIXED}."


def test_search_leaves_resolved_suggestions_alone(phase6):
    """A resolved suggestion's text is what a decision was made on."""
    with dbmod.connection() as conn:
        conn.autocommit = False
        ids = _seed(conn)
        groups = repo.search(conn, TYPO, FIXED)

    found = {
        r.key["id"]
        for g in groups if g.target.key == "suggestion_description"
        for r in g.rows
    }
    assert ids["suggestion"] in found
    assert ids["resolved_suggestion"] not in found


def test_match_case_off_still_replaces_the_matched_span(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        conn.execute("insert into albums (name, source) values ('summer CANACA', 'import')")
        conn.commit()
        groups = repo.search(conn, TYPO, FIXED, match_case=False)
    rows = [r for g in groups if g.target.key == "album_name" for r in g.rows]
    assert any(r.new_value == f"summer {FIXED}" for r in rows)


def test_replace_text_is_literal_not_a_regex_template():
    # A replacement containing a backslash or a group reference must land
    # verbatim; `re.sub` would otherwise interpret it.
    assert repo.replace_text("a-b", "-", r"\1", True) == r"a\1b"
    assert repo.replace_text("a-b", "-", r"\1", False) == r"a\1b"
    assert repo.replace_text("a.b.c", ".", "_", True) == "a_b_c"
    # Case-insensitive search must not turn the needle into a pattern.
    assert repo.replace_text("aXb", "x", "+", False) == "a+b"


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def test_apply_writes_every_shape_and_audits_previous_and_new(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        ids = _seed(conn)
        groups = repo.search(conn, TYPO, FIXED)
        rows = [r for g in groups for r in g.rows if g.target.editable]
        result = repo.apply_correction(
            conn, needle=TYPO, replacement=FIXED, match_case=True, rows=rows,
        )
        conn.commit()

        assert result.changed == len(rows)
        assert result.skipped == []

        # Column shape.
        assert conn.execute(
            "select name from albums where id = %s", (ids["album"],)
        ).fetchone()[0] == f"Summer 1992 - {FIXED}"
        # jsonb shape — and the rest of the payload survives.
        payload = conn.execute(
            "select payload from suggestions where id = %s", (ids["suggestion"],)
        ).fetchone()[0]
        assert payload["text"] == f"A lake in {FIXED}."
        assert payload["tags"] == ["lake", "trees"]
        assert payload["prompt_version"] == "describe.v1"
        # Composite-key shape.
        assert conn.execute(
            "select alias from place_aliases where place_id = %s", (ids["place"],)
        ).fetchone()[0] == f"{FIXED} (west)"
        # And the two desktop-authoritative text columns.
        assert conn.execute(
            "select transcribed_text from photo_backs where id = %s", (ids["back"],)
        ).fetchone()[0] == f"written on back: {FIXED} 1992"
        assert FIXED in conn.execute(
            "select physical_ref_note from photos where id = %s", (ids["photo"],)
        ).fetchone()[0]

        # One audit row per changed row, carrying both values.
        audits = conn.execute(
            """
            select previous_value, new_value from audit_log
             where action = 'correction.replace'
               and new_value->>'batch_id' = %s
            """,
            (result.batch_id,),
        ).fetchall()
        assert len(audits) == result.changed
        for prev, new in audits:
            assert TYPO in prev["value"]
            assert FIXED in new["value"]
            assert new["search"] == TYPO and new["replace"] == FIXED

        # Plus one batch summary row.
        summary = conn.execute(
            """
            select new_value from audit_log
             where action = 'correction.batch' and new_value->>'batch_id' = %s
            """,
            (result.batch_id,),
        ).fetchone()[0]
        assert summary["changed"] == result.changed


def test_apply_never_writes_a_master_mirror_even_if_asked(phase6):
    """The UI gives these no checkbox. If a caller ticks one anyway it is
    a bug, and silently rewriting the mirror of a read-only disk would be
    the worst possible outcome of it."""
    with dbmod.connection() as conn:
        conn.autocommit = False
        ids = _seed(conn)
        groups = repo.search(conn, TYPO, FIXED)
        mirrors = [r for g in groups for r in g.rows if not g.target.editable]
        assert mirrors, "expected the master-derived rows to match"

        result = repo.apply_correction(
            conn, needle=TYPO, replacement=FIXED, match_case=True, rows=mirrors,
        )
        conn.commit()

        assert result.changed == 0
        assert len(result.skipped) == len(mirrors)
        assert all("not editable" in s for s in result.skipped)
        # Still spelled the way the disk spells it.
        folder, batch = conn.execute(
            "select source_folder, scan_batch from photos where id = %s", (ids["photo"],)
        ).fetchone()
        assert TYPO in folder and TYPO in batch
        assert TYPO in conn.execute(
            "select master_path from photo_masters where photo_id = %s", (ids["photo"],)
        ).fetchone()[0]


def test_apply_stamps_edited_on_desktop_at_on_the_five_lww_tables(phase6):
    """`edited_on_desktop_at` is what the web's guard compares against.
    Without it a desktop correction would lose every race with the web."""
    with dbmod.connection() as conn:
        conn.autocommit = False
        ids = _seed(conn)
        groups = repo.search(conn, TYPO, FIXED)
        rows = [r for g in groups for r in g.rows if g.target.editable]
        repo.apply_correction(
            conn, needle=TYPO, replacement=FIXED, match_case=True, rows=rows,
        )
        conn.commit()

        for table, rid in (
            ("albums", ids["album"]),
            ("suggestions", ids["suggestion"]),
            ("people", ids["person"]),
            ("person_name_variants", ids["variant"]),
            ("places", ids["place"]),
        ):
            stamp = conn.execute(
                f"select edited_on_desktop_at from {table} where id = %s", (rid,)
            ).fetchone()[0]
            assert stamp is not None, f"{table} must record that a human edited it here"


def test_apply_skips_a_row_that_changed_since_the_search(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        ids = _seed(conn)
        groups = repo.search(conn, TYPO, FIXED)
        rows = [r for g in groups if g.target.key == "album_name" for r in g.rows]

        # Somebody else renames it between the search and the apply.
        conn.execute(
            "update albums set name = 'Something else entirely' where id = %s",
            (ids["album"],),
        )
        result = repo.apply_correction(
            conn, needle=TYPO, replacement=FIXED, match_case=True, rows=rows,
        )
        conn.commit()

        assert result.changed == 0
        assert any("changed since the search" in s for s in result.skipped)
        assert conn.execute(
            "select name from albums where id = %s", (ids["album"],)
        ).fetchone()[0] == "Something else entirely"


def test_apply_refuses_an_alias_rename_that_would_collide(phase6):
    """The alias text is part of the primary key. Letting the constraint
    raise would abort the whole correction over one duplicate, and an
    upsert that dropped the row would lose the alias outright."""
    with dbmod.connection() as conn:
        conn.autocommit = False
        ids = _seed(conn)
        # The place already holds the name the correction would produce.
        conn.execute(
            "insert into place_aliases (place_id, alias, kind) values (%s, %s, 'alias')",
            (ids["place"], f"{FIXED} (west)"),
        )
        groups = repo.search(conn, TYPO, FIXED)
        rows = [r for g in groups if g.target.key == "place_alias" for r in g.rows]
        result = repo.apply_correction(
            conn, needle=TYPO, replacement=FIXED, match_case=True, rows=rows,
        )
        conn.commit()

        assert result.changed == 0
        assert any("already exists" in s for s in result.skipped)
        # Both aliases still there; nothing was lost.
        aliases = {
            r[0] for r in conn.execute(
                "select alias from place_aliases where place_id = %s", (ids["place"],)
            ).fetchall()
        }
        assert aliases == {f"{TYPO} (west)", f"{FIXED} (west)"}


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------

def test_undo_restores_the_batch_from_the_audit_log(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        ids = _seed(conn)
        groups = repo.search(conn, TYPO, FIXED)
        rows = [r for g in groups for r in g.rows if g.target.editable]
        applied = repo.apply_correction(
            conn, needle=TYPO, replacement=FIXED, match_case=True, rows=rows,
        )
        conn.commit()

        # Nothing is held in memory between these two calls other than the
        # batch id — the same position a fresh process is in.
        undone = repo.undo_batch(conn, applied.batch_id)
        conn.commit()

        assert undone.restored == applied.changed
        assert undone.skipped_ids == []
        assert conn.execute(
            "select name from albums where id = %s", (ids["album"],)
        ).fetchone()[0] == f"Summer 1992 - {TYPO}"
        assert conn.execute(
            "select payload->>'text' from suggestions where id = %s", (ids["suggestion"],)
        ).fetchone()[0] == f"A lake in {TYPO}."
        assert conn.execute(
            "select alias from place_aliases where place_id = %s", (ids["place"],)
        ).fetchone()[0] == f"{TYPO} (west)"

        # The undo is itself audited, and the batch now reads as undone.
        assert conn.execute(
            """
            select count(*) from audit_log
             where action = 'correction.undo' and new_value->>'batch_id' = %s
            """,
            (applied.batch_id,),
        ).fetchone()[0] == undone.restored
        batches = {b.batch_id: b for b in repo.list_batches(conn)}
        assert batches[applied.batch_id].undone is True


def test_undo_restores_an_alias_whose_text_is_its_own_key(phase6):
    """`place_aliases` keys on (place_id, alias), so correcting the text
    renames the row. The audit entry records the key as it was *before*
    the write, and an undo that looked the row up under that stale key
    found nothing and silently skipped a row it should have restored.
    This is the only shape where the key moves, so it needs its own case.
    """
    with dbmod.connection() as conn:
        conn.autocommit = False
        ids = _seed(conn)
        groups = repo.search(conn, TYPO, FIXED)
        rows = [r for g in groups if g.target.key == "place_alias" for r in g.rows]
        applied = repo.apply_correction(
            conn, needle=TYPO, replacement=FIXED, match_case=True, rows=rows,
        )
        conn.commit()
        assert applied.changed == 1
        assert conn.execute(
            "select alias from place_aliases where place_id = %s", (ids["place"],)
        ).fetchone()[0] == f"{FIXED} (west)"

        undone = repo.undo_batch(conn, applied.batch_id)
        conn.commit()

        assert undone.skipped_ids == []
        assert undone.restored == 1
        assert conn.execute(
            "select alias from place_aliases where place_id = %s", (ids["place"],)
        ).fetchone()[0] == f"{TYPO} (west)"


def test_undo_skips_and_lists_a_row_edited_since_the_correction(phase6):
    """Answer 4. An undo that overwrote newer work would be a second
    mistake with no third chance."""
    with dbmod.connection() as conn:
        conn.autocommit = False
        ids = _seed(conn)
        groups = repo.search(conn, TYPO, FIXED)
        rows = [r for g in groups if g.target.key in ("album_name", "place_name")
                for r in g.rows]
        applied = repo.apply_correction(
            conn, needle=TYPO, replacement=FIXED, match_case=True, rows=rows,
        )
        conn.commit()

        # George then renames the album again by hand.
        conn.execute(
            "update albums set name = 'Summer 1992 - Canada (Banff)' where id = %s",
            (ids["album"],),
        )
        conn.commit()

        undone = repo.undo_batch(conn, applied.batch_id)
        conn.commit()

        assert undone.restored == 1           # the place came back
        assert len(undone.skipped_ids) == 1   # the album did not
        assert "Album names" in undone.skipped_ids[0]
        assert conn.execute(
            "select name from albums where id = %s", (ids["album"],)
        ).fetchone()[0] == "Summer 1992 - Canada (Banff)"
        assert conn.execute(
            "select name from places where id = %s", (ids["place"],)
        ).fetchone()[0] == TYPO


def test_undo_of_an_unknown_batch_does_nothing(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        _seed(conn)
        out = repo.undo_batch(conn, "deadbeef" * 4)
        conn.commit()
    assert out.restored == 0 and out.skipped_ids == []


# ---------------------------------------------------------------------------
# Albums editor
# ---------------------------------------------------------------------------

def test_album_rename_audits_and_stamps(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        ids = _seed(conn)
        assert albums_repo.update_album(conn, ids["album"], name="Summer 1992 - Canada")
        # A second save with the same value is a no-op, not a second audit row.
        assert albums_repo.update_album(conn, ids["album"], name="Summer 1992 - Canada") is False
        conn.commit()

        name, stamp = conn.execute(
            "select name, edited_on_desktop_at from albums where id = %s", (ids["album"],)
        ).fetchone()
        assert name == "Summer 1992 - Canada"
        assert stamp is not None
        prev, new = conn.execute(
            """
            select previous_value, new_value from audit_log
             where action = 'album.update' and entity_id = %s
            """,
            (ids["album"],),
        ).fetchone()
        assert prev["name"] == f"Summer 1992 - {TYPO}"
        assert new["name"] == "Summer 1992 - Canada"


def test_removing_a_photo_from_an_album_is_a_soft_delete(phase6):
    """`album_photos` was insert-or-update-only and the push only upserts,
    so a removal never reached the web and the site kept showing the photo
    in the album. It has to be a flag the push can carry."""
    with dbmod.connection() as conn:
        conn.autocommit = False
        ids = _seed(conn)
        assert albums_repo.set_photo_in_album(
            conn, ids["album"], ids["photo"], False)
        conn.commit()

        row = conn.execute(
            """
            select is_deleted, deleted_at, updated_at from album_photos
             where album_id = %s and photo_id = %s
            """,
            (ids["album"], ids["photo"]),
        ).fetchone()
        assert row is not None, "the row must survive — no real deletes, ever"
        assert row[0] is True and row[1] is not None and row[2] is not None
        assert albums_repo.album_photos(conn, ids["album"]) == []

        # And it comes back.
        assert albums_repo.set_photo_in_album(conn, ids["album"], ids["photo"], True)
        conn.commit()
        assert [r.photo_id for r in albums_repo.album_photos(conn, ids["album"])] \
            == [ids["photo"]]


def test_album_reorder_only_writes_what_moves(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        ids = _seed(conn)
        second = insert_photo(conn)
        insert_master(conn, second)
        conn.execute(
            "insert into album_photos (album_id, photo_id, position) values (%s, %s, 2)",
            (ids["album"], second),
        )
        conn.commit()

        assert albums_repo.reorder_album(
            conn, ids["album"], [ids["photo"], second]) == 0
        assert albums_repo.reorder_album(
            conn, ids["album"], [second, ids["photo"]]) == 2
        conn.commit()
        assert [r.photo_id for r in albums_repo.album_photos(conn, ids["album"])] \
            == [second, ids["photo"]]


def test_album_soft_delete_and_restore(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        ids = _seed(conn)
        assert albums_repo.set_album_deleted(conn, ids["album"], True)
        conn.commit()
        assert [a.id for a in albums_repo.list_albums(conn)] == []
        assert ids["album"] in [
            a.id for a in albums_repo.list_albums(conn, include_deleted=True)
        ]
        assert albums_repo.set_album_deleted(conn, ids["album"], False)
        conn.commit()
        assert [a.id for a in albums_repo.list_albums(conn)] == [ids["album"]]


# ---------------------------------------------------------------------------
# Places editor
# ---------------------------------------------------------------------------

def test_place_rename_collision_is_reported_not_raised(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        ids = _seed(conn)
        other = conn.execute(
            "insert into places (name) values ('Canada') returning id"
        ).fetchone()[0]
        conn.commit()
        assert places_repo.name_holder(conn, "Canada", excluding=ids["place"]) == other
        assert places_repo.name_holder(conn, "Canada", excluding=other) is None


def test_place_update_and_aliases(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        ids = _seed(conn)
        assert places_repo.update_place(
            conn, ids["place"], name="Canada", notes="the Canada house",
            latitude=51.5, longitude=-114.1, set_coords=True,
        )
        conn.commit()
        name, lat, lon, stamp = conn.execute(
            """
            select name, latitude, longitude, edited_on_desktop_at
              from places where id = %s
            """,
            (ids["place"],),
        ).fetchone()
        assert (name, lat, lon) == ("Canada", 51.5, -114.1)
        assert stamp is not None

        assert places_repo.add_alias(conn, ids["place"], "Canuckia")
        assert places_repo.add_alias(conn, ids["place"], "canuckia") is False
        conn.commit()
        assert ("Canuckia", "alias") in places_repo.list_aliases(conn, ids["place"])

        assert places_repo.remove_alias(conn, ids["place"], "Canuckia")
        conn.commit()
        assert "Canuckia" not in dict(places_repo.list_aliases(conn, ids["place"]))
        assert conn.execute(
            """
            select count(*) from audit_log
             where action in ('place.alias.add', 'place.alias.remove')
               and entity_id = %s
            """,
            (ids["place"],),
        ).fetchone()[0] == 2


def test_clearing_coordinates_is_distinguishable_from_leaving_them(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        ids = _seed(conn)
        places_repo.update_place(
            conn, ids["place"], latitude=10.0, longitude=20.0, set_coords=True)
        conn.commit()
        # A save that does not mention the coordinates leaves them alone…
        places_repo.update_place(conn, ids["place"], notes="changed")
        conn.commit()
        assert conn.execute(
            "select latitude from places where id = %s", (ids["place"],)
        ).fetchone()[0] == 10.0
        # …and one that does, with None, clears them.
        places_repo.update_place(
            conn, ids["place"], latitude=None, longitude=None, set_coords=True)
        conn.commit()
        assert conn.execute(
            "select latitude, longitude from places where id = %s", (ids["place"],)
        ).fetchone() == (None, None)


# ---------------------------------------------------------------------------
# Targets table hygiene
# ---------------------------------------------------------------------------

def test_every_target_key_is_unique_and_sql_is_well_formed(phase6):
    """`BY_KEY` is what undo looks a target up in, so a duplicate key
    would silently point an old correction's undo at the wrong table."""
    from photoarchive.modes.corrections.targets import TARGETS

    assert len(BY_KEY) == len(TARGETS)
    with dbmod.connection() as conn:
        conn.autocommit = True
        for target in TARGETS:
            # Every find_sql must run, and must return the three columns
            # `repo.search` reads by name.
            with conn.cursor() as cur:
                cur.execute(target.find_sql.format(op="like"), {"pattern": "%zzz-nothing%"})
                names = [d.name for d in cur.description]
            assert names == ["key", "context", "value"], target.key
