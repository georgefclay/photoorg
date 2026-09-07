"""transcribe_backs — VLM handwriting transcription on scanned print backs.

Selector: every `photo_backs` row without transcribed_text whose front photo
(if any) is triage_status in ('keep','private') and not deleted. Orphan
backs (photo_id null) are eligible.

Ref convention: `b<photo_backs.id>` for the identity orientation. The
low-confidence retry (below 0.5) is done inline via single-image calls to
`/transcribe-back` on the flipped and rot180 crops rather than shuttling
variants back through the batch queue — the retry rate is small and this
keeps the writer's state machine trivial.

Writer output for the WINNING orientation (highest confidence across
identity/flip/rot180):
  - `photo_backs.transcribed_text` + `transcription_confidence` are set,
    `transcription_confirmed=false`. (Observational text is safe to store
    directly per SCHEMA.md; the confirmed flag distinguishes admin-promoted
    text from unreviewed AI output.)
  - One `suggestions` row kind='transcription', source='ai', payload =
    {"text","parsed_dates","names","photo_back_id","orientation_used"},
    confidence = winner's confidence, model+prompt_version stored.
  - For each parsed date, one `suggestions` row kind='date' on the FRONT
    photo (if any) at confidence 0.8, evidence "handwritten on back: ...".
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import psycopg
from PIL import Image, ImageOps

from ..inference_client import (
    ENDPOINT_MAX_EDGE,
    ENDPOINT_TIMEOUT_S,
    ENDPOINT_TRANSCRIBE_BACK,
    RefImage,
    ResultLine,
)
from ..inference_client.image_prep import prepare_jpeg_bytes
from .base import Job, JobContext, SelectedItem, Selector, Uploader, Writer

log = logging.getLogger(__name__)

RETRY_CONFIDENCE_THRESHOLD = 0.5


class BacksSelector(Selector):
    """photo_backs without transcribed_text, filtered by front-photo triage."""

    def select(
        self,
        conn: psycopg.Connection,
        *,
        model: str,
        prompt_version: str | None,
        limit: int | None = None,
    ) -> list[SelectedItem]:
        # Note: the `model + prompt_version` gate is not applied here because
        # this job keys retries on `transcribed_text is null`, not on
        # photo_job_status. A prompt bump re-runs by manually nulling the
        # transcribed_text on the rows to re-do (documented ops procedure).
        rows = conn.execute(
            """
            select b.id, b.photo_id, coalesce(b.working_path, b.master_path)
            from photo_backs b
            left join photos p on p.id = b.photo_id
            where b.transcribed_text is null
              and (p.id is null
                   or (p.triage_status in ('keep','private') and not p.is_deleted))
            order by b.id
            """
            + ("" if limit is None else " limit %s")
            ,
            () if limit is None else (limit,),
        ).fetchall()
        return [
            SelectedItem(
                ref=f"b{back_id}",
                photo_id=None,  # keep it out of photo_job_status
                working_path=str(working_path),
            )
            for back_id, _photo_id, working_path in rows
        ]


class BacksUploader(Uploader):
    def prepare(self, item: SelectedItem, max_edge: int) -> RefImage:
        return RefImage(ref=item.ref, path=Path(item.working_path))


class BacksWriter(Writer):
    def apply(
        self,
        conn: psycopg.Connection,
        line: ResultLine,
        ctx: JobContext,
    ) -> str:
        back_id = _back_id_from_ref(line.ref)
        if back_id is None:
            return "skipped_bad_ref"

        # Idempotency: if this back already has transcribed_text and a
        # matching model+prompt_version transcription suggestion, do nothing.
        existing_text_row = conn.execute(
            "select transcribed_text from photo_backs where id = %s",
            (back_id,),
        ).fetchone()
        if existing_text_row is None:
            return "skipped_missing_back"
        if existing_text_row[0] is not None and _has_transcription_suggestion(
            conn, back_id, line.model or "", line.prompt_version or ""
        ):
            return "skipped_duplicate"

        # The result envelope is the mini's usual shape. Extract the identity
        # orientation first, then (if low-confidence) retry inline.
        identity = _extract_transcription(line, orientation="identity")
        attempts: list[dict[str, Any]] = [identity]

        if identity["confidence"] is not None and identity["confidence"] < RETRY_CONFIDENCE_THRESHOLD:
            path_row = conn.execute(
                "select coalesce(working_path, master_path) from photo_backs where id = %s",
                (back_id,),
            ).fetchone()
            back_path = Path(path_row[0]) if path_row and path_row[0] else None
            if back_path is not None and back_path.exists():
                for orientation in ("flip", "rot180"):
                    try:
                        variant = _call_variant(ctx, back_id, back_path, orientation)
                        attempts.append(variant)
                    except Exception as e:
                        log.warning(
                            "transcribe_backs: variant %s for back %d failed: %s",
                            orientation, back_id, e,
                        )
            else:
                log.warning("transcribe_backs: back %d file missing at %s; skipping retry", back_id, back_path)

        winner = max(
            (a for a in attempts if a.get("confidence") is not None),
            key=lambda a: a["confidence"],
            default=attempts[0],
        )

        # Insert one transcription suggestion PER attempt so all evidence
        # survives; the writer sets the fact columns from the winner only.
        for attempt in attempts:
            _insert_transcription_suggestion(
                conn,
                back_id=back_id,
                front_photo_id=_front_photo_id(conn, back_id),
                attempt=attempt,
                model=line.model or (attempt.get("model") or ""),
                prompt_version=line.prompt_version or (attempt.get("prompt_version") or ""),
            )

        # Set fact columns from the winner (observational text; confirmed=false).
        conn.execute(
            """
            update photo_backs
            set transcribed_text = %s,
                transcription_confidence = %s
            where id = %s
            """,
            (winner["text"], winner["confidence"], back_id),
        )

        # Front-photo date suggestions (skip for orphan backs).
        front_id = _front_photo_id(conn, back_id)
        if front_id is not None:
            for parsed in winner.get("parsed_dates", []) or []:
                _insert_date_suggestion_from_back(
                    conn,
                    front_photo_id=front_id,
                    text_on_back=winner["text"],
                    parsed=parsed,
                    model=line.model or "",
                    prompt_version=line.prompt_version or "",
                )

        return "ok"


# --- helpers --------------------------------------------------------------


def _back_id_from_ref(ref: str) -> int | None:
    # We only accept `b<id>` because variants are handled inline.
    if not ref or not ref.startswith("b"):
        return None
    body = ref[1:]
    # Defensive: if a caller ever ships b<id>_f/_r from a hypothetical batch
    # variant, still treat as the same back so we don't crash.
    if "_" in body:
        body = body.split("_", 1)[0]
    try:
        return int(body)
    except ValueError:
        return None


def _extract_transcription(line: ResultLine, *, orientation: str) -> dict[str, Any]:
    result = line.result or {}
    return {
        "orientation": orientation,
        "text": result.get("text") or "",
        "confidence": float(result["confidence"]) if result.get("confidence") is not None else None,
        "parsed_dates": result.get("parsed_dates") or [],
        "names": result.get("names") or [],
        "model": line.model,
        "prompt_version": line.prompt_version,
    }


def _call_variant(
    ctx: JobContext,
    back_id: int,
    back_path: Path,
    orientation: str,
) -> dict[str, Any]:
    """Read the back image, apply the transform, POST to /transcribe-back."""
    with Image.open(back_path) as im:
        im = ImageOps.exif_transpose(im)
        if orientation == "flip":
            im = ImageOps.mirror(im)
        elif orientation == "rot180":
            im = im.rotate(180, expand=False)
        else:
            raise ValueError(f"unknown orientation {orientation!r}")
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        # Encode then downscale via prepare_jpeg_bytes for consistency.
        import io as _io
        buf = _io.BytesIO()
        im.save(buf, format="JPEG", quality=95)
        data = buf.getvalue()

    prepared = prepare_jpeg_bytes(data, ENDPOINT_MAX_EDGE[ENDPOINT_TRANSCRIBE_BACK])
    envelope = ctx.client.call_endpoint(
        ENDPOINT_TRANSCRIBE_BACK,
        RefImage(ref=f"b{back_id}_{orientation[0]}", path=back_path, prepared_bytes=prepared),
        timeout=ENDPOINT_TIMEOUT_S[ENDPOINT_TRANSCRIBE_BACK],
    )
    result = envelope.get("result") or {}
    return {
        "orientation": orientation,
        "text": result.get("text") or "",
        "confidence": float(result["confidence"]) if result.get("confidence") is not None else None,
        "parsed_dates": result.get("parsed_dates") or [],
        "names": result.get("names") or [],
        "model": envelope.get("model"),
        "prompt_version": envelope.get("prompt_version"),
    }


def _front_photo_id(conn: psycopg.Connection, back_id: int) -> int | None:
    row = conn.execute(
        "select photo_id from photo_backs where id = %s", (back_id,)
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def _has_transcription_suggestion(
    conn: psycopg.Connection,
    back_id: int,
    model: str,
    prompt_version: str,
) -> bool:
    row = conn.execute(
        """
        select 1 from suggestions
        where kind = 'transcription'
          and source = 'ai'
          and (payload ->> 'photo_back_id')::bigint = %s
          and coalesce(model, '') = %s
          and coalesce(payload ->> 'prompt_version', '') = %s
        limit 1
        """,
        (back_id, model, prompt_version),
    ).fetchone()
    return row is not None


def _insert_transcription_suggestion(
    conn: psycopg.Connection,
    *,
    back_id: int,
    front_photo_id: int | None,
    attempt: dict[str, Any],
    model: str,
    prompt_version: str,
) -> None:
    payload = {
        "text": attempt["text"],
        "parsed_dates": attempt.get("parsed_dates", []),
        "names": attempt.get("names", []),
        "photo_back_id": back_id,
        "orientation_used": attempt["orientation"],
        "prompt_version": prompt_version,
    }
    # Idempotent: skip if a suggestion already exists for
    # (back, model, prompt_version, orientation).
    exists = conn.execute(
        """
        select 1 from suggestions
        where kind = 'transcription'
          and source = 'ai'
          and (payload ->> 'photo_back_id')::bigint = %s
          and coalesce(model, '') = %s
          and coalesce(payload ->> 'prompt_version', '') = %s
          and (payload ->> 'orientation_used') = %s
        limit 1
        """,
        (back_id, model, prompt_version, attempt["orientation"]),
    ).fetchone()
    if exists:
        return
    conn.execute(
        """
        insert into suggestions (photo_id, kind, payload, confidence, source, model)
        values (%s, 'transcription', %s, %s, 'ai', %s)
        """,
        (front_photo_id, json.dumps(payload), attempt.get("confidence"), model),
    )


def _insert_date_suggestion_from_back(
    conn: psycopg.Connection,
    *,
    front_photo_id: int,
    text_on_back: str,
    parsed: dict[str, Any],
    model: str,
    prompt_version: str,
) -> None:
    iso = parsed.get("iso")
    precision = parsed.get("precision") or "unknown"
    text = parsed.get("text") or ""
    payload = {
        "date": iso,
        "precision": precision,
        "evidence": f"handwritten on back: {text_on_back[:200]}",
        "raw_text": text,
        "prompt_version": prompt_version,
    }
    # Idempotent: skip if the same (photo, model, iso, precision, evidence prefix) suggestion exists.
    exists = conn.execute(
        """
        select 1 from suggestions
        where kind = 'date'
          and source = 'ai'
          and photo_id = %s
          and coalesce(model, '') = %s
          and coalesce(payload ->> 'date', '') = coalesce(%s, '')
          and coalesce(payload ->> 'precision', '') = %s
        limit 1
        """,
        (front_photo_id, model, iso, precision),
    ).fetchone()
    if exists:
        return
    conn.execute(
        """
        insert into suggestions (photo_id, kind, payload, confidence, source, model)
        values (%s, 'date', %s, 0.8, 'ai', %s)
        """,
        (front_photo_id, json.dumps(payload), model),
    )


def make_job() -> Job:
    return Job(
        name="transcribe_backs",
        endpoint=ENDPOINT_TRANSCRIBE_BACK,
        selector=BacksSelector(),
        uploader=BacksUploader(),
        writer=BacksWriter(),
        updates_photo_job_status=False,
    )
