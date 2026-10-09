"""Where human-visible text lives, declared once.

Phase 15's rule is that every piece of text a person can see must be
correctable from the app that owns it. This module is the list of those
places on the desktop side — one `Target` per (table, field) — and it is
deliberately pure: no Qt, no database, no I/O. `repo.py` turns a target
into rows; `ui.py` turns rows into checkboxes. A new editable field is a
new entry here and nothing else.

Three row shapes, because three tables genuinely differ:

* ``COLUMN`` — a text column on a table with a bigint ``id``. The common
  case.
* ``JSON`` — ``suggestions.payload`` is jsonb and the words sit at a path
  inside it (``text`` for a description, ``evidence`` for a date). The
  payload around them must survive the edit untouched, so the write is a
  ``jsonb_set``, never a replacement of the whole document.
* ``ALIAS`` — ``place_aliases`` keys on ``(place_id, alias)``: the text
  *is* the primary key, so an edit renames the row and can collide with an
  alias the place already has. `repo` handles that explicitly rather than
  hiding it behind an upsert that would drop the row. Removed aliases are
  soft-deleted (fix-up 1) and are not offered for correction.

The master-derived group is listed too, with ``editable=False``. Phase 15
answer 3: `photos.source_folder`, `photos.scan_batch` and
`photo_masters.master_path` are faithful records of what is on the
read-only master disk (and `master_path` is the key that makes re-ingest
a no-op), so they keep the typo on purpose. Showing them — with a count
and the reason — is how the typo's full extent stays visible instead of
looking like rows the tool missed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Shape(str, Enum):
    COLUMN = "column"
    JSON = "json"
    ALIAS = "alias"


@dataclass(frozen=True)
class Target:
    #: Stable identifier. Goes into the audit row, so never rename one of
    #: these without a thought for the undo of an older correction.
    key: str
    #: What the group is called in the UI.
    label: str
    shape: Shape
    #: Table the text lives in, and the audit `entity_type`.
    table: str
    entity_type: str
    #: Text column (COLUMN / ALIAS) or jsonb path (JSON).
    field: str
    #: SQL returning `key` (jsonb — whatever `repo`'s writer needs to find
    #: the row again), `context` (text shown next to the value in the
    #: preview) and `value` (the text itself). One `%(pattern)s`
    #: placeholder, already wrapped in `%…%` by the caller, and
    #: `%(op)s` is spliced in as `like` or `ilike` by `repo`.
    find_sql: str
    #: False for the read-only master-derived group: counted and shown,
    #: never written, no checkbox.
    editable: bool = True
    #: Shown under the group heading.
    note: str = ""
    #: True when the row also carries `edited_on_desktop_at` (the five
    #: tables of the Phase 15 LWW pattern). Desktop-authoritative tables
    #: that the web cannot edit do not have the column and do not need it.
    stamps_edit: bool = True


def _column(
    key: str, label: str, table: str, entity_type: str, field: str,
    *, display: str, extra: str = "", note: str = "",
    editable: bool = True, stamps_edit: bool = True,
) -> Target:
    where = f"t.{field} {{op}} %(pattern)s"
    if extra:
        where = f"{where} and {extra}"
    return Target(
        key=key, label=label, shape=Shape.COLUMN, table=table,
        entity_type=entity_type, field=field, note=note,
        editable=editable, stamps_edit=stamps_edit,
        find_sql=f"""
            select jsonb_build_object('id', t.id) as key,
                   {display} as context,
                   t.{field} as value
              from {table} t
             where {where}
             order by t.id
        """,
    )


TARGETS: list[Target] = [
    # ---- albums -----------------------------------------------------
    _column(
        "album_name", "Album names", "albums", "album", "name",
        display="'#' || t.id",
    ),
    _column(
        "album_description", "Album descriptions", "albums", "album", "description",
        display="'#' || t.id || ' — ' || t.name",
    ),

    # ---- pending suggestions ----------------------------------------
    # Only `pending` rows. An accepted or rejected suggestion is a record
    # of a decision that was made on the text as it stood; rewriting it
    # would falsify the decision. The fact column the accept wrote is
    # corrected on its own screen instead.
    Target(
        key="suggestion_description", label="Pending description suggestions",
        shape=Shape.JSON, table="suggestions", entity_type="suggestion",
        field="text",
        note="The photo's words live here, not in a caption column.",
        find_sql="""
            select jsonb_build_object('id', t.id, 'path', 'text') as key,
                   '#' || t.id || ' — photo ' || coalesce(t.photo_id::text, '—') as context,
                   t.payload->>'text' as value
              from suggestions t
             where t.kind = 'description' and t.status = 'pending'
               and t.payload->>'text' {op} %(pattern)s
             order by t.id
        """,
    ),
    Target(
        key="suggestion_transcription", label="Pending transcription suggestions",
        shape=Shape.JSON, table="suggestions", entity_type="suggestion",
        field="text",
        find_sql="""
            select jsonb_build_object('id', t.id, 'path', 'text') as key,
                   '#' || t.id || ' — photo ' || coalesce(t.photo_id::text, '—') as context,
                   t.payload->>'text' as value
              from suggestions t
             where t.kind = 'transcription' and t.status = 'pending'
               and t.payload->>'text' {op} %(pattern)s
             order by t.id
        """,
    ),
    Target(
        key="suggestion_date_evidence", label="Pending date suggestions (evidence)",
        shape=Shape.JSON, table="suggestions", entity_type="suggestion",
        field="evidence",
        note="The quoted words a date was read from — not the date itself.",
        find_sql="""
            select jsonb_build_object('id', t.id, 'path', 'evidence') as key,
                   '#' || t.id || ' — photo ' || coalesce(t.photo_id::text, '—') as context,
                   t.payload->>'evidence' as value
              from suggestions t
             where t.kind = 'date' and t.status = 'pending'
               and t.payload->>'evidence' {op} %(pattern)s
             order by t.id
        """,
    ),

    # ---- people -----------------------------------------------------
    # display_name is generated by the people_set_display_name trigger, so
    # it is not listed: correcting a name field regenerates it.
    *[
        _column(
            f"person_{col}", f"People — {human}", "people", "person", col,
            display="'#' || t.id || ' — ' || coalesce(t.display_name, '(unnamed)')",
            extra="t.is_deleted = false",
        )
        for col, human in [
            ("given_name", "given name"), ("middle_name", "middle name"),
            ("surname", "surname"), ("maiden_name", "maiden name"),
            ("nickname", "nickname"), ("suffix", "suffix"), ("notes", "notes"),
        ]
    ],
    _column(
        "person_variant", "People — name variants", "person_name_variants",
        "person", "variant",
        display="'#' || t.id || ' — person ' || t.person_id",
        extra="t.is_deleted = false",
    ),

    # ---- places -----------------------------------------------------
    _column(
        "place_name", "Place names", "places", "place", "name",
        display="'#' || t.id",
        extra="t.is_deleted = false",
    ),
    _column(
        "place_notes", "Place notes", "places", "place", "notes",
        display="'#' || t.id || ' — ' || t.name",
        extra="t.is_deleted = false",
    ),
    Target(
        key="place_alias", label="Place aliases", shape=Shape.ALIAS,
        table="place_aliases", entity_type="place", field="alias",
        find_sql="""
            select jsonb_build_object('place_id', t.place_id, 'alias', t.alias) as key,
                   'place ' || t.place_id || ' — ' || p.name as context,
                   t.alias as value
              from place_aliases t
              join places p on p.id = t.place_id
             where t.alias {op} %(pattern)s and t.is_deleted = false
             order by t.place_id, t.alias
        """,
    ),

    # ---- backs and physical locators --------------------------------
    # Desktop-authoritative: the web has no editor for either, and
    # /sync/photo_backs and /sync/photos both assign them in their
    # `do update set`, so an edit here reaches the VM on the next push.
    # Neither table carries `edited_on_desktop_at`; neither needs it.
    _column(
        "back_transcription", "Back-of-print transcriptions", "photo_backs",
        "photo_back", "transcribed_text",
        display="'back #' || t.id || ' — photo ' || coalesce(t.photo_id::text, '(orphan)')",
        stamps_edit=False,
    ),
    _column(
        "physical_ref_note", "Photo physical-reference notes", "photos",
        "photo", "physical_ref_note",
        note="Where the print lives. Dedupe appends scan locators here with '| '.",
        display="'photo #' || t.id",
        stamps_edit=False,
    ),

    # ---- read-only: mirrors of the master disk ----------------------
    _column(
        "master_source_folder", "Photo source folders", "photos", "photo", "source_folder",
        display="'photo #' || t.id", editable=False, stamps_edit=False,
        note="Mirrors the master disk, not editable.",
    ),
    _column(
        "master_scan_batch", "Photo scan batches", "photos", "photo", "scan_batch",
        display="'photo #' || t.id", editable=False, stamps_edit=False,
        note="Mirrors the master disk, not editable.",
    ),
    _column(
        "master_path", "Master file paths", "photo_masters", "photo_master", "master_path",
        display="'master #' || t.id || ' — photo ' || t.photo_id",
        editable=False, stamps_edit=False,
        note="Mirrors the master disk, and is the key that makes re-ingest a no-op.",
    ),
]

BY_KEY: dict[str, Target] = {t.key: t for t in TARGETS}

#: Why the read-only group exists, shown on screen under its heading.
READ_ONLY_REASON = (
    "These mirror the read-only master disk exactly, so they keep the "
    "original spelling on purpose. Correct the derived text (album names, "
    "suggestions) and leave these disagreeing with it."
)


def editable_targets() -> list[Target]:
    return [t for t in TARGETS if t.editable]


def read_only_targets() -> list[Target]:
    return [t for t in TARGETS if not t.editable]
