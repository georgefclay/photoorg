"""Agglomerative clustering on cosine distance, in-memory with numpy +
scipy. Post-fix-up 2 this uses **average** linkage — single linkage was
happily chaining ~3500 faces into one blob through low-quality
intermediates. Average linkage plus the quality gate + recursive split
below keeps the top clusters human-sized (tens to a few hundred).

Pipeline:
  1. Caller filters out low-quality faces (det_score / bbox short edge)
     before handing embeddings in. Those rows are surfaced separately
     under a "low quality" bucket.
  2. `cluster_faces` runs scipy `linkage(..., method='average',
     metric='cosine')` + `fcluster` at FACE_CLUSTER_DIST.
  3. `recursive_split` re-clusters any cluster larger than
     `max_cluster` on its own members at threshold × 0.8, iteratively.
  4. `order_cluster_by_centroid_distance` puts the "best fit" faces
     first and the outliers last — useful when a cluster mixes
     siblings and George wants to shift-select the tail.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import pdist


LOW_QUALITY_BUCKET_KEY = "__low_quality__"


@dataclass
class ClusterMeta:
    face_ids: list[int]
    split_from_larger: bool = False       # True if produced by recursive split
    threshold_used: float = 0.0           # cosine cutoff that produced this cluster
    ordered_by_centroid: bool = False


@dataclass
class ClusteringResult:
    clusters: list[ClusterMeta]           # sorted: big first, small (< 3) at back
    threshold: float
    total_faces: int
    low_quality_face_ids: list[int] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)

    def size_histogram(self, bins: tuple[int, ...] = (1, 2, 5, 10, 25, 100, 300)) -> dict[str, int]:
        buckets = {f">={b}": 0 for b in bins}
        for c in self.clusters:
            size = len(c.face_ids)
            for b in bins:
                if size >= b:
                    buckets[f">={b}"] += 1
        return buckets


# --- clustering ------------------------------------------------------------


def cluster_faces(
    face_ids: list[int],
    embeddings: np.ndarray,
    *,
    max_cosine_distance: float,
    max_cluster: int = 300,
    recursive_shrink: float = 0.8,
    recursion_depth_cap: int = 5,
) -> ClusteringResult:
    """Cluster the given (already-quality-gated) faces. Returns clusters
    ordered by size (largest first) except that clusters with < 3 faces
    are pushed to the back so the meaty ones surface first."""
    if len(face_ids) != embeddings.shape[0]:
        raise ValueError("face_ids and embeddings must be the same length")
    n = len(face_ids)
    if n == 0:
        return ClusteringResult(clusters=[], threshold=max_cosine_distance, total_faces=0)
    if n == 1:
        return ClusteringResult(
            clusters=[ClusterMeta(face_ids=list(face_ids), threshold_used=max_cosine_distance)],
            threshold=max_cosine_distance,
            total_faces=1,
        )

    normed = _l2_normalise(embeddings)
    labels = _fcluster_average(normed, max_cosine_distance)

    groups: dict[int, list[int]] = defaultdict(list)
    for idx, lbl in enumerate(labels):
        groups[int(lbl)].append(face_ids[idx])

    metas: list[ClusterMeta] = []
    for members in groups.values():
        if len(members) > max_cluster:
            sub = _recursive_split(
                members, face_ids, normed,
                threshold=max_cosine_distance * recursive_shrink,
                max_cluster=max_cluster,
                shrink=recursive_shrink,
                depth=1,
                depth_cap=recursion_depth_cap,
            )
            metas.extend(sub)
        else:
            metas.append(ClusterMeta(
                face_ids=members,
                threshold_used=max_cosine_distance,
            ))

    metas = _order_clusters(metas)
    return ClusteringResult(
        clusters=metas,
        threshold=max_cosine_distance,
        total_faces=n,
    )


def _fcluster_average(normed: np.ndarray, threshold: float) -> np.ndarray:
    n = normed.shape[0]
    # scipy pdist('cosine') needs at least two rows; the n==1 case is
    # handled by the caller.
    dist = pdist(normed, metric="cosine")
    Z = linkage(dist, method="average")
    return fcluster(Z, t=threshold, criterion="distance")


def _recursive_split(
    members: list[int],
    all_face_ids: list[int],
    all_normed: np.ndarray,
    *,
    threshold: float,
    max_cluster: int,
    shrink: float,
    depth: int,
    depth_cap: int,
) -> list[ClusterMeta]:
    """Split a too-big cluster by re-running average linkage on its own
    members at a tighter threshold. Recurses if the sub-cluster is still
    too big."""
    if depth > depth_cap or threshold <= 0.01:
        return [ClusterMeta(
            face_ids=members,
            split_from_larger=True,
            threshold_used=threshold,
        )]
    # Slice the normed matrix to just these members.
    id_to_idx = {fid: i for i, fid in enumerate(all_face_ids)}
    idxs = np.array([id_to_idx[fid] for fid in members], dtype=np.int64)
    sub = all_normed[idxs]
    labels = _fcluster_average(sub, threshold)
    groups: dict[int, list[int]] = defaultdict(list)
    for local_i, lbl in enumerate(labels):
        groups[int(lbl)].append(members[local_i])

    out: list[ClusterMeta] = []
    for sub_members in groups.values():
        if len(sub_members) > max_cluster:
            out.extend(_recursive_split(
                sub_members, all_face_ids, all_normed,
                threshold=threshold * shrink,
                max_cluster=max_cluster,
                shrink=shrink,
                depth=depth + 1,
                depth_cap=depth_cap,
            ))
        else:
            out.append(ClusterMeta(
                face_ids=sub_members,
                split_from_larger=True,
                threshold_used=threshold,
            ))
    return out


def _order_clusters(metas: list[ClusterMeta]) -> list[ClusterMeta]:
    """Big first, then small (< 3) at the back — sizes tied by insertion
    order (stable)."""
    big = [m for m in metas if len(m.face_ids) >= 3]
    small = [m for m in metas if len(m.face_ids) < 3]
    big.sort(key=lambda m: len(m.face_ids), reverse=True)
    small.sort(key=lambda m: len(m.face_ids), reverse=True)
    return big + small


# --- suggestions -----------------------------------------------------------


def nearest_person(
    query_embedding: np.ndarray,
    people_means: dict[int, np.ndarray],
) -> tuple[int | None, float]:
    """Closest labelled-person mean by cosine distance."""
    if not people_means:
        return None, float("inf")
    q = _l2_normalise_one(query_embedding)
    best_pid: int | None = None
    best_dist = float("inf")
    for pid, mean in people_means.items():
        mn = _l2_normalise_one(mean)
        dist = 1.0 - float(np.dot(q, mn))
        if dist < best_dist:
            best_dist = dist
            best_pid = pid
    return best_pid, best_dist


def nearest_two_people(
    query_embedding: np.ndarray,
    people_means: dict[int, np.ndarray],
) -> list[tuple[int, float]]:
    """Return the two closest labelled people to a query, best first.
    Used by the sibling-split action (fix-up 2 item 7)."""
    if not people_means:
        return []
    q = _l2_normalise_one(query_embedding)
    dists = []
    for pid, mean in people_means.items():
        mn = _l2_normalise_one(mean)
        dists.append((int(pid), 1.0 - float(np.dot(q, mn))))
    dists.sort(key=lambda t: t[1])
    return dists[:2]


# --- centroid / diagnostics ------------------------------------------------


def cluster_centroid(
    face_ids: list[int],
    face_id_to_embedding: dict[int, np.ndarray],
) -> np.ndarray | None:
    embs = [face_id_to_embedding[fid] for fid in face_ids if fid in face_id_to_embedding]
    if not embs:
        return None
    return np.mean(np.stack(embs), axis=0)


def order_cluster_by_centroid_distance(
    face_ids: list[int],
    face_id_to_embedding: dict[int, np.ndarray],
) -> list[int]:
    """Closest-to-centroid first; outliers last."""
    centroid = cluster_centroid(face_ids, face_id_to_embedding)
    if centroid is None:
        return list(face_ids)
    cn = _l2_normalise_one(centroid)
    def _dist(fid: int) -> float:
        e = face_id_to_embedding.get(fid)
        if e is None:
            return float("inf")
        en = _l2_normalise_one(e)
        return 1.0 - float(np.dot(cn, en))
    return sorted(face_ids, key=_dist)


def mean_pairwise_cosine(embeddings: np.ndarray) -> float:
    """Cheap-ish mean pairwise cosine distance over up to a few hundred
    faces. Above that, sample."""
    normed = _l2_normalise(embeddings)
    n = normed.shape[0]
    if n < 2:
        return 0.0
    if n > 400:
        rng = np.random.default_rng(0)
        idx = rng.choice(n, size=400, replace=False)
        normed = normed[idx]
    dists = pdist(normed, metric="cosine")
    return float(np.mean(dists))


def diagnose_faces(
    face_ids: list[int],
    embeddings: np.ndarray,
    det_scores: list[float | None],
    short_edges: list[float | None],
    *,
    score_buckets: tuple[float, ...] = (0.5, 0.6, 0.7, 0.8, 0.9, 0.95),
    px_buckets: tuple[int, ...] = (20, 40, 60, 100, 200),
) -> dict:
    """Summary counts used by the diagnostics report on Recompute."""
    n = len(face_ids)
    score_hist = {f">={s:.2f}": 0 for s in score_buckets}
    for s in det_scores:
        if s is None:
            continue
        for b in score_buckets:
            if s >= b:
                score_hist[f">={b:.2f}"] += 1
    px_hist = {f">={p}": 0 for p in px_buckets}
    for p in short_edges:
        if p is None:
            continue
        for b in px_buckets:
            if p >= b:
                px_hist[f">={b}"] += 1
    return {
        "total_faces": n,
        "det_score_histogram": score_hist,
        "short_edge_px_histogram": px_hist,
    }


# --- helpers ---------------------------------------------------------------


def _l2_normalise(arr: np.ndarray) -> np.ndarray:
    a = arr.astype(np.float32)
    norms = np.linalg.norm(a, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return a / norms


def _l2_normalise_one(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / max(n, 1e-9)
