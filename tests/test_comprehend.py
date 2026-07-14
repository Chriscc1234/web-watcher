"""Offline tests for site comprehension — the deterministic evidence-rendering and the
verdict normalization. The comprehension QUALITY is validated live against the 72b."""

from __future__ import annotations

import json

from web_watcher import comprehend as CO
from web_watcher.config import AppConfig


def test_evidence_block_surfaces_box_labels():
    struct = {
        "title": "National Weather Service",
        "nav_links": ["Forecast", "Radar", "Safety"],
        "headings": ["Local Forecast"],
        "search_boxes": [{"label": "Enter Your City, ST or ZIP Code", "name": "inputstring"}],
        "price_count": 0, "cardish_links": 12, "text_sample": "Today's forecast...",
    }
    ev = CO._evidence_block(struct)
    assert "Enter Your City, ST or ZIP Code" in ev
    assert "Forecast, Radar, Safety" in ev
    assert "PRICE-LIKE VALUES ON PAGE: 0" in ev


def test_normalize_understanding_coerces_purpose():
    u = CO._normalize_understanding(
        {"site_kind": "Weather", "is_listings_site": False,
         "search_box": {"present": True, "purpose": "LOCATION picker", "evidence": "City or ZIP"},
         "viable_for_watch": False, "reason": "no items for sale"}, "qwen2.5:72b")
    assert u["site_kind"] == "weather"
    assert u["is_listings_site"] is False
    assert u["viable_for_watch"] is False
    assert u["search_box"]["purpose"] == "location"
    assert u["model"] == "qwen2.5:72b"


def test_normalize_understanding_keyword_box():
    u = CO._normalize_understanding(
        {"site_kind": "marketplace", "is_listings_site": True,
         "search_box": {"purpose": "keyword-items", "evidence": "Search for anything"},
         "viable_for_watch": True}, "m")
    assert u["is_listings_site"] and u["viable_for_watch"]
    assert u["search_box"]["purpose"] == "keyword-items"


def test_normalize_understanding_defaults_safe():
    u = CO._normalize_understanding({}, "m")
    assert u["site_kind"] == "other"
    assert u["is_listings_site"] is False and u["viable_for_watch"] is False
    assert u["search_box"]["purpose"] == "none"


def test_understanding_round_trips_through_storage(tmp_path):
    from web_watcher import storage as S
    db = tmp_path / "t.db"
    S.init_db(db)
    u = {"site_kind": "weather", "is_listings_site": False, "viable_for_watch": False,
         "search_box": {"present": True, "purpose": "location", "evidence": "City or ZIP"},
         "reason": "weather site"}
    S.save_site_understanding("https://www.weather.gov/forecast", u, db_path=db)
    got = S.get_site_understanding("https://www.weather.gov", db_path=db)
    assert got and got["site_kind"] == "weather" and got["search_box"]["purpose"] == "location"
    # a mechanical profile upsert must NOT wipe the understanding
    S.upsert_site_profile({"domain": "weather.gov", "search_param": "q"}, db_path=db)
    still = S.get_site_understanding("https://www.weather.gov", db_path=db)
    assert still and still["site_kind"] == "weather"
