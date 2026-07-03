"""
Orchestrator — the single driver that services ALL active topics like one person.

This is the north-star consolidation made real: instead of one daemon thread + one
browser PER continuous watch (which doesn't scale and is N× inference all day), a
single orchestrator owns ONE browser and cycles through the user's active topics,
deciding what to look at next, sweeping it, and moving on — varying its order so it
behaves like a real shopper, not clockwork. It reuses the exact same sweep + dedup +
deep-read + judge + alert pipeline the per-watch engine uses; it only changes WHO
decides what gets looked at and WHEN.

The Watcher is its voice: every decision ("checking the truck watch next — longest
since I looked") is narrated via the oversight agent, so the orchestrator's reasoning
is visible in the dock and you can chat with it while it works.

Rollout (user decision): OPT-IN and COEXISTS. While the orchestrator runs it is the
single driver for continuous watches (the per-watch threads are stood down); turn it
off and the old per-watch Start/Stop works exactly as before. Schedule-mode watches are
never touched — they stay on APScheduler.

Attention policy (user decision): STALENESS + PRODUCTIVITY. Favor whichever topic has
gone longest unchecked, nudged by how often it actually finds things, plus a little
jitter so it's human-like — and never revisit a topic before its own idle floor. No LLM
call per cycle (keeps the one local GPU free for judging/chat).

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  Orchestrator.start/stop   ~L70    Thread lifecycle (one daemon, one browser)
  Orchestrator.status       ~L95    Snapshot for /api/orchestrator
  _loop                     ~L120   Cycle: pick → narrate → sweep → record → idle
  _active_topics            ~L185   Enabled continuous watches = the active topics
  _pick_next                ~L195   Staleness + productivity + jitter, with idle floor
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Productivity nudge: each matched listing a topic has found adds this many "seconds of
# staleness" to its score, so productive topics get checked a little more often.
_PRODUCTIVITY_WEIGHT = 8.0
# Human-like randomness added to every topic's score so the order isn't deterministic.
_SCORE_JITTER = 12.0
# Pause between cycles (a different topic each time) — small; sweep duration dominates.
_CYCLE_IDLE = 8


class Orchestrator:
    """One driver servicing all active topics. Owned by ServiceManager, opt-in."""

    def __init__(self, scheduler, oversight=None,
                 config_path: Optional[Path] = None, db_path: Optional[Path] = None) -> None:
        self._scheduler = scheduler
        self._oversight = oversight
        self._config_path = config_path
        self._db_path = db_path

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        # Per-topic runtime state, keyed by stable watch id: when last serviced (monotonic),
        # how many times, and the running match total used for the productivity nudge.
        self._state: dict[str, dict] = {}
        self._cycles = 0
        self._current: Optional[str] = None     # name of the topic being serviced now
        self._started_at: Optional[float] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="orchestrator", daemon=True)
            self._started_at = time.monotonic()
            self._thread.start()
        log.info("Orchestrator started")
        return True

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=15.0)
        self._thread = None
        self._current = None
        log.info("Orchestrator stopped")

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict:
        return {
            "running": self.is_running(),
            "current": self._current,
            "cycles": self._cycles,
            "topics": [
                {"name": st.get("name"), "services": st.get("services", 0),
                 "matches": st.get("matches", 0)}
                for st in self._state.values()
            ],
        }

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        from web_watcher.scheduler import (
            _open_continuous_browser, _close_continuous_browser,
            _run_continuous_sweep, _run_agent_continuous_sweep, _save_error,
        )
        from web_watcher.config import load
        from web_watcher.storage import count_matches

        self._note("status", "I'm taking the wheel — I'll watch all your active watches "
                             "myself now, one at a time, and tell you what I'm doing.")

        session = None
        page = None
        persistent = None          # whether the open browser uses the login profile
        idle_announced = False
        if self._idle(3):
            return

        while not self._stop.is_set():
            try:
                cfg = load(self._config_path)
                topics = self._active_topics(cfg)
                if not topics:
                    if not idle_announced:
                        self._note("status", "No active watches to run right now. Add or enable "
                                            "a continuous watch and I'll start cycling through it.")
                        idle_announced = True
                    session, page = _close_continuous_browser(session), None
                    if self._idle(20):
                        break
                    continue
                idle_announced = False

                # (Re)open the shared browser. Use the login profile if ANY active topic needs
                # it (logged-out sites still work fine in a persistent-profile browser).
                want_persistent = any(t.use_login_profile for t in topics)
                if session is None or page is None or page.is_closed() or persistent != want_persistent:
                    rep = next((t for t in topics if t.use_login_profile), topics[0])
                    session, page = _open_continuous_browser(session, rep, cfg)
                    persistent = want_persistent

                topic = self._pick_next(topics)
                st = self._state.setdefault(topic.id or topic.name, {})

                # Respect each topic's idle floor: if even the stalest pick was serviced very
                # recently, everything is fresh — idle a beat rather than hammer a site.
                last = st.get("last_serviced")
                floor = max(1, topic.continuous_idle_seconds)
                if last is not None and (time.monotonic() - last) < floor:
                    if self._idle(min(floor, 15)):
                        break
                    continue

                self._note("decision", self._decision_line(topic, st), watch=topic.name)
                self._current = topic.name
                sweep_index = st.get("services", 0)

                try:
                    if topic.autonomous:
                        _run_agent_continuous_sweep(topic, cfg, self._db_path, sweep_index, page, self._stop)
                    else:
                        _run_continuous_sweep(topic, cfg, self._db_path, sweep_index, page, self._stop)
                except Exception as exc:
                    log.error("Orchestrator sweep error for %r: %s", topic.name, exc, exc_info=True)
                    _save_error(topic.name, datetime.now(timezone.utc).isoformat(),
                                f"orchestrator sweep: {exc}", self._db_path, perception_mode="orchestrator")
                    session, page = _close_continuous_browser(session), None  # browser may be the casualty

                # Record service + refresh the productivity signal.
                try:
                    st["matches"] = count_matches(topic.id or topic.name, self._db_path)
                except Exception:
                    pass
                st["name"] = topic.name
                st["last_serviced"] = time.monotonic()
                st["services"] = st.get("services", 0) + 1
                self._current = None
                self._cycles += 1
            except Exception as exc:
                log.error("Orchestrator cycle error: %s", exc, exc_info=True)
                self._current = None

            if self._idle(_jitter(_CYCLE_IDLE)):
                break

        _close_continuous_browser(session)
        log.info("Orchestrator loop ended (%d cycles)", self._cycles)

    # ------------------------------------------------------------------
    # Topic selection
    # ------------------------------------------------------------------

    def _active_topics(self, cfg) -> list:
        """The active topics = enabled continuous-mode watches. Schedule-mode watches are
        left to APScheduler and never touched here."""
        return [w for w in cfg.watches if w.enabled and w.mode == "continuous"]

    def _pick_next(self, topics: list):
        """Staleness + productivity + jitter. Never-serviced topics score highest (each
        gets an early first look); then the stalest, nudged by how productive it's been."""
        now = time.monotonic()
        best, best_score = topics[0], -1.0
        for t in topics:
            st = self._state.get(t.id or t.name, {})
            last = st.get("last_serviced")
            staleness = 1e9 if last is None else (now - last)
            matches = st.get("matches", 0)
            score = staleness + matches * _PRODUCTIVITY_WEIGHT + random.uniform(0, _SCORE_JITTER)
            if score > best_score:
                best, best_score = t, score
        return best

    def _decision_line(self, topic, st: dict) -> str:
        services = st.get("services", 0)
        matches = st.get("matches", 0)
        if services == 0:
            return f"Starting a first look at '{topic.name}'."
        if matches:
            return random.choice([
                f"Checking '{topic.name}' next — it's been finding things ({matches} so far).",
                f"Back to '{topic.name}' — it's one of the productive ones.",
            ])
        return random.choice([
            f"Checking '{topic.name}' next — longest since I looked.",
            f"Swinging back to '{topic.name}' to see what's new.",
        ])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _note(self, kind: str, text: str, watch: Optional[str] = None) -> None:
        if self._oversight is not None:
            try:
                self._oversight.note(kind, text, watch=watch)
            except Exception:
                pass

    def _idle(self, seconds: float) -> bool:
        """Interruptible sleep; returns True if we should stop."""
        self._stop.wait(timeout=max(0.1, seconds))
        return self._stop.is_set()


def _jitter(base: int) -> float:
    return max(1.0, base + random.uniform(-0.3 * base, 0.6 * base))
