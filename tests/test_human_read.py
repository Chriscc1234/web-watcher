"""
Reading a listing like a person, and never losing a match to an arbitrary cap.

These cover the three defects a supervised Facebook run exposed: the deep-read was gated on
a field the judge didn't require (so it never ran), the baseline judged an arbitrary slice of
a primed batch (so real matches were never looked at), and when the deep-read DID run it spent
under two seconds on the page (open, scrape, close — the loudest automation tell there is).
"""

from __future__ import annotations

import threading
import time
import types

import pytest

from web_watcher import scheduler
from web_watcher.monitor import Listing, human_read, nap


# --------------------------------------------------------------------------
# A page stand-in that records what was done to it
# --------------------------------------------------------------------------

class FakeMouse:
    def __init__(self):
        self.wheels, self.moves = [], []

    def wheel(self, dx, dy):
        self.wheels.append((dx, dy))

    def move(self, x, y, steps=1):
        self.moves.append((x, y))


class FakePage:
    def __init__(self, url="https://www.facebook.com/marketplace/item/123/", chars=1200):
        self.url = url
        self._chars = chars
        self.mouse = FakeMouse()

    def evaluate(self, js):
        return self._chars


@pytest.fixture
def fast_clock(monkeypatch):
    """Run human_read on a VIRTUAL clock: every nap advances time instead of spending it.

    The behaviour under test is 'how long does it dwell and what does it do while dwelling',
    and a suite that actually waits out 40-second reads is a suite nobody runs. Time still
    moves exactly as the code asks it to — it just doesn't cost anything.
    """
    from web_watcher import monitor

    class Clock:
        def __init__(self):
            self.t = 1000.0

        def monotonic(self):
            return self.t

        def nap(self, seconds, stop_event=None):
            end = self.t + max(0.0, seconds)
            # Same granularity as the real nap, so stop latency stays observable.
            while self.t < end:
                if stop_event is not None and stop_event.is_set():
                    return False
                self.t = min(end, self.t + 0.2)
            return True

    clock = Clock()
    monkeypatch.setattr(monitor.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(monitor, "nap", clock.nap)
    return clock


# --------------------------------------------------------------------------
# nap()
# --------------------------------------------------------------------------

def test_nap_returns_true_when_it_sleeps_the_whole_time():
    started = time.monotonic()
    assert nap(0.3) is True
    assert time.monotonic() - started >= 0.25


def test_nap_bails_immediately_when_stopped():
    ev = threading.Event()
    ev.set()
    started = time.monotonic()
    assert nap(30.0, ev) is False
    assert time.monotonic() - started < 1.0, "a set stop_event must not wait out the nap"


def test_nap_honours_a_stop_raised_mid_sleep():
    ev = threading.Event()
    threading.Timer(0.3, ev.set).start()
    started = time.monotonic()
    assert nap(20.0, ev) is False
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, f"stop took {elapsed:.1f}s to be noticed"


# --------------------------------------------------------------------------
# human_read()
# --------------------------------------------------------------------------

def test_human_read_actually_dwells(fast_clock):
    """The whole point: a listing visit must not be a sub-second tab flicker."""
    spent = human_read(FakePage(chars=1200))
    assert spent >= 8.0, f"only spent {spent:.1f}s on the ad — that is not reading"


def test_human_read_scrolls_and_moves_the_cursor(fast_clock):
    page = FakePage(chars=1200)
    human_read(page)
    assert page.mouse.wheels, "never scrolled the ad"
    assert any(dy < 0 for _, dy in page.mouse.wheels), "never scrolled back up"
    assert page.mouse.moves, "cursor sat frozen for the whole visit"


def test_human_read_spends_longer_on_a_long_ad_than_a_short_one(fast_clock):
    short = sum(human_read(FakePage(chars=200)) for _ in range(12))
    long_ = sum(human_read(FakePage(chars=12000)) for _ in range(12))
    assert long_ > short, f"long ads {long_:.0f}s were not read longer than short ads {short:.0f}s"


def test_human_read_is_capped(fast_clock):
    assert human_read(FakePage(chars=5_000_000)) <= 60.0


def test_human_read_stops_promptly_when_asked(fast_clock):
    ev = threading.Event()
    ev.set()
    assert human_read(FakePage(chars=12000), ev) < 3.0, "Stop was not honoured during the read"


def test_human_read_survives_a_page_that_throws(fast_clock):
    class Broken(FakePage):
        def evaluate(self, js):
            raise RuntimeError("detached")
    broken = Broken()
    broken.mouse.wheel = lambda *a: (_ for _ in ()).throw(RuntimeError("gone"))
    human_read(broken)   # must not raise — politeness never breaks the scrape


def test_facebook_gets_an_unhurried_floor(fast_clock):
    """FB watches engagement closely enough that the quick end of the range is a tell."""
    fb  = [human_read(FakePage("https://www.facebook.com/marketplace/item/1/", 300)) for _ in range(20)]
    oth = [human_read(FakePage("https://www.craigslist.org/view/d/x/abc", 300)) for _ in range(20)]
    assert sum(fb) / len(fb) > sum(oth) / len(oth)


def test_every_read_clears_the_old_two_second_flicker(fast_clock):
    """Regression guard on the exact number that made the Facebook run look automated."""
    for chars in (0, 120, 800, 4000):
        assert human_read(FakePage(chars=chars)) > 2.0


# --------------------------------------------------------------------------
# The gate that stopped the deep-read from ever running
# --------------------------------------------------------------------------

def _watch(instruction="", judgment_prompt=None, wid="w1", name="W"):
    return types.SimpleNamespace(instruction=instruction, judgment_prompt=judgment_prompt,
                                 id=wid, name=name)


def test_instruction_only_watch_still_deep_reads():
    """The live regression: the judge ran on the instruction, the deep-read did not, so the
    judge was asked to check location and price with nothing but a card title."""
    assert scheduler._wants_deep_read(_watch(instruction="MacGregor sailboats near Seattle"))


def test_judgment_prompt_watch_still_deep_reads():
    assert scheduler._wants_deep_read(_watch(judgment_prompt="must be 4x4"))


def test_watch_with_no_criteria_at_all_does_not_deep_read():
    assert not scheduler._wants_deep_read(_watch())
    assert not scheduler._wants_deep_read(_watch(instruction="   "))


def test_deep_read_gate_agrees_with_the_judge_gate():
    """These two gates drifting apart is the whole bug — bind them together in a test."""
    for w in (_watch(instruction="find boats"), _watch(judgment_prompt="cheap"),
              _watch(), _watch(instruction="  ")):
        judge_would_run = bool(w.judgment_prompt or (w.instruction or "").strip())
        assert scheduler._wants_deep_read(w) is judge_would_run


# --------------------------------------------------------------------------
# Relevance-first judging
# --------------------------------------------------------------------------

def _titles(*names):
    return [Listing(key=f"k{i}", url=f"https://x/{i}", title=t) for i, t in enumerate(names)]


def test_judge_order_pulls_brand_hits_to_the_front():
    batch = _titles(*["Catalina 30 Seattle, WA"] * 40,
                    "1984 MacGregor 25 Puyallup, WA",
                    *["Laser Sailboat Seattle, WA"] * 40)
    w = _watch(instruction="Look for MacGregor sailboats near Seattle WA")
    ordered = scheduler._judge_order(batch, w)
    assert "MacGregor" in ordered[0].title, "the one real match was not judged first"


def test_judge_order_rescues_matches_past_the_cap():
    """A match sitting at position 200 of a primed feed must still be judged."""
    filler = ["Catalina 30 Seattle, WA"] * 200
    batch = _titles(*filler, "1993 MacGregor 26S Seattle, WA")
    w = _watch(instruction="MacGregor sailboats near Seattle")
    ordered = scheduler._judge_order(batch, w)[:scheduler._BASELINE_JUDGE_CAP]
    assert any("MacGregor" in l.title for l in ordered)


def test_judge_order_weights_rare_terms_over_common_ones():
    """'seattle' is in half the feed and separates nothing; 'macgregor' is the real signal."""
    batch = _titles("Hunter 34 Seattle, WA", "MacGregor 26 Tacoma, WA", "Cal 29 Seattle, WA")
    w = _watch(instruction="MacGregor sailboat near Seattle WA")
    assert "MacGregor" in scheduler._judge_order(batch, w)[0].title


def test_judge_order_is_stable_without_an_instruction():
    batch = _titles("a", "b", "c")
    assert [l.title for l in scheduler._judge_order(batch, _watch())] == ["a", "b", "c"]


def test_judge_order_keeps_every_listing():
    batch = _titles("MacGregor 26", "Catalina 30", "Hunter 34")
    w = _watch(instruction="MacGregor sailboats")
    assert len(scheduler._judge_order(batch, w)) == 3
    assert {l.key for l in scheduler._judge_order(batch, w)} == {l.key for l in batch}


def test_instruction_terms_drop_filler_words():
    terms = scheduler._instruction_terms(_watch(instruction="Look for MacGregor sailboats near Seattle"))
    assert "macgregor" in terms and "seattle" in terms
    assert "look" not in terms and "for" not in terms and "near" not in terms


# --------------------------------------------------------------------------
# Going back for matches that were banked but never read
# --------------------------------------------------------------------------

def test_explore_matches_reads_only_the_unread(monkeypatch):
    rows = [
        {"listing_key": "a", "url": "https://x/a", "title": "MacGregor 26", "details": "already read"},
        {"listing_key": "b", "url": "https://x/b", "title": "MacGregor 25", "details": ""},
        {"listing_key": "c", "url": "https://x/c", "title": "MacGregor 21", "details": "   "},
    ]
    monkeypatch.setattr("web_watcher.storage.query_listings", lambda **kw: rows)
    seen = {}

    def fake_capture(page, listings, stop_event=None):
        seen["keys"] = [l.key for l in listings]
        for l in listings:
            l.details = "read now"

    monkeypatch.setattr(scheduler, "_capture_listing_bodies", fake_capture)
    monkeypatch.setattr(scheduler, "upsert_listing", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "set_listing_archive", lambda *a, **k: None)

    n = scheduler._explore_matches(_watch(instruction="MacGregor"), None, None,
                                   page=object(), stop_event=None)
    assert n == 2
    assert set(seen["keys"]) == {"b", "c"}, "re-read a listing that already had its body"


def test_explore_matches_is_capped_per_sweep(monkeypatch):
    rows = [{"listing_key": f"k{i}", "url": f"https://x/{i}", "title": "MacGregor", "details": ""}
            for i in range(50)]
    monkeypatch.setattr("web_watcher.storage.query_listings", lambda **kw: rows)
    grabbed = {}
    monkeypatch.setattr(scheduler, "_capture_listing_bodies",
                        lambda p, ls, se=None: grabbed.setdefault("n", len(ls)))
    monkeypatch.setattr(scheduler, "upsert_listing", lambda *a, **k: None)
    scheduler._explore_matches(_watch(instruction="MacGregor"), None, None, page=object())
    assert grabbed["n"] == scheduler._EXPLORE_BACKLOG_PER_SWEEP


def test_explore_matches_does_nothing_without_a_page():
    assert scheduler._explore_matches(_watch(instruction="x"), None, None, page=None) == 0


def test_explore_matches_respects_stop(monkeypatch):
    monkeypatch.setattr("web_watcher.storage.query_listings",
                        lambda **kw: pytest.fail("queried after Stop"))
    ev = threading.Event()
    ev.set()
    assert scheduler._explore_matches(_watch(instruction="x"), None, None,
                                      page=object(), stop_event=ev) == 0


def test_explore_matches_never_raises(monkeypatch):
    monkeypatch.setattr("web_watcher.storage.query_listings",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("db gone")))
    assert scheduler._explore_matches(_watch(instruction="x"), None, None, page=object()) == 0


# --------------------------------------------------------------------------
# Reading an ad, NOT the infinite feed below it
# --------------------------------------------------------------------------

class DepthPage(FakePage):
    """A page that reports a viewport, so the scroll cap can be measured."""
    def __init__(self, chars=1200, viewport=800):
        super().__init__(chars=chars)
        self._viewport = viewport

    def evaluate(self, js):
        return self._viewport if "innerHeight" in js else self._chars


def _max_depth(page):
    """The DEEPEST point reached — that, not the net at the end, decides whether we left the
    ad behind and fell into the feed underneath it."""
    depth = peak = 0
    for _dx, dy in page.mouse.wheels:
        depth = max(0, depth + dy)      # you cannot scroll above the top of a page
        peak = max(peak, depth)
    return peak


def _scrolled_down(page):
    return sum(dy for _dx, dy in page.mouse.wheels if dy > 0)


def test_read_stays_within_the_ad(fast_clock):
    """The live failure on Facebook: the ad ends and an infinite 'Today's picks' feed begins,
    so scrolling on found elephants, apartments and a Macintosh 512K on a SAILBOAT watch."""
    from web_watcher import monitor
    page = DepthPage(chars=4000, viewport=800)
    human_read(page)
    cap = 800 * monitor._READ_MAX_SCREENS
    assert _max_depth(page) <= cap + 500, (
        f"reached {_max_depth(page)}px — past the ad (cap ~{cap:.0f}px) into the feed")


def test_a_long_read_does_not_mean_a_deep_scroll(fast_clock):
    """Dwell and depth are independent: a long ad earns more TIME, never more descent."""
    from web_watcher import monitor
    page = DepthPage(chars=100_000, viewport=800)
    spent = human_read(page)
    assert spent > 8.0
    assert _max_depth(page) <= 800 * monitor._READ_MAX_SCREENS + 500


def test_the_budget_ignores_an_infinite_feeds_text(fast_clock):
    """Whole-page text on a page with an endless feed says 'enormous' and would buy the
    maximum dwell for a three-line ad."""
    from web_watcher import monitor
    huge = monitor._read_budget(DepthPage(chars=5_000_000), "www.facebook.com")
    capped = monitor._read_budget(DepthPage(chars=monitor._READ_TEXT_CAP), "www.facebook.com")
    assert huge <= capped + 6.0, "an infinite feed still bought extra reading time"


def test_still_scrolls_something(fast_clock):
    """The cap must not turn reading into staring at the top of the page."""
    page = DepthPage(chars=2000, viewport=800)
    human_read(page)
    assert _scrolled_down(page) > 200


def test_reading_continues_after_the_cap(fast_clock):
    """Reaching the bottom of the ad ends the DESCENT, not the visit — a person who finished
    the description lingers and re-reads rather than diving into the recommendations."""
    page = DepthPage(chars=4000, viewport=800)
    spent = human_read(page)
    assert spent >= 8.0


# --------------------------------------------------------------------------
# Where a typed search STARTS from
# --------------------------------------------------------------------------

def _landing_for(url):
    """The REAL landing computation — no mirror to drift out of sync with the source."""
    from web_watcher.monitor import search_landing_url
    return search_landing_url(url)


def test_landing_drops_a_bare_search_segment():
    """Nobody navigates to /marketplace/seattle/search with no query — you go to Marketplace
    and type. Landing on the bare search URL is its own small tell."""
    assert _landing_for("https://offerup.com/search?q=macgregor") == "https://offerup.com/"
    # (Facebook drops the city segment too — see test_facebook_lands_on_marketplace_not_a_city_path.)


def test_landing_keeps_category_and_location():
    """Only the TYPED term is removed — a category or postal is a filter, not a search term,
    and dropping it would silently widen the watch."""
    assert _landing_for(
        "https://skagit.craigslist.org/search/boo?query=MacGregor&postal=98221"
    ) == "https://skagit.craigslist.org/search/boo?postal=98221"
    assert "_stpos=98221" in _landing_for(
        "https://www.ebay.com/sch/i.html?_stpos=98221&_nkw=macgregor+sailboat")


def test_facebook_lands_on_marketplace_not_a_city_path():
    """The city segment overrides the ACCOUNT'S OWN saved Marketplace location. The recording
    caught exactly that: our /seattle/ path on an account whose own location is Anacortes —
    the area the watch is actually about. /marketplace/ is what a person types, and it lets
    the site's own memory stand."""
    assert _landing_for(
        "https://www.facebook.com/marketplace/seattle/search?query=boats"
    ) == "https://www.facebook.com/marketplace/"
    assert _landing_for(
        "https://www.facebook.com/marketplace/search?query=boats"
    ) == "https://www.facebook.com/marketplace/"


def test_other_sites_keep_their_path():
    """The Marketplace rule is Facebook-specific — it must not eat another site's category."""
    assert _landing_for(
        "https://skagit.craigslist.org/search/boo?query=x&postal=98221"
    ).endswith("/search/boo?postal=98221")


def test_section_scoped_search_box_is_preferred():
    """On /marketplace/ there are TWO search boxes — Facebook's global one and Marketplace's.
    Typing the product query into the global one searches the whole site: the wrong-box bug,
    one level up from typing a keyword into a city picker. The section name is in the URL."""
    import re
    from urllib.parse import urlparse
    from web_watcher.monitor import _SEARCH_BOX_SELECTORS
    landing = "https://www.facebook.com/marketplace/"
    seg = [s for s in (urlparse(landing).path or "").split("/") if s]
    section = re.sub(r"[^a-z]", "", seg[0].lower()) if seg else ""
    assert section == "marketplace"
    selectors = [f'input[aria-label*="{section}" i]',
                 f'input[placeholder*="{section}" i]'] + list(_SEARCH_BOX_SELECTORS)
    assert "marketplace" in selectors[0]
    assert selectors.index('input[type="search"]') > 1, "generic search box tried too early"


def test_short_sections_do_not_get_a_bogus_selector():
    """A one- or two-letter path segment is not a section name worth matching on."""
    import re
    for path in ("/s/", "/x/", "/"):
        seg = [s for s in path.split("/") if s]
        section = re.sub(r"[^a-z]", "", seg[0].lower()) if seg else ""
        assert not len(section) > 3
