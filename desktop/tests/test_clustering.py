"""Clustering + reference-set semantics on synthetic embeddings."""

from __future__ import annotations

import numpy as np

from photoarchive.modes.faces.clustering import cluster_faces, nearest_person


def _rand_unit(d: int, rng: np.random.Generator) -> np.ndarray:
    v = rng.standard_normal(d).astype(np.float32)
    return v / np.linalg.norm(v)


def test_cluster_finds_three_groups():
    rng = np.random.default_rng(42)
    d = 32
    # Three well-separated centroids, 10 faces each.
    centroids = [_rand_unit(d, rng) for _ in range(3)]
    face_ids: list[int] = []
    embs: list[np.ndarray] = []
    fid = 1
    for c in centroids:
        for _ in range(10):
            noise = 0.02 * _rand_unit(d, rng)
            e = c + noise
            e = e / np.linalg.norm(e)
            face_ids.append(fid)
            embs.append(e)
            fid += 1
    embeddings = np.stack(embs)
    result = cluster_faces(face_ids, embeddings, max_cosine_distance=0.10)
    assert len(result.clusters) == 3
    for c in result.clusters:
        assert len(c) == 10


def test_cluster_below_threshold_becomes_singletons():
    rng = np.random.default_rng(7)
    d = 16
    embs = [_rand_unit(d, rng) for _ in range(20)]
    face_ids = list(range(1, 21))
    embeddings = np.stack(embs)
    # Very tight threshold — random unit vectors in high-d are usually far apart.
    result = cluster_faces(face_ids, embeddings, max_cosine_distance=0.001)
    assert sum(len(c) for c in result.clusters) == 20
    # Overwhelming majority should be size-1 clusters.
    singles = [c for c in result.clusters if len(c) == 1]
    assert len(singles) >= 15


def test_nearest_person_picks_closest_mean():
    rng = np.random.default_rng(1)
    d = 12
    person_a = _rand_unit(d, rng)
    person_b = _rand_unit(d, rng)
    means = {1: person_a, 2: person_b}
    # A query very close to person_a
    q = person_a + 0.01 * _rand_unit(d, rng)
    q = q / np.linalg.norm(q)
    pid, dist = nearest_person(q, means)
    assert pid == 1
    assert dist < 0.05


def test_nearest_person_empty_returns_none():
    q = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    pid, dist = nearest_person(q, {})
    assert pid is None
    assert dist == float("inf")


def test_size_histogram_shape():
    rng = np.random.default_rng(3)
    d = 8
    embs = [_rand_unit(d, rng) for _ in range(4)]
    face_ids = [1, 2, 3, 4]
    embeddings = np.stack(embs)
    result = cluster_faces(face_ids, embeddings, max_cosine_distance=2.0)  # everything merges
    hist = result.size_histogram()
    assert hist[">=1"] == len(result.clusters)
