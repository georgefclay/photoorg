"""Which dates are not facts. Pure: no database, no Qt, no I/O.

Two callers. `flag` reads a photo whose date is already confirmed and was
never set by a person, and names the rules it breaks — the `date_hygiene`
report. `ingest_reason` reads an EXIF date at ingest and says why it must
arrive as a suggestion instead of a fact, or None when it may be confirmed
exactly as before.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from ..ingest.scanner import parse_date_folder

# Niépce's view from the window at Le Gras. Nothing in a family archive is older.
EARLIEST_YEAR = 1826
# A folder month is a hint about when the files were exported; a camera
# date within a season of it agrees.
FOLDER_MAX_MONTHS = 3
# A file copied across time zones can carry a timestamp a day ahead of its
# own mtime without anything being wrong.
MTIME_SLACK = timedelta(days=1)

RULES = ("a", "b", "c", "d")
REASONS = {
    "a": "scanner_clock",
    "b": "unset_camera_clock",
    "c": "folder_disagrees",
    "d": "implausible_date",
}
RULE_TEXT = {
    "a": "a scan: the EXIF date is when it was scanned",
    "b": "1 January before 00:01: a camera clock never set",
    "c": f"more than {FOLDER_MAX_MONTHS} months from its _YYYY-MM folder",
    "d": f"after the photo was ingested, or before {EARLIEST_YEAR}",
}


def parse_rules(text: str) -> list[str]:
    """`"a,b"` → `["a", "b"]`; refuses anything that is not a rule."""
    out: list[str] = []
    for part in (text or "").replace(" ", "").lower().split(","):
        if not part:
            continue
        if part not in RULES:
            raise ValueError(f"unknown rule {part!r}; rules are {', '.join(RULES)}")
        if part not in out:
            out.append(part)
    if not out:
        raise ValueError("no rules given")
    return out


def is_unset_clock(taken_at: datetime) -> bool:
    return taken_at.month == 1 and taken_at.day == 1 and taken_at.time() < time(0, 1)


def months_apart(d: date, year: int, month: int) -> int:
    return abs((d.year * 12 + d.month) - (year * 12 + month))


@dataclass(frozen=True)
class DateRow:
    """One confirmed date that no person set."""
    photo_id: int
    capture_date: date
    precision: str
    exif_taken_at: datetime | None
    is_scan: bool
    source_root: str | None
    source_folder: str | None
    scan_batch: str | None
    ingested_at: datetime
    triage_status: str = "keep"
    is_deleted: bool = False


def flag(row: DateRow, *, scan_roots: frozenset[str] | set[str] = frozenset()) -> list[str]:
    """The rules this ingest-sourced date breaks, in rule order."""
    hits: list[str] = []
    if row.is_scan or (row.source_root in scan_roots):
        hits.append("a")
    if row.exif_taken_at is not None and is_unset_clock(row.exif_taken_at):
        hits.append("b")
    hint = parse_date_folder(row.source_folder or "")
    if hint is not None and months_apart(row.capture_date, *hint) > FOLDER_MAX_MONTHS:
        hits.append("c")
    if row.capture_date > row.ingested_at.date() or row.capture_date.year < EARLIEST_YEAR:
        hits.append("d")
    return hits


def reason_for(hits: list[str], chosen: list[str]) -> tuple[str, str] | None:
    """The first chosen rule this row breaks, as `(rule, reason)`."""
    for r in RULES:
        if r in hits and r in chosen:
            return r, REASONS[r]
    return None


def ingest_reason(
    taken_at: datetime | None, *, is_scan: bool, master_mtime: datetime | None,
) -> str | None:
    """Why ingest must not confirm this EXIF date, or None if it may.

    A scan's EXIF date is the scanner's clock; 1 January before 00:01 is a
    camera that was never set; a date after the file itself existed (a day
    of slack for time zones), or before photography, is not a date at all.
    Every other digital EXIF date stays confirmed exactly as before.
    """
    if taken_at is None:
        return None
    if is_scan:
        return "scanner_clock"
    if is_unset_clock(taken_at):
        return "unset_camera_clock"
    if taken_at.year < EARLIEST_YEAR:
        return "implausible_date"
    if master_mtime is not None and taken_at > master_mtime + MTIME_SLACK:
        return "implausible_date"
    return None
