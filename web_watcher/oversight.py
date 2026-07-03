"""
Oversight agent — the visible "mind" that watches the watches.

This is the first slice of the oversight-agent vision: a background loop that
periodically looks over every watch, NOTICES what changed since last time (new
matches, a watch that started/stopped, a watch finding nothing, a run that
errored), and narrates it in the first person. The narration is surfaced live in
the Watches tab so the app feels like a little character is running the whole
thing and talking to itself — and it's the same brain that will later DRIVE the
watches (the orchestrator) instead of only commenting on them.

Design choices
--------------
- Delta-driven: each tick gathers state and diffs it against the previous tick,
  so the agent narrates *events* ("'Trucks' just found 2"), not a stream of the
  same status. This is exactly the signal an attention-allocating orchestrator
  needs, surfaced as narration first.
- Templated voice is the reliable backbone (instant, no inference). A focused LLM
  "voice" pass is used ONLY for the periodic check-in review, best-effort with a
  short timeout and a templated fallback — so the panel never stalls on the 14B
  and we stay honest about the local model's latency.
- Never crashes the thread: the whole tick is wrapped; one bad read just skips a
  beat. Idle is interruptible via the stop event.

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  OversightAgent.start/stop   ~L70    Thread lifecycle
  OversightAgent.snapshot     ~L95    Thread-safe view for /api/oversight
  _loop / _tick               ~L120   Periodic gather → diff → narrate
  _gather_state               ~L165   Per-watch matches/observations/running/error
  _narrate_deltas             ~L195   Templated event narration (finds/start/stop/concern)
  _emit_review                ~L255   Periodic check-in (LLM voice, templated fallback)
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import random
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434"

# How often the agent looks things over. Long enough to be calm (not chatty), short
# enough to feel alive. Jittered slightly so it isn't perfectly clockwork.
_TICK_SECONDS = 75
# A spoken check-in roughly every Nth tick (≈ every 12-15 min at the default tick).
_REVIEW_EVERY = 11
# Keep the narration feed bounded — the panel shows the most recent slice.
_MAX_ENTRIES = 60
# A watch is "finding nothing" once it has looked at this many listings with zero
# matches — below this it's just early, not a problem worth flagging.
_DRY_OBS_THRESHOLD = 25


class OversightAgent:
    """Background narrator over all watches. One per process, owned by ServiceManager."""

    def __init__(
        self,
        manager,
        config_path: Optional[Path] = None,
        db_path: Optional[Path] = None,
        tick_seconds: int = _TICK_SECONDS,
    ) -> None:
        self._manager = manager
        self._config_path = config_path
        self._db_path = db_path
        self._tick_seconds = tick_seconds

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # Set to wake the loop early (e.g. a watch just started/stopped) so The Watcher
        # reacts within a second or two instead of waiting out the full idle. Also set by
        # stop() so a shutdown interrupts the idle immediately.
        self._wake = threading.Event()
        self._lock = threading.Lock()

        self._entries: deque[dict] = deque(maxlen=_MAX_ENTRIES)
        self._prev: dict[str, dict] = {}        # watch name → last-seen counts
        self._running_prev: set[str] = set()
        self._dry_flagged: set[str] = set()      # watches we've already nudged about
        self._error_seen: dict[str, str] = {}    # watch name → last error narrated
        self._tick_count = 0
        self._started = False
        self._updated_at: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="oversight", daemon=True)
        self._thread.start()
        log.info("Oversight agent started")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()          # interrupt the idle immediately
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=5.0)
        self._thread = None
        log.info("Oversight agent stopped")

    def nudge(self) -> None:
        """Ask the loop to look NOW rather than waiting out its idle — call this right
        after a watch starts/stops so the narration reflects the change near-instantly."""
        self._wake.set()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def note(self, kind: str, text: str, watch: Optional[str] = None,
             action: Optional[dict] = None) -> None:
        """Inject a narration line from another component — the orchestrator uses this to
        make The Watcher voice its decisions ('checking the truck watch next…'). Thread-safe;
        the line shows up in the feed/dock on the next poll."""
        self._emit(kind, text, watch=watch, action=action)

    # ------------------------------------------------------------------
    # Public view (consumed by /api/oversight)
    # ------------------------------------------------------------------

    def snapshot(self, limit: int = 40) -> dict:
        """Newest-first narration feed plus the current per-watch state."""
        with self._lock:
            entries = list(self._entries)[-limit:]
            watches = self._current_watch_view()
        entries.reverse()  # newest first for the panel
        return {
            "running": self.is_running(),
            "updated_at": self._updated_at,
            "entries": entries,
            "watches": watches,
        }

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        # A short settle so the scheduler/Ollama are up before the first look.
        if self._idle(3):
            return
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # a bad read must never kill the narrator
                log.debug("Oversight tick failed: %s", exc, exc_info=True)
            # Light jitter so the agent isn't perfectly periodic.
            idle = self._tick_seconds + random.randint(-8, 8)
            if self._idle(max(15, idle)):
                break

    def _idle(self, seconds: float) -> bool:
        """Sleep up to `seconds`, but return early if stop() or nudge() fires. Returns
        True only if we should stop. A nudge (watch started/stopped) wakes us to tick now."""
        self._wake.wait(timeout=seconds)
        self._wake.clear()
        return self._stop.is_set()

    def _tick(self) -> None:
        state = self._gather_state()
        self._updated_at = datetime.now(timezone.utc).isoformat()

        if not self._started:
            self._emit_greeting(state)
            self._started = True
        else:
            self._narrate_deltas(state)

        self._tick_count += 1
        if self._tick_count % _REVIEW_EVERY == 0:
            self._emit_review(state)

        # Roll state forward for the next diff.
        self._prev = {w["name"]: w for w in state}
        self._running_prev = {w["name"] for w in state if w["running"]}

    # ------------------------------------------------------------------
    # State gathering
    # ------------------------------------------------------------------

    def _gather_state(self) -> list[dict]:
        from web_watcher.config import load
        from web_watcher.storage import count_matches, count_observations, get_last_run

        cfg = load(self._config_path)
        try:
            job_map = {j["watch_name"]: j for j in self._manager.get_job_info()}
        except Exception:
            job_map = {}
        # When the orchestrator owns the continuous watches (per-watch threads are stood
        # down), they're still being watched — by it. Treat them as running so we don't
        # falsely narrate "stopped" the moment the orchestrator takes the wheel.
        try:
            orchestrating = bool(self._manager.orchestrator_running())
        except Exception:
            orchestrating = False

        out = []
        for w in cfg.watches:
            wid = w.id or w.name
            try:
                matches = count_matches(wid, self._db_path)
                obs = count_observations(wid, self._db_path)
            except Exception:
                matches, obs = 0, 0
            err = None
            try:
                last = get_last_run(w.name, self._db_path)
                if last and last.get("error"):
                    err = str(last["error"])[:160]
            except Exception:
                pass
            running = bool(job_map.get(w.name, {}).get("continuous_running"))
            if orchestrating and w.enabled and w.mode == "continuous":
                running = True
            out.append({
                "name": w.name,
                "enabled": w.enabled,
                "running": running,
                "mode": w.mode,
                "matches": matches,
                "observations": obs,
                "error": err,
            })
        return out

    def _current_watch_view(self) -> list[dict]:
        # Reuse the last gathered numbers without re-querying (cheap, lock-held).
        return [
            {k: v.get(k) for k in ("name", "running", "matches", "observations", "enabled")}
            for v in self._prev.values()
        ]

    # ------------------------------------------------------------------
    # Narration
    # ------------------------------------------------------------------

    def _emit(self, kind: str, text: str, watch: Optional[str] = None,
              action: Optional[dict] = None) -> None:
        with self._lock:
            self._entries.append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": kind,
                "text": text,
                "watch": watch,
                "action": action,   # optional one-click fix the panel renders as a button
            })

    def _emit_greeting(self, state: list[dict]) -> None:
        if not state:
            self._emit("status",
                       "I'm on watch — but there's nothing to watch yet. Tell me what to look "
                       "for over in the assistant and I'll start keeping an eye out.")
            return
        running = sum(1 for w in state if w["running"])
        total = sum(w["matches"] for w in state)
        self._emit(
            "status",
            f"I'm on watch. Keeping an eye on {len(state)} "
            f"watch{'es' if len(state) != 1 else ''}"
            + (f", {running} running right now" if running else " (none running yet)")
            + (f". {total} matches found so far." if total else "."),
        )

    def _narrate_deltas(self, state: list[dict]) -> None:
        running_now = {w["name"] for w in state if w["running"]}

        for w in state:
            name = w["name"]
            prev = self._prev.get(name)

            # New matches since last look.
            if prev is not None and w["matches"] > prev.get("matches", 0):
                d = w["matches"] - prev["matches"]
                self._emit(
                    "find",
                    random.choice([
                        f"Good news — '{name}' just turned up {d} new "
                        f"match{'es' if d != 1 else ''}. ({w['matches']} total now.)",
                        f"'{name}' found {d} more — that's {w['matches']} matches in all.",
                        f"{d} fresh hit{'s' if d != 1 else ''} on '{name}'. Worth a look.",
                    ]),
                    watch=name,
                )

            # A new error on a watch's last run.
            if w["error"] and self._error_seen.get(name) != w["error"]:
                self._error_seen[name] = w["error"]
                self._emit(
                    "concern",
                    f"'{name}' hit a snag last run: {w['error']}. I'll keep watching — "
                    f"if it keeps up we should look at it.",
                    watch=name,
                )
            elif not w["error"]:
                self._error_seen.pop(name, None)

            # Finding nothing: lots of observations, no matches. Flag once until it
            # either matches something or its observation count resets.
            if (w["observations"] >= _DRY_OBS_THRESHOLD and w["matches"] == 0
                    and name not in self._dry_flagged):
                self._dry_flagged.add(name)
                self._emit(
                    "concern",
                    f"Heads up: '{name}' has looked at {w['observations']} listings but "
                    f"matched none of them. Its search terms may be too literal — I can "
                    f"broaden them to widen the net.",
                    watch=name,
                    action={"type": "broaden_terms", "watch": name,
                            "label": "Broaden its search terms"},
                )
            if w["matches"] > 0:
                self._dry_flagged.discard(name)

        # Watches that started / stopped since last look.
        for name in running_now - self._running_prev:
            self._emit("decision",
                       f"'{name}' is running now — I'll keep tabs on what it finds.", watch=name)
        for name in self._running_prev - running_now:
            self._emit("status",
                       f"'{name}' stopped. I'll pick back up when it's started again.", watch=name)

    # ------------------------------------------------------------------
    # Periodic spoken review
    # ------------------------------------------------------------------

    def _emit_review(self, state: list[dict]) -> None:
        if not state:
            return
        running = [w for w in state if w["running"]]
        facts = "; ".join(
            f"{w['name']}: {w['matches']} matches from {w['observations']} seen"
            + (" (running)" if w["running"] else "")
            for w in state
        )
        templated = (
            f"Checking in — {len(running)} of {len(state)} watch"
            f"{'es' if len(state) != 1 else ''} running. " + facts + "."
        )
        text = self._voice_review(facts, len(running), len(state)) or templated
        self._emit("review", text)

    def _voice_review(self, facts: str, running: int, total: int) -> Optional[str]:
        """Best-effort conversational rephrase of the review. Returns None on any
        failure so the caller falls back to the templated line. Deliberately uses the
        LIGHT text_model, not the heavy council model — narration phrasing doesn't need
        the big model, and keeping it off the council model leaves the GPU free for the
        sweep judge and the assistant/Watcher chat (the real contention on one local model)."""
        try:
            from web_watcher.config import load
            model = load(self._config_path).models.text_model
        except Exception:
            return None
        system = (
            "You are the oversight agent for a personal marketplace-watching app, talking to "
            "yourself out loud while you keep an eye on the user's watches. Given the raw facts, "
            "write ONE short, warm, first-person check-in line (max 30 words). Be specific and "
            "honest — if a watch is finding nothing, gently say so. No preamble, no quotes, no JSON."
        )
        try:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",
                     "content": f"{running} of {total} watches running. Facts: {facts}\nYour line:"},
                ],
                "stream": False,
            }
            with httpx.Client(timeout=20.0) as client:
                r = client.post(f"{OLLAMA_URL}/api/chat", json=payload)
                r.raise_for_status()
            line = (r.json()["message"]["content"] or "").strip().strip('"').strip()
            # Guard against the model rambling or returning nothing usable.
            if line and len(line) <= 280:
                return line.splitlines()[0].strip()
        except Exception as exc:
            log.debug("Oversight voice pass failed (using templated): %s", exc)
        return None
