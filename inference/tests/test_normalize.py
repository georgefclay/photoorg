"""Clamping and coercion rules, straight from the prompt's promises."""

import datetime as dt

from app import normalize


def test_classify_normalises_case_and_separators():
    assert normalize.classify({"label": "Back-Of-Print"})["label"] == "back_of_print"
    assert normalize.classify({"label": " SCREENSHOT "})["label"] == "screenshot"


def test_classify_missing_fields():
    result = normalize.classify({})
    assert result == {"label": "other", "confidence": 0.0, "reason": ""}


def test_confidence_is_clamped_and_survives_junk():
    assert normalize.classify({"label": "photo", "confidence": 5})["confidence"] == 1.0
    assert normalize.classify({"label": "photo", "confidence": -2})["confidence"] == 0.0
    assert normalize.classify({"label": "photo", "confidence": "high"})["confidence"] == 0.0


def test_transcription_keeps_line_breaks_verbatim():
    text = "Peggy & Bill\nEaster 1962\n[illegible]"
    assert normalize.transcribe_back({"text": text})["text"] == text


def test_transcription_handles_missing_lists():
    result = normalize.transcribe_back({"text": "x"})
    assert result["parsed_dates"] == []
    assert result["names"] == []


def test_parsed_date_precision_vocabulary_matches_the_database():
    assert normalize.DATE_PRECISIONS == ("exact", "month", "year", "decade", "unknown")


def test_parsed_date_rejects_impossible_iso():
    dates = normalize.transcribe_back(
        {"parsed_dates": [{"text": "Feb 30 62", "iso": "1962-02-30", "precision": "exact"}]}
    )["parsed_dates"]
    assert dates == [{"text": "Feb 30 62", "iso": None, "precision": "exact"}]


def test_describe_deduplicates_and_caps_tags():
    result = normalize.describe({"text": "a photo", "tags": [f"tag{i}" for i in range(20)]})
    assert len(result["tags"]) == 10


def test_describe_short_text_is_untouched():
    result = normalize.describe({"text": "Two children on a porch with a dog."})
    assert result == {"text": "Two children on a porch with a dog.", "tags": []}


def test_estimate_date_swaps_reversed_range():
    result = normalize.estimate_date(
        {"year_min": 1975, "year_max": 1960, "confidence": 0.9, "reasoning": "r"}
    )
    assert result["year_min"] == 1960
    assert result["year_max"] == 1975


def test_estimate_date_clamps_absurd_years():
    result = normalize.estimate_date(
        {"year_min": 1200, "year_max": 3000, "confidence": 0.9, "reasoning": "r"}
    )
    assert result["year_min"] == 1826
    assert result["year_max"] == dt.date.today().year + 1


def test_estimate_date_without_a_range_is_none():
    assert normalize.estimate_date({"confidence": 0.9}) is None
    assert normalize.estimate_date({"year_min": "sixties", "year_max": 1969}) is None


def test_estimate_date_confident_narrow_range_widens_to_three():
    result = normalize.estimate_date(
        {"year_min": 1962, "year_max": 1962, "confidence": 0.95, "reasoning": "r"}
    )
    assert (result["year_min"], result["year_max"]) == (1961, 1963)
    assert result["widened"] is True


def test_estimate_date_wide_enough_range_is_left_alone():
    result = normalize.estimate_date(
        {"year_min": 1960, "year_max": 1969, "confidence": 0.4, "reasoning": "r"}
    )
    assert (result["year_min"], result["year_max"]) == (1960, 1969)
    assert "widened" not in result
