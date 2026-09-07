"""Tidy raw VLM JSON into the shapes the desktop's suggestion writer expects.

Nothing here invents a value; it clamps, coerces and drops. If the model returned
something unusable the caller sees it in the error shape instead.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

CLASSIFY_LABELS = (
    "photo",
    "document",
    "screenshot",
    "receipt",
    "blank",
    "back_of_print",
    "other",
)

# Matches shared/migrations date_precision so Phase 6 can copy it straight into a
# `date` suggestion payload.
DATE_PRECISIONS = ("exact", "month", "year", "decade", "unknown")

_EARLIEST_PHOTO_YEAR = 1826
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _clamp01(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return round(min(1.0, max(0.0, number)), 4)


def _as_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = _as_text(item)
        if text:
            out.append(text)
    return out


def classify(raw: dict[str, Any]) -> dict[str, Any]:
    label = _as_text(raw.get("label")).lower().replace(" ", "_").replace("-", "_")
    result: dict[str, Any] = {
        "label": label if label in CLASSIFY_LABELS else "other",
        "confidence": _clamp01(raw.get("confidence")),
        "reason": _as_text(raw.get("reason")),
    }
    if label and label not in CLASSIFY_LABELS:
        result["raw_label"] = label
    return result


def _parsed_date(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    text = _as_text(raw.get("text"))
    if not text:
        return None
    iso = _as_text(raw.get("iso")) or None
    if iso is not None:
        if not _ISO.match(iso):
            iso = None
        else:
            try:
                dt.date.fromisoformat(iso)
            except ValueError:
                iso = None
    precision = _as_text(raw.get("precision")).lower()
    if precision not in DATE_PRECISIONS:
        precision = "unknown"
    return {"text": text, "iso": iso, "precision": precision}


def transcribe_back(raw: dict[str, Any]) -> dict[str, Any]:
    dates = []
    for item in raw.get("parsed_dates") or []:
        parsed = _parsed_date(item)
        if parsed is not None:
            dates.append(parsed)
    return {
        "text": raw.get("text") if isinstance(raw.get("text"), str) else "",
        "parsed_dates": dates,
        "names": _as_str_list(raw.get("names")),
        "confidence": _clamp01(raw.get("confidence")),
    }


def describe(raw: dict[str, Any], max_words: int = 30) -> dict[str, Any]:
    text = _as_text(raw.get("text"))
    words = text.split()
    truncated = len(words) > max_words
    if truncated:
        text = " ".join(words[:max_words])

    seen: set[str] = set()
    tags: list[str] = []
    for tag in _as_str_list(raw.get("tags")):
        lowered = tag.lower()
        if lowered not in seen:
            seen.add(lowered)
            tags.append(lowered)

    result: dict[str, Any] = {"text": text, "tags": tags[:10]}
    if truncated:
        result["truncated"] = True
    return result


def estimate_date(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Enforce the range rules the prompt asks for. None if there is no usable range."""
    try:
        year_min = int(raw["year_min"])
        year_max = int(raw["year_max"])
    except (KeyError, TypeError, ValueError):
        return None

    this_year = dt.date.today().year
    year_min = min(max(year_min, _EARLIEST_PHOTO_YEAR), this_year + 1)
    year_max = min(max(year_max, _EARLIEST_PHOTO_YEAR), this_year + 1)
    if year_min > year_max:
        year_min, year_max = year_max, year_min

    confidence = _clamp01(raw.get("confidence"))
    # A range of >= 3 years always; >= 10 when the model is not confident. The
    # prompt asks for this; the code guarantees it.
    required_span = 10 if confidence < 0.5 else 3
    widened = False
    span = year_max - year_min + 1
    if span < required_span:
        widened = True
        short_by = required_span - span
        year_min -= short_by // 2 + short_by % 2
        year_max += short_by // 2
        year_min = max(year_min, _EARLIEST_PHOTO_YEAR)
        year_max = min(year_max, this_year + 1)

    result: dict[str, Any] = {
        "year_min": year_min,
        "year_max": year_max,
        "confidence": confidence,
        "reasoning": _as_text(raw.get("reasoning")),
        "is_scan_of_print": bool(raw.get("is_scan_of_print")),
    }
    if widened:
        result["widened"] = True
    return result
