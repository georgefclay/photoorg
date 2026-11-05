"""The contributions pull against a contrib root that really is locked.

Every earlier test pulled into an unprotected temp folder, so nothing
noticed that the root's documented deny (WD,AD,DC) forbade the very
`<uploader>/` folder the pull creates. The first real pull (2026-11-04)
failed with "Access is denied: D:\\Contributed\\georgefclay_gmail.com".
These tests put the real icacls denies on a temp root.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from photoarchive.config import MasterRoot
from photoarchive.modes.ingest.guard import (
    _try_write, remediation_message, run_masters_guard, RootGuardResult, GuardResult,
)
from photoarchive.modes.sync.pull import ContribLockError, pull_contributions

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows icacls test")
USER = os.environ.get("USERNAME", "")


class FakeClient:
    def __init__(self, items):
        self.items = items
        self.pulled: list[int] = []

    def pull_contributions(self):
        return {"items": self.items}

    def pull_contribution_file(self, cid, fid, dest: Path):
        dest.write_bytes(f"contribution {cid} file {fid}".encode())

    def mark_contribution_pulled(self, cid):
        self.pulled.append(cid)
        return {"ok": True}


def _item(cid, n=2, email="relative+test@gmail.com"):
    return {"id": cid, "user_email": email, "user_display_name": "Relative",
            "note": "n", "group_ids": [1],
            "files": [{"id": i, "mime": "image/jpeg"} for i in range(1, n + 1)]}


def _icacls(*args, check=True):
    return subprocess.run(["icacls", *map(str, args)], capture_output=True, text=True, check=check)


@pytest.fixture
def contrib_root(tmp_path):
    if not USER:
        pytest.skip("no USERNAME env")
    root = tmp_path / "Contributed"
    (root / "_incoming").mkdir(parents=True)
    yield root
    # Lift every deny so pytest can delete the tree.
    _icacls(root, "/remove:d", USER, "/T", "/C", check=False)


@windows_only
def test_pull_into_a_locked_root_commits_and_locks(contrib_root):
    _icacls(contrib_root, "/deny", f"{USER}:(WD,DC)")
    client = FakeClient([_item(1)])

    assert pull_contributions(client, contrib_root) == (1, 2)

    uploader = contrib_root / "relative_test_gmail.com"
    target = uploader / "1"
    assert sorted(p.name for p in target.iterdir()) == ["1.jpg", "2.jpg", "manifest.json"]
    assert not (contrib_root / "_incoming" / "1").exists()
    assert client.pulled == [1]
    # Committed: no new files anywhere outside _incoming, and the files
    # themselves can't be rewritten or deleted.
    assert not _try_write(contrib_root)
    assert not _try_write(uploader)
    assert not _try_write(target)
    with pytest.raises(PermissionError):
        (target / "1.jpg").write_bytes(b"changed")
    # Deletes can't be tried here: an elevated process (how this suite often
    # runs) deletes straight through a deny. So check the deny itself is on
    # the folder and inherited by every file.
    for path in (target, target / "1.jpg", target / "manifest.json"):
        acl = _icacls(path).stdout
        mine = [ln for ln in acl.splitlines() if f"\{USER}:" in ln and "(DENY)" in ln]
        assert mine, acl
        assert "(DENY)(D,WD,AD,DC)" in mine[0], mine[0]
    # And the masters guard — what ingest actually checks — agrees.
    guard = run_masters_guard([MasterRoot(label="contrib", path=contrib_root, kind="contrib")])
    assert guard.all_read_only, remediation_message(guard)


@windows_only
def test_a_second_batch_from_the_same_uploader_still_lands(contrib_root):
    _icacls(contrib_root, "/deny", f"{USER}:(WD,DC)")
    pull_contributions(FakeClient([_item(1)]), contrib_root)
    client = FakeClient([_item(2, n=1)])

    assert pull_contributions(client, contrib_root) == (1, 1)
    assert (contrib_root / "relative_test_gmail.com" / "2" / "1.jpg").exists()
    assert client.pulled == [2]


@windows_only
def test_the_old_root_deny_fails_with_the_fix_and_leaves_it_unpulled(contrib_root, monkeypatch):
    # The old root deny (WD,AD,DC) refuses the uploader folder only to an
    # ordinary process: an elevated one (how this suite often runs) makes
    # folders through an AD deny. So the refusal is simulated exactly as
    # Windows raised it to George's app.
    real_mkdir = Path.mkdir

    def refusing_mkdir(self, *a, **kw):
        if self.parent == contrib_root and self.name != "_incoming":
            raise PermissionError(13, "Access is denied", str(self))
        return real_mkdir(self, *a, **kw)

    monkeypatch.setattr(Path, "mkdir", refusing_mkdir)
    _icacls(contrib_root, "/deny", f"{USER}:(WD,DC)")
    client = FakeClient([_item(1)])

    with pytest.raises(ContribLockError) as exc:
        pull_contributions(client, contrib_root)

    msg = str(exc.value)
    assert f'icacls "{contrib_root}" /remove:d {USER}' in msg
    assert "(WD,DC)" in msg
    assert client.pulled == []  # the web still offers it next time
    assert (contrib_root / "_incoming" / "1" / "1.jpg").exists()

    # George swaps the deny; the next pull finishes the job.
    monkeypatch.setattr(Path, "mkdir", real_mkdir)
    assert pull_contributions(client, contrib_root) == (1, 2)
    assert client.pulled == [1]


def test_guard_never_tells_a_contrib_root_to_take_an_inherited_deny(tmp_path):
    r = RootGuardResult(label="contrib", path=tmp_path, root_writable=True,
                        sub_writable=True, sub_path=tmp_path / "someone", kind="contrib")
    msg = remediation_message(GuardResult(checked_at="now", per_root=[r]))
    assert "(OI)(CI)" not in msg
    assert "(WD,AD,DC)" not in msg
    assert f'icacls "{tmp_path}" /deny "%USERNAME%:(WD,DC)"' in msg
    assert f'icacls "{tmp_path / "someone"}" /deny "%USERNAME%:(WD,DC)"' in msg
