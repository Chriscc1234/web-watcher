# -*- coding: utf-8 -*-
"""The Scarcity Scout — the BOT looks wider when a watch runs thin, and asks the owner.

The admin's design, verbatim: "wouldn't this be the right time to ask him about updating
the distance or something? have we looked further? we should let him know if there is
anything in the area outside his search distance. don't do it yourself. the app/bot needs
to do it."
"""

from __future__ import annotations

import time
from types import SimpleNamespace as NS

import pytest

from web_watcher import scout
from web_watcher.monitor import Listing


def _watch(**over):
    base = dict(
        name="Sailrite Sewing Machine Watch", id="w1", owner="8991052415",
        keywords=["sailrite"],
        notify=NS(telegram=True, email=False),
        urls=["https://seattle.craigslist.org/search/sss?query=Sailrite+sewing+machine"
              "&max_price=1000&postal=98101&search_distance=150"])
    base.update(over)
    return NS(**base)


def test_widened_urls_go_national_and_wide():
    w = _watch()
    urls = scout.widened_urls(w)
    assert any("search_distance=1000" in u for u in urls)          # craigslist, wide
    assert any("ebay.com" in u and "_stpos" not in u for u in urls)  # national, no zip pin
    # A watch that already has a pinned eBay url gets it UNPINNED, not duplicated.
    w2 = _watch(urls=["https://www.ebay.com/sch/i.html?_nkw=sailrite&_stpos=98101&_sadis=200"])
    urls2 = scout.widened_urls(w2)
    assert urls2 and all("_stpos" not in u and "_sadis" not in u for u in urls2)


def test_thin_trigger_counts_alerts_heard_not_matches_recorded(monkeypatch):
    monkeypatch.setattr("web_watcher.storage.watch_stats",
                        lambda wid, name, db_path=None: {"runs": 10})
    monkeypatch.setattr("web_watcher.storage.alerted_count",
                        lambda wid, db_path=None: 1)
    assert scout._is_thin(_watch(), None) is True
    monkeypatch.setattr("web_watcher.storage.alerted_count",
                        lambda wid, db_path=None: 5)
    assert scout._is_thin(_watch(), None) is False
    monkeypatch.setattr("web_watcher.storage.watch_stats",
                        lambda wid, name, db_path=None: {"runs": 2})
    assert scout._is_thin(_watch(), None) is False                 # too young to judge


class _Page:
    def __init__(self):
        self.visited = []

    def goto(self, url, timeout=None, wait_until=None):
        self.visited.append(url)


def _arm(monkeypatch, tmp_path, found):
    monkeypatch.setattr(scout, "_notes_path", lambda: tmp_path / "scout_notes.json")
    monkeypatch.setattr(scout, "_is_thin", lambda w, db: True)
    monkeypatch.setattr("web_watcher.monitor.extract_listings", lambda page: found)
    monkeypatch.setattr("web_watcher.monitor.dismiss_popups",
                        lambda page, settle_ms=0: None)
    monkeypatch.setattr("web_watcher.storage.has_seen_listing",
                        lambda name, key, db: False)
    sent = []
    monkeypatch.setattr("web_watcher.notify.send_plain_telegram",
                        lambda msg, notif, chat_id_override=None:
                        sent.append((chat_id_override, msg)) or True)
    monkeypatch.setattr("web_watcher.notify._mirror_to_thread",
                        lambda chat, text, url="", image="": None)
    return sent


def test_scout_messages_the_owner_in_the_watchers_voice(monkeypatch, tmp_path):
    found = [Listing(key="e1", url="https://e/1",
                     title="Sailrite LSZ-1 walking foot machine", price="$850"),
             Listing(key="e2", url="https://e/2",
                     title="Sailrite Ultrafeed LS-1", price="$700"),
             Listing(key="e3", url="https://e/3",
                     title="Brother serger", price="$100")]        # noise — filtered out
    sent = _arm(monkeypatch, tmp_path, found)
    page = _Page()
    cfg = NS(notifications=NS(telegram=NS(chat_id="111")))
    assert scout.maybe_scout(_watch(), cfg, None, page) is True
    chat, msg = sent[0]
    assert chat == "8991052415"                                    # the OWNER, not the admin
    assert "wider look" in msg and "2" in msg
    assert "Sailrite LSZ-1" in msg and "Brother serger" not in msg
    assert "widen the Sailrite Sewing Machine Watch" in msg        # the exact reply words
    assert page.visited                                            # it really probed


def test_scout_cooldown_and_data_only_watches(monkeypatch, tmp_path):
    sent = _arm(monkeypatch, tmp_path, [Listing(key="e1", url="https://e/1",
                                                title="Sailrite LSZ-1", price="$850")])
    cfg = NS(notifications=NS(telegram=NS(chat_id="111")))
    w = _watch()
    assert scout.maybe_scout(w, cfg, None, _Page()) is True
    assert scout.maybe_scout(w, cfg, None, _Page()) is False       # 3-day cooldown holds
    assert len(sent) == 1
    silent = _watch(notify=NS(telegram=False, email=False))
    assert scout.maybe_scout(silent, cfg, None, _Page()) is False  # data-only: never nags


def test_suggestions_match_the_watch_never_a_noop(monkeypatch, tmp_path):
    """"is ebay already added?" — the offer line derives from the watch's own urls: no
    "add ebay" for a watch that already searches eBay; no "widen to N miles" for a watch
    with no distance-bearing url to widen."""
    hit = [Listing(key="e1", url="https://e/1", title="Sailrite LSZ-1", price="$850")]
    # Charlie-shaped: craigslist distance + NO ebay → both offers are real changes.
    sent = _arm(monkeypatch, tmp_path, hit)
    cfg = NS(notifications=NS(telegram=NS(chat_id="111")))
    scout.maybe_scout(_watch(), cfg, None, _Page())
    msg = sent[0][1]
    assert "add ebay" in msg and "widen the" in msg

    # Fiat-shaped: eBay already pinned → "add ebay" must NOT be offered.
    sent2 = _arm(monkeypatch, tmp_path, hit)
    w2 = _watch(name="Fiat X19 Cars Watch",
                keywords=["sailrite"],
                urls=["https://www.ebay.com/sch/i.html?_nkw=sailrite&_stpos=98101&_sadis=200"])
    scout.maybe_scout(w2, cfg, None, _Page())
    msg2 = sent2[0][1]
    assert "add ebay" not in msg2 and "widen the Fiat X19 Cars Watch" in msg2

    # FB/OU-only: nothing url-widenable → the generic broaden ask, never a fake offer.
    sent3 = _arm(monkeypatch, tmp_path, hit)
    w3 = _watch(name="FB Only Watch",
                urls=["https://www.facebook.com/marketplace/search?query=sailrite"])
    scout.maybe_scout(w3, cfg, None, _Page())
    msg3 = sent3[0][1]
    assert "widen the FB Only Watch to 500" not in msg3
    assert "add ebay" in msg3          # no ebay url → that offer IS real


def test_dry_sweeps_reach_the_scout():
    """The dry sweep is the scout's whole reason to exist — and the no-listings early
    return sat ABOVE the hook, so watches finding nothing never got the wider look.
    Source-level pin: every no-listings return is preceded by the scout call."""
    src = open("web_watcher/scheduler.py", encoding="utf-8").read()
    import re
    blocks = src.split("no listings found")
    assert len(blocks) >= 2
    for blk in blocks[1:]:
        head = blk[:900]
        assert "maybe_scout" in head, "a no-listings return skips the scout"


def test_a_motor_kit_is_not_a_machine(monkeypatch, tmp_path):
    """The FIRST live scout note (2026-08-30 22:20) offered "Sailrite Sewing Machine LSZ
    Motor, Foot Control, Balance Wheel - $95" as its example find. That is a PARTS
    listing; a person scanning wider says "only parts out there". "updates need to be
    smart and not just jibberish for users"."""
    motor = Listing(key="p1", url="https://e/p1",
                    title="Sailrite Sewing Machine LSZ Motor, Foot Control, Balance Wheel",
                    price="$95")
    machine = Listing(key="m1", url="https://e/m1",
                      title="Sailrite Ultrafeed LSZ-1 walking foot machine", price="$850")
    cfg = NS(notifications=NS(telegram=NS(chat_id="111")))

    # Mixed probe: the machine is the example; the motor never headlines the message.
    sent = _arm(monkeypatch, tmp_path, [machine, motor])
    assert scout.maybe_scout(_watch(), cfg, None, _Page()) is True
    msg = sent[0][1]
    assert "Ultrafeed" in msg and "Motor, Foot Control" not in msg

    # Parts-only probe: still a message — but an HONEST one about scarcity, not a "find".
    # (fresh notes dir — the mixed probe above just armed this watch's 3-day cooldown)
    sent2 = _arm(monkeypatch, tmp_path / "again", [motor])
    (tmp_path / "again").mkdir()
    assert scout.maybe_scout(_watch(), cfg, None, _Page()) is True
    chat2, msg2 = sent2[0]
    assert chat2 == "8991052415"
    assert "parts and accessories" in msg2
    assert "found 1 beyond it" not in msg2          # never counts a motor as a machine
