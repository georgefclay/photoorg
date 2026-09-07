"""detect_faces — InsightFace/ArcFace face detection + embeddings.

Selector: keep/private photos, not deleted, whose photo_job_status for
detect_faces is not 'done' at the current model. The selector deliberately
gates on `photo_job_status` (not on `faces` row count) so a legitimately
zero-face photo is not re-processed forever.

Ref convention: `<photo_id>` as a decimal string.

Writer:
  - Inserts one `faces` row per detected face (source='ai', person_id null,
    embedding, embedding_model, confidence, bbox scaled from what the mini
    saw back into working-copy pixels using the returned `image_w`/`image_h`
    and `photos.width`/`height`).
  - Zero faces → one `suggestions` row kind='classification' payload
    {"label":"no_people"}. Never sets `has_no_people` directly.
  - Precomputes a face-crop thumbnail at THUMBS_DIR/faces/{face_id}.jpg so
    the Faces mode's cluster grid stays cheap.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import psycopg
from PIL import Image, ImageOps

from ..config import load as load_settings
from ..inference_client import ENDPOINT_DETECT_FACES, RefImage, ResultLine
from .base import Job, JobContext, SelectedItem, Selector, Uploader, Writer

log = logging.getLogger(__name__)


class FacesSelector(Selector):
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
              on pjs.photo_id = p.id and pjs.job_name = 'detect_faces'
            where p.triage_status in ('keep','private')
              and not p.is_deleted
              and p.working_path is not null
              and (
                pjs.photo_id is null
                or pjs.status <> 'done'
                or coalesce(pjs.model, '') <> %s
              )
            order by p.id
            """
            + ("" if limit is None else " limit %s"),
            (model,) if limit is None else (model, limit),
        ).fetchall()
        return [
            SelectedItem(ref=str(pid), photo_id=pid, working_path=wp)
            for pid, wp in rows
        ]


class FacesUploader(Uploader):
    def prepare(self, item: SelectedItem, max_edge: int) -> RefImage:
        return RefImage(ref=item.ref, path=Path(item.working_path))


class FacesWriter(Writer):
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
            log.warning("detect_faces: %s failed: %s", line.ref, line.error)
            return "error"

        model = line.model or ctx.model or "unknown"
        # Skip if faces from this model already exist for this photo — the
        # writer is idempotent under result-line replay.
        existing = conn.execute(
            """
            select count(*) from faces
            where photo_id = %s and coalesce(embedding_model, '') = %s
              and not is_deleted
            """,
            (photo_id, model),
        ).fetchone()
        if existing and existing[0] > 0:
            # Still need to record the zero-face suggestion if that was the
            # outcome? No — if we already have rows, this is a duplicate.
            return "skipped_duplicate"

        result = line.result or {}
        image_w = int(result.get("image_w") or 0)
        image_h = int(result.get("image_h") or 0)
        faces = result.get("faces") or []

        # Working-copy dimensions for bbox scale-back.
        dims = conn.execute(
            "select width, height, working_path from photos where id = %s", (photo_id,)
        ).fetchone()
        if not dims:
            return "skipped_missing_photo"
        wc_w, wc_h, working_path = dims
        if not wc_w or not wc_h:
            log.warning("detect_faces: photo %d has no width/height; using image_w/h", photo_id)
            wc_w, wc_h = image_w or 1, image_h or 1
        if not image_w or not image_h:
            # Nothing to scale from; assume the mini scaled to working-copy
            # (this shouldn't happen — /detect-faces returns image_w/h).
            image_w, image_h = wc_w, wc_h

        sx = wc_w / image_w if image_w else 1.0
        sy = wc_h / image_h if image_h else 1.0

        settings = load_settings()
        (settings.THUMBS_DIR / "faces").mkdir(parents=True, exist_ok=True)

        inserted_faces: list[tuple[int, dict[str, float]]] = []
        for face in faces:
            bbox = face.get("bbox") or {}
            x = float(bbox.get("x", 0)) * sx
            y = float(bbox.get("y", 0)) * sy
            w = float(bbox.get("w", 0)) * sx
            h = float(bbox.get("h", 0)) * sy
            wc_bbox = {"x": x, "y": y, "w": w, "h": h}
            embedding = face.get("embedding") or None
            det_score = face.get("det_score")
            row = conn.execute(
                """
                insert into faces
                  (photo_id, person_id, bbox, embedding, embedding_model,
                   confidence, source, is_disputed, is_deleted)
                values (%s, null, %s, %s, %s, %s, 'ai', false, false)
                returning id
                """,
                (
                    photo_id,
                    json.dumps(wc_bbox),
                    embedding,
                    model,
                    float(det_score) if det_score is not None else None,
                ),
            ).fetchone()
            inserted_faces.append((row[0], wc_bbox))

        # Zero-face → classification suggestion (never set has_no_people).
        if not faces:
            _insert_no_people_suggestion(
                conn,
                photo_id=photo_id,
                model=model,
                prompt_version=line.prompt_version or ctx.prompt_version or "",
            )

        # Precompute face crop thumbnails outside the DB txn's scope
        # (safe — files, not DB). Failures just log; the crop is regenerable.
        if inserted_faces and working_path:
            _write_face_crops(Path(working_path), inserted_faces, settings.THUMBS_DIR / "faces")

        return "ok"


# --- helpers --------------------------------------------------------------


def _photo_id(ref: str) -> int | None:
    try:
        return int(ref)
    except (TypeError, ValueError):
        return None


def _insert_no_people_suggestion(
    conn: psycopg.Connection,
    *,
    photo_id: int,
    model: str,
    prompt_version: str,
) -> None:
    exists = conn.execute(
        """
        select 1 from suggestions
        where kind = 'classification'
          and source = 'ai'
          and photo_id = %s
          and coalesce(model, '') = %s
          and (payload ->> 'label') = 'no_people'
        limit 1
        """,
        (photo_id, model),
    ).fetchone()
    if exists:
        return
    payload = {"label": "no_people", "prompt_version": prompt_version}
    conn.execute(
        """
        insert into suggestions (photo_id, kind, payload, source, model)
        values (%s, 'classification', %s, 'ai', %s)
        """,
        (photo_id, json.dumps(payload), model),
    )


def _write_face_crops(
    working_path: Path,
    inserted_faces: list[tuple[int, dict[str, float]]],
    out_dir: Path,
) -> None:
    if not working_path.exists():
        log.warning("detect_faces: working file missing for face crops: %s", working_path)
        return
    try:
        with Image.open(working_path) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            W, H = im.size
            for face_id, bbox in inserted_faces:
                x = max(0, int(round(bbox["x"])))
                y = max(0, int(round(bbox["y"])))
                w = max(1, int(round(bbox["w"])))
                h = max(1, int(round(bbox["h"])))
                x2 = min(W, x + w)
                y2 = min(H, y + h)
                # A little padding so hair/chin aren't clipped in the grid.
                pad_x = int(round(0.15 * (x2 - x)))
                pad_y = int(round(0.15 * (y2 - y)))
                cx = max(0, x - pad_x)
                cy = max(0, y - pad_y)
                cx2 = min(W, x2 + pad_x)
                cy2 = min(H, y2 + pad_y)
                crop = im.crop((cx, cy, cx2, cy2))
                # Uniform 256px edge for the cluster grid.
                crop.thumbnail((256, 256), Image.LANCZOS)
                out_path = out_dir / f"{face_id}.jpg"
                crop.save(out_path, format="JPEG", quality=88, optimize=True)
    except Exception as e:
        log.warning("detect_faces: face-crop generation failed for %s: %s", working_path, e)


def make_job() -> Job:
    return Job(
        name="detect_faces",
        endpoint=ENDPOINT_DETECT_FACES,
        selector=FacesSelector(),
        uploader=FacesUploader(),
        writer=FacesWriter(),
        updates_photo_job_status=True,
    )
