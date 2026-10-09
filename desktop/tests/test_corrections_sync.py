"""Phase 15 — a correction has to reach every copy on its own.

Two halves, and the asymmetry between them is the point.

**Upward** (laptop to VM) was already solved by Phase 9 fix-up 2: the
metadata selectors are a full re-send, so an ordinary `update` arrives on
the next push. What Phase 15 adds is the columns that let the VM know a
*human* made the edit, plus a `place_aliases` stage — alias corrections
used to stop at the laptop entirely.

**Downward** is new. The web can now correct text on rows the laptop
owns, so `pull_web_edits` has to bring that text down; otherwise the next
push hands the old wording straight back and the correction appears to
un-happen.

The load-bearing detail, and the reason these tests exist at all:
**the comparison is `edited_on_web_at` vs `edited_on_desktop_at`, never
`updated_at`.** Every `/sync/*` upsert sets `updated_at = now()`, so on
the web that column means "when a push last touched this row". A
last-writer-wins reading it would conclude the web was newer for every
row after every push, edited or not — the same trap that forced
`tombstoned_at` to be its own column in Phase 7.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from photoarchive import db as dbmod
from photoarchive.id_ranges import WEB_ID_FLOOR
from photoarchive.modes.sync import pull as pullmod
from photoarchive.modes.sync.push import _META_STAGES

from .phase6_fixtures import insert_master, insert_photo, phase6  # noqa: F401


NOW = datetime.now(timezone.utc)
EARLIER = NOW - timedelta(hours=2)
LATER = NOW + timedelta(hours=2)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# `photoorg_test` is migrated as a WEB database (web/tools/test-setup.js
# passes PHOTOORG_DB_ROLE=web), so every id-ranged sequence starts at
# WEB_ID_FLOOR. Rows left to those sequences would be web-origin, which
# both the push selectors (`id < floor`) and `_apply_web_edits`
# (`is_web_origin`) correctly refuse. Everything below therefore assigns
# explicit desktop-range ids - that is what a laptop-born row looks like,
# and it is the only thing these tests are about.
_next_id = iter(range(1, 10_000))


def _desktop_id() -> int:
    return next(_next_id)


def _album(conn, name: str, *, desktop_edit: datetime | None = None) -> int:
    album_id = _desktop_id()
    conn.execute(
        """
        insert into albums (id, name, source, edited_on_desktop_at)
        values (%s, %s, 'import', %s)
        """,
        (album_id, name, desktop_edit),
    )
    return album_id


# ---------------------------------------------------------------------------
# Downward: the web's edits land on the laptop
# ---------------------------------------------------------------------------

def test_a_web_edit_lands_when_the_desktop_has_no_human_edit(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        album_id = _album(conn, "Summer 1992 - Canaca")
        conn.commit()

    counts = {t: 0 for t in pullmod._WEB_EDIT_TABLES}
    applied = pullmod._apply_web_edits({
        "albums": [{
            "id": album_id, "name": "Summer 1992 - Canada",
            "description": None, "edited_on_web_at": _iso(NOW),
        }],
    }, counts)

    assert applied == 1 and counts["albums"] == 1
    with dbmod.connection() as conn:
        conn.autocommit = True
        name, web_stamp = conn.execute(
            "select name, edited_on_web_at from albums where id = %s", (album_id,)
        ).fetchone()
    assert name == "Summer 1992 - Canada"
    # The laptop records *when the web edited it*, so the push that follows
    # sends an older edited_on_desktop_at and the web's guard keeps its own
    # text. The two tiers agree with nothing else to remember.
    assert web_stamp is not None


def test_a_newer_desktop_edit_beats_an_older_web_edit(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        album_id = _album(conn, "Desktop wording", desktop_edit=LATER)
        conn.commit()

    counts = {t: 0 for t in pullmod._WEB_EDIT_TABLES}
    applied = pullmod._apply_web_edits({
        "albums": [{
            "id": album_id, "name": "Web wording", "description": None,
            "edited_on_web_at": _iso(EARLIER),
        }],
    }, counts)

    assert applied == 0
    with dbmod.connection() as conn:
        conn.autocommit = True
        assert conn.execute(
            "select name from albums where id = %s", (album_id,)
        ).fetchone()[0] == "Desktop wording"


def test_a_tie_goes_to_the_web(phase6):
    """Answer 2: the web wins ties — it is the copy a relative is looking
    at, and a tie means the two edits are indistinguishable in time."""
    same = NOW
    with dbmod.connection() as conn:
        conn.autocommit = False
        album_id = _album(conn, "Desktop wording", desktop_edit=same)
        conn.commit()

    counts = {t: 0 for t in pullmod._WEB_EDIT_TABLES}
    pullmod._apply_web_edits({
        "albums": [{
            "id": album_id, "name": "Web wording", "description": None,
            "edited_on_web_at": _iso(same),
        }],
    }, counts)

    with dbmod.connection() as conn:
        conn.autocommit = True
        assert conn.execute(
            "select name from albums where id = %s", (album_id,)
        ).fetchone()[0] == "Web wording"


def test_web_origin_rows_are_left_to_pull_web_origin(phase6):
    """A row born on the web is web-authoritative outright, not by
    timestamp; it travels as a whole row through /pull/web_origin."""
    counts = {t: 0 for t in pullmod._WEB_EDIT_TABLES}
    applied = pullmod._apply_web_edits({
        "albums": [{
            "id": WEB_ID_FLOOR + 7, "name": "Born on the web",
            "description": None, "edited_on_web_at": _iso(NOW),
        }],
    }, counts)
    assert applied == 0


def test_a_web_edit_to_a_suggestion_payload_lands_as_jsonb(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        photo_id = insert_photo(conn)
        insert_master(conn, photo_id)
        sug_id = _desktop_id()
        conn.execute(
            """
            insert into suggestions (id, photo_id, kind, payload, status, source)
            values (%s, %s, 'description', %s::jsonb, 'pending', 'ai')
            """,
            (sug_id, photo_id, json.dumps({"text": "A lake in Canaca.", "tags": ["lake"]})),
        )
        conn.commit()

    counts = {t: 0 for t in pullmod._WEB_EDIT_TABLES}
    pullmod._apply_web_edits({
        "suggestions": [{
            "id": sug_id,
            "payload": {"text": "A lake in Canada.", "tags": ["lake"]},
            "edited_on_web_at": _iso(NOW),
        }],
    }, counts)

    with dbmod.connection() as conn:
        conn.autocommit = True
        payload = conn.execute(
            "select payload from suggestions where id = %s", (sug_id,)
        ).fetchone()[0]
    assert payload["text"] == "A lake in Canada."
    assert payload["tags"] == ["lake"]


def test_every_pulled_edit_is_audited(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        album_id = _album(conn, "Canaca")
        conn.commit()

    counts = {t: 0 for t in pullmod._WEB_EDIT_TABLES}
    pullmod._apply_web_edits({
        "albums": [{"id": album_id, "name": "Canada", "description": None,
                    "edited_on_web_at": _iso(NOW)}],
    }, counts)

    with dbmod.connection() as conn:
        conn.autocommit = True
        actor, nv = conn.execute(
            """
            select actor, new_value from audit_log
             where action = 'correction.pull' and entity_id = %s
            """,
            (album_id,),
        ).fetchone()
    assert actor == "web"
    assert nv["name"] == "Canada"


# ---------------------------------------------------------------------------
# Paging: the cursor has to be able to get past a tie
# ---------------------------------------------------------------------------

def test_pull_web_edits_pages_until_drained(phase6):
    """A web bulk replace stamps every row it changed inside one
    transaction, so hundreds of rows share one `edited_on_web_at` to the
    microsecond. A timestamp-only cursor would hand back the same first
    page forever, which is why the wire cursor is `(edited_on_web_at, id)`
    and why the desktop keeps asking while the server says `has_more`.
    """
    with dbmod.connection() as conn:
        conn.autocommit = False
        ids = [_album(conn, f"Album {i} Canaca") for i in range(5)]
        conn.commit()

    stamp = _iso(NOW)
    pages = [
        {
            "albums": [{"id": i, "name": f"Album {n} Canada", "description": None,
                        "edited_on_web_at": stamp}],
            "cursors": {"albums": f"{stamp}|{i}"},
            "has_more": {"albums": True},
        }
        for n, i in enumerate(ids)
    ]
    # The last page reports no more.
    pages[-1]["has_more"] = {"albums": False}

    class _Client:
        def __init__(self):
            self.cursors_seen = []
            self._i = 0

        def pull_web_edits(self, cursors):
            self.cursors_seen.append(cursors)
            page = pages[self._i]
            self._i += 1
            return page

    client = _Client()
    counts = pullmod.pull_web_edits(client, phase6.THUMBS_DIR)

    assert counts["albums"] == 5
    assert len(client.cursors_seen) == 5
    # First request carries no cursor; every later one carries the previous
    # page's composite, which is what gets past the shared timestamp.
    assert client.cursors_seen[0] is None
    assert client.cursors_seen[1] == {"albums": f"{stamp}|{ids[0]}"}

    with dbmod.connection() as conn:
        conn.autocommit = True
        names = [
            r[0] for r in conn.execute(
                "select name from albums order by id"
            ).fetchall()
        ]
    assert all("Canada" in n for n in names)


def test_pull_web_edits_stops_when_a_page_is_empty(phase6):
    """A server that kept saying `has_more` with nothing in it must not
    spin the loop."""
    class _Client:
        def __init__(self):
            self.calls = 0

        def pull_web_edits(self, cursors):
            self.calls += 1
            return {"albums": [], "cursors": {}, "has_more": {"albums": True}}

    client = _Client()
    pullmod.pull_web_edits(client, phase6.THUMBS_DIR)
    assert client.calls == 1


# ---------------------------------------------------------------------------
# Upward: the push selectors
# ---------------------------------------------------------------------------

def _stage(name):
    for stage, sql, marshal in _META_STAGES:
        if stage == name:
            return sql, marshal
    raise AssertionError(f"no push stage named {name}")


def test_place_aliases_push_sends_each_places_whole_alias_set(phase6):
    """Alias corrections used to stop at the laptop — there was no route
    at all (PROJECT-PLAN section 5 item 11). The text is the primary key,
    so there is no soft-delete to carry a removal; the stage therefore
    sends the complete set per place and the route replaces it."""
    sql, marshal = _stage("place_aliases")
    with dbmod.connection() as conn:
        conn.autocommit = False
        place_id = _desktop_id()
        conn.execute("insert into places (id, name) values (%s, 'Banff')", (place_id,))
        for alias in ("Banff Springs", "Banff AB"):
            conn.execute(
                "insert into place_aliases (place_id, alias, kind) values (%s, %s, 'alias')",
                (place_id, alias),
            )
        bare_id = _desktop_id()
        conn.execute("insert into places (id, name) values (%s, 'Nowhere')", (bare_id,))
        conn.commit()

        from psycopg.rows import dict_row
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, {"web_id_floor": WEB_ID_FLOOR})
            rows = {r["place_id"]: marshal(r) for r in cur.fetchall()}

    assert sorted(a["alias"] for a in rows[place_id]["aliases"]) \
        == ["Banff AB", "Banff Springs"]
    # A place whose last alias was removed must still be sent, with an
    # empty set — that is the one state a soft-delete column would have
    # represented, and leaving it out would strand the removal on the
    # laptop forever.
    assert rows[bare_id]["aliases"] == []


@pytest.mark.parametrize("stage,column", [
    ("people", "edited_on_desktop_at"),
    ("person_name_variants", "edited_on_desktop_at"),
    ("places", "edited_on_desktop_at"),
    ("albums", "edited_on_desktop_at"),
])
def test_the_lww_stamp_is_on_the_wire(phase6, stage, column):
    """The web's guard compares against this. A stage that forgot to send
    it would lose every race with the web, silently."""
    sql, marshal = _stage(stage)
    with dbmod.connection() as conn:
        conn.autocommit = False
        if stage == "albums":
            _album(conn, "Canaca", desktop_edit=NOW)
        elif stage == "places":
            conn.execute(
                "insert into places (id, name, edited_on_desktop_at) values (%s, 'P', %s)",
                (_desktop_id(), NOW))
        else:
            pid = _desktop_id()
            conn.execute(
                """
                insert into people (id, given_name, surname, edited_on_desktop_at)
                values (%s, 'A', 'B', %s)
                """,
                (pid, NOW),
            )
            if stage == "person_name_variants":
                conn.execute(
                    """
                    insert into person_name_variants
                      (id, person_id, variant, kind, edited_on_desktop_at)
                    values (%s, %s, 'Ab', 'nickname', %s)
                    """,
                    (_desktop_id(), pid, NOW),
                )
        conn.commit()

        from psycopg.rows import dict_row
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, {"web_id_floor": WEB_ID_FLOOR})
            items = [marshal(r) for r in cur.fetchall()]

    assert items, f"{stage} selector returned nothing to check"
    assert any(i.get(column) for i in items), \
        f"{stage} must put {column} on the wire"


@pytest.mark.parametrize("stage", ["album_photos", "photo_places"])
def test_a_soft_removed_join_row_is_on_the_wire(phase6, stage):
    """Both tables were insert-or-update-only and the push only upserts,
    so "take this photo out of the album" never reached the web and the
    site kept showing it there."""
    sql, marshal = _stage(stage)
    with dbmod.connection() as conn:
        conn.autocommit = False
        photo_id = insert_photo(conn)
        insert_master(conn, photo_id)
        if stage == "album_photos":
            parent = _album(conn, "An album")
            conn.execute(
                """
                insert into album_photos (album_id, photo_id, is_deleted, deleted_at)
                values (%s, %s, true, now())
                """,
                (parent, photo_id),
            )
        else:
            parent = _desktop_id()
            conn.execute(
                "insert into places (id, name) values (%s, 'Somewhere')", (parent,))
            conn.execute(
                """
                insert into photo_places (photo_id, place_id, is_deleted, deleted_at)
                values (%s, %s, true, now())
                """,
                (photo_id, parent),
            )
        conn.commit()

        from psycopg.rows import dict_row
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, {"web_id_floor": WEB_ID_FLOOR})
            items = [marshal(r) for r in cur.fetchall()]

    assert len(items) == 1
    assert items[0]["is_deleted"] is True
    assert items[0]["deleted_at"]
    # `updated_at` is what the route's LWW compares, so it has to travel.
    assert items[0]["updated_at"]
