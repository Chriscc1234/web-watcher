"""Tests for the in-memory activity ring buffer that backs the Live tab (#93)."""

from __future__ import annotations

import logging

from web_watcher.logbuffer import LogRing, RingHandler, _categorize, _watch_of


def _add(ring, msg, level=logging.INFO, logger="web_watcher.scheduler"):
    ring.add(level, logging.getLevelName(level), logger, msg)


def test_categorize_rules():
    assert _categorize(logging.INFO, "web_watcher.scheduler", "Continuous sweep 3 for 'Trucks'") == "search"
    assert _categorize(logging.INFO, "web_watcher.scheduler", "Rating judge kept 2/5 for 'Trucks'") == "ai"
    assert _categorize(logging.INFO, "web_watcher.scheduler", "3 alerted for 'Trucks'") == "alert"
    assert _categorize(logging.INFO, "web_watcher.scheduler", "excluded by keyword 'parts'") == "skipped"
    assert _categorize(logging.INFO, "web_watcher.scheduler", "hit a login wall — skipping") == "login"
    assert _categorize(logging.ERROR, "web_watcher.scheduler", "anything at all") == "error"
    # A WARNING that reads like a failure is surfaced as an error
    assert _categorize(logging.WARNING, "web_watcher.browser", "Could not save browser state") == "error"
    assert _categorize(logging.INFO, "web_watcher.services", "Starting services...") == "system"


def test_watch_extraction():
    assert _watch_of("Continuous sweep 3 for 'Diesel Trucks': harvested 40") == "Diesel Trucks"
    assert _watch_of("no quoted name here") == ""


def test_incremental_snapshot_by_seq():
    ring = LogRing(maxlen=100)
    _add(ring, "sweep A")
    _add(ring, "sweep B")
    snap = ring.snapshot(after=0)
    assert [e["message"] for e in snap["entries"]] == ["sweep A", "sweep B"]
    last = snap["last_seq"]
    _add(ring, "sweep C")
    snap2 = ring.snapshot(after=last)
    assert [e["message"] for e in snap2["entries"]] == ["sweep C"]   # only the new line


def test_category_and_text_filters():
    ring = LogRing(maxlen=100)
    _add(ring, "Continuous sweep for 'Trucks'")
    _add(ring, "Rating judge kept 1/3 for 'Trucks'")
    _add(ring, "3 alerted for 'Boats'")
    assert len(ring.snapshot(category="ai")["entries"]) == 1
    assert len(ring.snapshot(category="alert")["entries"]) == 1
    assert len(ring.snapshot(watch="trucks")["entries"]) == 2
    assert len(ring.snapshot(text="alerted")["entries"]) == 1


def test_ring_is_bounded():
    ring = LogRing(maxlen=10)
    for i in range(50):
        _add(ring, f"line {i}")
    entries = ring.snapshot(after=0)["entries"]
    assert len(entries) == 10
    assert entries[-1]["message"] == "line 49"   # newest retained


def test_handler_skips_noisy_loggers():
    ring = LogRing(maxlen=100)
    h = RingHandler(ring)
    rec = logging.LogRecord("httpx", logging.INFO, __file__, 1, "GET /api/tags 200", None, None)
    h.emit(rec)
    ok = logging.LogRecord("web_watcher.scheduler", logging.INFO, __file__, 1, "sweep done", None, None)
    h.emit(ok)
    msgs = [e["message"] for e in ring.snapshot()["entries"]]
    assert msgs == ["sweep done"]   # httpx dropped
