"""Clustering + reference-set semantics on synthetic embeddings.

Post fix-up 2: average-linkage cosine clustering with recursive split
above `max_cluster`. `result.clusters` is a list of `ClusterMeta` (not
raw `list[int]`). Tests below cover the properties fix-up 2 exists to
enforce: three well-separated groups still resolve to three clusters; a
chain of intermediates does NOT merge two tight groups (the regression
that fix-up 2 was written for); a too-big cluster is broken up by
`max_cluster`; face-ordering-by-centroid puts outliers last; the
two-nearest-people helper picks siblings correctly.
"""

from __future__ import annotations

import numpy as np

from photoarchive.modes.faces.clustering import (
    ClusterMeta,
    annotate_clusters_with_likely_person,
    cluster_faces,
    diagnose_faces,
    nearest_person,
    nearest_person_by_prototype,
    nearest_two_people,
    order_cluster_by_centroid_distance,
    topk_persons_by_prototype,
)


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
    # Filter to real clusters (size >= 3, which is where the big-first queue lives)
    real = [c for c in result.clusters if len(c.face_ids) >= 3]
    assert len(real) == 3
    for c in real:
        assert len(c.face_ids) == 10


def test_cluster_below_threshold_becomes_singletons():
    rng = np.random.default_rng(7)
    d = 16
    embs = [_rand_unit(d, rng) for _ in range(20)]
    face_ids = list(range(1, 21))
    embeddings = np.stack(embs)
    # Very tight threshold — random unit vectors in high-d are usually far apart.
    result = cluster_faces(face_ids, embeddings, max_cosine_distance=0.001)
    assert sum(len(c.face_ids) for c in result.clusters) == 20
    # Overwhelming majority should be size-1 clusters.
    singles = [c for c in result.clusters if len(c.face_ids) == 1]
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


# ---- fix-up 2 ------------------------------------------------------------


def test_average_linkage_does_not_chain_two_tight_groups():
    """The regression fix-up 2 exists for. Two tight groups (A and B),
    joined by a short chain of intermediate 'bridge' faces at 0.20 apart.
    Single linkage merges A + bridge + B into one cluster; average
    linkage keeps A and B distinct because the mean intra-cluster
    distance blows past the threshold when you'd merge the two ends."""
    rng = np.random.default_rng(11)
    d = 24

    def _cluster_of(centre: np.ndarray, k: int, spread: float) -> list[np.ndarray]:
        out = []
        for _ in range(k):
            n = spread * _rand_unit(d, rng)
            v = centre + n
            out.append(v / np.linalg.norm(v))
        return out

    # Build two orthogonal centres (cosine distance A↔B = 1.0), then a
    # bridge of intermediates that linearly interpolates between them.
    v_a = np.zeros(d, dtype=np.float32); v_a[0] = 1.0
    v_b = np.zeros(d, dtype=np.float32); v_b[1] = 1.0
    group_a_centre = v_a
    group_b_centre = v_b
    embs = _cluster_of(group_a_centre, 8, 0.05) + _cluster_of(group_b_centre, 8, 0.05)
    # Bridge chain: five vectors linearly interpolating A→B, each close
    # to the next. With single linkage this chains everything together;
    # with average linkage the intra-cluster distance defeats the merge.
    for t in np.linspace(0.1, 0.9, 5):
        mix = (1 - t) * group_a_centre + t * group_b_centre
        norm = float(np.linalg.norm(mix))
        if norm < 1e-9:
            continue
        embs.append(mix / norm)
    face_ids = list(range(1, len(embs) + 1))
    embeddings = np.stack(embs)

    result = cluster_faces(
        face_ids, embeddings, max_cosine_distance=0.20,
    )
    big = [c for c in result.clusters if len(c.face_ids) >= 5]
    # Two tight groups still resolve as two big clusters — the chain
    # does not merge them.
    assert len(big) == 2
    # Neither big cluster is the whole archive (that would be the chaining
    # regression).
    for c in big:
        assert len(c.face_ids) < len(face_ids) - 3


def test_recursive_split_breaks_up_oversized_cluster():
    """A single tight cluster of 400 faces exceeds the 200-face cap and
    should be broken into several sub-clusters, each tagged
    `split_from_larger`."""
    rng = np.random.default_rng(5)
    d = 16
    centre = _rand_unit(d, rng)
    embs = []
    for _ in range(400):
        e = centre + 0.02 * _rand_unit(d, rng)
        embs.append(e / np.linalg.norm(e))
    face_ids = list(range(1, len(embs) + 1))
    embeddings = np.stack(embs)
    result = cluster_faces(
        face_ids, embeddings,
        max_cosine_distance=0.10,
        max_cluster=200,
    )
    # At least one split-from-larger meta.
    splits = [c for c in result.clusters if c.split_from_larger]
    assert len(splits) >= 1
    # Every cluster (split or not) must be <= max_cluster OR flagged as
    # split (some deep-recursion terminals still exceed the cap and are
    # accepted, but the top-level cluster should not be a single 400-blob).
    top = result.clusters[0]
    assert len(top.face_ids) <= 200 or top.split_from_larger


def test_big_first_small_last_ordering():
    """Sizes ≥ 3 come first (sorted desc); < 3 clusters push to the back."""
    rng = np.random.default_rng(21)
    d = 16
    # Two big clumps + two singletons.
    big_a = _rand_unit(d, rng)
    big_b = _rand_unit(d, rng)
    embs = [
        *(big_a + 0.02 * _rand_unit(d, rng) for _ in range(10)),
        *(big_b + 0.02 * _rand_unit(d, rng) for _ in range(6)),
        _rand_unit(d, rng),  # singleton
        _rand_unit(d, rng),  # singleton
    ]
    embs = [e / np.linalg.norm(e) for e in embs]
    face_ids = list(range(1, len(embs) + 1))
    result = cluster_faces(
        face_ids, np.stack(embs), max_cosine_distance=0.10,
    )
    sizes = [len(c.face_ids) for c in result.clusters]
    # The first two are the bigs (≥3, descending), the last two are the singletons.
    bigs = [s for s in sizes if s >= 3]
    smalls = [s for s in sizes if s < 3]
    assert bigs == sorted(bigs, reverse=True)
    assert smalls == sorted(smalls, reverse=True)
    # Layout: all bigs before any small.
    for i, s in enumerate(sizes):
        if s < 3:
            for later in sizes[i:]:
                assert later < 3


def test_order_cluster_by_centroid_distance_puts_outlier_last():
    rng = np.random.default_rng(9)
    d = 12
    centre = _rand_unit(d, rng)
    ids = list(range(1, 6))
    embs = {}
    # Four tight members
    for i in ids[:4]:
        v = centre + 0.02 * _rand_unit(d, rng)
        embs[i] = v / np.linalg.norm(v)
    # One outlier — the opposite direction
    embs[ids[4]] = -centre
    ordered = order_cluster_by_centroid_distance(ids, embs)
    assert ordered[-1] == ids[4]     # outlier drops to the back
    assert set(ordered[:4]) == set(ids[:4])


def test_nearest_two_people_returns_best_two():
    rng = np.random.default_rng(4)
    d = 16
    person_a = _rand_unit(d, rng)
    person_b = _rand_unit(d, rng)
    person_c = _rand_unit(d, rng)
    means = {1: person_a, 2: person_b, 3: person_c}
    # A query midway between A and B
    q = (person_a + person_b) / 2
    q = q / np.linalg.norm(q)
    two = nearest_two_people(q, means)
    assert len(two) == 2
    pids = {two[0][0], two[1][0]}
    assert pids == {1, 2}
    # Distances sorted ascending.
    assert two[0][1] <= two[1][1]


def test_multi_prototype_beats_single_mean_across_age_bands():
    """Fix-up 3: one person, two age-band blobs. A single-mean reference
    lands halfway between the blobs and misses both — a nearest-prototype
    match still finds them."""
    d = 32
    # Two orthogonal directions — represent adult vs child, cosine ≈ 1.0.
    adult = np.zeros(d, dtype=np.float32); adult[0] = 1.0
    child = np.zeros(d, dtype=np.float32); child[1] = 1.0

    rng = np.random.default_rng(19)
    adult_faces = []
    for _ in range(10):
        v = adult + 0.02 * _rand_unit(d, rng)
        adult_faces.append(v / np.linalg.norm(v))
    child_faces = []
    for _ in range(10):
        v = child + 0.02 * _rand_unit(d, rng)
        child_faces.append(v / np.linalg.norm(v))

    means_only = {1: np.mean(np.stack(adult_faces + child_faces), axis=0)}
    prototypes = {1: [np.mean(np.stack(adult_faces), axis=0),
                       np.mean(np.stack(child_faces), axis=0)]}

    # A brand-new adult query — close to the adult prototype, far from the child one.
    q = adult + 0.03 * _rand_unit(d, rng)
    q = q / np.linalg.norm(q)

    _, mean_dist = nearest_person(q, means_only)
    _, proto_dist = nearest_person_by_prototype(q, prototypes)
    # The mean sits ~halfway between the two blobs, so its cosine
    # distance to any real face is much larger than the near-prototype
    # distance.
    assert mean_dist > 0.20
    assert proto_dist < 0.05
    assert proto_dist * 3 < mean_dist  # meaningfully closer


def test_topk_persons_by_prototype_returns_sorted():
    d = 16
    v_a = np.zeros(d, dtype=np.float32); v_a[0] = 1.0
    v_b = np.zeros(d, dtype=np.float32); v_b[1] = 1.0
    v_c = np.zeros(d, dtype=np.float32); v_c[2] = 1.0
    prototypes = {1: [v_a], 2: [v_b], 3: [v_c]}
    # Query biased toward A, then B.
    q = 3 * v_a + v_b
    q = q / np.linalg.norm(q)
    ranked = topk_persons_by_prototype(q, prototypes, k=3)
    assert [pid for pid, _ in ranked] == [1, 2, 3]
    # Distances ascending.
    assert ranked[0][1] < ranked[1][1] < ranked[2][1]


def test_annotate_clusters_badges_likely_person():
    d = 16
    rng = np.random.default_rng(2)
    centre = _rand_unit(d, rng)
    ids = [1, 2, 3, 4]
    embs = {}
    for i in ids:
        v = centre + 0.02 * _rand_unit(d, rng)
        embs[i] = v / np.linalg.norm(v)
    # The person's prototype IS this cluster's centre.
    prototypes = {42: [centre]}
    clusters = [ClusterMeta(face_ids=ids)]
    annotate_clusters_with_likely_person(
        clusters, embs, prototypes, match_threshold=0.10,
    )
    assert clusters[0].likely_person_id == 42
    assert clusters[0].likely_person_distance is not None
    assert clusters[0].likely_person_distance < 0.05


def test_annotate_clusters_leaves_far_clusters_alone():
    d = 16
    rng = np.random.default_rng(3)
    centre = _rand_unit(d, rng)
    other = _rand_unit(d, rng)  # random, likely far in high-d
    ids = [1, 2, 3, 4]
    embs = {i: (centre + 0.02 * _rand_unit(d, rng)) for i in ids}
    for i in ids:
        embs[i] = embs[i] / np.linalg.norm(embs[i])
    prototypes = {42: [other]}
    clusters = [ClusterMeta(face_ids=ids)]
    annotate_clusters_with_likely_person(
        clusters, embs, prototypes, match_threshold=0.10,
    )
    assert clusters[0].likely_person_id is None
    assert clusters[0].likely_person_distance is None


def test_diagnose_faces_bucket_shapes():
    face_ids = [1, 2, 3, 4, 5]
    embs = np.zeros((5, 8), dtype=np.float32)
    det = [0.4, 0.65, 0.72, 0.9, None]
    px = [15, 30, 45, 120, None]
    d = diagnose_faces(face_ids, embs, det, px)
    assert d["total_faces"] == 5
    # 4 faces have a det_score at all; 3 are ≥ 0.5, 2 are ≥ 0.7, 1 is ≥ 0.9.
    assert d["det_score_histogram"][">=0.50"] == 3
    assert d["det_score_histogram"][">=0.70"] == 2
    assert d["det_score_histogram"][">=0.90"] == 1
    # 4 faces have a short_edge; 3 are ≥ 20, 2 are ≥ 40, 1 is ≥ 100.
    assert d["short_edge_px_histogram"][">=20"] == 3
    assert d["short_edge_px_histogram"][">=40"] == 2
    assert d["short_edge_px_histogram"][">=100"] == 1
