"""Reading the facts, and recording a write.

Every selector here reads **live rows only** (Phase 15): `album_photos`,
`photo_places`, `person_name_variants` and the tables they point at all
carry `is_deleted`, and a removed album, place or nickname must leave the
file. And **facts only**: a date only when confirmed, a person only through
an assigned face (not disputed, not ignored, the person not deleted), a place
only through a confirmed `photo_places` row, `description_ai` only because it
is the accepted fact. `suggestions` is never read.

Scope (non-negotiable 5): `triage_status in ('keep','private')` and
`is_deleted = false`. A back that became a back is `is_deleted`, and the
`photo_backs` images are not `photos` rows at all — their *text* travels in
the front's description.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import psycopg

from .payload import (
    WRITTEN_VARIANT_KINDS, BackFacts, FaceFacts, PersonFacts, PhotoFacts,
    PlaceFacts, compose, payload_hash,
)

_SPLIT_SUFFIX = re.compile(r"#p(\d+)$")

SCOPE_SQL = "p.triage_status in ('keep','private') and not p.is_deleted"


@dataclass
class Candidate:
    """One in-scope photo with its composed payload and its write state."""
    facts: PhotoFacts
    payload: dict[str, Any]
    hash: str
    file_version: int
    metadata_hash: str | None
    metadata_file_version: int | None
    working_path: str | None
    mime: str | None
    in_group: bool

    @property
    def photo_id(self) -> int:
        return self.facts.photo_id

    @property
    def due(self) -> bool:
        return (self.hash != self.metadata_hash
                or self.metadata_file_version is None
                or int(self.file_version) != int(self.metadata_file_version))

    @property
    def reason(self) -> str:
        if self.metadata_hash is None:
            return "never written"
        if self.hash != self.metadata_hash:
            return "facts changed"
        if self.metadata_file_version is None or \
                int(self.file_version) != int(self.metadata_file_version):
            return "file replaced since the last write"
        return "up to date"


def _rows(conn: psycopg.Connection, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
    return conn.execute(sql, params).fetchall()


def load_candidates(
    conn: psycopg.Connection, *, photo_ids: Sequence[int] | None = None,
    batches: Sequence[str] | None = None,
) -> list[Candidate]:
    """Every in-scope photo (optionally narrowed), facts loaded in bulk."""
    where = [SCOPE_SQL]
    params: list[Any] = []
    if photo_ids:
        where.append("p.id = any(%s)")
        params.append(list(photo_ids))
    if batches:
        where.append("p.scan_batch = any(%s)")
        params.append(list(batches))

    photos = _rows(conn, f"""
        select p.id, p.sha256,
               -- A split child's own sha256 is an identity key, not a file
               -- hash; the scan it was cut from is the file someone will hold.
               coalesce(parent.sha256, pm.sha256, p.sha256), p.width, p.height,
               p.is_scan, p.scan_batch, p.scan_sequence, p.physical_ref_note,
               p.source_folder, p.source_filename, p.capture_date,
               p.capture_date_precision::text, p.capture_date_confirmed,
               to_char(p.exif_taken_at, 'YYYY:MM:DD HH24:MI:SS'),
               p.exif_gps_lat is not null and p.exif_gps_lon is not null,
               p.description_ai, p.parent_photo_id, p.file_version,
               p.metadata_hash, p.metadata_file_version, p.working_path, p.mime,
               exists (select 1 from photo_groups pg
                        where pg.photo_id = p.id and not pg.is_deleted),
               exists (select 1 from audit_log al
                        where al.entity_type = 'photo' and al.entity_id = p.id
                          and al.action = 'photo.capture_date.set')
          from photos p
          left join lateral (
                select sha256 from photo_masters m
                 where m.photo_id = p.id and m.is_preferred
                 order by m.id limit 1) pm on true
          left join photos parent on parent.id = p.parent_photo_id
         where {' and '.join(where)}
         order by p.id
    """, params)
    if not photos:
        return []
    ids = [r[0] for r in photos]

    albums: dict[int, list[str]] = {}
    for pid, name in _rows(conn, """
        select ap.photo_id, a.name
          from album_photos ap join albums a on a.id = ap.album_id
         where ap.photo_id = any(%s) and not ap.is_deleted and not a.is_deleted
    """, (ids,)):
        albums.setdefault(pid, []).append(name)

    faces: dict[int, list[FaceFacts]] = {}
    person_ids: set[int] = set()
    for fid, pid, person_id, bbox in _rows(conn, """
        select f.id, f.photo_id, f.person_id, f.bbox
          from faces f join people pe on pe.id = f.person_id
         where f.photo_id = any(%s)
           and not f.is_deleted and not coalesce(f.is_disputed, false)
           and coalesce(f.review_status, 'pending') <> 'ignore'
           and not pe.is_deleted
         order by f.id
    """, (ids,)):
        if not isinstance(bbox, dict) or not all(k in bbox for k in "xywh"):
            continue
        faces.setdefault(pid, []).append(FaceFacts(fid, person_id, bbox))
        person_ids.add(person_id)

    people: dict[int, PersonFacts] = {}
    if person_ids:
        variants: dict[int, list[str]] = {}
        for person_id, variant in _rows(conn, """
            select person_id, variant from person_name_variants
             where person_id = any(%s) and not is_deleted
               and kind::text = any(%s)
             order by person_id, kind, lower(variant), id
        """, (list(person_ids), list(WRITTEN_VARIANT_KINDS))):
            variants.setdefault(person_id, []).append(variant)
        for row in _rows(conn, """
            select id, given_name, middle_name, surname, maiden_name, nickname,
                   suffix, display_name
              from people where id = any(%s)
        """, (list(person_ids),)):
            people[row[0]] = PersonFacts(
                id=row[0], given_name=row[1], middle_name=row[2], surname=row[3],
                maiden_name=row[4], nickname=row[5], suffix=row[6],
                display_name=row[7], variants=tuple(variants.get(row[0], ())),
            )

    places: dict[int, list[PlaceFacts]] = {}
    for pid, plid, name, lat, lon in _rows(conn, """
        select pp.photo_id, pl.id, pl.name, pl.latitude, pl.longitude
          from photo_places pp join places pl on pl.id = pp.place_id
         where pp.photo_id = any(%s) and pp.confirmed
           and not pp.is_deleted and not pl.is_deleted
    """, (ids,)):
        places.setdefault(pid, []).append(PlaceFacts(plid, name, lat, lon))

    backs: dict[int, list[BackFacts]] = {}
    for bid, pid, text, confirmed in _rows(conn, """
        select id, photo_id, transcribed_text, coalesce(transcription_confirmed, false)
          from photo_backs
         where photo_id = any(%s) and nullif(btrim(transcribed_text), '') is not null
    """, (ids,)):
        backs.setdefault(pid, []).append(BackFacts(bid, text, bool(confirmed)))

    parents = sorted({r[17] for r in photos if r[17] is not None})
    siblings: dict[int, int] = {}
    if parents:
        for parent, n in _rows(conn, """
            select parent_photo_id, count(*) from photos
             where parent_photo_id = any(%s) group by 1
        """, (parents,)):
            siblings[parent] = int(n)

    out: list[Candidate] = []
    for r in photos:
        (pid, _sha, master_sha, w, h, is_scan, batch, seq, ref, folder, fname,
         cdate, prec, conf, taken, has_gps, desc_ai, parent, fv, mhash, mfv,
         wpath, mime, in_group, human_date) = r
        split_index = split_total = None
        if parent is not None and fname:
            m = _SPLIT_SUFFIX.search(fname)
            if m:
                split_index = int(m.group(1))
                split_total = siblings.get(parent)
        pf = faces.get(pid, [])
        facts = PhotoFacts(
            photo_id=pid, master_sha256=master_sha, width=w, height=h,
            is_scan=bool(is_scan), scan_batch=batch, scan_sequence=seq,
            physical_ref_note=ref, source_folder=folder, source_filename=fname,
            capture_date=cdate, capture_date_precision=prec,
            capture_date_confirmed=bool(conf), date_set_by_human=bool(human_date),
            exif_taken_at=taken,
            has_exif_gps=bool(has_gps), description_ai=desc_ai,
            split_index=split_index, split_total=split_total,
            albums=tuple(albums.get(pid, ())),
            people={f.person_id: people[f.person_id] for f in pf if f.person_id in people},
            faces=tuple(pf), places=tuple(places.get(pid, ())),
            backs=tuple(backs.get(pid, ())),
        )
        payload = compose(facts)
        out.append(Candidate(
            facts=facts, payload=payload, hash=payload_hash(payload),
            file_version=int(fv or 1), metadata_hash=mhash,
            metadata_file_version=mfv, working_path=wpath, mime=mime,
            in_group=bool(in_group),
        ))
    return out


def due(candidates: Iterable[Candidate]) -> list[Candidate]:
    return [c for c in candidates if c.due]


def count_due(conn: psycopg.Connection) -> tuple[int, int]:
    """(photos whose file would be rewritten, of those how many are in a live
    group — i.e. whose bytes the next push re-sends straight away)."""
    d = due(load_candidates(conn))
    return len(d), sum(1 for c in d if c.in_group)


def lock_for_write(conn: psycopg.Connection, photo_id: int) -> tuple[int, str | None] | None:
    """Re-read under a row lock. None when the photo has left scope."""
    row = conn.execute(f"""
        select p.file_version, p.working_path from photos p
         where p.id = %s and {SCOPE_SQL}
         for update
    """, (photo_id,)).fetchone()
    return (int(row[0] or 1), row[1]) if row else None


def record_write(
    conn: psycopg.Connection, *, photo_id: int, new_version: int,
    payload_hash: str, file_size: int,
) -> None:
    """The writer's own columns, plus `file_version` (the bytes changed, so
    sync re-pushes). Never the human-edit stamps."""
    conn.execute("""
        update photos
           set file_version = %s, file_size = %s,
               metadata_hash = %s, metadata_written_at = now(),
               metadata_file_version = %s
         where id = %s
    """, (new_version, file_size, payload_hash, new_version, photo_id))
