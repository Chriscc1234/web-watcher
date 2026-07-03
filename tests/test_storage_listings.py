"""
Tests for the Phase-2 listing-centric data layer: attribute parsing, the global
`listings` store, and per-watch `observations`. All offline, temp-db.
"""

from __future__ import annotations

import pytest

from web_watcher.monitor import parse_listing_attributes
from web_watcher.storage import (
    init_db, upsert_listing, record_observation, query_listings, find_duplicate,
    count_matches,
)


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    init_db(p)
    return p


# ---------------------------------------------------------------------------
# Attribute parsing
# ---------------------------------------------------------------------------

def test_parse_attributes_craigslist_style():
    a = parse_listing_attributes(
        "2005 Chevrolet C4500 Kodiak", "$27,500",
        "condition: good drive: 4wd odometer: 154,172 transmission: automatic")
    assert a == {"price_value": 27500, "year": 2005, "mileage": 154172,
                 "transmission": "automatic", "drivetrain": "4wd"}


def test_parse_attributes_keyword_fallback():
    a = parse_listing_attributes("1999 Ford F-350 4x4 5-speed manual", "$9,900", "")
    assert a["transmission"] == "manual"
    assert a["drivetrain"] == "4wd"
    assert a["year"] == 1999


def test_parse_attributes_missing_fields_omitted():
    a = parse_listing_attributes("Some boat", "", "")
    assert "transmission" not in a and "mileage" not in a


# ---------------------------------------------------------------------------
# listings store + observations
# ---------------------------------------------------------------------------

def _put(db, key="craigslist:1", title="2010 Ford F-150", price="$8,000",
         details="drive: 4wd transmission: manual odometer: 120,000", ts="t0"):
    attrs = parse_listing_attributes(title, price, details)
    upsert_listing(key, source="seattle.craigslist.org", url=f"https://x/{key}.html",
                   title=title, price_text=price, attributes=attrs, details=details,
                   ts=ts, db_path=db)


def test_upsert_and_global_query(db):
    _put(db)
    rows = query_listings(db_path=db)
    assert len(rows) == 1
    r = rows[0]
    assert r["listing_key"] == "craigslist:1"
    assert r["transmission"] == "manual" and r["drivetrain"] == "4wd"
    assert r["price_value"] == 8000 and r["mileage"] == 120000


def test_attribute_filters(db):
    _put(db, key="craigslist:1", title="2010 Ford F-150 manual",
         details="transmission: manual drive: 4wd odometer: 120,000", price="$8,000")
    _put(db, key="craigslist:2", title="2012 Ram auto",
         details="transmission: automatic drive: 4wd odometer: 90,000", price="$15,000")
    assert {r["listing_key"] for r in query_listings(transmission="manual", db_path=db)} == {"craigslist:1"}
    assert {r["listing_key"] for r in query_listings(max_price=10000, db_path=db)} == {"craigslist:1"}
    assert len(query_listings(drivetrain="4wd", db_path=db)) == 2


def test_posted_at_is_stored_and_preserved(db):
    upsert_listing("craigslist:1", source="seattle.craigslist.org",
                   url="https://x/craigslist:1.html", title="2010 Ford F-150",
                   price_text="$8,000", attributes={}, posted_at="2026-06-20T09:30:00-0700",
                   ts="t0", db_path=db)
    assert query_listings(db_path=db)[0]["posted_at"] == "2026-06-20T09:30:00-0700"
    # A later cheap re-sight with no posted_at must not wipe the stored one.
    upsert_listing("craigslist:1", source="seattle.craigslist.org",
                   url="https://x/craigslist:1.html", title="2010 Ford F-150",
                   price_text="$8,000", attributes={}, posted_at="", ts="t1", db_path=db)
    assert query_listings(db_path=db)[0]["posted_at"] == "2026-06-20T09:30:00-0700"


def test_upsert_preserves_richer_data(db):
    _put(db, details="drive: 4wd transmission: manual odometer: 120,000", ts="t0")
    # Re-sight with NO details (e.g. a cheap re-scrape) must not wipe captured attrs.
    upsert_listing("craigslist:1", source="seattle.craigslist.org",
                   url="https://x/craigslist:1.html", title="2010 Ford F-150",
                   price_text="$8,000", attributes={}, details="", ts="t1", db_path=db)
    r = query_listings(db_path=db)[0]
    assert r["transmission"] == "manual"        # preserved
    assert r["details"]                          # not wiped
    assert r["last_seen"] == "t1"                # but last_seen bumped


def test_observation_links_watch_to_listing(db):
    _put(db, key="craigslist:1")
    _put(db, key="craigslist:2", title="2012 Ram boat trailer", details="")
    record_observation("wid-A", "Trucks", "craigslist:1", "t0", matched=True,
                       judge_reason="4x4 manual truck", db_path=db)
    record_observation("wid-A", "Trucks", "craigslist:2", "t0", matched=False, db_path=db)

    all_obs = query_listings(watch_id="wid-A", db_path=db)
    assert len(all_obs) == 2
    matched = query_listings(watch_id="wid-A", matched=True, db_path=db)
    assert [r["listing_key"] for r in matched] == ["craigslist:1"]
    assert matched[0]["judge_reason"] == "4x4 manual truck"
    # A different watch sees nothing.
    assert query_listings(watch_id="wid-B", db_path=db) == []


def test_find_duplicate_requires_same_source(db):
    fp = "2010|8000|ford f150"
    upsert_listing("a:1", source="craigslist.org", url="u1", title="t",
                   fingerprint=fp, ts="t0", db_path=db)
    record_observation("w", "W", "a:1", "t0", matched=True, db_path=db)
    # same fingerprint but DIFFERENT source -> not merged (err toward separate)
    assert find_duplicate("w", fp, "facebook.com", "b:2", db_path=db) is None
    # same fingerprint + same source -> canonical returned, with its match verdict
    d = find_duplicate("w", fp, "craigslist.org", "b:2", db_path=db)
    assert d and d["listing_key"] == "a:1" and d["matched"] == 1


def test_fingerprint_weak_signal_guard():
    from web_watcher.monitor import listing_fingerprint
    # Rich distinctive title → fingerprints (real repost detection).
    assert listing_fingerprint("2002 Mazda Miata Roadster Wonderful Little Car", 5800, 2002)
    # Year + a 2-token make/model → fingerprints.
    assert listing_fingerprint("Ford F150", 8000, 2010)
    # Terse generic title, NO year → NO fingerprint (would merge different trucks otherwise).
    assert listing_fingerprint("Chevy Silverado", 5600, None) == ""
    # Single-word generic item, no year → no fingerprint.
    assert listing_fingerprint("Couch", 200, None) == ""


def test_results_link_to_freshest_repost(db):
    # A reposted item: same fingerprint+source, different post ids/urls, different last_seen.
    fp = "2002|5800|mazda miata roadster wonderful little"
    upsert_listing("cl:old", source="cl.org", url="https://cl.org/old.html", title="Miata",
                   fingerprint=fp, ts="t0", db_path=db)
    upsert_listing("cl:new", source="cl.org", url="https://cl.org/new.html", title="Miata",
                   fingerprint=fp, ts="t9", db_path=db)   # seen more recently
    # The canonical (observed) row is the OLDER post whose page is likely deleted.
    record_observation("w", "W", "cl:old", "t0", matched=True, db_path=db)
    rows = query_listings(watch_id="w", matched=True, db_path=db)
    assert len(rows) == 1 and rows[0]["dup_count"] == 2
    # …but the link must point at the FRESHEST repost, not the stale canonical.
    assert rows[0]["url"] == "https://cl.org/new.html"


def test_dup_count_surfaced_on_listing(db):
    fp = "2010|8000|ford f150"
    upsert_listing("a:1", source="cl.org", url="u1", title="t1", fingerprint=fp, ts="t0", db_path=db)
    upsert_listing("a:2", source="cl.org", url="u2", title="t2", fingerprint=fp, ts="t0", db_path=db)
    upsert_listing("a:3", source="cl.org", url="u3", title="t3", fingerprint="other", ts="t0", db_path=db)
    counts = {r["listing_key"]: r["dup_count"] for r in query_listings(db_path=db)}
    assert counts["a:1"] == 2 and counts["a:2"] == 2   # the two dups note each other
    assert counts["a:3"] == 1                           # unique


def test_term_expansion_learns_and_unions(db):
    from web_watcher.storage import get_term_expansion, save_term_expansion
    assert get_term_expansion("manual sports cars", db_path=db) == []
    save_term_expansion("manual sports cars", ["miata", "corvette"], db_path=db)
    assert get_term_expansion("Manual  Sports  Cars!", db_path=db) == ["miata", "corvette"]  # normalized key
    # learning: a second expansion UNIONs (grows), de-duped case-insensitively
    save_term_expansion("manual sports cars", ["Corvette", "mustang"], db_path=db)
    assert get_term_expansion("manual sports cars", db_path=db) == ["miata", "corvette", "mustang"]


def test_build_search_urls():
    from web_watcher.search_terms import build_search_urls
    urls = build_search_urls("https://seattle.craigslist.org/search/cta?query=sports+car&hasPic=1",
                             ["miata", "corvette"])
    assert len(urls) == 2
    assert "query=miata" in urls[0] and "hasPic=1" in urls[0]      # term swapped, filter kept
    assert "query=corvette" in urls[1]
    # no recognizable search param -> unchanged
    assert build_search_urls("https://forecast.weather.gov/x?lat=47", ["a"]) == \
           ["https://forecast.weather.gov/x?lat=47"]


def test_run_listing_query_maps_filters(db):
    from web_watcher.dashboard.server import _run_listing_query
    _put(db, key="cl:1", title="2012 Ford F-150", price="$8,000",
         details="drive: 4wd transmission: manual odometer: 90,000")
    _put(db, key="cl:2", title="2015 Ram 1500", price="$15,000",
         details="drive: 4wd transmission: automatic odometer: 60,000")
    assert [r["listing_key"] for r in
            _run_listing_query({"transmission": "manual", "max_price": 10000}, db_path=db)] == ["cl:1"]
    assert len(_run_listing_query({"drivetrain": "4wd"}, db_path=db)) == 2
    # an unresolvable watch name returns [] (never silently falls back to "all watches")
    assert _run_listing_query({"watch": "definitely not a real watch name"}, db_path=db) == []


def test_count_matches(db):
    _put(db, key="cl:1"); _put(db, key="cl:2"); _put(db, key="cl:3")
    record_observation("w", "W", "cl:1", "t0", matched=True, db_path=db)
    record_observation("w", "W", "cl:2", "t0", matched=True, db_path=db)
    record_observation("w", "W", "cl:3", "t0", matched=False, db_path=db)
    assert count_matches("w", db_path=db) == 2
    assert count_matches("other", db_path=db) == 0


def test_observation_upsert_updates_verdict(db):
    _put(db, key="craigslist:1")
    record_observation("wid-A", "Trucks", "craigslist:1", "t0", matched=False, db_path=db)
    record_observation("wid-A", "Trucks", "craigslist:1", "t1", matched=True,
                       judge_reason="reconsidered", db_path=db)
    rows = query_listings(watch_id="wid-A", db_path=db)
    assert len(rows) == 1 and rows[0]["matched"] == 1


def test_matched_only_filters_across_all_watches(db):
    # Two listings; only one is judged a match by any watch.
    _put(db, key="cl:good", title="2013 Ford F550 Diesel")
    _put(db, key="cl:junk", title="1985 Mazda RX-7")
    record_observation("w", "Diesel", "cl:good", "t0", matched=True, db_path=db)
    record_observation("w", "Diesel", "cl:junk", "t0", matched=False, db_path=db)
    # No watch filter + matched=True → only the globally-matched listing (not the junk).
    keys = {r["listing_key"] for r in query_listings(matched=True, db_path=db)}
    assert keys == {"cl:good"}
    # Without the filter, both show.
    assert {r["listing_key"] for r in query_listings(db_path=db)} == {"cl:good", "cl:junk"}
