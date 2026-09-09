from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

# Register HEIF/HEIC support if available. Pillow's PNG/JPEG/TIFF are built-in.
try:
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pass


def open_image(path: Path) -> Image.Image:
    """Open the master file with Pillow. Caller must close (use with-statement)."""
    return Image.open(path)


@dataclass(frozen=True)
class ImageDims:
    """Raw file dimensions vs. the EXIF-transposed (display) dimensions
    the rest of the pipeline consumes. `photos.width/height` are the
    display values; `photo_masters.width/height` are the raw values.
    See CLAUDE.md 'Faces mode / fix-up 6'.
    """
    master_width: int
    master_height: int
    display_width: int
    display_height: int
    orientation: int | None  # EXIF value 1..8, None if untagged


def probe_image(image: Image.Image) -> ImageDims:
    """Compute raw + display dims + orientation from an open Pillow image."""
    raw_w, raw_h = image.size
    exif_orientation = _read_orientation(image)
    if exif_orientation in (5, 6, 7, 8):
        disp_w, disp_h = raw_h, raw_w
    else:
        disp_w, disp_h = raw_w, raw_h
    return ImageDims(
        master_width=raw_w, master_height=raw_h,
        display_width=disp_w, display_height=disp_h,
        orientation=exif_orientation,
    )


def _read_orientation(image: Image.Image) -> int | None:
    try:
        exif = image.getexif()
    except Exception:
        return None
    if not exif:
        return None
    val = exif.get(0x0112)  # ExifTags.Base.Orientation
    if val is None:
        return None
    try:
        v = int(val)
    except (TypeError, ValueError):
        return None
    return v if 1 <= v <= 8 else None


def mime_for_ext(ext: str) -> str:
    ext = ext.lower().lstrip(".")
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "tif": "image/tiff",
        "tiff": "image/tiff",
        "heic": "image/heic",
        "mp4": "video/mp4",
        "mov": "video/quicktime",
    }.get(ext, "application/octet-stream")
