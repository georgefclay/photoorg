from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageOps

log = logging.getLogger(__name__)

THUMB_LONG_EDGE = 320


def write_thumb(image: Image.Image, out_path: Path) -> None:
    img = ImageOps.exif_transpose(image).convert("RGB")
    img.thumbnail((THUMB_LONG_EDGE, THUMB_LONG_EDGE), Image.LANCZOS)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "JPEG", quality=85, optimize=True)
