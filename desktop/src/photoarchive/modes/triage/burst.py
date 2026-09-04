"""Burst-shot grouping. 3+ photos within 2 s (EXIF DateTimeOriginal) whose
pHash Hamming distances are all ≤ 4 are a burst. The sharpest by Laplacian
variance is the keeper; the rest are `burst` extras.

Callers pass a list of PhotoRow (id, taken_at, phash) already restricted to
one source_root/folder — grouping across folders would false-positive on
unrelated bursts that happen to share a second.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Sequence

from ..ingest.hasher import hamming


@dataclass(frozen=True)
class PhotoTime:
    photo_id: int
    taken_at: datetime | None
    phash: str | None


@dataclass(frozen=True)
class BurstExtra:
    """One photo that is a non-keeper in a burst group."""
    photo_id: int
    sharpest_photo_id: int
    group_size: int
    time_span_s: float
    peers: list[int]  # all photo_ids in the group including the keeper


def _pairwise_close(hashes: list[str | None], max_distance: int) -> bool:
    """Every pair of non-null pHashes within max_distance."""
    valid = [h for h in hashes if h]
    if len(valid) < 2:
        return False
    for i in range(len(valid)):
        for j in range(i + 1, len(valid)):
            try:
                if hamming(valid[i], valid[j]) > max_distance:
                    return False
            except ValueError:
                return False
    return True


def group_bursts(
    photos: Sequence[PhotoTime],
    *,
    window: timedelta = timedelta(seconds=2),
    min_group: int = 3,
    max_phash_distance: int = 4,
) -> list[list[PhotoTime]]:
    """Return groups of 3+ photos taken within `window` whose pHashes are
    all pairwise close. Input is not required to be sorted; we sort by
    (taken_at, photo_id) here."""
    have_time = [p for p in photos if p.taken_at is not None]
    have_time.sort(key=lambda p: (p.taken_at, p.photo_id))  # type: ignore[arg-type]

    groups: list[list[PhotoTime]] = []
    i = 0
    while i < len(have_time):
        j = i + 1
        while (j < len(have_time)
               and have_time[j].taken_at - have_time[i].taken_at <= window):  # type: ignore[operator]
            j += 1
        cluster = have_time[i:j]
        if len(cluster) >= min_group and _pairwise_close(
            [p.phash for p in cluster], max_phash_distance,
        ):
            groups.append(cluster)
            i = j
        else:
            i += 1
    return groups


def burst_extras(
    groups: Iterable[list[PhotoTime]],
    sharpness: dict[int, float],
) -> list[BurstExtra]:
    """For each group, choose the sharpest as keeper (photo_id-tiebreak) and
    return one BurstExtra per non-keeper.
    `sharpness` maps photo_id → Laplacian-variance sharpness; photos missing
    from the dict get 0 and lose the tiebreak."""
    out: list[BurstExtra] = []
    for grp in groups:
        # Highest sharpness wins; on tie, lowest photo_id wins (stable).
        keeper = max(
            grp,
            key=lambda p: (sharpness.get(p.photo_id, 0.0), -p.photo_id),
        )
        peers = [p.photo_id for p in grp]
        span = (grp[-1].taken_at - grp[0].taken_at).total_seconds()  # type: ignore[operator]
        for p in grp:
            if p.photo_id == keeper.photo_id:
                continue
            out.append(BurstExtra(
                photo_id=p.photo_id,
                sharpest_photo_id=keeper.photo_id,
                group_size=len(grp),
                time_span_s=round(span, 3),
                peers=peers,
            ))
    return out
