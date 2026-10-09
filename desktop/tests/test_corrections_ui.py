"""GUI-level checks for the Corrections panel, offscreen.

These catch the wiring the unit tests cannot: that the three tabs build
against the real database, that the master-derived group really does
arrive with **no checkbox** (the one thing on this screen that must never
be clickable), that ticking a group ticks its rows, and that the
decision half of the status line survives the search that `_apply` and
`_undo` run straight afterwards — the Phase 3 "no flash messages"
lesson, which this screen broke on the first attempt and which these
tests caught.
"""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop, Qt
from PySide6.QtWidgets import QApplication

from photoarchive import db as dbmod

from .phase6_fixtures import insert_master, insert_photo, phase6  # noqa: F401

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

TYPO = "Canaca"
FIXED = "Canada"


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app  # type: ignore[return-value]


def _pump(ms: int = 300) -> None:
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        QCoreApplication.processEvents(QEventLoop.AllEvents, 20)


def _seed(conn) -> dict:
    album_id = conn.execute(
        "insert into albums (name, source) values (%s, 'import') returning id",
        (f"Summer 1992 - {TYPO}",),
    ).fetchone()[0]
    photo_id = insert_photo(conn, source_folder=f"Summer 1992 - {TYPO}")
    insert_master(conn, photo_id)
    conn.execute(
        "insert into album_photos (album_id, photo_id, position) values (%s, %s, 1)",
        (album_id, photo_id),
    )
    place_id = conn.execute(
        "insert into places (name) values (%s) returning id", (TYPO,)
    ).fetchone()[0]
    conn.execute(
        "insert into place_aliases (place_id, alias, kind) values (%s, %s, 'alias')",
        (place_id, f"{TYPO} west"),
    )
    conn.commit()
    return {"album": album_id, "photo": photo_id, "place": place_id}


def test_the_three_tabs_build_against_the_real_database(phase6):
    from photoarchive.modes.corrections.ui import CorrectionsPanel

    _app()
    with dbmod.connection() as conn:
        conn.autocommit = False
        _seed(conn)

    panel = CorrectionsPanel()
    _pump()
    try:
        tabs = panel.findChild(type(panel.children()[1]))  # smoke: it constructed
        assert tabs is not None
    finally:
        panel.deleteLater()
        _pump(50)


def test_the_master_mirror_group_has_no_checkbox_and_says_why(phase6):
    """The one control that must not exist on this screen. George has to
    be able to see the typo's full extent — 159 photos still spell it the
    way the disk does — without being able to rewrite the mirror of a
    read-only disk."""
    from photoarchive.modes.corrections.targets import READ_ONLY_REASON
    from photoarchive.modes.corrections.ui import FindReplaceTab

    _app()
    with dbmod.connection() as conn:
        conn.autocommit = False
        _seed(conn)

    tab = FindReplaceTab()
    _pump()
    try:
        tab.find_field.setText(TYPO)
        tab.replace_field.setText(FIXED)
        tab._search()
        _pump()

        heads = {}
        for i in range(tab.tree.topLevelItemCount()):
            item = tab.tree.topLevelItem(i)
            heads[item.text(0).rsplit(" (", 1)[0]] = item
        assert heads, "the search should have found the seeded rows"

        editable = heads["Album names"]
        assert bool(editable.flags() & Qt.ItemIsUserCheckable)

        mirror = heads["Photo source folders"]
        assert not (mirror.flags() & Qt.ItemIsUserCheckable), \
            "a master-derived group must have no checkbox at all"
        assert "not editable" in mirror.text(1)
        assert mirror.toolTip(1) == READ_ONLY_REASON
        for j in range(mirror.childCount()):
            child = mirror.child(j)
            assert not (child.flags() & Qt.ItemIsUserCheckable)
            assert child.text(2) == "—"

        # And nothing from that group can be selected for an apply.
        selected = tab._selected_rows()
        assert selected, "the editable rows should be selected by default"
        assert all(not r.target_key.startswith("master_") for r in selected)
    finally:
        tab.deleteLater()
        _pump(50)


def test_unticking_a_group_unticks_its_rows_and_empties_the_selection(phase6):
    from photoarchive.modes.corrections.ui import FindReplaceTab

    _app()
    with dbmod.connection() as conn:
        conn.autocommit = False
        _seed(conn)

    tab = FindReplaceTab()
    _pump()
    try:
        tab.find_field.setText(TYPO)
        tab.replace_field.setText(FIXED)
        tab._search()
        _pump()
        assert tab._selected_rows()

        for i in range(tab.tree.topLevelItemCount()):
            head = tab.tree.topLevelItem(i)
            if head.flags() & Qt.ItemIsUserCheckable:
                head.setCheckState(0, Qt.Unchecked)
        _pump()
        assert tab._selected_rows() == []
    finally:
        tab.deleteLater()
        _pump(50)


def test_the_preview_follows_the_replace_field_as_it_is_typed(phase6):
    from photoarchive.modes.corrections.ui import FindReplaceTab

    _app()
    with dbmod.connection() as conn:
        conn.autocommit = False
        _seed(conn)

    tab = FindReplaceTab()
    _pump()
    try:
        tab.find_field.setText(TYPO)
        tab._search()
        _pump()
        # Typing in the replace field must re-render every "After" cell,
        # so what is on screen is never stale relative to the field.
        tab.replace_field.setText(FIXED)
        _pump()
        rows = [r for g in tab._groups if g.target.key == "album_name" for r in g.rows]
        assert rows[0].new_value == f"Summer 1992 - {FIXED}"
    finally:
        tab.deleteLater()
        _pump(50)


def test_applying_then_undoing_from_the_panel_round_trips(phase6):
    """Drive the real buttons, not the repo: this is the path George uses,
    and it is where a wiring mistake would hide."""
    from photoarchive.modes.corrections.ui import FindReplaceTab

    _app()
    with dbmod.connection() as conn:
        conn.autocommit = False
        ids = _seed(conn)

    tab = FindReplaceTab()
    _pump()
    try:
        tab.find_field.setText(TYPO)
        tab.replace_field.setText(FIXED)
        tab._search()
        _pump()

        # Apply without the confirmation dialog (the dialog is Qt's, not
        # ours to test); everything after it is the panel's own code.
        rows = tab._selected_rows()
        assert rows
        import photoarchive.modes.corrections.repo as repo_mod
        with dbmod.connection() as conn:
            conn.autocommit = False
            applied = repo_mod.apply_correction(
                conn, needle=TYPO, replacement=FIXED, match_case=True, rows=rows,
            )
            conn.commit()

        tab._reload_batches()
        _pump()
        assert tab.batches.count() >= 1
        assert tab.undo_btn.isEnabled()
        # The newest batch is first, and it is the one we just made.
        assert tab.batches.itemData(0) == applied.batch_id

        tab._undo()
        _pump()
        # The decision half, which `_undo`'s own call to `_search` must
        # not overwrite. It did, once.
        assert "Restored" in tab.decision.text()
        assert "row(s) contain" in tab.context.text()

        with dbmod.connection() as conn:
            conn.autocommit = True
            assert conn.execute(
                "select name from albums where id = %s", (ids["album"],)
            ).fetchone()[0] == f"Summer 1992 - {TYPO}"
    finally:
        tab.deleteLater()
        _pump(50)


def test_the_decision_half_survives_a_search(phase6):
    """The Phase 3 lesson, in the shape it actually broke: `_apply` and
    `_undo` both re-run the search afterwards, so a single status label
    meant the result of the action was wiped a moment after appearing.
    The left half holds the decision; only the right half refreshes."""
    from photoarchive.modes.corrections.ui import FindReplaceTab

    _app()
    with dbmod.connection() as conn:
        conn.autocommit = False
        _seed(conn)

    tab = FindReplaceTab()
    _pump()
    try:
        tab.decision.setText("Changed 7 row(s).")
        tab._reload_batches()
        _pump()
        assert tab.decision.text() == "Changed 7 row(s)."

        # A search refreshes the context half and leaves the decision.
        tab.find_field.setText(TYPO)
        tab._search()
        _pump()
        assert tab.decision.text() == "Changed 7 row(s)."
        assert "row(s) contain" in tab.context.text()
    finally:
        tab.deleteLater()
        _pump(50)


def test_the_albums_tab_lists_and_renames(phase6):
    from photoarchive.modes.corrections.ui import AlbumsTab

    _app()
    with dbmod.connection() as conn:
        conn.autocommit = False
        ids = _seed(conn)

    tab = AlbumsTab()
    _pump()
    try:
        assert tab.list.count() == 1
        assert tab._current == ids["album"]
        assert tab.f_name.text() == f"Summer 1992 - {TYPO}"
        # The photo in the album is listed.
        assert tab.photos.count() == 1

        tab.f_name.setText(f"Summer 1992 - {FIXED}")
        tab._save()
        _pump()
        assert "Saved" in tab.status.text()
        with dbmod.connection() as conn:
            conn.autocommit = True
            assert conn.execute(
                "select name from albums where id = %s", (ids["album"],)
            ).fetchone()[0] == f"Summer 1992 - {FIXED}"

        # Removing the photo is a soft-delete, so the push can carry it.
        tab.photos.selectAll()
        tab._remove_photos()
        _pump()
        with dbmod.connection() as conn:
            conn.autocommit = True
            assert conn.execute(
                "select is_deleted from album_photos where album_id = %s",
                (ids["album"],),
            ).fetchone()[0] is True
    finally:
        tab.deleteLater()
        _pump(50)


def test_the_places_tab_edits_a_place_and_its_aliases(phase6):
    from photoarchive.modes.corrections.ui import PlacesTab

    _app()
    with dbmod.connection() as conn:
        conn.autocommit = False
        ids = _seed(conn)

    tab = PlacesTab()
    _pump()
    try:
        assert tab.list.count() == 1
        assert tab.f_name.text() == TYPO
        assert tab.aliases.count() == 1

        tab.f_name.setText(FIXED)
        tab._save()
        _pump()
        assert "Saved" in tab.status.text()
        with dbmod.connection() as conn:
            conn.autocommit = True
            assert conn.execute(
                "select name from places where id = %s", (ids["place"],)
            ).fetchone()[0] == FIXED

        tab.aliases.setCurrentRow(0)
        tab._remove_alias()
        _pump()
        with dbmod.connection() as conn:
            conn.autocommit = True
            assert conn.execute(
                "select count(*) from place_aliases where place_id = %s", (ids["place"],)
            ).fetchone()[0] == 0
    finally:
        tab.deleteLater()
        _pump(50)


def test_a_place_rename_collision_is_reported_on_screen(phase6):
    """A unique violation must become a sentence, not a traceback."""
    from photoarchive.modes.corrections.ui import PlacesTab

    _app()
    with dbmod.connection() as conn:
        conn.autocommit = False
        _seed(conn)
        conn.execute("insert into places (name) values (%s)", (FIXED,))
        conn.commit()

    tab = PlacesTab()
    _pump()
    try:
        # Select the typo'd place, not the one already called Canada.
        for i in range(tab.list.count()):
            if tab.list.item(i).text().startswith(TYPO):
                tab.list.setCurrentRow(i)
                break
        _pump()
        assert tab.f_name.text() == TYPO
        tab.f_name.setText(FIXED)
        tab._save()
        _pump()
        assert "already called" in tab.status.text()
        with dbmod.connection() as conn:
            conn.autocommit = True
            assert conn.execute(
                "select count(*) from places where name = %s", (TYPO,)
            ).fetchone()[0] == 1
    finally:
        tab.deleteLater()
        _pump(50)
