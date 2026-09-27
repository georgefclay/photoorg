from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_LABEL_RE = re.compile(r"^[a-z0-9_]+$")
_KIND_VALUES = {"digital", "scan", "contrib"}


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
    CLEANUP_DIR: Path = Field(
        default=Path(r"D:\PhotoArchive\cleanup"),
        description="Phase 7 cleanup proposals: previews, on-demand full-res renders, reports",
    )

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

    # --- Cleanup (Phase 7) ------------------------------------------------
    # Analysis runs on a downscale of this long edge; everything is applied
    # at full resolution.
    CLEANUP_ANALYSE_EDGE: int = Field(default=2000, ge=400)
    # The print must cover at least this fraction of the scan, or the
    # analyser refuses to guess (needs_manual).
    CLEANUP_MIN_PRINT_FRAC: float = Field(default=0.40, gt=0.0, le=1.0)
    # Long/short ratio above this is not a print shape (needs_manual).
    CLEANUP_MAX_ASPECT: float = Field(default=3.0, gt=1.0)
    # Deskew below this is noise; above CLEANUP_MAX_DESKEW_DEG is not a
    # skewed print (needs_manual). The bbox transform preserves face-box
    # size, which is only honest for small angles — see geometry.py.
    CLEANUP_DESKEW_MIN_DEG: float = Field(default=0.3, ge=0.0)
    CLEANUP_MAX_DESKEW_DEG: float = Field(default=15.0, gt=0.0)
    # A crop that removes less than this fraction of the area isn't worth a
    # new file version.
    CLEANUP_CROP_MIN_FRAC: float = Field(default=0.01, ge=0.0, le=1.0)
    # Crop inset, in pixels at 300 DPI. Scaled by the master's DPI when
    # known (so 16 px at 1200 DPI); this value is used as-is otherwise.
    CLEANUP_CROP_INSET_PX_AT_300: float = Field(default=4.0, ge=0.0)
    # A component must cover this fraction of the scan to count as a print
    # in a multi-print split.
    CLEANUP_SPLIT_MIN_FRAC: float = Field(default=0.12, gt=0.0, le=1.0)
    # component area / minAreaRect area — how rectangular a component must be.
    CLEANUP_SPLIT_RECTANGULARITY: float = Field(default=0.80, gt=0.0, le=1.0)
    # Colour cast: Lab a/b distance from neutral, measured on the print's
    # *near-neutral* mid-tones — the least-colourful CLEANUP_CAST_NEUTRAL_PCT
    # per cent of them. An age cast shifts the paper, greys included; scene
    # colour lives in the saturated pixels, so measuring over every mid-tone
    # (plain grey-world) reads a lawn or a warm indoor shot as a cast and
    # over-corrects. Measured on batches 1-5: grey-world fired on 270 of 444
    # scans at a median magnitude of 14.2; this estimator fires on 168 at 6.4,
    # and 152 of the 270 were overstated more than twofold.
    # 100 restores plain grey-world.
    CLEANUP_CAST_NEUTRAL_PCT: float = Field(default=40.0, gt=0.0, le=100.0)
    CLEANUP_CAST_MIN: float = Field(default=6.0, ge=0.0)
    # Fix-up 1. An age cast shifts the whole print, paper white included; a
    # scene colour (a lawn, a warm lamp, a beige wall) shifts the mid-tones
    # only. So the cast is measured twice — on the neutral mid-tones and on
    # the print's near-white highlights — and corrected only when the two
    # agree. Highlights are the top (100 - CLEANUP_CAST_HIGHLIGHT_PCT) per
    # cent of luminance inside the inset rect, blown pixels excluded.
    CLEANUP_CAST_HIGHLIGHT_PCT: float = Field(default=97.0, gt=0.0, lt=100.0)
    # The highlight cast must be at least this fraction of the mid-tone one,
    # and point the same way, or the colour op is skipped as scene colour.
    CLEANUP_CAST_HIGHLIGHT_AGREE: float = Field(default=0.5, ge=0.0, le=1.0)
    # Restorers under-correct on purpose: a print that keeps 30 % of its
    # warmth still looks like an old photo, while one pushed past neutral
    # looks wrong instantly. Gains are blended toward 1.0 by this factor.
    # 1.0 restores full correction.
    CLEANUP_CAST_STRENGTH: float = Field(default=0.7, gt=0.0, le=1.0)
    # 95th-percentile Lab chroma below this is a B&W print, not a cast.
    CLEANUP_MONO_CHROMA_MAX: float = Field(default=12.0, ge=0.0)
    # Sepia: high chroma but almost no hue spread (circular variance).
    CLEANUP_SEPIA_HUE_VAR_MAX: float = Field(default=0.05, ge=0.0, le=1.0)
    # Levels: dynamic range (p99.5 - p0.5) below this fraction of full scale
    # is a faded print.
    CLEANUP_CONTRAST_LOW: float = Field(default=0.72, gt=0.0, le=1.0)
    # Strength of the mild S-curve applied with the percentile stretch.
    CLEANUP_SCURVE: float = Field(default=0.12, ge=0.0, le=1.0)

    # Remote enhance. `null` disables the E key; `claid` needs CLAID_API_KEY.
    CLEANUP_REMOTE_PROVIDER: str = Field(default="null")
    CLAID_API_KEY: str = Field(default="")
    CLAID_BASE_URL: str = Field(default="https://api.claid.ai")
    CLAID_COST_PER_OP_USD: float = Field(default=0.03, ge=0.0)

    @field_validator("CLEANUP_REMOTE_PROVIDER")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        v = (v or "null").strip().lower()
        if v not in {"null", "claid"}:
            raise ValueError(
                f"CLEANUP_REMOTE_PROVIDER {v!r} must be one of null, claid"
            )
        return v

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
            "CLEANUP_DIR": _norm(self.CLEANUP_DIR),
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
