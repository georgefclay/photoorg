"""Phase 12 — albums edited on the web, on the laptop's side.

The round trip through the real server is in ``test_web_id_floor.py``
(``test_albums_round_trip``). These pin the rules one at a time: the
Phase 15 human-edit comparison on ``album_photos``, a web-born album being
read-only here (and out of the Corrections sweep), and the push never
sending a web-born album's rows.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from photoarchive import db as dbmod
from photoarchive.id_ranges import WEB_ID_FLOOR
from photoarchive.modes.corrections import albums as albums_repo
from photoarchive.modes.corrections import repo as corr_repo
from photoarchive.modes.sync import pull as pullmod
from photoarchive.modes.sync.push import _META_STAGES

from .phase6_fixtures import insert_photo, phase6  # noqa: F401

NOW = datetime.now(timezone.utc)


def _album_with(conn, name: str, photo_ids: list[int], *, album_id: int | None = None) -> int:
    if album_id is None:
        album_id = conn.execute("insert into albums (name) values (%s) returning id", (name,)).fetchone()[0]
    else:
        conn.execute("insert into albums (id, name) values (%s, %s)", (album_id, name))
    for pos, pid in enumerate(photo_ids, start=1):
        conn.execute("insert into album_photos (album_id, photo_id, position) values (%s, %s, %s)",
                     (album_id, pid, pos))
    return album_id


def _row(conn, aid, pid):
    return conn.execute(
        "select position, is_deleted from album_photos where album_id = %s and photo_id = %s", (aid, pid),
    ).fetchone()


def test_a_web_membership_edit_lands_unless_the_laptop_edited_later(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = True
        p1, p2, p3 = (insert_photo(conn) for _ in range(3))
        aid = _album_with(conn, "Album 6", [p1, p2])
        assert aid < WEB_ID_FLOOR
        # The laptop moved p2 an hour from now (a later human edit).
        albums_repo.reorder_album(conn, aid, [p2, p1])
        conn.execute("update album_photos set edited_on_desktop_at = %s where album_id = %s and photo_id = %s",
                     (NOW + timedelta(hours=1), aid, p2))
        # p1 moved too (the re-order stamps every row it moves); make that an
        # hour ago, so the web's later edit of p1 is the newer one.
        conn.execute("update album_photos set edited_on_desktop_at = %s where album_id = %s and photo_id = %s",
                     (NOW - timedelta(hours=1), aid, p1))

    applied = pullmod._apply_web_edits({"album_photos": [
        {"album_id": aid, "photo_id": p1, "position": 2, "is_deleted": True,
         "deleted_at": NOW.isoformat(), "edited_on_web_at": NOW.isoformat()},
        {"album_id": aid, "photo_id": p2, "position": 9, "is_deleted": False,
         "deleted_at": None, "edited_on_web_at": NOW.isoformat()},
        # A photo the web added to this desktop-born album.
        {"album_id": aid, "photo_id": p3, "position": 3, "is_deleted": False,
         "deleted_at": None, "edited_on_web_at": NOW.isoformat()},
    ]}, {"album_photos": 0, **{t: 0 for t in pullmod._WEB_EDIT_TABLES}})
    assert applied == 2
    with dbmod.connection() as conn:
        assert _row(conn, aid, p1) == (2, True), "the web's removal lands"
        assert _row(conn, aid, p2) == (1, False), "the laptop's later re-order stands"
        assert _row(conn, aid, p3) == (3, False), "the web's addition lands"
        audits = conn.execute(
            "select count(*) from audit_log where action = 'correction.pull' and entity_type = 'album_photos'"
        ).fetchone()[0]
        assert audits == 2


def test_an_older_server_without_albums_is_deleted_does_not_break_the_pull(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = True
        aid = _album_with(conn, "Old name", [])
    pullmod._apply_web_edits({"albums": [
        {"id": aid, "name": "New name", "description": None, "edited_on_web_at": NOW.isoformat()},
    ]}, {t: 0 for t in pullmod._WEB_EDIT_TABLES})
    with dbmod.connection() as conn:
        assert conn.execute("select name, is_deleted from albums where id = %s", (aid,)).fetchone() == ("New name", False)


def test_a_web_born_album_is_read_only_here_and_never_pushed(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = True
        p1, p2 = insert_photo(conn), insert_photo(conn)
        web = _album_with(conn, "Reunion", [p1, p2], album_id=WEB_ID_FLOOR + 3)
        desk = _album_with(conn, "Album 6", [p1])
        assert albums_repo.update_album(conn, web, name="x") is False
        assert albums_repo.set_album_deleted(conn, web, True) is False
        assert albums_repo.set_photo_in_album(conn, web, p1, False) is False
        assert albums_repo.reorder_album(conn, web, [p2, p1]) == 0
        assert _row(conn, web, p1) == (1, False)
        rows = albums_repo.list_albums(conn)
        assert {a.name: a.web_born for a in rows} == {"Reunion": True, "Album 6": False}

        # The push's album_photos stage selects only desktop-born albums,
        # and carries the laptop's human-edit stamp.
        sql, marshal = next((q, m) for name, q, m in _META_STAGES if name == "album_photos")
        with conn.cursor(row_factory=__import__("psycopg").rows.dict_row) as cur:
            cur.execute(sql, {"web_id_floor": WEB_ID_FLOOR})
            items = [marshal(r) for r in cur.fetchall()]
        assert {i["album_id"] for i in items} == {desk}
        assert "edited_on_desktop_at" in items[0]

        # A laptop edit stamps the pair the web weighs it by.
        assert albums_repo.set_photo_in_album(conn, desk, p2, True) is True
        assert conn.execute(
            "select edited_on_desktop_at is not null from album_photos where album_id = %s and photo_id = %s",
            (desk, p2)).fetchone()[0] is True


def test_corrections_never_sweep_a_web_born_album(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = True
        _album_with(conn, "Summer Canaca", [], album_id=WEB_ID_FLOOR + 4)
        desk = _album_with(conn, "Winter Canaca", [])
        groups = corr_repo.search(conn, "Canaca", "Canada")
    names = {g.target.key: g for g in groups}
    assert names["album_name"].count == 1
    assert [r.key["id"] for r in names["album_name"].rows] == [desk]
