"""Tests for the contribution-pull writer's append-only invariant.

We simulate a `contrib` master root with a committed file plus a fresh
pull. The pull staging should land under `_incoming/`, then rename
into `<uploader>/<contribution_id>/`. Verifying:
  1. `snapshot_contrib_root` skips `_incoming/` completely.
  2. A pull whose file writes only under `_incoming/` before rename
     leaves the pre/post committed-area snapshots identical.
  3. If someone touched a committed file mid-pull, the writer raises.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from photoarchive.modes.sync.pull import ContribManifest, snapshot_contrib_root


def test_snapshot_ignores_incoming(tmp_path: Path) -> None:
    (tmp_path / "alice" / "17").mkdir(parents=True)
    (tmp_path / "alice" / "17" / "1.jpg").write_bytes(b"real committed file")
    (tmp_path / "_incoming" / "42").mkdir(parents=True)
    (tmp_path / "_incoming" / "42" / "1.jpg").write_bytes(b"in-progress pull")

    mf = snapshot_contrib_root(tmp_path)
    assert "alice/17/1.jpg" in mf.files
    for k in mf.files:
        assert not k.startswith("_incoming/"), k


def test_manifest_equals_after_staging_only_changes(tmp_path: Path) -> None:
    (tmp_path / "alice" / "17").mkdir(parents=True)
    (tmp_path / "alice" / "17" / "1.jpg").write_bytes(b"real committed")
    pre = snapshot_contrib_root(tmp_path)

    # Simulate a staging write under _incoming/…
    (tmp_path / "_incoming" / "42").mkdir(parents=True)
    (tmp_path / "_incoming" / "42" / "1.jpg").write_bytes(b"new file")

    post = snapshot_contrib_root(tmp_path)
    assert pre.equals(post), (
        "Writing under _incoming/ must not perturb the committed-area snapshot"
    )


def test_manifest_differs_when_committed_file_touched(tmp_path: Path) -> None:
    (tmp_path / "alice" / "17").mkdir(parents=True)
    committed = tmp_path / "alice" / "17" / "1.jpg"
    committed.write_bytes(b"real committed")
    pre = snapshot_contrib_root(tmp_path)

    # Someone (or something buggy) rewrote a committed file.
    committed.write_bytes(b"NEW BYTES SAME PATH")
    post = snapshot_contrib_root(tmp_path)
    assert not pre.equals(post), (
        "The committed-file mutation must be detected by the manifest"
    )
