from __future__ import annotations

import pytest

from photoarchive.config import _parse_master_roots


def test_parse_two_roots_default_kind():
    roots = list(_parse_master_roots(r"photos=D:\Photos;scans=D:\Scanned Photos|scan"))
    assert [r.label for r in roots] == ["photos", "scans"]
    assert roots[0].kind == "digital"
    assert roots[1].kind == "scan"
    assert str(roots[0].path).lower().endswith("photos")


def test_parse_ignores_whitespace_and_trailing_semicolon():
    roots = list(_parse_master_roots(
        r"  photos = D:\Photos ; scans = D:\Scanned Photos | scan ;  "
    ))
    assert [r.label for r in roots] == ["photos", "scans"]


def test_reject_bad_label():
    with pytest.raises(ValueError, match="label"):
        list(_parse_master_roots(r"Photos=D:\Photos"))  # uppercase not allowed


def test_reject_duplicate_label():
    with pytest.raises(ValueError, match="duplicated"):
        list(_parse_master_roots(r"photos=D:\a;photos=D:\b"))


def test_reject_bad_kind():
    with pytest.raises(ValueError, match="invalid kind"):
        list(_parse_master_roots(r"navy=E:\Navy|weird"))


def test_reject_missing_equals():
    with pytest.raises(ValueError, match="missing '='"):
        list(_parse_master_roots(r"photosD:\Photos"))
