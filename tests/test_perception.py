"""
Perception layer tests.

Offline tests only — no browser or Ollama needed.
Live tests (pytest -m live) visit a real page via Playwright.
"""

from __future__ import annotations

import pytest

from web_watcher.browser import PageResult
from web_watcher.config import Watch
from web_watcher.perception import (
    MIN_TEXT_CHARS,
    PerceptionResult,
    check_text_usable,
    perceive,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GOOD_TEXT = (
    "The National Weather Service has issued a winter storm warning for the Seattle area. "
    "Expect 8 to 12 inches of snow overnight, with wind gusts up to 45 mph. "
    "Travel will be very difficult. A warning is in effect until 6 AM Tuesday. "
    "Residents should avoid unnecessary travel and prepare emergency supplies."
)

def _watch(perception: str = "auto") -> Watch:
    return Watch(
        name="test",
        urls=["https://example.com"],
        interval_minutes=5,
        instruction="Alert on snow.",
        perception=perception,
    )

def _result(text: str = GOOD_TEXT, screenshot: bytes | None = None) -> PageResult:
    return PageResult(url="https://example.com", text=text, screenshot_bytes=screenshot)


# ---------------------------------------------------------------------------
# check_text_usable
# ---------------------------------------------------------------------------

def test_good_text_passes():
    ok, notes = check_text_usable(GOOD_TEXT)
    assert ok is True
    assert notes == []


def test_empty_text_fails():
    ok, notes = check_text_usable("")
    assert ok is False
    assert notes


def test_whitespace_only_fails():
    ok, notes = check_text_usable("   \n\t  ")
    assert ok is False


def test_too_short_fails():
    ok, notes = check_text_usable("Hi there.")
    assert ok is False
    assert "too short" in notes[0]


def test_js_required_fails():
    for snippet in [
        "Please enable JavaScript to view this page.",
        "This page requires JavaScript to function.",
        "You need to enable JavaScript to run this app.",
    ]:
        ok, notes = check_text_usable(snippet * 10)  # pad to min length
        assert ok is False, f"Expected failure for: {snippet!r}"
        assert any("JS-required" in n for n in notes)


def test_loading_placeholder_repeated_fails():
    # Must be long enough to pass the length gate before the loading check runs
    text = "loading... " * 20
    ok, notes = check_text_usable(text)
    assert ok is False
    assert any("loading" in n for n in notes)


def test_loading_placeholder_occasional_passes():
    text = GOOD_TEXT + " loading... " + GOOD_TEXT
    ok, _ = check_text_usable(text)
    assert ok is True  # only 1 occurrence, under threshold


def test_exactly_min_length_passes():
    text = "x" * MIN_TEXT_CHARS
    ok, _ = check_text_usable(text)
    assert ok is True


def test_one_under_min_length_fails():
    text = "x" * (MIN_TEXT_CHARS - 1)
    ok, _ = check_text_usable(text)
    assert ok is False


# ---------------------------------------------------------------------------
# perceive — mode=text
# ---------------------------------------------------------------------------

def test_mode_text_always_uses_text():
    result = perceive(_result(), _watch("text"))
    assert result.mode_used == "text"
    assert result.text == GOOD_TEXT
    assert result.image_bytes is None
    assert result.heuristic_passed is True


def test_mode_text_ignores_bad_text():
    bad = "Please enable JavaScript to view this page." * 10
    result = perceive(_result(text=bad), _watch("text"))
    assert result.mode_used == "text"   # forced, no heuristic run
    assert result.heuristic_passed is True


# ---------------------------------------------------------------------------
# perceive — mode=vision
# ---------------------------------------------------------------------------

def test_mode_vision_uses_screenshot():
    png = b"\x89PNG fake"
    result = perceive(_result(screenshot=png), _watch("vision"))
    assert result.mode_used == "vision"
    assert result.image_bytes == png
    assert result.text is None
    assert result.heuristic_passed is True


def test_mode_vision_with_no_screenshot_still_returns_vision_result():
    result = perceive(_result(), _watch("vision"))
    assert result.mode_used == "vision"
    assert result.image_bytes is None  # None but mode_used is still vision


# ---------------------------------------------------------------------------
# perceive — mode=auto, text passes
# ---------------------------------------------------------------------------

def test_auto_good_text_uses_text():
    result = perceive(_result(GOOD_TEXT), _watch("auto"))
    assert result.mode_used == "text"
    assert result.heuristic_passed is True
    assert result.text == GOOD_TEXT


# ---------------------------------------------------------------------------
# perceive — mode=auto, text fails, screenshot available
# ---------------------------------------------------------------------------

def test_auto_bad_text_falls_back_to_vision():
    bad = "Please enable JavaScript to run this app." * 10
    png = b"\x89PNG fake"
    result = perceive(_result(text=bad, screenshot=png), _watch("auto"))
    assert result.mode_used == "vision"
    assert result.image_bytes == png
    assert result.heuristic_passed is False
    assert result.heuristic_notes  # explains why text failed


def test_auto_bad_text_no_screenshot_degrades_to_text():
    bad = "Please enable JavaScript to run this app." * 10
    result = perceive(_result(text=bad, screenshot=None), _watch("auto"))
    assert result.mode_used == "text"   # graceful degradation
    assert result.heuristic_passed is False
    assert any("no screenshot" in n for n in result.heuristic_notes)


def test_auto_short_text_falls_back_to_vision():
    short = "Hi."
    png = b"\x89PNG fake"
    result = perceive(_result(text=short, screenshot=png), _watch("auto"))
    assert result.mode_used == "vision"
    assert result.heuristic_passed is False


# ---------------------------------------------------------------------------
# Live test — real page via Playwright
# ---------------------------------------------------------------------------

@pytest.mark.live
def test_live_nws_page_perception():
    """NWS page should produce usable text, no vision fallback needed."""
    from web_watcher.browser import BrowserSession

    url = (
        "https://forecast.weather.gov/MapClick.php"
        "?CityName=Seattle&state=WA&site=SEW"
        "&textField1=47.6062&textField2=-122.3321"
    )
    watch = Watch(
        name="nws",
        urls=[url],
        interval_minutes=30,
        instruction="frost warning",
        perception="auto",
    )

    with BrowserSession(headless=True) as session:
        page_results = session.run_watch(watch, screenshot=True)

    assert page_results, "Expected at least one page result"
    pr = page_results[0]
    assert pr.error is None, f"Browser error: {pr.error}"

    result = perceive(pr, watch)
    print(f"\nmode_used={result.mode_used}, passed={result.heuristic_passed}, notes={result.heuristic_notes}")
    print(f"text length: {len(pr.text)} chars")

    # NWS is a static government site — text path should succeed
    assert result.mode_used == "text", (
        f"Expected text mode for NWS, got {result.mode_used}. Notes: {result.heuristic_notes}"
    )
    assert result.heuristic_passed is True
