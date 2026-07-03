"""
Reasoning layer tests.

Offline tests mock the Ollama HTTP call so no model is needed.
Live tests hit real Ollama with mistral:latest (available on this machine).

  pytest tests/test_reasoning.py            # offline only
  pytest tests/test_reasoning.py -m live   # include live Ollama calls
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from web_watcher.reasoning import (
    OllamaUnavailableError,
    Reasoner,
    ReasoningResult,
    _parse_and_validate,
    _truncate,
    _validate_schema,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEXT_MODEL   = "mistral:latest"
VISION_MODEL = "moondream"

def _reasoner() -> Reasoner:
    return Reasoner(text_model=TEXT_MODEL, vision_model=VISION_MODEL)


def _mock_chat(reasoner: Reasoner, responses: list[str]):
    """Patch Reasoner._chat to return successive strings from responses list."""
    calls = iter(responses)
    reasoner._chat = lambda *args, **kwargs: next(calls)


# ---------------------------------------------------------------------------
# _parse_and_validate
# ---------------------------------------------------------------------------

def test_parse_clean_json():
    raw = '{"found": true, "summary": "Ice storm warning", "confidence": "high", "link": null}'
    r = _parse_and_validate(raw)
    assert r is not None
    assert r.found is True
    assert r.confidence == "high"
    assert r.link is None


def test_parse_strips_markdown_fences():
    raw = '```json\n{"found": false, "summary": "Nothing", "confidence": "low", "link": null}\n```'
    r = _parse_and_validate(raw)
    assert r is not None
    assert r.found is False


def test_parse_json_buried_in_text():
    raw = 'Sure! Here is the answer: {"found": true, "summary": "Match", "confidence": "medium", "link": "https://x.com"} Hope that helps!'
    r = _parse_and_validate(raw)
    assert r is not None
    assert r.found is True
    assert r.link == "https://x.com"


def test_parse_missing_found_key_returns_none():
    raw = '{"summary": "something", "confidence": "high", "link": null}'
    assert _parse_and_validate(raw) is None


def test_parse_invalid_json_returns_none():
    assert _parse_and_validate("this is not json at all") is None


def test_parse_non_dict_returns_none():
    assert _parse_and_validate("[1, 2, 3]") is None


def test_parse_confidence_coerced_to_low_if_unknown():
    raw = '{"found": false, "summary": "x", "confidence": "ultra", "link": null}'
    r = _parse_and_validate(raw)
    assert r is not None
    assert r.confidence == "low"


def test_parse_empty_link_normalised_to_none():
    raw = '{"found": true, "summary": "x", "confidence": "high", "link": ""}'
    r = _parse_and_validate(raw)
    assert r is not None
    assert r.link is None


# ---------------------------------------------------------------------------
# _validate_schema
# ---------------------------------------------------------------------------

def test_validate_valid_dict():
    r = _validate_schema({"found": True, "summary": "ok", "confidence": "high", "link": None})
    assert r is not None
    assert r.found is True


def test_validate_non_dict_returns_none():
    assert _validate_schema("hello") is None
    assert _validate_schema(42) is None
    assert _validate_schema([]) is None


# ---------------------------------------------------------------------------
# _truncate
# ---------------------------------------------------------------------------

def test_truncate_short_text_unchanged():
    t = "hello world"
    assert _truncate(t, 100) == t


def test_truncate_long_text_preserves_start_and_end():
    long = "A" * 5000 + "B" * 5000
    result = _truncate(long, 100)
    assert result.startswith("A" * 50)
    assert result.endswith("A" * 50) or result.endswith("B" * 50)
    assert "truncated" in result


# ---------------------------------------------------------------------------
# Reasoner — repair/retry loop (mocked)
# ---------------------------------------------------------------------------

def test_first_attempt_succeeds():
    r = _reasoner()
    good = '{"found": false, "summary": "No match", "confidence": "low", "link": null}'
    _mock_chat(r, [good])
    result = r.analyse_text("some page text", "alert me if snow warning")
    assert result.error is None
    assert result.found is False


def test_repair_attempt_succeeds_on_second_try():
    r = _reasoner()
    bad  = "Sure, the answer is: found is true."
    good = '{"found": true, "summary": "Snow warning active", "confidence": "high", "link": "https://nws.gov"}'
    _mock_chat(r, [bad, good])
    result = r.analyse_text("page with snow warning", "alert on snow")
    assert result.error is None
    assert result.found is True
    assert result.confidence == "high"


def test_both_attempts_fail_returns_error_sentinel():
    r = _reasoner()
    _mock_chat(r, ["not json", "still not json"])
    result = r.analyse_text("page", "instruction")
    assert result.error == "json_parse_failed_after_repair"
    assert result.found is False
    assert result.raw_output == "still not json"


def test_repair_after_schema_mismatch():
    r = _reasoner()
    # Valid JSON but missing "found" key
    bad  = '{"result": "yes", "description": "something"}'
    good = '{"found": true, "summary": "Match found", "confidence": "medium", "link": null}'
    _mock_chat(r, [bad, good])
    result = r.analyse_text("page", "instruction")
    assert result.found is True
    assert result.error is None


def test_ollama_connection_error_propagates():
    r = _reasoner()
    import httpx
    with patch.object(r, "_chat", side_effect=OllamaUnavailableError("not running")):
        with pytest.raises(OllamaUnavailableError):
            r.analyse_text("page", "instruction")


# ---------------------------------------------------------------------------
# Live tests — real Ollama + mistral:latest
# ---------------------------------------------------------------------------

@pytest.mark.live
def test_live_analyse_text_returns_valid_result():
    r = _reasoner()
    text = (
        "The National Weather Service has issued a WINTER STORM WARNING for this area. "
        "Expected: 8-12 inches of snow, wind gusts up to 45 mph. "
        "Travel will be very difficult. The warning is in effect until 6 AM Tuesday."
    )
    result = r.analyse_text(text, "Alert me if there is a winter storm or snow warning.")
    print(f"\nResult: found={result.found}, confidence={result.confidence}, summary={result.summary!r}")
    assert result.error is None, f"Expected no error, got: {result.error}\nRaw: {result.raw_output}"
    assert result.found is True, f"Expected found=True for obvious winter storm text, got False. Summary: {result.summary}"
    assert result.confidence in ("high", "medium", "low")


@pytest.mark.live
def test_live_analyse_text_not_found():
    r = _reasoner()
    text = "Partly cloudy skies today. High near 62. Light and variable winds."
    result = r.analyse_text(text, "Alert me if there is a winter storm or snow warning.")
    print(f"\nResult: found={result.found}, confidence={result.confidence}, summary={result.summary!r}")
    assert result.error is None
    assert result.found is False, f"Expected found=False for benign weather, got True. Summary: {result.summary}"


@pytest.mark.live
def test_live_json_contract_all_fields_present():
    """Validates the full output contract regardless of found value."""
    r = _reasoner()
    result = r.analyse_text("Some page content.", "Look for any price under $50.")
    assert result.error is None
    assert isinstance(result.found, bool)
    assert isinstance(result.summary, str) and len(result.summary) > 0
    assert result.confidence in ("high", "medium", "low")
    # link is either None or a non-empty string
    assert result.link is None or (isinstance(result.link, str) and len(result.link) > 0)
