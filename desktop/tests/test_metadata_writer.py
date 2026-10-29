"""Phase 13 — the metadata writer against the test database and real files.

Every test writes real working copies with the real exiftool and reads them
back with it, because the acceptance rule is "what the dry run says is what
exiftool reads back". Covered:

* masters: a target under a master root (or outside WORKING_DIR) raises
  before a byte is written, and the probe refuses a writable root;
* facts only, the wording, and the date rules, read back from the file;
* the version swap: `_versions/…_v1` keeps the original byte for byte,
  `file_version` bumps, one `metadata.write` audit row;
* pixels: JPEG decodes identically, a 16-bit TIFF's strips are byte-identical;
* face regions land on the face in the stored pixels for orientations 1, 6, 8;
* atomic: a crash before the swap leaves the original intact;
* incremental: a second run writes nothing, one new fact → one file, a
  removal in each soft-delete table → exactly that photo, a pulled rename →
  that person's photos, a replaced working file → re-selected;
* dry run writes nothing.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import date
from pathlib import Path

import numpy as np
import psycopg
import pytest
from PIL import Image, ImageOps

from photoarchive import db as dbmod
from photoarchive.modes.metadata import exiftool as et_mod
from photoarchive.modes.metadata import repo, run as run_mod, writer
from photoarchive.modes.ingest.paths import working_path

from .conftest import TEST_DATABASE_URL
from .phase6_fixtures import _sha, insert_back, phase6  # noqa: F401

_DESKTOP_ENV = Path(__file__).resolve().parents[1] / ".env"


def _exiftool_path() -> str | None:
    if os.environ.get("EXIFTOOL_PATH"):
        return os.environ["EXIFTOOL_PATH"]
    if _DESKTOP_ENV.exists():
        for line in _DESKTOP_ENV.read_text(encoding="utf-8").splitlines():
            if line.startswith("EXIFTOOL_PATH="):
                return line.split("=", 1)[1].strip()
    return None


EXIFTOOL = _exiftool_path()
pytestmark = pytest.mark.skipif(
    not EXIFTOOL or not Path(EXIFTOOL).is_file(), reason="exiftool not installed")


class _FakeGuard:
    all_read_only = True


@pytest.fixture
def mw(phase6, monkeypatch, request):
    """phase6 settings + exiftool; the probe stubbed (a temp dir is writable
    by definition) except in the test that is about the probe."""
    settings = phase6.model_copy(update={"EXIFTOOL_PATH": EXIFTOOL})
    if "refuses" not in request.node.name:
        monkeypatch.setattr(writer, "run_masters_guard", lambda roots: _FakeGuard())
    return settings


# --------------------------------------------------------------------------
# Fixture helpers
# --------------------------------------------------------------------------

def _q(sql, params=(), *, one=False):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        if cur.description is None:
            return None
        return cur.fetchone() if one else cur.fetchall()


def _noise(w, h, seed=1, mode="RGB"):
    rng = np.random.default_rng(seed)
    if mode == "I;16":
        return Image.fromarray(rng.integers(0, 65535, (h, w), dtype=np.uint16))
    return Image.fromarray(rng.integers(0, 255, (h, w, 3), dtype=np.uint8))


def _photo(settings, *, w=400, h=300, fmt="JPEG", image=None, orientation=None,
           is_scan=True, batch="Batch 00012", seq=17, exif_bytes=None, **cols) -> int:
    """A photo row plus its working file at the standard name."""
    sha = _sha()
    ext = "tif" if fmt == "TIFF" else "jpg"
    mime = "image/tiff" if fmt == "TIFF" else "image/jpeg"
    img = image if image is not None else _noise(w, h)
    disp_w, disp_h = (img.size[1], img.size[0]) if orientation in (5, 6, 7, 8) else img.size
    pid = _q("""
        insert into photos (sha256, mime, width, height, is_scan, source_root,
                            source_folder, source_filename, scan_batch, scan_sequence,
                            triage_status, file_version, orientation)
        values (%s, %s, %s, %s, %s, 'm1', %s, %s, %s, %s, 'keep', 1, %s)
        returning id
    """, (sha, mime, disp_w, disp_h, is_scan, batch, f"scan{seq:03d}.{ext}",
          batch if is_scan else None, seq if is_scan else None, orientation), one=True)[0]
    wp = working_path(settings, pid, sha, ext)
    kw = {}
    if fmt == "TIFF":
        kw["compression"] = "tiff_lzw"
    else:
        kw["quality"] = 92
    if orientation:
        ex = Image.Exif()
        ex[0x0112] = orientation
        kw["exif"] = ex.tobytes()
    if exif_bytes is not None:
        kw["exif"] = exif_bytes
    img.save(wp, fmt, **kw)
    _q("insert into photo_masters (photo_id, master_path, sha256, mime, is_preferred) "
       "values (%s, %s, %s, %s, true)", (pid, f"M:\\{sha[:8]}.{ext}", sha, mime))
    _q("update photos set working_path = %s where id = %s", (str(wp), pid))
    for k, v in cols.items():
        _q(f"update photos set {k} = %s where id = %s", (v, pid))
    return pid


def _person(given, surname, *, nickname=None, middle=None, maiden=None, suffix=None) -> int:
    return _q("""insert into people (given_name, middle_name, surname, maiden_name,
                                     nickname, suffix)
                 values (%s, %s, %s, %s, %s, %s) returning id""",
              (given, middle, surname, maiden, nickname, suffix), one=True)[0]


def _variant(person_id, variant, kind="nickname") -> int:
    return _q("insert into person_name_variants (person_id, variant, kind) "
              "values (%s, %s, %s) returning id", (person_id, variant, kind), one=True)[0]


def _face(photo_id, person_id, bbox, **cols) -> int:
    fid = _q("insert into faces (photo_id, person_id, bbox, source, confidence) "
             "values (%s, %s, %s::jsonb, 'human', 0.99) returning id",
             (photo_id, person_id, json.dumps(bbox)), one=True)[0]
    for k, v in cols.items():
        _q(f"update faces set {k} = %s where id = %s", (v, fid))
    return fid


def _album(name, *photo_ids) -> int:
    aid = _q("insert into albums (name) values (%s) returning id", (name,), one=True)[0]
    for i, pid in enumerate(photo_ids):
        _q("insert into album_photos (album_id, photo_id, position) values (%s, %s, %s)",
           (aid, pid, i))
    return aid


def _place(name, *photo_ids, lat=None, lon=None) -> int:
    plid = _q("insert into places (name, latitude, longitude) values (%s, %s, %s) returning id",
              (name, lat, lon), one=True)[0]
    for pid in photo_ids:
        _q("insert into photo_places (photo_id, place_id, confirmed) values (%s, %s, true)",
           (pid, plid))
    return plid


def _human_date(pid):
    """What the accept path records when a person sets the date."""
    _q("insert into audit_log (actor, action, entity_type, entity_id, new_value) "
       "values ('desktop', 'photo.capture_date.set', 'photo', %s, '{}'::jsonb)", (pid,))


def _wp(pid) -> Path:
    return Path(_q("select working_path from photos where id = %s", (pid,), one=True)[0])


def _fv(pid) -> int:
    return _q("select file_version from photos where id = %s", (pid,), one=True)[0]


def _read(settings, path) -> dict:
    with et_mod.ExifTool(settings.EXIFTOOL_PATH) as et:
        return et.read(path)


def _due_ids() -> list[int]:
    with dbmod.connection() as conn:
        conn.autocommit = True
        return [c.photo_id for c in repo.due(repo.load_candidates(conn))]


def _write(settings, ids=None, *, dry_run=False):
    ids = ids if ids is not None else _due_ids()
    return writer.run_chunk(settings, ids, dry_run=dry_run)


def _as_list(v):
    return v if isinstance(v, list) else [v]


# --------------------------------------------------------------------------
# Non-negotiable 1: masters
# --------------------------------------------------------------------------

def test_a_target_under_a_master_root_raises_before_anything_is_written(mw):
    master_file = mw.master_roots[0].path / "IMG_1.jpg"
    with pytest.raises(writer.MasterWriteRefused):
        writer.assert_working_target(mw, master_file)
    with pytest.raises(writer.MasterWriteRefused):
        writer.assert_working_target(mw, mw.master_roots[0].path)
    with pytest.raises(writer.MasterWriteRefused):
        writer.assert_working_target(mw, Path(mw.WORKING_DIR).parent / "elsewhere.jpg")
    assert writer.assert_working_target(mw, Path(mw.WORKING_DIR) / "x.jpg")


def test_a_photo_whose_working_path_points_into_the_masters_is_refused(mw):
    pid = _photo(mw)
    master_copy = mw.master_roots[0].path / _wp(pid).name
    shutil.copy2(_wp(pid), master_copy)
    before = master_copy.read_bytes()
    _q("update photos set working_path = %s where id = %s", (str(master_copy), pid))
    with pytest.raises(writer.MasterWriteRefused):
        _write(mw, [pid])
    assert master_copy.read_bytes() == before
    assert _fv(pid) == 1


def test_the_masters_probe_refuses_when_a_root_is_writable(mw):
    pid = _photo(mw)
    before = _wp(pid).read_bytes()
    with pytest.raises(writer.MastersWritable) as exc:
        _write(mw, [pid])
    assert "icacls" in str(exc.value)
    assert _wp(pid).read_bytes() == before


# --------------------------------------------------------------------------
# What is written
# --------------------------------------------------------------------------

def test_day_precise_date_people_back_and_identity_read_back(mw):
    pid = _photo(mw, capture_date=date(2001, 6, 12), capture_date_precision="exact",
                 capture_date_confirmed=True, physical_ref_note="album 3, page 2")
    _human_date(pid)
    original = _wp(pid).read_bytes()
    peggy = _person("Margaret", "Clay", nickname="Peggy", middle="Jane", maiden="Schmidt")
    _variant(peggy, "Maggie")
    _variant(peggy, "Margret", kind="misspelling")
    george = _person("George", "Clay", suffix="Jr.")
    _face(pid, peggy, {"x": 40, "y": 30, "w": 60, "h": 60})
    _face(pid, george, {"x": 200, "y": 30, "w": 60, "h": 60})
    _album("Summer 1992 - Canada", pid)
    insert_back_text(pid, "Peggy & George, June 2001", confirmed=False)

    [r] = _write(mw)
    assert r.status == "written", r

    t = _read(mw, _wp(pid))
    assert str(t["XMP-photoshop:DateCreated"]).startswith("2001")
    assert str(t["XMP-photoshop:DateCreated"]).replace(":", "-")[:10] == "2001-06-12"
    assert "ExifIFD:DateTimeOriginal" not in t, "no time of day is held: none invented"
    assert str(t["XMP-xmp:CreateDate"]).replace(":", "-")[:10] == "2001-06-12"
    assert _as_list(t["XMP-dc:Subject"]) == ["Peggy Clay", "George Clay Jr."]
    assert _as_list(t["IPTC:Keywords"]) == ["Peggy Clay", "George Clay Jr."]
    assert _as_list(t["XMP-iptcExt:PersonInImage"]) == \
        ["Margaret Jane Clay (née Schmidt)", "George Clay Jr."]
    desc = t["XMP-dc:Description"]
    assert desc == t["IPTC:Caption-Abstract"] == t["IFD0:ImageDescription"]
    assert desc.split("\n") == [
        "Albums: Summer 1992 - Canada",
        "People: Margaret Jane 'Peggy' Clay (née Schmidt; also Maggie), George Clay Jr.",
        "Back of print (unverified transcription): Peggy & George, June 2001",
    ]
    assert "Margret" not in desc, "a misspelling is a search aid, not a name"
    assert t["XMP-photoarchive:PhotoId"] == pid
    assert t["XMP-photoarchive:ScanLocator"] == "Batch 00012 #017"
    assert t["XMP-photoarchive:PhysicalRef"] == "album 3, page 2"
    assert len(t["XMP-photoarchive:MasterSha256"]) == 64

    # The version swap.
    assert _fv(pid) == 2
    kept = Path(mw.WORKING_DIR) / "_versions" / f"{pid:08d}_v1.jpg"
    assert kept.read_bytes() == original
    row = _q("select metadata_hash, metadata_file_version, metadata_written_at, "
             "edited_on_desktop_at from photos p left join people pe on false "
             "where p.id = %s", (pid,), one=True)
    assert row[0] and row[1] == 2 and row[2] is not None
    [(prev, new)] = _q("select previous_value, new_value from audit_log "
                       "where action = 'metadata.write' and entity_id = %s", (pid,))
    assert prev["file_version"] == 1 and new["file_version"] == 2
    assert new["people"] == ["Peggy Clay", "George Clay Jr."]
    # A job, not a human: the human-edit stamps are untouched.
    assert _q("select count(*) from people where edited_on_desktop_at is not null",
              one=True)[0] == 0

    # JPEG pixels identical.
    with Image.open(kept) as a, Image.open(_wp(pid)) as b:
        assert np.array_equal(np.asarray(a), np.asarray(b))
    assert not any((Path(mw.WORKING_DIR) / "_tmp").glob(f"{pid}.*"))


def insert_back_text(pid, text, *, confirmed):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        bid = insert_back(conn, photo_id=pid, transcribed_text=text)
        conn.execute("update photo_backs set transcription_confirmed = %s where id = %s",
                     (confirmed, bid))


@pytest.mark.parametrize("prec, xmp, line", [
    ("year", "1962", "Date: about 1962"),
    ("decade", None, "Date: 1960s"),
])
def test_imprecise_dates_never_reach_exif(mw, prec, xmp, line):
    pid = _photo(mw, capture_date=date(1962, 1, 1), capture_date_precision=prec,
                 capture_date_confirmed=True)
    _human_date(pid)
    _write(mw)
    t = _read(mw, _wp(pid))
    assert "ExifIFD:DateTimeOriginal" not in t
    assert "IPTC:DateCreated" not in t
    if xmp:
        assert str(t["XMP-photoshop:DateCreated"]) == xmp
    else:
        assert "XMP-photoshop:DateCreated" not in t
    # Windows shows a partial xmp:CreateDate as 1 January: never written.
    assert "XMP-xmp:CreateDate" not in t
    assert t["XMP-dc:Description"] == line


def test_ai_description_only_as_the_fact_and_always_prefixed(mw):
    fact = _photo(mw, description_ai="Three children on a porch.")
    pending = _photo(mw, seq=18)
    _q("""insert into suggestions (photo_id, kind, payload, source, status)
          values (%s, 'description', '{"text": "A pending guess."}'::jsonb, 'ai', 'pending')""",
       (pending,))
    _write(mw)
    assert _read(mw, _wp(fact))["XMP-dc:Description"] == \
        "AI-generated description: Three children on a porch."
    t = _read(mw, _wp(pending))
    assert "XMP-dc:Description" not in t, "a pending suggestion is never written"


def test_place_with_gps_and_a_confirmed_back(mw):
    pid = _photo(mw)
    _place("Banff, Alberta, Canada", pid, lat=51.1784, lon=-115.5708)
    insert_back_text(pid, "Banff 1975", confirmed=True)
    _write(mw)
    t = _read(mw, _wp(pid))
    [loc] = _as_list(t["XMP-iptcExt:LocationShown"])
    assert loc["LocationName"] == "Banff, Alberta, Canada"
    assert loc["City"] == "Banff" and loc["CountryName"] == "Canada"
    assert t["XMP-photoshop:State"] == "Alberta"
    assert t["GPS:GPSLatitude"] == pytest.approx(51.1784, abs=1e-4)
    assert t["GPS:GPSLongitudeRef"] == "W"
    assert _as_list(t["XMP-photoarchive:WroteExif"]) == ["GPS"]
    assert t["XMP-dc:Description"] == "Back of print: Banff 1975"


def test_camera_metadata_is_left_alone_and_a_date_mismatch_is_reported(mw):
    ex = Image.Exif()
    ex[0x010F] = "OLYMPUS"
    ex[0x010E] = "OLYMPUS DIGITAL CAMERA"
    ex.get_ifd(0x8769)[0x9003] = "2003:07:04 10:11:12"
    pid = _photo(mw, is_scan=False, exif_bytes=ex.tobytes(),
                 capture_date=date(2003, 7, 5), capture_date_precision="exact",
                 capture_date_confirmed=True)
    [dry] = _write(mw, dry_run=True)
    assert dry.date_mismatch == {"db": "2003-07-05", "file": "2003:07:04 10:11:12"}
    assert dry.foreign == {"IFD0:ImageDescription": "OLYMPUS DIGITAL CAMERA"}

    [r] = _write(mw)
    assert r.status == "written"
    t = _read(mw, _wp(pid))
    assert t["ExifIFD:DateTimeOriginal"] == "2003:07:04 10:11:12", "camera's own, untouched"
    assert t["IFD0:Make"] == "OLYMPUS"
    assert str(t["XMP-photoshop:DateCreated"]).replace(":", "-")[:10] == "2003-07-05"
    assert "IFD0:ImageDescription" not in t, "an owned tag is replaced wholesale"
    assert "XMP-photoarchive:WroteExif" not in t


# --------------------------------------------------------------------------
# Pixels
# --------------------------------------------------------------------------

def _strips(path: Path) -> tuple[bytes, dict]:
    with Image.open(path) as im:
        offsets = im.tag_v2[273]
        counts = im.tag_v2[279]
        info = {"mode": im.mode, "compression": im.info.get("compression"),
                "size": im.size, "bits": im.tag_v2.get(258)}
    raw = path.read_bytes()
    return b"".join(raw[o:o + n] for o, n in zip(offsets, counts)), info


def test_tiff_16_bit_strips_are_byte_identical(mw):
    pid = _photo(mw, fmt="TIFF", image=_noise(300, 200, mode="I;16"))
    before, info_before = _strips(_wp(pid))
    assert info_before["mode"] == "I;16" and info_before["compression"] == "tiff_lzw"
    [r] = _write(mw)
    assert r.status == "written", r
    assert _wp(pid).suffix == ".tif"
    after, info_after = _strips(_wp(pid))
    assert after == before
    assert info_after == info_before
    assert _read(mw, _wp(pid))["XMP-photoarchive:PhotoId"] == pid


# --------------------------------------------------------------------------
# Regions land on the face, whatever the orientation
# --------------------------------------------------------------------------

_INVERSE_TRANSPOSE = {1: None, 6: Image.Transpose.ROTATE_90, 8: Image.Transpose.ROTATE_270}


@pytest.mark.parametrize("orientation", [1, 6, 8])
def test_regions_cut_from_the_stored_pixels_match_the_face_crop(mw, orientation):
    # Draw the display frame, then store it so exif_transpose gives it back.
    display = Image.new("RGB", (400, 300), (20, 20, 20))
    display.paste(_noise(80, 60, seed=9), (240, 40))
    bbox = {"x": 240, "y": 40, "w": 80, "h": 60}
    op = _INVERSE_TRANSPOSE[orientation]
    stored = display.transpose(op) if op is not None else display
    pid = _photo(mw, image=stored, orientation=None if orientation == 1 else orientation)
    assert _q("select width, height from photos where id = %s", (pid,), one=True) == (400, 300)
    person = _person("Ann", "Lee")
    _face(pid, person, bbox)
    [r] = _write(mw)
    assert r.status == "written" and not r.notes, r

    t = _read(mw, _wp(pid))
    info = t["XMP-mwg-rs:RegionInfo"]
    region = _as_list(info["RegionList"])[0]
    assert region["Name"] == "Ann Lee"
    with Image.open(_wp(pid)) as im:
        sw, sh = im.size
        assert (info["AppliedToDimensions"]["W"], info["AppliedToDimensions"]["H"]) == (sw, sh)
        a = region["Area"]
        box = (round((a["X"] - a["W"] / 2) * sw), round((a["Y"] - a["H"] / 2) * sh),
               round((a["X"] + a["W"] / 2) * sw), round((a["Y"] + a["H"] / 2) * sh))
        from_stored = im.convert("RGB").crop(box)
        shown = ImageOps.exif_transpose(im).convert("RGB")
    if op is not None:
        # Turn the stored crop the way the viewer turns the whole picture.
        from_stored = from_stored.transpose(
            Image.Transpose.ROTATE_270 if orientation == 6 else Image.Transpose.ROTATE_90)
    face_crop = shown.crop((240, 40, 320, 100))
    assert from_stored.size == face_crop.size
    assert np.array_equal(np.asarray(from_stored), np.asarray(face_crop))

    mp = _as_list(t["XMP-MP:RegionInfoMP"]["Regions"])[0]
    x, y, w, h = (float(v) for v in mp["Rectangle"].split(","))
    assert (round(x * sw), round(y * sh)) == box[:2]


def test_regions_are_skipped_when_the_file_is_not_the_frame_the_boxes_were_drawn_in(mw):
    pid = _photo(mw)
    _q("update photos set width = 999 where id = %s", (pid,))
    _face(pid, _person("Ann", "Lee"), {"x": 10, "y": 10, "w": 40, "h": 40})
    [r] = _write(mw)
    assert r.status == "written"
    assert any("regions skipped" in n for n in r.notes)
    t = _read(mw, _wp(pid))
    assert "XMP-mwg-rs:RegionInfo" not in t
    assert _as_list(t["XMP-dc:Subject"]) == ["Ann Lee"], "the name is still a fact"


def test_disputed_and_ignored_faces_never_reach_the_file(mw):
    pid = _photo(mw)
    _face(pid, _person("Ann", "Lee"), {"x": 10, "y": 10, "w": 40, "h": 40}, is_disputed=True)
    _face(pid, _person("Bob", "Ray"), {"x": 90, "y": 10, "w": 40, "h": 40},
          review_status="ignore")
    _face(pid, None, {"x": 190, "y": 10, "w": 40, "h": 40}, review_status="unknown")
    _write(mw)
    t = _read(mw, _wp(pid))
    assert "XMP-dc:Subject" not in t and "XMP-mwg-rs:RegionInfo" not in t


# --------------------------------------------------------------------------
# Atomic
# --------------------------------------------------------------------------

def test_a_crash_before_the_swap_leaves_the_original_intact(mw, monkeypatch):
    pid = _photo(mw, description_ai="A porch.")
    original = _wp(pid).read_bytes()

    def boom(*a, **kw):
        raise OSError("simulated crash before the swap")

    monkeypatch.setattr(writer, "swap_in_new_version", boom)
    [r] = _write(mw)
    assert r.status == "failed" and "simulated crash" in r.error
    assert _wp(pid).read_bytes() == original
    assert _fv(pid) == 1
    assert _q("select metadata_hash from photos where id = %s", (pid,), one=True)[0] is None
    assert _q("select count(*) from audit_log where action = 'metadata.write'", one=True)[0] == 0
    assert (Path(mw.WORKING_DIR) / "_tmp" / f"{pid}.jpg").exists(), "the temp file is left"

    monkeypatch.undo()
    monkeypatch.setattr(writer, "run_masters_guard", lambda roots: _FakeGuard())
    [r] = _write(mw)
    assert r.status == "written", "the next run replaces the stray temp file"
    assert _fv(pid) == 2


# --------------------------------------------------------------------------
# Incremental
# --------------------------------------------------------------------------

def test_a_second_run_writes_nothing(mw):
    a, b = _photo(mw), _photo(mw, seq=18)
    assert sorted(_due_ids()) == [a, b]
    assert {r.status for r in _write(mw)} == {"written"}
    assert _due_ids() == []
    assert _write(mw, [a, b]) == []
    assert (_fv(a), _fv(b)) == (2, 2)


def test_one_new_fact_touches_one_file(mw):
    a, b = _photo(mw), _photo(mw, seq=18)
    _write(mw)
    _face(a, _person("Ann", "Lee"), {"x": 10, "y": 10, "w": 40, "h": 40})
    assert _due_ids() == [a]
    [r] = _write(mw)
    assert r.photo_id == a and r.reason == "facts changed"
    assert (_fv(a), _fv(b)) == (3, 2)


def _soft_delete_album(ctx):
    _q("update album_photos set is_deleted = true, deleted_at = now() where photo_id = %s",
       (ctx["a"],))


def _soft_delete_place(ctx):
    _q("update photo_places set is_deleted = true, deleted_at = now() where photo_id = %s",
       (ctx["a"],))


def _soft_delete_variant(ctx):
    _q("update person_name_variants set is_deleted = true, deleted_at = now() where id = %s",
       (ctx["variant_a"],))


@pytest.mark.parametrize("remove, gone, on_b", [
    (_soft_delete_album, "Albums: Lake trip", True),
    (_soft_delete_place, "Lake Louise", True),
    (_soft_delete_variant, "also Scooter", False),
])
def test_a_soft_deleted_row_leaves_exactly_its_photo(mw, remove, gone, on_b):
    a, b = _photo(mw), _photo(mw, seq=18)
    pa, pb = _person("Ann", "Lee"), _person("Bob", "Ray")
    _face(a, pa, {"x": 10, "y": 10, "w": 40, "h": 40})
    _face(b, pb, {"x": 10, "y": 10, "w": 40, "h": 40})
    variant_a = _variant(pa, "Scooter")
    _variant(pb, "Bobby")
    _album("Lake trip", a, b)
    _place("Lake Louise", a, b)
    _write(mw)
    t = _read(mw, _wp(a))
    assert gone in json.dumps(t, ensure_ascii=False)

    remove({"a": a, "variant_a": variant_a})
    assert _due_ids() == [a]
    _write(mw)
    assert gone not in json.dumps(_read(mw, _wp(a)), ensure_ascii=False)
    if on_b:
        assert gone in json.dumps(_read(mw, _wp(b)), ensure_ascii=False)
    assert _fv(b) == 2, "the other photo was not rewritten"


def test_place_aliases_are_not_written_so_removing_one_changes_nothing(mw):
    a = _photo(mw)
    plid = _place("Lake Louise", a)
    _q("insert into place_aliases (place_id, alias) values (%s, 'The Lake')", (plid,))
    _write(mw)
    assert "The Lake" not in json.dumps(_read(mw, _wp(a)))
    _q("update place_aliases set is_deleted = true where place_id = %s", (plid,))
    assert _due_ids() == []


def test_a_pulled_rename_touches_that_persons_photos_only(mw):
    """`pull_web_edits` lands a rename as a plain update of `people`."""
    p1, p2, p3 = _photo(mw), _photo(mw, seq=18), _photo(mw, seq=19)
    ann, bob = _person("Ann", "Lee"), _person("Bob", "Ray")
    _face(p1, ann, {"x": 10, "y": 10, "w": 40, "h": 40})
    _face(p2, ann, {"x": 10, "y": 10, "w": 40, "h": 40})
    _face(p3, bob, {"x": 10, "y": 10, "w": 40, "h": 40})
    _write(mw)
    _q("update people set surname = 'Leigh', edited_on_web_at = now() where id = %s", (ann,))
    assert sorted(_due_ids()) == [p1, p2]
    _write(mw)
    assert _as_list(_read(mw, _wp(p1))["XMP-dc:Subject"]) == ["Ann Leigh"]


def test_a_rebuilt_working_file_is_selected_again(mw):
    """check_working_files' restore-from-_versions path bumps file_version, and
    the metadata it carried is gone; the writer must notice without any fact
    having changed."""
    from photoarchive.tools import check_working_files as cwf
    pid = _photo(mw, description_ai="A porch.")
    _write(mw)
    assert _fv(pid) == 2 and _due_ids() == []
    # The working file goes missing (moved aside, not deleted).
    aside = Path(mw.WORKING_DIR).parent / "aside.jpg"
    _wp(pid).replace(aside)
    counts = cwf.check(dry_run=False, limit=None)
    assert counts.restored_from_versions == 1
    assert _fv(pid) == 3
    assert "XMP-photoarchive:PhotoId" not in _read(mw, _wp(pid))
    with dbmod.connection() as conn:
        conn.autocommit = True
        [c] = [c for c in repo.load_candidates(conn) if c.photo_id == pid]
    assert c.due and c.reason == "file replaced since the last write"
    [r] = _write(mw)
    assert r.status == "written" and _fv(pid) == 4
    assert _read(mw, _wp(pid))["XMP-photoarchive:PhotoId"] == pid


def test_a_missing_file_is_skipped_and_the_chunk_goes_on(mw):
    gone, ok = _photo(mw), _photo(mw, seq=18)
    _wp(gone).replace(Path(mw.WORKING_DIR).parent / "gone.jpg")
    results = {r.photo_id: r for r in _write(mw)}
    assert results[gone].status == "skipped" and "missing file" in results[gone].reason
    assert results[ok].status == "written"
    assert _due_ids() == [gone], "skipped photos stay due"


# --------------------------------------------------------------------------
# Dry run
# --------------------------------------------------------------------------

def test_dry_run_writes_nothing_and_predicts_what_exiftool_reads_back(mw):
    pid = _photo(mw, description_ai="A porch.")
    _face(pid, _person("Ann", "Lee"), {"x": 10, "y": 10, "w": 40, "h": 40})
    before = _wp(pid).read_bytes()
    [dry] = _write(mw, dry_run=True)
    assert dry.status == "would_write"
    assert _wp(pid).read_bytes() == before and _fv(pid) == 1
    assert not (Path(mw.WORKING_DIR) / "_tmp").exists() or \
        not any((Path(mw.WORKING_DIR) / "_tmp").iterdir())
    assert _q("select count(*) from audit_log", one=True)[0] == 0

    _write(mw)
    after = _read(mw, _wp(pid))
    for tag, (_old, new) in dry.changes.items():
        assert et_mod._norm_tag(tag, after.get(tag)) == et_mod._norm_tag(tag, new), tag


def test_orchestrator_report_and_resume(mw, monkeypatch, tmp_path):
    """The chunk processes are replaced by in-process calls (the probe stub
    does not cross a process boundary); everything else is the real path."""
    from photoarchive.tools import write_metadata as cli
    ids = [_photo(mw, seq=s) for s in range(1, 6)]
    insert_back_text(ids[0], "Back text", confirmed=False)

    class _InProc:
        def __init__(self, cmd):
            dry = "--dry-run" in cmd
            ids_file = Path(cmd[cmd.index("--chunk-worker") + 1])
            out = Path(cmd[cmd.index("--out") + 1])
            self.code = cli._chunk_worker(mw, ids_file, out, dry_run=dry)
            self.stdout = iter(["  chunk ran in-process\n"])

        def wait(self):
            return self.code

    monkeypatch.setattr(run_mod, "_spawn", _InProc)
    lines: list[str] = []
    s = run_mod.run(mw, dry_run=True, chunk_size=2, out=lines.append)
    assert (s.due, s.would_write, s.chunks) == (5, 5, 3)
    rdir = Path(s.report_dir)
    for name in ("summary.md", "changes.txt", "samples.json", "foreign_values.csv",
                 "date_mismatch.csv", "skipped.csv"):
        assert (rdir / name).exists(), name
    assert all(_fv(i) == 1 for i in ids), "the dry run wrote nothing"

    s = run_mod.run(mw, dry_run=False, chunk_size=2, out=lines.append)
    assert s.written == 5 and s.failed == 0
    assert all(_fv(i) == 2 for i in ids)
    s = run_mod.run(mw, dry_run=False, chunk_size=2, out=lines.append)
    assert s.due == 0 and s.written == 0
    last = _q("select status, params->'stats'->>'written' from job_runs "
              "where job_name = 'metadata_write' order by id desc limit 1", one=True)
    assert last == ("done", "0")


def test_a_split_child_carries_its_scans_hash_and_print_number(mw):
    """A split child's own sha256 is an identity key; the file someone holds
    is the scan it was cut from."""
    parent = _photo(mw, seq=17)
    parent_sha = _q("select sha256 from photos where id = %s", (parent,), one=True)[0]
    _q("update photos set triage_status = 'junk', is_deleted = true where id = %s", (parent,))
    kids = []
    for n in (1, 2, 3):
        kid = _photo(mw, seq=17, batch=f"Batch 00012 child {n}")
        _q("update photos set scan_batch = 'Batch 00012' where id = %s", (kid,))
        _q("update photos set parent_photo_id = %s, source_filename = %s where id = %s",
           (parent, f"scan017.jpg#p{n}", kid))
        kids.append(kid)
    results = {r.photo_id: r for r in _write(mw)}
    assert set(results) == set(kids), "the junked parent is out of scope"
    t = _read(mw, _wp(kids[1]))
    assert t["XMP-photoarchive:ScanLocator"] == "Batch 00012 #017, print 2 of 3"
    assert t["XMP-photoarchive:MasterSha256"] == parent_sha


def test_a_scans_exif_date_is_the_scanners_clock_and_is_never_written(mw):
    """Answer 9: ingest confirmed these from the scan file's EXIF, which is
    when the print was scanned. No person set them, so no date is written."""
    ex = Image.Exif()
    ex.get_ifd(0x8769)[0x9003] = "2025:11:07 12:07:00"
    pid = _photo(mw, capture_date=date(2025, 11, 7), capture_date_precision="exact",
                 capture_date_confirmed=True, exif_taken_at="2025-11-07 12:07:00")
    cleaned = _photo(mw, seq=18, capture_date=date(2025, 11, 7),
                     capture_date_precision="exact", capture_date_confirmed=True,
                     exif_taken_at="2025-11-07 12:07:00")
    _write(mw)
    for p in (pid, cleaned):
        t = _read(mw, _wp(p))
        assert "XMP-photoshop:DateCreated" not in t and "IPTC:DateCreated" not in t
        assert "ExifIFD:DateTimeOriginal" not in t, "a cleaned scan does not get the clock back"


def test_a_human_set_scan_date_is_written_but_never_into_exif(mw):
    pid = _photo(mw, capture_date=date(1968, 2, 1), capture_date_precision="exact",
                 capture_date_confirmed=True, exif_taken_at="1968-02-01 09:00:00")
    _human_date(pid)
    _write(mw)
    t = _read(mw, _wp(pid))
    assert str(t["XMP-photoshop:DateCreated"]).replace(":", "-")[:10] == "1968-02-01"
    assert "ExifIFD:DateTimeOriginal" not in t
