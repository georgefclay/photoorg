"""Union-find grouping of candidate pairs into dedupe groups.

Given a list of (photo_a, photo_b, phash_dist, dhash_dist, matched_by,
transform) candidate edges, coalesce transitively-connected photos into
one group each. So a chain A<-eq->B<-eq->C becomes one group of three.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass
class CandidatePair:
    photo_a: int
    photo_b: int
    phash_dist: int | None
    dhash_dist: int | None
    matched_by: str  # 'phash' | 'dhash' | 'both'
    transform_on_b: str  # transform of b that matched a's identity

    def distance(self) -> int:
        """Minimum of the two hash distances (whichever is available)."""
        vals = [d for d in (self.phash_dist, self.dhash_dist) if d is not None]
        return min(vals) if vals else 256


@dataclass
class GroupEdge:
    """A pairwise edge used inside a group after union-find. Stored so
    the UI can display distance to the eventual keeper.
    """
    a: int
    b: int
    phash_dist: int | None
    dhash_dist: int | None
    matched_by: str
    transform_on_b: str


@dataclass
class DedupeGroup:
    """A connected component of candidate pairs."""
    members: list[int]                 # photo ids, sorted asc
    edges: list[GroupEdge]             # every pair-edge in the component
    min_distance: int                  # smallest edge distance in the group


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[int, int] = {}
        self._rank: dict[int, int] = {}

    def add(self, x: int) -> None:
        if x not in self._parent:
            self._parent[x] = x
            self._rank[x] = 0

    def find(self, x: int) -> int:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            nxt = self._parent[x]
            self._parent[x] = root
            x = nxt
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1


def build_groups(pairs: Iterable[CandidatePair]) -> list[DedupeGroup]:
    """Union-find over the candidate edges. Returns one DedupeGroup per
    connected component, sorted by size desc then min-distance asc.
    """
    uf = _UnionFind()
    edges: list[CandidatePair] = list(pairs)
    for p in edges:
        uf.add(p.photo_a)
        uf.add(p.photo_b)
        uf.union(p.photo_a, p.photo_b)

    by_root: dict[int, list[int]] = {}
    for photo_id in uf._parent:
        r = uf.find(photo_id)
        by_root.setdefault(r, []).append(photo_id)

    edges_by_root: dict[int, list[GroupEdge]] = {}
    for p in edges:
        r = uf.find(p.photo_a)
        edges_by_root.setdefault(r, []).append(GroupEdge(
            a=p.photo_a, b=p.photo_b,
            phash_dist=p.phash_dist, dhash_dist=p.dhash_dist,
            matched_by=p.matched_by, transform_on_b=p.transform_on_b,
        ))

    groups: list[DedupeGroup] = []
    for r, members in by_root.items():
        member_set = set(members)
        component_edges = edges_by_root.get(r, [])
        # By construction every edge in this component has both endpoints
        # in member_set, but assert to catch a broken union-find early.
        for e in component_edges:
            assert e.a in member_set and e.b in member_set
        min_d = min((_edge_distance(e) for e in component_edges), default=256)
        groups.append(DedupeGroup(
            members=sorted(members),
            edges=component_edges,
            min_distance=min_d,
        ))

    groups.sort(key=lambda g: (-len(g.members), g.min_distance, g.members[0]))
    return groups


def _edge_distance(e: GroupEdge) -> int:
    vals = [d for d in (e.phash_dist, e.dhash_dist) if d is not None]
    return min(vals) if vals else 256
