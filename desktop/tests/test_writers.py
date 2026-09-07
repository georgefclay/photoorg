"""Writer behaviour end-to-end against TEST_DATABASE_URL. Each test builds
minimal photo/back fixtures, hand-crafts a ResultLine as the mini would
return it, applies the writer, and asserts DB state."""

from __future__ import annotations

import json
import pytest

from photoarchive import db as dbmod
from photoarchive.inference_client import ResultLine
from photoarchive.jobs.base import JobContext
from photoarchive.jobs.classify import ClassifyWriter
from photoarchive.jobs.describe import DescribeWriter
from photoarchive.jobs.detect_faces import FacesWriter
from photoarchive.jobs.estimate_date import EstimateDateWriter
from photoarchive.jobs.transcribe_backs import BacksWriter

from .phase6_fixtures import (
    insert_back,
    insert_master,
    insert_photo,
    phase6,          # noqa: F401 — pytest fixture import
    write_test_jpeg,
)


def _line(ref: str, result: dict, *, ok: bool = True, model: str = "vlm-1",
          prompt_version: str = "v1", line_no: int = 1) -> ResultLine:
    return ResultLine(
        line_no=line_no, ref=ref, ok=ok, model=model,
        prompt_version=prompt_version, elapsed_ms=100,
        result=result, error=None, raw={"ref": ref, "result": result},
    )


def _ctx() -> JobContext:
    return JobContext(client=None, model="vlm-1", prompt_version="v1")


# --- classify -------------------------------------------------------------


def test_classify_writer_inserts_suggestion(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = insert_photo(conn)
        insert_master(conn, pid)
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = False
        line = _line(str(pid), {"label": "photo", "confidence": 0.87,
                                 "reason": "family portrait"})
        assert ClassifyWriter().apply(conn, line, _ctx()) == "ok"
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = True
        row = conn.execute(
            "select payload, model from suggestions where photo_id = %s and kind='classification'",
            (pid,),
        ).fetchone()
        assert row is not None
        payload = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        assert payload["label"] == "photo"


def test_classify_writer_is_idempotent(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = insert_photo(conn)
        insert_master(conn, pid)
        conn.commit()

    line = _line(str(pid), {"label": "photo", "confidence": 0.9, "reason": "x"})
    for _ in range(3):
        with dbmod.connection() as conn:
            conn.autocommit = False
            ClassifyWriter().apply(conn, line, _ctx())
            conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = True
        n = conn.execute(
            "select count(*) from suggestions where photo_id = %s and kind='classification'",
            (pid,),
        ).fetchone()[0]
        assert n == 1


def test_classify_ai_junk_hint_when_presort_absent(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = insert_photo(conn)
        insert_master(conn, pid)
        conn.commit()

    line = _line(str(pid), {"label": "document", "confidence": 0.95, "reason": "typed page"})
    with dbmod.connection() as conn:
        conn.autocommit = False
        ClassifyWriter().apply(conn, line, _ctx())
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = True
        row = conn.execute(
            "select hint, details from triage_hints where photo_id = %s",
            (pid,),
        ).fetchone()
        assert row is not None
        assert row[0] == "ai_junk"
        assert (row[1] or {})["also"]["ai_classify"]["label"] == "document"


def test_classify_presort_wins_over_ai_junk(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = insert_photo(conn)
        insert_master(conn, pid)
        # Presort already wrote a hint for this photo.
        conn.execute(
            """
            insert into triage_hints (photo_id, hint, confidence, details)
            values (%s, 'possible_back', 0.7, '{}'::jsonb)
            """,
            (pid,),
        )
        conn.commit()

    line = _line(str(pid), {"label": "receipt", "confidence": 0.99, "reason": "grocery"})
    with dbmod.connection() as conn:
        conn.autocommit = False
        ClassifyWriter().apply(conn, line, _ctx())
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = True
        row = conn.execute(
            "select hint, details from triage_hints where photo_id = %s",
            (pid,),
        ).fetchone()
        assert row[0] == "possible_back"  # presort wins
        also = (row[1] or {}).get("also") or {}
        assert also["ai_classify"]["label"] == "receipt"  # AI label preserved


def test_classify_back_of_print_creates_pending_pairing(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        # Predecessor front + this back candidate on a scan root.
        front_wp = phase6.WORKING_DIR / "front.jpg"
        back_wp = phase6.WORKING_DIR / "back-cand.jpg"
        write_test_jpeg(front_wp)
        write_test_jpeg(back_wp)
        front = insert_photo(conn, is_scan=True, source_folder="Batch 0002",
                             scan_sequence=5, working_path=str(front_wp))
        insert_master(conn, front)
        back_cand = insert_photo(conn, is_scan=True, source_folder="Batch 0002",
                                 scan_sequence=6, working_path=str(back_wp))
        insert_master(conn, back_cand)
        conn.commit()

    line = _line(str(back_cand), {"label": "back_of_print", "confidence": 0.82,
                                    "reason": "border, no image"})
    with dbmod.connection() as conn:
        conn.autocommit = False
        ClassifyWriter().apply(conn, line, _ctx())
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = True
        row = conn.execute(
            """
            select status, front_photo_id, back_photo_id, back_score, details
            from ingest_pairings where back_photo_id = %s
            """,
            (back_cand,),
        ).fetchone()
        assert row is not None
        assert row[0] == "pending"
        assert row[1] == front
        assert row[2] == back_cand
        assert abs(row[3] - 0.82) < 0.001
        details = row[4] if isinstance(row[4], dict) else json.loads(row[4])
        assert details["source"] == "ai_classify"
        assert details["reason"] == "back_of_print"


def test_classify_back_of_print_ignored_for_non_scan(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = insert_photo(conn, is_scan=False)
        insert_master(conn, pid)
        conn.commit()

    line = _line(str(pid), {"label": "back_of_print", "confidence": 0.9})
    with dbmod.connection() as conn:
        conn.autocommit = False
        ClassifyWriter().apply(conn, line, _ctx())
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = True
        n = conn.execute(
            "select count(*) from ingest_pairings where back_photo_id = %s",
            (pid,),
        ).fetchone()[0]
        assert n == 0


# --- describe -------------------------------------------------------------


def test_describe_writer_inserts_description(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = insert_photo(conn)
        conn.commit()

    line = _line(str(pid), {"text": "children on a porch",
                             "tags": ["children", "porch"]})
    with dbmod.connection() as conn:
        conn.autocommit = False
        DescribeWriter().apply(conn, line, _ctx())
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = True
        payload = conn.execute(
            "select payload from suggestions where photo_id = %s and kind='description'",
            (pid,),
        ).fetchone()[0]
        payload = payload if isinstance(payload, dict) else json.loads(payload)
        assert payload["text"] == "children on a porch"
        assert set(payload["tags"]) == {"children", "porch"}


# --- estimate_date --------------------------------------------------------


@pytest.mark.parametrize("y_min,y_max,expected_precision", [
    (1962, 1963, "year"),
    (1960, 1969, "year"),
    (1960, 1970, "decade"),
    (1950, 1975, "decade"),
])
def test_estimate_date_writer_precision_bucket(phase6, y_min, y_max, expected_precision):
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = insert_photo(conn)
        conn.commit()

    line = _line(str(pid), {"year_min": y_min, "year_max": y_max,
                             "confidence": 0.6, "reasoning": "clothing + border",
                             "is_scan_of_print": True})
    with dbmod.connection() as conn:
        conn.autocommit = False
        EstimateDateWriter().apply(conn, line, _ctx())
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = True
        payload, conf = conn.execute(
            "select payload, confidence from suggestions where photo_id = %s and kind='date'",
            (pid,),
        ).fetchone()
        payload = payload if isinstance(payload, dict) else json.loads(payload)
        assert payload["precision"] == expected_precision
        assert payload["range"]["year_min"] == y_min
        assert payload["range"]["year_max"] == y_max
        assert abs(conf - 0.6 * 0.5) < 1e-6  # service conf × 0.5


# --- detect_faces ---------------------------------------------------------


def test_detect_faces_writer_inserts_bbox_scaled_back(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        # Working file so face-crop generation doesn't complain.
        wp = phase6.WORKING_DIR / "detect-test.jpg"
        write_test_jpeg(wp, 800, 600)
        pid = insert_photo(conn, width=800, height=600, working_path=str(wp))
        conn.commit()

    # The mini downscaled to 400×300 and reported the box there. We should
    # scale back by 2× on each axis.
    line = _line(str(pid), {
        "image_w": 400, "image_h": 300,
        "faces": [{
            "bbox": {"x": 100.0, "y": 50.0, "w": 40.0, "h": 60.0},
            "det_score": 0.98,
            "embedding": [0.1] * 512,
            "landmarks": [[1.0, 2.0]] * 5,
        }],
    })
    with dbmod.connection() as conn:
        conn.autocommit = False
        FacesWriter().apply(conn, line, _ctx())
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = True
        row = conn.execute(
            "select bbox, embedding_model, confidence, source from faces where photo_id = %s",
            (pid,),
        ).fetchone()
        bbox = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        assert abs(bbox["x"] - 200.0) < 1e-3
        assert abs(bbox["y"] - 100.0) < 1e-3
        assert abs(bbox["w"] - 80.0) < 1e-3
        assert abs(bbox["h"] - 120.0) < 1e-3
        assert row[1] == "vlm-1"
        assert row[3] == "ai"


def test_detect_faces_writer_zero_faces_inserts_no_people_suggestion(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        wp = phase6.WORKING_DIR / "empty.jpg"
        write_test_jpeg(wp)
        pid = insert_photo(conn, width=800, height=600, working_path=str(wp))
        conn.commit()

    line = _line(str(pid), {"image_w": 400, "image_h": 300, "faces": []})
    with dbmod.connection() as conn:
        conn.autocommit = False
        FacesWriter().apply(conn, line, _ctx())
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = True
        row = conn.execute(
            """
            select payload from suggestions
            where photo_id = %s and kind = 'classification'
            """,
            (pid,),
        ).fetchone()
        payload = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        assert payload["label"] == "no_people"
        # Never sets has_no_people directly.
        flag = conn.execute("select has_no_people from photos where id = %s",
                            (pid,)).fetchone()[0]
        assert flag is False


def test_detect_faces_writer_idempotent(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        wp = phase6.WORKING_DIR / "idem.jpg"
        write_test_jpeg(wp)
        pid = insert_photo(conn, width=800, height=600, working_path=str(wp))
        conn.commit()

    line = _line(str(pid), {"image_w": 400, "image_h": 300, "faces": [
        {"bbox": {"x": 10, "y": 10, "w": 20, "h": 20}, "det_score": 0.9,
         "embedding": [0.1] * 512}
    ]})
    for _ in range(3):
        with dbmod.connection() as conn:
            conn.autocommit = False
            FacesWriter().apply(conn, line, _ctx())
            conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = True
        n = conn.execute(
            "select count(*) from faces where photo_id = %s", (pid,)
        ).fetchone()[0]
        assert n == 1


# --- transcribe_backs -----------------------------------------------------


class _MockClient:
    """Stand-in inference client for the transcribe_backs inline retry path."""

    def __init__(self, variants: dict[str, dict]) -> None:
        self.variants = variants  # {"flip": {...envelope...}, "rot180": {...}}
        self.calls: list[str] = []

    def call_endpoint(self, endpoint, image, *, timeout=None):
        # The writer identifies the orientation by the ref suffix.
        for key, envelope in self.variants.items():
            if image.ref.endswith(f"_{key[0]}"):
                self.calls.append(key)
                return envelope
        raise AssertionError(f"unexpected ref {image.ref!r}")


def test_transcribe_backs_writer_orphan_no_date_suggestion(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        # Orphan back (no photo_id).
        bp = phase6.WORKING_DIR / "back-orphan.jpg"
        write_test_jpeg(bp)
        back_id = insert_back(conn, photo_id=None, working_path=str(bp))
        conn.commit()

    line = _line(f"b{back_id}", {
        "text": "Mar 1962 — Peggy at the porch",
        "confidence": 0.85,
        "parsed_dates": [{"text": "Mar 1962", "iso": "1962-03-01",
                           "precision": "month"}],
        "names": ["Peggy"],
    })
    ctx = JobContext(client=None, model="vlm-1", prompt_version="v1")
    with dbmod.connection() as conn:
        conn.autocommit = False
        BacksWriter().apply(conn, line, ctx)
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = True
        text, conf = conn.execute(
            "select transcribed_text, transcription_confidence from photo_backs where id = %s",
            (back_id,),
        ).fetchone()
        assert "Peggy" in text
        assert conf == pytest.approx(0.85)
        # Transcription suggestion exists with photo_back_id, orientation_used=identity.
        s = conn.execute(
            """
            select payload from suggestions
            where kind = 'transcription' and (payload ->> 'photo_back_id')::bigint = %s
            """,
            (back_id,),
        ).fetchone()[0]
        s = s if isinstance(s, dict) else json.loads(s)
        assert s["orientation_used"] == "identity"
        # No date suggestion because it's an orphan back (no front photo).
        n = conn.execute("select count(*) from suggestions where kind='date'").fetchone()[0]
        assert n == 0


def test_transcribe_backs_writer_writes_date_suggestion_on_front(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        front = insert_photo(conn)
        insert_master(conn, front)
        bp = phase6.WORKING_DIR / "back-front.jpg"
        write_test_jpeg(bp)
        back_id = insert_back(conn, photo_id=front, working_path=str(bp))
        conn.commit()

    line = _line(f"b{back_id}", {
        "text": "August 1975 — beach trip",
        "confidence": 0.9,
        "parsed_dates": [{"text": "August 1975", "iso": "1975-08-01",
                           "precision": "month"}],
        "names": [],
    })
    ctx = JobContext(client=None, model="vlm-1", prompt_version="v1")
    with dbmod.connection() as conn:
        conn.autocommit = False
        BacksWriter().apply(conn, line, ctx)
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = True
        date = conn.execute(
            """
            select payload, confidence from suggestions
            where kind = 'date' and photo_id = %s
            """,
            (front,),
        ).fetchone()
        assert date is not None
        payload = date[0] if isinstance(date[0], dict) else json.loads(date[0])
        assert payload["date"] == "1975-08-01"
        assert payload["precision"] == "month"
        assert "handwritten on back" in payload["evidence"]
        assert date[1] == pytest.approx(0.8)  # spec: confidence 0.8


def test_transcribe_backs_writer_low_conf_retries_variants_inline(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        bp = phase6.WORKING_DIR / "back-low.jpg"
        write_test_jpeg(bp)
        back_id = insert_back(conn, photo_id=None, working_path=str(bp))
        conn.commit()

    variants = {
        "flip": {"ref": f"b{back_id}_f", "model": "vlm-1",
                  "prompt_version": "v1",
                  "result": {"text": "flipped attempt", "confidence": 0.3,
                              "parsed_dates": [], "names": []}},
        "rot180": {"ref": f"b{back_id}_r", "model": "vlm-1",
                    "prompt_version": "v1",
                    "result": {"text": "rotated attempt — best",
                                "confidence": 0.75, "parsed_dates": [],
                                "names": []}},
    }
    client = _MockClient(variants)
    ctx = JobContext(client=client, model="vlm-1", prompt_version="v1")

    identity_line = _line(f"b{back_id}", {
        "text": "unclear", "confidence": 0.2, "parsed_dates": [], "names": [],
    })
    with dbmod.connection() as conn:
        conn.autocommit = False
        BacksWriter().apply(conn, identity_line, ctx)
        conn.commit()

    # Rotated was best (0.75) — its text should win.
    with dbmod.connection() as conn:
        conn.autocommit = True
        text, conf = conn.execute(
            "select transcribed_text, transcription_confidence from photo_backs where id = %s",
            (back_id,),
        ).fetchone()
        assert text == "rotated attempt — best"
        assert conf == pytest.approx(0.75)
        # Three attempts recorded as suggestions (identity + flip + rot180).
        n = conn.execute(
            """
            select count(*) from suggestions
            where kind='transcription' and (payload ->> 'photo_back_id')::bigint = %s
            """,
            (back_id,),
        ).fetchone()[0]
        assert n == 3
    assert set(client.calls) == {"flip", "rot180"}
