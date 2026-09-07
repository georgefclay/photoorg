from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageOps

JPEG_QUALITY = 85


def prepare_jpeg(path: Path, max_edge: int) -> bytes:
    """Read `path`, downscale so the longest edge is `max_edge`, and encode as
    JPEG at Q85. EXIF orientation is applied first so the mini sees the image
    the right way up."""
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        w, h = im.size
        edge = max(w, h)
        if edge > max_edge:
            scale = max_edge / edge
            new = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
            im = im.resize(new, Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return buf.getvalue()


def prepare_jpeg_bytes(data: bytes, max_edge: int) -> bytes:
    """Same as prepare_jpeg but from an in-memory JPEG (e.g. a face-crop)."""
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        w, h = im.size
        edge = max(w, h)
        if edge > max_edge:
            scale = max_edge / edge
            new = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
            im = im.resize(new, Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return buf.getvalue()
