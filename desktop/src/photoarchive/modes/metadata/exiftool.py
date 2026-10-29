"""exiftool: one `-stay_open` process, and the tags the archive owns.

Why exiftool: it rewrites metadata segments and leaves the image data alone —
JPEG scan data and TIFF strips are copied byte for byte — which is
non-negotiable 4 ("pixels are never re-encoded").

How it is driven (Phase 13 answer 8):

* **Never `-overwrite_original` on a working file.** Every write is
  `-o _tmp/{id}.{ext}`; the caller verifies the temp file and then swaps it
  in exactly as Cleanup does.
* Multi-line values (the description) go in with `-TAG<=file`: arguments
  travel one per line, and `-ec` C-escaping mangles backslashes.
* Struct values (regions, `LocationShown`) use exiftool's inline struct
  syntax, with `, [ ] { } |` escaped by a leading `|`.

The archive **owns** the tags in `OWNED_TAGS` and replaces them wholesale on
every write (answer 4): a merge would mean a removed person or album could
never leave the file. Camera data is never touched. The two EXIF fields the
writer may add — `DateTimeOriginal` and GPS — are only written where the
camera left none, and `XMP-photoarchive:WroteExif` records that it was us,
so a later write may change or clear them and nothing else ever does.
"""
from __future__ import annotations

import json
import logging
import queue
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import regions as regions_mod

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).with_name("photoarchive.config")

# --------------------------------------------------------------------------
# Tag vocabulary
# --------------------------------------------------------------------------

DESCRIPTION_TAGS = ("XMP-dc:Description", "IPTC:Caption-Abstract", "IFD0:ImageDescription")
KEYWORD_TAGS = ("XMP-dc:Subject", "IPTC:Keywords")
PERSON_TAG = "XMP-iptcExt:PersonInImage"
MWG_TAG = "XMP-mwg-rs:RegionInfo"
MP_TAG = "XMP-MP:RegionInfoMP"
DATE_TAGS = ("XMP-photoshop:DateCreated", "XMP-xmp:CreateDate", "IPTC:DateCreated")
LOCATION_TAG = "XMP-iptcExt:LocationShown"
CITY_TAGS = {"city": ("XMP-photoshop:City", "IPTC:City"),
             "state": ("XMP-photoshop:State", "IPTC:Province-State"),
             "country": ("XMP-photoshop:Country", "IPTC:Country-PrimaryLocationName")}
ARCHIVE_TAGS = {
    "photo_id": "XMP-photoarchive:PhotoId",
    "master_sha256": "XMP-photoarchive:MasterSha256",
    "scan_locator": "XMP-photoarchive:ScanLocator",
    "physical_ref": "XMP-photoarchive:PhysicalRef",
    "source_folder": "XMP-photoarchive:SourceFolder",
    "source_filename": "XMP-photoarchive:SourceFilename",
}
HASH_TAG = "XMP-photoarchive:PayloadHash"
WROTE_EXIF_TAG = "XMP-photoarchive:WroteExif"

EXIF_DATE_TAG = "ExifIFD:DateTimeOriginal"
GPS_TAGS = ("GPS:GPSLatitude", "GPS:GPSLatitudeRef", "GPS:GPSLongitude", "GPS:GPSLongitudeRef")

OWNED_TAGS: tuple[str, ...] = (
    *DESCRIPTION_TAGS, *KEYWORD_TAGS, PERSON_TAG, MWG_TAG, MP_TAG,
    *DATE_TAGS, LOCATION_TAG,
    *(t for pair in CITY_TAGS.values() for t in pair),
    *ARCHIVE_TAGS.values(), HASH_TAG, WROTE_EXIF_TAG,
)
# Read alongside the owned tags: the camera fields we may add to, and the
# marker that tells ours from the camera's.
READ_TAGS: tuple[str, ...] = (*OWNED_TAGS, EXIF_DATE_TAG, *GPS_TAGS)


class ExifToolMissing(RuntimeError):
    """EXIFTOOL_PATH is unset or does not point at a file."""


class ExifToolError(RuntimeError):
    pass


def find_exiftool(path: str | Path | None) -> Path:
    if not path or not str(path).strip():
        raise ExifToolMissing(
            "EXIFTOOL_PATH is not set in desktop/.env. Install exiftool "
            "(winget install OliverBetz.ExifTool) and set EXIFTOOL_PATH to "
            "ExifTool.exe; the metadata writer will not run without it."
        )
    p = Path(str(path).strip())
    if not p.is_file():
        raise ExifToolMissing(f"EXIFTOOL_PATH does not exist: {p}")
    return p


# --------------------------------------------------------------------------
# The process
# --------------------------------------------------------------------------

class ExifTool:
    """One long-lived exiftool. Use as a context manager."""

    def __init__(self, executable: str | Path) -> None:
        self.executable = find_exiftool(executable)
        self._proc: subprocess.Popen[bytes] | None = None
        self._n = 0
        self._err: queue.Queue[str] = queue.Queue()

    def __enter__(self) -> "ExifTool":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def start(self) -> None:
        if self._proc is not None:
            return
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(
            [str(self.executable), "-config", str(CONFIG_PATH),
             "-stay_open", "True", "-@", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=flags,
        )
        t = threading.Thread(target=self._pump_stderr, daemon=True)
        t.start()

    def _pump_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for raw in iter(self._proc.stderr.readline, b""):
            self._err.put(raw.decode("utf-8", "replace").rstrip("\r\n"))

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            assert self._proc.stdin is not None
            self._proc.stdin.write(b"-stay_open\nFalse\n")
            self._proc.stdin.flush()
            self._proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            self._proc.kill()
        self._proc = None

    def execute(self, args: Sequence[str]) -> tuple[str, list[str]]:
        """Run one command. Returns (stdout, stderr lines)."""
        if self._proc is None:
            self.start()
        assert self._proc is not None and self._proc.stdin and self._proc.stdout
        self._n += 1
        n = self._n
        err_marker = f"{{ready-err{n}}}"
        lines = ["-charset", "filename=utf8", *args,
                 "-echo4", err_marker, f"-execute{n}"]
        for a in lines:
            if "\n" in a or "\r" in a:
                raise ValueError(f"exiftool argument contains a newline: {a[:60]!r}")
        self._proc.stdin.write(("\n".join(lines) + "\n").encode("utf-8"))
        self._proc.stdin.flush()

        out: list[str] = []
        ready = f"{{ready{n}}}"
        while True:
            raw = self._proc.stdout.readline()
            if not raw:
                raise ExifToolError("exiftool exited unexpectedly")
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.strip() == ready:
                break
            out.append(line)
        errs: list[str] = []
        while True:
            try:
                line = self._err.get(timeout=60)
            except queue.Empty:
                raise ExifToolError("exiftool did not finish writing its errors")
            if line.strip() == err_marker:
                break
            errs.append(line)
        return "\n".join(out), errs

    # ---- reads ------------------------------------------------------------

    def read(self, path: Path, tags: Iterable[str] | None = None) -> dict[str, Any]:
        """Tags as `{group1:name: value}`, numbers unformatted (`-n`),
        structs as dicts. `tags=None` reads everything."""
        args = ["-j", "-G1", "-struct", "-n", "-a"]
        if tags is not None:
            args += [f"-{t}" for t in tags]
        args.append(str(path))
        out, errs = self.execute(args)
        if not out.strip():
            raise ExifToolError(f"exiftool could not read {path}: {'; '.join(errs)}")
        data = json.loads(out)[0]
        data.pop("SourceFile", None)
        return data

    # ---- writes -----------------------------------------------------------

    def write_copy(self, src: Path, dst: Path, args: Sequence[str], *,
                   ignore_minor: bool = False) -> list[str]:
        """Write `src` + metadata changes to a new file `dst`. Never in place.

        `ignore_minor` adds `-m`: exiftool then writes past a *minor* fault in
        the file's existing metadata (a duplicate XMP property, a bad
        MakerNotes offset) instead of skipping that block or the file.
        Returns exiftool's warnings."""
        if Path(dst).resolve() == Path(src).resolve():
            raise ExifToolError("write_copy refuses to write a file onto itself")
        out, errs = self.execute([*(["-m"] if ignore_minor else []), *args,
                                  "-o", str(dst), str(src)])
        hard = [e for e in errs if e.startswith("Error")]
        if hard or "1 image files created" not in out:
            raise ExifToolError(
                f"exiftool did not write {dst}: {'; '.join(hard or errs) or out.strip()}"
            )
        return [e for e in errs if e.startswith("Warning")]


# --------------------------------------------------------------------------
# From payload to tags
# --------------------------------------------------------------------------

def _wrote_exif(current: dict[str, Any]) -> set[str]:
    v = current.get(WROTE_EXIF_TAG)
    if v is None:
        return set()
    return {str(x) for x in (v if isinstance(v, list) else [v])}


def desired_tags(
    payload: dict[str, Any], *, payload_hash: str, current: dict[str, Any],
    stored_w: int, stored_h: int, orientation: int | None, dims_ok: bool,
) -> tuple[dict[str, Any], list[str]]:
    """Every tag this write sets, with `None` meaning "clear it".

    Returns (tags, notes). `notes` says what was decided and why (regions
    skipped, a camera date left alone), for the dry-run and the audit row.
    """
    notes: list[str] = []
    t: dict[str, Any] = {}

    desc = payload.get("description")
    for tag in DESCRIPTION_TAGS:
        t[tag] = desc

    for tag in KEYWORD_TAGS:
        t[tag] = list(payload.get("keywords") or []) or None
    t[PERSON_TAG] = list(payload.get("persons_in_image") or []) or None

    regs = payload.get("regions") or []
    stored = []
    if regs and not dims_ok:
        notes.append("regions skipped: the file's dimensions do not match the "
                     "frame the face boxes were measured in")
    elif regs:
        stored = regions_mod.to_stored(regs, orientation)
    t[MWG_TAG] = regions_mod.mwg_struct(stored, stored_w, stored_h) if stored else None
    t[MP_TAG] = regions_mod.mp_struct(stored) if stored else None

    d = payload.get("date") or {}
    t["XMP-photoshop:DateCreated"] = d.get("xmp")
    # Windows reads `xmp:CreateDate` as "Date taken" and shows a year-only
    # value as 1 January, midnight — the fabricated date non-negotiable 3
    # forbids. A partial date therefore goes only to `photoshop:DateCreated`
    # (the IPTC date, which allows it) and the description line.
    t["XMP-xmp:CreateDate"] = d.get("xmp") if d.get("precision") == "exact" else None
    t["IPTC:DateCreated"] = d.get("iptc")

    locs = payload.get("locations") or []
    if locs:
        shown = []
        for loc in locs:
            item: dict[str, Any] = {"LocationName": loc["name"]}
            if loc.get("city"):
                item["City"] = loc["city"]
            if loc.get("state"):
                item["ProvinceState"] = loc["state"]
            if loc.get("country"):
                item["CountryName"] = loc["country"]
            if loc.get("lat") is not None and loc.get("lon") is not None:
                item["GPSLatitude"] = loc["lat"]
                item["GPSLongitude"] = loc["lon"]
            shown.append(item)
        t[LOCATION_TAG] = shown
    else:
        t[LOCATION_TAG] = None
    first = locs[0] if locs else {}
    for key, tags in CITY_TAGS.items():
        for tag in tags:
            t[tag] = first.get(key) or None

    for key, tag in ARCHIVE_TAGS.items():
        v = payload.get(key)
        t[tag] = None if v is None else str(v)
    t[HASH_TAG] = payload_hash

    # The two camera fields: only where the camera left none, or where we
    # wrote them last time.
    ours = _wrote_exif(current)
    wrote: list[str] = []
    want_dt = d.get("exif_datetime")
    if current.get(EXIF_DATE_TAG) not in (None, "") and "DateTimeOriginal" not in ours:
        if want_dt and str(current.get(EXIF_DATE_TAG))[:10] != want_dt[:10]:
            notes.append("camera DateTimeOriginal differs from the confirmed "
                         "date; left alone, the date is in XMP")
    elif want_dt:
        t[EXIF_DATE_TAG] = want_dt
        wrote.append("DateTimeOriginal")
    elif "DateTimeOriginal" in ours:
        t[EXIF_DATE_TAG] = None

    gps = payload.get("gps")
    camera_gps = current.get("GPS:GPSLatitude") not in (None, "") and "GPS" not in ours
    if camera_gps:
        pass
    elif gps:
        lat, lon = float(gps["lat"]), float(gps["lon"])
        t["GPS:GPSLatitude"] = abs(lat)
        t["GPS:GPSLatitudeRef"] = "N" if lat >= 0 else "S"
        t["GPS:GPSLongitude"] = abs(lon)
        t["GPS:GPSLongitudeRef"] = "E" if lon >= 0 else "W"
        wrote.append("GPS")
    elif "GPS" in ours:
        for tag in GPS_TAGS:
            t[tag] = None

    t[WROTE_EXIF_TAG] = wrote or None
    return t, notes


_STRUCT_SPECIAL = re.compile(r"([,\[\]{}|])")


def _struct_scalar(v: Any) -> str:
    return _STRUCT_SPECIAL.sub(r"|\1", str(v))


def struct_string(v: Any) -> str:
    if isinstance(v, dict):
        return "{" + ",".join(f"{k}={struct_string(x)}" for k, x in v.items()) + "}"
    if isinstance(v, list):
        return "[" + ",".join(struct_string(x) for x in v) + "]"
    return _struct_scalar(v)


_STRUCT_TAGS = {MWG_TAG, MP_TAG, LOCATION_TAG}


def write_args(tags: dict[str, Any], *, text_file: Path | None) -> list[str]:
    """exiftool arguments for `desired_tags`' output.

    A value with a newline is read from `text_file` (`-TAG<=file`); the
    caller writes it there first (`multiline_value`). Every other value is
    one argument.
    """
    args: list[str] = []
    iptc_values = False
    for tag, v in tags.items():
        if v is None or v == [] or v == {}:
            args.append(f"-{tag}=")
            continue
        if tag.startswith("IPTC:"):
            iptc_values = True
        if tag in _STRUCT_TAGS:
            args.append(f"-{tag}={struct_string(v)}")
        elif isinstance(v, list):
            args.extend(f"-{tag}={x}" for x in v)
        elif isinstance(v, str) and ("\n" in v or "\r" in v):
            if text_file is None:
                raise ValueError(f"{tag} has a multi-line value and no text file")
            args.append(f"-{tag}<={text_file}")
        else:
            args.append(f"-{tag}={v}")
    if iptc_values:
        args = ["-charset", "iptc=UTF8", "-IPTC:CodedCharacterSet=UTF8", *args]
    return args


def multiline_value(tags: dict[str, Any]) -> str | None:
    """The one multi-line value (the description), if any. Every tag that
    carries it carries the same text, so one file serves them all."""
    vals = {v for v in tags.values() if isinstance(v, str) and "\n" in v}
    if len(vals) > 1:
        raise ValueError("more than one distinct multi-line value")
    return next(iter(vals), None)


# --------------------------------------------------------------------------
# Comparing what is there with what would be written
# --------------------------------------------------------------------------

_DATEISH = re.compile(r"^(\d{4})[:-](\d{2})(?:[:-](\d{2}))?(.*)$")


def _norm(v: Any) -> Any:
    if v is None or v == "" or v == [] or v == {}:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 5)
    if isinstance(v, str):
        s = v.strip().replace("\r\n", "\n")
        m = _DATEISH.match(s)
        if m:
            s = "-".join(x for x in m.group(1, 2, 3) if x) + m.group(4)
        try:
            return round(float(s), 5) if re.fullmatch(r"-?\d+(\.\d+)?", s) else s
        except ValueError:
            return s
    if isinstance(v, list):
        items = [_norm(x) for x in v]
        return items or None
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in sorted(v.items()) if _norm(x) is not None} or None
    return str(v)


def _norm_tag(tag: str, v: Any) -> Any:
    n = _norm(v)
    # A one-item list reads back as a scalar; compare like with like.
    if tag in KEYWORD_TAGS + (PERSON_TAG, WROTE_EXIF_TAG, LOCATION_TAG) and n is not None \
            and not isinstance(n, list):
        n = [n]
    if tag == MP_TAG and isinstance(n, dict):
        # Rectangle strings: compare the numbers.
        for r in n.get("Regions") or []:
            if isinstance(r, dict) and isinstance(r.get("Rectangle"), str):
                r["Rectangle"] = [round(float(x), 5) for x in r["Rectangle"].split(",")]
    if tag == MWG_TAG and isinstance(n, dict) and isinstance(n.get("RegionList"), dict):
        n["RegionList"] = [n["RegionList"]]
    if tag == MP_TAG and isinstance(n, dict) and isinstance(n.get("Regions"), dict):
        n["Regions"] = [n["Regions"]]
    return n


def diff(current: dict[str, Any], desired: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    """{tag: (now, would be)} for every tag whose value would change."""
    out: dict[str, tuple[Any, Any]] = {}
    for tag, want in desired.items():
        have = current.get(tag)
        if _norm_tag(tag, have) != _norm_tag(tag, want):
            out[tag] = (have, want)
    return out


def foreign_values(current: dict[str, Any]) -> dict[str, Any]:
    """Values in tags we own that this archive did not write.

    A file the writer has never touched (no `PayloadHash`) — every non-empty
    owned tag is someone else's: a camera's caption, Picasa keywords, a
    Lightroom face region. Once we have written, the owned tags are ours.
    """
    if current.get(HASH_TAG):
        return {}
    return {tag: current[tag] for tag in OWNED_TAGS
            if _norm(current.get(tag)) is not None}


def named_regions(current: dict[str, Any]) -> list[str]:
    """Names on pre-existing face regions (MWG or Microsoft) — family
    labelling worth a look before it is replaced."""
    names: list[str] = []
    mwg = current.get(MWG_TAG) or {}
    lst = mwg.get("RegionList") if isinstance(mwg, dict) else None
    for r in (lst if isinstance(lst, list) else [lst] if lst else []):
        if isinstance(r, dict) and r.get("Name"):
            names.append(str(r["Name"]))
    mp = current.get(MP_TAG) or {}
    lst = mp.get("Regions") if isinstance(mp, dict) else None
    for r in (lst if isinstance(lst, list) else [lst] if lst else []):
        if isinstance(r, dict) and r.get("PersonDisplayName"):
            names.append(str(r["PersonDisplayName"]))
    return names
