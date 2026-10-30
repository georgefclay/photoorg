"""Phase 12 — where a photo goes inside the export (and a download zip).

The Python port of ``web/services/zip-tree.js``. Pure: no database, no I/O.
Both ports are tested against the same fixtures,
``shared/zip-tree-fixtures.json``, so the desktop's full export and the
web's download zip cannot build different trees.

The rules:

* a date counts only when ``capture_date_confirmed`` — a pending suggestion
  is not a date;
* ``exact`` / ``month`` → ``YYYY/MM/``; ``year`` → ``YYYY/`` (top of the year
  folder — never invent a January); ``decade`` → ``1960s/``; anything else →
  ``Unsorted/``;
* file names ``{YYYY-MM-DD|YYYY-MM|YYYY|1960s|undated}_{id:08d}.{ext}``;
* ``People/<common name>/<same file name>`` for the optional parallel tree.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Iterable, Mapping

UNSORTED = "Unsorted"
MAX_FOLDER = 80


@dataclass(frozen=True)
class Placement:
    dir: str
    stem: str


def norm_ext(ext: str | None) -> str:
    e = (ext or "").strip().lower().lstrip(".")
    if e == "jpeg":
        e = "jpg"
    if e == "tiff":
        e = "tif"
    return e or "jpg"


def _date_string(v: object) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.strftime("%Y-%m-%d")
    return str(v)


_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def placement(photo: Mapping) -> Placement:
    """``photo`` = {id, capture_date, precision, confirmed}."""
    d = _date_string(photo.get("capture_date"))
    m = _DATE_RE.match(d) if (photo.get("confirmed") and d) else None
    if m:
        y, mo, day = m.groups()
        prec = photo.get("precision")
        if prec == "exact":
            return Placement(f"{y}/{mo}", f"{y}-{mo}-{day}")
        if prec == "month":
            return Placement(f"{y}/{mo}", f"{y}-{mo}")
        if prec == "year":
            return Placement(y, y)
        if prec == "decade":
            dec = f"{int(y) // 10 * 10}s"
            return Placement(dec, dec)
    return Placement(UNSORTED, "undated")


def file_name(photo: Mapping) -> str:
    return f"{placement(photo).stem}_{int(photo['id']):08d}.{norm_ext(photo.get('ext'))}"


def photo_path(photo: Mapping) -> str:
    """Path inside the tree, forward slashes."""
    return f"{placement(photo).dir}/{file_name(photo)}"


def people_path(photo: Mapping, folder: str) -> str:
    return f"People/{folder}/{file_name(photo)}"


# ---- People folder names ---------------------------------------------------

def _clean(s: object) -> str:
    return " ".join(str(s if s is not None else "").split())


def _join(*parts: object) -> str:
    return " ".join(p for p in (_clean(x) for x in parts) if p)


def common_name(p: Mapping) -> str:
    """The Phase 13 keyword form: nickname-or-given + surname + suffix."""
    first = _clean(p.get("nickname")) or _clean(p.get("given_name"))
    return (_join(first, p.get("surname"), p.get("suffix"))
            or _clean(p.get("display_name")) or f"Person {p['id']}")


_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.IGNORECASE)


def sanitize_folder(name: str) -> str:
    """A folder name every desktop OS will accept."""
    s = _BAD.sub("_", _clean(name))[:MAX_FOLDER].rstrip(" .")
    if _RESERVED.match(s):
        s += "_"
    return s


def people_folders(people: Iterable[Mapping]) -> dict[int, str]:
    """id → folder name, unique case-insensitively. A collision gets
    `` (b. YYYY)``; anything still colliding gets `` #id``."""
    people = list(people)
    by_id = {int(p["id"]): p for p in people}
    base = {pid: (sanitize_folder(common_name(p)) or f"Person {pid}") for pid, p in by_id.items()}

    def groups(m: dict[int, str]) -> list[list[int]]:
        g: dict[str, list[int]] = {}
        for pid, name in m.items():
            g.setdefault(name.lower(), []).append(pid)
        return [ids for ids in g.values() if len(ids) > 1]

    out = dict(base)
    for ids in groups(base):
        for pid in ids:
            by = by_id[pid].get("birth_year")
            out[pid] = f"{base[pid]} (b. {by})" if by else f"{base[pid]} #{pid}"
    for ids in groups(out):
        for pid in ids:
            out[pid] = f"{base[pid]} #{pid}"
    return out
