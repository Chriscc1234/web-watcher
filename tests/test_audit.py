# -*- coding: utf-8 -*-
"""The Watch Auditor — every seed check is a bug that was found by hand first.

The admin's ask, verbatim: "is there an agent that reviews the watches and looks at issues
like this? takes a long time is ok... like it only sent one watch notification, it never
delivered the initial search listings etc."
"""

from __future__ import annotations

import json

import pytest

from web_watcher import audit


def _ev(**over):
    base = {
        "name": "T", "enabled": True, "mode": "continuous", "owner": "",
        "instruction": "", "judgment_prompt": "",
        "urls": ["https://x?q=a"], "url_hosts": ["craigslist.org"],
        "url_terms": ["Sailrite sewing machine"], "keywords": [], "min_rating": None,
        "stats": {"observations": 10, "matches": 2, "runs": 5},
        "seen_count": 10, "unalerted": [], "briefed": True, "desired_running": True,
    }
    base.update(over)
    return base


def _kinds(ev):
    return {f["kind"] for f in audit.deterministic_findings(ev)}


def test_healthy_watch_has_no_findings():
    assert audit.deterministic_findings(_ev()) == []


def test_named_site_gap_is_flagged():
    """Charlie's create: three sites in the instruction, one site in the urls."""
    ev = _ev(instruction="watch craigslist, offerup and facebook for sewing machines")
    kinds = _kinds(ev)
    assert "site_coverage" in kinds


def test_junk_rotation_is_flagged():
    ev = _ev(url_terms=["Sailrite sewing machine", "Seattle Sailrite",
                        "affordable Sailrite"])
    assert "junk_rotation" in _kinds(ev)


def test_unalerted_matches_are_flagged():
    """The 15-MacGregor hole: found, recorded, never pushed to anyone."""
    ev = _ev(unalerted=[{"title": "MacGregor 26", "rating": 4}])
    assert "unalerted_matches" in _kinds(ev)


def test_dead_and_dry_watches_are_flagged():
    assert "never_ran" in _kinds(_ev(stats={"observations": 0, "matches": 0, "runs": 0}))
    assert "dry_watch" in _kinds(_ev(stats={"observations": 80, "matches": 0, "runs": 9}))


def test_missing_briefing_is_flagged():
    """"it never delivered the initial search listings" — primed but nobody was told."""
    assert "no_briefing" in _kinds(_ev(briefed=False, seen_count=40))


def test_run_audit_persists_and_latest_reads_back(monkeypatch, tmp_path):
    from types import SimpleNamespace
    monkeypatch.setattr(audit, "_dir", lambda: tmp_path)
    monkeypatch.setattr(audit, "gather_evidence",
                        lambda w, db: _ev(name=w.name, briefed=False, seen_count=5))
    cfg = SimpleNamespace(watches=[SimpleNamespace(name="A")])
    report = audit.run_audit(cfg, use_llm=False)
    assert report["watches"] == 1
    assert any(f["kind"] == "no_briefing" for f in report["findings"])
    assert audit.latest()["findings"] == report["findings"]


# ── the alert boundary: nothing pings a phone without the confirm (when possible) ─

def test_alert_boundary_blocks_the_golf_club(monkeypatch, tmp_path):
    """Live: a $25 "Macgregor Great-Scot MP-8" — a GOLF CLUB — alerted as a four-star
    sailboat. The cloud confirm at the alert boundary is the automatic, no-toggle answer."""
    import web_watcher.scheduler as sch
    from types import SimpleNamespace

    watch = SimpleNamespace(name="W", id="w1", judgment_prompt="MacGregor sailboats only",
                            instruction="", continuous_max_alerts=5,
                            notify=SimpleNamespace(telegram=True, email=False),
                            owner="", urls=["https://x"])
    club = sch.Listing(key="fb:1", url="https://x/1",
                       title="Macgregor Great-Scot MP-8", price="$25")
    club.rating = 4
    boat = sch.Listing(key="fb:2", url="https://x/2",
                       title="1997 MacGregor 26X", price="$14,000")
    boat.rating = 4

    monkeypatch.setattr(sch, "_cloud_confirm_match",
                        lambda w, l, cfg: (False, "a golf club, not a sailboat")
                        if "MP-8" in l.title else (True, "a real MacGregor sailboat"))
    sent, seen, demoted = [], [], []
    monkeypatch.setattr(sch, "send_notifications", lambda payload, *a, **k:
                        sent.append(payload.result.summary))
    monkeypatch.setattr(sch, "save_seen_listing",
                        lambda name, key, ts, **k: seen.append(key))
    monkeypatch.setattr(sch, "record_observation",
                        lambda *a, **k: demoted.append(k.get("judge_reason", "")))
    monkeypatch.setattr(sch.time, "sleep", lambda s: None)
    from types import SimpleNamespace as NS
    cfg = NS(notifications=NS())

    n = sch._alert_new_listings(watch, cfg, [club, boat], "2026-08-30T20:00:00", tmp_path)
    assert n == 1
    assert sent and "MacGregor 26X" in sent[0]            # the boat went out
    assert not any("MP-8" in s for s in sent)             # the club never did
    assert "fb:1" in seen                                 # ...and won't come back
    assert any("golf club" in d for d in demoted)


def test_alert_boundary_fails_open(monkeypatch, tmp_path):
    """No cloud, no blocking: an alert must never be lost to a cloud hiccup."""
    import web_watcher.scheduler as sch
    from types import SimpleNamespace as NS
    watch = NS(name="W", id="w1", judgment_prompt="x", instruction="",
               continuous_max_alerts=5, notify=NS(telegram=True, email=False),
               owner="", urls=["https://x"])
    l = sch.Listing(key="k", url="https://x/1", title="thing", price="$5")
    monkeypatch.setattr(sch, "_cloud_confirm_match", lambda w, li, cfg: None)
    sent = []
    monkeypatch.setattr(sch, "send_notifications",
                        lambda payload, *a, **k: sent.append(1))
    monkeypatch.setattr(sch, "save_seen_listing", lambda *a, **k: None)
    monkeypatch.setattr(sch.time, "sleep", lambda s: None)
    assert sch._alert_new_listings(watch, NS(notifications=NS()), [l],
                                   "2026-08-30T20:00:00", tmp_path) == 1
    assert sent


def test_origin_budget_missing_is_flagged():
    """"will the auditor also look back at the actual chat log?" — yes: each watch's
    origin chat rides in the evidence, and a budget the user stated that never became a
    cap is a deterministic finding."""
    ev = _ev(origin_chat=["Watch craigslist for a sailrite sewing machine under $1000"],
             instruction="Look for Sailrite sewing machines near Seattle",
             urls=["https://x?q=sailrite"])
    kinds = _kinds(ev)
    assert "origin_budget_missing" in kinds


def test_origin_budget_present_is_quiet():
    ev = _ev(origin_chat=["under $1000 please"],
             instruction="Sailrite sewing machines under $1000",
             urls=["https://x?q=sailrite&max_price=1000"])
    assert "origin_budget_missing" not in _kinds(ev)
