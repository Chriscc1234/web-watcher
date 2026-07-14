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
        # Deep Inspect: on-demand deal/scam evaluation of one listing. url -> {status, verdict}.
        # status: running | done | error. Runs on a worker thread (browser + a slow big model).
        self._inspections: dict = {}
        self._inspect_lock = threading.Lock()
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
        # Wake the narrator so an added/removed/edited watch shows up in its feed and
        # per-watch view immediately, not a tick-interval later.
        self._nudge_oversight()

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
