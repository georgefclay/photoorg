from __future__ import annotations

import hashlib
from pathlib import Path

import imagehash
from PIL import Image, ImageOps


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def perceptual_hashes(image: Image.Image) -> tuple[str, str]:
    """pHash and dHash (both hash_size=16 → 256-bit hex) on orientation-
    corrected copy. Convert to grayscale via imagehash internals."""
    img = ImageOps.exif_transpose(image)
    phash = imagehash.phash(img, hash_size=16)
    dhash = imagehash.dhash(img, hash_size=16)
    return str(phash), str(dhash)


def phash_hex_to_bits(h: str) -> int:
    """Return int form of a 256-bit hex hash; hamming = bin(a^b).count('1')."""
    return int(h, 16)


def hamming(a_hex: str, b_hex: str) -> int:
    return bin(phash_hex_to_bits(a_hex) ^ phash_hex_to_bits(b_hex)).count("1")
