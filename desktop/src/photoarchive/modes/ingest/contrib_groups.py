"""Groups for photos ingested from a `contrib` root.

An uploader picks the groups a batch is for, an admin or moderator
approves each file for some of them, and the pull writes that decision
into the batch's `manifest.json` (`files[].approved_group_ids`). The web
cannot file the photo itself — it has no `photos` row until the desktop
ingests one — so this is where the decision becomes `photo_groups`
rows. Without it the first real contribution (2026-11-04) arrived
unfiled: admin-only on the site, and with no file pushed, since the push
only sends bytes for grouped photos.

A photo's file inside `<root>/<uploader>/<cid>/` is `<file id>.<ext>`,
named by the pull, so the stem finds its manifest entry.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path, PurePosixPath

from ... import db

log = logging.getLogger(__name__)


def approved_groups(root_path: Path, source_folder: str, source_filename: str) -> tuple[int | None, list[int]]:
    """(contribution_id, approved group ids) for one ingested file, or
    (None, []) when there is no readable manifest or no entry for it."""
    manifest = root_path.joinpath(*PurePosixPath(source_folder).parts) / "manifest.json"
    try:
        data = json.loads(manifest.read_text("utf-8"))
    except (OSError, ValueError) as e:
        log.warning("contrib manifest unreadable at %s: %s", manifest, e)
        return None, []
    stem = PurePosixPath(source_filename).stem
    entry = next((f for f in data.get("files", []) if str(f.get("id")) == stem), None)
    if entry is None:
        log.warning("contrib manifest %s has no file %s", manifest, source_filename)
        return data.get("contribution_id"), []
    ids = entry.get("approved_group_ids")
    if ids is None:  # a manifest written before per-file approval existed
        ids = data.get("group_ids", [])
    return data.get("contribution_id"), sorted({int(g) for g in ids})


def apply_groups(conn, photo_id: int, group_ids: list[int], *, contribution_id: int | None,
                 actor: str = "desktop") -> list[int]:
    """Add `photo_id` to every live local group in `group_ids`. Returns the
    groups added. A group the laptop doesn't hold (not pulled yet, or
    removed) is skipped and logged, never created."""
    if not group_ids:
        return []
    live = [r[0] for r in conn.execute(
        "select id from groups where id = any(%s) and not is_deleted order by id",
        (group_ids,)).fetchall()]
    missing = sorted(set(group_ids) - set(live))
    if missing:
        log.warning("photo %s: contribution %s names groups %s this laptop does not hold",
                    photo_id, contribution_id, missing)
    for gid in live:
        conn.execute(
            """
            insert into photo_groups (photo_id, group_id, is_deleted)
            values (%s, %s, false)
            on conflict (photo_id, group_id) do update
                 set is_deleted = false, deleted_at = null, deleted_by = null,
                     updated_at = now()
            """,
            (photo_id, gid),
        )
    if live:
        db.audit(conn, actor=actor, action="photo_group.contribution",
                 entity_type="photo", entity_id=photo_id,
                 new_value={"contribution_id": contribution_id, "group_ids": live,
                            "skipped_group_ids": missing})
    return live
