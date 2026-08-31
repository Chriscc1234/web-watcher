"""
ServiceManager — owns the lifecycle of every long-running service.

Services:
  "ollama"    — local Ollama process (adopted if already running, started if not)
  "server"    — uvicorn / FastAPI dashboard
  "scheduler" — APScheduler watch loop

Ollama is started first so the scheduler can immediately fire watches.
On a remote machine this is the only way to start Ollama; locally we
detect an already-running instance and adopt it without killing it.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import httpx

log = logging.getLogger(__name__)

def _anthropic_installed() -> bool:
    """True when the cloud SDK is importable. A tiny module function so the self-heal is testable
    without actually (un)installing the package."""
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


OLLAMA_URL     = "http://localhost:11434"
# How long the scheduled self-audit will wait for watching to go quiet before running anyway.
# A continuous watch is busy nearly always, so an unbounded "wait for idle" means never.
_REVIEW_BUSY_GRACE_S = 2 * 3600.0
OLLAMA_TIMEOUT = 30.0   # seconds to wait for Ollama to become ready after launch


class Status(str, Enum):
    STOPPED  = "stopped"
    STARTING = "starting"
    RUNNING  = "running"
    ERROR    = "error"


@dataclass
class ServiceState:
    name: str
    status: Status = Status.STOPPED
    started_at: Optional[float] = None
    error: Optional[str] = None


class ServiceManager:
    PORT = 7878

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Order matters — determines display order in the dashboard
        self._states: dict[str, ServiceState] = {
            "ollama":    ServiceState(name="Ollama"),
            "server":    ServiceState(name="Dashboard Server"),
            "scheduler": ServiceState(name="Scheduler"),
        }
        self._uvicorn_server = None
        self._server_thread:  Optional[threading.Thread] = None
        self._scheduler       = None
        self._oversight       = None   # OversightAgent — the visible narrator over all watches
        self._telegram        = None   # TelegramBridge — inbound phone chat (opt-in)
        self._orchestrator    = None   # Orchestrator — the single driver (opt-in)
        # Global master switch. True = ALL watching is paused (scheduled jobs, continuous loops,
        # and the driver) for everyone; persisted so it survives a restart. Admin-only at the API.
        self._paused          = False
        self._ollama_proc:    Optional[subprocess.Popen] = None
        self._ollama_adopted  = False   # True when we adopted an existing instance
        # Facebook (login-profile) connect flow state, surfaced via get_statuses().
        self._fb_connect_status = "idle"  # idle | opening | waiting_for_login | done | error
        # Deep Inspect: on-demand deal/scam evaluation of one listing. url -> {status, verdict}.
        # status: running | done | error. Runs on a worker thread (browser + a slow big model).
        self._inspections: dict = {}
        self._inspect_lock = threading.Lock()
        # Scheduled self-audit (off unless the user turns it on) — see _review_scheduler.
        self._review_stop = threading.Event()
        # Auto-update state (populated by the background checker; surfaced via /api/update).
        self._window          = None    # pywebview window — set by main.py, used to restart
        self._update_available = None   # dict {version, notes} when a newer release is staged
        self._update_checked_at = None  # epoch seconds of the last completed check
        self._update_error    = None    # human-readable reason the last check failed
        # Full-installer updates (runtime bumps). Downloaded in the background; NEVER run
        # without the user clicking Install — it closes the app and replaces the folder.
        self._installer_path  = None    # Path to the verified .exe, once downloaded
        self._installer_pct   = 0       # 0-100 download progress
        self._installer_busy  = False   # a download is in flight
        self._update_thread:  Optional[threading.Thread] = None
        self._update_stop     = threading.Event()

    # ------------------------------------------------------------------
    # Bulk lifecycle
    # ------------------------------------------------------------------

    def start_all(self) -> None:
        self._start_ollama()    # first: scheduler needs Ollama available
        self._start_server()
        self._start_scheduler()
        threading.Thread(target=self._review_scheduler, daemon=True, name="ww-review-sched").start()
        # Master switch on launch. If the user left it PAUSED, re-pause so nothing sweeps. If it's
        # ON (the default), actually WATCH: hand any enabled continuous watches to the driver —
        # scheduled watches already run via apscheduler, but continuous ones register "stopped", so
        # without this a not-paused app would show "watching" while nothing actually swept (the
        # "it says it's on but never finds anything" gap).
        if self._load_paused():
            self.pause_all()
        else:
            try:
                if self._has_enabled_continuous():
                    self.start_orchestrator()
            except Exception as exc:
                log.warning("start_all: could not start the driver: %s", exc)
        self._start_update_checker()
        self._start_telegram()  # last: the bridge talks to the server we just started
        # If cloud is configured but its SDK never got installed, install it in the background.
        threading.Thread(target=self._ensure_cloud_deps, daemon=True, name="ww-cloud-deps").start()

    def stop_all(self) -> None:
        self._update_stop.set()
        self._stop_telegram()
        self._stop_scheduler()
        self._stop_server()
        self._stop_ollama()     # last: don't kill Ollama while scheduler is live

    # ------------------------------------------------------------------
    # Two-way Telegram (opt-in) — text the bot, talk to The Watcher
    # ------------------------------------------------------------------

    def _another_instance_owns_the_port(self) -> bool:
        """True when ANOTHER Web Watcher already owns the dashboard port. Two live instances mean
        two Telegram bridges racing for the same bot (an older build could answer messages) and two
        drivers browsing at once — so the second instance must not start a bridge."""
        import socket
        if self._uvicorn_server is not None:
            return False                     # WE own it
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                return s.connect_ex(("127.0.0.1", self.PORT)) == 0
        except Exception:
            return False

    def _start_telegram(self) -> None:
        """Start the inbound Telegram bridge when the user has enabled two-way chat. Failure to
        start is never fatal — alerts and watches work regardless."""
        if self._another_instance_owns_the_port():
            log.warning("Another Web Watcher instance is already running — not starting a second "
                        "Telegram bridge (two bridges would fight over the same bot)")
            return
        try:
            from web_watcher.config import load as load_config
            from web_watcher.telegram_bot import TelegramBridge
            tg = load_config().notifications.telegram
            if not getattr(tg, "two_way", False):
                return
            bridge = TelegramBridge(tg.bot_token, tg.chat_id, f"http://127.0.0.1:{self.PORT}",
                                    allowed_chat_ids=list(getattr(tg, "allowed_chat_ids", []) or []),
                                    checkin_hours=float(getattr(tg, "checkin_hours", 12.0) or 0))
            if bridge.start():
                self._telegram = bridge
        except Exception as exc:
            log.warning("Telegram two-way chat could not start: %s", exc)

    def _stop_telegram(self) -> None:
        bridge, self._telegram = getattr(self, "_telegram", None), None
        if bridge is not None:
            try:
                bridge.stop()
            except Exception as exc:
                log.debug("Telegram bridge stop failed: %s", exc)

    def restart_telegram(self) -> bool:
        """Re-read config and restart the bridge — called after the credentials page saves, so
        turning two-way chat on takes effect without restarting the app."""
        self._stop_telegram()
        self._start_telegram()
        return getattr(self, "_telegram", None) is not None

    # ------------------------------------------------------------------
    # Cloud SDK self-heal
    # ------------------------------------------------------------------

    def _ensure_cloud_deps(self) -> None:
        """Install the `anthropic` SDK when the user has set a cloud key but the package is
        missing. Without it EVERY cloud escalation dies on ImportError and silently falls back to
        local — so the user's loaded credits can never be spent, with no visible reason. The
        install runs in the app's OWN process (the real interpreter, real disk), so it actually
        persists; it's best-effort and never fatal — a failure just leaves us local-only, exactly
        as before. Guarded on a configured key so a purely-local install never runs pip."""
        try:
            from web_watcher.config import load as load_config
            cloud = load_config().models.cloud
            key = (getattr(cloud, "anthropic_api_key", "") or "").strip() or \
                os.environ.get("ANTHROPIC_API_KEY", "").strip()
            if not key or _anthropic_installed():
                return
            log.info("Cloud key is set but the 'anthropic' SDK is missing — installing it now")
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                 "anthropic>=1.0"],
                capture_output=True, text=True, timeout=300,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0:
                log.info("Installed the 'anthropic' SDK — cloud escalation is available now")
            else:
                log.warning("Could not install 'anthropic' (exit %s) — staying local-only: %s",
                            r.returncode, (r.stderr or "")[-300:])
        except Exception as exc:
            log.debug("cloud-deps self-heal skipped (%s)", exc)

    # ------------------------------------------------------------------
    # Auto-update (checks GitHub Releases; stages in the background; the UI
    # notifies and a one-click restart applies it via launcher.py)
    # ------------------------------------------------------------------

    _UPDATE_CHECK_INTERVAL = 6 * 3600   # re-check every 6 hours
    _UPDATE_CHECK_DELAY    = 6          # first check soon after launch (was 25 — too long a wait
                                        # before the update banner could appear, esp. on the one-time
                                        # runtime-bump installer download)

    def _start_update_checker(self) -> None:
        def _loop():
            # Check on launch (after a short delay) and then periodically.
            if self._update_stop.wait(self._UPDATE_CHECK_DELAY):
                return
            while not self._update_stop.is_set():
                try:
                    self.check_updates_now()
                except Exception as exc:
                    log.debug("update check failed: %s", exc)
                if self._update_stop.wait(self._UPDATE_CHECK_INTERVAL):
                    return
        self._update_thread = threading.Thread(target=_loop, daemon=True, name="ww-updater")
        self._update_thread.start()

    def check_updates_now(self) -> dict:
        """Check GitHub for a newer release; if found, download + stage it so a one-click
        restart can apply it. Returns the current update status. Safe to call anytime.

        An unreachable GitHub is recorded rather than swallowed: "we couldn't check" and "you're
        up to date" look identical to the user otherwise, and only one of them is reassuring."""
        from web_watcher import updater
        from web_watcher.__version__ import __version__
        self._update_checked_at = time.time()
        # Already staged? then we're done — surface it.
        staged = updater.pending_update()
        if staged:
            self._update_error = None
            self._update_available = {"version": staged, "notes": (self._update_available or {}).get("notes", "")}
            return self.update_status()
        try:
            data = updater._fetch_latest_release(updater.GITHUB_OWNER, updater.GITHUB_REPO)
        except updater.UpdateUnreachable as exc:
            self._update_error = f"Couldn't reach GitHub: {exc}"
            return self.update_status()
        self._update_error = None
        info = updater.parse_release(data) if data else None
        if info is None or not updater.is_newer(info.version, __version__):
            return self.update_status()

        if updater.needs_installer(info):
            # New pip deps / Python / DLLs: a code swap would leave the app unable to import.
            # Fetch the installer in the background; the user decides when to run it.
            self._update_available = {"version": info.version, "notes": info.notes,
                                      "kind": "installer",
                                      "size_mb": round((info.installer_size or 0) / 1_000_000)}
            self._start_installer_download(info)
            return self.update_status()

        if updater.download_and_stage(info) is not None:
            self._update_available = {"version": info.version, "notes": info.notes, "kind": "code"}
            log.info("update %s staged and ready to apply", info.version)
        else:
            self._update_error = "The update downloaded but failed its integrity check."
        return self.update_status()

    # -- full-installer path ------------------------------------------------

    def _start_installer_download(self, info) -> None:
        """Fetch + verify the installer on a worker thread. Idempotent: a second call while one
        is in flight, or after it landed, does nothing."""
        from web_watcher import updater
        if self._installer_busy or self._installer_path:
            return
        self._installer_busy = True
        self._installer_pct = 0

        def _progress(done: int, total: int) -> None:
            if total:
                self._installer_pct = min(99, int(done * 100 / total))

        def _run() -> None:
            try:
                path = updater.download_installer(info, on_progress=_progress)
                if path is None:
                    self._update_error = ("The full update could not be downloaded or failed its "
                                          "security check. Your current version is untouched.")
                    return
                self._installer_path = path
                self._installer_pct = 100
                log.info("installer %s ready to run", info.version)
            except Exception as exc:
                log.warning("installer download failed: %s", exc)
                self._update_error = f"The full update could not be downloaded: {exc}"
            finally:
                self._installer_busy = False

        threading.Thread(target=_run, daemon=True, name="ww-installer-dl").start()

    def run_installer(self) -> bool:
        """Start the verified installer and close the app so it can replace the folder. Returns
        False when nothing is downloaded yet — the UI must never offer this before then.

        The window is closed ONLY after the installer proves it survived launch. The installer is
        unsigned, so antivirus can kill it on sight; closing first would leave the user with a
        vanished app and no explanation."""
        from web_watcher import updater
        if not self._installer_path:
            return False
        if not updater.launch_installer(self._installer_path):
            self._update_error = ("The update installer wouldn't start — antivirus or a system "
                                  "policy may have blocked it. You're still on your current "
                                  "version. Try downloading the installer from GitHub by hand.")
            return False
        if self._window is not None:
            try:
                self._window.destroy()   # the installer waits for us to let go of the files
            except Exception as exc:
                log.debug("window destroy failed: %s", exc)
        return True

    def update_status(self) -> dict:
        from web_watcher import updater
        from web_watcher.__version__ import __version__
        staged = updater.pending_update()
        return {
            "current":   __version__,
            "available": self._update_available,     # {version, notes} or None
            "staged":    bool(staged),               # downloaded + ready to apply on restart
            "configured": bool(updater.GITHUB_OWNER),
            "checked_at": self._update_checked_at,   # epoch seconds, or None if never checked
            "error":     self._update_error,         # why the last check failed, or None
            # Full-installer update: downloading in the background, then one click to run it.
            "installer_ready":       self._installer_path is not None,
            "installer_downloading": self._installer_busy,
            "installer_pct":         self._installer_pct,
        }

    def _force_exit_soon(self, grace: float = 12.0) -> None:
        """Guarantee this process actually DIES after a restart/reset is requested.

        Closing the window normally unwinds main() and exits — but a hung Playwright browser, a
        uvicorn worker, or any non-daemon thread can keep the process alive. When that happened the
        launcher started a NEW instance while the old one kept running: several copies of the app
        at once, each polling Telegram (so an OUTDATED build could answer messages) and each
        driving its own browser. After a grace period for a clean shutdown, exit hard."""
        import os as _os
        import threading as _th

        def _bail():
            log.warning("restart: forcing exit after %.0fs so no stale instance survives", grace)
            try:
                _os._exit(0)                 # bypasses atexit/thread joins — nothing can block it
            except Exception:
                pass

        t = _th.Timer(grace, _bail)
        t.daemon = True
        t.start()

    def request_restart(self) -> bool:
        """Flag a restart and close the window so launcher.py applies the staged update and
        relaunches. Returns True if a staged update exists to apply."""
        from web_watcher import updater
        if not updater.pending_update():
            return False
        try:
            updater.UPDATES_DIR.mkdir(parents=True, exist_ok=True)
            updater.RESTART_FLAG.write_text("1", encoding="utf-8")
        except Exception as exc:
            log.warning("could not write restart flag: %s", exc)
            return False
        # Closing the window triggers the normal shutdown; launcher sees the flag + relaunches.
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception as exc:
                log.debug("window destroy failed: %s", exc)
        # ...and make sure THIS process is gone even if something refuses to unwind, so the
        # relaunched app is the only one running (no stale build answering Telegram).
        self._force_exit_soon()
        return True

    def request_reset(self) -> bool:
        """Flag a FULL RESET (fresh install) and close the window. launcher.py wipes all user
        data — watches, results, DB, saved logins, history — and resets config before it
        relaunches, so the wipe happens while nothing holds the DB open. Destructive; the UI
        gates this behind multiple confirmations. Always returns True (nothing to validate)."""
        from web_watcher import updater
        try:
            updater.UPDATES_DIR.mkdir(parents=True, exist_ok=True)
            (updater.UPDATES_DIR / "RESET_REQUESTED").write_text("1", encoding="utf-8")
        except Exception as exc:
            log.warning("could not write reset flag: %s", exc)
            return False
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception as exc:
                log.debug("window destroy failed: %s", exc)
        return True

    # ------------------------------------------------------------------
    # Individual service control (called by API routes)
    # ------------------------------------------------------------------

    def start(self, name: str) -> None:
        dispatch = {"ollama": self._start_ollama, "server": self._start_server,
                    "scheduler": self._start_scheduler}
        if name not in dispatch:
            raise ValueError(f"Unknown service: {name!r}")
        dispatch[name]()

    def stop(self, name: str) -> None:
        dispatch = {"ollama": self._stop_ollama, "server": self._stop_server,
                    "scheduler": self._stop_scheduler}
        if name not in dispatch:
            raise ValueError(f"Unknown service: {name!r}")
        dispatch[name]()

    def restart(self, name: str) -> None:
        self.stop(name)
        time.sleep(0.5)
        self.start(name)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_statuses(self) -> dict:
        with self._lock:
            now = time.time()
            result = {
                key: {
                    "name":           state.name,
                    "status":         state.status.value,
                    "uptime_seconds": int(now - state.started_at) if state.started_at else None,
                    "error":          state.error,
                }
                for key, state in self._states.items()
            }
        # Model list added outside the lock (network call)
        if result.get("ollama", {}).get("status") == "running":
            result["ollama"]["models"] = self.get_ollama_models()
            result["ollama"]["adopted"] = self._ollama_adopted
        return result

    # Cache the model list so the dashboard's 3s status poll doesn't hammer
    # Ollama's /api/tags on every tick — that competed with the agent's own
    # inference calls during a run. The model list rarely changes.
    _models_cache: list[str] = []
    _models_cache_at: float = 0.0
    _MODELS_TTL: float = 30.0

    def get_ollama_models(self) -> list[str]:
        now = time.time()
        if self._models_cache and (now - self._models_cache_at) < self._MODELS_TTL:
            return self._models_cache
        try:
            r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=3.0)
            if r.is_success:
                self._models_cache = [m["name"] for m in r.json().get("models", [])]
                self._models_cache_at = now
        except Exception:
            pass
        return self._models_cache

    # ------------------------------------------------------------------
    # Scheduler passthrough
    # ------------------------------------------------------------------

    def run_watch_now(self, watch_name: str) -> None:
        if self._scheduler is None:
            raise RuntimeError("Scheduler is not running")
        if self._paused:
            log.info("The Watcher is paused — not running %r now; resume watching first", watch_name)
            return
        self._scheduler.run_now(watch_name)

    def get_job_info(self) -> list[dict]:
        return self._scheduler.get_job_info() if self._scheduler else []

    # ── THE one answer to "is this watch actually running?" ─────────────────────────
    # Two engines can drive a continuous watch — a per-watch scheduler thread, or the
    # orchestrator servicing every enabled watch in rotation — and each only reported its
    # own. The API said running=False while the orchestrator was mid-sweep on that very
    # watch; the assistant then repeated the wrong answer to a person. Asking "did I start
    # it?" is assumption; this queries what is true right now, whichever engine owns it.
    def watch_runtime(self, watch_name: str, cfg=None) -> dict:
        """{'running': bool, 'engine': 'orchestrator'|'scheduler'|None, 'sweeping_now': bool}

        Ask the engine, never infer. This used to reason its own way to an answer — orchestrator
        up + watch enabled + continuous ⇒ running — which is a guess dressed as a fact, and it
        was wrong for 13 straight hours while the rotation held nothing. The orchestrator now
        answers for itself."""
        try:
            if self.orchestrator_running() and not self._paused:
                if self._orchestrator.is_servicing(watch_name, cfg):
                    cur = (self.orchestrator_status() or {}).get("current")
                    return {"running": True, "engine": "orchestrator",
                            "sweeping_now": bool(cur and cur == watch_name)}
            if self._scheduler and self._scheduler.is_continuous_running(watch_name):
                return {"running": True, "engine": "scheduler", "sweeping_now": True}
        except Exception as exc:
            log.debug("watch_runtime(%r) failed: %s", watch_name, exc)
        return {"running": False, "engine": None, "sweeping_now": False}

    def runtime_map(self) -> dict:
        """watch_name -> watch_runtime(...) for every watch, cheap enough for a list call."""
        try:
            from web_watcher.config import load as _load
            cfg = _load()
            return {w.name: self.watch_runtime(w.name, cfg) for w in cfg.watches}
        except Exception:
            return {}

    def reload_scheduler(self) -> None:
        if self._scheduler:
            self._scheduler.reload()
        # While the orchestrator drives, the scheduler's own continuous-thread count is
        # 0 by design — that reload line ("0 continuous watch(es) running") looks alarming
        # but isn't. Clarify that The Watcher still owns the watches and picks up the edit.
        if self.orchestrator_running():
            # Belt and braces: whatever route a per-watch loop took to exist, it must not
            # coexist with the driver. Standing it down is recorded as NOT the user's choice.
            stray = list(getattr(self._scheduler, "_continuous_threads", {}) or {})
            if stray:
                log.warning("Reload left %d per-watch loop(s) running under the orchestrator "
                            "(%s) — standing them down", len(stray), ", ".join(stray))
                self._scheduler.stop_all_continuous()
            n = len(self.orchestrator_status().get("topics", []))
            log.info("Config reloaded — The Watcher (orchestrator) is driving %d continuous "
                     "watch(es) and will apply the change on its next cycle", n)
        # Wake the narrator so an added/removed/edited watch shows up in its feed and
        # per-watch view immediately, not a tick-interval later.
        self._nudge_oversight()

    # ------------------------------------------------------------------
    # Continuous watches
    # ------------------------------------------------------------------

    def start_continuous(self, watch_name: str) -> None:
        if self._scheduler is None:
            raise RuntimeError("Scheduler is not running")
        # Master switch off → nothing watches. Don't quietly start one watch behind a global
        # pause; the caller (dashboard/bot) tells the user to resume The Watcher first.
        if self._paused:
            log.info("The Watcher is paused — not starting %r; resume watching first", watch_name)
            return
        # While the orchestrator is driving, it owns the continuous watches — starting a
        # per-watch thread too would double-sweep the same site. But the INTENT still counts:
        # record that this watch should be running, so the orchestrator includes it in its
        # rotation and a restart resumes it. Before this, a start under the orchestrator
        # recorded nothing — the desired-state file stayed empty and the resume-on-launch
        # feature was a no-op for exactly the people using The Watcher.
        if self.orchestrator_running():
            self._scheduler._remember_running(watch_name, True)
            log.info("Orchestrator is running — %r joins its rotation (no per-watch thread)",
                     watch_name)
            self._nudge_oversight()
            return
        # THE DRIVER IS THE ENGINE, NOT A BOOT-TIME ACCIDENT. start_orchestrator() only ever
        # ran at launch, and only if an enabled continuous watch existed at that instant — so
        # after a boot with everything disabled, every later start spawned an ad-hoc
        # per-watch thread and the app ran the whole day on the wrong engine (extra browsers,
        # no rotation, no shared pacing). Starting a watch now starts The Watcher itself;
        # the per-watch thread survives only as the fallback when the driver won't start.
        self._scheduler._remember_running(watch_name, True)
        try:
            if self.start_orchestrator():
                log.info("%r starts The Watcher (orchestrator) — it will drive the rotation",
                         watch_name)
                self._nudge_oversight()
                return
        except Exception as exc:
            log.warning("could not start the orchestrator for %r (%s) — falling back to a "
                        "per-watch loop", watch_name, exc)
        self._scheduler.start_continuous(watch_name)
        self._nudge_oversight()

    def rename_continuous(self, old_name: str, new_name: str) -> None:
        """Carry the should-be-running intent across a rename. The desired-state file stores
        NAMES; without this, a renamed running watch would silently drop out of the rotation
        at the next restart — the same class of quiet death v0.144 was about."""
        if self._scheduler is None:
            return
        try:
            if old_name in (self._scheduler._remembered_running() or set()):
                self._scheduler._remember_running(old_name, False)
                self._scheduler._remember_running(new_name, True)
        except Exception as exc:
            log.warning("could not migrate desired-state for rename %r→%r: %s",
                        old_name, new_name, exc)

    def stop_continuous(self, watch_name: str) -> None:
        if self._scheduler is None:
            raise RuntimeError("Scheduler is not running")
        # Always record the intent — under the orchestrator there is no per-watch thread to
        # stop, and before this a "stop" was a polite no-op: the rotation kept sweeping the
        # watch the user had just been told was stopped.
        self._scheduler._remember_running(watch_name, False)
        self._scheduler.stop_continuous(watch_name)
        if self.orchestrator_running():
            log.info("%r leaves The Watcher's rotation (stopped by request)", watch_name)
        self._nudge_oversight()

    # ------------------------------------------------------------------
    # Orchestrator (the single driver — opt-in; coexists with per-watch mode)
    # ------------------------------------------------------------------

    def start_orchestrator(self) -> bool:
        """Hand the continuous watches to the single orchestrator. Stops any per-watch
        continuous loops first so a site isn't swept twice. Returns True if it started."""
        if self._scheduler is None:
            raise RuntimeError("Scheduler is not running")
        from web_watcher.orchestrator import Orchestrator
        try:
            self._scheduler.stop_all_continuous()   # stand down per-watch loops
        except Exception as exc:
            log.warning("Could not stop per-watch loops before orchestrator: %s", exc)
        if self._orchestrator is None:
            self._orchestrator = Orchestrator(self._scheduler, self._oversight)
        started = self._orchestrator.start()
        # From here the orchestrator owns the continuous watches — the scheduler must not
        # start per-watch loops behind its back on the next config reload.
        self._scheduler._orchestrator_owns = True
        self._nudge_oversight()
        return started

    def stop_orchestrator(self) -> None:
        self._stop_orchestrator()
        self._nudge_oversight()

    def _stop_orchestrator(self) -> None:
        if self._orchestrator is not None:
            try:
                self._orchestrator.stop()
            except Exception:
                pass
        if self._scheduler is not None:
            self._scheduler._orchestrator_owns = False   # per-watch loops are allowed again

    def orchestrator_running(self) -> bool:
        return bool(self._orchestrator and self._orchestrator.is_running())

    def orchestrator_status(self) -> dict:
        if self._orchestrator is None:
            return {"running": False, "current": None, "cycles": 0, "topics": []}
        return self._orchestrator.status()

    # ------------------------------------------------------------------
    # Global master switch — "The Watcher" watching vs paused (the whole program)
    # ------------------------------------------------------------------
    #
    # Two distinct levels the user controls:
    #   1. THIS master switch — is the program watching at all? Pausing stops EVERYTHING for
    #      everyone: scheduled jobs, continuous loops, and the driver. Admin-only at the API.
    #   2. Individual watches — enable/disable/start/stop one watch, scoped to its owner.
    # Persisted so a pause survives a restart (an end user pausing overnight stays paused).

    def _paused_flag_path(self):
        from web_watcher import paths
        return paths.data_dir() / "watcher_paused.flag"

    def is_paused(self) -> bool:
        return self._paused

    def _load_paused(self) -> bool:
        try:
            return self._paused_flag_path().exists()
        except Exception:
            return False

    def _persist_paused(self, paused: bool) -> None:
        try:
            p = self._paused_flag_path()
            if paused:
                p.write_text("paused\n", encoding="utf-8")
            elif p.exists():
                p.unlink()
        except Exception as exc:
            log.warning("could not persist paused state: %s", exc)

    def _has_enabled_continuous(self) -> bool:
        try:
            from web_watcher.config import load as load_config
            return any(getattr(w, "enabled", True) and getattr(w, "mode", "") == "continuous"
                       for w in load_config().watches)
        except Exception:
            return False

    def pause_all(self) -> None:
        """Master switch OFF: stop ALL watching — the driver, every continuous loop, and every
        scheduled job — for everyone. Persisted. Individual watch settings are untouched, so a
        later resume brings back exactly what was enabled."""
        self._paused = True
        self._persist_paused(True)
        self._stop_orchestrator()
        if self._scheduler is not None:
            try:
                self._scheduler.stop_all_continuous()
            except Exception as exc:
                log.warning("pause: could not stop continuous loops: %s", exc)
            try:
                self._scheduler.pause_jobs()
            except Exception as exc:
                log.warning("pause: could not pause scheduled jobs: %s", exc)
        self._nudge_oversight()
        log.info("The Watcher PAUSED — all watching stopped (master switch off)")

    def resume_all(self) -> bool:
        """Master switch ON: resume watching. Unpause scheduled jobs and, if any enabled
        continuous watches exist, hand them back to the driver. Returns True if the driver started."""
        self._paused = False
        self._persist_paused(False)
        if self._scheduler is not None:
            try:
                self._scheduler.resume_jobs()
            except Exception as exc:
                log.warning("resume: could not resume scheduled jobs: %s", exc)
        started = False
        try:
            if self._has_enabled_continuous():
                started = self.start_orchestrator()
        except Exception as exc:
            log.warning("resume: could not start the driver: %s", exc)
        self._nudge_oversight()
        log.info("The Watcher RESUMED — watching active (master switch on)")
        return started

    def watcher_status(self) -> dict:
        """Unified global status for the dashboard + bot: is the program watching, and what's
        actually running underneath. 'running' == not paused (the user-facing on/off)."""
        st = {"running": (not self._paused), "paused": self._paused,
              "driver_running": self.orchestrator_running()}
        try:
            st["current"] = self.orchestrator_status().get("current")
        except Exception:
            st["current"] = None
        try:
            st["continuous_running"] = self._scheduler.running_continuous() if self._scheduler else []
        except Exception:
            st["continuous_running"] = []
        return st

    def _nudge_oversight(self) -> None:
        """Wake The Watcher so it narrates a start/stop near-instantly instead of on its
        next slow tick."""
        if self._oversight is not None:
            try:
                self._oversight.nudge()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Facebook / login-profile connect flow
    # ------------------------------------------------------------------

    def connect_facebook(self) -> None:
        """
        Open a visible persistent-profile browser at Facebook so the user can log in
        by hand. Cookies persist in the on-disk profile and are reused by watches with
        use_login_profile=True. Runs on a background thread (sync Playwright).

        Continuous watches that are RUNNING are stopped first to release the profile's
        SingletonLock, then restarted afterwards (now logged in). Watches that were
        already stopped stay stopped — connect never auto-starts anything.
        """
        from web_watcher.browser import BrowserSession
        from web_watcher.config import load as load_config

        self._fb_connect_status = "opening"
        # Capture which continuous watches are running, then stop them to free the
        # profile lock (only one process may use the profile dir at a time).
        was_running: list[str] = []
        if self._scheduler is not None:
            try:
                was_running = self._scheduler.running_continuous()
                self._scheduler.stop_all_continuous()
            except Exception as exc:
                log.warning("Could not stop continuous watches before FB connect: %s", exc)

        try:
            cfg = load_config()
            # HANDS OFF. This window is a PERSON typing their own password into Facebook's own
            # page — we inject nothing into it. Our patches cannot help here and can only hurt:
            # a non-configurable window.print override from the dialog guard threw inside
            # Facebook's login bundle and left the user staring at a white screen, and every
            # injected script is one more surface for the most detection-happy site we touch.
            # A plain Chrome profile with a human driving it is the most legitimate thing we can
            # present. The cookies it leaves behind are what the watches reuse.
            with BrowserSession(
                headless=False, stealth=cfg.browser.stealth,
                persistent=True, profile_dir=cfg.browser.profile_dir,
                inject_patches=False,
            ) as session:
                # A persistent context opens with its own about:blank page. Reuse THAT instead of
                # adding a second tab, so the user gets one clean window rather than a stray
                # blank tab sitting next to the login (they noticed, and it looks broken).
                page = None
                try:
                    existing = [p for p in session.context.pages if not p.is_closed()]
                    page = existing[0] if existing else None
                except Exception:
                    page = None
                if page is None:
                    page = session.new_page()
                # DIAGNOSTICS. A blank page has several possible causes that look identical from
                # the outside — a JS exception, a blocked resource, a CSP violation, or a server
                # that simply returned an empty shell — and guessing between them has cost this
                # project several releases. Record what the page actually reports so the log can
                # answer it. Read-only listeners; they cannot affect the page.
                _diag = {"errors": [], "failed": [], "console": []}

                def _on_pageerror(exc):
                    _diag["errors"].append(str(exc)[:300])
                    log.warning("FB connect: page error: %s", str(exc)[:300])

                def _on_requestfailed(req):
                    # Ad/telemetry blocking is normal noise; a failed FACEBOOK asset is not.
                    line = f"{req.failure} {req.url[:140]}"
                    _diag["failed"].append(line)
                    if "facebook.com" in req.url or "fbcdn" in req.url:
                        log.warning("FB connect: request failed: %s", line)

                def _on_console(msg):
                    if msg.type == "error":
                        _diag["console"].append(msg.text[:250])
                        log.warning("FB connect: console error: %s", msg.text[:250])

                page.on("pageerror", _on_pageerror)
                page.on("requestfailed", _on_requestfailed)
                page.on("console", _on_console)
                page.goto("https://www.facebook.com/", timeout=60_000, wait_until="domcontentloaded")
                self._fb_connect_status = "waiting_for_login"
                log.info("Facebook login window open — waiting for user to sign in and close it")
                # While the person works, sample the page every few seconds. If it goes blank we
                # want the URL and the body size AT THAT MOMENT — after the window closes there
                # is nothing left to inspect, which is why the last several attempts produced
                # screenshots and no data. A page that is 200-OK, error-free and still empty is
                # a very different diagnosis from one throwing exceptions, and only this tells
                # them apart.
                def _sample():
                    try:
                        if page.is_closed():
                            return None
                        body = page.inner_text("body", timeout=2_000) or ""
                        return (page.url, len(body.strip()))
                    except Exception:
                        return None

                last_seen = None
                for _ in range(200):                      # ~10 minutes at 3s
                    if page.is_closed():
                        break
                    got = _sample()
                    if got and got != last_seen:
                        url, size = got
                        last_seen = got
                        log.info("FB connect: at %s — %d chars of visible text", url[:110], size)
                        if size < 40:
                            log.warning(
                                "FB connect: THIS PAGE IS BLANK (%d chars). errors=%d "
                                "console_errors=%d failed_requests=%d",
                                size, len(_diag["errors"]), len(_diag["console"]),
                                len([f for f in _diag["failed"]
                                     if "facebook.com" in f or "fbcdn" in f]))
                    if session.wait_until_closed(poll_seconds=1.0, timeout=3.0):
                        break
            self._fb_connect_status = "done"
            log.info("Facebook connect complete — profile session saved "
                     "(page errors: %d, console errors: %d, failed FB requests: %d)",
                     len(_diag["errors"]), len(_diag["console"]),
                     len([f for f in _diag["failed"] if "facebook.com" in f or "fbcdn" in f]))
        except Exception as exc:
            self._fb_connect_status = "error"
            log.error("connect_facebook failed: %s", exc)
        finally:
            # Restart only the continuous watches that were running before connect.
            if self._scheduler is not None:
                for name in was_running:
                    try:
                        self._scheduler.start_continuous(name)
                    except Exception as exc:
                        log.warning("Could not restart continuous watch %r after FB connect: %s", name, exc)

    def fb_connect_status(self) -> str:
        return self._fb_connect_status

    # ------------------------------------------------------------------
    # Deep Inspect (on-demand deal/scam evaluation of one listing)
    # ------------------------------------------------------------------

    def inspect_start(self, url: str, criteria: str = "") -> dict:
        """Kick off a Deep Inspect of one listing on a worker thread (browser fetch + a slow
        big model). Idempotent while one is running for the same URL. Returns the current
        status entry immediately; poll inspect_status(url) for the verdict."""
        if not url:
            return {"status": "error", "error": "no url"}
        with self._inspect_lock:
            cur = self._inspections.get(url)
            if cur and cur.get("status") == "running":
                return cur
            self._inspections[url] = {"status": "running", "url": url}

        def _run() -> None:
            from web_watcher.config import load as load_config
            from web_watcher import inspect as _inspect
            try:
                cfg = load_config()
                verdict = _inspect.deep_inspect_listing(url, criteria, cfg)
                status = "error" if verdict.get("error") else "done"
                with self._inspect_lock:
                    self._inspections[url] = {"status": status, "url": url, "verdict": verdict}
            except Exception as exc:
                log.warning("inspect_start failed for %s: %s", url, exc)
                with self._inspect_lock:
                    self._inspections[url] = {"status": "error", "url": url, "error": str(exc)}

        threading.Thread(target=_run, daemon=True, name="ww-inspect").start()
        return self._inspections[url]

    def inspect_status(self, url: str) -> dict:
        with self._inspect_lock:
            return dict(self._inspections.get(url) or {"status": "unknown", "url": url})

    # ------------------------------------------------------------------
    # Site comprehension (understand a site: kind, search-box purpose, viability)
    # ------------------------------------------------------------------

    def comprehend_start(self, url: str, refresh: bool = False) -> dict:
        """Comprehend a site on a worker thread (browser scan + a slow big model) and cache
        the understanding on its profile. Idempotent while one is running for the same URL."""
        if not url:
            return {"status": "error", "error": "no url"}
        with self._inspect_lock:
            cur = self._inspections.get("comprehend:" + url)
            if cur and cur.get("status") == "running":
                return cur
            self._inspections["comprehend:" + url] = {"status": "running", "url": url}

        def _run() -> None:
            from web_watcher.config import load as load_config
            from web_watcher import comprehend as _co
            try:
                u = _co.understanding_for(url, load_config(), refresh=refresh)
                status = "error" if u.get("error") else "done"
                with self._inspect_lock:
                    self._inspections["comprehend:" + url] = {"status": status, "url": url, "understanding": u}
            except Exception as exc:
                log.warning("comprehend_start failed for %s: %s", url, exc)
                with self._inspect_lock:
                    self._inspections["comprehend:" + url] = {"status": "error", "url": url, "error": str(exc)}

        threading.Thread(target=_run, daemon=True, name="ww-comprehend").start()
        return self._inspections["comprehend:" + url]

    def comprehend_status(self, url: str) -> dict:
        with self._inspect_lock:
            return dict(self._inspections.get("comprehend:" + url) or {"status": "unknown", "url": url})

    # ------------------------------------------------------------------
    # Chat review (the big model audits our own conversations)
    # ------------------------------------------------------------------

    def review_start(self, since: float | None = None, model: str = "") -> dict:
        """Kick off a chat-history audit on a worker thread. It is slow on purpose (the biggest
        local model, no time limit) and reports progress as it goes, so the UI can show a live
        line instead of a spinner. Only one review runs at a time."""
        key = "review"
        with self._inspect_lock:
            cur = self._inspections.get(key)
            if cur and cur.get("status") == "running":
                return cur
            self._inspections[key] = {"status": "running", "progress": "starting…"}

        def _note(msg: str) -> None:
            with self._inspect_lock:
                entry = self._inspections.get(key) or {}
                entry["progress"] = msg
                self._inspections[key] = entry

        def _run() -> None:
            from web_watcher.config import load as load_config
            from web_watcher import review as _review
            try:
                report = _review.review_chats(load_config(), since=since, model=model, progress=_note)
                with self._inspect_lock:
                    self._inspections[key] = {"status": "done", "progress": "done", "report": report}
            except Exception as exc:
                log.warning("chat review failed: %s", exc)
                with self._inspect_lock:
                    self._inspections[key] = {"status": "error", "error": str(exc)}

        threading.Thread(target=_run, daemon=True, name="ww-review").start()
        return self._inspections[key]

    def review_status(self) -> dict:
        with self._inspect_lock:
            return dict(self._inspections.get("review") or {"status": "idle"})

    def _review_scheduler(self) -> None:
        """Run the self-audit on the user's schedule. Checks every few minutes rather than
        sleeping for hours, so turning it on or changing the interval takes effect right away
        instead of after the old sleep finally expires."""
        from web_watcher.config import load as load_config
        from web_watcher import review as _review

        while not self._review_stop.wait(300.0):
            try:
                cfg = load_config()
                rc = getattr(cfg, "review", None)
                if not rc or not rc.enabled:
                    continue
                every = max(1.0, float(rc.every_hours or 24.0)) * 3600.0
                last = float(_review.watermark().get("last_run_at", 0.0) or 0.0)
                if last and (time.time() - last) < every:
                    continue
                if self.review_status().get("status") == "running":
                    continue
                # Prefer a quiet moment — the big model holds the GPU for a while. But a
                # continuous watch is busy essentially always, so "wait for idle" alone would
                # defer forever and the audit would never run at all. So we wait only for a
                # bounded grace period past due, then go ahead: the audit is chunked and releases
                # the GPU between chunks, and a person's chat jumps the queue regardless.
                busy = self.orchestrator_running() or bool(
                    self._scheduler and self._scheduler.running_continuous())
                overdue_by = (time.time() - last - every) if last else every
                if busy and overdue_by < _REVIEW_BUSY_GRACE_S:
                    log.debug("chat review: watching is busy — deferring (%.0f min into the grace "
                              "period)", max(0.0, overdue_by) / 60)
                    continue
                if busy:
                    log.info("chat review: watching is still busy but the audit is %.1fh overdue "
                             "— running it anyway (it yields the GPU between chunks)",
                             overdue_by / 3600)
                log.info("chat review: scheduled run starting (every %.1fh)", rc.every_hours)
                self.review_start()
                self._await_review_then_notify(cfg)
                # The Watch Auditor rides the same schedule: after the chat audit, a slow
                # deep review of every WATCH (config vs activity vs the delivery ledger).
                # The admin's ask, verbatim: "an agent that reviews the watches and looks
                # at issues like this... takes a long time is ok."
                try:
                    from web_watcher import audit as _audit
                    self._notify_audit_findings(cfg, _audit.run_audit(cfg))
                except Exception as _exc:
                    log.warning("watch audit failed: %s", _exc)
            except Exception as exc:
                log.warning("chat review scheduler: %s", exc)

    def _notify_audit_findings(self, cfg, report: dict) -> None:
        """Ping the ADMIN with the audit's serious findings — a report nobody reads is
        a report that didn't happen. High-severity only; the full report sits at
        /api/audit/latest."""
        try:
            high = [f for f in (report or {}).get("findings", [])
                    if f.get("severity") == "high"]
            if not high:
                return
            from web_watcher import notify
            lines = ["\U0001f50e Watch audit: %d thing(s) need a look:" % len(high)]
            for f in high[:6]:
                lines.append("\u2022 %s: %s" % (f["watch"], f["finding"]))
            notify.send_plain_telegram(chr(10).join(lines), cfg.notifications)
        except Exception as exc:
            log.debug("could not send audit findings: %s", exc)

    def _await_review_then_notify(self, cfg) -> None:
        """Wait for the running audit, then tell the admin if it found anything serious. A report
        nobody reads is a report that didn't happen — but only HIGH findings are worth a ping."""
        def _wait() -> None:
            for _ in range(720):                       # up to ~2h; the 72b is slow on purpose
                if self._review_stop.wait(10.0):
                    return
                st = self.review_status()
                if st.get("status") != "running":
                    break
            report = (self.review_status().get("report")) or {}
            highs = int((report.get("counts") or {}).get("high", 0))
            rc = getattr(cfg, "review", None)
            if not (highs and rc and rc.notify):
                return
            try:
                from web_watcher.notify import send_plain_telegram
                send_plain_telegram(
                    f"🔎 Chat self-review: {highs} thing(s) worth a look across "
                    f"{report.get('turns_reviewed', 0)} messages.\n\n"
                    "Open the app → Settings → Self-review for the details.",
                    cfg.notifications)
            except Exception as exc:
                log.debug("could not send the review summary: %s", exc)

        threading.Thread(target=_wait, daemon=True, name="ww-review-notify").start()

    # ------------------------------------------------------------------
    # Site drill (can we actually use this site? — run before trusting a watch on it)
    # ------------------------------------------------------------------

    def drill_start(self, site: str, headless: bool = False) -> dict:
        """Run a site competency drill on a worker thread. Opens a real browser, so continuous
        watches are stopped first to release the login profile's lock — exactly like the Facebook
        connect flow — and restarted afterwards."""
        key = "drill"
        with self._inspect_lock:
            cur = self._inspections.get(key)
            if cur and cur.get("status") == "running":
                return cur
            self._inspections[key] = {"status": "running", "site": site, "progress": "starting…"}

        def _note(msg: str) -> None:
            with self._inspect_lock:
                entry = self._inspections.get(key) or {}
                entry["progress"] = msg
                self._inspections[key] = entry

        def _run() -> None:
            from web_watcher.config import load as load_config
            from web_watcher import drill as _drill
            was_running: list[str] = []
            if self._scheduler is not None:
                try:
                    was_running = self._scheduler.running_continuous()
                    self._scheduler.stop_all_continuous()
                except Exception as exc:
                    log.warning("could not stop watches before the drill: %s", exc)
            try:
                report = _drill.run_drill(site, load_config(), progress=_note, headless=headless)
                with self._inspect_lock:
                    self._inspections[key] = {"status": "done", "site": site,
                                              "progress": "done", "report": report}
            except Exception as exc:
                log.warning("drill failed: %s", exc)
                with self._inspect_lock:
                    self._inspections[key] = {"status": "error", "site": site, "error": str(exc)}
            finally:
                for name in was_running:
                    try:
                        self._scheduler.start_continuous(name)
                    except Exception as exc:
                        log.warning("could not restart %r after the drill: %s", name, exc)

        threading.Thread(target=_run, daemon=True, name="ww-drill").start()
        return self._inspections[key]

    def drill_status(self) -> dict:
        with self._inspect_lock:
            return dict(self._inspections.get("drill") or {"status": "idle"})

    # ------------------------------------------------------------------
    # Ollama
    # ------------------------------------------------------------------

    def _start_ollama(self) -> None:
        with self._lock:
            if self._states["ollama"].status == Status.RUNNING:
                return
            self._states["ollama"].status = Status.STARTING
            self._states["ollama"].error  = None

        threading.Thread(target=self._do_start_ollama, daemon=True,
                         name="ww-ollama-start").start()

    def _do_start_ollama(self) -> None:
        # Adopt if already running
        if self._ollama_reachable():
            with self._lock:
                self._states["ollama"].status     = Status.RUNNING
                self._states["ollama"].started_at = time.time()
                self._ollama_adopted = True
            log.info("Adopted existing Ollama instance")
            return

        # Launch a new instance. CREATE_NO_WINDOW so ollama.exe doesn't pop its own console
        # window when we're launched windowless (pythonw) — without it, a GUI parent spawning a
        # console app makes Windows allocate a visible blank "ollama.exe" terminal.
        try:
            self._ollama_proc   = subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._ollama_adopted = False
        except FileNotFoundError:
            msg = "ollama not found in PATH — install from https://ollama.com"
            log.error(msg)
            with self._lock:
                self._states["ollama"].status = Status.ERROR
                self._states["ollama"].error  = msg
            return

        # Wait until ready
        deadline = time.time() + OLLAMA_TIMEOUT
        while time.time() < deadline:
            if self._ollama_reachable():
                with self._lock:
                    self._states["ollama"].status     = Status.RUNNING
                    self._states["ollama"].started_at = time.time()
                log.info("Ollama started")
                return
            time.sleep(0.5)

        msg = f"Ollama did not respond within {OLLAMA_TIMEOUT:.0f}s"
        log.error(msg)
        with self._lock:
            self._states["ollama"].status = Status.ERROR
            self._states["ollama"].error  = msg

    def _stop_ollama(self) -> None:
        if not self._ollama_adopted and self._ollama_proc is not None:
            self._ollama_proc.terminate()
            self._ollama_proc = None
            log.info("Ollama process terminated")
        elif self._ollama_adopted:
            log.info("Ollama was adopted — leaving it running")
        with self._lock:
            self._states["ollama"].status     = Status.STOPPED
            self._states["ollama"].started_at = None
            self._ollama_adopted = False

    def _ollama_reachable(self) -> bool:
        try:
            r = httpx.get(f"{OLLAMA_URL}/", timeout=1.5)
            return r.is_success or r.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Server (uvicorn / FastAPI)
    # ------------------------------------------------------------------

    def _start_server(self) -> None:
        import uvicorn
        from web_watcher.dashboard.server import create_app

        with self._lock:
            if self._states["server"].status == Status.RUNNING:
                log.warning("Server already running — skipping start")
                return
            self._states["server"].status = Status.STARTING
            self._states["server"].error  = None

        app    = create_app(self)
        config = uvicorn.Config(app, host="127.0.0.1", port=self.PORT,
                                log_level="info", access_log=True)
        self._uvicorn_server = uvicorn.Server(config)

        def _run() -> None:
            try:
                with self._lock:
                    self._states["server"].status     = Status.RUNNING
                    self._states["server"].started_at = time.time()
                self._uvicorn_server.run()
            except Exception as exc:
                log.error("Dashboard server crashed: %s", exc)
                with self._lock:
                    self._states["server"].status = Status.ERROR
                    self._states["server"].error  = str(exc)

        self._server_thread = threading.Thread(target=_run, daemon=True, name="ww-server")
        self._server_thread.start()

    def _stop_server(self) -> None:
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        with self._lock:
            self._states["server"].status     = Status.STOPPED
            self._states["server"].started_at = None

    # ------------------------------------------------------------------
    # Scheduler (APScheduler)
    # ------------------------------------------------------------------

    def _start_scheduler(self) -> None:
        from web_watcher.scheduler import WatchScheduler

        with self._lock:
            if self._states["scheduler"].status == Status.RUNNING:
                return
            self._states["scheduler"].status = Status.STARTING
            self._states["scheduler"].error  = None

        try:
            self._scheduler = WatchScheduler()
            # Let the continuous loop voice things (e.g. the "exploring this site first"
            # heads-up) into The Watcher's feed.
            self._scheduler._narrator = self.narrate
            self._scheduler.start()
            self._start_oversight()   # narrator rides with the scheduler
            with self._lock:
                self._states["scheduler"].status     = Status.RUNNING
                self._states["scheduler"].started_at = time.time()
        except Exception as exc:
            log.error("Scheduler failed to start: %s", exc)
            with self._lock:
                self._states["scheduler"].status = Status.ERROR
                self._states["scheduler"].error  = str(exc)

    def _stop_scheduler(self) -> None:
        self._stop_orchestrator()     # stop the driver before its browser/scheduler go away
        self._stop_oversight()        # quiet the narrator before the watches go away
        if self._scheduler is not None:
            try:
                self._scheduler.stop()
            except Exception:
                pass
        with self._lock:
            self._states["scheduler"].status     = Status.STOPPED
            self._states["scheduler"].started_at = None

    # ------------------------------------------------------------------
    # Oversight agent (the visible narrator over all watches)
    # ------------------------------------------------------------------

    def _start_oversight(self) -> None:
        from web_watcher.oversight import OversightAgent
        try:
            if self._oversight is None:
                self._oversight = OversightAgent(self)
            self._oversight.start()
        except Exception as exc:
            # The narrator is a nicety — never let it block the scheduler coming up.
            log.warning("Oversight agent failed to start: %s", exc)

    def _stop_oversight(self) -> None:
        if self._oversight is not None:
            try:
                self._oversight.stop()
            except Exception:
                pass

    def oversight_snapshot(self, limit: int = 40) -> dict:
        """Live narration feed + current watch state for the Watches-tab panel."""
        if self._oversight is None:
            return {"running": False, "updated_at": None, "entries": [], "watches": []}
        return self._oversight.snapshot(limit=limit)

    def narrate(self, kind: str, text: str, watch: str | None = None) -> None:
        """Post a narration line into The Watcher's feed from outside the oversight loop
        (e.g. background site-exploration progress). No-op if oversight isn't running."""
        if self._oversight is not None:
            try:
                self._oversight.note(kind, text, watch=watch)
            except Exception:
                pass
