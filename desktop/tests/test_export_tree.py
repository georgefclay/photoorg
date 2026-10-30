"""Phase 12 — the export tree, against the fixtures the web's zip reads too.

``shared/zip-tree-fixtures.json`` is the single statement of the tree
rules; ``web/test/zip-tree.test.js`` runs the same cases against the JS
port. No database.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from photoarchive import export_tree as tree

FIX = json.loads(
    (Path(__file__).resolve().parents[2] / "shared" / "zip-tree-fixtures.json").read_text("utf-8")
)


@pytest.mark.parametrize("case", FIX["paths"], ids=lambda c: c["name"])
def test_path(case):
    assert tree.photo_path(case["in"]) == case["out"]


@pytest.mark.parametrize("case", FIX["people_folders"], ids=lambda c: c["name"])
def test_people_folders(case):
    got = {str(k): v for k, v in tree.people_folders(case["in"]).items()}
    assert got == case["out"]


@pytest.mark.parametrize("case", FIX["people_paths"], ids=lambda c: c["name"])
def test_people_path(case):
    assert tree.people_path(case["in"]["photo"], case["in"]["folder"]) == case["out"]


def test_a_date_object_places_the_same_as_its_string():
    p = {"id": 1, "ext": "jpg", "capture_date": dt.date(1962, 7, 4),
         "precision": "exact", "confirmed": True}
    assert tree.photo_path(p) == "1962/07/1962-07-04_00000001.jpg"
