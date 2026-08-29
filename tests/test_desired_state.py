"""The desired-state record: which continuous watches the user WANTS running.

This file exists because of a 13-hour outage. At launch both watches resumed correctly, then
the orchestrator took the wheel and called stop_all_continuous() to stand the per-watch threads
down — and that teardown wrote an EMPTY desired set. The rotation, which filters by that set,
was therefore empty; nothing swept all night; and the dashboard reported both watches "running"
the whole time because it derived that answer itself instead of asking the engine.

Two rules are pinned here:
  1. Only an EXPLICIT start/stop may change the record. Teardown must not.
  2. "Is it running?" is answered by the rotation, never re-derived from enabled+continuous.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from web_watcher.orchestrator import Orchestrator


def _sched(tmp_path):
    """A real scheduler instance with its state file redirected into tmp."""
    from web_watcher.scheduler import WatchScheduler
    s = WatchScheduler.__new__(WatchScheduler)          # no APScheduler, no config load
    s._continuous_threads = {}
    s._stop_events = {}
    s._lock = threading.Lock()
    s._running_state_path = lambda: tmp_path / "continuous_running.json"
    return s


# ── rule 1: teardown must not rewrite what the user asked for ────────────────────

def test_explicit_stop_records_intent(tmp_path):
    s = _sched(tmp_path)
    s._remember_running("A", True)
    s._remember_running("B", True)
    assert s._remembered_running() == {"A", "B"}
    s._remember_running("A", False)
    assert s._remembered_running() == {"B"}


def test_handover_teardown_leaves_the_record_alone(tmp_path):
    """THE REGRESSION: orchestrator handover stopped the per-watch threads, and each stop
    erased that watch from the desired set. Next restart resumed nothing and the rotation
    filtered down to zero."""
    s = _sched(tmp_path)
    s._remember_running("A", True)
    s._remember_running("B", True)

    # Two live "threads" for stop_all_continuous to stand down.
    for name in ("A", "B"):
        ev = threading.Event()
        th = threading.Thread(target=ev.wait, daemon=True)
        th.start()
        s._stop_events[name], s._continuous_threads[name] = ev, th

    s.stop_all_continuous()

    assert s._continuous_threads == {}                 # threads really stopped
    assert s._remembered_running() == {"A", "B"}       # ...and the intent survived


def test_a_real_stop_still_records(tmp_path):
    """The other half of the flag: a stop the USER asked for must still be written down, or
    a restart would resume a watch they turned off."""
    s = _sched(tmp_path)
    s._remember_running("A", True)
    ev = threading.Event()
    th = threading.Thread(target=ev.wait, daemon=True)
    th.start()
    s._stop_events["A"], s._continuous_threads["A"] = ev, th

    assert s.stop_continuous("A") is True
    assert s._remembered_running() == set()


def test_missing_file_is_not_an_empty_set(tmp_path):
    """None (never recorded) means legacy-resume-everything; an empty set means the user
    stopped everything. Collapsing the two silently stops a whole install."""
    s = _sched(tmp_path)
    assert s._remembered_running() is None
    s._remember_running("A", True)
    s._remember_running("A", False)
    assert s._remembered_running() == set()


# ── rule 2: running-ness comes from the rotation ─────────────────────────────────

def _cfg(*names, enabled=True):
    return SimpleNamespace(watches=[
        SimpleNamespace(name=n, enabled=enabled, mode="continuous", id=n) for n in names])


def _orch_with(tmp_path, desired):
    s = _sched(tmp_path)
    for n in desired:
        s._remember_running(n, True)
    o = Orchestrator(scheduler=s, oversight=None)
    o._thread = SimpleNamespace(is_alive=lambda: True)   # "running" without a real loop
    o._stop = threading.Event()
    return o


def test_is_servicing_follows_the_rotation(tmp_path):
    o = _orch_with(tmp_path, ["A"])
    cfg = _cfg("A", "B")
    assert o.is_servicing("A", cfg) is True
    # B is enabled and continuous — the OLD inference would have called it running.
    assert o.is_servicing("B", cfg) is False


def test_is_servicing_is_false_when_the_orchestrator_is_down(tmp_path):
    o = _orch_with(tmp_path, ["A"])
    o._thread = None
    assert o.is_servicing("A", _cfg("A")) is False


def test_empty_desired_set_means_nothing_is_serviced(tmp_path):
    o = _orch_with(tmp_path, [])
    o._scheduler._remember_running("A", True)
    o._scheduler._remember_running("A", False)          # user stopped the only watch
    assert o._active_topics(_cfg("A")) == []
    assert o.is_servicing("A", _cfg("A")) is False


def test_no_record_at_all_falls_back_to_every_enabled_watch(tmp_path):
    s = _sched(tmp_path)                                # nothing ever recorded
    o = Orchestrator(scheduler=s, oversight=None)
    o._thread = SimpleNamespace(is_alive=lambda: True)
    o._stop = threading.Event()
    assert [w.name for w in o._active_topics(_cfg("A", "B"))] == ["A", "B"]


# ── one watch, one engine ────────────────────────────────────────────────────────

def test_reload_under_the_orchestrator_starts_no_per_watch_loop(tmp_path, monkeypatch):
    """Found live, minutes after the desired-state record started working again: a single
    watch edit triggers a config reload, _load_jobs saw the watch in the desired set and
    started a per-watch loop — while the orchestrator was already sweeping it. A second
    visible Chrome opened and hit the same site alongside the first."""
    from web_watcher.scheduler import WatchScheduler
    from web_watcher.config import AppConfig, Watch

    s = _sched(tmp_path)
    s._remember_running("A", True)
    s._explored_domains = set()
    s._config_path = None
    started = []
    monkeypatch.setattr(WatchScheduler, "start_continuous",
                        lambda self, name: started.append(name))
    cfg = AppConfig(watches=[Watch(name="A", urls=["https://x"], instruction="x",
                                   interval_minutes=30, mode="continuous")])
    monkeypatch.setattr("web_watcher.scheduler.load_config", lambda p=None: cfg)

    s._orchestrator_owns = True
    s._load_jobs()
    assert started == []                    # the driver keeps it

    s._orchestrator_owns = False
    s._load_jobs()
    assert started == ["A"]                 # ...and without a driver, it resumes as before
