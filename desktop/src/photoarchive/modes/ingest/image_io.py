from __future__ import annotations

from pathlib import Path

from PIL import Image

# Register HEIF/HEIC support if available. Pillow's PNG/JPEG/TIFF are built-in.
try:
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pass


def open_image(path: Path) -> Image.Image:
    """Open the master file with Pillow. Caller must close (use with-statement)."""
    return Image.open(path)


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
