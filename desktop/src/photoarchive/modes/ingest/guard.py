from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from ...config import MasterRoot

log = logging.getLogger(__name__)

PROBE_NAME = "._photoarchive_write_probe"


@dataclass
class RootGuardResult:
    label: str
    path: Path
    root_writable: bool
    sub_writable: bool
    sub_path: Path | None
    error: str | None = None

    @property
    def writable(self) -> bool:
        return self.root_writable or self.sub_writable

    @property
    def ok_read_only(self) -> bool:
        """Passes only when the root exists AND is read-only. A missing root
        (offline drive, wrong path) does NOT pass."""
        return self.error is None and not self.writable


@dataclass
class GuardResult:
    checked_at: str
    per_root: list[RootGuardResult] = field(default_factory=list)

    @property
    def all_read_only(self) -> bool:
        """True only when every root exists AND is read-only."""
        return bool(self.per_root) and all(r.ok_read_only for r in self.per_root)

    @property
    def writable_labels(self) -> list[str]:
        return [r.label for r in self.per_root if r.writable]

    @property
    def missing_labels(self) -> list[str]:
        return [r.label for r in self.per_root if r.error is not None]

    def to_params(self) -> dict:
        return {
            "checked_at": self.checked_at,
            "per_root": [
                {
                    "label": r.label,
                    "path": str(r.path),
                    "root_writable": r.root_writable,
                    "sub_writable": r.sub_writable,
                    "sub_path": str(r.sub_path) if r.sub_path else None,
                    "error": r.error,
                }
                for r in self.per_root
            ],
            "all_read_only": self.all_read_only,
        }


def run_masters_guard(roots: Sequence[MasterRoot]) -> GuardResult:
    """Probe each root and one random subfolder. Success = writable = BAD."""
    result = GuardResult(checked_at=datetime.now(timezone.utc).isoformat())
    for root in roots:
        r = _probe_root(root)
        result.per_root.append(r)
        if r.error is not None:
            log.warning("Masters guard: root '%s' — %s", root.label, r.error)
        elif r.writable:
            log.warning(
                "Masters guard: root '%s' at %s is WRITABLE — ingest will refuse to run.",
                root.label, root.path,
            )
        else:
            log.info("Masters guard: root '%s' is read-only.", root.label)
    return result


def _probe_root(root: MasterRoot) -> RootGuardResult:
    r = RootGuardResult(label=root.label, path=root.path,
                        root_writable=False, sub_writable=False, sub_path=None)
    if not root.path.exists() or not root.path.is_dir():
        r.error = f"Root does not exist or is not a directory: {root.path}"
        return r

    r.root_writable = _try_write(root.path)

    subs: list[Path] = []
    try:
        for entry in root.path.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                subs.append(entry)
    except OSError as e:
        r.error = f"Could not list root: {e}"
        return r

    if subs:
        sub = random.choice(subs)
        r.sub_path = sub
        r.sub_writable = _try_write(sub)
    return r


def _try_write(dir_path: Path) -> bool:
    probe = dir_path / PROBE_NAME
    try:
        with open(probe, "wb") as f:
            f.write(b"probe")
    except OSError:
        return False
    else:
        try:
            probe.unlink()
        except OSError:
            log.warning("Could not remove probe file %s", probe)
        return True


def remediation_message(result: GuardResult) -> str:
    """Human-readable message with the exact icacls command to run."""
    if result.all_read_only:
        return "All master roots are read-only. Guard OK."
    lines = ["Masters guard refused ingest:"]
    missing = [r for r in result.per_root if r.error is not None]
    if missing:
        lines.append("")
        lines.append("Missing / unreachable roots:")
        for r in missing:
            lines.append(f"  {r.label}: {r.error}")
        lines.append("(Mount the drive or fix MASTER_ROOTS in desktop/.env.)")
    writable = [r for r in result.per_root if r.writable]
    if writable:
        lines += [
            "",
            "Writable roots. Run these in an elevated PowerShell (Run as administrator):",
            "",
        ]
        for r in writable:
            cmd = f'icacls "{r.path}" /deny "%USERNAME%:(OI)(CI)(WD,AD,DC)"'
            lines.append(f"  {cmd}")
        lines += [
            "",
            "To later add new files to a root, temporarily lift the deny:",
            '  icacls "<root>" /remove:d "%USERNAME%"',
            "…add your files, then re-apply the deny above.",
            "",
            "Note: `attrib +R` on a directory is advisory and does NOT block writes; "
            "use the icacls deny above.",
        ]
    return "\n".join(lines)
