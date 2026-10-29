"""Writing one working copy's metadata, safely.

Non-negotiable 1 — **writes only to working copies**, asserted in code:
`assert_working_target` refuses any path that is not under `WORKING_DIR`, or
that is under a master root, before a byte is written; it guards the source,
the temp file and the `_versions/` target alike. The masters-guard probe runs
at the start of every chunk (`run_masters_guard`), as in ingest and cleanup.

Non-negotiable 4 — **atomic**: exiftool writes `WORKING_DIR/_tmp/{id}.{ext}`
(never in place), the temp file must decode to the same stored dimensions and
read back the payload hash, and only then does Cleanup's
`swap_in_new_version` move the old file to `_versions/{id:08d}_v{n}.{ext}`
and the new one into place, inside the transaction that bumps
`file_version`. A crash before the swap leaves a stray temp file and the
original untouched; the next run overwrites the temp file.

A photo whose file is missing or undecodable is skipped and listed; it never
aborts the chunk (Phase 6 fix-up 11's hand-over rule).
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from PIL import Image

from ... import db
from ...config import Settings, _norm, _overlaps
from ..cleanup import paths as cpaths
from ..cleanup.accept import swap_in_new_version
from ..ingest.guard import remediation_message, run_masters_guard
from ..ingest.paths import resolve_working_path
from . import exiftool as et_mod
from . import regions as regions_mod
from . import repo
from .payload import summary

log = logging.getLogger(__name__)

TMP_SUBDIR = "_tmp"
ACTION = "metadata.write"


class MasterWriteRefused(RuntimeError):
    """A write target resolved outside WORKING_DIR or inside a master root."""


class MastersWritable(RuntimeError):
    """The guard found a writable master root. Refuse to run."""


# --------------------------------------------------------------------------
# Non-negotiable 1
# --------------------------------------------------------------------------

def assert_working_target(settings: Settings, path: Path | str) -> Path:
    """Return `path` resolved, or raise before anything is written to it."""
    p = _norm(Path(path))
    wd = _norm(Path(settings.WORKING_DIR))
    for root in settings.master_roots:
        if _overlaps(_norm(root.path), p):
            raise MasterWriteRefused(
                f"refusing to write {path}: it is inside master root "
                f"'{root.label}' ({root.path}). Masters are read-only."
            )
    if p == wd or not _is_under(p, wd):
        raise MasterWriteRefused(
            f"refusing to write {path}: not under WORKING_DIR ({settings.WORKING_DIR})"
        )
    return Path(path).resolve()


def _is_under(p: Path, parent: Path) -> bool:
    try:
        p.relative_to(parent)
        return True
    except ValueError:
        return False


def check_masters(settings: Settings) -> None:
    guard = run_masters_guard(settings.master_roots)
    if not guard.all_read_only:
        raise MastersWritable(remediation_message(guard))


def tmp_dir(settings: Settings) -> Path:
    return Path(settings.WORKING_DIR) / TMP_SUBDIR


# --------------------------------------------------------------------------
# One photo
# --------------------------------------------------------------------------

@dataclass
class PhotoResult:
    photo_id: int
    status: str            # written | would_write | skipped | failed
    reason: str = ""
    in_group: bool = False
    changes: dict[str, list[Any]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    foreign: dict[str, Any] = field(default_factory=dict)
    foreign_region_names: list[str] = field(default_factory=list)
    date_mismatch: dict[str, str] | None = None
    payload: dict[str, Any] | None = None
    error: str | None = None
    new_version: int | None = None
    version_file: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _stored_geometry(path: Path) -> tuple[int, int, int]:
    """(stored w, stored h, EXIF orientation) from the header only."""
    with Image.open(path) as im:
        w, h = im.size
        orientation = int(im.getexif().get(0x0112) or 1)
    return w, h, orientation


def _verify_decodes(path: Path, want: tuple[int, int]) -> None:
    with Image.open(path) as im:
        im.load()
        if im.size != want:
            raise RuntimeError(
                f"{path.name} decodes to {im.size[0]}×{im.size[1]}, "
                f"expected {want[0]}×{want[1]}"
            )


class _MinorFault(RuntimeError):
    """The first write fell over a minor fault in the existing metadata."""


def _write_and_check(
    et: et_mod.ExifTool, src: Path, tmp: Path, args: Sequence[str],
    size: tuple[int, int], expected_hash: str, *, ignore_minor: bool = False,
) -> list[str]:
    """Write the temp copy and prove it: same stored size, our hash readable."""
    try:
        warnings = et.write_copy(src, tmp, args, ignore_minor=ignore_minor)
    except et_mod.ExifToolError as e:
        if not ignore_minor and "[minor]" in str(e).lower():
            raise _MinorFault(str(e)) from e
        raise
    _verify_decodes(tmp, size)
    back = et.read(tmp, [et_mod.HASH_TAG])
    if str(back.get(et_mod.HASH_TAG) or "") != expected_hash:
        msg = "the written file does not read back the payload hash"
        if not ignore_minor:
            raise _MinorFault(f"{msg} ({'; '.join(warnings) or 'no warning'})")
        raise RuntimeError(msg)
    return warnings


def write_one(
    settings: Settings, cand: repo.Candidate, et: et_mod.ExifTool, *,
    dry_run: bool, actor: str = "desktop",
) -> PhotoResult:
    pid = cand.photo_id
    res = PhotoResult(photo_id=pid, status="skipped", reason=cand.reason,
                      in_group=cand.in_group, payload=cand.payload)

    src = resolve_working_path(settings.WORKING_DIR, cand.working_path)
    if src is None or not src.is_file():
        res.reason = f"missing file: {src or '(no working_path)'}"
        return res
    src = assert_working_target(settings, src)

    try:
        sw, sh, orientation = _stored_geometry(src)
    except Exception as e:  # noqa: BLE001 — an unreadable file is listed, not fatal
        res.reason = f"undecodable: {e}"
        return res

    try:
        current = et.read(src, et_mod.READ_TAGS)
    except et_mod.ExifToolError as e:
        res.reason = f"unreadable metadata: {e}"
        return res

    f = cand.facts
    dims_ok = regions_mod.dims_match(f.width, f.height, sw, sh, orientation)
    desired, notes = et_mod.desired_tags(
        cand.payload, payload_hash=cand.hash, current=current,
        stored_w=sw, stored_h=sh, orientation=orientation, dims_ok=dims_ok,
    )
    res.notes = notes
    res.changes = {t: [old, new] for t, (old, new) in et_mod.diff(current, desired).items()}
    res.foreign = et_mod.foreign_values(current)
    res.foreign_region_names = et_mod.named_regions(current) if res.foreign else []
    d = cand.payload.get("date") or {}
    cam = current.get(et_mod.EXIF_DATE_TAG)
    if d.get("precision") == "exact" and cam and \
            "DateTimeOriginal" not in et_mod._wrote_exif(current):
        cam_day = str(cam)[:10].replace(":", "-")
        if cam_day != d["xmp"][:10]:
            res.date_mismatch = {"db": d["xmp"][:10], "file": str(cam)}

    if dry_run:
        res.status = "would_write"
        return res

    ext = src.suffix.lstrip(".").lower()
    tmp = assert_working_target(settings, tmp_dir(settings) / f"{pid}.{ext}")
    text_file = assert_working_target(settings, tmp_dir(settings) / f"{pid}.description.txt")
    version_target = assert_working_target(
        settings, cpaths.version_path(settings, pid, cand.file_version, ext))
    if version_target.exists():
        res.reason = (f"{version_target.name} already exists in _versions/; "
                      "refusing to overwrite a kept version")
        return res

    tmp.parent.mkdir(parents=True, exist_ok=True)
    if tmp.exists():
        tmp.unlink()  # a stray temp file from a crashed run; ours to replace
    try:
        text = et_mod.multiline_value(desired)
        if text is not None:
            # newline="\n": Windows would otherwise store CRLF in the file,
            # and what exiftool reads back would not be what the dry run said.
            text_file.write_text(text, encoding="utf-8", newline="\n")
        args = et_mod.write_args(desired, text_file=text_file)
        try:
            _write_and_check(et, src, tmp, args, (sw, sh), cand.hash)
        except _MinorFault as first:
            # A fault in the file's own metadata — Photoshop's duplicate XMP
            # property, a camera's bad MakerNotes offset — makes exiftool skip
            # the block (or the file) unless told -m. The check below still
            # has to pass; the master keeps the original either way.
            if tmp.exists():
                tmp.unlink()
            warnings = _write_and_check(et, src, tmp, args, (sw, sh), cand.hash,
                                        ignore_minor=True)
            notes.append(f"written with exiftool -m after: {first}"
                         + (f" ({'; '.join(warnings)})" if warnings else ""))

        with db.connection() as conn:
            conn.autocommit = False
            try:
                locked = repo.lock_for_write(conn, pid)
                if locked is None:
                    conn.rollback()
                    res.reason = "left scope while running"
                    return res
                fv, wpath = locked
                if fv != cand.file_version or \
                        resolve_working_path(settings.WORKING_DIR, wpath) != src:
                    conn.rollback()
                    res.reason = ("the working file changed while running; "
                                  "the next run picks it up")
                    return res
                new_version = fv + 1
                repo.record_write(conn, photo_id=pid, new_version=new_version,
                                  payload_hash=cand.hash, file_size=tmp.stat().st_size)
                db.audit(
                    conn, actor=actor, action=ACTION, entity_type="photo", entity_id=pid,
                    previous_value={"file_version": fv,
                                    "metadata_hash": cand.metadata_hash},
                    new_value={"file_version": new_version,
                               "version_file": str(version_target),
                               "reason": cand.reason, "notes": notes,
                               "tags_changed": sorted(res.changes),
                               **summary(cand.payload)},
                )
                swap_in_new_version(src, version_target, tmp, src)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except Exception as e:  # noqa: BLE001 — listed per photo, the chunk goes on
        log.exception("metadata: photo %d failed", pid)
        res.status = "failed"
        res.error = str(e)
        return res
    finally:
        if text_file.exists():
            text_file.unlink()

    res.status = "written"
    res.new_version = new_version
    res.version_file = str(version_target)
    return res


# --------------------------------------------------------------------------
# A chunk
# --------------------------------------------------------------------------

def run_chunk(
    settings: Settings, photo_ids: Sequence[int], *, dry_run: bool,
    progress: Callable[[PhotoResult, int, int], None] | None = None,
    actor: str = "desktop",
) -> list[PhotoResult]:
    """Guard, re-select (facts may have moved since the orchestrator looked),
    and write each photo that is still due."""
    check_masters(settings)
    exe = et_mod.find_exiftool(settings.EXIFTOOL_PATH)
    with db.connection() as conn:
        conn.autocommit = True
        cands = repo.load_candidates(conn, photo_ids=photo_ids)
    todo = repo.due(cands)
    results: list[PhotoResult] = []
    with et_mod.ExifTool(exe) as et:
        for i, cand in enumerate(todo, 1):
            r = write_one(settings, cand, et, dry_run=dry_run, actor=actor)
            results.append(r)
            if progress:
                progress(r, i, len(todo))
    return results


def env_for_children() -> dict[str, str]:
    """The environment a chunk process inherits (unbuffered output)."""
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env
