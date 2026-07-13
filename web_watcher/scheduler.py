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
    extract_listings, extract_listing_body, extract_listing_posted_at, vary_search,
    human_scroll, is_login_wall, dismiss_popups, humanized_search,
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
    find_duplicate,
    list_site_profiles,
)
from web_watcher.monitor import parse_listing_attributes, listing_fingerprint
from web_watcher import fb_safety

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

# When silently baselining a big backlog (first sweep, or a flood), still JUDGE up to this
# many so the matches show in Results — capped to keep the single judge call fast/accurate.
_BASELINE_JUDGE_CAP = 60


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
        log.info("Continuous watch %r started", watch_name)
        return True

    def stop_continuous(self, watch_name: str) -> bool:
        """Signal a continuous watch to stop and wait briefly for it. Returns True if it was running."""
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
        """Stop every running continuous loop (public; used by the FB connect flow)."""
        self._stop_all_continuous()

    def _stop_all_continuous(self) -> None:
        names = list(self._continuous_threads.keys())
        for name in names:
            self.stop_continuous(name)

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
                # Continuous watches do NOT auto-start on launch — they open a browser
                # and run all day, so the user opts in via the dashboard Start button.
                # (reload() separately restores any that were already running.)
                log.info("Continuous watch %r registered (stopped) — start it from the dashboard",
                         watch.name)
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


def _open_continuous_browser(old: Optional["BrowserSession"], watch: Watch, cfg: AppConfig):
    """
    Open a fresh persistent browser session + page for the continuous loop, closing
    any prior one first. Returns (session, page). Kept open across sweeps so the watch
    is one stable window instead of flickering open/closed each sweep.
    """
    _close_continuous_browser(old)
    session = BrowserSession(
        headless    = cfg.browser.headless,
        stealth     = cfg.browser.stealth,
        persistent  = watch.use_login_profile,
        profile_dir = cfg.browser.profile_dir,
        show_cursor = cfg.browser.show_agent_cursor,
    )
    session.__enter__()
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

    # Facebook cooldown: after a checkpoint we back off this watch's FB sweeps for hours
    # instead of poking a flagged account every idle cycle.
    if fb_safety.is_facebook(plan["start_url"]) and _fb_on_cooldown(watch.name):
        log.info("Continuous agent sweep %d for %r: Facebook on cooldown — skipping",
                 sweep_index, watch.name)
        return

    # Accumulate listings across every page the agent visits, keeping the richest
    # title per stable key. Dedup vs seen-state happens later in the shared pipeline.
    # Learned site profiles let the extractor key listings on sites beyond the 3 built-ins.
    harvested: dict = {}
    profiles = list_site_profiles(db_path)

    def _harvest(pg) -> None:
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

    try:
        # Sometimes land on the homepage first (like a person) before the deep search URL,
        # so we're not always teleporting straight to a results page — a subtle bot tell.
        maybe_warm_homepage(page, plan["start_url"])
        page.goto(plan["start_url"], timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
    except Exception as exc:
        log.warning("Continuous agent sweep %d: navigation failed for %s: %s",
                    sweep_index, plan["start_url"], exc)
        _save_error(watch.name, run_ts, f"navigation: {exc}", db_path,
                    perception_mode="continuous-agent")
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
    try:
        run_agent(
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
        )
    except Exception as exc:
        # Process whatever we harvested before the error rather than losing the sweep.
        log.error("Continuous agent sweep %d for %r errored mid-browse: %s",
                  sweep_index, watch.name, exc)

    # The agent stopped on a Facebook checkpoint mid-browse → alert + back off, and do
    # NOT process/alert listings from a flagged session.
    if _checkpoint_hit["reason"]:
        _handle_fb_checkpoint(watch, cfg, run_ts, db_path, _checkpoint_hit["reason"])
        return

    listings = list(harvested.values())
    log.info("Continuous agent sweep %d for %r: harvested %d unique listing(s) while browsing",
             sweep_index, watch.name, len(listings))
    _process_sweep_listings(watch, cfg, db_path, sweep_index, listings, run_ts,
                            mode_label="continuous-agent",
                            page=page, fetch_bodies=bool(watch.judgment_prompt),
                            stop_event=stop_event)


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

    # Humanize: when stealth is on, TYPE the search into the box (real keyboard + mouse)
    # rather than jumping straight to the query URL — looks like a person, no LLM. Falls
    # back to a direct navigation when there's no search term or no box (or stealth off).
    typed = False
    if cfg.browser.stealth:
        try:
            typed = humanized_search(page, url)
        except Exception as exc:
            log.debug("humanized_search errored, will goto directly: %s", exc)
    if not typed:
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

    # Deep-read is decoupled from the agent: read new ads' attributes whenever there's a
    # judgment_prompt to consume them, regardless of how the listings were gathered.
    _process_sweep_listings(watch, cfg, db_path, sweep_index, listings, run_ts,
                            page=page, fetch_bodies=bool(watch.judgment_prompt),
                            stop_event=stop_event)
    return len(listings)


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
            time.sleep(random.uniform(1.5, 4.0))
        tab = None
        try:
            tab = ctx.new_page()
            tab.goto(l.url, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
            dismiss_popups(tab, settle_ms=0)
            l.details = extract_listing_body(tab)
            l.posted_at = extract_listing_posted_at(tab)
            fetched += 1
            time.sleep(random.uniform(0.8, 2.0))   # "read" the ad before closing the tab
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
        except Exception as exc:
            log.debug("Persist listing %s failed: %s", l.key, exc)


def _baseline_batch(watch, cfg, batch: list, run_ts: str, db_path, mode_label: str, verb: str) -> None:
    """Silently baseline a large batch (first sweep, or a flood-guard trip): mark the WHOLE
    batch seen so we never notify on pre-existing backlog — but still JUDGE a capped slice and
    record its matches so they populate Results. This is the difference between a new watch
    looking broken (hundreds found, nothing shown) and it showing real matches from the start,
    just without a wall of notifications."""
    to_judge = batch[:_BASELINE_JUDGE_CAP]
    # Keyword prefilter first (free) — even on the baseline, so parts/salvage never reach
    # the LLM and are recorded as keyword-excluded non-matches.
    kw_kept, kw_dropped = _keyword_prefilter(to_judge, watch)
    matched = kw_kept
    if kw_kept and (watch.judgment_prompt or (watch.instruction or "").strip()):
        # Card-level judge (no deep-read) — one LLM call for the slice; keeps priming cheap.
        # Runs against the instruction even without an explicit judgment_prompt, so a plain
        # watch doesn't baseline EVERYTHING as a match (the "everything is a match" bug).
        matched = _filter_listings_by_judgment(kw_kept, watch, cfg)
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
        _baseline_batch(watch, cfg, listings, run_ts, db_path, mode_label, "primed")
        return

    # Flood guard: an implausibly large "new" batch almost always means a baseline gap (a
    # thin first sweep, a rotated search term's fresh backlog, or the feed restructured)
    # rather than a genuine burst. Baseline it silently — but, like priming, still judge +
    # record matches to Results; just no notifications for what is really pre-existing stock.
    if len(new_listings) >= _FLOOD_REBASELINE_THRESHOLD:
        _baseline_batch(watch, cfg, new_listings, run_ts, db_path, mode_label, "re-baselined")
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

    # Cheap keyword prefilter FIRST (free; before any deep-read or LLM): drop listings
    # with an antikeyword / missing a required keyword. Dropped ones are still recorded
    # (as non-matches, with the reason) so they show in the log/Results, just not alerted.
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
    # Persist keyword-dropped listings too (non-match), so they're recorded, not lost.
    fresh = fresh + kw_dropped

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
    "Rate how well each listing matches the user's criteria on a 1-5 scale:\n"
    "  1 = No match: wrong item/category/brand, or looks like spam/a scam.\n"
    "  2 = Weak: missing essential info (condition, model, key spec) or barely relevant.\n"
    "  3 = Acceptable: matches the basics but with some mismatch or missing detail.\n"
    "  4 = Good match: clearly meets the criteria with relevant details.\n"
    "  5 = Great deal: fully matches with excellent condition and/or price."
)


def _keyword_prefilter(listings: list, watch: Watch) -> tuple[list, list]:
    """Cheap, deterministic keyword gate run BEFORE the LLM judge (free; cuts GPU load and
    false alerts). Returns (kept, dropped). A listing is dropped if it contains ANY
    antikeyword, or (when keywords are set) contains NONE of them. Matches case-insensitively
    over the title + ad body. Each dropped listing gets .judge_reason set for the log/UI."""
    kw   = [k.lower() for k in (watch.keywords or []) if k.strip()]
    anti = [k.lower() for k in (watch.antikeywords or []) if k.strip()]
    if not kw and not anti:
        return listings, []
    kept, dropped = [], []
    for l in listings:
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
        log.info("Keyword prefilter dropped %d/%d listing(s) for %r",
                 len(dropped), len(listings), watch.name)
    return kept, dropped


def _filter_listings_by_judgment(new_listings: list, watch: Watch, cfg: AppConfig,
                                 fail_closed: bool = False) -> list:
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
    import httpx
    OLLAMA_URL = "http://localhost:11434"
    threshold = getattr(watch, "min_rating", 3)

    def _entry(i: int, l) -> str:
        line = f"{i}. {l.title} {l.price}".strip()
        if l.details:
            line += f"\n   AD DETAILS: {l.details[:600]}"
        return line

    numbered = "\n".join(_entry(i, l) for i, l in enumerate(new_listings))
    system_prompt = (
        "You rate marketplace listings against a user's criteria. Each entry has a "
        "title/price and, when available, an 'AD DETAILS' line with the listing's "
        "description and attributes — USE those details (transmission, drivetrain, "
        "mileage, condition) when rating.\n" + _RATING_RUBRIC + "\n"
        "Return ONLY a JSON object of the form "
        '{"ratings": [{"i": <index>, "r": <1-5>, "why": "<≤10 words>"}, ...]}. '
        "Include EVERY listing exactly once. No other text."
    )
    user_msg = (
        f"Criteria: {watch.instruction}\n"
        f"{watch.judgment_prompt or ''}\n\n"
        f"Listings:\n{numbered}\n\n"
        "Rate every listing."
    )
    try:
        payload = {
            "model": cfg.models.effective_council_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_msg},
            ],
            "stream": False,
            "format": "json",
        }
        with httpx.Client(timeout=90.0) as client:
            r = client.post(f"{OLLAMA_URL}/api/chat", json=payload)
            r.raise_for_status()
        data = json.loads(r.json()["message"]["content"])
        by_idx: dict[int, tuple[int, str]] = {}
        for item in (data.get("ratings") or []):
            try:
                n = int(item.get("i"))
                r_ = max(1, min(5, int(item.get("r"))))
            except (TypeError, ValueError):
                continue
            if 0 <= n < len(new_listings):
                by_idx[n] = (r_, str(item.get("why", "")).strip())

        keep = []
        for i, l in enumerate(new_listings):
            rating, why = by_idx.get(i, (threshold, ""))   # unrated → assume it clears (never drop silently)
            l.rating = rating
            l.judge_reason = why or getattr(l, "judge_reason", "")
            if rating >= threshold:
                keep.append(l)
        log.info("Rating judge kept %d/%d (>=%d) for %r",
                 len(keep), len(new_listings), threshold, watch.name)
        for l in new_listings:
            if getattr(l, "rating", threshold) < threshold:
                log.info("   rated %d: %s %s — %s", l.rating,
                         (l.title or "(no title)")[:70], l.price, getattr(l, "judge_reason", ""))
        return keep
    except Exception as exc:
        if fail_closed:
            log.warning("Rating judge failed for %r (%s) — cross-watch SKIPPED (fail-closed)",
                        watch.name, exc)
            return []
        log.warning("Rating judge failed for %r (%s) — alerting all new listings", watch.name, exc)
        return new_listings


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
    import html as _html
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
        title = _html.escape(l.title or "(listing)")
        price = _html.escape(l.price or "")
        rating = getattr(l, "rating", None)
        why    = _html.escape(getattr(l, "judge_reason", "") or "")
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
            )
            _mark_seen(l)   # only after a send attempt that didn't raise
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
    """Facebook threw a security checkpoint / block / CAPTCHA. STOP (never solve it), alert
    the user ONCE, and put this watch's Facebook sweeps on a cooldown so we back off instead
    of hammering a flagged account — the behavior that turns a soft flag into a real ban."""
    from datetime import datetime as _dt
    _FB_COOLDOWN[watch.name] = time.time() + _FB_COOLDOWN_SECONDS
    log.warning("Facebook checkpoint on %r (%s) — backing off for %d h",
                watch.name, reason, _FB_COOLDOWN_SECONDS // 3600)

    last = get_last_run(watch.name, db_path)
    already = bool(last and last.get("error") and "checkpoint" in (last["error"] or "").lower())
    _save_error(watch.name, run_ts, f"facebook checkpoint: {reason}", db_path,
                perception_mode="continuous-agent")

    if not already and (watch.notify.telegram or watch.notify.email):
        msg = (f"'{watch.name}' stopped: Facebook showed a security check ({reason}). "
               "I did NOT try to solve it — that protects the account. I'll leave Facebook "
               "alone for a few hours. If it keeps happening, open Facebook yourself, clear "
               "the check, and make sure the login is healthy before restarting the watch.")
        result = ReasoningResult(found=True, summary=msg, confidence="high", link=watch.urls[0])
        payload = NotificationPayload(watch_name=watch.name, result=result, timestamp=_dt.fromisoformat(run_ts))
        try:
            send_notifications(payload, cfg.notifications,
                               use_telegram=watch.notify.telegram, use_email=watch.notify.email)
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
                               use_telegram=watch.notify.telegram, use_email=watch.notify.email)
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

        _run_pipeline(watch, cfg, run_ts, db_path)

    except Exception as exc:
        log.error("Unhandled error in watch %r: %s", watch_name, exc, exc_info=True)
        _save_error(watch_name, run_ts, str(exc), db_path)


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
