"""Rotated / mirrored variants of a thumbnail's pHash and dHash.

Scans get put on the flatbed any way up. To catch a rotated or mirrored
duplicate we compute the perceptual hashes of seven transforms of the
thumbnail and match every variant against the identity hashes of the rest.

Variants (labels used in `dedupe_members.transform`):
    identity, mirror, rot90, rot180, rot270, rot90+mirror, rot270+mirror

Note: rot180+mirror == mirror composed with rot180 == "flip vertically",
which is the same pixel set as rot180 of mirror. We omit it because it
is the composition rot180 * mirror, i.e. redundant with matching mirror
against rot180 or vice versa. The seven listed variants cover the eight
elements of the dihedral group D4 up to the symmetry that swap identity
with rot180+mirror; every distinct rotation/reflection is represented.
"""
from __future__ import annotations

from pathlib import Path

import imagehash
from PIL import Image, ImageOps

from .index import hex_to_int


TRANSFORMS = (
    "identity",
    "mirror",
    "rot90",
    "rot180",
    "rot270",
    "rot90+mirror",
    "rot270+mirror",
)


def _apply(image: Image.Image, transform: str) -> Image.Image:
    if transform == "identity":
        return image
    if transform == "mirror":
        return ImageOps.mirror(image)
    if transform == "rot90":
        return image.rotate(90, expand=True)
    if transform == "rot180":
        return image.rotate(180, expand=True)
    if transform == "rot270":
        return image.rotate(270, expand=True)
    if transform == "rot90+mirror":
        return ImageOps.mirror(image.rotate(90, expand=True))
    if transform == "rot270+mirror":
        return ImageOps.mirror(image.rotate(270, expand=True))
    raise ValueError(f"unknown transform {transform!r}")


def variant_hashes(image: Image.Image) -> dict[str, tuple[int, int]]:
    """Return {transform_name: (phash_int, dhash_int)} for all seven
    transforms. The image is EXIF-transposed first, matching how
    `ingest.hasher.perceptual_hashes` computed the identity hash.
    """
    base = ImageOps.exif_transpose(image)
    out: dict[str, tuple[int, int]] = {}
    for t in TRANSFORMS:
        img = _apply(base, t)
        p = imagehash.phash(img, hash_size=16)
        d = imagehash.dhash(img, hash_size=16)
        out[t] = (hex_to_int(str(p)), hex_to_int(str(d)))
    return out


def variant_hashes_from_thumb(thumb_path: Path) -> dict[str, tuple[int, int]]:
    """Convenience wrapper: open the thumbnail, close the file, return
    the seven variants.
    """
    with Image.open(thumb_path) as im:
        im.load()
        return variant_hashes(im)
