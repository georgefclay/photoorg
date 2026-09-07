"""Image intake: multipart bytes or a path under SHARED_ROOT. Never both."""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, status
from PIL import Image, ImageOps

from .config import get_settings

Image.MAX_IMAGE_PIXELS = 400_000_000  # 1200 DPI TIFF scans are legitimately huge


@dataclass
class LoadedImage:
    """An image ready for a model, plus what it takes to map back to the original."""

    image: Image.Image  # RGB, EXIF-oriented, downscaled to <= max_edge
    original_w: int  # pixel size of the ORIGINAL, after EXIF orientation
    original_h: int
    scale: float  # multiply model-space coords by this to get original coords
    nbytes: int
    source: str  # "upload" or "path"


def resolve_shared_path(raw: str) -> Path:
    """Resolve a caller-supplied path inside SHARED_ROOT, or refuse."""
    root = get_settings().shared_root_path
    if root is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SHARED_ROOT is not configured; use a multipart upload",
        )
    if not raw or not raw.strip():
        raise HTTPException(status_code=400, detail="Empty path")

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    # resolve() follows symlinks, so a symlink pointing out of the root is caught.
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise HTTPException(
            status_code=400, detail="Path is outside SHARED_ROOT"
        )
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail=f"No such file: {raw}")
    return resolved


def _prepare(img: Image.Image, nbytes: int, source: str) -> LoadedImage:
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
    ow, oh = img.size
    max_edge = get_settings().max_image_edge
    longest = max(ow, oh)
    if longest > max_edge:
        ratio = max_edge / float(longest)
        img = img.resize(
            (max(1, round(ow * ratio)), max(1, round(oh * ratio))), Image.LANCZOS
        )
    scale = ow / float(img.size[0]) if img.size[0] else 1.0
    return LoadedImage(
        image=img, original_w=ow, original_h=oh, scale=scale, nbytes=nbytes, source=source
    )


def load_from_bytes(data: bytes) -> LoadedImage:
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:  # noqa: BLE001 - any decode failure is a 400
        raise HTTPException(status_code=400, detail=f"Undecodable image: {exc}") from exc
    return _prepare(img, len(data), "upload")


def load_from_path(raw: str) -> LoadedImage:
    resolved = resolve_shared_path(raw)
    try:
        img = Image.open(resolved)
        img.load()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Undecodable image: {exc}") from exc
    return _prepare(img, resolved.stat().st_size, "path")
