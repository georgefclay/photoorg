"""Rotation/mirror variant hashes: verify that a rotated/mirrored copy
of an image, hashed as an identity, matches the source image's variant
for that transform.
"""
from __future__ import annotations

from PIL import Image, ImageDraw

from photoarchive.modes.dedupe.index import hamming_int, hex_to_int
from photoarchive.modes.dedupe.variants import TRANSFORMS, variant_hashes
import imagehash


def _make_test_image(seed: int = 1) -> Image.Image:
    """Deterministic 320x240 image with enough structure that pHash and
    dHash produce meaningful values.
    """
    img = Image.new("RGB", (320, 240), (30, 30, 30))
    draw = ImageDraw.Draw(img)
    for i in range(0, 320, 20):
        color = (i % 255, (i * seed) % 255, (i + 40) % 255)
        draw.rectangle((i, 0, i + 20, 240), fill=color)
    for j in range(0, 240, 30):
        draw.line((0, j, 320, j), fill=(200, 200, 200), width=2)
    draw.ellipse((100, 60, 220, 180), outline=(255, 255, 0), width=3)
    return img


def _identity_phash_hex(img: Image.Image) -> str:
    from PIL import ImageOps
    return str(imagehash.phash(ImageOps.exif_transpose(img), hash_size=16))


def _apply(img: Image.Image, transform: str) -> Image.Image:
    from PIL import ImageOps
    if transform == "identity":
        return img
    if transform == "mirror":
        return ImageOps.mirror(img)
    if transform == "rot90":
        return img.rotate(90, expand=True)
    if transform == "rot180":
        return img.rotate(180, expand=True)
    if transform == "rot270":
        return img.rotate(270, expand=True)
    if transform == "rot90+mirror":
        return ImageOps.mirror(img.rotate(90, expand=True))
    if transform == "rot270+mirror":
        return ImageOps.mirror(img.rotate(270, expand=True))
    raise ValueError(transform)


def test_variant_hash_matches_hashed_rotated_copy():
    """For each transform T, the source's variant-T hash should equal
    the phash of a copy that has been actually rotated by T. Same for
    dhash.
    """
    src = _make_test_image()
    src_variants = variant_hashes(src)

    for t in TRANSFORMS:
        rotated = _apply(src, t)
        rot_identity_p_hex = _identity_phash_hex(rotated)
        rot_identity_p = hex_to_int(rot_identity_p_hex)
        variant_p, variant_d = src_variants[t]
        # phashes should be identical for a perfect rotation of the same
        # image (no interpolation loss for 90-degree multiples on
        # rectangular images; mirror is bit-perfect).
        assert hamming_int(variant_p, rot_identity_p) == 0, (
            f"{t} phash mismatch: variant={variant_p:x} identity={rot_identity_p:x}"
        )


def test_variants_dictionary_has_all_seven_transforms():
    src = _make_test_image()
    result = variant_hashes(src)
    assert set(result.keys()) == set(TRANSFORMS)


def test_rotated_scan_finds_original_via_multi_index():
    """End-to-end: two "photos", one is a rotated copy of the other.
    Indexing all seven of each into a MultiIndex and querying the
    identity of one against the other's variants finds the pair at
    distance 0.
    """
    from photoarchive.modes.dedupe.index import MultiIndex

    a = _make_test_image(seed=2)
    b = a.rotate(180, expand=True)

    va = variant_hashes(a)
    vb = variant_hashes(b)
    items = []
    for pid, vs in ((1, va), (2, vb)):
        for t, (p, _d) in vs.items():
            items.append(((pid, t), p))
    idx = MultiIndex.build(items)

    # a's identity hash queried against the index should find b's rot180.
    a_identity = va["identity"][0]
    hits = dict(idx.query(a_identity, 10))
    found_b_rot180 = any(
        key[0] == 2 and key[1] == "rot180" and dist == 0
        for key, dist in hits.items()
    )
    assert found_b_rot180
