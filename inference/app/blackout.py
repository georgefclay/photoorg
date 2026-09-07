"""Windows when unattended batches stand down.

The mini is George's machine before it is a batch runner. A batch finishes the
item in flight, then sleeps until the window closes. Interactive requests are
never affected — a blackout is about not competing for the machine overnight,
not about refusing work.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

DAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}
_CLAUSE = re.compile(
    r"^(?P<day>[A-Za-z]{3,9})\s+(?P<from>\d{1,2}:\d{2})\s*-\s*(?P<to>\d{1,2}:\d{2})$"
)


@dataclass(frozen=True)
class Window:
    weekday: int  # 0 = Monday
    start: dt.time
    end: dt.time

    @property
    def wraps_midnight(self) -> bool:
        return self.end <= self.start

    def contains(self, moment: dt.datetime) -> bool:
        clock = moment.time()
        if not self.wraps_midnight:
            return moment.weekday() == self.weekday and self.start <= clock < self.end
        # e.g. "Fri 23:00-01:00" runs into Saturday.
        if moment.weekday() == self.weekday and clock >= self.start:
            return True
        return moment.weekday() == (self.weekday + 1) % 7 and clock < self.end

    def ends_after(self, moment: dt.datetime) -> dt.datetime:
        """When this window releases, given a moment inside it."""
        end_today = moment.replace(
            hour=self.end.hour, minute=self.end.minute, second=0, microsecond=0
        )
        if end_today <= moment:
            end_today += dt.timedelta(days=1)
        return end_today


def parse(spec: str) -> list[Window]:
    """Parse "Tue 04:30-07:30;Fri 04:30-07:30". Bad clauses are skipped, not fatal."""
    windows: list[Window] = []
    for clause in (spec or "").split(";"):
        clause = clause.strip()
        if not clause:
            continue
        match = _CLAUSE.match(clause)
        if not match:
            continue
        weekday = DAYS.get(match.group("day")[:3].lower())
        if weekday is None:
            continue
        try:
            start = dt.time.fromisoformat(_pad(match.group("from")))
            end = dt.time.fromisoformat(_pad(match.group("to")))
        except ValueError:
            continue
        windows.append(Window(weekday=weekday, start=start, end=end))
    return windows


def _pad(value: str) -> str:
    hour, _, minute = value.partition(":")
    return f"{int(hour):02d}:{minute}"


def active_window(spec: str, moment: dt.datetime | None = None) -> Window | None:
    moment = moment or dt.datetime.now()
    for window in parse(spec):
        if window.contains(moment):
            return window
    return None


def describe(spec: str) -> list[str]:
    names = {value: key.capitalize() for key, value in DAYS.items()}
    return [
        f"{names[w.weekday]} {w.start.strftime('%H:%M')}-{w.end.strftime('%H:%M')}"
        for w in parse(spec)
    ]
