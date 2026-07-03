"""
Tests for the orchestrator's attention policy — the staleness + productivity scoring
that decides which topic to service next. Pure/offline: drive _pick_next directly with
synthetic topics and state (no threads/browser/Ollama). Jitter (≤12s) is dominated by
the staleness/productivity gaps chosen here, so picks are deterministic.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from web_watcher.orchestrator import Orchestrator, _PRODUCTIVITY_WEIGHT


def _orch() -> Orchestrator:
    return Orchestrator(scheduler=None, oversight=None)


def _topic(tid, name):
    return SimpleNamespace(id=tid, name=name)


def test_never_serviced_topic_is_picked_first():
    o = _orch()
    a, b = _topic("a", "A"), _topic("b", "B")
    o._state["a"] = {"last_serviced": time.monotonic(), "matches": 0}   # A just serviced
    # B has never been serviced (staleness ~1e9) → always chosen until it has been.
    picks = {o._pick_next([a, b]).name for _ in range(8)}
    assert picks == {"B"}


def test_stalest_topic_wins_among_serviced():
    o = _orch()
    a, b = _topic("a", "A"), _topic("b", "B")
    now = time.monotonic()
    o._state["a"] = {"last_serviced": now - 100, "matches": 0}   # stale
    o._state["b"] = {"last_serviced": now - 1,   "matches": 0}   # fresh
    picks = {o._pick_next([a, b]).name for _ in range(25)}
    assert picks == {"A"}


def test_productivity_nudges_the_pick():
    o = _orch()
    a, b = _topic("a", "A"), _topic("b", "B")
    now = time.monotonic()
    # Equal staleness; B has found a lot → its productivity bonus dominates the jitter.
    o._state["a"] = {"last_serviced": now - 30, "matches": 0}
    o._state["b"] = {"last_serviced": now - 30, "matches": 50}   # +50*weight
    assert 50 * _PRODUCTIVITY_WEIGHT > 30          # sanity: bonus outweighs the staleness gap
    picks = {o._pick_next([a, b]).name for _ in range(25)}
    assert picks == {"B"}


def test_status_shape_when_idle():
    o = _orch()
    s = o.status()
    assert s["running"] is False and s["cycles"] == 0 and s["topics"] == []
