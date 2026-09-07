"""Keeper pre-selection.

Score each group member; highest total wins, ties broken by lower photo id.
Weights are chosen so a higher-ranked rule can never be overturned by
lower-ranked ones (each weight >= sum of all lower weights).

Rules (highest to lowest):
  1. Has EXIF `DateTimeOriginal` AND `camera` — digital original evidence.
  2. TIFF over JPG.
  3. More pixels.
  4. Larger file size.
  5. Scan over digital, tiebreak when neither has EXIF (a scan carries a
     physical reference; without EXIF the scan is the more durable copy).

The reason chain is displayed in the UI ("keeper: has EXIF > 2x pixels").
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MemberFacts:
    """The subset of `photos` columns keeper scoring needs. Loaded once
    per member when the group is opened.
    """
    photo_id: int
    is_scan: bool
    mime: str
    width: int | None
    height: int | None
    file_size: int | None
    exif_taken_at: object | None  # datetime or None
    exif_camera: str | None

    @property
    def pixels(self) -> int:
        w = self.width or 0
        h = self.height or 0
        return w * h

    @property
    def has_exif_original(self) -> bool:
        return self.exif_taken_at is not None and bool(self.exif_camera)

    @property
    def is_tiff(self) -> bool:
        return (self.mime or "").lower() in ("image/tiff", "image/tif")


# Weights are placeholders — we do NOT sum weights. Ordering is enforced
# by a strict tuple comparison so no lower rule can overturn a higher one.


@dataclass(frozen=True)
class KeeperResult:
    keeper_id: int
    reason: str                        # human-readable chain
    ranking: list[tuple[int, str]]    # (photo_id, reason) sorted best-first


def _sort_key(m: MemberFacts) -> tuple:
    # Higher tuple wins. Booleans compare as ints. All fields chosen so
    # "better" is greater. Final tiebreak: lower id wins → negate.
    return (
        int(m.has_exif_original),
        int(m.is_tiff),
        m.pixels,
        m.file_size or 0,
        int(m.is_scan),  # only decides when the tuple is equal above
        -m.photo_id,
    )


def _reason_chain(winner: MemberFacts, others: list[MemberFacts]) -> str:
    """One-line explanation of why `winner` beat the field. Emit the
    first rule that separates the winner from at least one other member.
    """
    if not others:
        return "only member"

    reasons: list[str] = []

    if winner.has_exif_original and any(not o.has_exif_original for o in others):
        reasons.append("has EXIF")
    if winner.is_tiff and any(not o.is_tiff for o in others):
        reasons.append("TIFF")

    max_other_pixels = max(o.pixels for o in others)
    if winner.pixels > max_other_pixels:
        if max_other_pixels > 0:
            ratio = winner.pixels / max_other_pixels
            reasons.append(f"{ratio:.1f}x pixels")
        else:
            reasons.append("has pixel count")

    max_other_size = max((o.file_size or 0) for o in others)
    winner_size = winner.file_size or 0
    if winner_size > max_other_size and max_other_size > 0:
        reasons.append(f"{winner_size / max_other_size:.1f}x file size")

    if not reasons and winner.is_scan and any(not o.is_scan for o in others):
        reasons.append("scan carries physical ref")

    if not reasons:
        reasons.append("lower id")

    return " > ".join(reasons)


def pick_keeper(members: list[MemberFacts]) -> KeeperResult:
    """Return the keeper (highest score, lowest id on ties) and a reason
    chain. Deterministic.
    """
    if not members:
        raise ValueError("pick_keeper: empty member list")

    ordered = sorted(members, key=_sort_key, reverse=True)
    keeper = ordered[0]
    others = [m for m in members if m.photo_id != keeper.photo_id]
    reason = _reason_chain(keeper, others)

    ranking: list[tuple[int, str]] = []
    ranking.append((keeper.photo_id, reason))
    for m in ordered[1:]:
        ranking.append((m.photo_id, ""))
    return KeeperResult(keeper_id=keeper.photo_id, reason=reason, ranking=ranking)
