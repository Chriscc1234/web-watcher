"""A watch only ever READS.

Facebook was guarded first because the penalty there is an account ban, but the rule was never
Facebook's. On craigslist the agent clicked "hide posting" — which removes a listing from the
feed, and craigslist remembers that in localStorage which is reloaded into every sweep. A watch
that can hide its own results quietly shrinks what it will ever find, and "flag", "favourite"
and "delete" sit on the same card.

See fb_safety.is_mutating_action and browser.clear_site_local_storage."""

from __future__ import annotations

import json

from web_watcher.browser import clear_site_local_storage
from web_watcher.fb_safety import is_blocked_action, is_mutating_action


# ── what a watch must never click, on any site ───────────────────────────────────

def test_the_craigslist_click_that_started_this_is_blocked():
    assert is_mutating_action("hide posting") is True
    assert is_mutating_action("add to favorites list") is True


def test_other_mutating_controls_are_blocked():
    for label in ("hide", "flag as prohibited", "delete this posting", "renew", "repost",
                  "reply", "contact the seller", "buy it now", "make an offer", "place bid",
                  "report", "post an ad", "subscribe", "save this search"):
        assert is_mutating_action(label) is True, label


def test_browsing_is_never_blocked():
    """A guard that stops the agent browsing is worse than no guard at all."""
    for label in ("see more", "show more", "next page", "sort by newest", "gallery view",
                  "search craigslist", "search results", "price", "map view", "photos",
                  "cars & trucks", "back to results", "open listing",
                  "show hidden posts", "saved searches"):
        assert is_mutating_action(label) is False, label


def test_a_listing_title_is_not_an_action():
    for label in ("1998 Scarab 22'", "$12,500", "GLASPLY 16' FISHING POWER BOAT",
                  "1983 Bayliner Explorer 2070"):
        assert is_mutating_action(label) is False, label


def test_blank_labels_are_safe():
    assert is_mutating_action("") is False
    assert is_mutating_action(None) is False


def test_facebooks_stricter_list_still_applies_there():
    """The site-agnostic list is narrower on purpose — Facebook keeps its own on top."""
    assert is_mutating_action("Like") is False          # not a mutation of OUR results
    assert is_blocked_action("Like") is True            # but never allowed on Facebook


# ── forgetting what was already hidden ───────────────────────────────────────────

def _state(tmp_path, origins):
    p = tmp_path / "browser_state.json"
    p.write_text(json.dumps({"cookies": [{"name": "cl_b", "domain": ".craigslist.org"}],
                             "origins": origins}), encoding="utf-8")
    return p


def test_hidden_postings_are_forgotten_but_cookies_are_kept(tmp_path):
    p = _state(tmp_path, [
        {"origin": "https://seattle.craigslist.org",
         "localStorage": [{"name": "bannedPostings", "value": "[1,2,3]"},
                          {"name": "recentSearches", "value": "[]"}]},
        {"origin": "https://www.offerup.com", "localStorage": [{"name": "x", "value": "1"}]},
    ])
    out = clear_site_local_storage("craigslist", p)
    assert out["cleared"] == 2
    data = json.loads(p.read_text(encoding="utf-8"))
    assert [o["origin"] for o in data["origins"]] == ["https://www.offerup.com"]
    assert data["cookies"]            # cookies keep us looking like a returning visitor


def test_clearing_is_a_no_op_when_there_is_nothing_to_clear(tmp_path):
    p = _state(tmp_path, [{"origin": "https://www.offerup.com", "localStorage": []}])
    before = p.read_text(encoding="utf-8")
    assert clear_site_local_storage("craigslist", p)["cleared"] == 0
    assert p.read_text(encoding="utf-8") == before      # file untouched


def test_a_missing_or_broken_state_file_never_raises(tmp_path):
    assert clear_site_local_storage("craigslist", tmp_path / "nope.json")["cleared"] == 0
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert clear_site_local_storage("craigslist", bad)["cleared"] == 0
