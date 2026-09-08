from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_LABEL_RE = re.compile(r"^[a-z0-9_]+$")
_KIND_VALUES = {"digital", "scan"}


@dataclass(frozen=True)
class MasterRoot:
    """One master directory. `kind` decides whether ingest runs scan-only
    steps (batch/sequence, back detect, rescan detect, folder-name album).
    """

    label: str
    path: Path
    kind: str  # "digital" | "scan"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Semicolon-separated list of `label=path[|kind]`. Example:
    #   MASTER_ROOTS=photos=D:\Photos;scans=D:\Scanned Photos|scan
    # Kind defaults to `digital`. `|scan` opts a root into scan-only handling.
    MASTER_ROOTS: str = Field(..., description="Master roots list; see config.py")

    WORKING_DIR: Path = Field(..., description="Derived working copies (writable)")
    QUARANTINE_DIR: Path = Field(..., description="Soft-deleted files (writable)")
    MANUAL_FIX_DIR: Path = Field(..., description="Cleanup rejects awaiting manual attention (writable)")
    THUMBS_DIR: Path = Field(..., description="Thumbnail cache (writable)")

    DATABASE_URL: str
    INFERENCE_URL: str
    INFERENCE_TOKEN: str
    WEB_API_URL: str
    WEB_API_TOKEN: str

    # Dedupe (Phase 4). Two 256-bit perceptual hashes; a pair is a candidate
    # when either Hamming distance is at or below its threshold.
    DEDUPE_PHASH_MAX: int = Field(default=10, ge=0, le=256)
    DEDUPE_DHASH_MAX: int = Field(default=10, ge=0, le=256)

    # Faces (Phase 6). Agglomerative average-linkage cosine-distance
    # threshold for the in-memory clustering the Faces mode runs on
    # unlabelled embeddings. 0.45 is the starting point; tune on real data.
    FACE_CLUSTER_DIST: float = Field(default=0.45, ge=0.0, le=2.0)

    # Fix-up 2: quality gate. Faces below either threshold (InsightFace
    # detection score / short-edge in pixels) are excluded from
    # clustering AND from reference-set means so they don't chain the
    # rest into one giant cluster. Rows are kept and reachable via the
    # Faces mode's "Include low-quality" toggle.
    FACE_MIN_SCORE: float = Field(default=0.7, ge=0.0, le=1.0)
    FACE_MIN_PX: int = Field(default=40, ge=1)

    # Fix-up 2: recursive split. Any cluster larger than this cap gets
    # re-clustered on its own members at a tighter threshold
    # (× 0.8 per level), iteratively, so a single mega-cluster becomes
    # several plausible-sized ones.
    FACE_MAX_CLUSTER: int = Field(default=300, ge=1)

    @field_validator("MASTER_ROOTS")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("MASTER_ROOTS must not be empty")
        return v

    @property
    def master_roots(self) -> list[MasterRoot]:
        return list(_parse_master_roots(self.MASTER_ROOTS))

    def master_root_by_label(self, label: str) -> MasterRoot | None:
        for root in self.master_roots:
            if root.label == label:
                return root
        return None

    def check_masters_isolation(self) -> None:
        """Refuse to start if any master root overlaps a writable directory."""
        writable = {
            "WORKING_DIR": _norm(self.WORKING_DIR),
            "QUARANTINE_DIR": _norm(self.QUARANTINE_DIR),
            "MANUAL_FIX_DIR": _norm(self.MANUAL_FIX_DIR),
            "THUMBS_DIR": _norm(self.THUMBS_DIR),
        }
        for root in self.master_roots:
            master_path = _norm(root.path)
            for writable_name, writable_path in writable.items():
                if _overlaps(master_path, writable_path):
                    raise RuntimeError(
                        f"Refusing to start: master root '{root.label}' = "
                        f"{master_path} overlaps with {writable_name}="
                        f"{writable_path}. Masters must be isolated from "
                        "writable directories."
                    )


def _parse_master_roots(raw: str) -> Iterator[MasterRoot]:
    seen_labels: set[str] = set()
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(
                f"MASTER_ROOTS entry {chunk!r} is missing '=': "
                "expected label=path[|kind]"
            )
        label, rest = chunk.split("=", 1)
        label = label.strip()
        rest = rest.strip()
        if not _LABEL_RE.match(label):
            raise ValueError(
                f"MASTER_ROOTS label {label!r} must match [a-z0-9_]+"
            )
        if label in seen_labels:
            raise ValueError(f"MASTER_ROOTS label {label!r} is duplicated")
        seen_labels.add(label)
        if "|" in rest:
            path_str, kind = rest.rsplit("|", 1)
            kind = kind.strip().lower()
        else:
            path_str, kind = rest, "digital"
        path_str = path_str.strip()
        if not path_str:
            raise ValueError(f"MASTER_ROOTS entry {label!r} has empty path")
        if kind not in _KIND_VALUES:
            raise ValueError(
                f"MASTER_ROOTS entry {label!r} has invalid kind {kind!r}; "
                f"expected one of {sorted(_KIND_VALUES)}"
            )
        yield MasterRoot(label=label, path=Path(path_str), kind=kind)


def _norm(p: Path) -> Path:
    return Path(str(p).lower()).resolve() if _is_windows() else p.resolve()


def _is_windows() -> bool:
    import sys as _sys
    return _sys.platform.startswith("win")


def _overlaps(a: Path, b: Path) -> bool:
    if a == b:
        return True
    try:
        a.relative_to(b)
        return True
    except ValueError:
        pass
    try:
        b.relative_to(a)
        return True
    except ValueError:
        pass
    return False


def load() -> Settings:
    settings = Settings()
    settings.check_masters_isolation()
    return settings
