from app.jsonparse import extract_json


def test_plain_object():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_fenced_block():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('```\n{"a": 1}\n```') == {"a": 1}


def test_object_wrapped_in_prose():
    text = 'Sure! Here is the result:\n{"label": "photo", "confidence": 0.9}\nHope that helps.'
    assert extract_json(text) == {"label": "photo", "confidence": 0.9}


def test_braces_inside_strings_do_not_confuse_the_scanner():
    assert extract_json('prefix {"text": "a } b", "n": 1} suffix') == {"text": "a } b", "n": 1}


def test_escaped_quote_inside_string():
    assert extract_json(r'{"text": "she said \"hi\""}') == {"text": 'she said "hi"'}


def test_nested_object():
    assert extract_json('{"a": {"b": [1, 2]}}') == {"a": {"b": [1, 2]}}


def test_unparseable_returns_none():
    assert extract_json("I think this is a photo of a dog.") is None
    assert extract_json("") is None
    assert extract_json("{not json at all") is None


def test_a_bare_array_is_not_a_result():
    assert extract_json("[1, 2, 3]") is None
