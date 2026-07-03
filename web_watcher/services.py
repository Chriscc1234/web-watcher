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
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import httpx

log = logging.getLogger(__name__)

OLLAMA_URL     = "http://localhost:11434"
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
        self._orchestrator    = None   # Orchestrator — the single driver (opt-in)
        self._ollama_proc:    Optional[subprocess.Popen] = None
        self._ollama_adopted  = False   # True when we adopted an existing instance
        # Facebook (login-profile) connect flow state, surfaced via get_statuses().
        self._fb_connect_status = "idle"  # idle | opening | waiting_for_login | done | error
        # Auto-update state (populated by the background checker; surfaced via /api/update).
        self._window          = None    # pywebview window — set by main.py, used to restart
        self._update_available = None   # dict {version, notes} when a newer release is staged
        self._update_thread:  Optional[threading.Thread] = None
        self._update_stop     = threading.Event()

    # ------------------------------------------------------------------
    # Bulk lifecycle
    # ------------------------------------------------------------------

    def start_all(self) -> None:
        self._start_ollama()    # first: scheduler needs Ollama available
        self._start_server()
        self._start_scheduler()
        self._start_update_checker()

    def stop_all(self) -> None:
        self._update_stop.set()
        self._stop_scheduler()
        self._stop_server()
        self._stop_ollama()     # last: don't kill Ollama while scheduler is live

    # ------------------------------------------------------------------
    # Auto-update (checks GitHub Releases; stages in the background; the UI
    # notifies and a one-click restart applies it via launcher.py)
    # ------------------------------------------------------------------

    _UPDATE_CHECK_INTERVAL = 6 * 3600   # re-check every 6 hours
    _UPDATE_CHECK_DELAY    = 25         # first check shortly after launch (let the UI settle)

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
        restart can apply it. Returns the current update status. Safe to call anytime."""
        from web_watcher import updater
        from web_watcher.__version__ import __version__
        # Already staged? then we're done — surface it.
        staged = updater.pending_update()
        if staged:
            self._update_available = {"version": staged, "notes": (self._update_available or {}).get("notes", "")}
            return self.update_status()
        info = updater.check_for_update(__version__)
        if info is None:
            return self.update_status()
        if updater.download_and_stage(info) is not None:
            self._update_available = {"version": info.version, "notes": info.notes}
            log.info("update %s staged and ready to apply", info.version)
        return self.update_status()

    def update_status(self) -> dict:
        from web_watcher import updater
        from web_watcher.__version__ import __version__
        staged = updater.pending_update()
        return {
            "current":   __version__,
            "available": self._update_available,     # {version, notes} or None
            "staged":    bool(staged),               # downloaded + ready to apply on restart
            "configured": bool(updater.GITHUB_OWNER),
        }

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
        self._scheduler.run_now(watch_name)

    def get_job_info(self) -> list[dict]:
        return self._scheduler.get_job_info() if self._scheduler else []

    def reload_scheduler(self) -> None:
        if self._scheduler:
            self._scheduler.reload()
        # While the orchestrator drives, the scheduler's own continuous-thread count is
        # 0 by design — that reload line ("0 continuous watch(es) running") looks alarming
        # but isn't. Clarify that The Watcher still owns the watches and picks up the edit.
        if self.orchestrator_running():
            n = len(self.orchestrator_status().get("topics", []))
            log.info("Config reloaded — The Watcher (orchestrator) is driving %d continuous "
                     "watch(es) and will apply the change on its next cycle", n)

    # ------------------------------------------------------------------
    # Continuous watches
    # ------------------------------------------------------------------

    def start_continuous(self, watch_name: str) -> None:
        if self._scheduler is None:
            raise RuntimeError("Scheduler is not running")
        # While the orchestrator is driving, it owns the continuous watches — starting a
        # per-watch thread too would double-sweep the same site. Ignore (The Watcher is
        # already on it).
        if self.orchestrator_running():
            log.info("Orchestrator is running — %r is serviced by it; ignoring per-watch start", watch_name)
            return
        self._scheduler.start_continuous(watch_name)
        self._nudge_oversight()

    def stop_continuous(self, watch_name: str) -> None:
        if self._scheduler is None:
            raise RuntimeError("Scheduler is not running")
        self._scheduler.stop_continuous(watch_name)
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

    def orchestrator_running(self) -> bool:
        return bool(self._orchestrator and self._orchestrator.is_running())

    def orchestrator_status(self) -> dict:
        if self._orchestrator is None:
            return {"running": False, "current": None, "cycles": 0, "topics": []}
        return self._orchestrator.status()

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
            with BrowserSession(
                headless=False, stealth=cfg.browser.stealth,
                persistent=True, profile_dir=cfg.browser.profile_dir,
            ) as session:
                page = session.new_page()
                page.goto("https://www.facebook.com/", timeout=60_000, wait_until="domcontentloaded")
                self._fb_connect_status = "waiting_for_login"
                log.info("Facebook login window open — waiting for user to sign in and close it")
                session.wait_until_closed(poll_seconds=1.0, timeout=600.0)
            self._fb_connect_status = "done"
            log.info("Facebook connect complete — profile session saved")
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

        # Launch a new instance
        try:
            self._ollama_proc   = subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
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
