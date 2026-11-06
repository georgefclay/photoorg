"""Phase 13 fix-up 3 — the Windows boxes and the sentence-first caption,
written by the real writer with the real exiftool and read back.

* Title (XPTitle, and dc:Title with the same value) is the albums; a scan
  with no album gets its locator; a digital photo with no album gets none;
* Subject (XPSubject) is the AI sentence, plain; Comments (XPComment) the
  back lines and the date note, absent when there are none;
* non-ASCII and multi-line text survives the UTF-16 round trip;
* the caption leads with the sentence; albums are keywords beside people;
* a file written at payload version 1 that holds someone else's XPTitle is
  listed by the census before it is replaced, and is not listed again;
* a second run writes nothing.
"""
from __future__ import annotations

from datetime import date

from photoarchive.modes.metadata import exiftool as et_mod

from .phase6_fixtures import phase6  # noqa: F401
from .test_metadata_writer import (  # noqa: F401
    _album, _as_list, _due_ids, _face, _human_date, _person, _photo, _q, _read,
    _write, _wp, insert_back_text, mw,
)


def test_scan_with_a_back_and_an_album_fills_all_three_boxes(mw):
    pid = _photo(mw, description_ai="Peggy beside the lake.")
    _album("Summer 1992 - Canada", pid)
    peggy = _person("Margaret", "Clay", nickname="Peggy")
    _face(pid, peggy, {"x": 40, "y": 40, "w": 60, "h": 60})
    insert_back_text(pid, "Peggy (née Schmidt) — Lake Louise\n1962", confirmed=False)
    _q("update photos set capture_date = '1962-01-01', capture_date_precision = 'year', "
       "capture_date_confirmed = true where id = %s", (pid,))
    _human_date(pid)
    [r] = _write(mw)
    assert r.status == "written", r.error or r.reason

    t = _read(mw, _wp(pid))
    assert t["IFD0:XPTitle"] == "Summer 1992 - Canada"
    assert t["XMP-dc:Title"] == t["IFD0:XPTitle"]
    assert t["IFD0:XPSubject"] == "Peggy beside the lake."
    assert t["IFD0:XPComment"].replace("\r\n", "\n").split("\n") == [
        "Back of print (unverified transcription): Peggy (née Schmidt) — Lake Louise 1962",
        "Date: about 1962",
    ], "UTF-16 round trip keeps the accent, the dash and the line break"
    assert t["XMP-dc:Description"].split("\n") == [
        "Peggy beside the lake. (AI-generated)",
        "Back of print (unverified transcription): Peggy (née Schmidt) — Lake Louise 1962",
        "Date: about 1962",
        "People: Margaret 'Peggy' Clay",
        "Albums: Summer 1992 - Canada",
    ]
    assert _as_list(t["XMP-dc:Subject"]) == ["Peggy Clay", "Summer 1992 - Canada"]
    assert _as_list(t["IPTC:Keywords"]) == ["Peggy Clay", "Summer 1992 - Canada"]
    assert str(t[et_mod.VERSION_TAG]) == "2"
    assert _due_ids() == [], "a second run writes nothing"


def test_scan_without_album_or_back_gets_its_locator_and_no_comment(mw):
    pid = _photo(mw, batch="Batch 00020", seq=32, description_ai="A barn.")
    _write(mw)
    t = _read(mw, _wp(pid))
    assert t["IFD0:XPTitle"] == "Batch 00020 #032"
    assert t["XMP-dc:Title"] == "Batch 00020 #032"
    assert t["IFD0:XPSubject"] == "A barn."
    assert "IFD0:XPComment" not in t
    assert t["XMP-dc:Description"] == "A barn. (AI-generated)"


def test_digital_photo_without_album_gets_no_title(mw):
    pid = _photo(mw, is_scan=False, description_ai="A dog on a lawn.")
    _write(mw)
    t = _read(mw, _wp(pid))
    assert "IFD0:XPTitle" not in t and "XMP-dc:Title" not in t
    assert t["IFD0:XPSubject"] == "A dog on a lawn."


def test_someone_elses_xptitle_is_listed_once_then_replaced(mw):
    pid = _photo(mw, is_scan=False, description_ai="A cake.")
    [first] = _write(mw)
    assert first.status == "written"
    # As if written at version 1 by the old writer, with a camera's own title
    # in a field the archive did not own yet.
    with et_mod.ExifTool(mw.EXIFTOOL_PATH) as et:
        out, errs = et.execute(["-overwrite_original", "-IFD0:XPTitle=Birthday 2004",
                                "-IFD0:XPSubject=", "-IFD0:XPComment=", "-XMP-dc:Title=",
                                f"-{et_mod.VERSION_TAG}=", str(_wp(pid))])
    assert "1 image files updated" in out, errs
    _q("update photos set metadata_hash = 'stale' where id = %s", (pid,))

    [dry] = _write(mw, [pid], dry_run=True)
    assert dry.foreign == {"IFD0:XPTitle": "Birthday 2004"}, dry.foreign
    [real] = _write(mw, [pid])
    assert real.status == "written"
    t = _read(mw, _wp(pid))
    assert "IFD0:XPTitle" not in t, "an album-less digital photo has no title"

    _q("update photos set metadata_hash = 'stale' where id = %s", (pid,))
    [again] = _write(mw, [pid], dry_run=True)
    assert again.foreign == {}, "written at version 2: the field is ours now"


def test_the_cap_still_cuts_the_ai_text_first_in_the_file(mw):
    pid = _photo(mw, description_ai="word " * 600)
    insert_back_text(pid, "Grandma's house", confirmed=True)
    _write(mw)
    t = _read(mw, _wp(pid))
    lines = t["XMP-dc:Description"].split("\n")
    assert len(t["XMP-dc:Description"]) <= 2000
    assert lines[0].endswith("… (AI-generated)")
    assert lines[1] == "Back of print: Grandma's house"
    assert t["IFD0:XPComment"] == "Back of print: Grandma's house"
