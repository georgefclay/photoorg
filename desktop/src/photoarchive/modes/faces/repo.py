"""DB helpers for the Faces mode. Keeps SQL out of the widgets so the UI
stays focused on interactions."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Iterable

import numpy as np
import psycopg

from ... import db as dbmod

log = logging.getLogger(__name__)


@dataclass
class FaceRow:
    id: int
    photo_id: int
    person_id: int | None
    bbox: dict
    embedding: list[float] | None
    embedding_model: str | None
    confidence: float | None
    is_disputed: bool
    is_deleted: bool
    # Fix-up 8: for unassigned faces, one of 'pending' | 'unknown' | 'ignore'.
    review_status: str = "pending"
    # Populated by unlabelled_faces_with_embeddings when we join the
    # photos row for the year-context display in the cluster grid.
    photo_capture_year: int | None = None
    photo_source_folder: str | None = None

    @property
    def short_edge_px(self) -> float | None:
        w = self.bbox.get("w") if isinstance(self.bbox, dict) else None
        h = self.bbox.get("h") if isinstance(self.bbox, dict) else None
        if w is None or h is None:
            return None
        try:
            return float(min(w, h))
        except (TypeError, ValueError):
            return None


@dataclass
class PersonRow:
    id: int
    display_name: str
    given_name: str | None
    middle_name: str | None
    surname: str | None
    maiden_name: str | None
    nickname: str | None
    birth_year: int | None
    death_year: int | None
    notes: str | None
    is_deleted: bool
    suffix: str | None = None  # Phase 6 fix-up 4: Jr./II/III/…


def photo_ids_with_backs(
    conn: psycopg.Connection, photo_ids: list[int],
) -> set[int]:
    """Fix-up 10 item 4: the cluster grid tags tiles whose photo has any
    `photo_backs` row with a ✎ badge. One query for the batch."""
    if not photo_ids:
        return set()
    rows = conn.execute(
        """
        select distinct photo_id
        from photo_backs
        where photo_id = ANY(%s)
        """,
        (list(photo_ids),),
    ).fetchall()
    return {int(r[0]) for r in rows}


def unlabelled_faces_with_embeddings(
    conn: psycopg.Connection,
) -> list[FaceRow]:
    """Every unlabelled, non-deleted face with an embedding, joined to
    its photo for the year context shown under each crop. Excludes
    faces marked `ignore` (fix-up 8) — those are noise and never come
    back into the labelling flow. `unknown` faces ARE included so their
    clusters can later match against a labelled person's prototype."""
    rows = conn.execute(
        """
        select f.id, f.photo_id, f.person_id, f.bbox, f.embedding,
               f.embedding_model, f.confidence, f.is_disputed, f.is_deleted,
               f.review_status,
               extract(year from p.capture_date)::int as capture_year,
               p.source_folder
        from faces f
        join photos p on p.id = f.photo_id
        where f.person_id is null
          and not f.is_deleted
          and not p.is_deleted
          and f.embedding is not null
          and f.review_status <> 'ignore'
        order by f.id
        """
    ).fetchall()
    return [_face_with_photo(row) for row in rows]


def faces_for_person(conn: psycopg.Connection, person_id: int) -> list[FaceRow]:
    rows = conn.execute(
        """
        select id, photo_id, person_id, bbox, embedding, embedding_model,
               confidence, is_disputed, is_deleted
        from faces
        where person_id = %s and not is_deleted
        order by id
        """,
        (person_id,),
    ).fetchall()
    return [_face(row) for row in rows]


@dataclass
class PhotoContext:
    """Everything the fix-up 5 preview needs about one photo in one round-trip."""
    photo_id: int
    working_path: str | None
    width: int | None
    height: int | None
    capture_year: int | None
    source_folder: str | None
    scan_batch: str | None
    scan_sequence: int | None
    back_transcription: str | None
    faces: list["PhotoFaceOverlay"]
    # Fix-up 10: everything the Back panel needs.
    backs: list["PhotoBack"] = field(default_factory=list)
    date_suggestions: list["DateSuggestion"] = field(default_factory=list)
    description_suggestion: str | None = None
    folder_hint: str | None = None
    has_confirmed_date: bool = False


@dataclass
class PhotoBack:
    id: int
    working_path: str | None
    master_path: str
    transcribed_text: str | None
    transcription_confidence: float | None
    transcription_confirmed: bool
    orientation_used: str | None    # from the transcription suggestion payload
    parsed_dates: list[dict]         # [{text, iso, precision}, ...]
    names: list[str]


@dataclass
class DateSuggestion:
    suggestion_id: int
    date: str | None                 # ISO or None
    precision: str
    confidence: float | None
    evidence: str
    year_range: tuple[int, int] | None


@dataclass
class PhotoFaceOverlay:
    face_id: int
    bbox: dict
    person_id: int | None
    person_name: str | None
    is_disputed: bool
    review_status: str = "pending"


def load_photo_context(conn: psycopg.Connection, photo_id: int) -> PhotoContext | None:
    """Photo row + faces (with person names) + one back transcription.
    Returns None if the photo row is missing or deleted."""
    prow = conn.execute(
        """
        select p.working_path, p.width, p.height,
               extract(year from p.capture_date)::int as capture_year,
               p.source_folder, p.scan_batch, p.scan_sequence
        from photos p
        where p.id = %s and not p.is_deleted
        """,
        (photo_id,),
    ).fetchone()
    if prow is None:
        return None
    working_path, width, height, capture_year, source_folder, scan_batch, scan_sequence = prow

    face_rows = conn.execute(
        """
        select f.id, f.bbox, f.person_id, pe.display_name, f.is_disputed,
               f.review_status
        from faces f
        left join people pe on pe.id = f.person_id and not pe.is_deleted
        where f.photo_id = %s and not f.is_deleted
        order by f.id
        """,
        (photo_id,),
    ).fetchall()
    faces = []
    for fid, bbox, person_id, display_name, is_disputed, review_status in face_rows:
        faces.append(PhotoFaceOverlay(
            face_id=int(fid),
            bbox=bbox if isinstance(bbox, dict) else json.loads(bbox),
            person_id=int(person_id) if person_id is not None else None,
            person_name=display_name,
            is_disputed=bool(is_disputed),
            review_status=review_status or "pending",
        ))

    # All photo_backs rows on this photo, with the newest AI transcription
    # suggestion joined so the preview can show orientation_used + chips.
    back_rows = conn.execute(
        """
        select b.id, b.working_path, b.master_path,
               b.transcribed_text, b.transcription_confidence,
               b.transcription_confirmed,
               (
                 select payload from suggestions s
                 where s.kind = 'transcription' and s.source = 'ai'
                   and (s.payload->>'photo_back_id')::bigint = b.id
                 order by s.confidence desc nulls last, s.id desc
                 limit 1
               ) as suggestion_payload
        from photo_backs b
        where b.photo_id = %s
        order by b.id
        """,
        (photo_id,),
    ).fetchall()
    backs: list[PhotoBack] = []
    for bid, bwp, bmp, text, conf, confirmed, payload in back_rows:
        payload = payload if isinstance(payload, dict) else (
            json.loads(payload) if payload else {}
        )
        backs.append(PhotoBack(
            id=int(bid), working_path=bwp, master_path=bmp,
            transcribed_text=text,
            transcription_confidence=float(conf) if conf is not None else None,
            transcription_confirmed=bool(confirmed),
            orientation_used=payload.get("orientation_used"),
            parsed_dates=list(payload.get("parsed_dates") or []),
            names=list(payload.get("names") or []),
        ))
    back_text = next(
        (b.transcribed_text for b in backs if b.transcribed_text), None,
    )

    # Pending date + description suggestions for the "Suggestions" block.
    date_rows = conn.execute(
        """
        select id, payload, confidence
        from suggestions
        where photo_id = %s and kind = 'date' and status = 'pending'
        order by confidence desc nulls last, id desc
        """,
        (photo_id,),
    ).fetchall()
    dates: list[DateSuggestion] = []
    for sid, payload, conf in date_rows:
        payload = payload if isinstance(payload, dict) else json.loads(payload)
        rng = payload.get("range") or {}
        yr = None
        if "year_min" in rng and "year_max" in rng:
            try:
                yr = (int(rng["year_min"]), int(rng["year_max"]))
            except (TypeError, ValueError):
                yr = None
        dates.append(DateSuggestion(
            suggestion_id=int(sid),
            date=payload.get("date"),
            precision=payload.get("precision") or "unknown",
            confidence=float(conf) if conf is not None else None,
            evidence=payload.get("evidence") or "",
            year_range=yr,
        ))

    desc_row = conn.execute(
        """
        select payload from suggestions
        where photo_id = %s and kind = 'description' and status = 'pending'
        order by id desc limit 1
        """,
        (photo_id,),
    ).fetchone()
    if desc_row:
        payload = desc_row[0] if isinstance(desc_row[0], dict) else json.loads(desc_row[0])
        description_suggestion = payload.get("text")
    else:
        description_suggestion = None

    has_confirmed = conn.execute(
        "select capture_date_confirmed from photos where id = %s", (photo_id,)
    ).fetchone()
    has_confirmed_flag = bool(has_confirmed[0]) if has_confirmed else False

    return PhotoContext(
        photo_id=photo_id,
        working_path=working_path,
        width=int(width) if width is not None else None,
        height=int(height) if height is not None else None,
        capture_year=int(capture_year) if capture_year is not None else None,
        source_folder=source_folder,
        scan_batch=scan_batch,
        scan_sequence=int(scan_sequence) if scan_sequence is not None else None,
        back_transcription=back_text,
        faces=faces,
        backs=backs,
        date_suggestions=dates,
        description_suggestion=description_suggestion,
        folder_hint=source_folder,
        has_confirmed_date=has_confirmed_flag,
    )


def faces_for_photo(conn: psycopg.Connection, photo_id: int) -> list[FaceRow]:
    rows = conn.execute(
        """
        select id, photo_id, person_id, bbox, embedding, embedding_model,
               confidence, is_disputed, is_deleted
        from faces
        where photo_id = %s and not is_deleted
        order by id
        """,
        (photo_id,),
    ).fetchall()
    return [_face(row) for row in rows]


def search_people(
    conn: psycopg.Connection, query: str, limit: int = 25,
) -> list[PersonRow]:
    """Fix-up 9 assign dialog: prefix match on any name field (given,
    surname, maiden, nickname, suffix) OR trigram similarity on
    display_name and any hand-curated name variant. Returns ordered by
    a simple relevance score: prefix hits before trigram, alpha within
    tier. Empty query returns list_people()."""
    q = (query or "").strip()
    if not q:
        return list_people(conn)
    like = f"{q.lower()}%"
    rows = conn.execute(
        """
        with matches as (
          select p.id,
                 case
                   when lower(coalesce(p.given_name, '')) like %s then 3
                   when lower(coalesce(p.surname, '')) like %s then 3
                   when lower(coalesce(p.maiden_name, '')) like %s then 2
                   when lower(coalesce(p.nickname, '')) like %s then 2
                   when lower(coalesce(p.suffix, '')) like %s then 1
                   when exists (
                     select 1 from person_name_variants v
                     where v.person_id = p.id and lower(v.variant) like %s
                   ) then 2
                   when similarity(coalesce(p.display_name, ''), %s) > 0.15 then 1
                   else 0
                 end as score
          from people p
          where not p.is_deleted
        )
        select p.id, p.display_name, p.given_name, p.middle_name, p.surname,
               p.maiden_name, p.nickname, p.birth_year, p.death_year, p.notes,
               p.is_deleted, p.suffix, m.score
        from people p
        join matches m on m.id = p.id
        where m.score > 0
        order by m.score desc, lower(coalesce(p.display_name, ''))
        limit %s
        """,
        (like, like, like, like, like, like, q, limit),
    ).fetchall()
    # Strip the trailing score column before mapping to PersonRow.
    return [_person(r[:-1]) for r in rows]


def list_people(conn: psycopg.Connection) -> list[PersonRow]:
    rows = conn.execute(
        """
        select id, display_name, given_name, middle_name, surname,
               maiden_name, nickname, birth_year, death_year, notes,
               is_deleted, suffix
        from people
        where not is_deleted
        order by lower(coalesce(display_name, ''))
        """
    ).fetchall()
    return [_person(row) for row in rows]


def get_person(conn: psycopg.Connection, person_id: int) -> PersonRow | None:
    row = conn.execute(
        """
        select id, display_name, given_name, middle_name, surname,
               maiden_name, nickname, birth_year, death_year, notes,
               is_deleted, suffix
        from people where id = %s
        """,
        (person_id,),
    ).fetchone()
    return _person(row) if row else None


def person_prototypes(
    conn: psycopg.Connection,
    *,
    max_prototypes: int = 5,
    min_score: float | None = None,
    min_short_edge_px: float | None = None,
) -> dict[int, list[np.ndarray]]:
    # Note: `review_status` is orthogonal to `person_id` — an assigned
    # face keeps `pending` status per spec — so no explicit filter here.
    # `ignore` faces are always unassigned, so they can't feed into
    # `person_id is not null` queries anyway. Defensive filter kept
    # below for clarity.
    """Per-person K-means (up to `max_prototypes` centroids) over their
    non-disputed, non-deleted, quality-gated embeddings. Faces are
    partitioned across age / hairstyle / lighting bands so a single mean
    can't cover the whole lifetime (fix-up 3). Persons with < 10 faces
    get `min(k, faces)` prototypes; < 2 faces just return their
    embedding as one prototype.
    """
    from scipy.cluster.vq import kmeans2

    rows = conn.execute(
        """
        select person_id, embedding, confidence, bbox
        from faces
        where person_id is not null
          and not is_disputed
          and not is_deleted
          and embedding is not null
          and review_status <> 'ignore'
        """
    ).fetchall()
    per_person: dict[int, list[np.ndarray]] = {}
    for pid, emb, conf, bbox in rows:
        if emb is None:
            continue
        if min_score is not None and conf is not None and conf < min_score:
            continue
        if min_short_edge_px is not None:
            se = _short_edge(bbox)
            if se is not None and se < min_short_edge_px:
                continue
        per_person.setdefault(pid, []).append(np.asarray(emb, dtype=np.float32))

    out: dict[int, list[np.ndarray]] = {}
    for pid, embs in per_person.items():
        arr = np.stack(embs)
        n = arr.shape[0]
        if n <= 2:
            out[pid] = [np.mean(arr, axis=0)]
            continue
        # Full k when the person has enough faces; scale down otherwise
        # so tiny sample sizes don't get one-face-per-prototype clusters.
        k = min(max_prototypes, max(2, n // 4))
        try:
            centroids, _labels = kmeans2(
                arr, k, minit="++", seed=0, missing="warn",
            )
        except Exception as e:
            log.warning("person_prototypes: kmeans2 failed for %d (%s); "
                        "falling back to single mean", pid, e)
            out[pid] = [np.mean(arr, axis=0)]
            continue
        # kmeans2 with 'warn' can leave empty centroids — drop the ones
        # that are all-zero (uninitialised) or NaN.
        good = []
        for c in centroids:
            if np.any(np.isnan(c)):
                continue
            if float(np.linalg.norm(c)) < 1e-9:
                continue
            good.append(c.astype(np.float32))
        if not good:
            out[pid] = [np.mean(arr, axis=0)]
        else:
            out[pid] = good
    return out


def person_reference_means(
    conn: psycopg.Connection,
    *,
    min_score: float | None = None,
    min_short_edge_px: float | None = None,
) -> dict[int, np.ndarray]:
    """Mean embedding per non-deleted person, computed over their
    non-disputed, non-deleted, embedded faces. Faces below the quality
    gate (fix-up 2) are excluded from the reference set — a wrong tag
    on a blurry crop must not poison future matches. Pass min_score /
    min_short_edge_px = None to get the raw un-gated mean."""
    rows = conn.execute(
        """
        select person_id, embedding, confidence, bbox
        from faces
        where person_id is not null
          and not is_disputed
          and not is_deleted
          and embedding is not null
          and review_status <> 'ignore'
        """
    ).fetchall()
    accum: dict[int, list[np.ndarray]] = {}
    for pid, emb, conf, bbox in rows:
        if emb is None:
            continue
        if min_score is not None and conf is not None and conf < min_score:
            continue
        if min_short_edge_px is not None:
            se = _short_edge(bbox)
            if se is not None and se < min_short_edge_px:
                continue
        accum.setdefault(pid, []).append(np.asarray(emb, dtype=np.float32))
    means: dict[int, np.ndarray] = {}
    for pid, embs in accum.items():
        means[pid] = np.mean(np.stack(embs), axis=0)
    return means


def _short_edge(bbox) -> float | None:
    if bbox is None:
        return None
    if not isinstance(bbox, dict):
        try:
            bbox = json.loads(bbox)
        except Exception:
            return None
    w, h = bbox.get("w"), bbox.get("h")
    if w is None or h is None:
        return None
    try:
        return float(min(w, h))
    except (TypeError, ValueError):
        return None


def create_person(
    conn: psycopg.Connection,
    *,
    given_name: str | None,
    middle_name: str | None,
    surname: str | None,
    maiden_name: str | None,
    nickname: str | None,
    birth_year: int | None,
    death_year: int | None,
    notes: str | None,
    suffix: str | None = None,
) -> int:
    row = conn.execute(
        """
        insert into people
          (given_name, middle_name, surname, maiden_name, nickname,
           birth_year, death_year, notes, suffix, is_deleted)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, false)
        returning id
        """,
        (given_name, middle_name, surname, maiden_name, nickname,
         birth_year, death_year, notes, suffix),
    ).fetchone()
    person_id = int(row[0])
    dbmod.audit(
        conn, actor="desktop", action="person.create",
        entity_type="person", entity_id=person_id,
        new_value={
            "given_name": given_name, "surname": surname,
            "nickname": nickname, "birth_year": birth_year,
            "suffix": suffix,
        },
    )
    return person_id


def list_name_variants(conn: psycopg.Connection, person_id: int) -> list[tuple[int, str, str]]:
    """(variant_id, variant, kind) — a person's hand-curated aliases."""
    rows = conn.execute(
        """
        select id, variant, kind::text
        from person_name_variants
        where person_id = %s
        order by lower(variant)
        """,
        (person_id,),
    ).fetchall()
    return [(int(r[0]), r[1], r[2]) for r in rows]


def add_name_variant(
    conn: psycopg.Connection,
    *,
    person_id: int,
    variant: str,
    kind: str = "nickname",
) -> int | None:
    """Case-insensitively unique per person; returns the new id or None
    if the variant already exists on this person."""
    if not variant.strip():
        return None
    row = conn.execute(
        """
        insert into person_name_variants (person_id, variant, kind)
        values (%s, %s, %s)
        on conflict do nothing
        returning id
        """,
        (person_id, variant.strip(), kind),
    ).fetchone()
    if row is None:
        return None
    variant_id = int(row[0])
    dbmod.audit(
        conn, actor="desktop", action="person.variant.add",
        entity_type="person", entity_id=person_id,
        new_value={"variant": variant.strip(), "kind": kind, "variant_id": variant_id},
    )
    return variant_id


def remove_name_variant(conn: psycopg.Connection, variant_id: int) -> None:
    row = conn.execute(
        "select person_id, variant, kind from person_name_variants where id = %s",
        (variant_id,),
    ).fetchone()
    if row is None:
        return
    conn.execute("delete from person_name_variants where id = %s", (variant_id,))
    dbmod.audit(
        conn, actor="desktop", action="person.variant.remove",
        entity_type="person", entity_id=int(row[0]),
        previous_value={"variant": row[1], "kind": row[2], "variant_id": variant_id},
    )


@dataclass
class DateAcceptResult:
    accepted: bool
    conflict: bool
    existing_date: str | None
    existing_precision: str | None
    new_date: str | None
    new_precision: str | None


def promote_date_suggestion(
    conn: psycopg.Connection,
    *,
    suggestion_id: int,
    allow_overwrite: bool = False,
) -> DateAcceptResult:
    """Fix-up 10: 'Accept date' promotes a `suggestions` row of kind
    'date' to `photos.capture_date` + precision + confirmed=true. Writes
    an audit row and refreshes the completeness score. If the target
    photo already has a confirmed date and `allow_overwrite` is False,
    returns `conflict=True` with the existing values so the UI can
    prompt."""
    row = conn.execute(
        """
        select photo_id, kind::text, payload, status::text
        from suggestions where id = %s
        """,
        (suggestion_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"suggestion {suggestion_id} not found")
    photo_id, kind, payload, status = row
    if kind != "date":
        raise ValueError(f"suggestion {suggestion_id} is kind {kind!r}, not 'date'")
    if status != "pending":
        raise ValueError(f"suggestion {suggestion_id} is already {status}")
    if photo_id is None:
        raise ValueError("date suggestion has no photo_id")
    payload = payload if isinstance(payload, dict) else json.loads(payload)
    new_date = payload.get("date")
    new_precision = payload.get("precision") or "unknown"

    existing = conn.execute(
        """
        select capture_date::text, capture_date_precision::text,
               capture_date_confirmed
        from photos where id = %s
        """,
        (photo_id,),
    ).fetchone()
    if existing is None:
        raise ValueError(f"photo {photo_id} not found")
    exist_date, exist_prec, exist_confirmed = existing

    if exist_confirmed and not allow_overwrite:
        return DateAcceptResult(
            accepted=False, conflict=True,
            existing_date=exist_date, existing_precision=exist_prec,
            new_date=new_date, new_precision=new_precision,
        )

    conn.execute(
        """
        update photos
        set capture_date = %s::date,
            capture_date_precision = %s::date_precision,
            capture_date_confirmed = true
        where id = %s
        """,
        (new_date, new_precision, photo_id),
    )
    conn.execute(
        """
        update suggestions
        set status = 'accepted', resolved_at = now()
        where id = %s
        """,
        (suggestion_id,),
    )
    dbmod.audit(
        conn, actor="desktop", action="photo.capture_date.set",
        entity_type="photo", entity_id=int(photo_id),
        previous_value={"capture_date": exist_date, "precision": exist_prec,
                        "confirmed": exist_confirmed},
        new_value={"capture_date": new_date, "precision": new_precision,
                   "confirmed": True, "suggestion_id": suggestion_id},
    )
    # Best-effort completeness refresh. The function may not exist in
    # every DB (older test DBs?), so guard.
    try:
        conn.execute("select refresh_completeness(%s)", (int(photo_id),))
    except Exception:
        pass
    return DateAcceptResult(
        accepted=True, conflict=False,
        existing_date=exist_date, existing_precision=exist_prec,
        new_date=new_date, new_precision=new_precision,
    )


def confirm_back_transcription(
    conn: psycopg.Connection,
    *,
    photo_back_id: int,
    edited_text: str | None = None,
    confidence: float | None = None,
) -> None:
    """Fix-up 10 item 2: George confirms (or edits and confirms) the
    OCR/VLM transcription on a photo_backs row. Sets
    `transcription_confirmed = true` and, if `edited_text` is given,
    updates `transcribed_text` too. Writes `back.transcription_edit`
    audit."""
    row = conn.execute(
        """
        select transcribed_text, transcription_confidence, transcription_confirmed
        from photo_backs where id = %s
        """,
        (photo_back_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"photo_back {photo_back_id} not found")
    prev_text, prev_conf, prev_confirmed = row
    if edited_text is not None:
        conn.execute(
            """
            update photo_backs
            set transcribed_text = %s,
                transcription_confidence = coalesce(%s, transcription_confidence),
                transcription_confirmed = true
            where id = %s
            """,
            (edited_text, confidence, photo_back_id),
        )
        action = "back.transcription_edit"
        new_val = {
            "transcribed_text": edited_text,
            "transcription_confidence": confidence or prev_conf,
            "transcription_confirmed": True,
        }
    else:
        conn.execute(
            """
            update photo_backs
            set transcription_confirmed = true
            where id = %s
            """,
            (photo_back_id,),
        )
        action = "back.transcription_confirm"
        new_val = {"transcription_confirmed": True}
    dbmod.audit(
        conn, actor="desktop", action=action,
        entity_type="photo_back", entity_id=photo_back_id,
        previous_value={
            "transcribed_text": prev_text,
            "transcription_confidence": prev_conf,
            "transcription_confirmed": bool(prev_confirmed),
        },
        new_value=new_val,
    )


def photos_for_person(conn: psycopg.Connection, person_id: int) -> list[int]:
    """Distinct photo ids where this person has at least one non-deleted face."""
    rows = conn.execute(
        """
        select distinct photo_id
        from faces
        where person_id = %s and not is_deleted
        order by photo_id
        """,
        (person_id,),
    ).fetchall()
    return [int(r[0]) for r in rows]


def face_count_for_person(conn: psycopg.Connection, person_id: int) -> int:
    row = conn.execute(
        """
        select count(*) from faces
        where person_id = %s and not is_deleted
        """,
        (person_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def update_person(conn: psycopg.Connection, person_id: int, **fields) -> None:
    cols = []
    vals = []
    prev = get_person(conn, person_id)
    for key, value in fields.items():
        cols.append(f"{key} = %s")
        vals.append(value)
    if not cols:
        return
    vals.append(person_id)
    conn.execute(f"update people set {', '.join(cols)} where id = %s", vals)
    dbmod.audit(
        conn, actor="desktop", action="person.update",
        entity_type="person", entity_id=person_id,
        previous_value=(prev.__dict__ if prev else None),
        new_value=fields,
    )


def assign_face(
    conn: psycopg.Connection,
    *,
    face_id: int,
    person_id: int,
    source: str = "human",
    audit_reason: str = "assign_from_cluster",
) -> None:
    prev = conn.execute(
        "select person_id, source from faces where id = %s", (face_id,)
    ).fetchone()
    conn.execute(
        """
        update faces
        set person_id = %s, source = %s, is_disputed = false, disputed_by = null,
            dispute_note = null
        where id = %s
        """,
        (person_id, source, face_id),
    )
    dbmod.audit(
        conn, actor="desktop", action="face.assign",
        entity_type="face", entity_id=face_id,
        previous_value={"person_id": prev[0] if prev else None,
                        "source": prev[1] if prev else None},
        new_value={"person_id": person_id, "source": source,
                   "reason": audit_reason},
    )


def dispute_face(
    conn: psycopg.Connection,
    *,
    face_id: int,
    note: str | None = None,
) -> None:
    prev = conn.execute(
        "select person_id, is_disputed from faces where id = %s", (face_id,)
    ).fetchone()
    conn.execute(
        """
        update faces
        set is_disputed = true, disputed_by = null, dispute_note = %s
        where id = %s
        """,
        (note, face_id),
    )
    dbmod.audit(
        conn, actor="desktop", action="face.dispute",
        entity_type="face", entity_id=face_id,
        previous_value={"person_id": prev[0] if prev else None,
                        "was_disputed": bool(prev[1]) if prev else None},
        new_value={"is_disputed": True, "note": note},
    )


def set_review_status(
    conn: psycopg.Connection,
    *,
    face_ids: list[int],
    new_status: str,
    note: str | None = None,
) -> list[tuple[int, str]]:
    """Set faces.review_status on a batch of faces (fix-up 8). Returns
    a list of (face_id, previous_status) so an undo can restore. Writes
    one `face.review` audit row per face.
    """
    if new_status not in ("pending", "unknown", "ignore"):
        raise ValueError(f"invalid review status {new_status!r}")
    if not face_ids:
        return []
    rows = conn.execute(
        "select id, review_status from faces where id = ANY(%s)",
        (list(face_ids),),
    ).fetchall()
    previous = {int(r[0]): (r[1] or "pending") for r in rows}
    conn.execute(
        """
        update faces
        set review_status = %s,
            review_note = coalesce(%s, review_note),
            reviewed_at = case when %s = 'pending' then null else now() end
        where id = ANY(%s)
        """,
        (new_status, note, new_status, list(face_ids)),
    )
    for fid in face_ids:
        prev = previous.get(int(fid), "pending")
        dbmod.audit(
            conn, actor="desktop", action="face.review",
            entity_type="face", entity_id=int(fid),
            previous_value={"review_status": prev},
            new_value={"review_status": new_status, "note": note},
        )
    return [(fid, previous.get(int(fid), "pending")) for fid in face_ids]


def restore_review_status(
    conn: psycopg.Connection,
    *,
    face_id: int,
    status: str,
) -> None:
    """Set faces.review_status back to `status` (used by the Faces
    mode's Z key on a U/I action). Writes a `face.review.undo` audit
    row."""
    current = conn.execute(
        "select review_status from faces where id = %s", (face_id,)
    ).fetchone()
    prev = (current[0] if current else None) or "pending"
    conn.execute(
        """
        update faces
        set review_status = %s,
            reviewed_at = case when %s = 'pending' then null else now() end
        where id = %s
        """,
        (status, status, face_id),
    )
    dbmod.audit(
        conn, actor="desktop", action="face.review.undo",
        entity_type="face", entity_id=face_id,
        previous_value={"review_status": prev},
        new_value={"review_status": status},
    )


def soft_delete_face(
    conn: psycopg.Connection,
    *,
    face_id: int,
    reason: str = "not_a_face",
) -> None:
    prev = conn.execute(
        "select person_id, is_deleted from faces where id = %s", (face_id,)
    ).fetchone()
    conn.execute(
        """
        update faces
        set is_deleted = true, deleted_at = now(), delete_reason = %s
        where id = %s
        """,
        (reason, face_id),
    )
    dbmod.audit(
        conn, actor="desktop", action="face.delete",
        entity_type="face", entity_id=face_id,
        previous_value={"person_id": prev[0] if prev else None,
                        "was_deleted": bool(prev[1]) if prev else None},
        new_value={"is_deleted": True, "reason": reason},
    )


def update_face_bbox(
    conn: psycopg.Connection,
    *,
    face_id: int,
    new_bbox: dict,
    source: str = "human",
    reason: str = "manual_bbox_edit",
) -> dict | None:
    """Fix-up 9: manual bbox adjustment from the preview. Updates
    `faces.bbox`, marks the source `human`, and writes a
    `face.bbox_edit` audit row with previous and new bboxes. The
    embedding stays put (potentially stale) — the caller decides
    whether to refresh it via `/detect-faces` or set
    `embedding_stale=true`."""
    prev = conn.execute(
        "select bbox, source from faces where id = %s", (face_id,)
    ).fetchone()
    if prev is None:
        return None
    prev_bbox = prev[0] if isinstance(prev[0], dict) else json.loads(prev[0])
    conn.execute(
        """
        update faces
        set bbox = %s, source = %s
        where id = %s
        """,
        (json.dumps(new_bbox), source, face_id),
    )
    dbmod.audit(
        conn, actor="desktop", action="face.bbox_edit",
        entity_type="face", entity_id=face_id,
        previous_value={"bbox": prev_bbox, "source": prev[1]},
        new_value={"bbox": new_bbox, "source": source, "reason": reason},
    )
    return prev_bbox


def refresh_face_embedding(
    conn: psycopg.Connection,
    *,
    face_id: int,
    embedding: list[float],
    embedding_model: str,
) -> None:
    conn.execute(
        """
        update faces
        set embedding = %s,
            embedding_model = %s,
            embedding_stale = false
        where id = %s
        """,
        (embedding, embedding_model, face_id),
    )


def mark_face_embedding_stale(
    conn: psycopg.Connection, *, face_id: int, stale: bool = True,
) -> None:
    conn.execute(
        "update faces set embedding_stale = %s where id = %s",
        (stale, face_id),
    )


def stale_faces(conn: psycopg.Connection) -> list[FaceRow]:
    rows = conn.execute(
        """
        select id, photo_id, person_id, bbox, embedding, embedding_model,
               confidence, is_disputed, is_deleted
        from faces
        where embedding_stale and not is_deleted
        order by id
        """
    ).fetchall()
    return [_face(row) for row in rows]


def draw_face(
    conn: psycopg.Connection,
    *,
    photo_id: int,
    bbox: dict,
    embedding: list[float] | None,
    embedding_model: str | None,
    person_id: int | None,
) -> int:
    row = conn.execute(
        """
        insert into faces
          (photo_id, person_id, bbox, embedding, embedding_model,
           confidence, source, is_disputed, is_deleted)
        values (%s, %s, %s, %s, %s, null, 'human', false, false)
        returning id
        """,
        (photo_id, person_id, json.dumps(bbox), embedding, embedding_model),
    ).fetchone()
    face_id = int(row[0])
    dbmod.audit(
        conn, actor="desktop", action="face.draw",
        entity_type="face", entity_id=face_id,
        new_value={"photo_id": photo_id, "bbox": bbox,
                   "person_id": person_id, "has_embedding": embedding is not None},
    )
    return face_id


def flag_photo_needs_detection(
    conn: psycopg.Connection, *, photo_id: int,
) -> None:
    """Reset the detect_faces status for this photo so the next batch fills
    in the missing embedding for a manually-drawn box."""
    conn.execute(
        """
        insert into photo_job_status
          (photo_id, job_name, status, completed_at)
        values (%s, 'detect_faces', 'pending', null)
        on conflict (photo_id, job_name) do update
          set status = 'pending', completed_at = null
        """,
        (photo_id,),
    )


def _face(row) -> FaceRow:
    (id_, photo_id, person_id, bbox, embedding, embedding_model,
     confidence, is_disputed, is_deleted) = row
    return FaceRow(
        id=int(id_), photo_id=int(photo_id), person_id=person_id,
        bbox=bbox if isinstance(bbox, dict) else json.loads(bbox),
        embedding=list(embedding) if embedding is not None else None,
        embedding_model=embedding_model, confidence=confidence,
        is_disputed=bool(is_disputed), is_deleted=bool(is_deleted),
    )


def _face_with_photo(row) -> FaceRow:
    (id_, photo_id, person_id, bbox, embedding, embedding_model,
     confidence, is_disputed, is_deleted, review_status,
     capture_year, source_folder) = row
    return FaceRow(
        id=int(id_), photo_id=int(photo_id), person_id=person_id,
        bbox=bbox if isinstance(bbox, dict) else json.loads(bbox),
        embedding=list(embedding) if embedding is not None else None,
        embedding_model=embedding_model, confidence=confidence,
        is_disputed=bool(is_disputed), is_deleted=bool(is_deleted),
        review_status=review_status or "pending",
        photo_capture_year=int(capture_year) if capture_year is not None else None,
        photo_source_folder=source_folder,
    )


def _person(row) -> PersonRow:
    (id_, display_name, given_name, middle_name, surname, maiden_name,
     nickname, birth_year, death_year, notes, is_deleted, suffix) = row
    return PersonRow(
        id=int(id_), display_name=display_name or "",
        given_name=given_name, middle_name=middle_name, surname=surname,
        maiden_name=maiden_name, nickname=nickname,
        birth_year=birth_year, death_year=death_year, notes=notes,
        is_deleted=bool(is_deleted), suffix=suffix,
    )
