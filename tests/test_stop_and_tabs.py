# -*- coding: utf-8 -*-
"""v0.204 — one evening's findings, each pinned:

  * deep-read tabs flashed 'about:blank' -> listings open by a real Ctrl+click from the
    results page; the fallback loads behind the results page.
  * a stop mid-sweep still sent Jordan an alert 20s later -> matches are HELD for restart.
  * the narrator said "'Sailrite' is running now" while the rotation held it stopped.
  * "stop all watches" -> "Stopping all 7 of your watches." (five already off, two not his).
"""

from __future__ import annotations

import threading
from types import SimpleNamespace as NS

import web_watcher.scheduler as sch


# ── opening a listing ─────────────────────────────────────────────────────────────────

class _Anchor:
    def __init__(self, path):
        self.path = path

    def evaluate(self, js):
        return self.path

    def is_visible(self):
        return True


class _Locator:
    def __init__(self, anchors):
        self.anchors = anchors

    def count(self):
        return len(self.anchors)

    def nth(self, i):
        return self.anchors[i]


class _Tab:
    def __init__(self, href="https://skagit.craigslist.org/boa/d/macgregor/7712.html"):
        self.href, self.url = href, href
        self.fronted = self.closed = False
        self.gotos = []

    def evaluate(self, js):
        return [self.href, "complete"]

    def wait_for_timeout(self, ms):
        pass

    def bring_to_front(self):
        self.fronted = True

    def goto(self, url, **kw):
        self.gotos.append((url, kw.get("referer")))

    def close(self):
        self.closed = True


class _Ctx:
    def __init__(self, results):
        self.pages = [results]
        self.new_pages = []

    def new_page(self):
        t = _Tab()
        self.new_pages.append(t)
        self.pages.append(t)
        return t


class _Results:
    url = "https://www.craigslist.org/search/city/anacortes-wa?query=MacGregor"

    def __init__(self, anchors):
        self._anchors = anchors
        self.fronted = 0
        self.context = None

    def locator(self, sel):
        return _Locator(self._anchors)

    def wait_for_timeout(self, ms):
        pass

    def bring_to_front(self):
        self.fronted += 1


def test_a_listing_on_the_results_page_is_opened_by_ctrl_click(monkeypatch):
    from web_watcher import humanize
    results = _Results([_Anchor("/boa/d/macgregor/7712.html")])
    ctx = _Ctx(results)
    clicks = []

    def fake_click(page, loc, timeout=5000, modifiers=None):
        clicks.append(modifiers)
        ctx.pages.append(_Tab())          # the browser opens the background tab
        return True
    monkeypatch.setattr(humanize, "click", fake_click)

    tab = sch._open_listing_tab(results, ctx,
                                "https://skagit.craigslist.org/boa/d/macgregor/7712.html")
    assert clicks == [["Control"]]
    assert ctx.new_pages == []            # never an empty tab pointed somewhere afterwards
    assert tab is ctx.pages[-1] and tab.fronted


def test_link_match_is_exact_not_a_prefix():
    """"/itm/123" must not grab the "/itm/1234567" card and open the wrong ad."""
    results = _Results([_Anchor("/itm/1234567")])
    assert sch._listing_link(results, "https://www.ebay.com/itm/123456") is None
    results = _Results([_Anchor("/itm/1234567"), _Anchor("/itm/123456/")])
    assert sch._listing_link(results, "https://www.ebay.com/itm/123456") is results._anchors[1]


def test_fallback_loads_behind_the_results_page(monkeypatch):
    results = _Results([])                # paginated away: no card to click
    ctx = _Ctx(results)
    tab = sch._open_listing_tab(results, ctx, "https://x.example/item/99999")
    assert ctx.new_pages == [tab]
    assert results.fronted == 1           # results kept in front while the ad loaded
    assert tab.gotos == [("https://x.example/item/99999", results.url)]   # referer kept
    assert tab.fronted                    # then switched to the loaded ad


def test_tab_is_not_shown_until_the_listing_itself_loaded():
    """Measured live: a Ctrl+click tab sits on about:blank (tab.url '') before navigating,
    and Playwright calls that 'loaded'. Switching then showed an 'Untitled' blank page."""
    blank = _Tab(href="about:blank")
    blank.url = ""
    stop = threading.Event()
    calls = {"n": 0}

    def evaluate(js):
        calls["n"] += 1
        if calls["n"] >= 3:               # the ad arrives on the third look
            blank.href = blank.url = "https://x.example/item/5"
        return [blank.href, "complete"]
    blank.evaluate = evaluate
    assert sch._wait_for_listing_document(blank, stop) is True
    assert calls["n"] >= 3
    stop.set()
    assert sch._wait_for_listing_document(_Tab(href="about:blank"), stop) is False


def test_a_late_ctrl_click_tab_is_never_left_open(monkeypatch):
    results = _Results([])
    ctx = _Ctx(results)
    results.context = ctx
    late = _Tab()

    def open_tab(page, c, url, stop_event=None):
        c.pages.append(late)              # the click's tab turned up after its wait...
        return None                       # ...and the read gave up
    monkeypatch.setattr(sch, "_open_listing_tab", open_tab)
    l = NS(url="https://x.example/item/1", title="t", key="k", details="", posted_at=None)
    sch._capture_listing_bodies(results, [l], stop_event=None)
    assert late.closed


# ── stop means nothing more goes out ────────────────────────────────────────────────────

def test_stop_mid_sweep_holds_matches_for_restart(monkeypatch, tmp_path):
    watch = NS(name="MacGregor", id="w1", judgment_prompt="", instruction="boats",
               continuous_max_alerts=5, notify=NS(telegram=True, email=False),
               owner="8708818228", shared_with=[], urls=["https://x"])
    read = sch.Listing(key="cl:1", url="https://x/1", title="MacGregor 26X", price="$9k")
    read.details = "1999 MacGregor 26X, trailer included"
    unread = sch.Listing(key="cl:2", url="https://x/2", title="MacGregor 22", price="$4k")
    sent, seen = [], []
    monkeypatch.setattr(sch, "send_notifications", lambda *a, **k: sent.append(1))
    monkeypatch.setattr(sch, "save_seen_listing", lambda name, key, ts, **k: seen.append(key))
    monkeypatch.setattr(sch, "_boundary_vet", lambda *a: None)
    monkeypatch.setattr(sch.time, "sleep", lambda s: None)
    stop = threading.Event()
    stop.set()
    n = sch._alert_new_listings(watch, NS(notifications=NS()), [read, unread],
                                "2026-11-01T20:25:00", tmp_path, stop_event=stop)
    assert n == 0 and sent == []          # the user said stop
    assert seen == ["cl:1"]               # read -> the drip delivers it on restart
    # the unread one stays unseen, so the next sweep re-reads and alerts it


def test_stop_between_alerts_holds_the_rest(monkeypatch, tmp_path):
    watch = NS(name="W", id="w1", judgment_prompt="", instruction="x",
               continuous_max_alerts=5, notify=NS(telegram=True, email=False),
               owner="", shared_with=[], urls=["https://x"])
    a = sch.Listing(key="a", url="https://x/a", title="a", price="")
    b = sch.Listing(key="b", url="https://x/b", title="b", price="")
    b.details = "body"
    stop = threading.Event()
    sent = []
    monkeypatch.setattr(sch, "send_notifications",
                        lambda payload, *x, **k: sent.append(payload) or stop.set())
    monkeypatch.setattr(sch, "save_seen_listing", lambda *x, **k: None)
    monkeypatch.setattr(sch, "_boundary_vet", lambda *x: None)
    monkeypatch.setattr(sch.time, "sleep", lambda s: None)
    assert sch._alert_new_listings(watch, NS(notifications=NS()), [a, b],
                                   "2026-11-01T20:25:00", tmp_path, stop_event=stop) == 1
    assert len(sent) == 1                 # the stop landed after the first: b was held


# ── the narrator asks the engine ────────────────────────────────────────────────────────

def test_narrator_does_not_call_a_held_watch_running(monkeypatch):
    from web_watcher.config import AppConfig, Watch
    from web_watcher.oversight import OversightAgent
    cfg = AppConfig(watches=[
        Watch(name="Sailrite", urls=["https://x"], instruction="x", interval_minutes=30,
              mode="continuous", enabled=True),
        Watch(name="MacGregor", urls=["https://y"], instruction="y", interval_minutes=30,
              mode="continuous", enabled=True)])

    class _Mgr:
        def get_job_info(self):
            return []

        def orchestrator_running(self):
            return True                   # the old rule: orchestrating => everything runs

        def watch_runtime(self, name, cfg=None):
            return {"running": name == "MacGregor"}

    monkeypatch.setattr("web_watcher.config.load", lambda path=None: cfg)
    monkeypatch.setattr("web_watcher.storage.count_matches", lambda *a: 0)
    monkeypatch.setattr("web_watcher.storage.count_observations", lambda *a: 0)
    monkeypatch.setattr("web_watcher.storage.get_last_run", lambda *a: None)
    state = {w["name"]: w["running"] for w in OversightAgent(_Mgr())._gather_state()}
    assert state == {"Sailrite": False, "MacGregor": True}


# ── "stop all watches" says what it stops ───────────────────────────────────────────────

def test_stop_all_names_what_actually_stops(monkeypatch):
    from web_watcher.config import AppConfig, NotificationsConfig, TelegramConfig, Watch
    from web_watcher.dashboard import server as S

    def w(name, owner="", enabled=True):
        return Watch(name=name, urls=["https://x"], instruction="x", interval_minutes=30,
                     mode="continuous", enabled=enabled, owner=owner)
    cfg = AppConfig(
        notifications=NotificationsConfig(telegram=TelegramConfig(chat_id="111")),
        watches=[w("MacGregor", "222"), w("Sailrite", "333"), w("Miata", "111"),
                 w("Fiat", enabled=False)])
    monkeypatch.setattr("web_watcher.scheduler._remembered_running_set",
                        lambda: {"MacGregor", "Miata"})
    monkeypatch.setattr(S, "_load_owner_names", lambda: {"222": "Jordan", "333": "Charlie"})

    msg = S._bulk_lifecycle_message("stop", cfg.watches, cfg, "111")
    assert msg.startswith("Stopping ")
    assert "“MacGregor” (Jordan’s)" in msg and "“Miata”" in msg
    assert "Sailrite" not in msg and "Fiat" not in msg      # already stopped: not claimed
    assert "The other 2 were already stopped." in msg
    assert "your watches" not in msg

    monkeypatch.setattr("web_watcher.scheduler._remembered_running_set", lambda: set())
    assert S._bulk_lifecycle_message("stop", cfg.watches, cfg, "111") == \
        "Nothing to stop — none of the 4 watches is running."
    start = S._bulk_lifecycle_message("start", cfg.watches, cfg, "111")
    assert start.startswith("Starting ") and "(Charlie’s)" in start
