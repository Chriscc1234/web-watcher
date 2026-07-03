"""
Tests for the pure (no-browser) parts of the site learner: profile assembly from
harvested signals, the confirm check, and search/sort resolution. learn_site itself is
the thin live wrapper and is exercised in live verification, not here.
"""

from __future__ import annotations

from web_watcher.sitelearn import (
    synthesize_profile, profile_confirms, _resolve_search, _resolve_sort,
)


def test_synthesize_numeric_site():
    p = synthesize_profile(
        "https://www.cargurus.com/Cars/inventorylisting/x",
        listing_urls=[
            "https://www.cargurus.com/Cars/link/123456789",
            "https://www.cargurus.com/Cars/link/987654321",
            "https://www.cargurus.com/Cars/link/111222333",
        ],
        search_info={"param": "q", "action": "https://www.cargurus.com/search"},
        sort_info={"param": "sortType", "values": ["PRICE", "MILEAGE"]},
        learned_at="2026-06-29T00:00:00Z",
    )
    assert p["domain"] == "cargurus.com"
    assert p["search_param"] == "q"
    assert p["sort_param"] == "sortType"
    assert p["sort_values"] == ["PRICE", "MILEAGE"]
    assert profile_confirms(p, "https://www.cargurus.com/Cars/link/123456789")


def test_synthesize_ignores_off_site_urls():
    # Only same-registrable-site URLs feed the inference.
    p = synthesize_profile(
        "https://offerup.com/search?q=truck",
        listing_urls=[
            "https://offerup.com/item/detail/a1b2c3d4e5",
            "https://offerup.com/item/detail/f6g7h8i9j0",
            "https://offerup.com/item/detail/k1l2m3n4o5",
            "https://facebook.com/marketplace/item/999999999999",  # ignored
        ],
    )
    assert p["domain"] == "offerup.com"
    assert "listing_url_regex" in p
    assert p["key_prefix"]  # non-empty


def test_synthesize_no_pattern_when_no_listings():
    p = synthesize_profile("https://example.com/help", listing_urls=[])
    assert p["domain"] == "example.com"
    assert "listing_url_regex" not in p
    assert profile_confirms(p, "https://example.com/help") is False


def test_resolve_search_prefers_landing_url_param():
    # The results URL already uses ?query= (Craigslist) — that wins over the input name.
    out = _resolve_search({"name": "search-box", "action": "x"},
                          "https://seattle.craigslist.org/search/cta?query=truck")
    assert out == {"param": "query", "action": "https://seattle.craigslist.org/search/cta?query=truck"}


def test_resolve_search_falls_back_to_input_name():
    out = _resolve_search({"name": "keywords", "action": "https://site/s"},
                          "https://site/home")
    assert out["param"] == "keywords"


def test_resolve_sort_requires_param_and_values():
    assert _resolve_sort({"param": "sort", "values": ["new"]}, "u") == {"param": "sort", "values": ["new"]}
    assert _resolve_sort({"param": "sort", "values": []}, "u") is None
    assert _resolve_sort(None, "u") is None
