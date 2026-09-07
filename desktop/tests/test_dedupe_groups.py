"""Union-find grouping of candidate pairs."""
from __future__ import annotations

from photoarchive.modes.dedupe.groups import CandidatePair, build_groups


def _pair(a: int, b: int, d: int) -> CandidatePair:
    return CandidatePair(
        photo_a=min(a, b), photo_b=max(a, b),
        phash_dist=d, dhash_dist=None,
        matched_by="phash", transform_on_b="identity",
    )


def test_two_isolated_pairs_form_two_groups():
    pairs = [_pair(1, 2, 3), _pair(10, 11, 4)]
    groups = build_groups(pairs)
    assert len(groups) == 2
    sizes = sorted(len(g.members) for g in groups)
    assert sizes == [2, 2]


def test_chain_of_three_becomes_one_group_of_three():
    pairs = [_pair(1, 2, 3), _pair(2, 3, 4)]
    groups = build_groups(pairs)
    assert len(groups) == 1
    assert groups[0].members == [1, 2, 3]
    assert groups[0].min_distance == 3


def test_star_of_five_becomes_one_group():
    pairs = [_pair(1, 2, 2), _pair(1, 3, 3), _pair(1, 4, 4), _pair(1, 5, 5)]
    groups = build_groups(pairs)
    assert len(groups) == 1
    assert groups[0].members == [1, 2, 3, 4, 5]
    assert groups[0].min_distance == 2


def test_groups_sorted_by_size_desc_then_min_distance_asc():
    # small-tight vs big-loose
    pairs = [
        _pair(1, 2, 8),
        _pair(3, 4, 1),
        _pair(1, 5, 5),   # extends the 1-2 group to size 3
    ]
    groups = build_groups(pairs)
    assert [len(g.members) for g in groups] == [3, 2]
    assert groups[0].members == [1, 2, 5]
    assert groups[1].members == [3, 4]


def test_singletons_are_not_emitted():
    # No pairs at all → no groups
    assert build_groups([]) == []
