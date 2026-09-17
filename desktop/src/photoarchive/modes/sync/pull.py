"""Pull:
   1. Groups + memberships + photo_groups from the web (last-writer-wins).
   2. Confirmed values (accepted suggestions, fact-set audit rows) into
      the laptop DB, tagged `source='web'` in audit rows for provenance.
      First, web-born rows (ids >= WEB_ID_FLOOR: people, places,
      relationships, faces) are copied down with their web ids so the
      facts that reference them have something to land on (Phase 9
      fix-up 1). Web-born faces arrive with `embedding=null`,
      `embedding_stale=true` and a freshly cut face crop.
   3. Approved contributions: fetch bytes into an append-only `contrib`
      master root at CONTRIB_ROOT, then trigger ingest for that root
      with `triage_status` pre-set to `keep` and provenance
      `uploaded_by`.

The append-only invariant is enforced by two things:
  - the masters guard, which for kind='contrib' roots requires the
    root itself + every committed subfolder to be read-only, and only
    `<root>/_incoming/<contribution_id>/` may be created;
  - the pull writer, which stages files under `_incoming` first and
    then renames to `<root>/<uploader>/<contribution_id>/` only after
    a manifest confirms nothing existing changed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from psycopg.rows import dict_row

from ... import db
from ...id_ranges import is_web_origin
from ..ingest.paths import resolve_working_path
from .client import WebSyncClient

log = logging.getLogger(__name__)

STATE_FILE = "sync_state.json"


@dataclass
class PullProgress:
    stage: str
    done: int
    total: int
    detail: str = ""


@dataclass
class PullStats:
    groups: int = 0
    members: int = 0
    photo_groups: int = 0
    facts_applied: int = 0
    contributions_pulled: int = 0
    files_pulled: int = 0


def _state_path(state_dir: Path) -> Path:
    return state_dir / STATE_FILE


def _load_state(state_dir: Path) -> dict:
    p = _state_path(state_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return {}


def _save_state(state_dir: Path, state: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    _state_path(state_dir).write_text(json.dumps(state, indent=2, default=str), "utf-8")


# ---------------------------------------------------------------------------
# 1. Groups pull
# ---------------------------------------------------------------------------

def pull_groups(client: WebSyncClient, state_dir: Path) -> tuple[int, int, int]:
    state = _load_state(state_dir)
    since = state.get("groups_cursor")
    data = client.pull_groups(since)
    g_n = m_n = pg_n = 0
    with db.connection() as conn:
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                for u in data.get("users", []):
                    cur.execute(
                        """
                        insert into users (id, email, display_name, role, status, is_service)
                        values (%s, lower(%s), %s, %s, %s, false)
                        on conflict (id) do update set
                          email = excluded.email,
                          display_name = excluded.display_name,
                          role = excluded.role,
                          status = excluded.status,
                          updated_at = now()
                        """,
                        (u["id"], u["email"], u.get("display_name"), u.get("role"), u.get("status")),
                    )
                for g in data.get("groups", []):
                    cur.execute(
                        """
                        insert into groups (id, name, description, created_by,
                                            is_deleted, deleted_at, deleted_by,
                                            created_at, updated_at)
                        values (%s, %s, %s, %s, %s, %s, %s,
                                coalesce(%s::timestamptz, now()), now())
                        on conflict (id) do update set
                          name = excluded.name,
                          description = excluded.description,
                          is_deleted = excluded.is_deleted,
                          deleted_at = excluded.deleted_at,
                          deleted_by = excluded.deleted_by,
                          updated_at = now()
                        """,
                        (
                            g["id"], g["name"], g.get("description"), g.get("created_by"),
                            g.get("is_deleted", False),
                            g.get("deleted_at"), g.get("deleted_by"),
                            g.get("created_at"),
                        ),
                    )
                    g_n += 1
                for m in data.get("members", []):
                    cur.execute(
                        """
                        insert into group_members (group_id, user_id, role, added_by, added_at,
                                                   is_deleted, deleted_at, deleted_by, updated_at)
                        values (%s, %s, %s, %s, coalesce(%s::timestamptz, now()),
                                %s, %s, %s, now())
                        on conflict (group_id, user_id) do update set
                          role = excluded.role,
                          added_by = coalesce(excluded.added_by, group_members.added_by),
                          is_deleted = excluded.is_deleted,
                          deleted_at = excluded.deleted_at,
                          deleted_by = excluded.deleted_by,
                          updated_at = now()
                        """,
                        (
                            m["group_id"], m["user_id"], m.get("role", "member"),
                            m.get("added_by"), m.get("added_at"),
                            m.get("is_deleted", False),
                            m.get("deleted_at"), m.get("deleted_by"),
                        ),
                    )
                    m_n += 1
                for pg in data.get("photo_groups", []):
                    # Only merge if we have the photo id locally (private-only
                    # photos never travel, so a photo_groups row we get from the
                    # web with an unknown photo id is dropped silently).
                    cur.execute(
                        """
                        insert into photo_groups (photo_id, group_id, added_by, added_at,
                                                  is_deleted, deleted_at, deleted_by, updated_at)
                        select %s, %s, %s, coalesce(%s::timestamptz, now()),
                               %s, %s, %s, now()
                         where exists (select 1 from photos where id = %s)
                        on conflict (photo_id, group_id) do update set
                          added_by = coalesce(excluded.added_by, photo_groups.added_by),
                          is_deleted = excluded.is_deleted,
                          deleted_at = excluded.deleted_at,
                          deleted_by = excluded.deleted_by,
                          updated_at = now()
                        """,
                        (
                            pg["photo_id"], pg["group_id"], pg.get("added_by"),
                            pg.get("added_at"),
                            pg.get("is_deleted", False),
                            pg.get("deleted_at"), pg.get("deleted_by"),
                            pg["photo_id"],
                        ),
                    )
                    pg_n += 1
            db.audit(conn, actor="web", action="sync.pull.groups",
                     entity_type="sync", entity_id=None,
                     new_value={"groups": g_n, "members": m_n, "photo_groups": pg_n})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    state["groups_cursor"] = data.get("cursor")
    _save_state(state_dir, state)
    return g_n, m_n, pg_n


# ---------------------------------------------------------------------------
# 2. Confirmed values pull
# ---------------------------------------------------------------------------

_PEOPLE_UPSERT = """
    insert into people (id, given_name, middle_name, surname, maiden_name,
                        nickname, suffix, birth_year, death_year, notes,
                        is_deleted, created_at)
    values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, coalesce(%s::timestamptz, now()))
    on conflict (id) do update set
      given_name = excluded.given_name, middle_name = excluded.middle_name,
      surname = excluded.surname, maiden_name = excluded.maiden_name,
      nickname = excluded.nickname, suffix = excluded.suffix,
      birth_year = excluded.birth_year, death_year = excluded.death_year,
      notes = excluded.notes, is_deleted = excluded.is_deleted
"""

_PLACES_UPSERT = """
    insert into places (id, name, latitude, longitude, notes, is_deleted, created_at)
    values (%s,%s,%s,%s,%s,%s, coalesce(%s::timestamptz, now()))
    on conflict (id) do update set
      name = excluded.name, latitude = excluded.latitude,
      longitude = excluded.longitude, notes = excluded.notes,
      is_deleted = excluded.is_deleted
"""

_FACES_UPSERT = """
    insert into faces (id, photo_id, person_id, bbox, embedding, embedding_stale,
                       source, is_disputed, dispute_note,
                       review_status, review_note, reviewed_at,
                       is_deleted, deleted_at, delete_reason, created_at)
    values (%(id)s, %(photo_id)s, %(person_id)s, %(bbox)s::jsonb, null, true,
            'human', %(is_disputed)s, %(dispute_note)s,
            coalesce(%(review_status)s, 'pending'), %(review_note)s, %(reviewed_at)s,
            %(is_deleted)s, %(deleted_at)s, %(delete_reason)s,
            coalesce(%(created_at)s::timestamptz, now()))
    on conflict (id) do update set
      person_id = excluded.person_id,
      bbox = excluded.bbox,
      embedding = case when %(bbox_changed)s then null else faces.embedding end,
      embedding_stale = case when %(bbox_changed)s then true else faces.embedding_stale end,
      is_disputed = excluded.is_disputed,
      dispute_note = excluded.dispute_note,
      review_status = excluded.review_status,
      review_note = excluded.review_note,
      reviewed_at = excluded.reviewed_at,
      is_deleted = excluded.is_deleted,
      deleted_at = excluded.deleted_at,
      delete_reason = excluded.delete_reason
"""


def pull_web_origin(
    client: WebSyncClient,
    state_dir: Path,
    *,
    working_dir: Path | None = None,
    thumbs_dir: Path | None = None,
) -> dict[str, int]:
    """Copy web-born people / places / relationships / faces (ids at or
    above WEB_ID_FLOOR) down with their web ids. The web is authoritative
    for these rows: a desktop edit to one is overwritten by the next pull
    and never pushed. Face crops go to THUMBS_DIR/faces/ when both
    directories are given. Returns per-table counts."""
    state = _load_state(state_dir)
    data = client.pull_web_origin(state.get("web_origin_cursor"))
    counts = {"people": 0, "places": 0, "relationships": 0, "faces": 0, "crops": 0}
    crops: list[tuple[int, int, dict]] = []  # (face_id, photo_id, bbox)
    with db.connection() as conn:
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                for p in data.get("people", []):
                    if not is_web_origin(p.get("id")):
                        continue
                    cur.execute(_PEOPLE_UPSERT, (
                        p["id"], p.get("given_name"), p.get("middle_name"), p.get("surname"),
                        p.get("maiden_name"), p.get("nickname"), p.get("suffix"),
                        p.get("birth_year"), p.get("death_year"), p.get("notes"),
                        bool(p.get("is_deleted")), p.get("created_at"),
                    ))
                    counts["people"] += 1

                for pl in data.get("places", []):
                    if not is_web_origin(pl.get("id")):
                        continue
                    # places.name is unique on lower(name): a same-named
                    # local place keeps its row; log and skip the web one.
                    cur.execute(
                        "select id from places where lower(name) = lower(%s) and id <> %s",
                        (pl["name"], pl["id"]),
                    )
                    clash = cur.fetchone()
                    if clash:
                        log.warning("pull web_origin: place %s %r clashes with local place %s; skipped",
                                    pl["id"], pl["name"], clash[0])
                        continue
                    cur.execute(_PLACES_UPSERT, (
                        pl["id"], pl["name"], pl.get("latitude"), pl.get("longitude"),
                        pl.get("notes"), bool(pl.get("is_deleted")), pl.get("created_at"),
                    ))
                    counts["places"] += 1

                for r in data.get("relationships", []):
                    if not is_web_origin(r.get("id")):
                        continue
                    cur.execute(
                        """
                        select id from relationships
                         where person_a_id = %s and person_b_id = %s and type = %s and id <> %s
                        """,
                        (r["person_a_id"], r["person_b_id"], r["type"], r["id"]),
                    )
                    local = cur.fetchone()
                    if local:
                        # Same triple already here under a desktop id.
                        cur.execute(
                            "update relationships set confirmed = confirmed or %s where id = %s",
                            (bool(r.get("confirmed")), local[0]),
                        )
                    else:
                        cur.execute(
                            """
                            insert into relationships (id, person_a_id, person_b_id, type, confirmed, created_at)
                            values (%s,%s,%s,%s,%s, coalesce(%s::timestamptz, now()))
                            on conflict (id) do update set confirmed = excluded.confirmed
                            """,
                            (r["id"], r["person_a_id"], r["person_b_id"], r["type"],
                             bool(r.get("confirmed")), r.get("created_at")),
                        )
                    counts["relationships"] += 1

                for f in data.get("faces", []):
                    if not is_web_origin(f.get("id")):
                        continue
                    cur.execute("select 1 from photos where id = %s", (f["photo_id"],))
                    if cur.fetchone() is None:
                        log.warning("pull web_origin: face %s on unknown photo %s; skipped",
                                    f["id"], f["photo_id"])
                        continue
                    bbox = f.get("bbox") or {}
                    cur.execute("select bbox from faces where id = %s", (f["id"],))
                    existing = cur.fetchone()
                    bbox_changed = existing is None or (existing[0] or {}) != bbox
                    cur.execute(_FACES_UPSERT, {
                        "id": f["id"], "photo_id": f["photo_id"], "person_id": f.get("person_id"),
                        "bbox": json.dumps(bbox),
                        "is_disputed": bool(f.get("is_disputed")), "dispute_note": f.get("dispute_note"),
                        "review_status": f.get("review_status"), "review_note": f.get("review_note"),
                        "reviewed_at": f.get("reviewed_at"),
                        "is_deleted": bool(f.get("is_deleted")), "deleted_at": f.get("deleted_at"),
                        "delete_reason": f.get("delete_reason"), "created_at": f.get("created_at"),
                        "bbox_changed": bbox_changed,
                    })
                    counts["faces"] += 1
                    if bbox_changed and not f.get("is_deleted"):
                        crops.append((int(f["id"]), int(f["photo_id"]), bbox))
                    cur.execute("select refresh_completeness(%s)", (f["photo_id"],))
            db.audit(conn, actor="web", action="sync.pull.web_origin",
                     entity_type="sync", entity_id=None,
                     new_value={k: v for k, v in counts.items() if k != "crops"})
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        if crops and working_dir is not None and thumbs_dir is not None:
            counts["crops"] = _write_web_face_crops(conn, crops, working_dir, thumbs_dir)

    state["web_origin_cursor"] = data.get("cursor")
    _save_state(state_dir, state)
    return counts


def _write_web_face_crops(conn, crops, working_dir: Path, thumbs_dir: Path) -> int:
    """THUMBS_DIR/faces/{face_id}.jpg for web-born faces — the same crop
    detect_faces makes. A missing working file logs and skips; the DB
    side is already committed."""
    from ...jobs.detect_faces import _write_face_crops

    out_dir = thumbs_dir / "faces"
    out_dir.mkdir(parents=True, exist_ok=True)
    by_photo: dict[int, list[tuple[int, dict]]] = {}
    for face_id, photo_id, bbox in crops:
        by_photo.setdefault(photo_id, []).append((face_id, bbox))
    written = 0
    with conn.cursor() as cur:
        for photo_id, faces in by_photo.items():
            cur.execute("select working_path from photos where id = %s", (photo_id,))
            row = cur.fetchone()
            if not row or not row[0]:
                log.warning("pull web_origin: photo %s has no working_path; no face crops", photo_id)
                continue
            _write_face_crops(resolve_working_path(working_dir, row[0]), faces, out_dir)
            written += sum(1 for fid, _ in faces if (out_dir / f"{fid}.jpg").exists())
    conn.commit()
    return written


def pull_confirmed(
    client: WebSyncClient,
    state_dir: Path,
    *,
    working_dir: Path | None = None,
    thumbs_dir: Path | None = None,
) -> int:
    # Web-born rows first: a face.assign / photo.place.set / relationship
    # fact may point at a face, place or person that only exists on the web.
    pull_web_origin(client, state_dir, working_dir=working_dir, thumbs_dir=thumbs_dir)

    state = _load_state(state_dir)
    since = state.get("confirmed_cursor")
    data = client.pull_confirmed(since)
    applied = 0
    with db.connection() as conn:
        conn.autocommit = False
        try:
            for entry in data.get("fact_audits", []):
                action = entry.get("action")
                eid = entry.get("entity_id")
                new_value = entry.get("new_value") or {}
                if action == "photo.capture_date.set" and eid:
                    _apply_date_set(conn, eid, new_value)
                    applied += 1
                elif action == "photo.description.set" and eid:
                    _apply_description(conn, eid, new_value)
                    applied += 1
                elif action == "photo.place.set" and eid:
                    _apply_place(conn, eid, new_value)
                    applied += 1
                elif action == "photo.has_no_people.set" and eid:
                    _apply_no_people(conn, eid, new_value)
                    applied += 1
                elif action == "face.assign" and eid:
                    _apply_face_assign(conn, eid, new_value)
                    applied += 1
                elif action == "face.dispute.resolve" and eid:
                    _apply_face_dispute_resolve(conn, eid, new_value)
                    applied += 1
                elif action == "relationship.confirm":
                    _apply_relationship(conn, new_value)
                    applied += 1
                elif action == "photo.rescan_wanted" and eid:
                    _apply_rescan_wanted(conn, eid, new_value)
                    applied += 1
                elif action == "suggestion.reject" and eid and not is_web_origin(eid):
                    _apply_suggestion_status(conn, eid, "rejected", entry)
            # Desktop-pushed suggestions (ids below the floor) accepted on
            # the web: mirror the status so the laptop copy stops being pending.
            for s in data.get("accepted_suggestions", []):
                if s.get("id") and not is_web_origin(s["id"]):
                    _apply_suggestion_status(conn, s["id"], "accepted", s)
            db.audit(conn, actor="web", action="sync.pull.confirmed",
                     entity_type="sync", entity_id=None,
                     new_value={"applied": applied})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    state["confirmed_cursor"] = data.get("cursor")
    _save_state(state_dir, state)
    return applied


def _apply_date_set(conn, photo_id: int, nv: dict) -> None:
    cd = nv.get("capture_date")
    cp = nv.get("capture_date_precision", "unknown")
    with conn.cursor() as cur:
        cur.execute(
            """
            update photos
               set capture_date = %s,
                   capture_date_precision = %s,
                   capture_date_confirmed = true
             where id = %s
            """,
            (cd, cp, photo_id),
        )
        cur.execute("select refresh_completeness(%s)", (photo_id,))
    db.audit(conn, actor="web", action="photo.capture_date.set",
             entity_type="photo", entity_id=photo_id,
             new_value={"capture_date": cd, "capture_date_precision": cp, "source": "web"})


def _apply_description(conn, photo_id: int, nv: dict) -> None:
    text = nv.get("description_ai")
    with conn.cursor() as cur:
        cur.execute("update photos set description_ai = %s where id = %s", (text, photo_id))
    db.audit(conn, actor="web", action="photo.description.set",
             entity_type="photo", entity_id=photo_id,
             new_value={"description_ai": text, "source": "web"})


def _apply_place(conn, photo_id: int, nv: dict) -> None:
    place_id = nv.get("place_id")
    if not place_id:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into photo_places (photo_id, place_id, confirmed)
            values (%s, %s, true)
            on conflict (photo_id, place_id) do update set confirmed = true
            """,
            (photo_id, place_id),
        )
        cur.execute("select refresh_completeness(%s)", (photo_id,))
    db.audit(conn, actor="web", action="photo.place.set",
             entity_type="photo", entity_id=photo_id,
             new_value={"place_id": place_id, "source": "web"})


def _apply_rescan_wanted(conn, photo_id: int, nv: dict) -> None:
    wanted = bool(nv.get("rescan_wanted"))
    with conn.cursor() as cur:
        cur.execute(
            "update photos set rescan_wanted = %s where id = %s and rescan_wanted is distinct from %s",
            (wanted, photo_id, wanted),
        )
        changed = cur.rowcount
    if changed:
        db.audit(conn, actor="web", action="photo.rescan_wanted",
                 entity_type="photo", entity_id=photo_id,
                 new_value={"rescan_wanted": wanted, "source": "web"})


def _apply_suggestion_status(conn, suggestion_id: int, status: str, src: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            update suggestions
               set status = %s,
                   resolved_at = coalesce(%s::timestamptz, now()),
                   resolution_note = coalesce(%s, resolution_note)
             where id = %s and status = 'pending'
            """,
            (status, src.get("resolved_at") or src.get("created_at"),
             src.get("resolution_note"), suggestion_id),
        )


def _apply_no_people(conn, photo_id: int, _: dict) -> None:
    with conn.cursor() as cur:
        cur.execute("update photos set has_no_people = true where id = %s", (photo_id,))
        cur.execute("select refresh_completeness(%s)", (photo_id,))
    db.audit(conn, actor="web", action="photo.has_no_people.set",
             entity_type="photo", entity_id=photo_id, new_value={"source": "web"})


def _apply_face_assign(conn, face_id: int, nv: dict) -> None:
    person_id = nv.get("person_id")
    if not person_id:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            update faces
               set person_id = %s,
                   source = 'human',
                   is_disputed = false,
                   dispute_note = null
             where id = %s
            """,
            (person_id, face_id),
        )
        cur.execute("select photo_id from faces where id = %s", (face_id,))
        row = cur.fetchone()
        if row:
            cur.execute("select refresh_completeness(%s)", (row[0],))
    db.audit(conn, actor="web", action="face.assign",
             entity_type="face", entity_id=face_id,
             new_value={"person_id": person_id, "source": "web"})


def _apply_face_dispute_resolve(conn, face_id: int, nv: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update faces set is_disputed = false, dispute_note = null where id = %s",
            (face_id,),
        )
    db.audit(conn, actor="web", action="face.dispute.resolve",
             entity_type="face", entity_id=face_id, new_value={"source": "web"})


def _apply_relationship(conn, nv: dict) -> None:
    a = nv.get("person_a_id"); b = nv.get("person_b_id"); t = nv.get("type")
    if not a or not b or not t:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into relationships (person_a_id, person_b_id, type, confirmed)
            values (%s, %s, %s, true)
            on conflict (person_a_id, person_b_id, type) do update set confirmed = true
            """,
            (a, b, t),
        )
    db.audit(conn, actor="web", action="relationship.confirm",
             entity_type="relationship", entity_id=None,
             new_value={"person_a_id": a, "person_b_id": b, "type": t, "source": "web"})


# ---------------------------------------------------------------------------
# 3. Contributions pull → append-only master root
# ---------------------------------------------------------------------------

@dataclass
class ContribManifest:
    """Snapshot of every file inside a contrib root's committed area,
    used to prove nothing outside `_incoming/` changed between the
    pre-copy scan and the post-copy verification."""

    files: dict[str, dict] = field(default_factory=dict)  # relpath → {size, mtime}

    def add(self, root: Path, path: Path) -> None:
        rel = path.relative_to(root).as_posix()
        st = path.stat()
        self.files[rel] = {"size": st.st_size, "mtime": st.st_mtime_ns}

    def equals(self, other: "ContribManifest") -> bool:
        return self.files == other.files


def snapshot_contrib_root(root: Path) -> ContribManifest:
    """Build a manifest of every file under `root` except anything under
    `_incoming/`. Used to verify nothing existing changed after the
    pull writes new files."""
    mf = ContribManifest()
    if not root.exists():
        return mf
    for dirpath, dirnames, filenames in os.walk(root):
        dp = Path(dirpath)
        try:
            rel = dp.relative_to(root)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] == "_incoming":
            dirnames[:] = []  # don't descend
            continue
        # In _incoming's parent, prune _incoming so os.walk skips it.
        if "_incoming" in dirnames:
            dirnames.remove("_incoming")
        for name in filenames:
            mf.add(root, dp / name)
    return mf


def _uploader_folder(uploader_email: str | None, uploader_display: str | None) -> str:
    src = uploader_email or uploader_display or "unknown"
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in src).strip("_")
    return safe or "unknown"


def pull_contributions(
    client: WebSyncClient,
    contrib_root: Path,
    *,
    progress: Callable[[PullProgress], None] | None = None,
) -> tuple[int, int]:
    """Pull every approved contribution not yet pulled. Returns
    (contributions_count, files_count). Raises on any manifest failure."""
    data = client.pull_contributions()
    items = data.get("items", [])
    if not items:
        return 0, 0

    contrib_root.mkdir(parents=True, exist_ok=True)
    incoming_root = contrib_root / "_incoming"
    incoming_root.mkdir(parents=True, exist_ok=True)

    # Snapshot BEFORE any writes.
    pre = snapshot_contrib_root(contrib_root)

    file_count = 0
    for i, c in enumerate(items):
        cid = c["id"]
        staged = incoming_root / str(cid)
        staged.mkdir(parents=True, exist_ok=True)
        # Copy files.
        for f in c.get("files", []):
            fid = f["id"]
            ext = _ext_for_mime(f.get("mime")) or "bin"
            dest = staged / f"{fid}.{ext}"
            client.pull_contribution_file(cid, fid, dest)
            file_count += 1
            if progress:
                progress(PullProgress(stage="contributions", done=file_count,
                                       total=sum(len(x["files"]) for x in items),
                                       detail=f"cid={cid} fid={fid}"))
        # Write per-contribution manifest for the desktop ingest later.
        (staged / "manifest.json").write_text(json.dumps({
            "contribution_id": cid,
            "uploader_email": c.get("user_email"),
            "uploader_display_name": c.get("user_display_name"),
            "note": c.get("note"),
            "group_ids": c.get("group_ids", []),
            "files": c.get("files", []),
            "pulled_at": datetime.utcnow().isoformat(),
        }, indent=2, default=str), "utf-8")

        # Verify nothing outside _incoming/ changed.
        post = snapshot_contrib_root(contrib_root)
        if not pre.equals(post):
            raise RuntimeError(
                "Contrib root append-only invariant violated: files outside "
                "_incoming/ changed between pre-copy snapshot and post-copy check."
            )

        # Rename staged/<cid> → <root>/<uploader>/<cid>
        target = contrib_root / _uploader_folder(c.get("user_email"), c.get("user_display_name")) / str(cid)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            # A retry of an already-pulled contribution — allow it if the
            # existing folder is the same set of files.
            for name in os.listdir(staged):
                dst = target / name
                if not dst.exists():
                    shutil.move(str(staged / name), str(dst))
                else:
                    (staged / name).unlink()
            staged.rmdir()
        else:
            shutil.move(str(staged), str(target))

        # Tell the web the contribution is pulled.
        client.mark_contribution_pulled(cid)

    return len(items), file_count


def _ext_for_mime(mime: str | None) -> str | None:
    m = (mime or "").lower()
    return {
        "image/jpeg": "jpg", "image/png": "png",
        "image/tiff": "tif", "image/heic": "heic", "image/webp": "webp",
        "video/mp4": "mp4", "video/quicktime": "mov",
        "video/x-msvideo": "avi", "video/webm": "webm",
    }.get(m)
