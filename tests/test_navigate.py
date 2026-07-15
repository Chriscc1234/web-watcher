"""Offline tests for the human-first navigation layer. The browser-driving primitives
(type_search / set_location) are proven LIVE (Craigslist / OfferUp); here we test the pure
hint lookup and that the module's control map is well-formed."""

from __future__ import annotations

from web_watcher import navigate as N


def test_hints_for_matches_site():
    assert N.hints_for("https://seattle.craigslist.org")["search_box"]
    assert N.hints_for("https://www.craigslist.org/area/seattle")["search_box"]
    assert "location" in N.hints_for("https://offerup.com/search?q=truck")
    assert N.hints_for("https://example.com/anything") == {}
    assert N.hints_for("") == {}


def test_craigslist_hint_covers_homepage_placeholder_box():
    # The homepage box has only a placeholder — the hint must cover it (the bug that made the
    # first live proof fail was a hint that only matched the results-page box).
    assert "placeholder" in N.hints_for("https://craigslist.org")["search_box"].lower()


def test_offerup_location_hint_is_complete():
    loc = N.hints_for("https://offerup.com/search")["location"]
    assert loc["open"] and loc["input"] and loc["confirm"]   # the mapped dialog flow


def test_control_hints_are_well_formed():
    for host, h in N.CONTROL_HINTS.items():
        assert isinstance(host, str) and isinstance(h, dict)
        if "location" in h:
            assert isinstance(h["location"], dict) and h["location"].get("open")
