"""Pull:
   1. Groups + memberships + photo_groups from the web (last-writer-wins).
   2. Confirmed values (accepted suggestions, fact-set audit rows) into
      the laptop DB, tagged `source='web'` in audit rows for provenance.
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

def pull_confirmed(client: WebSyncClient, state_dir: Path) -> int:
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
