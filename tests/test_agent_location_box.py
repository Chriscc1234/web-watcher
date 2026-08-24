"""The location-box guard: a city/place picker must be shown to the agent as a
>>> LOCATION INPUT <<< so it types only the place, not topic keywords.

This is the weather-site regression Chris hit — the app typed "severe weather Seattle"
into a *city* search box that should only ever receive "Seattle". These are pure offline
tests of the classifier + the element rendering (no browser needed)."""

from __future__ import annotations

from web_watcher.agent import (
    _elements_text, _is_location_input, _is_topic_plus_place, _place_tail, _SYSTEM,
)
from web_watcher.monitor import label_is_location_box


def _inp(label: str, tag: str = "input", type_: str = "text", value: str = "") -> dict:
    return {"index": 0, "tag": tag, "type": type_, "label": label,
            "href": "", "value": value, "focused": False, "inViewport": True}


def test_weather_gov_main_box_detected_via_value():
    # weather.gov's real main box: no aria-label/placeholder, generic name, hint in the value.
    e = _inp("inputstring", value="Enter location ...")
    assert _is_location_input(e) is True
    assert ">>> LOCATION INPUT <<<" in _elements_text([e])


# ── the classifier ────────────────────────────────────────────────────────────

def test_location_labels_are_detected():
    for label in ("City", "City, State or ZIP", "Enter ZIP code", "Search location",
                  "Your area", "Set location", "Town or postcode", "Neighborhood",
                  "Where are you?"):
        assert _is_location_input(_inp(label)) is True, label


def test_keyword_boxes_are_not_location():
    for label in ("Search", "Search for anything", "Search Marketplace",
                  "What are you looking for?", "Search listings", "Find trucks"):
        assert _is_location_input(_inp(label)) is False, label


def test_marketplace_does_not_false_match_place():
    # \bplace\b must NOT fire inside "marketplace" — the exact false positive to avoid.
    assert _is_location_input(_inp("Search Marketplace")) is False


def test_non_text_inputs_are_never_location():
    assert _is_location_input(_inp("City", type_="checkbox")) is False
    assert _is_location_input({"tag": "button", "type": "", "label": "City"}) is False


# ── the rendering the agent actually sees ──────────────────────────────────────

def test_city_box_renders_as_location_input():
    txt = _elements_text([_inp("City or ZIP")])
    assert ">>> LOCATION INPUT <<<" in txt
    assert "type ONLY the place" in txt
    assert ">>> TEXT INPUT <<<" not in txt


def test_keyword_box_still_renders_as_text_input():
    txt = _elements_text([_inp("Search for anything")])
    assert ">>> TEXT INPUT <<<" in txt
    assert "LOCATION INPUT" not in txt


def test_system_prompt_teaches_the_location_rule():
    # The preventive half: the agent must be told what a LOCATION INPUT means.
    assert "LOCATION INPUT" in _SYSTEM
    assert "Seattle" in _SYSTEM      # the worked example


# ── the shared helper both paths use (agent rendering + humanized_search) ───────

def test_topic_plus_place_catches_the_real_failure():
    # The exact live failure from a screenshot: typed into weather.gov's city box, which
    # answered "No results found". Tagging the box wasn't enough — this is what BLOCKS it.
    assert _is_topic_plus_place("weather warning Saipan") is True
    assert _place_tail("weather warning Saipan") == "Saipan"
    assert _is_topic_plus_place("severe weather Seattle") is True


def test_bare_places_are_never_blocked():
    # Must not fire on a correct retry, or the agent would loop forever. Note looks_like_location
    # says False for "Saipan" and a bare ZIP, so the word-count floor is what protects them.
    for ok in ("Saipan", "Seattle", "Anacortes, WA", "98221"):
        assert _is_topic_plus_place(ok) is False, ok


def test_place_tail_keeps_city_state_pairs():
    assert _place_tail("show me trucks in Anacortes, WA") == "Anacortes, WA"


def test_shared_label_helper_agrees():
    # Same helper backs the agent's _is_location_input and humanized_search's guard.
    assert label_is_location_box("City, State or ZIP") is True
    assert label_is_location_box("Search location") is True
    assert label_is_location_box("Search Marketplace") is False
    assert label_is_location_box("Search for anything") is False
    assert _is_location_input(_inp("Enter ZIP code")) == label_is_location_box("Enter ZIP code")
