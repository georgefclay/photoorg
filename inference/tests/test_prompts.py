from app.prompts import PROMPT_DIR, load_prompt

ENDPOINT_PROMPTS = ["classify", "transcribe-back", "describe", "estimate-date"]


def test_every_endpoint_has_a_prompt():
    for name in ENDPOINT_PROMPTS:
        text, version = load_prompt(name)
        assert text
        assert version == f"{name}.v1"


def test_prompts_demand_bare_json():
    for name in ENDPOINT_PROMPTS:
        text, _ = load_prompt(name)
        assert "single JSON object" in text
        assert "no code fences" in text.lower() or "no markdown" in text.lower()


def test_transcribe_prompt_forbids_guessing():
    text, _ = load_prompt("transcribe-back")
    assert "[illegible]" in text
    assert "Never guess" in text


def test_describe_prompt_forbids_identities_and_speculation():
    text, _ = load_prompt("describe")
    assert "Do NOT name anyone" in text
    assert "relationships" in text
    assert "emotions" in text


def test_highest_version_wins(tmp_path, monkeypatch):
    import app.prompts as prompts

    monkeypatch.setattr(prompts, "PROMPT_DIR", tmp_path)
    prompts.load_prompt.cache_clear()
    (tmp_path / "thing.v1.txt").write_text("one")
    (tmp_path / "thing.v2.txt").write_text("two")
    (tmp_path / "thing.v10.txt").write_text("ten")
    try:
        assert prompts.load_prompt("thing") == ("ten", "thing.v10")
    finally:
        prompts.load_prompt.cache_clear()


def test_missing_prompt_raises(tmp_path, monkeypatch):
    import app.prompts as prompts

    monkeypatch.setattr(prompts, "PROMPT_DIR", tmp_path)
    prompts.load_prompt.cache_clear()
    try:
        import pytest

        with pytest.raises(FileNotFoundError):
            prompts.load_prompt("nothing")
    finally:
        prompts.load_prompt.cache_clear()


def test_prompt_files_are_committed():
    assert sorted(p.name for p in PROMPT_DIR.glob("*.txt")) == [
        "classify.v1.txt",
        "describe.v1.txt",
        "estimate-date.v1.txt",
        "transcribe-back.v1.txt",
    ]
