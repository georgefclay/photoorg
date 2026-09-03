from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from photoarchive.config import MasterRoot
from photoarchive.modes.ingest.guard import remediation_message, run_masters_guard


def test_writable_root_is_refused(tmp_path):
    (tmp_path / "sub").mkdir()
    root = MasterRoot(label="tmp", path=tmp_path, kind="scan")
    result = run_masters_guard([root])
    assert not result.all_read_only
    assert "tmp" in result.writable_labels
    msg = remediation_message(result)
    assert "icacls" in msg
    assert "attrib +R" in msg


def test_missing_root_is_refused(tmp_path):
    """A drive that's not mounted must not pass as 'read-only'."""
    root = MasterRoot(label="ghost", path=tmp_path / "does-not-exist", kind="scan")
    result = run_masters_guard([root])
    assert not result.all_read_only
    assert "ghost" in result.missing_labels
    msg = remediation_message(result)
    assert "Missing" in msg or "does not exist" in msg


@pytest.mark.skipif(sys.platform != "win32", reason="Windows icacls test")
def test_windows_read_only_root_passes(tmp_path):
    # Use icacls to deny writes for the current user; requires the user has
    # rights to alter DACLs on their own temp dir (default in %TEMP%).
    (tmp_path / "sub").mkdir()
    user = os.environ.get("USERNAME")
    if not user:
        pytest.skip("no USERNAME env")
    subprocess.run(
        ["icacls", str(tmp_path), "/deny", f"{user}:(OI)(CI)(WD,AD,DC)"],
        check=True, capture_output=True,
    )
    try:
        root = MasterRoot(label="ro", path=tmp_path, kind="scan")
        result = run_masters_guard([root])
        assert result.all_read_only, remediation_message(result)
    finally:
        subprocess.run(
            ["icacls", str(tmp_path), "/remove:d", user],
            check=False, capture_output=True,
        )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX chmod test")
def test_posix_read_only_root_passes(tmp_path):
    (tmp_path / "sub").mkdir()
    os.chmod(tmp_path, 0o500)
    os.chmod(tmp_path / "sub", 0o500)
    try:
        root = MasterRoot(label="ro", path=tmp_path, kind="scan")
        result = run_masters_guard([root])
        assert result.all_read_only, remediation_message(result)
    finally:
        os.chmod(tmp_path, 0o700)
        os.chmod(tmp_path / "sub", 0o700)
