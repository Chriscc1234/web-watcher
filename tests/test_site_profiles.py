"""
Tests for the learned-site layer: site_key normalization, the site_profiles store, the
deterministic listing-URL pattern inference, and profile-aware listing keys.
All offline, temp-db.
"""

from __future__ import annotations

import pytest

from web_watcher.storage import (
    init_db, site_key, upsert_site_profile, get_site_profile,
    list_site_profiles, delete_site_profile,
)
from web_watcher.monitor import infer_listing_pattern, _listing_key
import re


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    init_db(p)
    return p


# ---------------------------------------------------------------------------
# site_key normalization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("inp,expected", [
    ("https://seattle.craigslist.org/search/cta?query=truck", "craigslist.org"),
    ("www.offerup.com", "offerup.com"),
    ("https://www.ebay.com/itm/123", "ebay.com"),
    ("foo.bar.gumtree.com.au", "gumtree.com.au"),
    ("https://www.kijiji.ca/b-cars/", "kijiji.ca"),
    ("cargurus.com", "cargurus.com"),
    ("", ""),
])
def test_site_key(inp, expected):
    assert site_key(inp) == expected


# ---------------------------------------------------------------------------
# site_profiles store
# ---------------------------------------------------------------------------

def test_upsert_and_get_profile(db):
    upsert_site_profile({
        "domain": "https://offerup.com/item/detail/x",   # normalized on the way in
        "display_name": "OfferUp",
        "key_prefix": "offerup",
        "listing_url_regex": r"/item/detail/([A-Za-z0-9_-]{5,})",
        "search_param": "q",
        "sort_values": ["price", "newest"],
    }, db_path=db)
    p = get_site_profile("www.offerup.com/foo", db_path=db)
    assert p is not None
    assert p["domain"] == "offerup.com"
    assert p["key_prefix"] == "offerup"
    assert p["sort_values"] == ["price", "newest"]   # decoded back to a list


def test_upsert_replaces_and_list_and_delete(db):
    upsert_site_profile({"domain": "kijiji.ca", "listing_url_regex": "/a/(\\d+)"}, db_path=db)
    upsert_site_profile({"domain": "kijiji.ca", "listing_url_regex": "/b/(\\d+)"}, db_path=db)
    p = get_site_profile("kijiji.ca", db_path=db)
    assert p["listing_url_regex"] == "/b/(\\d+)"     # REPLACE, not duplicate
    upsert_site_profile({"domain": "offerup.com", "listing_url_regex": "/i/(\\d+)"}, db_path=db)
    assert {x["domain"] for x in list_site_profiles(db_path=db)} == {"kijiji.ca", "offerup.com"}
    assert delete_site_profile("www.kijiji.ca", db_path=db) is True
    assert {x["domain"] for x in list_site_profiles(db_path=db)} == {"offerup.com"}


def test_get_missing_profile_is_none(db):
    assert get_site_profile("nope.example", db_path=db) is None


# ---------------------------------------------------------------------------
# Pattern inference
# ---------------------------------------------------------------------------

def test_infer_ebay_numeric():
    urls = [
        "https://www.ebay.com/itm/355123456789",
        "https://www.ebay.com/itm/355987654321",
        "https://www.ebay.com/itm/355111222333",
    ]
    p = infer_listing_pattern(urls)
    assert p is not None
    assert re.search(p["listing_url_regex"], "/itm/355123456789").group(1) == "355123456789"
    assert p["id_kind"] == "numeric"


def test_infer_craigslist_with_slug_and_html():
    # The title slug VARIES per listing and must NOT break grouping; id ends in .html.
    urls = [
        "https://seattle.craigslist.org/see/cto/d/seattle-2015-ford-f150/7891234567.html",
        "https://seattle.craigslist.org/tac/cto/d/tacoma-ram-1500/7890000001.html",
        "https://seattle.craigslist.org/est/cto/d/everett-chevy-truck/7895555555.html",
    ]
    p = infer_listing_pattern(urls)
    assert p is not None
    rgx = p["listing_url_regex"]
    m = re.search(rgx, "/see/cto/d/seattle-2015-ford-f150/7891234567.html")
    assert m and m.group(1) == "7891234567"
    # A non-listing nav URL on the same shape-space must NOT match.
    assert re.search(rgx, "/about/help") is None


def test_infer_guid_style_alnum():
    urls = [
        "https://offerup.com/item/detail/a1b2c3d4e5",
        "https://offerup.com/item/detail/f6g7h8i9j0",
        "https://offerup.com/item/detail/k1l2m3n4o5",
    ]
    p = infer_listing_pattern(urls)
    assert p is not None
    assert p["id_kind"] == "alnum"
    assert re.search(p["listing_url_regex"], "/item/detail/a1b2c3d4e5").group(1) == "a1b2c3d4e5"


def test_infer_returns_none_without_id_like_urls():
    assert infer_listing_pattern(["https://x.com/about", "https://x.com/help"]) is None
    assert infer_listing_pattern([]) is None


# ---------------------------------------------------------------------------
# Profile-aware listing keys
# ---------------------------------------------------------------------------

def test_listing_key_uses_learned_profile():
    profiles = [{
        "domain": "offerup.com",
        "key_prefix": "offerup",
        "listing_url_regex": r"/item/detail/([A-Za-z0-9_-]{5,})",
    }]
    assert _listing_key("https://offerup.com/item/detail/a1b2c3d4e5", profiles) == "offerup:a1b2c3d4e5"
    # A profile exists for the site but this URL is chrome → no key, no fuzzy fallback.
    assert _listing_key("https://offerup.com/login", profiles) is None


def test_learned_profile_does_not_override_builtin():
    # Even with a junk profile present, the built-in eBay pattern wins for ebay hosts.
    profiles = [{"domain": "ebay.com", "key_prefix": "x", "listing_url_regex": r"/(\d+)"}]
    assert _listing_key("https://www.ebay.com/itm/355123456789", profiles) == "ebay:355123456789"


def test_listing_key_generic_fallback_when_no_profile():
    assert _listing_key("https://newsite.com/listing/item-12345678") == "listing:12345678"


def test_empty_regex_profile_does_not_poison_site():
    # A failed learn (empty regex) must NOT block detection: the generic fallback still runs.
    profiles = [{"domain": "newsite.com", "key_prefix": "newsite", "listing_url_regex": ""}]
    assert _listing_key("https://newsite.com/listing/item-12345678", profiles) == "listing:12345678"


def test_site_status_builtin_learned_unknown(db):
    from web_watcher.sitelearn import site_status, unknown_sites, first_url_for_domain
    assert site_status("https://seattle.craigslist.org/search/cta", db_path=db)["kind"] == "builtin"
    assert site_status("https://www.ebay.com/sch/i.html?_nkw=x", db_path=db)["kind"] == "builtin"
    assert site_status("https://offerup.com/search?q=kayak", db_path=db)["kind"] == "unknown"
    upsert_site_profile({"domain": "offerup.com", "listing_url_regex": r"/item/detail/(\d+)"}, db_path=db)
    assert site_status("https://offerup.com/search?q=kayak", db_path=db)["kind"] == "learned"
    urls = ["https://seattle.craigslist.org/x", "https://offerup.com/a",
            "https://www.mercari.com/search/?keyword=drill", "https://www.mercari.com/x2"]
    assert unknown_sites(urls, db_path=db) == ["mercari.com"]
    assert first_url_for_domain(urls, "mercari.com") == "https://www.mercari.com/search/?keyword=drill"


def test_listing_key_craigslist_new_and_legacy_formats():
    # NEW 2026 format: /view/d/<slug>/<alphanumeric-id>, no ".html".
    assert _listing_key("https://www.craigslist.org/view/d/seattle-2015-ford/fvjPbmYUxaNP8qvENeUHUn") \
        == "craigslist:fvjPbmYUxaNP8qvENeUHUn"
    # LEGACY format still resolves (old stored URLs).
    assert _listing_key("https://seattle.craigslist.org/see/ctd/d/seattle-ford/7891234567.html") \
        == "craigslist:7891234567"
    # Craigslist chrome (no listing id) → no key.
    assert _listing_key("https://www.craigslist.org/about/help") is None
