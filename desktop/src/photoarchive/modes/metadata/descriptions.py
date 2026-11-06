"""Bulk-accept the AI descriptions (Phase 13 fix-up 2).

George's decision (PROJECT-PLAN §5 item 19): every AI description goes into
the files, and corrections happen on the site afterwards. So each pending
`description` suggestion with `source='ai'` on a kept, live photo is promoted
to the fact column `photos.description_ai`, and the folder-name `import`
descriptions are resolved `rejected` — the import album and the
`XMP-photoarchive:SourceFolder` field already carry the event name in the file.

Shape (the corrections shape): one `photo.description.set` audit row per photo
(previous and new), one `suggestion.reject` per rejected suggestion, all
carrying one `batch_id`, and a `description.bulk_accept.batch` summary row.
Work commits in chunks so a killed run keeps what it finished; selection is
"still pending", so a re-run continues and a finished run finds 0 due.

Two things are deliberately left pending rather than guessed at:
* a photo whose `description_ai` is already set to something else — a person
  (on the web, or a pull) wrote it, and the AI text must not overwrite them;
* a suggestion with no text.
Neither touches the files. The metadata writer needs no flag: the payload hash
changes with `description_ai`, so every touched photo is due by construction.
"""
from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Iterator

import psycopg

from ... import db as dbmod

IMPORT_NOTE = "event name already carried by the import album and SourceFolder XMP"
SUPERSEDED_NOTE = "superseded by a newer AI description of the same photo"
ACCEPT_NOTE = "bulk accept of AI descriptions (Phase 13 fix-up 2)"
DEFAULT_CHUNK = 500

# Kept, live photos only: the writer's own scope. AI text on a junk photo
# stays pending — if the photo is ever restored, the suggestion is still there.
_IN_SCOPE = "p.is_deleted = false and p.triage_status in ('keep', 'private')"


@dataclass(frozen=True)
class Accept:
    suggestion_id: int
    photo_id: int
    text: str


@dataclass
class Plan:
    accept: list[Accept] = field(default_factory=list)
    superseded: list[int] = field(default_factory=list)       # older ai rows, same photo
    imports: list[int] = field(default_factory=list)          # source='import'
    conflict: list[int] = field(default_factory=list)         # photo already has other text
    empty: list[int] = field(default_factory=list)            # no text in the payload
    out_of_scope: int = 0                                     # ai rows on junk / deleted photos

    @property
    def due(self) -> int:
        return len(self.accept) + len(self.superseded) + len(self.imports)


@dataclass
class Result:
    batch_id: str
    counts: Counter = field(default_factory=Counter)
    photo_ids: list[int] = field(default_factory=list)


def _text(payload) -> str:
    p = payload if isinstance(payload, dict) else json.loads(payload or "{}")
    return " ".join(str(p.get("text") or "").split())


def plan(conn: psycopg.Connection) -> Plan:
    out = Plan()
    rows = conn.execute(f"""
        select s.id, s.photo_id, s.payload, p.description_ai,
               row_number() over (partition by s.photo_id
                                  order by s.created_at desc, s.id desc) as rn
          from suggestions s
          join photos p on p.id = s.photo_id
         where s.kind = 'description' and s.source = 'ai' and s.status = 'pending'
           and {_IN_SCOPE}
         order by s.photo_id, rn
    """).fetchall()
    for sid, pid, payload, current, rn in rows:
        if rn > 1:
            out.superseded.append(sid)
            continue
        text = _text(payload)
        if not text:
            out.empty.append(sid)
        elif current and current.strip() and current.strip() != text:
            out.conflict.append(sid)
        else:
            out.accept.append(Accept(sid, pid, text))
    out.imports = [r[0] for r in conn.execute("""
        select id from suggestions
         where kind = 'description' and source = 'import' and status = 'pending'
         order by id
    """).fetchall()]
    out.out_of_scope = conn.execute(f"""
        select count(*) from suggestions s join photos p on p.id = s.photo_id
         where s.kind = 'description' and s.source = 'ai' and s.status = 'pending'
           and not ({_IN_SCOPE})
    """).fetchone()[0]
    return out


def _chunks(items: list, n: int) -> Iterator[list]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _accept_chunk(conn: psycopg.Connection, items: list[Accept], batch_id: str,
                  res: Result) -> None:
    for a in items:
        # Re-read under lock: anything that changed since the plan is skipped
        # (and stays pending), never overwritten.
        row = conn.execute("""
            select s.status::text, p.description_ai
              from suggestions s join photos p on p.id = s.photo_id
             where s.id = %s for update of s, p
        """, (a.suggestion_id,)).fetchone()
        if row is None or row[0] != "pending":
            res.counts["skipped_changed"] += 1
            continue
        current = row[1]
        if current and current.strip() and current.strip() != a.text:
            res.counts["conflict"] += 1
            continue
        conn.execute("update photos set description_ai = %s where id = %s",
                     (a.text, a.photo_id))
        conn.execute("""
            update suggestions
               set status = 'accepted', resolved_at = now(), resolution_note = %s
             where id = %s
        """, (ACCEPT_NOTE, a.suggestion_id))
        dbmod.audit(conn, actor="desktop", action="photo.description.set",
                    entity_type="photo", entity_id=a.photo_id,
                    previous_value={"description_ai": current},
                    new_value={"description_ai": a.text, "via": "description.bulk_accept",
                               "suggestion_id": a.suggestion_id, "batch_id": batch_id})
        res.counts["accepted"] += 1
        res.photo_ids.append(a.photo_id)


def _reject_chunk(conn: psycopg.Connection, ids: list[int], note: str, kind: str,
                  batch_id: str, res: Result) -> None:
    for sid in ids:
        cur = conn.execute("""
            update suggestions
               set status = 'rejected', resolved_at = now(), resolution_note = %s
             where id = %s and status = 'pending'
            returning photo_id
        """, (note, sid))
        hit = cur.fetchone()
        if hit is None:
            res.counts["skipped_changed"] += 1
            continue
        dbmod.audit(conn, actor="desktop", action="suggestion.reject",
                    entity_type="suggestion", entity_id=sid,
                    previous_value={"status": "pending"},
                    new_value={"status": "rejected", "resolution_note": note,
                               "photo_id": hit[0], "via": "description.bulk_accept",
                               "batch_id": batch_id})
        res.counts[kind] += 1


def apply(
    conn: psycopg.Connection, p: Plan, *, chunk: int = DEFAULT_CHUNK,
    progress: Callable[[str], None] | None = None,
) -> Result:
    """Apply `p`, committing every `chunk` rows. `conn` must not be in
    autocommit; each chunk is its own transaction."""
    res = Result(batch_id=uuid.uuid4().hex)
    steps = [("accept", c) for c in _chunks(p.accept, chunk)]
    steps += [("superseded", c) for c in _chunks(p.superseded, chunk)]
    steps += [("import_rejected", c) for c in _chunks(p.imports, chunk)]
    for i, (kind, items) in enumerate(steps, start=1):
        try:
            if kind == "accept":
                _accept_chunk(conn, items, res.batch_id, res)
            elif kind == "superseded":
                _reject_chunk(conn, items, SUPERSEDED_NOTE, kind, res.batch_id, res)
            else:
                _reject_chunk(conn, items, IMPORT_NOTE, kind, res.batch_id, res)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        if progress:
            progress(f"chunk {i}/{len(steps)} ({kind}, {len(items)}): "
                     + ", ".join(f"{k}={v}" for k, v in sorted(res.counts.items())))
    dbmod.audit(conn, actor="desktop", action="description.bulk_accept.batch",
                entity_type="photo", entity_id=None,
                new_value={"batch_id": res.batch_id, "counts": dict(res.counts),
                           "left_pending": {"conflict": len(p.conflict), "empty": len(p.empty),
                                            "out_of_scope": p.out_of_scope}})
    conn.commit()
    return res
