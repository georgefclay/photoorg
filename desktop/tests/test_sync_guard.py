"""Masters-guard tests for the new `contrib` kind.

The `contrib` root is append-only: committed files under it (including
subfolders that were once `_incoming` and then renamed to
`<uploader>/<contribution_id>/`) must be read-only, but the guard
must NOT flag a writable `_incoming/` folder as a violation, because
the Sync pull needs to create files there.
"""
from __future__ import annotations

import os
import sys

import pytest

from photoarchive.config import MasterRoot
from photoarchive.modes.ingest.guard import run_masters_guard


def test_contrib_root_ignores_incoming_when_picking_probe(tmp_path):
    """A contrib root with only an _incoming/ subfolder must not be
    reported as writable via its probe subfolder — the guard has to
    skip _incoming entirely."""
    # Make root itself read-only-ish by putting our probe attempt at the
    # root level; on Windows without icacls we can't guarantee it's
    # actually read-only, so this test focuses on the probe-selection
    # logic rather than the write result.
    incoming = tmp_path / "_incoming"
    incoming.mkdir()
    (incoming / "42").mkdir()  # simulate an in-progress pull

    root = MasterRoot(label="contrib", path=tmp_path, kind="contrib")
    result = run_masters_guard([root])
    # We can't assert all_read_only on a tmp_path we can write to, but we
    # can assert the sub_path picked is NOT anywhere under _incoming.
    per = result.per_root[0]
    if per.sub_path is not None:
        rel = os.path.relpath(per.sub_path, tmp_path)
        assert "_incoming" not in rel.split(os.sep), (
            f"Guard picked _incoming as its probe subfolder: {per.sub_path}"
        )


def test_contrib_root_with_committed_folder_probes_committed(tmp_path):
    """A contrib root with both _incoming/ and a committed uploader
    subfolder should probe the committed subfolder — that's the one
    that must be read-only."""
    (tmp_path / "_incoming").mkdir()
    (tmp_path / "alice").mkdir()
    (tmp_path / "alice" / "17").mkdir()

    root = MasterRoot(label="contrib", path=tmp_path, kind="contrib")
    result = run_masters_guard([root])
    per = result.per_root[0]
    if per.sub_path is not None:
        rel = os.path.relpath(per.sub_path, tmp_path)
        assert "_incoming" not in rel.split(os.sep)


def test_kind_contrib_accepted_by_config_parser(tmp_path):
    """Just verifying the enum widened correctly."""
    from photoarchive.config import _parse_master_roots  # type: ignore

    roots = list(_parse_master_roots(f"a=D:\\Foo|digital;b=D:\\Bar|scan;c=D:\\Baz|contrib"))
    kinds = {r.label: r.kind for r in roots}
    assert kinds == {"a": "digital", "b": "scan", "c": "contrib"}
