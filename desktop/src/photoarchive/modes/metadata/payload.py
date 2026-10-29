"""What goes into a working copy: the canonical payload (Phase 13).

Pure: no Qt, no database, no I/O. `repo.load_facts` reads the facts,
`compose` turns them into one canonical, JSON-serialisable dict, and
`payload_hash` is what the selector compares with `photos.metadata_hash`.
The exiftool arguments are derived from the payload (`exiftool.write_args`),
never from the facts directly, so the hash covers everything that is written.

The five non-negotiables this module is responsible for:

* **Facts only.** The facts arrive pre-filtered (confirmed dates, assigned
  faces, live rows); nothing here can see a pending suggestion.
* **Never write a guess into a date field.** `exact` → full date in XMP and
  IPTC; `month` / `year` → partial XMP dates plus a description line;
  `decade` → description line only; `unknown` → nothing. EXIF
  `DateTimeOriginal` needs a time of day, and is offered only when the DB
  holds one for that same day (`exif_taken_at`) — no fabricated midnight.
* The description is composed in a fixed order and kept under
  `DESCRIPTION_MAX` characters, the AI text truncated first.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence

# Bump when the payload's shape or wording changes: every photo's hash then
# differs and the next run rewrites the archive.
PAYLOAD_VERSION = 1

DESCRIPTION_MAX = 2000
AI_PREFIX = "AI-generated description: "
BACK_PREFIX = "Back of print: "
BACK_UNVERIFIED_PREFIX = "Back of print (unverified transcription): "
ELLIPSIS = "…"

# Variant kinds that are names a person really goes by. `misspelling` is a
# search aid and stays out of the file.
WRITTEN_VARIANT_KINDS = ("nickname", "alternate_spelling")

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")

# A two-part place name ("Toronto, Canada") is only split when the last part
# is recognisably a country; otherwise "Springfield, Ohio" would make Ohio a
# country. Three or more parts read as city, …, state, country.
_COUNTRIES = frozenset(s.lower() for s in (
    "USA", "U.S.A.", "US", "United States", "United States of America",
    "Canada", "England", "Scotland", "Wales", "Ireland", "Northern Ireland",
    "UK", "United Kingdom", "Great Britain", "Germany", "France", "Italy",
    "Spain", "Portugal", "Netherlands", "Holland", "Belgium", "Switzerland",
    "Austria", "Denmark", "Norway", "Sweden", "Finland", "Iceland", "Poland",
    "Greece", "Mexico", "Australia", "New Zealand", "Japan", "China",
    "India", "South Africa", "Israel", "Egypt", "Brazil", "Argentina",
))


# --------------------------------------------------------------------------
# Facts (what repo.load_facts hands over)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PersonFacts:
    id: int
    given_name: str | None = None
    middle_name: str | None = None
    surname: str | None = None
    maiden_name: str | None = None
    nickname: str | None = None
    suffix: str | None = None
    display_name: str | None = None
    # Live variants of the written kinds only, in a stable order.
    variants: tuple[str, ...] = ()


@dataclass(frozen=True)
class FaceFacts:
    id: int
    person_id: int
    bbox: dict[str, float]  # display frame, pixels: x, y, w, h


@dataclass(frozen=True)
class PlaceFacts:
    id: int
    name: str
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True)
class BackFacts:
    id: int
    text: str
    confirmed: bool


@dataclass
class PhotoFacts:
    photo_id: int
    master_sha256: str
    width: int | None = None          # display frame (DB)
    height: int | None = None
    is_scan: bool = False
    scan_batch: str | None = None
    scan_sequence: int | None = None
    physical_ref_note: str | None = None
    source_folder: str | None = None
    source_filename: str | None = None
    capture_date: date | None = None
    capture_date_precision: str | None = None
    capture_date_confirmed: bool = False
    # A person set the date (a `photo.capture_date.set` audit row). On a scan
    # nothing else counts: ingest's EXIF date is the scanner's clock (Q9).
    date_set_by_human: bool = False
    # 'YYYY:MM:DD HH:MM:SS', the camera's own local time, when known.
    exif_taken_at: str | None = None
    has_exif_gps: bool = False
    description_ai: str | None = None
    split_index: int | None = None    # 1-based "print N of M" for a split child
    split_total: int | None = None
    albums: tuple[str, ...] = ()
    people: dict[int, PersonFacts] = field(default_factory=dict)
    faces: tuple[FaceFacts, ...] = ()
    places: tuple[PlaceFacts, ...] = ()
    backs: tuple[BackFacts, ...] = ()


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------

def _clean(s: str | None) -> str:
    return " ".join((s or "").split())


def _join(*parts: str | None) -> str:
    return " ".join(p for p in (_clean(x) for x in parts) if p)


def keyword_name(p: PersonFacts) -> str:
    """The common name: nickname-or-given + surname + suffix.

    `Peggy Clay`, `George Clay Jr.`. Falls back to `display_name` when the
    parts yield nothing.
    """
    first = _clean(p.nickname) or _clean(p.given_name)
    name = _join(first, p.surname, p.suffix)
    return name or _clean(p.display_name) or f"Person {p.id}"


def formal_name(p: PersonFacts) -> str:
    """The formal full name: given middle surname suffix (née maiden)."""
    name = _join(p.given_name, p.middle_name, p.surname, p.suffix)
    maiden = _clean(p.maiden_name)
    if not name:
        return _clean(p.display_name) or keyword_name(p)
    if maiden and maiden.lower() != _clean(p.surname).lower():
        name += f" (née {maiden})"
    return name


def full_name_line(p: PersonFacts) -> str:
    """Every known name, for the description:
    `Margaret Jane 'Peggy' Clay (née Schmidt; also Maggie, Meg)`."""
    nick = _clean(p.nickname)
    base = _join(p.given_name, p.middle_name)
    if nick:
        base = _join(base, f"'{nick}'") if base else nick
    base = _join(base, p.surname, p.suffix)
    if not base:
        base = _clean(p.display_name) or keyword_name(p)

    seen = {x.lower() for x in (_clean(p.given_name), nick, _clean(p.surname),
                                _clean(p.middle_name)) if x}
    seen.add(base.lower())
    also: list[str] = []
    for v in p.variants:
        v = _clean(v)
        if v and v.lower() not in seen:
            seen.add(v.lower())
            also.append(v)

    bracket: list[str] = []
    maiden = _clean(p.maiden_name)
    if maiden and maiden.lower() != _clean(p.surname).lower():
        bracket.append(f"née {maiden}")
    if also:
        bracket.append("also " + ", ".join(also))
    return f"{base} ({'; '.join(bracket)})" if bracket else base


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------

def date_fields(f: PhotoFacts) -> dict[str, Any] | None:
    """The date part of the payload, or None when nothing may be written."""
    if not f.capture_date_confirmed or f.capture_date is None:
        return None
    if f.is_scan and not f.date_set_by_human:
        # Answer 9: a scan's EXIF date is when it was scanned, not taken.
        return None
    prec = (f.capture_date_precision or "unknown").lower()
    d = f.capture_date
    if prec in ("exact", "day"):
        out: dict[str, Any] = {"precision": "exact", "xmp": d.isoformat(),
                               "iptc": d.strftime("%Y:%m:%d"),
                               "note": None, "exif_datetime": None}
        # A time of day only when the DB holds one for this very day.
        if f.exif_taken_at and f.exif_taken_at[:10] == d.strftime("%Y:%m:%d"):
            out["xmp"] = f"{d.isoformat()}T{f.exif_taken_at[11:19]}"
            # Never into a scan's EXIF: the only time a scan carries is the
            # scanner's.
            if not f.is_scan:
                out["exif_datetime"] = f.exif_taken_at
        return out
    if prec == "month":
        return {"precision": "month", "xmp": f"{d.year:04d}-{d.month:02d}",
                "iptc": None, "exif_datetime": None,
                "note": f"Date: {_MONTHS[d.month - 1]} {d.year}"}
    if prec == "year":
        return {"precision": "year", "xmp": f"{d.year:04d}", "iptc": None,
                "exif_datetime": None, "note": f"Date: about {d.year}"}
    if prec == "decade":
        return {"precision": "decade", "xmp": None, "iptc": None,
                "exif_datetime": None, "note": f"Date: {d.year // 10 * 10}s"}
    return None  # unknown: absence is the statement


# --------------------------------------------------------------------------
# Places
# --------------------------------------------------------------------------

def parse_place(name: str) -> dict[str, str | None]:
    parts = [p.strip() for p in name.split(",") if p.strip()]
    city = state = country = None
    if len(parts) >= 3:
        city, state, country = parts[0], parts[-2], parts[-1]
    elif len(parts) == 2 and parts[1].lower() in _COUNTRIES:
        city, country = parts
    return {"city": city, "state": state, "country": country}


def _round(v: float | None, nd: int = 6) -> float | None:
    return None if v is None else round(float(v), nd)


# --------------------------------------------------------------------------
# Description
# --------------------------------------------------------------------------

def _truncate(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(ELLIPSIS))].rstrip() + ELLIPSIS


def compose_description(
    *, albums: Sequence[str], people_lines: Sequence[str],
    date_note: str | None, ai_text: str | None,
    backs: Sequence[tuple[str, bool]], limit: int = DESCRIPTION_MAX,
) -> str:
    """Fixed order: albums, people, date note, AI description, back text.

    Over `limit`: the AI text is cut first, then the back text, then the
    whole thing (which only a pathological album list could need).
    """
    head: list[str] = []
    if albums:
        head.append("Albums: " + "; ".join(albums))
    if people_lines:
        head.append("People: " + ", ".join(people_lines))
    if date_note:
        head.append(date_note)

    ai_line = (AI_PREFIX + _clean(ai_text)) if _clean(ai_text) else None
    back_lines = [(BACK_PREFIX if confirmed else BACK_UNVERIFIED_PREFIX)
                  + " ".join(text.split())
                  for text, confirmed in backs if text and text.strip()]

    def build(ai: str | None, bl: list[str]) -> str:
        return "\n".join(head + ([ai] if ai else []) + bl)

    text = build(ai_line, back_lines)
    if len(text) <= limit:
        return text

    if ai_line:
        fixed = len(build(None, back_lines)) + 1  # +1 for the newline
        room = limit - fixed
        ai_line = _truncate(ai_line, room) if room > len(AI_PREFIX) + 1 else None
        text = build(ai_line, back_lines)
        if len(text) <= limit:
            return text

    if back_lines:
        room = limit - len(build(ai_line, [])) - 1
        joined = "\n".join(back_lines)
        cut = _truncate(joined, room)
        back_lines = [cut] if cut else []
        text = build(ai_line, back_lines)
    return _truncate(text, limit)


# --------------------------------------------------------------------------
# The payload
# --------------------------------------------------------------------------

def scan_locator(f: PhotoFacts) -> str | None:
    """`Batch 00012 #017`, plus `, print 2 of 3` for a split child."""
    if not f.scan_batch and f.scan_sequence is None:
        return None
    s = _clean(f.scan_batch)
    if f.scan_sequence is not None:
        s = _join(s, f"#{int(f.scan_sequence):03d}")
    if f.split_index:
        s += f", print {f.split_index}" + (f" of {f.split_total}" if f.split_total else "")
    return s or None


def _ordered_people(f: PhotoFacts) -> list[PersonFacts]:
    """Left to right by the leftmost box of each person, then by id —
    the way a family caption reads, and stable for the hash."""
    first_x: dict[int, float] = {}
    for face in f.faces:
        x = float(face.bbox.get("x", 0.0))
        if face.person_id in f.people:
            first_x[face.person_id] = min(x, first_x.get(face.person_id, x))
    return [f.people[pid] for pid in
            sorted(first_x, key=lambda pid: (first_x[pid], pid))]


def _regions(f: PhotoFacts) -> list[dict[str, Any]]:
    """Named face boxes, normalised to the DB's display frame. Mapping to
    the stored pixels happens at write time, from the file's own dims and
    orientation (`regions.to_stored`)."""
    if not f.width or not f.height:
        return []
    out = []
    for face in sorted(f.faces, key=lambda fc: fc.id):
        p = f.people.get(face.person_id)
        if p is None:
            continue
        b = face.bbox
        out.append({
            "face_id": face.id,
            "name": keyword_name(p),
            "x": _round(float(b["x"]) / f.width),
            "y": _round(float(b["y"]) / f.height),
            "w": _round(float(b["w"]) / f.width),
            "h": _round(float(b["h"]) / f.height),
        })
    return out


def compose(f: PhotoFacts) -> dict[str, Any]:
    people = _ordered_people(f)
    # One entry per person, never de-duplicated by text: a father and son
    # can share every name, and merging them drops a person from the file.
    keywords = [keyword_name(p) for p in people]
    persons = [formal_name(p) for p in people]

    dates = date_fields(f)
    places = []
    for pl in sorted(f.places, key=lambda x: x.id):
        parsed = parse_place(pl.name)
        places.append({"name": _clean(pl.name), **parsed,
                       "lat": _round(pl.latitude), "lon": _round(pl.longitude)})
    gps = next(({"lat": p["lat"], "lon": p["lon"]} for p in places
                if p["lat"] is not None and p["lon"] is not None), None)

    description = compose_description(
        albums=sorted({_clean(a) for a in f.albums if _clean(a)}),
        people_lines=[full_name_line(p) for p in people],
        date_note=dates["note"] if dates else None,
        ai_text=f.description_ai,
        backs=[(b.text, b.confirmed) for b in sorted(f.backs, key=lambda b: b.id)],
    )

    return {
        "v": PAYLOAD_VERSION,
        "photo_id": f.photo_id,
        "master_sha256": f.master_sha256,
        "date": dates,
        "keywords": keywords,
        "persons_in_image": persons,
        "regions": _regions(f),
        "locations": places,
        # Camera GPS is never touched; ours goes in only where there is none.
        "gps": gps if not f.has_exif_gps else None,
        "description": description or None,
        "scan_locator": scan_locator(f),
        "physical_ref": _clean(f.physical_ref_note) or None,
        "source_folder": f.source_folder or None,
        "source_filename": f.source_filename or None,
    }


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def summary(payload: dict[str, Any]) -> dict[str, Any]:
    """What the `metadata.write` audit row carries: enough to see what went
    in, not a second copy of the archive."""
    d = payload.get("date") or {}
    return {
        "hash": payload_hash(payload),
        "date": d.get("xmp") or d.get("note"),
        "people": payload.get("keywords") or [],
        "regions": len(payload.get("regions") or []),
        "places": [p["name"] for p in payload.get("locations") or []],
        "description_chars": len(payload.get("description") or ""),
        "scan_locator": payload.get("scan_locator"),
    }
