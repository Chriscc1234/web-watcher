"""
Entry point — starts all services then opens the dashboard in a native app window.

Launch via:  python -m web_watcher.main
         or: start.bat (double-click)

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  _setup_logging()   ~L22   Per-session log files in data/logs/web_watcher_YYYYMMDD_HHMMSS.log
                             Keeps last _LOG_KEEP=30 files, prunes oldest on startup
  main()             ~L60   Startup sequence: logging → services → scheduler → webview
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

_LOG_DIR  = Path(__file__).parent.parent / "data" / "logs"
_LOG_FMT  = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_KEEP = 30  # number of session log files to retain


def _setup_logging() -> None:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Windows consoles default to cp1252, which can't encode the Unicode arrows
    # (→, ↳) used in agent log messages and raises UnicodeEncodeError in the console
    # handler. Force UTF-8 on the console streams (no-op if already UTF-8 or detached).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # Per-session file: web_watcher_20260621_143000.log
    session_ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_log = _LOG_DIR / f"web_watcher_{session_ts}.log"

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_LOG_FMT))
    root.addHandler(console)

    fh = logging.FileHandler(session_log, encoding="utf-8")
    fh.setFormatter(logging.Formatter(_LOG_FMT))
    root.addHandler(fh)

    # Quiet noisy third-party loggers. httpx logs every request at INFO, which
    # floods the log with the dashboard's 3s Ollama health-check (GET /api/tags)
    # and buries actual agent activity. WARNING keeps real failures visible.
    for noisy in ("httpx", "httpcore", "apscheduler.executors", "urllib3", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Prune oldest sessions beyond the keep limit
    logs = sorted(_LOG_DIR.glob("web_watcher_*.log"))
    for old in logs[:-_LOG_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass

_setup_logging()

DASHBOARD_URL   = "http://127.0.0.1:7878"
SERVER_TIMEOUT  = 10.0   # seconds to wait for uvicorn to accept connections

BASE_W, BASE_H = 1150, 780


class _WindowApi:
    """JS-callable bridge (exposed as window.pywebview.api) that resizes the NATIVE window
    to fit the selected text size — but ONLY when the window is not maximized. A maximized
    window is left alone (resizing it would un-maximize and fight the user, the exact bug
    the 0.14.1 accessibility fix removed). The web UI still counter-scales viewport units by
    --zoom, so this just gives the scaled layout a correctly-sized window to live in."""

    def __init__(self) -> None:
        self._window = None
        self._maximized = False

    def bind(self, window) -> None:
        self._window = window
        # Track maximized state so resize_for_zoom can skip it. Events fire on the GUI thread.
        try:
            window.events.maximized += lambda: self._set_max(True)
            window.events.restored  += lambda: self._set_max(False)
        except Exception as exc:
            log.debug("could not bind window state events: %s", exc)

    def _set_max(self, value: bool) -> None:
        self._maximized = value

    def resize_for_zoom(self, zoom) -> bool:
        """Resize the window to BASE size × zoom (clamped to the screen and the min size),
        so larger text gets a proportionally larger window. No-op when maximized. Returns
        True if a resize was applied."""
        try:
            if self._window is None or self._maximized:
                return False
            z = float(zoom) or 1.0
            w = max(900, round(BASE_W * z))
            h = max(600, round(BASE_H * z))
            # Never exceed the screen work area (leave a little margin for the taskbar).
            try:
                import webview
                scr = webview.screens[0]
                sw, sh = int(getattr(scr, "width", 0)), int(getattr(scr, "height", 0))
                if sw: w = min(w, int(sw * 0.96))
                if sh: h = min(h, int(sh * 0.92))
            except Exception:
                pass
            self._window.resize(w, h)
            return True
        except Exception as exc:
            log.debug("resize_for_zoom failed: %s", exc)
            return False


def _wait_for_server(timeout: float = SERVER_TIMEOUT) -> bool:
    """Poll /api/health until the server is up or we give up."""
    import httpx
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{DASHBOARD_URL}/api/health", timeout=1.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def main() -> None:
    import webview
    from web_watcher.services import ServiceManager

    manager = ServiceManager()

    log.info("Starting services...")
    manager.start_all()

    log.info("Waiting for dashboard server on port %d...", ServiceManager.PORT)
    ready = _wait_for_server()
    if not ready:
        log.warning("Server did not respond within %.0fs — opening window anyway", SERVER_TIMEOUT)

    log.info("Opening dashboard window")
    window_api = _WindowApi()
    window = webview.create_window(
        title="Web Watcher",
        url=DASHBOARD_URL,
        width=BASE_W,
        height=BASE_H,
        min_size=(900, 600),
        js_api=window_api,   # exposes window.pywebview.api.resize_for_zoom(zoom) to the UI
    )
    window_api.bind(window)
    manager._window = window   # lets the update flow close+relaunch the app

    def _on_closed() -> None:
        log.info("Window closed — shutting down services")
        manager.stop_all()

    window.events.closed += _on_closed

    # private_mode=False + a fixed storage_path so the webview PERSISTS localStorage/cookies
    # across launches. Default pywebview is private_mode=True, which discards them on exit —
    # that's why the chosen text size (and other UI prefs in localStorage) didn't survive a
    # restart. Keep the profile under data/ next to the rest of the app's state.
    storage_path = str(Path(__file__).parent.parent / "data" / "webview")
    Path(storage_path).mkdir(parents=True, exist_ok=True)
    # webview.start() blocks until the window is closed
    webview.start(private_mode=False, storage_path=storage_path)
    log.info("Web Watcher exited cleanly")


if __name__ == "__main__":
    main()
