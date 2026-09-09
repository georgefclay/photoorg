"""DB helpers for the Faces mode. Keeps SQL out of the widgets so the UI
stays focused on interactions."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
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


def unlabelled_faces_with_embeddings(
    conn: psycopg.Connection,
) -> list[FaceRow]:
    """Every unlabelled, non-deleted face with an embedding, joined to
    its photo for the year context shown under each crop."""
    rows = conn.execute(
        """
        select f.id, f.photo_id, f.person_id, f.bbox, f.embedding,
               f.embedding_model, f.confidence, f.is_disputed, f.is_deleted,
               extract(year from p.capture_date)::int as capture_year,
               p.source_folder
        from faces f
        join photos p on p.id = f.photo_id
        where f.person_id is null
          and not f.is_deleted
          and not p.is_deleted
          and f.embedding is not null
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


def list_people(conn: psycopg.Connection) -> list[PersonRow]:
    rows = conn.execute(
        """
        select id, display_name, given_name, middle_name, surname,
               maiden_name, nickname, birth_year, death_year, notes, is_deleted
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
               maiden_name, nickname, birth_year, death_year, notes, is_deleted
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
) -> int:
    row = conn.execute(
        """
        insert into people
          (given_name, middle_name, surname, maiden_name, nickname,
           birth_year, death_year, notes, is_deleted)
        values (%s, %s, %s, %s, %s, %s, %s, %s, false)
        returning id
        """,
        (given_name, middle_name, surname, maiden_name, nickname,
         birth_year, death_year, notes),
    ).fetchone()
    person_id = int(row[0])
    dbmod.audit(
        conn, actor="desktop", action="person.create",
        entity_type="person", entity_id=person_id,
        new_value={
            "given_name": given_name, "surname": surname,
            "nickname": nickname, "birth_year": birth_year,
        },
    )
    return person_id


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
     confidence, is_disputed, is_deleted, capture_year, source_folder) = row
    return FaceRow(
        id=int(id_), photo_id=int(photo_id), person_id=person_id,
        bbox=bbox if isinstance(bbox, dict) else json.loads(bbox),
        embedding=list(embedding) if embedding is not None else None,
        embedding_model=embedding_model, confidence=confidence,
        is_disputed=bool(is_disputed), is_deleted=bool(is_deleted),
        photo_capture_year=int(capture_year) if capture_year is not None else None,
        photo_source_folder=source_folder,
    )


def _person(row) -> PersonRow:
    (id_, display_name, given_name, middle_name, surname, maiden_name,
     nickname, birth_year, death_year, notes, is_deleted) = row
    return PersonRow(
        id=int(id_), display_name=display_name or "",
        given_name=given_name, middle_name=middle_name, surname=surname,
        maiden_name=maiden_name, nickname=nickname,
        birth_year=birth_year, death_year=death_year, notes=notes,
        is_deleted=bool(is_deleted),
    )
