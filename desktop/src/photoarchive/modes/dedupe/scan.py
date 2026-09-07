"""`dedupe_scan` job — build/refresh the pending dedupe queue.

Re-runnable and incremental in the sense that we never touch resolved
or not-duplicates groups; we drop and rebuild all pending groups so a
newly-triaged photo or a newly-added file is caught next time.

Steps:
  1. Load every keep photo's (id, phash, dhash, thumb_path, ...).
  2. For each photo, compute the 7 variant hashes from its thumbnail
     (identity is trivially the stored hash; the other 6 come from the
     thumb) and index them all into two banded indices (pHash, dHash).
     Photos with a missing thumb still participate through the identity
     hash.
  3. Query each photo's identity hash against both indices; the union of
     matches (excluding self and other transforms of the same photo) is
     the candidate set for that photo. Skip pairs in `dedupe_exclusions`.
  4. Union-find to form connected components; drop singletons.
  5. Rebuild `dedupe_groups` (pending) and `dedupe_members` inside one
     transaction. Score each group's keeper.
  6. Return statistics: group counts by size, min-distance histogram,
     elapsed time.

The scan reads `is_scan`, `exif_taken_at`, `exif_camera`, etc. from the
DB into MemberFacts objects at scoring time; it does not re-read EXIF.
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import psycopg

from ... import db
from ...config import Settings
from ..ingest.paths import thumb_path
from .groups import CandidatePair, DedupeGroup, build_groups
from .index import MAX_SAFE_DISTANCE, MultiIndex, brute_pairs, hamming_int, hex_to_int
from .keeper import MemberFacts, pick_keeper
from .variants import TRANSFORMS, variant_hashes_from_thumb

log = logging.getLogger(__name__)


@dataclass
class ScanStats:
    photos_scanned: int = 0
    thumbs_missing: int = 0
    variant_errors: int = 0
    candidate_pairs: int = 0
    excluded_pairs: int = 0
    groups_created: int = 0
    groups_by_size: dict[int, int] = field(default_factory=dict)
    min_distance_histogram: dict[int, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "photos_scanned": self.photos_scanned,
            "thumbs_missing": self.thumbs_missing,
            "variant_errors": self.variant_errors,
            "candidate_pairs": self.candidate_pairs,
            "excluded_pairs": self.excluded_pairs,
            "groups_created": self.groups_created,
            "groups_by_size": self.groups_by_size,
            "min_distance_histogram": self.min_distance_histogram,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


@dataclass
class _PhotoRow:
    photo_id: int
    phash_hex: str | None
    dhash_hex: str | None
    is_scan: bool
    mime: str
    width: int | None
    height: int | None
    file_size: int | None
    exif_taken_at: object | None
    exif_camera: str | None


def _load_keep_photos(conn: psycopg.Connection) -> list[_PhotoRow]:
    # Exclude photos that were "marked as a back" by any earlier signal:
    #   * triage classified them possible_back (the visual heuristic said
    #     it's a print's back with only a date on it), OR
    #   * ingest proposed converting them to a back (regardless of
    #     whether George accepted or rejected the pairing).
    # These are near-blank scans that will otherwise cluster with each
    # other in dedupe (blank-vs-blank looks pHash-identical) and produce
    # noisy pairs that are not real duplicates.
    #
    # Photos that HAVE photo_backs rows attached (i.e., they are fronts
    # of a scanned back) are not excluded — they are legitimate photos.
    rows = conn.execute("""
        select id, phash, dhash, is_scan, mime, width, height, file_size,
               exif_taken_at, exif_camera
        from photos p
        where triage_status in ('keep', 'private')
          and is_deleted = false
          and phash is not null
          and dhash is not null
          and not exists (
            select 1 from triage_hints h
            where h.photo_id = p.id and h.hint = 'possible_back'
          )
          and not exists (
            select 1 from ingest_pairings ip
            where ip.back_photo_id = p.id
          )
        order by id
    """).fetchall()
    return [
        _PhotoRow(
            photo_id=r[0], phash_hex=r[1], dhash_hex=r[2], is_scan=bool(r[3]),
            mime=r[4], width=r[5], height=r[6], file_size=r[7],
            exif_taken_at=r[8], exif_camera=r[9],
        )
        for r in rows
    ]


def _load_exclusions(conn: psycopg.Connection) -> set[tuple[int, int]]:
    rows = conn.execute(
        "select photo_a, photo_b from dedupe_exclusions"
    ).fetchall()
    return {(a, b) for a, b in rows}


def _ordered_pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _hash_or_none(hex_str: str | None) -> int | None:
    if hex_str is None:
        return None
    try:
        return hex_to_int(hex_str)
    except ValueError:
        return None


def _build_indices(
    settings: Settings,
    photos: list[_PhotoRow],
    stats: ScanStats,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[MultiIndex, MultiIndex, dict[int, dict[str, tuple[int, int]]]]:
    """Return (phash_index, dhash_index, variants_by_photo) where the
    indices carry every (photo_id, transform) entry. `variants_by_photo`
    holds the per-photo variant table so we can look up a match's
    transform and its own distance later.
    """
    phash_items: list[tuple[object, int]] = []
    dhash_items: list[tuple[object, int]] = []
    variants_by_photo: dict[int, dict[str, tuple[int, int]]] = {}

    total = len(photos)
    for i, ph in enumerate(photos):
        identity_phash = _hash_or_none(ph.phash_hex)
        identity_dhash = _hash_or_none(ph.dhash_hex)
        if identity_phash is None or identity_dhash is None:
            continue

        variants: dict[str, tuple[int, int]] = {}
        variants["identity"] = (identity_phash, identity_dhash)

        t_path = thumb_path(settings, ph.photo_id)
        if t_path.exists():
            try:
                thumb_variants = variant_hashes_from_thumb(t_path)
                # `identity` from thumb should match the stored hash; prefer
                # the stored one to keep provenance consistent, but adopt the
                # 6 transformed ones from the thumb.
                for t, (p, d) in thumb_variants.items():
                    if t == "identity":
                        continue
                    variants[t] = (p, d)
            except (OSError, Exception) as e:
                stats.variant_errors += 1
                log.warning("dedupe: variant hashes failed for photo %d: %s",
                            ph.photo_id, e)
        else:
            stats.thumbs_missing += 1

        variants_by_photo[ph.photo_id] = variants
        for t, (p, d) in variants.items():
            phash_items.append(((ph.photo_id, t), p))
            dhash_items.append(((ph.photo_id, t), d))

        if progress and (i % 500 == 0 or i == total - 1):
            progress(i + 1, total)

    return MultiIndex.build(phash_items), MultiIndex.build(dhash_items), variants_by_photo


def _search_pairs(
    photos: list[_PhotoRow],
    variants_by_photo: dict[int, dict[str, tuple[int, int]]],
    phash_index: MultiIndex,
    dhash_index: MultiIndex,
    phash_max: int,
    dhash_max: int,
    exclusions: set[tuple[int, int]],
    stats: ScanStats,
) -> list[CandidatePair]:
    """For every photo, query only the identity hashes against the indices
    (which carry every transform of every other photo). Deduplicate to one
    edge per unordered pair; keep the best (smallest) distance and record
    which transform on the neighbour matched.
    """
    # Per unordered pair, track the smallest edge and which transform was
    # applied to one of the members to make it match the other. Direction
    # is not meaningful — the pair is stored (a<b), the transform tag
    # describes what was needed to align the two hashes, and the review UI
    # shows it as informational context ("matched rot180").
    best: dict[tuple[int, int], dict] = {}

    def _record(key: tuple[int, int], pdist: int | None, ddist: int | None,
                transform: str) -> None:
        entry = best.setdefault(key, {
            "phash_dist": None, "dhash_dist": None,
            "transform": "identity", "best": 256,
        })
        cur = min((v for v in (pdist, ddist) if v is not None), default=256)
        if cur < entry["best"]:
            entry["best"] = cur
            entry["transform"] = transform
        if pdist is not None and (entry["phash_dist"] is None or pdist < entry["phash_dist"]):
            entry["phash_dist"] = pdist
        if ddist is not None and (entry["dhash_dist"] is None or ddist < entry["dhash_dist"]):
            entry["dhash_dist"] = ddist

    for ph in photos:
        vs = variants_by_photo.get(ph.photo_id)
        if vs is None:
            continue
        identity_phash, identity_dhash = vs["identity"]

        for (neighbour_id, neighbour_transform), pdist in phash_index.query(
            identity_phash, phash_max,
        ):
            if neighbour_id == ph.photo_id:
                continue
            key = _ordered_pair(ph.photo_id, neighbour_id)
            if key in exclusions:
                continue
            _record(key, pdist, None, neighbour_transform)

        for (neighbour_id, neighbour_transform), ddist in dhash_index.query(
            identity_dhash, dhash_max,
        ):
            if neighbour_id == ph.photo_id:
                continue
            key = _ordered_pair(ph.photo_id, neighbour_id)
            if key in exclusions:
                continue
            _record(key, None, ddist, neighbour_transform)

    stats.excluded_pairs = sum(1 for _ in exclusions)  # informational

    pairs: list[CandidatePair] = []
    for (a, b), entry in best.items():
        pd, dd = entry["phash_dist"], entry["dhash_dist"]
        matched_by = (
            "both" if (pd is not None and dd is not None)
            else "phash" if pd is not None
            else "dhash"
        )
        pairs.append(CandidatePair(
            photo_a=a, photo_b=b,
            phash_dist=pd, dhash_dist=dd,
            matched_by=matched_by,
            transform_on_b=entry["transform"],
        ))
    stats.candidate_pairs = len(pairs)
    return pairs


def _brute_pairs_fallback(
    photos: list[_PhotoRow],
    variants_by_photo: dict[int, dict[str, tuple[int, int]]],
    phash_max: int,
    dhash_max: int,
    exclusions: set[tuple[int, int]],
    stats: ScanStats,
) -> list[CandidatePair]:
    """Fallback for thresholds above MAX_SAFE_DISTANCE. Compares every
    identity hash to every variant of every other photo. O(N^2) — only
    used at George's explicit choice to raise the threshold that high.
    """
    log.warning(
        "dedupe: threshold above safe multi-index limit; using brute force"
    )
    best: dict[tuple[int, int], dict] = {}
    photo_list = [p for p in photos if p.photo_id in variants_by_photo]
    ids = [p.photo_id for p in photo_list]
    for i, pa_id in enumerate(ids):
        ip_a, id_a = variants_by_photo[pa_id]["identity"]
        for pb_id in ids[i + 1:]:
            key = (pa_id, pb_id)
            if key in exclusions:
                continue
            best_p, best_d, best_t = None, None, None
            for t, (vp, vd) in variants_by_photo[pb_id].items():
                pd = hamming_int(ip_a, vp) if vp is not None else None
                dd = hamming_int(id_a, vd) if vd is not None else None
                if pd is not None and pd <= phash_max:
                    if best_p is None or pd < best_p:
                        best_p, best_t = pd, t
                if dd is not None and dd <= dhash_max:
                    if best_d is None or dd < best_d:
                        best_d = dd
                        if best_t is None:
                            best_t = t
            if best_p is not None or best_d is not None:
                matched_by = (
                    "both" if (best_p is not None and best_d is not None)
                    else "phash" if best_p is not None
                    else "dhash"
                )
                best[key] = {
                    "phash_dist": best_p, "dhash_dist": best_d,
                    "transform_on_b": best_t or "identity",
                    "matched_by": matched_by,
                }
    stats.candidate_pairs = len(best)
    return [
        CandidatePair(
            photo_a=a, photo_b=b,
            phash_dist=e["phash_dist"], dhash_dist=e["dhash_dist"],
            matched_by=e["matched_by"], transform_on_b=e["transform_on_b"],
        )
        for (a, b), e in best.items()
    ]


def _rebuild_pending_groups(
    conn: psycopg.Connection,
    groups: list[DedupeGroup],
    photos_by_id: dict[int, _PhotoRow],
) -> int:
    """Drop existing pending groups and their members, then insert the
    new groups + members + keeper choice. Returns the number of groups
    actually written (some proposed groups may collapse if a member has
    been junked since the pair index was built).
    """
    conn.execute("""
        delete from dedupe_members
        where group_id in (select id from dedupe_groups where status = 'pending')
    """)
    conn.execute("delete from dedupe_groups where status = 'pending'")

    written = 0
    for g in groups:
        live_members = [pid for pid in g.members if pid in photos_by_id]
        if len(live_members) < 2:
            continue
        member_facts = [
            MemberFacts(
                photo_id=pid,
                is_scan=photos_by_id[pid].is_scan,
                mime=photos_by_id[pid].mime,
                width=photos_by_id[pid].width,
                height=photos_by_id[pid].height,
                file_size=photos_by_id[pid].file_size,
                exif_taken_at=photos_by_id[pid].exif_taken_at,
                exif_camera=photos_by_id[pid].exif_camera,
            )
            for pid in live_members
        ]
        keeper = pick_keeper(member_facts)

        row = conn.execute("""
            insert into dedupe_groups (status, size, min_distance)
            values ('pending', %s, %s)
            returning id
        """, (len(live_members), g.min_distance)).fetchone()
        group_id = row[0]
        written += 1

        # Per-member distance-to-keeper and transform: find the edge that
        # touches the keeper for each non-keeper; if none direct (chain
        # component), fall back to the minimum edge involving that member.
        keeper_id = keeper.keeper_id
        keeper_edges: dict[int, "GroupEdge"] = {}
        member_min_edge: dict[int, "GroupEdge"] = {}
        for e in g.edges:
            for owner, other in ((e.a, e.b), (e.b, e.a)):
                cur = member_min_edge.get(owner)
                if cur is None or _edge_distance(e) < _edge_distance(cur):
                    member_min_edge[owner] = e
                if other == keeper_id:
                    cur_k = keeper_edges.get(owner)
                    if cur_k is None or _edge_distance(e) < _edge_distance(cur_k):
                        keeper_edges[owner] = e

        for pid in live_members:
            is_keeper = pid == keeper_id
            if is_keeper:
                phash_d = 0
                dhash_d = 0
                matched_by = "both"
                transform = "identity"
                dist = 0
            else:
                edge = keeper_edges.get(pid) or member_min_edge.get(pid)
                if edge is None:
                    phash_d = None
                    dhash_d = None
                    matched_by = "phash"
                    transform = "identity"
                    dist = 256
                else:
                    phash_d = edge.phash_dist
                    dhash_d = edge.dhash_dist
                    matched_by = edge.matched_by
                    # transform_on_b is the transform of the non-`a` endpoint;
                    # if this member is the `b`, that's the transform; if the
                    # member is `a`, the transform applies to the other end
                    # so we invert to "identity" for the member itself.
                    transform = edge.transform_on_b if pid == edge.b else "identity"
                    dist = _edge_distance(edge)

            reason = keeper.reason if is_keeper else None
            conn.execute("""
                insert into dedupe_members
                  (group_id, photo_id, is_keeper, phash_dist, dhash_dist,
                   matched_by, transform, distance_to_keeper, keeper_reason)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (group_id, pid, is_keeper, phash_d, dhash_d,
                  matched_by, transform, dist, reason))
    return written


def _edge_distance(edge) -> int:
    vals = [d for d in (edge.phash_dist, edge.dhash_dist) if d is not None]
    return min(vals) if vals else 256


def run_dedupe_scan(
    settings: Settings,
    *,
    progress: Callable[[str, int, int], None] | None = None,
) -> ScanStats:
    """Run the full dedupe scan and return statistics. Writes a job_runs
    row and updates the pending queue transactionally.

    `progress(stage, done, total)` is called at coarse intervals for the UI.
    """
    stats = ScanStats()
    t0 = time.perf_counter()

    with db.connection() as conn:
        conn.autocommit = False
        try:
            job_run_id = db.start_job_run(
                conn, job_name="dedupe_scan",
                params={
                    "phash_max": settings.DEDUPE_PHASH_MAX,
                    "dhash_max": settings.DEDUPE_DHASH_MAX,
                },
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    with db.connection() as conn:
        photos = _load_keep_photos(conn)
        exclusions = _load_exclusions(conn)
    stats.photos_scanned = len(photos)

    def _prog(stage: str):
        def _cb(done: int, total: int) -> None:
            if progress:
                progress(stage, done, total)
        return _cb

    phash_index, dhash_index, variants_by_photo = _build_indices(
        settings, photos, stats, progress=_prog("index"),
    )

    phash_max = settings.DEDUPE_PHASH_MAX
    dhash_max = settings.DEDUPE_DHASH_MAX
    if phash_max > MAX_SAFE_DISTANCE or dhash_max > MAX_SAFE_DISTANCE:
        pairs = _brute_pairs_fallback(
            photos, variants_by_photo, phash_max, dhash_max, exclusions, stats,
        )
    else:
        pairs = _search_pairs(
            photos, variants_by_photo, phash_index, dhash_index,
            phash_max, dhash_max, exclusions, stats,
        )

    groups = build_groups(pairs)
    # Drop singletons: build_groups only emits components with >=1 edge,
    # so every group already has >=2 members.

    photos_by_id = {p.photo_id: p for p in photos}
    with db.connection() as conn:
        conn.autocommit = False
        try:
            n = _rebuild_pending_groups(conn, groups, photos_by_id)
            stats.groups_created = n
            stats.groups_by_size = dict(Counter(len(g.members) for g in groups))
            stats.min_distance_histogram = dict(
                Counter(g.min_distance for g in groups)
            )
            stats.elapsed_seconds = time.perf_counter() - t0
            db.finish_job_run(
                conn, job_run_id=job_run_id,
                status="ok", stats=stats.to_dict(),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            with db.connection() as ec:
                ec.autocommit = True
                db.finish_job_run(
                    ec, job_run_id=job_run_id,
                    status="failed", stats=stats.to_dict(),
                )
            raise

    return stats
