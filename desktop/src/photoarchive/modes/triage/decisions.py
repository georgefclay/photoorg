"""Triage decision engine. Keep/Junk/Private/Untriaged with reversible
transitions, audit rows, and file movement.

Post-condition invariants for every target state:
  junk       : file at quarantine_path, working_path NULL, is_deleted=true,
               deleted_at=now(), triage_status='junk'.
  keep       : file at working_path, quarantine_path NULL, is_deleted=false,
               deleted_at=NULL, is_private=false, triage_status='keep'.
  private    : file at working_path, quarantine_path NULL, is_deleted=false,
               deleted_at=NULL, is_private=true, triage_status='private'.
  untriaged  : file at working_path, quarantine_path NULL, is_deleted=false,
               deleted_at=NULL, is_private=false, triage_status='untriaged'.

The DB is the source of truth. File moves happen AFTER the DB commit; a move
failure logs but leaves the DB row as-decided. The quarantine browser
surfaces mismatches so they can be reconciled.
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from ... import db
from ...config import Settings
from ..ingest.paths import working_name

log = logging.getLogger(__name__)


VALID_STATES = ("untriaged", "keep", "junk", "private")


@dataclass(frozen=True)
class PhotoState:
    photo_id: int
    triage_status: str
    is_private: bool
    is_deleted: bool
    working_path: str | None
    quarantine_path: str | None
    sha256: str
    ext: str


def _ext_from_row(mime: str | None, current_path: str | None) -> str:
    if current_path:
        e = Path(current_path).suffix.lower().lstrip(".")
        if e:
            return e
    if mime:
        return {
            "image/jpeg": "jpg", "image/png": "png",
            "image/tiff": "tif", "image/heic": "heic",
            "image/webp": "webp",
        }.get(mime, "jpg")
    return "jpg"


def load_state(conn: psycopg.Connection, photo_id: int) -> PhotoState:
    row = conn.execute(
        """
        select id, triage_status, is_private, is_deleted,
               working_path, quarantine_path, sha256, mime
        from photos where id = %s
        """,
        (photo_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"photo {photo_id} not found")
    return PhotoState(
        photo_id=row[0], triage_status=row[1], is_private=bool(row[2]),
        is_deleted=bool(row[3]), working_path=row[4], quarantine_path=row[5],
        sha256=row[6], ext=_ext_from_row(row[7], row[4] or row[5]),
    )


def quarantine_target(settings: Settings, state: PhotoState) -> Path:
    return settings.QUARANTINE_DIR / working_name(
        state.photo_id, state.sha256, state.ext,
    )


def working_target(settings: Settings, state: PhotoState) -> Path:
    return settings.WORKING_DIR / working_name(
        state.photo_id, state.sha256, state.ext,
    )


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError:
        shutil.copy2(src, dst)
        try:
            os.unlink(src)
        except OSError as e:
            log.warning("could not remove source after copy %s: %s", src, e)


@dataclass(frozen=True)
class DecisionResult:
    photo_id: int
    previous: dict[str, Any]
    new: dict[str, Any]
    file_move: tuple[str, str] | None


def _target_columns(
    settings: Settings, state: PhotoState, new_status: str,
) -> tuple[dict[str, Any], tuple[Path, Path] | None]:
    """Return (column dict for UPDATE, file move tuple or None)."""
    wp_working = working_target(settings, state)
    wp_quar = quarantine_target(settings, state)
    move: tuple[Path, Path] | None = None

    if new_status == "junk":
        # File must end up in quarantine.
        if state.working_path and Path(state.working_path).exists():
            move = (Path(state.working_path), wp_quar)
            final_quar = str(wp_quar)
        elif state.quarantine_path and Path(state.quarantine_path).exists():
            final_quar = state.quarantine_path
        else:
            # Nothing on disk — record intent anyway.
            final_quar = str(wp_quar)
        return {
            "triage_status": "junk",
            "is_private": state.is_private,   # orthogonal to junk
            "is_deleted": True,
            "deleted_at_sql": "now()",
            "working_path": None,
            "quarantine_path": final_quar,
        }, move

    # keep / private / untriaged all end up in the working set.
    if state.quarantine_path and Path(state.quarantine_path).exists() \
            and not (state.working_path and Path(state.working_path).exists()):
        move = (Path(state.quarantine_path), wp_working)
        final_working = str(wp_working)
    else:
        final_working = state.working_path or str(wp_working)

    is_private = (new_status == "private")
    return {
        "triage_status": new_status,
        "is_private": is_private,
        "is_deleted": False,
        "deleted_at_sql": "null",
        "working_path": final_working,
        "quarantine_path": None,
    }, move


def _apply_columns(
    conn: psycopg.Connection, photo_id: int, cols: dict[str, Any],
) -> None:
    conn.execute(
        f"""
        update photos
        set triage_status = %s,
            is_private    = %s,
            is_deleted    = %s,
            working_path  = %s,
            quarantine_path = %s,
            deleted_at    = {cols['deleted_at_sql']},
            file_version  = file_version + 1
        where id = %s
        """,
        (cols["triage_status"], cols["is_private"], cols["is_deleted"],
         cols["working_path"], cols["quarantine_path"], photo_id),
    )


def apply_decision(
    settings: Settings, photo_id: int, new_status: str,
    *, hint: str | None = None, actor: str = "desktop",
) -> DecisionResult:
    if new_status not in VALID_STATES:
        raise ValueError(f"invalid triage status {new_status!r}")

    with db.connection() as conn:
        conn.autocommit = False
        try:
            state = load_state(conn, photo_id)
            prev = {
                "triage_status": state.triage_status,
                "is_private": state.is_private,
                "is_deleted": state.is_deleted,
                "working_path": state.working_path,
                "quarantine_path": state.quarantine_path,
            }
            cols, move = _target_columns(settings, state, new_status)
            _apply_columns(conn, photo_id, cols)
            new_row = {
                "triage_status": cols["triage_status"],
                "is_private": cols["is_private"],
                "is_deleted": cols["is_deleted"],
                "working_path": cols["working_path"],
                "quarantine_path": cols["quarantine_path"],
                "hint": hint,
            }
            db.audit(
                conn, actor=actor, action="triage.decision",
                entity_type="photo", entity_id=photo_id,
                previous_value=prev, new_value=new_row,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    file_move: tuple[str, str] | None = None
    if move is not None:
        try:
            _move(move[0], move[1])
            file_move = (str(move[0]), str(move[1]))
        except OSError as e:
            log.error("post-commit file move failed %s → %s: %s",
                      move[0], move[1], e)

    return DecisionResult(
        photo_id=photo_id, previous=prev, new=new_row, file_move=file_move,
    )


def restore_from_quarantine(settings: Settings, photo_id: int,
                            *, actor: str = "desktop") -> DecisionResult:
    """Junk → untriaged, moving the file back to working/."""
    return apply_decision(settings, photo_id, "untriaged", actor=actor)


def unprivate(settings: Settings, photo_id: int,
              *, actor: str = "desktop") -> DecisionResult:
    """Private → keep (flag off, no file movement)."""
    return apply_decision(settings, photo_id, "keep", actor=actor)


def undo(
    settings: Settings, result: DecisionResult, *, actor: str = "desktop",
) -> DecisionResult:
    """Revert a prior decision. Uses the recorded `previous.triage_status`
    as the target; the state machine takes care of moving the file back.

    Multi-select undo: the UI stores the list of DecisionResults produced by
    one keypress and calls undo() over each in reverse order.
    """
    target = result.previous.get("triage_status") or "untriaged"
    return apply_decision(settings, result.photo_id, target, actor=actor)
