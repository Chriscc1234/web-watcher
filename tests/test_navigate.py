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


# ── SearchRequest: the structured intent pulled back out of a watch's URL ──────

def test_build_from_refined_craigslist_url():
    r = N.build_search_request(
        "https://skagit.craigslist.org/search/cta"
        "?postal=98221&search_distance=50&max_price=10000&query=toyota%20tacoma&sort=date")
    assert r.site == "craigslist"
    assert r.terms == "toyota tacoma"
    assert r.zip == "98221"
    assert r.radius == 50
    assert r.price_max == 10000
    assert r.sort == "date"
    assert r.category == "cta"


def test_build_from_offerup_url():
    r = N.build_search_request("https://offerup.com/search?q=truck&price_max=15000&radius=50")
    assert r.site == "offerup"
    assert r.terms == "truck"
    assert r.price_max == 15000
    assert r.radius == 50


def test_build_from_ebay_motors_url():
    r = N.build_search_request(
        "https://www.ebay.com/sch/i.html?_nkw=tacoma&_stpos=98221&_sadis=50&_udhi=10000&_sacat=6001")
    assert r.site == "ebay"
    assert r.terms == "tacoma"
    assert r.zip == "98221"
    assert r.radius == 50
    assert r.price_max == 10000


def test_build_pulls_inline_params_out_of_query_text():
    # A model that stuffed price + zip into the keyword box — the terms we'd TYPE must be clean.
    r = N.build_search_request("https://skagit.craigslist.org/search/sss?query=tacoma%20under%205k%2098221")
    assert r.terms == "tacoma"
    assert r.price_max == 5000
    assert r.zip == "98221"


def test_build_falls_back_to_instruction_for_missing_location_and_price():
    # URL carries no location/price; the watch's instruction does.
    r = N.build_search_request(
        "https://skagit.craigslist.org/search/cta?query=tacoma",
        instruction="find a toyota tacoma in anacortes under 8k")
    assert r.terms == "tacoma"
    assert r.price_max == 8000
    assert r.zip is not None            # resolved from "in anacortes"


def test_build_generic_vehicle_category_has_empty_terms():
    # A generic cars+trucks watch is 'browse this category', not a keyword search.
    r = N.build_search_request("https://skagit.craigslist.org/search/cta?postal=98221&search_distance=50")
    assert r.category == "cta"
    assert r.terms == ""
    assert r.zip == "98221"
    assert "cat=cta" in r.describe()


def test_build_tolerates_garbage_url():
    r = N.build_search_request("not a url", instruction="")
    assert isinstance(r, N.SearchRequest)   # best-effort, never raises
