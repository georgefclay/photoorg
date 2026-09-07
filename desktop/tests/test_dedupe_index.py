"""Multi-index Hamming search: verify it finds every pair a brute-force
scan would find on a synthetic set of 2,000 hashes."""
from __future__ import annotations

import random

from photoarchive.modes.dedupe.index import (
    MAX_SAFE_DISTANCE, MultiIndex, brute_pairs,
)


def _rand_256() -> int:
    return random.getrandbits(256)


def _flip_bits(h: int, k: int) -> int:
    """Flip exactly k random bit positions in a 256-bit int."""
    positions = random.sample(range(256), k)
    for p in positions:
        h ^= (1 << p)
    return h


def test_multi_index_matches_brute_force_at_default_thresholds():
    random.seed(42)

    n = 2000
    ids = list(range(1, n + 1))
    hashes: dict[int, int] = {}
    for pid in ids:
        hashes[pid] = _rand_256()

    # Seed some deliberate near-neighbours so the test is meaningful.
    for offset in range(1, 21):
        source = ids[offset - 1]
        target = ids[n - offset]
        hashes[target] = _flip_bits(hashes[source], offset % 12)

    items = list(hashes.items())
    threshold = 10  # default DEDUPE_PHASH_MAX

    idx = MultiIndex.build(items)
    got: set[tuple[int, int, int]] = set()
    for pid, h in items:
        for other, d in idx.query(h, threshold):
            if other == pid:
                continue
            a, b = (pid, other) if pid < other else (other, pid)
            got.add((a, b, d))

    expected = set(brute_pairs(items, threshold))
    assert got == expected


def test_multi_index_safe_up_to_max_safe_distance():
    random.seed(7)
    items = [(i, _rand_256()) for i in range(1, 501)]
    # Insert one pair at exactly MAX_SAFE_DISTANCE (15).
    items[0] = (1, items[1][1] ^ ((1 << MAX_SAFE_DISTANCE) - 1))

    idx = MultiIndex.build(items)
    got: set[tuple[int, int]] = set()
    for pid, h in items:
        for other, d in idx.query(h, MAX_SAFE_DISTANCE):
            if other == pid:
                continue
            a, b = (pid, other) if pid < other else (other, pid)
            got.add((a, b))

    expected_pairs = {(a, b) for a, b, _ in brute_pairs(items, MAX_SAFE_DISTANCE)}
    assert got == expected_pairs


def test_multi_index_returns_distances_correctly():
    a = 0
    b = 0b1011  # 3 bits set
    c = (1 << 200)  # 1 bit set
    items = [(1, a), (2, b), (3, c)]
    idx = MultiIndex.build(items)

    matches = dict(idx.query(a, 5))
    assert matches[2] == 3
    assert matches[3] == 1
    assert matches.get(1, None) == 0
