# -*- coding: utf-8 -*-
"""The Market Map — aggregate reality over everything ever seen, matches first."""

from __future__ import annotations

from web_watcher import market, storage


def _seed(db):
    storage.init_db(db)
    rows = [
        ("k1", "craigslist.org", "2015 Toyota Tacoma manual", 15000, 2015, "manual", True),
        ("k2", "facebook.com",   "2019 Toyota Tacoma TRD",    32000, 2019, "automatic", False),
        ("k3", "offerup.com",    "2008 Toyota Tacoma",         9500, 2008, "automatic", False),
        ("k4", "craigslist.org", "1997 Toyota Tacoma xtracab", 4800, 1997, "manual", True),
        ("k5", "craigslist.org", "Tacoma truck canopy",         300, None, "", False),
    ]
    for key, src, title, price, year, trans, matched in rows:
        storage.upsert_listing(key, source=src, url=f"https://{src}/{key}", title=title,
                               price_text=f"${price}", details="body " * 40,
                               attributes={"price_value": price, "year": year,
                                           "transmission": trans},
                               ts="2026-08-30T00:00:00", db_path=db)
        with storage._connect(db) as conn:
            conn.execute("UPDATE listings SET price_value=?, year=?, transmission=? "
                         "WHERE listing_key=?", (price, year, trans, key))
        storage.record_observation("w1", "W", key, "2026-08-30T00:00:00",
                                   matched=matched, rating=4 if matched else 2, db_path=db)


def test_market_summary_aggregates_reality(tmp_path):
    db = tmp_path / "m.db"
    _seed(db)
    d = market.market_summary(q="tacoma", db_path=db)
    assert d["total"] == 5 and d["matched"] == 2
    assert d["price"]["min"] == 300 and d["price"]["max"] == 32000
    assert d["by_source"]["craigslist.org"] == 3
    assert d["by_decade"].get("1990s") == 1 and d["by_decade"].get("2010s") == 2
    assert d["by_transmission"]["manual"] == 2
    # matches lead the sample — the user's rule, verbatim: "matches should take priority".
    assert d["sample"][0]["matched"] and d["sample"][1]["matched"]


def test_market_summary_all_words_must_match(tmp_path):
    db = tmp_path / "m.db"
    _seed(db)
    assert market.market_summary(q="tacoma manual", db_path=db)["total"] == 1
    assert market.market_summary(q="nonexistent", db_path=db)["total"] == 0


def test_market_summary_by_watch(tmp_path):
    db = tmp_path / "m.db"
    _seed(db)
    d = market.market_summary(watch_id="w1", db_path=db)
    assert d["total"] == 5 and d["matched"] == 2
