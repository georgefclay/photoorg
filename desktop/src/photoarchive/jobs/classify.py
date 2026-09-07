"""classify — one-word content label per photo.

Selector: keep/private, not deleted, photo_job_status for classify not done
at (model, prompt_version).

Writer:
  - Insert `suggestions` kind='classification' payload {label, confidence,
    reason, prompt_version}.
  - Labels {document, screenshot, receipt, blank} at confidence ≥ 0.9 also
    stamp a Triage hint 'ai_junk' — but presort wins: if triage_hints has
    a row for this photo already, keep its hint and just append the AI label
    into `details.also.ai_classify`. If no row exists, insert with hint =
    'ai_junk'.
  - Label 'back_of_print' on a scan photo inserts a pending
    `ingest_pairings` row exactly like the B key path (photo-as-back):
    back_photo_id = this photo, back_score = confidence, front_photo_id =
    immediate predecessor in the same folder by scan_sequence unless the
    predecessor is itself a back / pending back (orphan in that case),
    details.source = 'ai_classify', details.reason = 'back_of_print'.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import psycopg

from ..inference_client import ENDPOINT_CLASSIFY, RefImage, ResultLine
from .base import Job, JobContext, SelectedItem, Selector, Uploader, Writer

log = logging.getLogger(__name__)


AI_JUNK_LABELS = frozenset({"document", "screenshot", "receipt", "blank"})
AI_JUNK_MIN_CONF = 0.9


class ClassifySelector(Selector):
    def select(
        self,
        conn: psycopg.Connection,
        *,
        model: str,
        prompt_version: str | None,
        limit: int | None = None,
    ) -> list[SelectedItem]:
        rows = conn.execute(
            """
            select p.id, p.working_path
            from photos p
            left join photo_job_status pjs
              on pjs.photo_id = p.id and pjs.job_name = 'classify'
            where p.triage_status in ('keep','private')
              and not p.is_deleted
              and p.working_path is not null
              and (
                pjs.photo_id is null
                or pjs.status <> 'done'
                or coalesce(pjs.model, '') <> %s
                or coalesce(pjs.prompt_version, '') <> %s
              )
            order by p.id
            """
            + ("" if limit is None else " limit %s"),
            (model, prompt_version or "") if limit is None else (model, prompt_version or "", limit),
        ).fetchall()
        return [
            SelectedItem(ref=str(pid), photo_id=pid, working_path=wp)
            for pid, wp in rows
        ]


class ClassifyUploader(Uploader):
    def prepare(self, item: SelectedItem, max_edge: int) -> RefImage:
        return RefImage(ref=item.ref, path=Path(item.working_path))


class ClassifyWriter(Writer):
    def apply(
        self,
        conn: psycopg.Connection,
        line: ResultLine,
        ctx: JobContext,
    ) -> str:
        photo_id = _photo_id(line.ref)
        if photo_id is None:
            return "skipped_bad_ref"
        if not line.ok:
            return "error"

        result = line.result or {}
        label = str(result.get("label") or "")
        confidence = _as_float(result.get("confidence"))
        reason = str(result.get("reason") or "")

        model = line.model or ctx.model or ""
        prompt_version = line.prompt_version or ctx.prompt_version or ""

        # Idempotency: skip if a classification suggestion for
        # (photo, model, prompt_version) already exists.
        exists = conn.execute(
            """
            select 1 from suggestions
            where kind = 'classification'
              and source = 'ai'
              and photo_id = %s
              and coalesce(model, '') = %s
              and coalesce(payload ->> 'prompt_version', '') = %s
            limit 1
            """,
            (photo_id, model, prompt_version),
        ).fetchone()
        if exists:
            return "skipped_duplicate"

        payload = {
            "label": label,
            "confidence": confidence,
            "reason": reason,
            "prompt_version": prompt_version,
        }
        conn.execute(
            """
            insert into suggestions (photo_id, kind, payload, confidence, source, model)
            values (%s, 'classification', %s, %s, 'ai', %s)
            """,
            (photo_id, json.dumps(payload), confidence, model),
        )

        # AI-junk triage hint (presort wins).
        if label in AI_JUNK_LABELS and confidence is not None and confidence >= AI_JUNK_MIN_CONF:
            _stamp_ai_junk_hint(
                conn,
                photo_id=photo_id,
                label=label,
                confidence=confidence,
                model=model,
            )

        # back_of_print on a scan → pending ingest_pairings row.
        if label == "back_of_print":
            _propose_back_of_print(
                conn,
                photo_id=photo_id,
                confidence=confidence if confidence is not None else 0.6,
                model=model,
                prompt_version=prompt_version,
            )

        return "ok"


# --- hint precedence ------------------------------------------------------


def _stamp_ai_junk_hint(
    conn: psycopg.Connection,
    *,
    photo_id: int,
    label: str,
    confidence: float,
    model: str,
) -> None:
    """Presort wins. If a row exists, keep its hint and append the AI label
    to details.also.ai_classify. Otherwise insert an ai_junk row."""
    row = conn.execute(
        "select hint, details from triage_hints where photo_id = %s",
        (photo_id,),
    ).fetchone()
    ai_entry = {"label": label, "confidence": confidence, "model": model}
    if row is None:
        details = {"also": {"ai_classify": ai_entry}}
        conn.execute(
            """
            insert into triage_hints (photo_id, hint, confidence, details)
            values (%s, 'ai_junk', %s, %s::jsonb)
            """,
            (photo_id, confidence, json.dumps(details)),
        )
        return
    # Merge into existing row's details.also.ai_classify.
    existing_hint, existing_details = row
    if not isinstance(existing_details, dict):
        existing_details = {}
    also = dict(existing_details.get("also") or {})
    also["ai_classify"] = ai_entry
    new_details = {**existing_details, "also": also}
    conn.execute(
        """
        update triage_hints
        set details = %s::jsonb
        where photo_id = %s
        """,
        (json.dumps(new_details), photo_id),
    )


# --- back_of_print → ingest_pairings --------------------------------------


def _propose_back_of_print(
    conn: psycopg.Connection,
    *,
    photo_id: int,
    confidence: float,
    model: str,
    prompt_version: str,
) -> None:
    """Photo-as-back proposal from classify. Mirrors the B-key rules but
    keyed by ai_classify and using the AI's confidence as back_score."""
    photo = conn.execute(
        """
        select p.id, p.source_root, p.source_folder, p.source_filename,
               p.scan_sequence, p.working_path, p.is_scan
        from photos p
        where p.id = %s
        """,
        (photo_id,),
    ).fetchone()
    if photo is None:
        return
    (_pid, source_root, source_folder, source_filename, scan_sequence,
     working_path, is_scan) = photo
    if not is_scan:
        # back_of_print only makes sense for scan-root photos.
        return
    if not working_path:
        # Selector already gates on working_path is not null; defensive.
        return

    master = conn.execute(
        """
        select master_path, sha256
        from photo_masters
        where photo_id = %s and is_preferred
        """,
        (photo_id,),
    ).fetchone()
    if master is None:
        return

    # Skip if there's already a pending or accepted pairing pointing at this
    # photo as back — the unique index would refuse the duplicate anyway.
    existing = conn.execute(
        """
        select id, status from ingest_pairings
        where back_photo_id = %s
          and status in ('pending','accepted')
        limit 1
        """,
        (photo_id,),
    ).fetchone()
    if existing is not None:
        return

    # Skip if the master_path is already used elsewhere in ingest_pairings
    # (would violate the back_master_path unique constraint).
    already_used = conn.execute(
        "select 1 from ingest_pairings where back_master_path = %s limit 1",
        (master[0],),
    ).fetchone()
    if already_used is not None:
        return

    # Skip if George has already rejected this photo as a back — don't
    # nag repeatedly.
    rejected = conn.execute(
        """
        select 1 from ingest_pairings
        where back_photo_id = %s and status = 'rejected'
        limit 1
        """,
        (photo_id,),
    ).fetchone()
    if rejected is not None:
        return

    front_id: int | None = None
    reason = "orphan_no_predecessor"
    if scan_sequence is not None:
        pred = conn.execute(
            """
            select id from photos
            where source_root = %s and source_folder = %s
              and scan_sequence is not null and scan_sequence < %s
              and not is_deleted
            order by scan_sequence desc, id desc
            limit 1
            """,
            (source_root, source_folder, scan_sequence),
        ).fetchone()
        if pred is not None:
            pred_id = int(pred[0])
            pred_is_back = conn.execute(
                """
                select 1 from ingest_pairings
                where back_photo_id = %s and status = 'pending'
                limit 1
                """,
                (pred_id,),
            ).fetchone()
            if pred_is_back is None:
                front_id = pred_id
                reason = "ok"
            else:
                reason = "orphan_predecessor_is_back"

    # Best-effort thumbnail path for the pairing row.
    from ..modes.ingest import paths as ingest_paths
    from ..config import load as load_settings
    settings = load_settings()
    thumb_path = str(ingest_paths.thumb_path(settings, photo_id))

    details = {
        "source": "ai_classify",
        "reason": "back_of_print",
        "predecessor_reason": reason,
        "model": model,
        "prompt_version": prompt_version,
    }

    conn.execute(
        """
        insert into ingest_pairings
          (front_photo_id, back_master_path, back_sha256,
           back_source_folder, back_source_filename,
           back_scan_sequence, back_score,
           staging_working_path, staging_thumb_path,
           back_photo_id, back_aspect_mismatch, details)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            front_id, master[0], master[1],
            source_folder, source_filename,
            scan_sequence, float(confidence),
            working_path, thumb_path,
            photo_id, False,
            json.dumps(details),
        ),
    )


# --- helpers --------------------------------------------------------------


def _photo_id(ref: str) -> int | None:
    try:
        return int(ref)
    except (TypeError, ValueError):
        return None


def _as_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def make_job() -> Job:
    return Job(
        name="classify",
        endpoint=ENDPOINT_CLASSIFY,
        selector=ClassifySelector(),
        uploader=ClassifyUploader(),
        writer=ClassifyWriter(),
    )
