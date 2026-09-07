"""Agglomerative single-linkage clustering on cosine distance, in-memory
with numpy. Fast enough for the ~25k faces the archive is likely to hold
without pulling in sklearn (and pgvector remains out of scope).

Algorithm:
  1. L2-normalise every embedding so dot product = cosine similarity.
  2. In blocks of BLOCK rows, dot the block against all embeddings.
  3. Any pair with (1 - similarity) <= threshold joins in a union-find.
  4. Return clusters sorted largest-first.

Memory: block × n × 4 bytes. For n=25000, BLOCK=500 → ~50 MB per block.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np


BLOCK_ROWS = 500


@dataclass
class ClusteringResult:
    clusters: list[list[int]]  # face_id lists, largest first
    threshold: float
    total_faces: int

    def size_histogram(self, bins: tuple[int, ...] = (1, 2, 5, 10, 25, 100)) -> dict[str, int]:
        buckets = {f">={b}": 0 for b in bins}
        for c in self.clusters:
            size = len(c)
            for b in bins:
                if size >= b:
                    buckets[f">={b}"] += 1
        return buckets


def cluster_faces(
    face_ids: list[int],
    embeddings: np.ndarray,   # shape (n, D)
    *,
    max_cosine_distance: float,
) -> ClusteringResult:
    if len(face_ids) != embeddings.shape[0]:
        raise ValueError("face_ids and embeddings must be the same length")
    n = len(face_ids)
    if n == 0:
        return ClusteringResult(clusters=[], threshold=max_cosine_distance, total_faces=0)

    normed = embeddings.astype(np.float32)
    norms = np.linalg.norm(normed, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normed = normed / norms

    threshold_sim = 1.0 - max_cosine_distance
    parents = list(range(n))

    def find(x: int) -> int:
        while parents[x] != x:
            parents[x] = parents[parents[x]]  # path compression
            x = parents[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parents[ra] = rb

    for start in range(0, n, BLOCK_ROWS):
        end = min(start + BLOCK_ROWS, n)
        block = normed[start:end]
        sim = block @ normed.T  # shape (end-start, n)
        # Zero out the diagonal so a face doesn't match itself.
        for i in range(end - start):
            sim[i, start + i] = -1.0
        rows, cols = np.where(sim >= threshold_sim)
        for r, c in zip(rows, cols):
            i = start + int(r)
            j = int(c)
            if j > i:
                union(i, j)

    groups: dict[int, list[int]] = defaultdict(list)
    for idx in range(n):
        groups[find(idx)].append(face_ids[idx])

    clusters = sorted(groups.values(), key=len, reverse=True)
    return ClusteringResult(
        clusters=clusters,
        threshold=max_cosine_distance,
        total_faces=n,
    )


def nearest_person(
    query_embedding: np.ndarray,
    people_means: dict[int, np.ndarray],
) -> tuple[int | None, float]:
    """Return (person_id, cosine_distance) for the closest reference
    embedding, or (None, inf) if there are no references."""
    if not people_means:
        return None, float("inf")
    q = query_embedding.astype(np.float32)
    qn = q / max(np.linalg.norm(q), 1e-9)
    best_pid: int | None = None
    best_dist = float("inf")
    for pid, mean in people_means.items():
        mn = mean / max(np.linalg.norm(mean), 1e-9)
        dist = 1.0 - float(np.dot(qn, mn))
        if dist < best_dist:
            best_dist = dist
            best_pid = pid
    return best_pid, best_dist
