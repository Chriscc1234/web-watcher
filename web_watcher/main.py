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

from web_watcher import paths
_LOG_DIR  = paths.log_dir()
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
            # Growing the window keeps its top-left anchored — re-clamp so the grown
            # window (and its title-bar buttons) stays fully on screen.
            _clamp_window_on_screen(self._window)
            return True
        except Exception as exc:
            log.debug("resize_for_zoom failed: %s", exc)
            return False


def _clamp_window_on_screen(window) -> None:
    """Move/shrink the window so it sits fully inside the primary screen's bounds.
    Windows sometimes places a new window mostly off the right edge (cascade placement on
    small/multi-DPI displays) — the title-bar buttons end up unreachable and the app looks
    broken on first open. Best-effort: any failure leaves the window where it is."""
    try:
        import webview
        scr = webview.screens[0]
        sw, sh = int(getattr(scr, "width", 0)), int(getattr(scr, "height", 0))
        if not sw or not sh:
            return
        w = min(int(window.width), sw)
        h = min(int(window.height), sh - 48)      # leave room for the taskbar
        if (w, h) != (int(window.width), int(window.height)):
            window.resize(w, h)
        x, y = int(window.x), int(window.y)
        nx = min(max(0, x), max(0, sw - w))
        ny = min(max(0, y), max(0, sh - h - 48))
        if (nx, ny) != (x, y):
            window.move(nx, ny)
    except Exception as exc:
        log.debug("could not clamp window on screen: %s", exc)


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
    # Give the app its own taskbar identity so Windows shows OUR icon (from the launching
    # shortcut) instead of the generic python one, and groups the taskbar button separately.
    _icon_path = Path(__file__).parent / "dashboard" / "static" / "icon.ico"
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("WebWatcher.App")
        except Exception as exc:
            log.debug("could not set AppUserModelID: %s", exc)

    window_api = _WindowApi()
    # The ?v= stamp makes every app version a DIFFERENT url to the embedded browser, so its cache
    # can never serve last version's page. Without it, WebView2 cached the dashboard HTML (served
    # with no Cache-Control) and kept rendering the old UI after updates — the version badge is
    # live JS so it showed the new number on the old page, which made updates look broken.
    from web_watcher.__version__ import __version__
    window = webview.create_window(
        title="Web Watcher",
        url=f"{DASHBOARD_URL}/?v={__version__}",
        width=BASE_W,
        height=BASE_H,
        min_size=(900, 600),
        js_api=window_api,   # exposes window.pywebview.api.resize_for_zoom(zoom) to the UI
    )
    window_api.bind(window)
    manager._window = window   # lets the update flow close+relaunch the app
    # Make sure the freshly-placed window is actually reachable (title bar + buttons on
    # screen) — the buddy's first launch opened far right with the buttons cut off.
    window.events.shown += lambda: _clamp_window_on_screen(window)

    def _on_closed() -> None:
        log.info("Window closed — shutting down services")
        manager.stop_all()

    window.events.closed += _on_closed

    # private_mode=False + a fixed storage_path so the webview PERSISTS localStorage/cookies
    # across launches. Default pywebview is private_mode=True, which discards them on exit —
    # that's why the chosen text size (and other UI prefs in localStorage) didn't survive a
    # restart. Keep the profile under data/ next to the rest of the app's state.
    storage_path = str(paths.webview_dir())
    Path(storage_path).mkdir(parents=True, exist_ok=True)
    # webview.start() blocks until the window is closed. Pass the app icon for the window +
    # taskbar (older pywebview builds may not accept `icon`; fall back gracefully).
    start_kwargs = {"private_mode": False, "storage_path": storage_path}
    if _icon_path.exists():
        start_kwargs["icon"] = str(_icon_path)
    try:
        webview.start(**start_kwargs)
    except TypeError:
        start_kwargs.pop("icon", None)
        webview.start(**start_kwargs)
    log.info("Web Watcher exited cleanly")


def _crash_dialog(exc: BaseException) -> None:
    """On a fatal startup error, show a native Windows message box instead of dying to a
    silent sand-timer. Points the user at the session log and the Report-a-bug flow so a
    non-technical buddy can tell us what happened. Best-effort — never raises."""
    import traceback
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        log.critical("FATAL startup error:\n%s", tb)
    except Exception:
        pass
    # Find the newest session log to name in the dialog.
    log_hint = ""
    try:
        logs = sorted(_LOG_DIR.glob("web_watcher_*.log"))
        if logs:
            log_hint = f"\n\nDetails were saved to:\n{logs[-1]}"
    except Exception:
        pass
    short = f"{type(exc).__name__}: {exc}"
    body = (
        "Web Watcher couldn't start.\n\n"
        f"{short}"
        f"{log_hint}\n\n"
        "Please send this log to Chris (or use the in-app \"Report a bug\" button "
        "next time it launches)."
    )
    if sys.platform == "win32":
        try:
            import ctypes
            # 0x10 = MB_ICONERROR, 0x40000 = MB_TOPMOST
            ctypes.windll.user32.MessageBoxW(0, body, "Web Watcher — startup error", 0x10 | 0x40000)
            return
        except Exception:
            pass
    # Non-Windows / no ctypes: at least print it where a console can see it.
    print(body, file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — last-resort UX guard
        _crash_dialog(exc)
        sys.exit(1)
