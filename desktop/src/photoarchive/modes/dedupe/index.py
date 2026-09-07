"""Multi-index Hamming candidate search over 256-bit perceptual hashes.

The 256-bit hash is split into 16 bands of 16 bits. By the pigeonhole
principle, two hashes at Hamming distance D differ in at most D bands, so
at least (16 - D) bands are identical. Any pair within D <= 15 must
therefore collide in at least one band's exact value — an exact-match hash
table across the bands catches every candidate, and we verify the full
distance afterwards. Above D = 15 the filter may miss pairs, so the scan
job falls back to brute force there (rare in practice; default is 10).

Complexity: build O(N * 16), query O(N * 16 * avg_bucket_size). For ~13k
photos this completes in under a second on a laptop.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


BANDS = 16
BAND_BITS = 16
_BAND_MASK = (1 << BAND_BITS) - 1
MAX_SAFE_DISTANCE = BANDS - 1  # 15


def hex_to_int(h: str) -> int:
    """Parse a 64-char hex string into a 256-bit int. Accepts leading 0x."""
    return int(h, 16)


def _bands(hash_int: int) -> tuple[int, ...]:
    return tuple((hash_int >> (i * BAND_BITS)) & _BAND_MASK for i in range(BANDS))


def hamming_int(a: int, b: int) -> int:
    """256-bit Hamming distance between two integer hashes."""
    return (a ^ b).bit_count()


@dataclass
class MultiIndex:
    """Banded index over one hash per key. Keys are arbitrary hashables;
    we use `(photo_id, transform_name)` so variant hashes coexist in one
    index without conflicting with the identity hash.

    Build with `build()`, then call `query(hash_int, max_distance)`.
    """

    # band_number -> {band_value -> [(key, full_hash_int)]}
    _tables: list[dict[int, list]]

    @classmethod
    def build(cls, items: Iterable[tuple[object, int]]) -> "MultiIndex":
        tables: list[dict[int, list]] = [dict() for _ in range(BANDS)]
        for key, h in items:
            for i, b in enumerate(_bands(h)):
                tables[i].setdefault(b, []).append((key, h))
        return cls(_tables=tables)

    def query(self, h: int, max_distance: int) -> list[tuple[object, int]]:
        """Return (key, distance) for every indexed item within max_distance
        of `h`. Each key appears at most once even if it collided in
        several bands.

        Correctness note: exhaustive for max_distance <= MAX_SAFE_DISTANCE.
        Callers passing a larger max_distance must handle the shortfall.
        """
        seen: dict[object, int] = {}
        for i, b in enumerate(_bands(h)):
            bucket = self._tables[i].get(b)
            if not bucket:
                continue
            for key, other in bucket:
                if key in seen:
                    continue
                d = hamming_int(h, other)
                if d <= max_distance:
                    seen[key] = d
        return list(seen.items())

    def size(self) -> int:
        """Number of unique keys in the index."""
        keys: set[object] = set()
        for t in self._tables:
            for bucket in t.values():
                for key, _ in bucket:
                    keys.add(key)
        return len(keys)


def brute_pairs(
    items: list[tuple[int, int]], max_distance: int,
) -> list[tuple[int, int, int]]:
    """Reference brute-force: return (id_a, id_b, distance) for every pair
    with id_a < id_b and hamming <= max_distance. Used by the scan job when
    the configured threshold exceeds MAX_SAFE_DISTANCE, and by tests to
    verify the multi-index misses nothing at safe thresholds.
    """
    n = len(items)
    out: list[tuple[int, int, int]] = []
    for i in range(n):
        a_id, a_h = items[i]
        for j in range(i + 1, n):
            b_id, b_h = items[j]
            d = hamming_int(a_h, b_h)
            if d <= max_distance:
                out.append((a_id, b_id, d))
    return out
