"""Closing the loop — the pure heuristics behind reading a page's response to a search:
is a box a location picker, and does the page say 'no results'. (Page-driven parts are
validated live, not here.)"""

from __future__ import annotations

from web_watcher.monitor import (
    looks_like_location,
    suggestions_are_locations,
    text_says_no_results,
)


def test_looks_like_location_city_state():
    assert looks_like_location("Seattle, WA")
    assert looks_like_location("Spokane, WA")
    assert looks_like_location("New York, NY")


def test_looks_like_location_bare_town_via_gazetteer():
    assert looks_like_location("Anacortes")


def test_looks_like_location_rejects_products():
    assert not looks_like_location("Toyota Tacoma SR5")
    assert not looks_like_location("Chevy Silverado 2500")
    assert not looks_like_location("diesel truck under 15000")
    assert not looks_like_location("")


def test_suggestions_are_locations_geo_box():
    # The weather-site case: every suggestion is a place.
    assert suggestions_are_locations(["Seattle, WA", "Spokane, WA", "Tacoma, WA", "Yakima, WA"])


def test_suggestions_are_locations_marketplace_box():
    # A real keyword search suggests products, not places.
    assert not suggestions_are_locations(
        ["Toyota Tacoma SR5", "Toyota Tacoma TRD", "Toyota Tacoma 4x4"])


def test_suggestions_are_locations_needs_two():
    assert not suggestions_are_locations(["Seattle, WA"])
    assert not suggestions_are_locations([])


def test_text_says_no_results():
    assert text_says_no_results("Sorry, no results found for your search.")
    assert text_says_no_results("Your search did not match any listings.")
    assert text_says_no_results("0 results")
    assert not text_says_no_results("Showing 24 results near you")
    assert not text_says_no_results("")
