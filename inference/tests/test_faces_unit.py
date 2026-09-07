"""The coordinate maths that the mocked endpoint tests cannot reach."""

import numpy as np
import pytest

from app.faces import cosine_distance, scale_detection


def test_scale_detection_maps_back_to_original_pixels():
    # Detection ran on a half-size copy, so every coordinate doubles.
    face = scale_detection(
        bbox_xyxy=[10.0, 20.0, 40.0, 60.0],
        kps=np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32),
        det_score=0.987654,
        embedding=np.array([0.5, -0.5], dtype=np.float32),
        scale=2.0,
    )
    assert face["bbox"] == {"x": 20.0, "y": 40.0, "w": 60.0, "h": 80.0}
    assert face["landmarks"] == [[2.0, 4.0], [6.0, 8.0]]
    assert face["det_score"] == 0.9877
    assert face["embedding"] == [0.5, -0.5]


def test_scale_detection_identity_when_not_downscaled():
    face = scale_detection(
        bbox_xyxy=[10.0, 20.0, 40.0, 60.0],
        kps=None,
        det_score=0.5,
        embedding=[0.0],
        scale=1.0,
    )
    assert face["bbox"] == {"x": 10.0, "y": 20.0, "w": 30.0, "h": 40.0}
    assert face["landmarks"] is None


def test_cosine_distance():
    a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    assert cosine_distance(a, a) == pytest.approx(0.0, abs=1e-6)
    assert cosine_distance(a, np.array([0.0, 1.0, 0.0], dtype=np.float32)) == pytest.approx(1.0)
    assert cosine_distance(a, np.array([-1.0, 0.0, 0.0], dtype=np.float32)) == pytest.approx(2.0)
    # A zero vector is not a match, and must not divide by zero.
    assert cosine_distance(a, np.zeros(3, dtype=np.float32)) == 1.0


def test_cosine_distance_ignores_magnitude():
    a = np.array([1.0, 1.0], dtype=np.float32)
    b = np.array([5.0, 5.0], dtype=np.float32)
    assert cosine_distance(a, b) == pytest.approx(0.0, abs=1e-6)


def test_distance_never_goes_negative_for_an_identical_pair():
    # Float error used to make a face matched against itself report -0.0.
    a = np.array([0.3, -0.7, 0.1], dtype=np.float32)
    distance = cosine_distance(a, a.copy())
    assert distance >= 0.0
    assert round(distance, 6) == 0.0  # what /match-faces actually serialises


def test_distance_is_bounded():
    for _ in range(50):
        a = np.random.randn(64).astype(np.float32)
        b = np.random.randn(64).astype(np.float32)
        assert 0.0 <= cosine_distance(a, b) <= 2.0
