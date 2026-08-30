"""
Scheduler — owns the APScheduler instance and the full per-watch run pipeline.

Each enabled watch becomes one APScheduler job. Jobs run in a thread pool
so a slow or hung watch never blocks others.

The full pipeline per watch run:
    1. Browser  — navigate, click-path, extract text + optional screenshot
    2. Perception — decide text vs vision, run heuristic
    3. Reasoning  — call Ollama, get structured result
    4. Notify     — Telegram and/or email if found=True
    5. Storage    — write run record to SQLite regardless of outcome

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  WatchScheduler              ~L60    Class: start/stop/reload/run_now
  start_continuous/stop_*     ~L150   Continuous-watch thread control (daemon threads)
  _load_jobs                  ~L225   Scheduled→APScheduler, continuous→auto-start thread
  _execute_continuous_watch   ~L260   Non-stop sweep loop; owns ONE persistent browser;
                                      dispatches agent vs scraper sweep per watch.autonomous
  _open_continuous_browser    ~L355   (Re)open the loop's persistent browser session
  _run_agent_continuous_sweep ~L400   Agent browses like a person; harvests listings via on_step
  _exploration_plan           ~L385   Randomized human-like browse style for an agent sweep
  _run_continuous_sweep       ~L470   Scraper sweep on the persistent page; rotates watch.urls
  _process_sweep_listings     ~L500   Shared dedup→prime→flood→judge→alert pipeline (both sweeps)
  _cross_watch_match          ~L560   Offer a sweep's fresh finds to OTHER watches' criteria
  _filter_listings_by_judgment ~L620  Batch LLM filter of new listings (optional)
  _alert_new_listings         ~L410   Per-listing notify, capped + paced (rate limits)
  _execute_watch()            ~L470   APScheduler job target — schedule-mode pipeline
  _run_pipeline()             ~L500   Full schedule-mode pipeline
  _run_agent_browse()         ~L640   Autonomous agent path: calls agent.run_agent()
  _run_judgment()             ~L690   Post-browse judgment step using scratchpad facts
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import random
import math
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from web_watcher.config import AppConfig, Watch, load as load_config
from web_watcher.browser import BrowserSession, NAV_TIMEOUT, maybe_warm_homepage
from web_watcher.perception import perceive
from web_watcher.reasoning import Reasoner, ReasoningResult, OllamaUnavailableError
from web_watcher.notify import NotificationPayload, send_notifications
from web_watcher.monitor import (
    Listing, extract_listings, extract_listing_body, extract_listing_posted_at, vary_search,
    human_scroll, is_login_wall, dismiss_popups, humanized_search, human_read, nap,
)
from web_watcher.storage import (
    SCREENSHOTS_DIR,
    RunRecord,
    init_db,
    save_run,
    get_last_run,
    has_seen_listing,
    save_seen_listing,
    count_seen_listings,
    upsert_listing,
    record_observation,
    set_listing_archive,
    find_duplicate,
    list_site_profiles,
)
from web_watcher.monitor import parse_listing_attributes, listing_fingerprint
from web_watcher import fb_safety, llm

log = logging.getLogger(__name__)

# How long to wait for a continuous loop thread to wind down on stop/reload.
_CONTINUOUS_JOIN_TIMEOUT = 30.0
# Small pause between per-listing notifications to stay under Telegram's ~1 msg/s
# sustained per-chat rate limit.
_ALERT_PACE_SECONDS = 1.2
# If a non-priming sweep finds this many "new" listings at once, treat it as a
# baseline gap (thin first sweep, feed restructure) rather than a genuine burst:
# re-baseline silently instead of alerting on what is almost certainly pre-existing
# inventory. Post-priming sweeps normally surface 0-5 new items.
_FLOOD_REBASELINE_THRESHOLD = 30
# How many listings a watch must have banked before we treat it as having a real baseline. Past
# this, a large "new" batch is ordinary churn on a broad search rather than a baseline gap, and
# is judged and alerted normally (capped by the watch's continuous_max_alerts).
_ESTABLISHED_SEEN = 250

# When silently baselining a big backlog (first sweep, or a flood), still JUDGE up to this
# many so the matches show in Results — capped to keep the single judge call fast/accurate.
_BASELINE_JUDGE_CAP = 60
# How many listings go to the judge in ONE call. Measured live: at 60 the 14b silently skipped
# 18 of them. Small models lose track over a long numbered list, and a skipped item costs a whole
# retry pass anyway, so a smaller batch is both more accurate AND usually cheaper.
_JUDGE_BATCH = 15


# Consecutive zero-listing scraper sweeps before a continuous watch auto-escalates to the
# AI agent (the page renders client-side and the fast scraper is blind to it). 2 = one
# confirming repeat, so a single transient empty load doesn't trigger the switch.
_SCRAPER_BLIND_THRESHOLD = 2


def _update_blind_streak(harvested: int, zero_streak: int) -> tuple[int, bool]:
    """Track consecutive zero-harvest scraper sweeps and decide whether to escalate to the
    agent. `harvested` is a sweep's listing count, or -1 when the sweep couldn't run (which
    does NOT count toward "blind" — a nav failure isn't evidence the site is JS-rendered).
    Returns (new_streak, escalate_now)."""
    if harvested == 0:
        zero_streak += 1
    elif harvested > 0:
        zero_streak = 0
    return zero_streak, zero_streak >= _SCRAPER_BLIND_THRESHOLD

# Cap on how many NEW listings we deep-read (open the ad page for) per sweep, so a
# busy sweep can't spawn dozens of page loads. Post-priming sweeps usually have only a
# handful of new items, so this rarely bites.
_MAX_BODY_FETCH = 12


# ---------------------------------------------------------------------------
# Scheduler wrapper
# ---------------------------------------------------------------------------

class WatchScheduler:
    """
    Wraps APScheduler. Loaded from config.yaml on start and on reload().
    Each watch gets one job; misses coalesce to a single catch-up run.
    """

    def __init__(self, config_path: Optional[Path] = None, db_path: Optional[Path] = None) -> None:
        self._config_path = config_path
        self._db_path     = db_path
        self._apscheduler = BackgroundScheduler(
            executors={"default": ThreadPoolExecutor(max_workers=4)},
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 120},
            timezone="UTC",
        )
        # Continuous watches run as dedicated daemon threads — NOT on the APScheduler
        # pool — because a never-returning sweep loop would permanently consume one of
        # only 4 pool workers and starve scheduled jobs. Each loop is interrupted via
        # its threading.Event; APScheduler job removal cannot stop running code.
        self._continuous_threads: dict[str, threading.Thread] = {}
        self._stop_events:        dict[str, threading.Event]  = {}
        # Optional narration callback (kind, text, watch) — set by ServiceManager so the
        # continuous loop can voice things (e.g. an "exploring this site first" warning)
        # into The Watcher's feed. None → narration is silently skipped.
        self._narrator = None
        # Domains explored this process, so a watch that couldn't be fully learned (SPA)
        # doesn't re-run a full exploration on every start/restart.
        self._explored_domains: set[str] = set()
        self._lock = threading.Lock()
        # Coarse mutex serializing the whole stop-then-restart sequence in reload()
        # so two concurrent reloads (e.g. update-watch + connect-facebook) cannot
        # interleave and double-launch a watch. Separate from _lock because reload
        # joins threads and _lock must never be held across a join.
        self._reload_lock = threading.Lock()
        # Does the orchestrator own the continuous watches right now? While it drives, this
        # scheduler must never start a per-watch loop — two engines on one watch means two
        # browsers sweeping the same site at once, which is both wasted work and a bot tell.
        # Set by ServiceManager.start_orchestrator / _stop_orchestrator.
        self._orchestrator_owns = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        init_db(self._db_path)
        self._load_jobs()
        self._apscheduler.start()
        log.info(
            "Scheduler started with %d scheduled job(s), %d continuous watch(es)",
            len(self._apscheduler.get_jobs()), len(self._continuous_threads),
        )

    def stop(self) -> None:
        # Signal and join continuous loops FIRST so their browsers close cleanly
        # (releasing any persistent-profile lock) before the process tears down.
        self._stop_all_continuous()
        if self._apscheduler.running:
            self._apscheduler.shutdown(wait=False)
            log.info("Scheduler stopped")

    def pause_jobs(self) -> None:
        """Pause firing of all scheduled (interval/cron) jobs — the scheduled half of a global
        pause. Running jobs finish; nothing new fires until resume_jobs()."""
        try:
            if self._apscheduler.running:
                self._apscheduler.pause()
        except Exception as exc:
            log.warning("could not pause scheduled jobs: %s", exc)

    def resume_jobs(self) -> None:
        """Resume firing scheduled jobs after a global pause."""
        try:
            if self._apscheduler.running:
                self._apscheduler.resume()
        except Exception as exc:
            log.warning("could not resume scheduled jobs: %s", exc)

    def reload(self) -> None:
        """Remove all jobs and re-read config.yaml. Call after saving config changes."""
        # Serialize the entire stop-then-restart so two concurrent reloads can't
        # interleave and double-launch a watch. Stop continuous loops before
        # rebuilding, else an old loop keeps running while _load_jobs starts a new one.
        with self._reload_lock:
            # Continuous watches don't auto-start, so a config edit must not silently
            # stop one the user had running. Capture the running set and restore it
            # after rebuilding (only for watches still enabled + continuous).
            running = self.running_continuous()
            self._stop_all_continuous()
            for job in self._apscheduler.get_jobs():
                job.remove()
            self._load_jobs()
            if running:
                cfg = load_config(self._config_path)
                still_valid = {w.name for w in cfg.watches if w.enabled and w.mode == "continuous"}
                for name in running:
                    if name in still_valid:
                        self.start_continuous(name)
            log.info(
                "Scheduler reloaded: %d scheduled job(s), %d continuous watch(es) running",
                len(self._apscheduler.get_jobs()), len(self._continuous_threads),
            )

    # ------------------------------------------------------------------
    # Manual trigger
    # ------------------------------------------------------------------

    def run_now(self, watch_name: str) -> None:
        """
        Manually trigger a watch. For schedule-mode watches this fires a single
        immediate run on the pool. For continuous-mode watches it (re)starts the loop.
        """
        cfg = load_config(self._config_path)
        watch = next((w for w in cfg.watches if w.name == watch_name), None)
        if watch is not None and watch.mode == "continuous":
            self.start_continuous(watch_name)
            return
        self._apscheduler.add_job(
            _execute_watch,
            args=[watch_name, self._config_path, self._db_path],
            id=f"{watch_name}__manual",
            replace_existing=True,
        )

    # ------------------------------------------------------------------
    # Continuous-mode control
    # ------------------------------------------------------------------

    # ── Which continuous watches SHOULD be running, across restarts ────────────────
    # Updated only on EXPLICIT start/stop (a person's or the assistant's decision), never on
    # shutdown or reload teardown — so an app-update restart knows exactly what to resume.
    # Before this, every self-update re-registered continuous watches as stopped: a user's
    # brand-new Fiat watch briefed him once and then silently sat dead through three updates
    # ("is the fiat watch continuous?" — it was, and it wasn't running).
    def _running_state_path(self):
        from web_watcher import paths
        return paths.data_dir() / "continuous_running.json"

    def _remember_running(self, watch_name: str, running: bool) -> None:
        try:
            import json as _json
            p = self._running_state_path()
            names = set()
            if p.exists():
                names = set(_json.loads(p.read_text(encoding="utf-8")) or [])
            (names.add if running else names.discard)(watch_name)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_json.dumps(sorted(names)), encoding="utf-8")
        except Exception as exc:
            log.debug("could not persist continuous-running state: %s", exc)

    def _remembered_running(self):
        """The recorded should-be-running set, or None when NOTHING was ever recorded.
        The distinction matters: an EMPTY set means "the user stopped everything" and must be
        honoured; a missing file means "this install predates desired-state" and callers fall
        back to legacy behaviour instead of silently stopping every watch."""
        try:
            import json as _json
            p = self._running_state_path()
            if p.exists():
                return set(_json.loads(p.read_text(encoding="utf-8")) or [])
        except Exception:
            pass
        return None

    def start_continuous(self, watch_name: str) -> bool:
        """Start (or restart) a continuous watch's sweep loop. Returns True if started."""
        with self._lock:
            existing = self._continuous_threads.get(watch_name)
            if existing and existing.is_alive():
                log.info("Continuous watch %r already running", watch_name)
                return False
            stop_event = threading.Event()
            thread = threading.Thread(
                target=_execute_continuous_watch,
                args=[self, watch_name, self._config_path, self._db_path, stop_event],
                name=f"continuous:{watch_name}",
                daemon=True,
            )
            self._stop_events[watch_name] = stop_event
            self._continuous_threads[watch_name] = thread
            thread.start()
        self._remember_running(watch_name, True)
        log.info("Continuous watch %r started", watch_name)
        return True

    def stop_continuous(self, watch_name: str, record: bool = True) -> bool:
        """Signal a continuous watch to stop and wait briefly for it. Returns True if it was running.

        `record=False` for INTERNAL teardown (handing over to the orchestrator, a master-switch
        pause, freeing the browser profile for an FB connect or a drill) — those stops are the
        program rearranging itself, not the user changing their mind, and writing them into the
        desired-state file erases what the user actually asked for. That exact bug cost a full
        night's watching: at launch both watches resumed, the orchestrator took the wheel and
        called stop_all_continuous() to stand the per-watch threads down, and THAT wrote an empty
        desired set — so the rotation it then filtered by that set was empty, and The Watcher sat
        idle for 13 hours while the API cheerfully reported both watches as running."""
        with self._lock:
            stop_event = self._stop_events.get(watch_name)
            thread = self._continuous_threads.get(watch_name)
        if not stop_event or not thread:
            return False
        stop_event.set()
        thread.join(timeout=_CONTINUOUS_JOIN_TIMEOUT)
        if thread.is_alive():
            log.warning("Continuous watch %r did not stop within %.0fs", watch_name, _CONTINUOUS_JOIN_TIMEOUT)
        # Identity-checked cleanup: only pop if THIS event is still registered. A
        # restart during the join window would have installed a new event/thread —
        # don't clobber it (that would orphan an unstoppable loop).
        self._deregister_continuous(watch_name, stop_event)
        if record:
            self._remember_running(watch_name, False)
        log.info("Continuous watch %r stopped", watch_name)
        return True

    def _deregister_continuous(self, watch_name: str, stop_event: "threading.Event") -> None:
        """Remove a watch's registry entry iff stop_event is still the registered one."""
        with self._lock:
            if self._stop_events.get(watch_name) is stop_event:
                self._continuous_threads.pop(watch_name, None)
                self._stop_events.pop(watch_name, None)

    def is_continuous_running(self, watch_name: str) -> bool:
        thread = self._continuous_threads.get(watch_name)
        return bool(thread and thread.is_alive())

    def running_continuous(self) -> list[str]:
        """Names of continuous watches currently running (for save/restore around reload/connect)."""
        with self._lock:
            return [n for n, t in self._continuous_threads.items() if t.is_alive()]

    def stop_all_continuous(self) -> None:
        """Stop every running continuous loop (public; used by the orchestrator handover, the
        master-switch pause, the FB connect flow and drills). Every caller restores afterwards,
        so this NEVER touches the desired-state record — see stop_continuous(record=...)."""
        self._stop_all_continuous()

    def _stop_all_continuous(self) -> None:
        names = list(self._continuous_threads.keys())
        for name in names:
            self.stop_continuous(name, record=False)

    # ------------------------------------------------------------------
    # Job management
    # ------------------------------------------------------------------

    def get_job_info(self) -> list[dict]:
        """Return run info for all watches: next-run for scheduled, status for continuous."""
        jobs = []
        for job in self._apscheduler.get_jobs():
            if job.id.endswith("__manual"):
                continue
            jobs.append({
                "watch_name":  job.id,
                "mode":        "schedule",
                "next_run_utc": job.next_run_time.isoformat() if job.next_run_time else None,
            })
        for name in self._continuous_threads:
            jobs.append({
                "watch_name":  name,
                "mode":        "continuous",
                "continuous_running": self.is_continuous_running(name),
                "next_run_utc": None,
            })
        return jobs

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_jobs(self) -> None:
        cfg = load_config(self._config_path)
        for watch in cfg.watches:
            if not watch.enabled:
                log.debug("Watch %r disabled — skipping", watch.name)
                continue
            if watch.mode == "continuous":
                # Resume what was RUNNING when the app last shut down (an update restart must
                # not silently kill a watch someone started); everything else waits for its
                # Start button. The remembered set changes only on explicit start/stop, so a
                # deliberately-stopped watch stays stopped through any number of restarts.
                if getattr(self, "_orchestrator_owns", False):
                    # A RELOAD while The Watcher drives (any watch edit triggers one). Resuming
                    # here would hand the same watch a second engine: seen live the moment the
                    # desired-state record started working again — one config edit, and a second
                    # visible Chrome opened and swept OfferUp alongside the orchestrator's.
                    log.debug("Continuous watch %r left to The Watcher (it drives them)", watch.name)
                elif watch.name in (self._remembered_running() or set()):
                    log.info("Continuous watch %r was running before the restart — resuming",
                             watch.name)
                    self.start_continuous(watch.name)
                else:
                    log.info("Continuous watch %r registered (stopped) — start it from the "
                             "dashboard", watch.name)
                continue
            self._add_job(watch)

    def _add_job(self, watch: Watch) -> None:
        # Jitter interval runs so a watch doesn't fire on a perfectly regular clock (a bot
        # tell, and it thunders all watches at once). Up to ±20% of the interval, capped at
        # 5 min so long intervals don't drift wildly. Cron watches are left exact (the user
        # picked a specific time).
        if watch.interval_minutes:
            jitter = min(300, int(watch.interval_minutes * 60 * 0.2))
            trigger = IntervalTrigger(minutes=watch.interval_minutes, jitter=jitter)
        else:
            trigger = CronTrigger.from_crontab(watch.cron_expression)
        self._apscheduler.add_job(
            _execute_watch,
            trigger=trigger,
            args=[watch.name, self._config_path, self._db_path],
            id=watch.name,
            name=watch.name,
            replace_existing=True,
        )
        log.info("Scheduled watch %r (%s)", watch.name,
                 f"every {watch.interval_minutes}m" if watch.interval_minutes else watch.cron_expression)


# ---------------------------------------------------------------------------
# Continuous monitor — non-stop sweep loop (runs on a dedicated thread)
# ---------------------------------------------------------------------------

def _narrate(scheduler, kind: str, text: str, watch: Optional[str] = None) -> None:
    """Voice a line into The Watcher's feed if a narrator is wired; else no-op."""
    fn = getattr(scheduler, "_narrator", None)
    if fn is not None:
        try:
            fn(kind, text, watch)
        except Exception as exc:
            log.debug("narrator failed: %s", exc)


def _explore_new_sites_on_start(scheduler, watch_name, config_path, db_path, stop_event) -> None:
    """Before a started watch begins sweeping, explore any site it targets that Web Watcher
    hasn't learned yet (built-ins are already known). Emits a heads-up first, learns each
    unknown site once per process, and never blocks the watch from running if it fails."""
    try:
        from web_watcher.sitelearn import unknown_sites, first_url_for_domain, learn_site
        cfg = load_config(config_path)
        watch = next((w for w in cfg.watches if w.name == watch_name), None)
        if watch is None:
            return
        todo = [d for d in unknown_sites(watch.urls, db_path)
                if d not in scheduler._explored_domains]
        if not todo:
            return
        pretty = ", ".join(todo)
        _narrate(scheduler, "concern",
                 f"Heads up — I haven't explored {pretty} yet, so I'll do a quick "
                 f"exploration round before I start watching.", watch_name)
        for domain in todo:
            if stop_event.is_set():
                return
            scheduler._explored_domains.add(domain)   # attempted — don't loop on failures
            url = first_url_for_domain(watch.urls, domain)
            _narrate(scheduler, "note", f"Exploring {domain} to learn its layout…", watch_name)
            try:
                res = learn_site(
                    url,
                    model=cfg.models.effective_council_model,
                    headless=cfg.browser.headless,
                    persistent=watch.use_login_profile,
                    profile_dir=cfg.browser.profile_dir,
                )
            except Exception as exc:
                log.warning("explore-on-start failed for %s: %s", domain, exc)
                continue
            if res.get("ok"):
                _narrate(scheduler, "note",
                         f"Learned {domain} — I can read its listings now. Starting the watch.",
                         watch_name)
            else:
                _narrate(scheduler, "concern",
                         f"Couldn't fully learn {domain} ({res.get('error') or 'unknown'}). "
                         f"I'll watch it with the AI agent instead.", watch_name)
    except Exception as exc:
        log.debug("explore-on-start skipped for %r: %s", watch_name, exc)


def _execute_continuous_watch(
    scheduler:   "WatchScheduler",
    watch_name:  str,
    config_path: Optional[Path],
    db_path:     Optional[Path],
    stop_event:  "threading.Event",
) -> None:
    """
    Continuous-mode loop: sweep the watch's search repeatedly until stop_event is
    set. Each sweep loads the (varied) search, scrolls, collects listings, dedupes
    against seen state, and alerts on NEW matches. One failed sweep never kills the
    loop — the try/except is INSIDE the while so the loop survives transient errors.

    On exit (stop signal, or the watch being deleted/disabled), the loop deregisters
    itself from the scheduler's registries so a self-ended watch doesn't linger as a
    phantom entry.
    """
    log.info("Continuous loop starting for %r", watch_name)

    # ── Explore-before-watching ──────────────────────────────────────────────
    # When this watch is STARTED (individually or via the Watcher), and it points at a
    # site Web Watcher hasn't learned yet, do ONE exploration round first so it can read
    # that site's listings reliably — with a visible heads-up. This runs on start, not on
    # watch creation, so nothing launches a browser until you actually start watching.
    _explore_new_sites_on_start(scheduler, watch_name, config_path, db_path, stop_event)

    sweep_index = 0
    session: Optional[BrowserSession] = None  # ONE browser kept open across sweeps
    page = None
    # Self-healing engine selection: a scraper sweep that harvests ZERO listings this many
    # times in a row means the page renders its listings with JavaScript (an SPA the fast
    # scraper is blind to). When that happens we auto-escalate THIS watch to the agent for
    # the rest of the session — the agent reads what a scraper can't. config is untouched
    # (the user's `autonomous` flag is unchanged); this is a runtime, self-correcting choice.
    zero_streak = 0
    force_agent = False
    try:
        while not stop_event.is_set():
            try:
                cfg = load_config(config_path)
                watch = next((w for w in cfg.watches if w.name == watch_name), None)
                if watch is None or not watch.enabled:
                    log.info("Continuous watch %r missing/disabled — ending loop", watch_name)
                    break

                # (Re)open the browser only when there isn't a live one. An always-on
                # watch should be ONE persistent window that reloads each sweep — not a
                # window that flickers open and closed every sweep. Reopen if the user
                # closed it manually or it crashed.
                if session is None or page is None or page.is_closed():
                    session, page = _open_continuous_browser(session, watch, cfg)

                # autonomous → the agent browses the page like a person (scroll/search/
                # open categories) and we harvest listings as it goes; otherwise the fast
                # scraper sweep. Both share the persistent page and the alert pipeline.
                # force_agent is the runtime escalation when the scraper proved blind.
                if watch.autonomous or force_agent:
                    _run_agent_continuous_sweep(watch, cfg, db_path, sweep_index, page, stop_event)
                else:
                    harvested = _run_continuous_sweep(watch, cfg, db_path, sweep_index, page, stop_event)
                    # A run of clean-but-empty sweeps (−1 = couldn't run, ignored) means the
                    # scraper is blind to this site → escalate it to the agent.
                    zero_streak, escalate = _update_blind_streak(harvested or 0, zero_streak)
                    if escalate and not force_agent:
                        force_agent = True
                        msg = (f"scraper saw 0 listings {zero_streak}x in a row — this site "
                               "likely renders listings with JavaScript; switching to the AI "
                               "agent for this watch")
                        log.warning("Continuous watch %r: %s", watch_name, msg)
                        try:
                            from web_watcher import issues
                            issues.record_issue("blind_escalation", watch_name, msg)
                        except Exception:
                            pass
                        save_run(RunRecord(watch_name, datetime.now(timezone.utc).isoformat(),
                                           found=False, summary=f"auto-switched to AI agent ({msg})",
                                           perception_mode_used="continuous"), db_path)
                idle = _jittered_idle(watch.continuous_idle_seconds)
            except Exception as exc:
                log.error("Continuous sweep error for %r: %s", watch_name, exc, exc_info=True)
                _save_error(watch_name, datetime.now(timezone.utc).isoformat(),
                            f"continuous sweep: {exc}", db_path, perception_mode="continuous")
                idle = 30  # back off after an error
                # The browser may be the casualty — drop it so the next sweep reopens.
                session, page = _close_continuous_browser(session), None
            sweep_index += 1
            # Interruptible idle — wakes immediately if stop_event is set during the wait.
            if stop_event.wait(idle):
                break
    finally:
        _close_continuous_browser(session)
        # Self-deregister (identity-checked) so a loop that ends on its own — not via
        # stop_continuous — doesn't leave a dead thread/event in the registries.
        scheduler._deregister_continuous(watch_name, stop_event)
    log.info("Continuous loop ended for %r (%d sweeps)", watch_name, sweep_index)


def _watch_geolocation(watch: Watch):
    """(lat, lon) for this watch's location — from a zip in any of its URLs (craigslist
    postal / eBay _stpos), else derived from its instruction — so IP/geo-based sites like
    OfferUp show the RIGHT area instead of a default (that's what served Florida junk).
    None when the location is unknown."""
    try:
        from web_watcher.cl_geo import url_zip, zip_from_text, zip_latlon
        # Try each source and accept the FIRST zip that actually resolves. A URL can carry a
        # non-gazetteer zip (e.g. craigslist postal=98214) that yields no anchor — which silently
        # disabled the out-of-area filter and let out-of-state OfferUp junk through. Falling back
        # to the instruction and the watch NAME ("Anacortes …") recovers a real anchor.
        for z in (next((zz for zz in (url_zip(u) for u in (watch.urls or [])) if zz), None),
                  zip_from_text(watch.instruction or ""),
                  zip_from_text(watch.name or "")):
            ll = zip_latlon(z) if z else None
            if ll:
                return ll
        # Last rung: the town named in WORDS. Every rung above looks for five digits, but people
        # write "within 150 miles of Anacortes", not a zip — so a watch could reach here with no
        # anchor at all, and with no anchor the out-of-area filter passes everything. That is how
        # Brooklyn and British Columbia listings reached a watch centred on Anacortes.
        from web_watcher.cl_geo import place_from_text
        for text in (watch.instruction or "", watch.name or ""):
            ll = place_from_text(text)
            if ll:
                return ll
        return None
    except Exception:
        return None


def _open_continuous_browser(old: Optional["BrowserSession"], watch: Watch, cfg: AppConfig):
    """
    Open a fresh persistent browser session + page for the continuous loop, closing
    any prior one first. Returns (session, page). Kept open across sweeps so the watch
    is one stable window instead of flickering open/closed each sweep.
    """
    _close_continuous_browser(old)
    # RECORD the session when the watch asks for it. Written as one .webm per page under
    # data/recordings/<watch>/, finalised when the session closes. Off unless requested: it is
    # for supervised runs where "what did the agent actually click?" needs an answer better than
    # a log line — Facebook above all, where the account is the thing at risk and the user is
    # not necessarily at the screen.
    rec_dir = None
    if getattr(watch, "record_video", False):
        from web_watcher import paths
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", watch.name)[:60]
        rec_dir = paths.data_dir() / "recordings" / safe
        log.info("Recording this watch's browser to %s", rec_dir)
    session = BrowserSession(
        headless    = cfg.browser.headless,
        stealth     = cfg.browser.stealth,
        persistent  = watch.use_login_profile,
        profile_dir = cfg.browser.profile_dir,
        show_cursor = cfg.browser.show_agent_cursor,
        geolocation = _watch_geolocation(watch),
        record_video_dir = rec_dir,
    )
    session.__enter__()
    # A PERSISTENT context opens with its own about:blank page. Calling new_page() here added a
    # SECOND tab beside it, so every login-profile sweep ran with a stray blank tab sitting next
    # to the real one — visible to the user, and it shows up as a 0-byte .webm in the recordings
    # (which is how it was finally pinned down). Reuse the context's own page when there is one.
    # The same fix already landed in the Connect Facebook flow; this is the sweep path.
    page = None
    try:
        existing = [p for p in (session.context.pages if session.context else []) if not p.is_closed()]
        page = existing[0] if existing else None
    except Exception:
        page = None
    if page is None:
        page = session.new_page()
    return session, page


def _close_continuous_browser(session: Optional["BrowserSession"]) -> None:
    """Best-effort close of a continuous-loop browser session. Always returns None."""
    if session is not None:
        try:
            session.__exit__(None, None, None)
        except Exception as exc:
            log.debug("Continuous browser close failed: %s", exc)
    return None


def _jittered_idle(idle_seconds: int) -> float:
    """
    Idle with a little randomness so sweeps aren't perfectly periodic (clockwork
    timing is an easy bot tell). Roughly -20%..+50% of the configured idle, min 1s.
    """
    base = max(1, idle_seconds)
    return max(1.0, base + random.uniform(-0.2 * base, 0.5 * base))


# Human-like browsing styles for the agent-driven sweep. One is picked at random each
# sweep so the agent doesn't traverse the page the same way every time.
_EXPLORATION_STYLES = [
    ("scroll",   "Scroll slowly through the whole feed like a person reading it. Pause now "
                 "and then. Open one or two listings that look relevant, then go back."),
    ("category", "Look for category links, tabs, or filters (e.g. a Vehicles or Trucks "
                 "category) and click into the most relevant one, then browse its listings."),
    ("search",   "Find the search box, type a relevant search term for what you're looking "
                 "for, submit it (press Enter), and browse the results."),
    ("sort",     "FIRST change how the results are sorted: find the sort control (a DROPDOWN "
                 "— use the 'select' action with its exact option text — or a button showing "
                 "the current sort, often 'newest': click it, then on the next step click one "
                 "of the choices that appears). Pick a DIFFERENT order than the current one — "
                 "prefer 'newest' if not already active, else a price order (price options "
                 "may be labeled with symbols like '$ → $$$' for cheapest-first). THEN scroll "
                 "the re-sorted results — a different order surfaces listings the default "
                 "hides."),
    ("filter",   "FIRST apply ONE relevant filter so the results better match the goal: a "
                 "price limit (type the number into the min/max price field if there is "
                 "one), a category refinement, or a condition/type filter. Filters live in "
                 "a sidebar or behind a 'Filters' button; DROPDOWN filters need the "
                 "'select' action. THEN scroll the filtered results."),
]


def _registrable_domain(url: str) -> str:
    """
    The last two labels of the host ('www.facebook.com' → 'facebook.com'), used to
    tell whether the agent has wandered off the start site. Good enough for the
    common cases (facebook.com vs threads.com); not a full public-suffix parse.
    """
    try:
        host = (urlparse(url).netloc or "").lower().split(":")[0]
    except Exception:
        return ""
    parts = [p for p in host.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _exploration_plan(sweep_index: int, watch: Watch) -> dict:
    """
    Pick a randomized, human-like exploration approach for this sweep (anti-pattern):
    rotates the start URL across the watch's urls, varies scroll depth, and chooses a
    browsing style. Kept tiny and deterministic-free so behavior differs sweep to sweep.
    """
    style_key, directive = random.choice(_EXPLORATION_STYLES)
    raw_url = watch.urls[sweep_index % len(watch.urls)]
    # Self-heal the URL every sweep: clean junk params AND, if it carries no location, pull
    # one from the watch's instruction (so an existing "vehicles in anacortes" watch whose
    # stored URL points at the wrong region gets corrected without recreating it).
    from web_watcher.cl_geo import ensure_location
    start_url = ensure_location(raw_url, watch.instruction)
    return {
        "start_url": start_url,
        "style":     style_key,
        "directive": directive,
    }


def _run_agent_continuous_sweep(
    watch:   Watch,
    cfg:     AppConfig,
    db_path: Optional[Path],
    sweep_index: int,
    page,
    stop_event=None,
) -> None:
    """
    Agent-driven continuous sweep: the autonomous agent browses the page like a person
    (scrolling, searching, opening categories — randomized each sweep) while we harvest
    every listing it sees via the on_step hook, then run the shared dedup/judge/alert
    pipeline. This is the "watch all day like a human" path; the scraper sweep is the
    cheaper, non-agent alternative.
    """
    from web_watcher.agent import run_agent

    run_ts = datetime.now(timezone.utc).isoformat()
    plan   = _exploration_plan(sweep_index, watch)
    model  = watch.model_override or cfg.models.text_model

    # Facebook HALT: a checkpoint stops ALL Facebook activity until a human clears it. The
    # flag is on the ACCOUNT, so this is global — another watch quietly browsing the same
    # flagged account is how a soft flag becomes a ban. Checked before every FB sweep.
    if fb_safety.is_facebook(plan["start_url"]):
        halt = fb_safety.halt_state()
        if halt:
            log.warning("Continuous agent sweep %d for %r: FACEBOOK HALTED (%s) — skipping. "
                        "Clear it in Settings once the account is healthy.",
                        sweep_index, watch.name, halt.get("reason"))
            return
        if _fb_on_cooldown(watch.name):
            log.info("Continuous agent sweep %d for %r: Facebook on cooldown — skipping",
                     sweep_index, watch.name)
            return

    # Accumulate listings across every page the agent visits, keeping the richest
    # title per stable key. Dedup vs seen-state happens later in the shared pipeline.
    # Learned site profiles let the extractor key listings on sites beyond the 3 built-ins.
    harvested: dict = {}
    profiles = list_site_profiles(db_path)

    def _harvest(pg) -> None:
        # Do NOT harvest a marketplace's HOME page. Its tiles are recommendations and promos,
        # not results for this search — and they sit on the same /itm/-style URLs, so the
        # extractor cannot tell them apart. A live eBay sweep hit an error page, recovered by
        # navigating to ebay.com, and banked fifteen homepage tiles (flip-flops, an iPhone,
        # earbuds) into a MacGregor sailboat watch — which then spent twelve real deep-reads
        # on them. Passing through the home page on the way to the search is normal and fine;
        # taking its contents as findings is not.
        try:
            from web_watcher.agent import page_kind
            if page_kind(pg.url) == "home":
                log.debug("Not harvesting the site home page (%s) — promos, not results", pg.url[:60])
                return
        except Exception:
            pass
        for l in extract_listings(pg, max_items=200, profiles=profiles):
            cur = harvested.get(l.key)
            if cur is None or len(l.title) > len(getattr(cur, "title", "")):
                harvested[l.key] = l

    instruction = (
        "You are browsing this marketplace like a real person shopping, as a GUEST "
        "(not logged in).\n"
        f"What you are looking for: {watch.instruction}\n\n"
        f"How to browse this time: {plan['directive']}\n\n"
        "YOUR ONLY JOB is to LOAD as many relevant listings as possible onto the page. "
        "You do NOT need to open or read individual listings — that is done for you "
        "afterwards automatically. So:\n"
        "- Do the setup step from 'How to browse this time' FIRST (a sort change, a "
        "filter, a category, or a search — at most one or two interactions), then "
        "mostly SCROLL: keep scrolling down to load more results.\n"
        "- You MAY run a search, but only ONCE per term, and only with a DIFFERENT "
        "term than before — never re-type a search you already did (the results won't "
        "change). If you've already searched, scroll instead.\n"
        "- Finish ('done') once you've scrolled through a good amount of results.\n\n"
        "HARD RULES — follow exactly:\n"
        "- READ-ONLY. You are only LOOKING. NEVER message a seller, make an offer, buy, "
        "add to cart, check out, like, react, comment, share, post, save/favorite, follow, "
        "add a friend, or report anything. Only scroll, search, sort, filter, open a "
        "listing to read it, and go back.\n"
        "- NEVER log in or sign up. NEVER type an email, password, or phone number. "
        "NEVER click 'Log In', 'Sign Up', or 'Continue with…'.\n"
        "- If the page shows a security check, CAPTCHA, or 'confirm your identity' / "
        "'unusual activity' message, do NOT try to solve it — just finish immediately.\n"
        "- If the page becomes a login or sign-up page, do NOT fill it in — just finish.\n"
        "- Stay on this website. Do NOT follow links to other sites.\n"
        "- Don't finish on the very first step — scroll first."
    )

    # If we already UNDERSTAND this site (comprehension pass), lead with what it IS and what
    # its search box is FOR — so the agent reasons from understanding, not blind. Cached only;
    # never comprehends in the hot sweep path.
    try:
        from web_watcher.storage import get_site_understanding
        u = get_site_understanding(plan.get("start_url") or (watch.urls[0] if watch.urls else ""))
        if u and not u.get("error"):
            sb = u.get("search_box") or {}
            # Understanding SERVES the goal — it never tells the agent to give up on a site
            # (a restock page or a data page is a valid goal, not a failure). It just helps it
            # use the site correctly: what the site is, and what each input actually does.
            note = f"WHAT THIS SITE IS: {u.get('site_kind', 'unknown')}."
            if u.get("how_to_find_listings"):
                note += " " + u["how_to_find_listings"]
            if sb.get("purpose") == "location":
                note += (" Its search box is a LOCATION picker, NOT a keyword search — use it "
                         "ONLY to set a place; do NOT type your goal or product keywords into it.")
            instruction = note + "\n\n" + instruction
    except Exception:
        pass

    # Guardrail: stop the agent the instant it leaves the start site or lands on a
    # login wall, so it never interacts with a login form or wanders off-site.
    start_site = _registrable_domain(plan["start_url"])

    _checkpoint_hit = {"reason": None}   # set when a Facebook security checkpoint stops us

    def _should_stop(pg) -> bool:
        # Honour a stop request (Stop button / reload / delete) mid-browse so the loop
        # halts within a step instead of after the whole sweep — keeps those actions snappy.
        if stop_event is not None and stop_event.is_set():
            log.info("Stop requested mid-sweep — ending agent browse")
            return True
        try:
            cur = pg.url or ""
        except Exception:
            return False
        if start_site and _registrable_domain(cur) != start_site:
            log.info("Agent left %s (now %s) — stopping sweep", start_site, cur[:60])
            return True
        # Facebook security checkpoint / block / CAPTCHA: STOP and remember why — never try
        # to solve it (that turns a soft flag into a ban). The caller alerts + backs off.
        if fb_safety.is_facebook(cur) and fb_safety.is_checkpoint(pg):
            _checkpoint_hit["reason"] = fb_safety.checkpoint_reason(pg)
            log.warning("Facebook checkpoint detected (%s) — STOPPING sweep for %r",
                        _checkpoint_hit["reason"], watch.name)
            return True
        if "/login" in cur or "/checkpoint" in cur or is_login_wall(pg):
            log.info("Agent hit a login wall (%s) — stopping sweep", cur[:60])
            return True
        return False

    drove = False        # bound before the try so the run_agent call below can always read it
    try:
        # HUMAN-FIRST, same as the scraper path. This used to be warm-the-homepage-then-teleport:
        # visit the front page 40% of the time, wait a second, then goto the deep parametric URL
        # anyway — which is theatre, not navigation. It never touched the site's own search box,
        # so every agent sweep still announced itself by materialising on a deep results URL.
        # _human_first_navigate drives the real controls (search box, zip, distance) and is gated
        # by can_fully_drive, so a location or price is never silently dropped; when it can't
        # fully drive the site we fall back to the old warm+goto path unchanged.
        if cfg.browser.stealth:
            try:
                drove = _human_first_navigate(page, plan["start_url"], watch)
            except Exception as exc:
                log.debug("agent sweep: human-first navigation errored, falling back: %s", exc)
        typed = False
        if drove:
            log.info("Continuous agent sweep %d for %r: drove the site's own search controls "
                     "(human-first) → %s", sweep_index, watch.name, page.url[:100])
        else:
            # Second rung: TYPE the search like a person even when we can't fully drive the
            # site's location/price controls. can_fully_drive gates the first rung to sites
            # whose every control is mapped (Craigslist today) — which meant Facebook fell all
            # the way to a bare goto and the sweep announced itself by materialising on a deep
            # parametric results URL. The user watched it happen: "it jumped straight to the
            # search". humanized_search lands on the page WITHOUT the query and types it into
            # the site's own box with real key events; the URL fallback below remains for
            # sites where even that fails.
            # ...unless the query ALREADY went into the site's own box upstairs. Human-first
            # nav returns False when it couldn't apply the location, but by then it has often
            # typed the search — retyping it is the double-search tell (seen live on Facebook).
            already = bool(getattr(page, "_ww_searched", False))
            if cfg.browser.stealth and not already:
                try:
                    typed = humanized_search(page, plan["start_url"])
                except Exception as exc:
                    log.debug("agent sweep: humanized search errored, falling back: %s", exc)
            elif already:
                log.info("Continuous agent sweep %d for %r: search already typed into the "
                         "site's own box — not typing it twice", sweep_index, watch.name)
                typed = True
            if typed:
                log.info("Continuous agent sweep %d for %r: typed the search like a person "
                         "→ %s", sweep_index, watch.name, page.url[:100])
            else:
                maybe_warm_homepage(page, plan["start_url"])
                page.goto(plan["start_url"], timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
    except Exception as exc:
        log.warning("Continuous agent sweep %d: navigation failed for %s: %s",
                    sweep_index, plan["start_url"], exc)
        _save_error(watch.name, run_ts, f"navigation: {exc}", db_path,
                    perception_mode="continuous-agent")
        try:
            from web_watcher import issues
            issues.record_issue("nav_failed", watch.name,
                                f"could not load {plan['start_url'][:80]}: {type(exc).__name__}")
        except Exception:
            pass
        return

    dismiss_popups(page)

    # A logged-out URL that redirects to a login page (e.g. FB's category feeds)
    # is useless to a guest watch — skip it instead of letting the agent poke at a
    # login form. (use_login_profile watches that lost their session get the normal
    # reconnect-notice path.)
    if is_login_wall(page):
        if watch.use_login_profile:
            _handle_login_wall(watch, cfg, run_ts, db_path)
        else:
            log.info("Continuous agent sweep %d for %r: %s is a login wall logged-out — skipping",
                     sweep_index, watch.name, plan["start_url"])
            save_run(RunRecord(watch.name, run_ts, found=False,
                               summary=f"skipped {plan['start_url']} (login wall when logged out)",
                               perception_mode_used="continuous-agent"), db_path)
        return

    # Did craigslist silently hand us the WHOLE SITE? An unknown category code isn't rejected —
    # it redirects to "all for sale", so a boat watch quietly starts harvesting lawn mowers and
    # chairs, the judge correctly throws every one away, and the watch looks like it just isn't
    # finding anything. From the URL alone this is invisible; from the redirect it's obvious.
    from web_watcher import cl_geo
    fell_back = cl_geo.category_fell_back(plan["start_url"], page.url)
    if fell_back:
        fixed = cl_geo.repair_craigslist_category(plan["start_url"], watch.instruction or "")
        hint = (f" Try {fixed} instead." if fixed != plan["start_url"] else "")
        msg = (f"craigslist does not have a category {fell_back!r} — it redirected to ALL for "
               f"sale, so this watch is searching the entire site instead of one category."
               + hint)
        log.error("Watch %r: %s", watch.name, msg)
        _save_error(watch.name, run_ts, msg, db_path, perception_mode="continuous-agent")

    # A checkpoint the moment we land (before the agent acts) → stop, alert, back off.
    if fb_safety.is_facebook(page.url) and fb_safety.is_checkpoint(page):
        _handle_fb_checkpoint(watch, cfg, run_ts, db_path, fb_safety.checkpoint_reason(page))
        return

    # Facebook watches get a tighter per-sweep action cap (pacing / smaller footprint) —
    # never more than the account-safety ceiling, whatever the watch configured.
    steps = watch.max_agent_steps
    if fb_safety.is_facebook(plan["start_url"]):
        steps = min(steps, fb_safety.SESSION_ACTION_CAP)

    log.info("Continuous agent sweep %d for %r: style=%s start=%s (max_steps=%d)",
             sweep_index, watch.name, plan["style"], plan["start_url"], steps)
    _agent_result = None      # bound before the try so the challenge check below is safe
    try:
        _agent_result = run_agent(
            page,
            instruction   = instruction,
            model         = model,
            max_steps     = steps,
            council_model = cfg.models.effective_council_model,
            vision_model  = cfg.models.vision_model or None,
            ocr_threshold = cfg.models.ocr_threshold,
            on_step       = _harvest,
            should_stop   = _should_stop,
            exploration_mode = True,
            # Lock the search when the query was established through the site's OWN controls —
            # fully driven (search + location) OR typed into its box. The typed rung was left
            # unlocked, and on Facebook the agent then clicked the sidebar's "Vehicles"
            # category item, swapping the MacGregor query for a feed of sedans. Only a plain
            # goto fallback leaves the agent free to fix the query itself.
            search_locked = drove or typed,
            # Let the agent see whether its scrolling is actually producing anything.
            harvest_size  = lambda: len(harvested),
        )
    except Exception as exc:
        # Process whatever we harvested before the error rather than losing the sweep.
        log.error("Continuous agent sweep %d for %r errored mid-browse: %s",
                  sweep_index, watch.name, exc)

    # A challenge we could NOT clear ended the run. Tell the user plainly — an unattended app
    # that silently reports "0 found" when it was actually blocked is how a watch looks healthy
    # for a week while finding nothing. The site is already resting (see sitecool); this is the
    # shoulder-tap so a human can clear it if they want that site back sooner.
    _blocked_at = getattr(_agent_result, "challenge_blocked", None) if _agent_result else None
    if _blocked_at:
        try:
            from web_watcher import sitecool
            from web_watcher.notify import send_plain_telegram
            mins = max(1, sitecool.cooling_for(_blocked_at) // 60)
            host = sitecool.host_of(_blocked_at) or _blocked_at[:40]
            send_alert(
                f"🛑 <b>{host}</b> asked for a human check that I couldn't clear.\n\n"
                f"Watch: “{watch.name}”\nI've stopped touching that site for about "
                f"{mins} minute(s) so we don't push it. Your other watches keep running.\n\n"
                f"If you want it back sooner, open it yourself, clear the check, then say "
                f"“resume {host}”.",
                cfg.notifications, html=True)
            from web_watcher import issues
            issues.record_issue("challenge", watch.name,
                                f"{host} showed a human check we couldn't clear — resting {mins}m")
        except Exception as exc:
            log.debug("could not send the challenge alert: %s", exc)

    # The agent stopped on a Facebook checkpoint mid-browse → alert + back off, and do
    # NOT process/alert listings from a flagged session.
    if _checkpoint_hit["reason"]:
        _handle_fb_checkpoint(watch, cfg, run_ts, db_path, _checkpoint_hit["reason"])
        return

    listings = list(harvested.values())
    log.info("Continuous agent sweep %d for %r: harvested %d unique listing(s) while browsing",
             sweep_index, watch.name, len(listings))

    # Record how this sweep STRUGGLED, if it did — one aggregated place to see recurring trouble
    # per watch, instead of it scrolling past in the raw log. Best-effort; never breaks a sweep.
    try:
        from web_watcher import issues
        sc = getattr(_agent_result, "stuck_count", 0) or 0
        fs = getattr(_agent_result, "forced_scrolls", 0) or 0
        if not listings:
            issues.record_issue("no_listings", watch.name,
                                "sweep completed but harvested nothing (blind extractor or empty feed)")
        if sc:
            issues.record_issue("stuck", watch.name,
                                f"agent hit the get-unstuck council {sc}× this sweep")
        if fs:
            issues.record_issue("forced_scroll", watch.name,
                                f"setup budget scrolled for the agent {fs}× (it kept re-fiddling controls)")
    except Exception as exc:
        log.debug("could not record sweep struggle: %s", exc)

    _process_sweep_listings(watch, cfg, db_path, sweep_index, listings, run_ts,
                            mode_label="continuous-agent",
                            page=page, fetch_bodies=_wants_deep_read(watch),
                            stop_event=stop_event)

    # Go back for matches this watch banked BEFORE it could read them — from a priming
    # sweep, or from any run made while the deep-read was gated off. See _explore_matches.
    _explore_matches(watch, cfg, db_path, page, stop_event)


# How often to take the long way round (back to the section, retype the search) when the
# browser is already sitting on this watch's results. Mostly we just refresh in place.
_RESEARCH_ODDS = 0.2


def _showing_our_results(page, req, section: str = "") -> bool:
    """Is the page already showing THIS search's results, in the RIGHT SECTION?

    Deliberately strict: same host, inside the watch's own section, a results-shaped path,
    and EVERY search term present in the query. A loose match keeps a stale or unrelated page
    and quietly sweeps the wrong thing — and without the section test, Facebook's GLOBAL
    /search/top?q=macgregor+sailboat matches perfectly, which is the very page v0.152 was
    about escaping."""
    from urllib.parse import urlparse, unquote_plus
    try:
        cur = page.url or ""
    except Exception:
        return False
    if not cur or not req.terms:
        return False
    u = urlparse(cur)
    if "search" not in (u.path or "").lower():
        return False
    if section:
        s = urlparse(section)
        if (u.netloc or "").lower() != (s.netloc or "").lower():
            return False
        want = (s.path or "/").rstrip("/")
        if want and not (u.path or "").startswith(want):
            return False
    haystack = unquote_plus(f"{u.path}?{u.query}").lower()
    return all(t.lower() in haystack for t in req.terms.split() if t)


def _human_first_navigate(page, url: str, watch: Watch) -> bool:
    """Run this watch's search by DRIVING the site's own controls like a person — land on the
    site's shallow entry (the region/site homepage), then type the search + set the sidebar
    zip/distance/price via navigate.apply_search_request — instead of goto-ing a deep parametric
    results URL (our biggest bot tell; see memory feedback_human_first_navigation).

    Gated by navigate.can_fully_drive: only runs when the site's hints can apply EVERY part of
    the request (search + location + price), so we NEVER silently drop a location or price a
    site can't yet drive. Otherwise returns False and the caller falls back to the URL path.
    This makes the human-first rollout automatic + safe: a site becomes driven exactly when its
    control hints are complete, not before. Returns True only if the search was really driven."""
    from urllib.parse import urlparse
    from web_watcher import navigate as N
    from web_watcher.monitor import search_landing_url

    # Fresh per navigation. The flag rides on the long-lived sweep page, so a stale True from
    # a previous sweep would suppress typing forever after.
    try:
        setattr(page, "_ww_searched", False)
    except Exception:
        pass
    hint = N.hints_for(url)
    if not hint:
        return False
    # Only DRIVE sites whose full flow is live-verified (Craigslist today). A site with mapped
    # hints but an unproven/flaky driver (OfferUp's location dialog) must NOT be driven in the
    # real sweep — it stays on the URL fallback until it graduates into HUMAN_FIRST_SITES.
    if not N.is_human_first_enabled(url):
        return False
    req = N.build_search_request(url, watch.instruction)
    # Drivable when there's a keyword to type OR a category to click (the "browse cars+trucks"
    # watch with no keyword — the shape most of the real watches actually have). can_fully_drive
    # still refuses a category we have no link hint for, so we never land on the front page and
    # quietly browse the wrong thing.
    if not (req.terms or req.category) or not N.can_fully_drive(req, hint):
        return False

    # ALREADY IN POSITION? After a sweep the browser is sitting on THIS WATCH'S OWN results.
    # Walking back to the section home and retyping the query — every 5 to 15 minutes, forever
    # — is not what a person does: someone watching for a boat leaves the results tab open and
    # refreshes it. It's also three navigations where one will do. So most of the time, stay
    # and reload in place.
    #
    # Not ALWAYS, though: doing the identical thing every single visit is its own pattern, and
    # a real person does drift back to Marketplace and search again now and then. _RESEARCH_ODDS
    # of the time we take the long way round on purpose.
    if _showing_our_results(page, req, search_landing_url(url) or ""):
        if random.random() > _RESEARCH_ODDS:
            log.info("Human-first nav on %s: already on this search's results — refreshing in "
                     "place rather than re-navigating", urlparse(url).netloc)
            try:
                page.reload(timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
            except Exception as exc:
                log.debug("in-place refresh failed, will re-navigate: %s", exc)
            else:
                dismiss_popups(page, settle_ms=0)
                setattr(page, "_ww_searched", True)   # the query is already in the page
                return True
        else:
            log.info("Human-first nav on %s: on the results already, but going back to search "
                     "again this time (variety)", urlparse(url).netloc)

    p = urlparse(url)
    # LAND IN THE RIGHT SECTION. This used to go to the bare domain root — on Facebook that's
    # the news feed, which doesn't HAVE a Marketplace search box, so the query went into the
    # global "Search Facebook" one and the sweep searched all of Facebook. search_landing_url
    # already knows the shallow entry for each site (facebook.com → /marketplace/); this is
    # the one place that wasn't asking it.
    home = search_landing_url(url) or f"{p.scheme}://{p.netloc}/"
    # ...AND DON'T RE-ENTER A PAGE WE'RE ALREADY ON. Every sweep reloaded the landing page
    # even when the browser was sitting right there: the user watched it close Marketplace,
    # open the homepage, and walk back to Marketplace again. A person who is already looking
    # at Marketplace just uses the search box.
    try:
        here = (page.url or "").split("?")[0].rstrip("/")
    except Exception:
        here = ""
    if here and here == home.split("?")[0].rstrip("/"):
        log.debug("human-first: already on %s — using the page we're on", home)
    else:
        try:
            page.goto(home, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
        except Exception as exc:
            log.debug("human-first landing nav failed: %s", exc)
            return False
    dismiss_popups(page, settle_ms=0)

    applied = N.apply_search_request(page, req, hint)
    # Remember what actually went into the page, even when we end up returning False. The
    # caller's next rung TYPES THE SEARCH AGAIN, and on Facebook the user watched exactly
    # that: the query typed once here, then a second time 9 seconds later in the Marketplace
    # box. Nobody searches twice — it's a worse tell than the deep URL this was avoiding.
    try:
        setattr(page, "_ww_searched", bool(applied.get("searched")))
    except Exception:
        pass
    if not (applied.get("searched") or applied.get("categorized")):
        return False
    # Landing on the right page but DROPPING the zip/price would quietly widen the watch to the
    # whole region at any price — worse than the URL we were avoiding. If the request asked for
    # a location or a price and the controls didn't take, fall back rather than sweep wrong.
    # A dropped CATEGORY silently widens the watch to every section of the site — the same class
    # of failure as a dropped zip, and the one that put golf clubs in a sailboat watch. Verified
    # against what actually happened on the page, not against what we hoped was possible.
    if req.category and not applied.get("categorized"):
        log.info("Human-first nav on %s: category %r was not applied — falling back to the URL",
                 p.netloc, req.category)
        return False
    if req.zip and not applied.get("located"):
        log.info("Human-first nav on %s: location was not applied — falling back to the URL",
                 p.netloc)
        return False
    if (req.price_min is not None or req.price_max is not None) and not applied.get("filtered"):
        log.info("Human-first nav on %s: price filter was not applied — falling back to the URL",
                 p.netloc)
        return False
    log.info("Human-first nav on %s: drove %s (applied %s)", p.netloc, req.describe(), applied)
    return True


def _run_continuous_sweep(
    watch:   Watch,
    cfg:     AppConfig,
    db_path: Optional[Path],
    sweep_index: int,
    page,
    stop_event = None,
) -> int:
    """
    Fast (non-agent) sweep on the loop's persistent page: load → scroll → extract →
    dedupe → deep-read new ads → judge → alert → record. This is the right path for
    sites with a clean search URL (Craigslist, eBay): it gets the listings instantly,
    then the SAME deep-read the agent path uses reads each new ad's attributes — so you
    get attribute filtering (manual transmission, 4x4, mileage) without the agent.

    When the watch has MULTIPLE urls (e.g. several category feeds), each sweep advances
    to the next one. With a single url it just reloads (with search variation) each sweep.

    Returns the number of listings harvested this sweep, or -1 when the sweep couldn't run
    (navigation failure / login wall) — the caller uses a run of 0-harvest sweeps to detect
    a site the scraper is blind to (a JS/SPA site) and auto-escalate it to the agent.
    """
    run_ts   = datetime.now(timezone.utc).isoformat()
    # Self-heal the URL's location from the watch instruction (fixes an existing watch whose
    # stored craigslist/eBay URL points at the wrong region), then apply the sweep variation.
    from web_watcher.cl_geo import ensure_location
    base_url = ensure_location(watch.urls[sweep_index % len(watch.urls)], watch.instruction)
    url      = vary_search(base_url, sweep_index, watch.continuous_search_variation)

    # Human-first navigation (preferred): for sites whose controls we can FULLY drive, run the
    # search by operating the page's OWN controls — land on the shallow entry, type the search,
    # set the sidebar zip/distance/price — instead of jumping to a deep parametric URL (our
    # biggest bot tell). Gated by can_fully_drive so a location/price is never silently dropped.
    drove = False
    if cfg.browser.stealth:
        try:
            drove = _human_first_navigate(page, url, watch)
        except Exception as exc:
            log.debug("human-first navigation errored, falling back: %s", exc)

    # Fallback 1: TYPE the search term into the box but land via the (still parametric) URL —
    # humanizes the keyword only. Fallback 2: a direct goto. Both keep working for sites/cases
    # human-first can't fully drive yet (no hints, generic-category, non-search filters).
    typed = bool(getattr(page, "_ww_searched", False))   # never type the same query twice
    if not drove and not typed and cfg.browser.stealth:
        try:
            typed = humanized_search(page, url)
        except Exception as exc:
            log.debug("humanized_search errored, will goto directly: %s", exc)
    if not drove and not typed:
        try:
            page.goto(url, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
        except Exception as exc:
            log.warning("Continuous sweep %d: navigation failed for %s: %s", sweep_index, url, exc)
            _save_error(watch.name, run_ts, f"navigation: {exc}", db_path, perception_mode="continuous")
            return -1

    # Close login/cookie/consent overlays (e.g. FB Marketplace's logged-out
    # "Log in or sign up" modal) that otherwise intercept scrolling and hide the
    # listings. Done before the login-wall check so a dismissable modal isn't
    # mistaken for a hard wall.
    dismiss_popups(page)

    # Login-required site that logged us out → notify (throttled) and skip.
    if watch.use_login_profile and is_login_wall(page):
        _handle_login_wall(watch, cfg, run_ts, db_path)
        return -1

    human_scroll(page, watch.continuous_scroll_passes)
    # FB re-injects the login modal after scrolling — clear it again before reading.
    dismiss_popups(page)
    listings = extract_listings(page, max_items=200, profiles=list_site_profiles(db_path))

    # Deep-read is decoupled from the agent: read new ads' attributes whenever the watch has
    # criteria to consume them, regardless of how the listings were gathered.
    _process_sweep_listings(watch, cfg, db_path, sweep_index, listings, run_ts,
                            page=page, fetch_bodies=_wants_deep_read(watch),
                            stop_event=stop_event)
    # Same backlog drain as the agent sweep — a watch that never used the agent can have
    # just as many unread matches banked behind it. See _explore_matches.
    _explore_matches(watch, cfg, db_path, page, stop_event)
    return len(listings)


# How many previously-banked matches to go back and read per sweep. Small on purpose: this
# runs IN ADDITION to the sweep's own deep-reads, and each one is a real ~10-45s page visit.
_EXPLORE_BACKLOG_PER_SWEEP = 4
# Rating floor for pushing a match nobody ever saw. A watch with its own min_rating uses that;
# otherwise only genuine 4-5 star finds are worth an unprompted card days after the fact.
_DRIP_MIN_RATING = 4


def _explore_matches(watch, cfg, db_path, page, stop_event=None) -> int:
    """Open and read matches this watch already found but never actually looked at.

    A match can be banked without its ad body ever being read — that is what a priming sweep
    does (it judges card titles cheaply), and it is what every sweep did while the deep-read
    was gated on `judgment_prompt`. Those rows sit in Results with an empty body, no posted
    date and no frozen copy: the listing is known to exist, but nothing about it is known.

    Each sweep this picks a few of them off the backlog and reads them properly — same human
    dwell, same archive capture as a fresh match. Small batches by design: a burst of twenty
    listing views in a row is the pattern we are specifically trying not to look like.
    Returns how many were read. Never raises — a failed backfill must not end a sweep.
    """
    if page is None or not _wants_deep_read(watch):
        return 0
    if stop_event is not None and stop_event.is_set():
        return 0
    try:
        from web_watcher.storage import query_listings
        wid  = watch.id or watch.name
        rows = query_listings(watch_id=wid, matched=True, limit=200, db_path=db_path)
        pending = [r for r in rows
                   if not (r.get("details") or "").strip() and (r.get("url") or "")]
        if not pending:
            return 0
        # Oldest-unread first, so the backlog actually drains instead of re-reading the head.
        pending.reverse()
        batch = [Listing(key=r["listing_key"], url=r["url"],
                         title=r.get("title") or "", price=r.get("price_text") or "")
                 for r in pending[:_EXPLORE_BACKLOG_PER_SWEEP]]
        log.info("Continuous watch %r: reading %d previously-unread match(es) "
                 "(%d still unread)", watch.name, len(batch), len(pending))
        _capture_listing_bodies(page, batch, stop_event)

        read = 0
        for l in batch:
            details = (getattr(l, "details", "") or "").strip()
            if not details:
                continue
            read += 1
            try:
                host  = urlparse(l.url).netloc if l.url else ""
                attrs = parse_listing_attributes(l.title, l.price, details)
                upsert_listing(l.key, source=host, url=l.url, title=l.title,
                               price_text=l.price, attributes=attrs, details=details,
                               fingerprint=listing_fingerprint(l.title, attrs.get("price_value"),
                                                               attrs.get("year")),
                               image=getattr(l, "image", "") or "",
                               posted_at=getattr(l, "posted_at", "") or "",
                               ts=datetime.now(timezone.utc).isoformat(), db_path=db_path)
                tmp = getattr(l, "_archive_tmp", None)
                if tmp is not None:
                    from web_watcher import archive
                    kept = archive.keep(tmp, l.key)   # already a match — always worth freezing
                    if kept:
                        set_listing_archive(l.key, str(kept), db_path=db_path)
                    l._archive_tmp = None
            except Exception as exc:
                log.debug("could not store backfilled read for %s: %s", l.key, exc)
        if read:
            log.info("Continuous watch %r: filled in the ad details for %d earlier match(es)",
                     watch.name, read)
        _drip_unalerted(watch, cfg, db_path, run_ts=datetime.now(timezone.utc).isoformat())
        return read
    except Exception as exc:
        log.debug("explore-matches pass failed for %r: %s", watch.name, exc)
        return 0


def _drip_unalerted(watch, cfg, db_path, run_ts: str) -> int:
    """Push matches that were found but never actually shown to anyone.

    THE HOLE THIS CLOSES: a baseline sweep judges and records its matches without alerting —
    correctly, or a fresh watch fires a wall of cards. But those rows are then marked seen, so
    they can never be "new" again, and nothing downstream ever offered them. Fifteen real
    MacGregor matches sat in Results for two days, deep-read, archived, and invisible from the
    user's phone: "i see results in the results tab but they were never pushed to my telegram".

    Paced by the watch's own continuous_max_alerts, so a backlog drips out over sweeps instead
    of arriving as the burst the baseline guard existed to prevent. Only reads that have a body
    (an unread match has nothing to say beyond its title) and only real matches by rating.
    """
    try:
        from web_watcher.storage import unalerted_matches
        cap = max(1, watch.continuous_max_alerts)
        floor = watch.min_rating if getattr(watch, "min_rating", None) else _DRIP_MIN_RATING
        rows = unalerted_matches(watch.id or watch.name, min_rating=floor,
                                 limit=cap, db_path=db_path)
        if not rows:
            return 0
        batch = []
        for r in rows:
            l = Listing(key=r["listing_key"], url=r.get("url") or "",
                        title=r.get("title") or "", price=r.get("price_text") or "")
            l.rating = r.get("rating")
            l.judge_reason = r.get("judge_reason") or ""
            l.image = r.get("image") or ""
            batch.append(l)
        log.info("Continuous watch %r: %d earlier match(es) were never sent — pushing now "
                 "(paced at %d/sweep)", watch.name, len(batch), cap)
        return _alert_new_listings(watch, cfg, batch, run_ts, db_path)
    except Exception as exc:
        log.debug("drip of unalerted matches failed for %r: %s", watch.name, exc)
        return 0


def _wants_deep_read(watch) -> bool:
    """Should this watch open each new listing and read the actual ad?

    This USED to be `bool(watch.judgment_prompt)` — but the judge one layer down runs on
    `judgment_prompt OR instruction`. So a watch with a plain instruction ("MacGregor
    sailboats near Seattle, under $8k") got judged on criteria it had no way to check: no
    body, no posted date, no archive — just the card title. That is how a Facebook run
    harvested fifteen real MacGregors, opened none of them, and rejected several on a
    location guessed from whatever leaked into the card text. The two gates have to agree.
    """
    return bool(watch.judgment_prompt or (watch.instruction or "").strip())


def _capture_listing_bodies(page, listings: list, stop_event=None) -> None:
    """
    Deep-read each NEW listing: open its ad page in a background tab (the agent's main
    page keeps its place), pull the body + attributes via extract_listing_body, and
    store it on listing.details so the judge can match on what's IN the ad (transmission,
    4x4, mileage, condition…) not just the card title. Sequential and capped; mutates
    the listings in place. Best-effort — a fetch that fails just leaves details empty.
    """
    if not listings:
        return
    try:
        ctx = page.context
    except Exception:
        return
    fetched = 0
    for i, l in enumerate(listings):
        if stop_event is not None and stop_event.is_set():
            break
        if not l.url:
            continue
        # Pace the reads like a person actually opening a listing, reading, and moving on
        # — NOT a bot machine-gunning tabs open/closed (a strong bot tell, especially on
        # Facebook, which watches for that). Pause before each (after the first).
        if i > 0:
            if not nap(random.uniform(4.0, 11.0), stop_event):
                break
        tab = None
        try:
            tab = ctx.new_page()
            # OPEN LIKE A CTRL-CLICK, NOT A COLD LOAD. A bare goto in a fresh tab arrives
            # with NO referrer — no click origin, no browsing context — which was the biggest
            # remaining tell on Facebook: the "user" reads a results page in one tab while
            # listing pages materialise from nowhere in others. With the results page sent as
            # the referer, each tab reads as "opened in a new tab from the search results" —
            # exactly how a person actually browses a marketplace.
            try:
                ref = page.url or None
            except Exception:
                ref = None
            tab.goto(l.url, timeout=NAV_TIMEOUT, wait_until="domcontentloaded", referer=ref)
            dismiss_popups(tab, settle_ms=0)
            # Spend a believable amount of time ON the ad — scrolling the description,
            # looking at the photos — BEFORE scraping it. Reading first also means the
            # lazy-loaded parts of the page (seller block, full description) have arrived
            # by the time we extract, so the body we get is more complete, not just more
            # human. See monitor.human_read.
            spent = human_read(tab, stop_event)
            l.details = extract_listing_body(tab)
            l.posted_at = extract_listing_posted_at(tab)
            # One line per ad. Reading properly turned this phase from seconds into minutes,
            # and a phase that logs nothing for six minutes is indistinguishable from a hang —
            # which is exactly the question asked of the last silent run.
            log.info("Read %d/%d: %s (%.0fs, %d chars)", i + 1, len(listings),
                     (l.title or l.url)[:52], spent, len(l.details or ""))
            # Freeze the page while we're on it — self-contained MHTML, no extra visit. Held in a
            # TEMP file; the persist step keeps it only if this listing matched, discards it
            # otherwise. See web_watcher/archive.py.
            try:
                from web_watcher import archive
                l._archive_tmp = archive.capture_temp(tab, l.key)
            except Exception:
                l._archive_tmp = None
            fetched += 1
        except Exception as exc:
            # Visible (not debug) so a systematic 0/N failure is diagnosable from the log.
            log.warning("Body fetch failed for %s: %s: %s",
                        l.url[:70], type(exc).__name__, str(exc)[:140])
        finally:
            if tab is not None:
                try:
                    tab.close()
                except Exception:
                    pass
    log.info("Deep-read %d/%d new listing(s) for their ad details", fetched, len(listings))


def _persist_listings(watch, listings: list, matched_keys: set, run_ts: str, db_path,
                      reason_by_key: dict | None = None) -> None:
    """
    Persist listings to the GLOBAL store and record this watch's observation of each
    (parsed attributes + content fingerprint + match verdict), keyed by the watch's
    stable id. This is the listing-centric data layer — it survives the watch being
    renamed or deleted, and a listing seen by several watches is one row. Best-effort:
    never breaks a sweep.
    """
    wid = watch.id or watch.name
    reason_by_key = reason_by_key or {}
    for l in listings:
        try:
            host    = urlparse(l.url).netloc if l.url else ""
            details = getattr(l, "details", "") or ""
            attrs   = parse_listing_attributes(l.title, l.price, details)
            fp      = listing_fingerprint(l.title, attrs.get("price_value"), attrs.get("year"))
            upsert_listing(l.key, source=host, url=l.url, title=l.title,
                           price_text=l.price, attributes=attrs, details=details,
                           fingerprint=fp, image=getattr(l, "image", "") or "",
                           posted_at=getattr(l, "posted_at", "") or "",
                           ts=run_ts, db_path=db_path)
            record_observation(wid, watch.name, l.key, run_ts,
                               matched=(l.key in matched_keys),
                               rating=getattr(l, "rating", None),
                               judge_reason=reason_by_key.get(l.key) or getattr(l, "judge_reason", None),
                               db_path=db_path)
            # Frozen page captured at deep-read: KEEP it for a match (a listing the user might act
            # on and later find deleted), discard it otherwise. Records the path on the row.
            _tmp = getattr(l, "_archive_tmp", None)
            if _tmp is not None:
                try:
                    from web_watcher import archive
                    if l.key in matched_keys:
                        kept = archive.keep(_tmp, l.key)
                        if kept:
                            set_listing_archive(l.key, str(kept), db_path=db_path)
                    else:
                        archive.discard(_tmp)
                except Exception:
                    pass
                l._archive_tmp = None
        except Exception as exc:
            log.debug("Persist listing %s failed: %s", l.key, exc)


_STOPWORDS = {
    "the", "and", "for", "with", "near", "from", "any", "all", "look", "find", "watch",
    "listing", "listings", "post", "posts", "read", "only", "under", "over", "about",
    "that", "this", "just", "new", "used", "good", "great", "please", "want", "wanted",
    "buy", "sell", "sale", "area", "around", "within", "miles", "mile", "price", "prices",
}


def _instruction_terms(watch) -> list[str]:
    """The distinctive words from a watch's own instruction — 'macgregor', 'sailboat'.
    Used to decide which listings are worth the judge's limited attention first."""
    text = f"{watch.instruction or ''} {watch.judgment_prompt or ''}".lower()
    seen, out = set(), []
    for w in re.findall(r"[a-z][a-z0-9'-]{2,}", text):
        if w in _STOPWORDS or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def _judge_order(batch: list, watch) -> list:
    """Order a batch so the listings most likely to matter are judged FIRST.

    The baseline judges a capped slice of what a priming sweep harvested, and it used to
    take that slice in raw FEED order. On a 233-listing Facebook prime that meant judging
    positions 1-60 and silently dropping the rest — including ten genuine MacGregors that
    happened to sit further down the feed. They went into the database unjudged and never
    appeared in Results. The cap is fine; taking an arbitrary slice under it was not.

    Scoring is deterministic and free: count how many of the watch's own instruction terms
    appear in the card title. Ties keep feed order, so nothing is shuffled without cause.
    """
    terms = _instruction_terms(watch)
    if not terms:
        return list(batch)
    titles = [(getattr(l, "title", "") or "").lower() for l in batch]
    n = len(titles) or 1
    # Weight each term by how RARE it is in this batch. On a "MacGregor sailboats near
    # Seattle" watch, half the feed says "seattle" and nearly all of it says "sailboat" —
    # those separate nothing. "macgregor" appears in a handful, so it is the term actually
    # carrying the search. Counting terms equally let common words outrank the brand.
    weight = {}
    for t in terms:
        hits = sum(1 for ti in titles if t in ti)
        weight[t] = 0.0 if hits == 0 else 1.0 + math.log(n / hits)
    def score(i):
        return sum(w for t, w in weight.items() if t in titles[i])
    ranked = sorted(range(len(batch)), key=lambda i: (-score(i), i))
    return [batch[i] for i in ranked]


def _baseline_batch(watch, cfg, batch: list, run_ts: str, db_path, mode_label: str, verb: str,
                    page=None, stop_event=None) -> None:
    """Silently baseline a large batch (first sweep, or a flood-guard trip): mark the WHOLE
    batch seen so we never notify on pre-existing backlog — but still JUDGE a capped slice and
    record its matches so they populate Results. This is the difference between a new watch
    looking broken (hundreds found, nothing shown) and it showing real matches from the start,
    just without a wall of notifications."""
    # Relevance-first, then cap — so a real match further down the feed is never the one
    # that falls off the end. See _judge_order.
    to_judge = _judge_order(batch, watch)[:_BASELINE_JUDGE_CAP]
    if len(batch) > _BASELINE_JUDGE_CAP:
        log.info("Continuous watch %r: judging the %d most relevant of %d primed listing(s)",
                 watch.name, len(to_judge), len(batch))
    # Keyword prefilter first (free) — even on the baseline, so parts/salvage never reach
    # the LLM and are recorded as keyword-excluded non-matches.
    kw_kept, kw_dropped = _keyword_prefilter(to_judge, watch)
    matched = kw_kept
    if kw_kept and (watch.judgment_prompt or (watch.instruction or "").strip()):
        # Card-level judge (no deep-read) — one LLM call for the slice; keeps priming cheap.
        # Runs against the instruction even without an explicit judgment_prompt, so a plain
        # watch doesn't baseline EVERYTHING as a match (the "everything is a match" bug).
        # Pass 1 ONLY — the cheap card-level screen. Pass 2 waits until the survivors have
        # been read (below), because verifying from a card title is how a transmission watch
        # rejects seven real candidates as "Transmission not specified".
        matched = _filter_listings_by_judgment(kw_kept, watch, cfg, verify=False)
    # Now go READ the ones that passed. Priming stays cheap on the JUDGE (one call over
    # card titles) but the handful that survive it are exactly the listings a person would
    # click, so they get opened, read and archived like any other match. Without this a
    # primed watch banked its best finds with an empty body and no frozen copy — the state
    # the Facebook run ended in.
    if matched and page is not None and _wants_deep_read(watch):
        _capture_listing_bodies(page, _judge_order(matched, watch)[:_MAX_BODY_FETCH],
                                stop_event)
        # NOW verify, with the ad bodies in hand. This is the pass that asks "does THIS
        # listing satisfy the criteria" one at a time, and it is the one that most needs the
        # text — the watch's own prompt says "open the listing and read its transmission".
        if watch.judgment_prompt or (watch.instruction or "").strip():
            matched = _verify_kept_listings(matched, watch, cfg,
                                            getattr(watch, "min_rating", 3))
    matched_keys = {l.key for l in matched}
    if to_judge:
        _persist_listings(watch, to_judge, matched_keys, run_ts, db_path)
    for l in batch:
        save_seen_listing(watch.name, l.key, run_ts, summary=l.title, link=l.url, db_path=db_path)
    log.info("Continuous watch %r %s %d listing(s); judged %d, recorded %d match(es) to Results (no alerts)",
             watch.name, verb, len(batch), len(to_judge), len(matched_keys))
    save_run(RunRecord(watch.name, run_ts, found=False,
                       summary=f"{verb} {len(batch)} listings; {len(matched_keys)} matches recorded (no alerts)",
                       perception_mode_used=mode_label), db_path)

    # Tell them it happened. Suppressing a wall of alerts is right; saying NOTHING is not — from
    # the outside a silent baseline is indistinguishable from a watch that found nothing, which
    # is exactly how "it never notified me about any boats" happens. So: one message with the
    # numbers and a way to act on the backlog we just quietly banked.
    try:
        from web_watcher.notify import send_baseline_briefing
        # Hand it the best few ACTUAL listings (highest-rated first) so the message carries
        # real cars, not just a number behind a button nobody may ever tap.
        best = sorted(matched, key=lambda l: -(getattr(l, "rating", 0) or 0))[:3]
        send_baseline_briefing(watch.name, len(batch), len(matched_keys), cfg.notifications,
                               owner_chat_id=getattr(watch, "owner", "") or "",
                               instruction=watch.instruction or "", top=best)
    except Exception as exc:
        log.debug("baseline briefing not sent for %r: %s", watch.name, exc)


def _process_sweep_listings(
    watch:   Watch,
    cfg:     AppConfig,
    db_path: Optional[Path],
    sweep_index: int,
    listings: list,
    run_ts:  str,
    mode_label: str = "continuous",
    page = None,
    fetch_bodies: bool = False,
    stop_event = None,
) -> None:
    """
    Shared post-extraction pipeline for BOTH the scraper sweep and the agent-driven
    sweep: dedup → prime → flood-guard → judgment filter → alert → record. Keeping
    this in one place means the two sweep kinds alert identically; the only difference
    is how the listings were gathered. `mode_label` tags the run-history row.
    """
    if not listings:
        log.info("Continuous sweep %d for %r: no listings found", sweep_index, watch.name)
        save_run(RunRecord(watch.name, run_ts, found=False,
                           summary="sweep found no listings", perception_mode_used=mode_label), db_path)
        return

    priming = count_seen_listings(watch.name, db_path) == 0

    # Dedup: which listings have we never seen for this watch?
    new_listings = [l for l in listings if not has_seen_listing(watch.name, l.key, db_path)]

    # First sweep ever: establish a baseline WITHOUT notifications — you don't want a push
    # for all pre-existing inventory. But still JUDGE the batch and record the matches so the
    # Results view is populated from the start (previously it recorded nothing → the watch
    # looked broken: lots found, nothing in Results).
    if priming:
        _baseline_batch(watch, cfg, listings, run_ts, db_path, mode_label, "primed",
                        page=page, stop_event=stop_event)
        return

    # Flood guard: an implausibly large "new" batch usually means a baseline gap (a thin first
    # sweep, a rotated term's backlog, the feed restructured) rather than a genuine burst, so we
    # baseline it silently instead of firing a wall of alerts.
    #
    # ⚠ ONLY WHILE THE WATCH IS STILL BUILDING ITS PICTURE. On a broad search — every boat within
    # 150 miles is thousands of listings — each sweep's scroll/sort variation legitimately turns
    # up another 80-200 unseen ones, so an unconditional guard trips forever: the watch
    # re-baselines on every single sweep and can NEVER alert. That's exactly what happened live
    # (200 → 189 → 86 re-baselined, no alerts ever). Once a watch has a real baseline behind it,
    # a big batch is normal and gets judged normally — `continuous_max_alerts` is what caps the
    # volume, and that is its whole job.
    established = count_seen_listings(watch.name, db_path) >= _ESTABLISHED_SEEN
    if len(new_listings) >= _FLOOD_REBASELINE_THRESHOLD and not established:
        _baseline_batch(watch, cfg, new_listings, run_ts, db_path, mode_label, "re-baselined",
                        page=page, stop_event=stop_event)
        return

    # Repost detection: a listing with a NEW id but the same content fingerprint AND
    # same source as one this watch already surfaced is a re-post of the same item. We do
    # NOT hide it — it's recorded and LINKED to the original (its dup is noted on the
    # listing), and it INHERITS the original's match verdict so a real match is never lost.
    # We only skip the redundant deep-read + re-alert (you were already pinged for this
    # item). Matching is conservative (same source) so we'd rather show a possible dup
    # than wrongly merge two different listings and miss real content.
    wid = watch.id or watch.name
    fresh, reposts, batch_fps, dup_of = [], [], {}, {}
    for l in new_listings:
        a   = parse_listing_attributes(l.title, l.price, "")   # card-level; details not read yet
        fp  = listing_fingerprint(l.title, a.get("price_value"), a.get("year"))
        src = urlparse(l.url).netloc if l.url else ""
        canon = None
        if fp:
            if fp in batch_fps:
                canon = {"listing_key": batch_fps[fp], "matched": 0}
            else:
                canon = find_duplicate(wid, fp, src, l.key, db_path)
        if canon:
            reposts.append(l)
            dup_of[l.key] = canon
        else:
            fresh.append(l)
            if fp:
                batch_fps[fp] = l.key
    if reposts:
        log.info("Continuous watch %r: %d listing(s) are reposts of already-seen items "
                 "(linked + noted, not re-alerted)", watch.name, len(reposts))

    # Deterministic OUT-OF-AREA drop (free; before any deep-read or LLM judge): some feeds are
    # anchored to the watch's area but NOT radius-limited — OfferUp's distance is "Maximum"
    # (nationwide) and isn't settable by URL or in its location dialog, so a Burbank-CA truck
    # surfaces on an Anacortes-WA watch even after we set the anchor. Drop listings we can
    # CONFIDENTLY place far from the watch; anything we can't locate is kept (conservative — this
    # never eats a listing whose city we can't read). Craigslist/eBay are already URL-localized
    # and rarely carry a "City, ST" in the title, so this is effectively an OfferUp/marketplace net.
    geo_dropped = []
    if fresh:
        anchor = _watch_geolocation(watch)
        if anchor:
            from web_watcher.cl_geo import out_of_area, state_for_latlon
            watch_state = state_for_latlon(*anchor)
            near = []
            for l in fresh:
                text = f"{getattr(l, 'title', '') or ''} {getattr(l, 'location', '') or ''}"
                (geo_dropped if out_of_area(text, anchor, watch_state) else near).append(l)
            if geo_dropped:
                log.info("Continuous watch %r: dropped %d out-of-area listing(s) (too far from "
                         "the watch's location)", watch.name, len(geo_dropped))
            fresh = near

    # Cheap keyword prefilter (free; before any deep-read or LLM): drop listings with an
    # antikeyword / missing a required keyword. Dropped ones are still recorded (as non-matches,
    # with the reason) so they show in the log/Results, just not alerted.
    kw_dropped = []
    if fresh:
        fresh, kw_dropped = _keyword_prefilter(fresh, watch)

    # Deep-read the FRESH listings' ad pages so the judge can match on what's in the ad
    # (transmission, 4x4, mileage, condition), not just the card title. Only the agent
    # sweep enables this (fetch_bodies); the scraper sweep stays cheap. Capped per sweep.
    if fetch_bodies and page is not None and fresh:
        _capture_listing_bodies(page, fresh[:_MAX_BODY_FETCH], stop_event)

    # Rating judge: rate every fresh listing against the watch's criteria (its instruction,
    # plus any judgment_prompt) and keep those >= min_rating. This runs for EVERY watch —
    # without it, a watch with no explicit judgment_prompt marked EVERYTHING a match, so
    # "matches only" in Results showed the raw feed (the "everything is a match" bug). The
    # instruction alone is enough criteria for the judge. On any failure it falls back to
    # keeping all fresh listings, so a judge hiccup never silently drops real finds.
    matched = fresh
    if fresh and (watch.judgment_prompt or (watch.instruction or "").strip()):
        matched = _filter_listings_by_judgment(fresh, watch, cfg)
    matched_keys = {l.key for l in matched}
    # Persist keyword- and out-of-area-dropped listings too (non-match), so they're recorded, not lost.
    fresh = fresh + kw_dropped + geo_dropped

    # Persist fresh (with verdict + attributes).
    _persist_listings(watch, fresh, matched_keys, run_ts, db_path)
    # Persist reposts: inherit the canonical's match verdict, and note which listing they
    # duplicate — so nothing is hidden and a real match isn't dropped.
    if reposts:
        rep_matched = {l.key for l in reposts if dup_of[l.key].get("matched")}
        rep_reason  = {l.key: f"duplicate of {dup_of[l.key]['listing_key']}" for l in reposts}
        _persist_listings(watch, reposts, rep_matched, run_ts, db_path, reason_by_key=rep_reason)

    # Record-as-seen so we never reprocess: non-matched fresh (matched ones are saved
    # only AFTER a successful alert send — see _alert_new_listings — so a crash never
    # swallows a real match) and every repost.
    for l in fresh:
        if l.key not in matched_keys:
            save_seen_listing(watch.name, l.key, run_ts, summary=l.title, link=l.url, db_path=db_path)
    for l in reposts:
        save_seen_listing(watch.name, l.key, run_ts, summary=l.title, link=l.url, db_path=db_path)

    alerted = _alert_new_listings(watch, cfg, matched, run_ts, db_path) if matched else 0

    # Cross-watch matching: a listing THIS watch stumbled on (e.g. a Corvette the truck
    # watch loaded) may be exactly what ANOTHER watch wants. Offer the fresh listings to
    # the user's other watches so a good find isn't lost just because the "wrong" watch
    # surfaced it. Opt-out via cfg.cross_watch_matching.
    if fresh and getattr(cfg, "cross_watch_matching", True):
        try:
            _cross_watch_match(watch, cfg, db_path, fresh, run_ts)
        except Exception as exc:
            log.warning("Cross-watch matching failed for %r: %s", watch.name, exc)

    log.info("Continuous sweep %d for %r: %d listings, %d new (%d fresh, %d repost), %d alerted",
             sweep_index, watch.name, len(listings), len(new_listings),
             len(fresh), len(reposts), alerted)
    save_run(RunRecord(
        watch.name, run_ts, found=bool(alerted),
        summary=(f"sweep {sweep_index}: {len(listings)} listings, {len(new_listings)} new "
                 f"({len(reposts)} repost), {alerted} alerted"),
        perception_mode_used=mode_label,
    ), db_path)


def _cross_watch_match(
    source_watch: Watch,
    cfg:          AppConfig,
    db_path:      Optional[Path],
    fresh:        list,
    run_ts:       str,
) -> None:
    """
    Offer THIS sweep's fresh listings to the user's OTHER continuous watches.

    Why: every listing is stored once globally, but a verdict ("is this a match?") is
    recorded per watch. So a Corvette the 4x4-truck watch loads while scrolling is real
    inventory the user wants — it just got surfaced by the "wrong" watch. Rather than lose
    it until the sports-car watch's own sweep happens to find it, we run each fresh listing
    against the OTHER watches' criteria here and, on a match, record it + alert under that
    watch (provenance noted), exactly as if that watch had found it itself.

    Bounded + safe:
      • Only other ENABLED, CONTINUOUS watches that have a judgment_prompt (without criteria
        we can't tell a match from noise) and have already been PRIMED (so we don't inject
        into a watch that hasn't set its own baseline yet).
      • Only listings that other watch hasn't already SEEN (no double-alert), capped per
        watch to keep the judge call cheap.
      • Every candidate is marked seen for the other watch afterwards (match or not) so the
        same listing isn't re-judged every sweep. The source watch's own state is untouched.
    """
    others = [
        w for w in cfg.watches
        if w.name != source_watch.name
        and w.enabled and w.mode == "continuous" and w.judgment_prompt
    ]
    if not others:
        return

    for other in others:
        try:
            # Skip until the other watch has primed its own baseline.
            if count_seen_listings(other.name, db_path) == 0:
                continue
            candidates = [
                l for l in fresh if not has_seen_listing(other.name, l.key, db_path)
            ][:_MAX_BODY_FETCH]
            if not candidates:
                continue

            # fail_closed: if the judge errors, do NOT inject un-judged listings into another
            # watch — that's exactly how a sports car leaks into the trucks results.
            matched = _filter_listings_by_judgment(candidates, other, cfg, fail_closed=True)
            matched_keys = {l.key for l in matched}

            if matched:
                reason = f"cross-watch: surfaced by '{source_watch.name}'"
                reason_by_key = {l.key: reason for l in matched}
                _persist_listings(other, matched, matched_keys, run_ts, db_path,
                                  reason_by_key=reason_by_key)
                alerted = _alert_new_listings(other, cfg, matched, run_ts, db_path)
                log.info("Cross-watch: %d listing(s) from %r matched %r — %d alerted",
                         len(matched), source_watch.name, other.name, alerted)

            # Mark every candidate seen for the other watch (matched ones are marked by the
            # alert path, but re-marking is idempotent) so we don't re-judge them each sweep.
            for l in candidates:
                save_seen_listing(other.name, l.key, run_ts,
                                  summary=l.title, link=l.url, db_path=db_path)
        except Exception as exc:
            log.warning("Cross-watch match into %r failed: %s", other.name, exc)


# The 1-5 rating rubric the graded judge scores against — lifted from
# ai-marketplace-monitor's design (their best idea). A listing is a "match" (alertable)
# when its rating >= the watch's min_rating (default 3).
_RATING_RUBRIC = (
    "Rate how well each listing matches the user's criteria on a 1-5 scale.\n"
    "THE SCORE MEASURES ONE THING: does this listing satisfy what the user ASKED FOR — every "
    "stated requirement (brand/model, item type, location, price, specs). A listing that is a "
    "perfectly nice REAL item but fails a stated requirement is NOT a match.\n"
    "  1 = Wrong KIND of thing entirely: toys/models/diecast/replicas of the item, spare PARTS "
    "or ACCESSORIES for it, unrelated categories (a dresser for a vehicle search, GOLF CLUBS "
    "for a sailboat search that shares a brand name), or obvious spam/scams.\n"
    "  2 = Right kind of thing but FAILS a stated requirement: wrong brand or model (a C&C or "
    "J30 sailboat when the user asked for a MacGregor), outside the stated area, over the "
    "stated budget, or missing a required spec (automatic when manual was required).\n"
    "  3 = Meets every stated requirement, but a detail is unconfirmed (the ad doesn't say).\n"
    "  4 = Clearly meets EVERY stated requirement with confirming details.\n"
    "  5 = Meets every stated requirement AND is an unusually good deal or condition.\n"
    "HARD RULE: if your own reason says the listing fails a requirement — wrong make, outside "
    "the area, too far, over budget — the rating MUST be 2 or lower. Never write a failing "
    "reason with a passing score.\n"
    "STATED means STATED: judge only requirements the user actually wrote. If they gave no "
    "price limit, price is NOT a criterion — never invent a budget. If they gave no location, "
    "location is NOT a criterion. 'Near <place>' means the surrounding region (a listing one "
    "or two towns over still qualifies), and listings have already been distance-screened "
    "before you see them — reject on location only if the AD ITSELF names somewhere clearly "
    "outside the stated area."
)


# Prose that admits the listing FAILS a requirement. The batch judge kept writing reasons like
# "Not within radius" / "Too far away" and then rating the listing a PASSING 3 — so listings the
# judge itself had rejected sailed into Results. The score is what gates; this makes a failing
# reason override a passing score deterministically, whatever the model felt like emitting.
_FAILING_REASON_RE = re.compile(
    r"\b(outside|too far|not within|beyond the|over budget|over the (stated |)budget|"
    r"wrong (make|model|brand|kind|type|category|item)|not a match|no match|"
    r"does\s?n[o']t match|unrelated|excluded|not the right|different (make|model|brand)|"
    r"another (country|state|region)|not rated by judge)\b", re.IGNORECASE)


def _rejected_block(watch: Watch) -> str:
    """The "you already told me no to these" block for the judge prompt, or "" when there are
    none. Capped and title-only: enough to convey the shape of a rejection without turning every
    judge call into a wall of history."""
    try:
        from web_watcher.storage import rejected_examples
        titles = rejected_examples(watch.id or watch.name, limit=6)
    except Exception:
        return ""
    if not titles:
        return ""
    lines = "\n".join(f"  - {t}" for t in titles)
    return ("THE USER ALREADY REJECTED THESE for this watch — anything essentially like them "
            f"is a 2 or lower:\n{lines}\n\n")


def _reason_contradicts_pass(rating: int, why: str, threshold: int) -> bool:
    """True when the judge's own words reject the listing while its score passes the gate."""
    return rating >= threshold and bool(_FAILING_REASON_RE.search(why or ""))


# How far over a stated budget a listing may be and still come through — the "on the edge" grace
# band the user asked to keep. 0.10 = 10%: a $15k watch still surfaces a $16.5k boat, but a $30k
# one is gone. Sellers routinely take a little less, and the price itself is fuzzy (title vs field).
_PRICE_TOLERANCE = 0.10


def _price_cap_for(watch: Watch) -> int | None:
    """This watch's maximum price, from its URLs or its own words. Cached on the watch object so
    a sweep parses it once. None when the watch never stated a budget — then nothing is dropped
    on price, because "no cap" must not become "cap of zero"."""
    cached = getattr(watch, "_price_cap_cache", "unset")
    if cached != "unset":
        return cached
    try:
        from web_watcher.cl_geo import watch_price_cap
        cap = watch_price_cap(list(watch.urls or []), watch.instruction or "")
    except Exception as exc:
        log.debug("could not read a price cap for %r: %s", watch.name, exc)
        cap = None
    try:
        object.__setattr__(watch, "_price_cap_cache", cap)
    except Exception:
        pass
    return cap


def _keyword_prefilter(listings: list, watch: Watch) -> tuple[list, list]:
    """Cheap, deterministic keyword gate run BEFORE the LLM judge (free; cuts GPU load and
    false alerts). Returns (kept, dropped). A listing is dropped if it contains ANY
    antikeyword, or (when keywords are set) contains NONE of them. Matches case-insensitively
    over the title + ad body. Each dropped listing gets .judge_reason set for the log/UI."""
    kw   = [k.lower() for k in (watch.keywords or []) if k.strip()]
    anti = [k.lower() for k in (watch.antikeywords or []) if k.strip()]
    # A watch with a stated budget must never surface something WAY over it. Whether $30,000
    # exceeds $15,000 is arithmetic, not a judgement call — leaving it to the 14b produced exactly
    # the failure you'd expect: $30k, $29k and $28k boats all "matched" a $15k watch. The site's
    # own price filter can't be relied on either, since the agent sorts and scrolls its way onto
    # pages the filter no longer applies to.
    #
    # But a HARD cutoff is too sharp: a great $16k boat on a $15k watch is exactly the kind of
    # "on the edge" post worth seeing — a seller often takes a bit less, and the number itself is
    # fuzzy (price in the title vs the field). So the gate keeps a small grace band above the cap
    # (_PRICE_TOLERANCE) and only drops what's clearly over. Edge posts come through on purpose
    # now, not by luck (before, they slipped through only when the price failed to parse).
    cap = _price_cap_for(watch)
    anchor = _watch_geolocation(watch)
    radius = None
    if anchor:
        from web_watcher.cl_geo import url_radius
        radius = next((r for r in (url_radius(u) for u in (watch.urls or [])) if r), 100)
    if not kw and not anti and cap is None and not anchor:
        return listings, []
    kept, dropped = [], []
    from web_watcher.cl_geo import is_placeholder_price
    for l in listings:
        price = getattr(l, "price_value", None)
        if price is None:
            price = (getattr(l, "attributes", None) or {}).get("price_value")
        # A "$12,345" / "$99,999" / "$0" isn't a price — it's "make me an offer". Treat it as
        # UNKNOWN so an under-budget make-offer post isn't binned as over budget.
        if is_placeholder_price(price):
            price = None
        if cap is not None and isinstance(price, (int, float)) and price > cap * (1 + _PRICE_TOLERANCE):
            l.judge_reason = f"over budget (${int(price):,} > ${cap:,})"
            dropped.append(l); continue
        # Where the listing actually IS. Craigslist honours the radius correctly — the surprise
        # is that 100 miles from Anacortes reaches Metro Vancouver, which is closer than Seattle.
        # Those listings are in range and still wrong: another country, another currency, a
        # border crossing. Judged from the town in the listing's own URL, resolved against the
        # watch's anchor, and only dropped when the town is positively somewhere else — a town
        # we can't place is kept.
        if anchor:
            from web_watcher.cl_geo import city_is_near, listing_city
            town = listing_city(getattr(l, "url", "") or "")
            if town and city_is_near(town, anchor, radius) is False:
                l.judge_reason = f"outside the {radius}-mile search area"
                dropped.append(l); continue
        hay = f"{l.title or ''} {getattr(l, 'details', '') or ''}".lower()
        hit_anti = next((a for a in anti if a in hay), None)
        if hit_anti:
            l.judge_reason = f"excluded by keyword {hit_anti!r}"
            dropped.append(l); continue
        if kw and not any(k in hay for k in kw):
            l.judge_reason = "no required keyword present"
            dropped.append(l); continue
        kept.append(l)
    if dropped:
        def _n(word):
            return sum(1 for d in dropped if word in (getattr(d, "judge_reason", "") or ""))
        bits = []
        if _n("over budget"):
            bits.append(f"{_n('over budget')} over the ${cap:,} budget")
        if _n("outside the"):
            bits.append(f"{_n('outside the')} outside the search area")
        log.info("Prefilter dropped %d/%d listing(s) for %r%s",
                 len(dropped), len(listings), watch.name,
                 f" ({'; '.join(bits)})" if bits else "")
    return kept, dropped


def _filter_listings_by_judgment(new_listings: list, watch: Watch, cfg: AppConfig,
                                 fail_closed: bool = False, verify: bool = True) -> list:
    """
    Batch-judge new listings against the watch's criteria in ONE LLM call, RATING each
    1-5 (see _RATING_RUBRIC). A listing is kept (alertable) when its rating >=
    watch.min_rating. Each judged listing gets `.rating` and `.judge_reason` attached so
    the persist/alert/Results path can show stars + the verdict. Returns the kept subset.

    On error the behavior depends on fail_closed:
      • fail_closed=False (default, a watch's OWN sweep) → return ALL new listings: for the
        watch the user explicitly created, over-alerting beats silently dropping a real match.
      • fail_closed=True (cross-watch matching) → return [] : injecting un-judged listings into
        ANOTHER watch is how off-topic items leak in. When we can't confidently judge, skip.
    """
    threshold = getattr(watch, "min_rating", 3)

    def _entry(i: int, l) -> str:
        line = f"{i}. {l.title} {l.price}".strip()
        # Stamp the COMPUTED distance on the row. The model's own geography rated a San Juan
        # in Bremerton "Too far from Seattle" (27 straight-line miles); with the gazetteer's
        # number on the line there is nothing left to guess.
        miles = _listing_miles(watch, l.title or "")
        if miles is not None:
            line += f"  [~{miles:.0f} mi from the user's area]"
        if l.details:
            line += f"\n   AD DETAILS: {l.details[:600]}"
        return line

    system_prompt = (
        "You rate marketplace listings against a user's criteria. Each entry has a "
        "title/price and, when available, an 'AD DETAILS' line with the listing's "
        "description and attributes — USE those details (transmission, drivetrain, "
        "mileage, condition) when rating.\n" + _RATING_RUBRIC + "\n"
        "Return ONLY a JSON object of the form "
        '{"ratings": [{"i": <index>, "r": <1-5>, "why": "<≤10 words>"}, ...]}. '
        "Include EVERY listing exactly once. No other text."
    )

    def _rate(subset: list) -> dict:
        """Rate a subset — a list of (orig_index, listing) — in one LLM call. Returns
        {orig_index: (rating, why)}. Prompt is numbered by LOCAL position, mapped back."""
        numbered = "\n".join(_entry(k, l) for k, (_oi, l) in enumerate(subset))
        # Counter-examples the USER hand-rejected on this watch. Without them the feedback loop
        # stopped at antikeywords — the judge never learned anything from a rejection, so it
        # rated the next near-identical listing the same way and the user rejected it again.
        rejected = _rejected_block(watch)
        user_msg = (
            f"Criteria: {watch.instruction}\n{watch.judgment_prompt or ''}\n\n"
            f"{rejected}"
            f"Listings:\n{numbered}\n\nRate every listing."
        )
        # Route through the provider layer: role "judge" may be config-routed to a cloud model
        # (Haiku by default) with the fixed rubric prompt-cached; otherwise it uses the local
        # council model, and any cloud failure falls back to local automatically. See llm.py.
        content = llm.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_msg},
            ],
            role="judge",
            local_model=cfg.models.effective_council_model,
            cfg=cfg,
            format_json=True,
            cache_system=True,
            timeout=90.0,
        )
        data = json.loads(content)
        out: dict[int, tuple[int, str]] = {}
        for item in (data.get("ratings") or []):
            try:
                k = int(item.get("i"))
                r_ = max(1, min(5, int(item.get("r"))))
            except (TypeError, ValueError):
                continue
            if 0 <= k < len(subset):
                out[subset[k][0]] = (r_, str(item.get("why", "")).strip())
        return out

    def _rate_chunked(pairs: list) -> dict:
        """Rate in CHUNKS. A 60-item batch had the 14b silently skipping 18 of them (measured
        live on eBay) — small models lose track over a long numbered list, and every skipped
        item then needs a retry pass anyway. Smaller batches keep each list inside what the
        model reliably enumerates, so far fewer items go missing in the first place."""
        out: dict[int, tuple[int, str]] = {}
        for i in range(0, len(pairs), _JUDGE_BATCH):
            out.update(_rate(pairs[i:i + _JUDGE_BATCH]))
        return out

    try:
        by_idx = _rate_chunked(list(enumerate(new_listings)))
        # Small models silently skip entries when the batch is long or an item reads as
        # 'obviously' off-topic. Re-judge the omitted ones once so a genuine match is never
        # dropped merely because it went unrated on the first pass.
        missing = [(i, l) for i, l in enumerate(new_listings) if i not in by_idx]
        if missing:
            log.info("Rating judge: %d/%d unrated on first pass for %r — re-judging",
                     len(missing), len(new_listings), watch.name)
            try:
                by_idx.update(_rate_chunked(missing))
            except Exception as exc:
                log.debug("re-judge of unrated items failed for %r: %s", watch.name, exc)

        keep = []
        for i, l in enumerate(new_listings):
            # STILL unrated after a retry → treat as a NON-MATCH, not a free pass. Defaulting
            # unrated→keep is exactly how toys/parts/a dresser flooded Results.
            rating, why = by_idx.get(i, (2, "not rated by judge — treated as non-match"))
            # The judge's own words override its score: "Not within radius" with a passing 3
            # is a rejection, whatever the number says. Demote deterministically.
            if _reason_contradicts_pass(rating, why, threshold):
                log.info("   demoted (reason contradicts score %d): %s — %s",
                         rating, (l.title or "")[:60], why[:60])
                rating = threshold - 1
            l.rating = rating
            l.judge_reason = why or getattr(l, "judge_reason", "")
            if rating >= threshold:
                keep.append(l)
            else:
                # A rejection on grounds the judge had no basis for — model geography the
                # gazetteer contradicts, or a budget the user never stated — gets the verify
                # second look instead of a silent death here (the overturn lives in verify,
                # which a failing batch rating never reaches: that asymmetry re-killed an
                # Ocean Shores MacGregor at ~110 mi even after the verify-side fix). Verify
                # re-checks EVERY criterion, so a wrong-brand boat still dies there.
                tag = _appealable_rejection(watch, why, l.title or "")
                if tag:
                    log.info("   second look (%s): %r was rejected as %r — sending to verify",
                             tag, (l.title or "")[:50], (why or "")[:40])
                    l.rating = threshold
                    keep.append(l)
        log.info("Rating judge kept %d/%d (>=%d) for %r",
                 len(keep), len(new_listings), threshold, watch.name)
        for l in new_listings:
            if getattr(l, "rating", threshold) < threshold:
                log.info("   rated %d: %s %s — %s", l.rating,
                         (l.title or "(no title)")[:70], l.price, getattr(l, "judge_reason", ""))
        # PASS 2 — verify each survivor INDIVIDUALLY. The batch call is a cheap screen, but it
        # judges by local index over a long numbered list, and small models drift: the C&C 25's
        # verdict arrived carrying the J30's reason. One listing per call cannot misalign, and
        # it runs only on the few that passed, so the cost is bounded.
        # verify=False lets a caller DEFER pass 2 until the survivors have been deep-read.
        # A watch whose criteria say "open the listing and read its transmission" cannot be
        # verified from a card title: on a priming run the verifier rejected seven real
        # candidates as "Transmission not specified" — the very fact the ad body would have
        # supplied. See _baseline_batch, which reads first and then verifies.
        if verify:
            keep = _verify_kept_listings(keep, watch, cfg, threshold)
        return keep
    except Exception as exc:
        if fail_closed:
            log.warning("Rating judge failed for %r (%s) — cross-watch SKIPPED (fail-closed)",
                        watch.name, exc)
            return []
        log.warning("Rating judge failed for %r (%s) — alerting all new listings", watch.name, exc)
        return new_listings


# Per-sweep cap on individual verifications — pass 2 exists to catch the batch judge's mistakes
# on the handful of listings that PASSED, not to re-judge a flood. Past the cap, the batch
# verdict stands (logged, so a capped sweep is diagnosable).
_VERIFY_CAP = 12


# A rejection that is ABOUT the listing's location (vs brand/price/spec). Used to decide when
# the deterministic gazetteer is allowed to overturn the judge.
_LOCATION_REASON_RE = re.compile(
    r"\b(location|too far|not (?:in|near)|is [A-Z][a-z]+ not|"
    r"out(?:side)?(?: of)? (?:the |stated |the stated )?(?:area|radius|region|state)|"
    r"beyond (?:the |stated |the stated )?(?:area|radius|region)|"
    r"miles? (?:away|from)|wrong (?:area|city|town|state)|different (?:city|state))\b", re.IGNORECASE)

# A rejection that is ABOUT the price. Appealable only when the watch never stated one.
# 'price mismatch' is here because marketplace cards show a struck-through OLD price next to
# the current one (a price DROP) — the judge reads the two numbers as an inconsistency and
# rejects a perfectly good listing for a display artifact.
_PRICE_REASON_RE = re.compile(
    r"\b(over ?priced|over (?:the |stated |the stated )?budget|price (?:is )?too high|"
    r"too expensive|above (?:the |stated |the stated )?(?:budget|price)|"
    r"outside (?:the |stated |the stated )?budget|exceeds? (?:the )?budget|"
    r"price (?:mismatch|does ?n[o']t match|discrepanc|inconsisten))", re.IGNORECASE)


def _watch_states_price(watch) -> bool:
    """Did the user actually write a price constraint into this watch? Only then is price a
    legitimate ground for rejection."""
    text = f"{getattr(watch, 'instruction', '') or ''} {getattr(watch, 'judgment_prompt', '') or ''}"
    return bool(re.search(
        r"[$€£]\s?\d|\b\d{1,3}k\b|\b(?:under|below|budget|max(?:imum)?\s+price|"
        r"less than|no more than|cheap)\b", text, re.IGNORECASE))


def _appealable_rejection(watch, why: str, title: str) -> str:
    """'' when a judge's rejection stands; otherwise a short tag naming why it deserves a
    second look. Two appealable grounds, both deterministic:

      geo   — the reason is about LOCATION and the gazetteer places the listing within
              range. Models are bad at geography; the map is not.
      price — the reason is about PRICE and the watch never stated a price. The rubric says
              'never invent a budget', but the model still writes "Price too high" against
              nothing (observed twice on the same $21,500 boat) — prompts ask, this enforces.

    Any OTHER failure cited alongside (wrong brand, parts, toy…) makes the rejection stand:
    the appeal exists for criteria the judge had no basis to apply, never to soften real ones.
    """
    why = why or ""
    rest, grounds = why, []
    if _LOCATION_REASON_RE.search(why):
        miles = _listing_miles(watch, title or "")
        if miles is None or miles > _NEAR_MILES:
            return ""                      # can't clear it, or genuinely far — stands
        grounds.append(f"geo {miles:.0f}mi")
        rest = _LOCATION_REASON_RE.sub(" ", rest)
    if _PRICE_REASON_RE.search(rest):
        if _watch_states_price(watch):
            return ""                      # the user DID state a price — stands
        grounds.append("no stated budget")
        rest = _PRICE_REASON_RE.sub(" ", rest)
    if not grounds or _FAILING_REASON_RE.search(rest):
        return ""
    return ", ".join(grounds)


def _listing_miles(watch: Watch, text: str) -> float | None:
    """Straight-line miles from the watch's anchor to the 'City, ST' in a listing's text, or
    None when either side can't be located. Deterministic — this is the gazetteer speaking,
    not a model guessing."""
    try:
        anchor = _watch_geolocation(watch)
        if not anchor:
            return None
        from web_watcher.cl_geo import (parse_city_state, place_latlon,
                                        place_latlon_in_state, miles_between)
        cs = parse_city_state(text or "")
        if not cs:
            return None
        city, st = cs
        words = re.findall(r"[A-Za-z][A-Za-z.'\-]*", city)
        for n in range(min(3, len(words)), 0, -1):
            name = " ".join(words[-n:])
            # State-qualified first ('Miami, FL' is exact); anchor-disambiguated second.
            ll = place_latlon_in_state(name, st) or place_latlon(name, anchor)
            if ll:
                return miles_between(anchor, ll)
    except Exception:
        pass
    return None


# Matches the out-of-area prefilter's default: past this, the prefilter would have dropped the
# listing before any judge saw it — so anything nearer is, by the watch's own screening
# standard, in range.
_NEAR_MILES = 200.0


def _geo_fact(watch: Watch, text: str) -> str:
    """A one-line, deterministic location fact for the judge prompt — or ''.

    The judge kept treating 'near Anacortes' as 'equals Anacortes': a MacGregor in Seattle
    (62 straight-line miles) was removed as 'Location is Seattle not Anacortes', exactly the
    reasoning that killed the Puyallup and Ocean Shores boats on the Facebook run. Models are
    bad at geography; the bundled gazetteer is not. So when we can compute the distance, we
    STATE it — and state the verdict that follows from it, so there is nothing left for the
    model to guess about."""
    miles = _listing_miles(watch, text)
    if miles is None:
        return ""
    if miles <= _NEAR_MILES:
        return (f"LOCATION FACT (computed, trust it): this listing is about {miles:.0f} miles "
                f"from the user's area — WITHIN range. Do not reject it for location.")
    return (f"LOCATION FACT (computed, trust it): this listing is about {miles:.0f} miles "
            f"from the user's area — outside range.")


def _verify_kept_listings(kept: list, watch: Watch, cfg: AppConfig, threshold: int) -> list:
    """Pass 2 of the judge: re-ask about each KEPT listing on its own.

    One listing per call, so a verdict cannot land on the wrong row (the batch pass judges by
    index over a numbered list, and small models drift — a C&C 25 arrived carrying the J30's
    reason). The question is also sharper than the batch one: not "rate these 20", but "does
    THIS listing satisfy THESE criteria" — which is exactly the question the user cares about,
    asked the way the Vet button asks it.

    Demotes (never promotes): a listing the verifier rejects drops below threshold with the
    verifier's reason attached; one it confirms keeps its batch rating. On any per-listing error
    the batch verdict stands — this pass only ever REMOVES false positives, so its failure mode
    is 'no worse than before'."""
    if not kept:
        return kept
    to_check, over_cap = kept[:_VERIFY_CAP], kept[_VERIFY_CAP:]
    if over_cap:
        log.info("Verify pass: %d listing(s) over the cap of %d for %r — batch verdict stands "
                 "for the overflow", len(over_cap), _VERIFY_CAP, watch.name)
    confirmed = []
    for l in to_check:
        why_ver = ""
        try:
            details = (getattr(l, "details", "") or "")[:900]
            body = f"{l.title or ''} {l.price or ''}"
            if details:
                body += f"\nAD DETAILS: {details}"
            geo = _geo_fact(watch, l.title or "")
            content = llm.chat(
                [
                    {"role": "system", "content":
                        "You verify ONE marketplace listing against a user's criteria. Answer "
                        "strictly from what the listing says. A listing that fails ANY stated "
                        "requirement (brand/model, item type, location, price, spec) is NOT a "
                        "match — being a real, nice example of the general category is not "
                        "enough.\n"
                        "Judge ONLY requirements the user actually stated: if they gave no "
                        "price limit, price is not a criterion; if they gave no location, "
                        "location is not a criterion. Never invent a budget or a radius.\n"
                        "'Near <place>' means the surrounding region, not that exact town — a "
                        "listing one or two towns over still qualifies. When a LOCATION FACT "
                        "line is present it was computed from map data: it OVERRIDES your own "
                        "geography.\n"
                        "Return ONLY JSON: "
                        '{"match": true|false, "why": "<10 words or fewer>"}'},
                    {"role": "user", "content":
                        f"Criteria: {watch.instruction}\n{watch.judgment_prompt or ''}\n"
                        + (f"{geo}\n" if geo else "")
                        + f"\nListing:\n{body}\n\nDoes this listing satisfy the criteria?"},
                ],
                role="judge",
                local_model=cfg.models.effective_council_model,
                cfg=cfg, format_json=True, cache_system=True, timeout=45.0,
            )
            data = json.loads(content)
            raw_match = data.get("match") if isinstance(data, dict) else None
            if raw_match is None:
                # No verdict in the reply at all → INCONCLUSIVE, not a rejection. Only an
                # explicit false may remove a listing; a malformed reply keeps the batch verdict.
                confirmed.append(l)
                continue
            ok = bool(raw_match)
            why_ver = str(data.get("why", "")).strip()
            # The verifier contradicting itself gets the same deterministic rule as the batch.
            if ok and _FAILING_REASON_RE.search(why_ver):
                ok = False
            # Deterministic facts beat model vibes: a rejection on grounds the judge had no
            # basis for — geography the gazetteer contradicts (the prefilter already screened
            # confidently-far listings before any judge saw this one), or a budget the user
            # never stated — is overturned. Real failures (brand, type, parts) always stand.
            if not ok:
                tag = _appealable_rejection(watch, why_ver, l.title or "")
                if tag:
                    log.info("   verify OVERTURNED (%s): %s — judge said %r",
                             tag, (l.title or "")[:50], why_ver[:60])
                    ok, why_ver = True, ""
            if not ok:
                l.rating = threshold - 1
                l.judge_reason = f"verify: {why_ver or 'does not satisfy the criteria'}"
                log.info("   verify REMOVED: %s %s — %s",
                         (l.title or "")[:60], l.price, l.judge_reason[:70])
                try:
                    from web_watcher import issues
                    issues.record_issue("false_positive", watch.name,
                                        f"batch judge wrongly kept {(l.title or '')[:60]!r} — "
                                        f"verify caught it: {why_ver[:80]}")
                except Exception:
                    pass
                continue
            if why_ver:
                l.judge_reason = why_ver
        except Exception as exc:
            log.debug("verify pass errored on %r (%s) — batch verdict stands",
                      (l.title or "")[:50], exc)
        confirmed.append(l)
    if len(confirmed) != len(to_check):
        log.info("Verify pass confirmed %d/%d kept listing(s) for %r",
                 len(confirmed), len(to_check), watch.name)
    return confirmed + over_cap


def _alert_new_listings(
    watch: Watch, cfg: AppConfig, listings: list, run_ts: str, db_path: Optional[Path] = None,
) -> int:
    """
    Send a notification per new listing, capped at watch.continuous_max_alerts and
    paced to respect Telegram rate limits. Overflow is summarised in one extra alert.

    Crash-safety: a listing is recorded as 'seen' only AFTER its alert attempt
    returns without raising. If the process dies mid-send, the listing is NOT marked
    seen and re-surfaces next sweep, so a new listing is never silently swallowed.
    Returns the number of listings individually alerted.
    """
    from datetime import datetime as _dt

    ts = _dt.fromisoformat(run_ts)

    def _mark_seen(l) -> None:
        save_seen_listing(watch.name, l.key, run_ts, summary=l.title, link=l.url, db_path=db_path)

    # Watch has notifications fully disabled: nothing to send, but still record the
    # listings so we don't reconsider them forever.
    if not (watch.notify.telegram or watch.notify.email):
        for l in listings:
            _mark_seen(l)
        return 0

    cap   = max(1, watch.continuous_max_alerts)
    # Best-rated finds first, so the per-sweep cap never truncates a 5-star deal in favor
    # of a barely-passing 3. Stable sort keeps discovery order within a rating tier.
    listings = sorted(listings, key=lambda l: getattr(l, "rating", 0) or 0, reverse=True)
    head  = listings[:cap]
    extra = listings[cap:]
    sent  = 0

    _stars = lambda r: ("★" * r + "☆" * (5 - r)) if r else ""

    for l in head:
        # Keep the summary RAW here — each notification channel escapes for its own format
        # (email HTML, Telegram HTML). Pre-escaping here caused Telegram to double-escape.
        title = l.title or "(listing)"
        price = l.price or ""
        rating = getattr(l, "rating", None)
        why    = getattr(l, "judge_reason", "") or ""
        star_prefix = f"{_stars(rating)} " if rating else ""
        summary = f"{star_prefix}New match: {title}" + (f" — {price}" if price else "")
        if why:
            summary += f"\n{why}"      # the judge's one-line verdict, in the alert
        result = ReasoningResult(found=True, summary=summary, confidence="high", link=l.url)
        payload = NotificationPayload(watch_name=watch.name, result=result, timestamp=ts)
        try:
            send_notifications(
                payload, cfg.notifications,
                use_telegram=watch.notify.telegram, use_email=watch.notify.email,
                owner_chat_id=getattr(watch, "owner", "") or "",
            )
            _mark_seen(l)   # only after a send attempt that didn't raise
            # Distinct from 'seen': this one was actually PUSHED. A baseline marks its
            # matches seen so they don't fire a wall of alerts, so 'seen' alone can never
            # tell "we told you" from "we deliberately didn't".
            try:
                from web_watcher.storage import mark_alerted
                mark_alerted(watch.id or watch.name, l.key, db_path=db_path)
            except Exception:
                pass
            sent += 1
        except Exception as exc:
            log.warning("Alert send failed for %r (%s) — will retry next sweep", watch.name, exc)
        time.sleep(_ALERT_PACE_SECONDS)

    if extra:
        summary = f"+{len(extra)} more new listing(s) this sweep (showing first {len(head)})."
        result = ReasoningResult(found=True, summary=summary, confidence="medium", link=watch.urls[0])
        payload = NotificationPayload(watch_name=watch.name, result=result, timestamp=ts)
        try:
            send_notifications(
                payload, cfg.notifications,
                use_telegram=watch.notify.telegram, use_email=watch.notify.email,
                owner_chat_id=getattr(watch, "owner", "") or "",
            )
            # Overflow items were surfaced via the summary — record them so they don't
            # re-summarise every sweep.
            for l in extra:
                _mark_seen(l)
        except Exception as exc:
            log.warning("Overflow alert send failed for %r (%s) — will retry next sweep", watch.name, exc)

    return sent


# A Facebook watch that hit a checkpoint is put on a cooldown so we don't keep poking a
# flagged account every idle cycle. Keyed by watch name; value is the epoch until which
# the watch's Facebook sweeps are skipped.
_FB_COOLDOWN: dict[str, float] = {}
_FB_COOLDOWN_SECONDS = 6 * 3600   # 6 hours — long enough for a soft flag to clear


def _fb_on_cooldown(watch_name: str) -> bool:
    return time.time() < _FB_COOLDOWN.get(watch_name, 0)


def _handle_fb_checkpoint(watch: Watch, cfg: AppConfig, run_ts: str, db_path: Optional[Path],
                          reason: str) -> None:
    """Facebook threw a security checkpoint / block / CAPTCHA. STOP (never solve it), HALT all
    Facebook activity app-wide until a human clears it, and alert the user once.

    The halt (not the old auto-expiring per-watch cooldown) is the real protection: the flag is
    on the ACCOUNT, so pausing one watch while another keeps browsing is how a soft flag becomes
    a ban — and a timer that resumes on its own resumes without anyone having checked. The
    cooldown is kept as a secondary belt for the case where the halt file can't be written."""
    from datetime import datetime as _dt
    fb_safety.engage_halt(reason, watch.name)
    _FB_COOLDOWN[watch.name] = time.time() + _FB_COOLDOWN_SECONDS
    log.warning("Facebook checkpoint on %r (%s) — ALL Facebook activity halted until cleared",
                watch.name, reason)

    last = get_last_run(watch.name, db_path)
    already = bool(last and last.get("error") and "checkpoint" in (last["error"] or "").lower())
    _save_error(watch.name, run_ts, f"facebook checkpoint: {reason}", db_path,
                perception_mode="continuous-agent")

    if not already and (watch.notify.telegram or watch.notify.email):
        msg = (f"⛔ Facebook STOPPED — '{watch.name}' hit a security check ({reason}).\n\n"
               "I did NOT try to solve it; that's what protects the account. ALL Facebook "
               "watching is now paused and will stay paused until you say so — it will not "
               "resume on its own, even if the app restarts.\n\n"
               "What to do: open Facebook yourself (Settings → Connect Facebook), clear the "
               "check, make sure the account looks healthy, then press "
               "\"Resume Facebook\" in Settings.")
        result = ReasoningResult(found=True, summary=msg, confidence="high", link=watch.urls[0])
        payload = NotificationPayload(watch_name=watch.name, result=result, timestamp=_dt.fromisoformat(run_ts))
        try:
            send_notifications(payload, cfg.notifications,
                               use_telegram=watch.notify.telegram, use_email=watch.notify.email,
                               owner_chat_id=getattr(watch, "owner", "") or "")
        except Exception as exc:
            log.warning("Checkpoint notification failed for %r: %s", watch.name, exc)


def _handle_login_wall(watch: Watch, cfg: AppConfig, run_ts: str, db_path: Optional[Path]) -> None:
    """
    A use_login_profile watch hit a login wall (session expired / logged out).
    Record an error and notify the user ONCE (throttled by checking the last run)
    so they can reconnect — never attempt to log in automatically.
    """
    from datetime import datetime as _dt
    msg = (f"'{watch.name}' could not access the site — the saved login looks expired. "
           "Open the dashboard and use 'Connect Facebook' to sign in again.")
    log.warning("Login wall for %r — saved session appears expired", watch.name)

    last = get_last_run(watch.name, db_path)
    already_warned = bool(last and last.get("error") and "login" in (last["error"] or "").lower())

    _save_error(watch.name, run_ts, "login required — reconnect", db_path, perception_mode="continuous")

    if not already_warned and (watch.notify.telegram or watch.notify.email):
        result = ReasoningResult(found=True, summary=msg, confidence="high", link=watch.urls[0])
        payload = NotificationPayload(watch_name=watch.name, result=result, timestamp=_dt.fromisoformat(run_ts))
        try:
            send_notifications(payload, cfg.notifications,
                               use_telegram=watch.notify.telegram, use_email=watch.notify.email,
                               owner_chat_id=getattr(watch, "owner", "") or "")
        except Exception as exc:
            log.warning("Login-wall notification failed for %r: %s", watch.name, exc)


# ---------------------------------------------------------------------------
# Pipeline — module-level so APScheduler can pickle the job reference
# ---------------------------------------------------------------------------

def _execute_watch(
    watch_name:  str,
    config_path: Optional[Path],
    db_path:     Optional[Path],
) -> None:
    """
    Full watch pipeline. Any unhandled exception is caught here so the
    scheduler thread stays alive and the next run fires on schedule.
    """
    log.info("Running watch %r", watch_name)
    run_ts = datetime.now(timezone.utc).isoformat()

    try:
        cfg   = load_config(config_path)
        watch = next((w for w in cfg.watches if w.name == watch_name), None)
        if watch is None:
            log.warning("Watch %r not found in config — skipping", watch_name)
            return
        if not watch.enabled:
            log.info("Watch %r is disabled — skipping", watch_name)
            return

        # Goal watches (restock, …) run a lightweight condition check, not the listings
        # pipeline. Listings is just the default template.
        if watch.goal_kind:
            _run_goal_check(watch, cfg, run_ts, db_path)
            return

        _run_pipeline(watch, cfg, run_ts, db_path)

    except Exception as exc:
        log.error("Unhandled error in watch %r: %s", watch_name, exc, exc_info=True)
        _save_error(watch_name, run_ts, str(exc), db_path)


def _run_goal_check(
    watch:   Watch,
    cfg:     AppConfig,
    run_ts:  str,
    db_path: Optional[Path],
) -> None:
    """Run one goal-watch check (currently: restock) and alert on the CONDITION FLIP — e.g.
    a size going out-of-stock → IN STOCK — using the best signal the site offers (a Shopify
    data endpoint here). Remembers the last state so it never re-alerts on an already-true
    condition, and records a run either way for the health line."""
    from web_watcher import goalwatch
    from web_watcher.storage import get_goal_state, save_goal_state
    key = watch.id or watch.name
    url = watch.urls[0] if watch.urls else ""

    if watch.goal_kind == "restock":
        res = goalwatch.check_restock(url, cfg, size_text=watch.target_size)
    else:
        log.warning("Unknown goal_kind %r for %r", watch.goal_kind, watch.name)
        return

    if not res.get("ok"):
        # Couldn't determine the state (size not found, non-Shopify, network) — record the
        # note, don't touch the remembered state, don't alert.
        log.info("Goal check %r: %s", watch.name, res.get("note"))
        save_run(RunRecord(watch.name, run_ts, found=False, summary=res.get("note", ""),
                           link=url, confidence="low"), db_path)
        return

    now_available = bool(res["available"])
    prev = get_goal_state(key, db_path) or {}
    was_available = bool(prev.get("available"))
    became_available = now_available and not was_available   # the flip (and first-check-in-stock)

    save_goal_state(key, {"available": now_available, "note": res.get("note", "")}, run_ts, db_path)
    save_run(RunRecord(watch.name, run_ts, found=became_available, summary=res.get("note", ""),
                       link=url, confidence="high"), db_path)
    log.info("Goal check %r: %s%s", watch.name, res.get("note", ""),
             "  → ALERTING (back in stock!)" if became_available else "")

    if became_available and (watch.notify.telegram or watch.notify.email):
        summary = f"BACK IN STOCK: {res.get('variant_title') or watch.target_size}"
        if res.get("price"):
            summary += f" — {res['price']}"
        summary += f"\n{res.get('note', '')}"
        result = ReasoningResult(found=True, summary=summary, confidence="high", link=url)
        payload = NotificationPayload(watch_name=watch.name, result=result, timestamp=run_ts)
        try:
            send_notifications(payload, cfg.notifications,
                               use_telegram=watch.notify.telegram, use_email=watch.notify.email,
                               owner_chat_id=getattr(watch, "owner", "") or "")
        except Exception as exc:
            log.warning("Restock alert send failed for %r (%s)", watch.name, exc)


def _run_pipeline(
    watch:   Watch,
    cfg:     AppConfig,
    run_ts:  str,
    db_path: Optional[Path],
) -> None:
    need_screenshot = watch.perception in ("vision", "auto")
    text_model = watch.model_override or cfg.models.text_model

    # 1. Browser (standard or autonomous agent)
    if watch.autonomous:
        agent_tuples  = _run_agent_browse(watch, cfg, text_model)
        page_results  = [t[0] for t in agent_tuples]
        scratchpads   = {t[0].url: t[1] for t in agent_tuples}
    else:
        with BrowserSession(
            headless=cfg.browser.headless,
            stealth=cfg.browser.stealth,
            geolocation=_watch_geolocation(watch),
        ) as session:
            page_results = session.run_watch(watch, screenshot=need_screenshot)
        scratchpads = {}

    for page_result in page_results:
        scratchpad = scratchpads.get(page_result.url, {})
        if page_result.error:
            log.warning("Browser error for %r / %s: %s", watch.name, page_result.url, page_result.error)
            _save_error(watch.name, run_ts, f"browser: {page_result.error}", db_path,
                        perception_mode="text")
            continue

        # Guard: empty page — agent got blocked or page closed before content loaded
        if not (page_result.text or "").strip() and not page_result.screenshot_bytes:
            log.warning("Watch %r: page is empty (agent was likely blocked or page closed) — skipping reasoner", watch.name)
            _save_error(watch.name, run_ts, "empty page — agent blocked or page closed", db_path,
                        perception_mode="text")
            continue

        # 2. Perception
        percept = perceive(page_result, watch)
        log.debug(
            "Watch %r: perception=%s heuristic_passed=%s",
            watch.name, percept.mode_used, percept.heuristic_passed,
        )

        # 3. Reasoning / Judgment
        text_model   = (watch.model_override or cfg.models.text_model)
        vision_model = (watch.model_override or cfg.models.vision_model)
        reasoner = Reasoner(text_model=text_model, vision_model=vision_model)

        try:
            if watch.judgment_prompt and scratchpad:
                # Judgment step: apply custom reasoning criteria to gathered facts
                log.info("Watch %r: running judgment step with %d scratchpad entries",
                         watch.name, len(scratchpad))
                result = _run_judgment(
                    scratchpad       = scratchpad,
                    page_text        = percept.text or "",
                    judgment_prompt  = watch.judgment_prompt,
                    model            = cfg.models.effective_council_model,
                    url              = page_result.url,
                )
            elif percept.mode_used == "vision":
                if not vision_model:
                    log.warning(
                        "Watch %r needs vision but no vision_model is configured — "
                        "falling back to text", watch.name
                    )
                    result = reasoner.analyse_text(
                        percept.text or "", watch.instruction, page_result.url
                    )
                else:
                    result = reasoner.analyse_image(
                        percept.image_bytes or b"", watch.instruction, page_result.url
                    )
            else:
                result = reasoner.analyse_text(
                    percept.text or "", watch.instruction, page_result.url
                )
        except OllamaUnavailableError as exc:
            log.error("Ollama unavailable for watch %r: %s", watch.name, exc)
            _save_error(watch.name, run_ts, f"ollama: {exc}", db_path,
                        perception_mode=percept.mode_used)
            continue

        log.info(
            "Watch %r: found=%s confidence=%s summary=%r",
            watch.name, result.found, result.confidence, result.summary[:80],
        )

        # 4. Screenshot persistence (save only if vision path was used and match found)
        screenshot_path: Optional[str] = None
        if percept.mode_used == "vision" and percept.image_bytes and result.found:
            screenshot_path = _save_screenshot(watch.name, run_ts, percept.image_bytes)

        # 5. Notify
        if result.found:
            notify_cfg  = watch.notify
            payload = NotificationPayload(
                watch_name=watch.name,
                result=result,
                timestamp=datetime.fromisoformat(run_ts),
                screenshot_bytes=percept.image_bytes if percept.mode_used == "vision" else None,
            )
            send_notifications(
                payload, cfg.notifications,
                use_telegram=notify_cfg.telegram,
                use_email=notify_cfg.email,
                owner_chat_id=getattr(watch, "owner", "") or "",
            )

        # 6. Storage
        record = RunRecord(
            watch_name=watch.name,
            run_timestamp=run_ts,
            found=result.found,
            summary=result.summary,
            link=result.link,
            confidence=result.confidence,
            perception_mode_used=percept.mode_used,
            error=result.error,
            screenshot_path=screenshot_path,
        )
        save_run(record, db_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _save_error(
    watch_name:      str,
    run_ts:          str,
    error_msg:       str,
    db_path:         Optional[Path],
    perception_mode: str = "text",
) -> None:
    save_run(RunRecord(
        watch_name=watch_name,
        run_timestamp=run_ts,
        found=False,
        error=error_msg,
        perception_mode_used=perception_mode,
    ), db_path)


def _run_agent_browse(
    watch:      "Watch",
    cfg:        "AppConfig",
    model:      str,
) -> list[tuple]:
    """
    Run the autonomous agent for every URL in the watch.
    Returns a list of (PageResult, scratchpad_dict) tuples.
    """
    from web_watcher.agent import run_agent
    from web_watcher.browser import BrowserSession, PageResult

    results = []
    with BrowserSession(
        headless=cfg.browser.headless,
        stealth=cfg.browser.stealth,
        geolocation=_watch_geolocation(watch),
    ) as session:
        for url in watch.urls:
            page = session.new_page()
            try:
                log.info("Agent browse starting: %s", url)
                page.goto(url, timeout=30_000, wait_until="domcontentloaded")
                agent_result = run_agent(
                    page,
                    instruction   = watch.instruction,
                    model         = model,
                    max_steps     = watch.max_agent_steps,
                    council_model = cfg.models.effective_council_model,
                    # Vision re-enabled with qwen2.5vl:7b — strong grounding/OCR,
                    # unlike llava which gave vague/wrong descriptions. Empty string
                    # disables it; None falls back to the configured vision model.
                    vision_model  = cfg.models.vision_model or None,
                    ocr_threshold = cfg.models.ocr_threshold,
                )
                if agent_result.scratchpad:
                    log.info("Agent scratchpad: %s", agent_result.scratchpad)
                results.append((
                    PageResult(url=page.url, text=agent_result.final_text),
                    agent_result.scratchpad,
                ))
                log.info("Agent browse complete (%d steps): %s", agent_result.steps_taken, page.url)
            except Exception as exc:
                log.error("Agent browse error for %s: %s", url, exc)
                results.append((PageResult(url=url, error=str(exc)), {}))
            finally:
                page.close()
    return results


def _run_judgment(
    scratchpad:      dict,
    page_text:       str,
    judgment_prompt: str,
    model:           str,
    url:             str,
) -> "ReasoningResult":
    """
    Post-browse judgment step. Uses the agent's scratchpad (facts gathered across
    pages) plus the final page text and a user-defined judgment prompt to produce
    a structured found/summary/confidence verdict.

    Uses the council model (mixtral by default) for better multi-step reasoning.
    """
    import httpx
    from web_watcher.reasoning import ReasoningResult

    if not scratchpad:
        log.warning("Judgment requested but scratchpad is empty — agent collected no data")
        return ReasoningResult(
            found=False,
            confidence="low",
            summary="Agent did not collect any data during browsing (scratchpad is empty). No facts to judge.",
            link=None,
        )

    OLLAMA_URL     = "http://localhost:11434"
    OLLAMA_TIMEOUT = 90.0

    mem_text = "\n".join(f"  {k}: {v}" for k, v in scratchpad.items())
    page_snippet = " ".join(page_text.split())[:1000]

    system_prompt = """\
You are a research analyst making a structured judgment based on gathered facts.

You will be given:
- Facts the agent collected (working memory scratchpad)
- A snippet of the final page
- Judgment criteria from the user

Output ONLY a JSON object — no other text:
{
  "found":      true | false,
  "confidence": "high" | "medium" | "low",
  "summary":    "<detailed explanation of your verdict, 2-4 sentences>",
  "link":       "<relevant URL or null>"
}

'found' should be true if the judgment criteria are met (e.g. it IS a good deal).
Be specific in the summary — include the key numbers and reasoning.
"""

    user_msg = (
        f"URL: {url}\n\n"
        f"Agent's collected facts:\n{mem_text or '  (none saved)'}\n\n"
        f"Final page excerpt:\n{page_snippet}\n\n"
        f"Judgment criteria:\n{judgment_prompt}\n\n"
        f"Apply the criteria to the facts and give your verdict."
    )

    try:
        payload = {
            "model":    model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_msg},
            ],
            "stream": False,
            "format": "json",
        }
        with httpx.Client(timeout=OLLAMA_TIMEOUT) as client:
            r = client.post(f"{OLLAMA_URL}/api/chat", json=payload)
            r.raise_for_status()
        data = json.loads(r.json()["message"]["content"])
        result = ReasoningResult(
            found      = bool(data.get("found", False)),
            confidence = data.get("confidence", "medium"),
            summary    = data.get("summary", ""),
            link       = data.get("link"),
        )
        log.info("Judgment: found=%s confidence=%s summary=%r",
                 result.found, result.confidence, result.summary[:80])
        return result
    except Exception as exc:
        log.error("Judgment step failed: %s", exc)
        return ReasoningResult(
            found=False, confidence="low",
            summary=f"Judgment step failed: {exc}", link=None, error=str(exc),
        )


def _save_screenshot(watch_name: str, run_ts: str, image_bytes: bytes) -> Optional[str]:
    try:
        safe_name = re.sub(r"[^\w\-]", "_", watch_name)
        safe_ts   = run_ts.replace(":", "").replace(".", "")[:15]
        filename  = f"{safe_name}_{safe_ts}.png"
        path      = SCREENSHOTS_DIR / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(image_bytes)
        return str(path)
    except Exception as exc:
        log.warning("Could not save screenshot for %r: %s", watch_name, exc)
        return None
