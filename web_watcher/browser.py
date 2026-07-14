"""
Playwright browser controller.

Provides navigate / click-path execution / text extraction / screenshot
for a single watch run. Caller is responsible for starting/stopping the
browser context (use BrowserSession as a context manager).

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  _PROFILE_DIR       ~L63    On-disk Chrome profile dir for login-required sites
  PageResult         ~L195   Dataclass: url, text, screenshot_bytes, error
  BrowserSession     ~L202   Context manager: launches Chrome, run_watch(), new_page()
  maybe_warm_homepage ~L80   Sometimes visit site root before a deep search URL (anti-tell)
  _enter_persistent  ~L255   launch_persistent_context path (reused login profile)
  _build_ctx_kwargs  ~L300   Dynamic UA (real bundled Chromium ver) + randomized viewport
  _session_fingerprint_js ~L340 Per-session cores/memory/screen overrides
  _enter_ephemeral   ~L360   default ephemeral context + storage_state snapshot
  run_watch()        ~L420   Visit each URL, run click-path, extract text/screenshot
  _apply_stealth()   ~L515   JS patches injected for bot-detection evasion
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import random
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeoutError,
    sync_playwright,
)

from web_watcher.config import ClickStep, Watch

log = logging.getLogger(__name__)

# Default timeouts (ms)
NAV_TIMEOUT = 30_000

# Chrome launch args that reduce automation fingerprinting
_STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--disable-notifications",
    "--start-maximized",
    "--lang=en-US",
]
ACTION_TIMEOUT = 10_000
SCREENSHOT_TIMEOUT = 15_000

# Fallback Chrome version for the UA when we can't read the bundled build's real version
# (persistent/installed-channel launches). Kept reasonably current; the ephemeral path
# overrides this with the ACTUAL bundled Chromium version so the UA never goes stale and
# mismatches the engine — a classic fingerprint tell we were previously guilty of.
_DEFAULT_CHROME_FULL = "124.0.6367.243"

# A small pool of real, common desktop viewport sizes. Using a FIXED 1920x1080 every
# session is itself a (weak) signal; rotating across realistic sizes is more human.
_VIEWPORT_POOL = [
    (1920, 1080), (1536, 864), (1600, 900), (1680, 1050), (1440, 900), (1366, 768),
]

# Self-consistent (cores, memory-GB) pairs — a 4-core machine reporting 32 GB, or a
# 16-core reporting 4 GB, is incoherent and flaggable. Pick a plausible pairing.
_HARDWARE_POOL = [(4, 8), (6, 8), (8, 8), (8, 16), (12, 16), (16, 16)]


# A visible fake cursor injected into the agent's pages so you can SEE where its synthetic
# mouse is. It follows the real mousemove events Playwright dispatches (page.mouse.move), and
# pulses on press. pointer-events:none so it never intercepts. Re-ensures itself on SPA nav.
_CURSOR_JS = r"""
(function () {
    function ensure() {
        if (document.getElementById('__ww_cursor__')) return;
        var root = document.body || document.documentElement;
        if (!root) return;
        var c = document.createElement('div');
        c.id = '__ww_cursor__';
        c.style.cssText = 'position:fixed;left:0;top:0;width:20px;height:20px;z-index:2147483647;'
            + 'pointer-events:none;border-radius:50%;background:rgba(255,64,64,.30);'
            + 'border:2px solid #ff4040;box-shadow:0 0 10px rgba(255,64,64,.85);'
            + 'transform:translate(-100px,-100px);transition:transform .05s linear,'
            + 'background .1s,width .1s,height .1s;margin:-11px 0 0 -11px';
        root.appendChild(c);
        document.addEventListener('mousemove', function (e) {
            c.style.transform = 'translate(' + e.clientX + 'px,' + e.clientY + 'px)';
        }, true);
        document.addEventListener('mousedown', function () {
            c.style.background = 'rgba(255,64,64,.75)'; c.style.width = '14px'; c.style.height = '14px';
        }, true);
        document.addEventListener('mouseup', function () {
            c.style.background = 'rgba(255,64,64,.30)'; c.style.width = '20px'; c.style.height = '20px';
        }, true);
    }
    if (document.body) ensure();
    else document.addEventListener('DOMContentLoaded', ensure);
    setInterval(ensure, 1500);   // SPA route changes can wipe the node — put it back
})();
"""


def maybe_warm_homepage(page: Page, url: str, prob: float = 0.4) -> None:
    """With probability `prob`, visit the site's homepage briefly before navigating to a
    deep search/category URL — humans rarely teleport straight to a deep results URL.
    Best-effort and silent; any failure just falls through to the caller's own goto."""
    if random.random() > prob:
        return
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        if not p.scheme or not p.netloc:
            return
        home = f"{p.scheme}://{p.netloc}/"
        if home.rstrip("/") == url.rstrip("/"):
            return
        page.goto(home, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
        page.wait_for_timeout(int(random.uniform(500, 1400)))
    except Exception as exc:
        log.debug("homepage warm-up skipped: %s", exc)

# Persistent browser state — cookies + localStorage survive across runs so sites
# see a returning user instead of a fresh bot session each time.
from web_watcher import paths
_DATA_DIR     = paths.data_dir()
_BROWSER_STATE = paths.browser_state_path()

# Persistent on-disk Chrome profile for login-required sites (e.g. Facebook).
# Unlike the storage_state JSON snapshot above, this is a full user-data-dir that
# Chrome owns (cookies, localStorage, service workers, fingerprint continuity), so a
# one-time manual login survives indefinitely. Only ONE browser may use a given
# profile dir at a time (Chromium SingletonLock).
_PROFILE_DIR = paths.profile_dir()

# Serializes access to the shared persistent profile dir. Two continuous watches
# that both use_login_profile (or a watch + the Connect-Facebook flow) would
# otherwise launch_persistent_context on the same dir at once and the second would
# crash on Chromium's SingletonLock. Holders wait their turn instead.
_PROFILE_LOCK = threading.Lock()
_PROFILE_ACQUIRE_TIMEOUT = 120.0  # seconds to wait for the profile before giving up

# JS injected into every page before any site script.  Supplements playwright-stealth
# with patches it doesn't cover: CDP window artifacts and Notification API.
_EXTRA_STEALTH_JS = """
(function () {

    // ── CDP artifact cleanup ─────────────────────────────────────────────────
    // Chrome DevTools Protocol leaves cdc_* / $cdc_* properties in window.
    // DataDome and PerimeterX scan for these as a definitive bot signal.
    var names = Object.getOwnPropertyNames(window);
    for (var i = 0; i < names.length; i++) {
        var k = names[i];
        if (k.indexOf('cdc_') === 0 || k.indexOf('$cdc_') === 0) {
            try { delete window[k]; } catch (e) {}
        }
    }

    // ── navigator.webdriver ──────────────────────────────────────────────────
    // playwright-stealth may not patch this in Playwright >= 1.40 because
    // Blink injects it at a lower level than a normal JS override.
    // Object.defineProperty with configurable:false prevents re-detection.
    try {
        Object.defineProperty(navigator, 'webdriver', {
            get: function () { return false; },
            configurable: false,
        });
    } catch (e) {}

    // ── WebGL renderer / vendor strings ─────────────────────────────────────
    // Headless Chromium reports SwiftShader (software rasteriser).
    // DataDome whitelists known real GPU strings and blocks SwiftShader.
    try {
        var getParam = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function (p) {
            // WEBGL_debug_renderer_info: UNMASKED_VENDOR (37445) / RENDERER (37446)
            if (p === 37445) return 'Google Inc. (NVIDIA)';
            if (p === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)';
            return getParam.call(this, p);
        };
    } catch (e) {}

    // ── window.chrome completeness ────────────────────────────────────────────
    // Real Chrome exposes chrome.runtime, chrome.loadTimes, chrome.csi.
    // Playwright Chromium leaves these absent or stub-only.
    try {
        if (!window.chrome) window.chrome = {};
        if (!window.chrome.runtime) {
            window.chrome.runtime = {
                id: '',
                connect: function () {},
                sendMessage: function () {},
                onMessage: { addListener: function () {}, removeListener: function () {} },
                PlatformOs: { WIN: 'win', MAC: 'mac', ANDROID: 'android', LINUX: 'linux' },
            };
        }
        if (!window.chrome.loadTimes) {
            window.chrome.loadTimes = function () {
                return {
                    requestTime: performance.timing.navigationStart / 1000,
                    startLoadTime: performance.timing.navigationStart / 1000,
                    commitLoadTime: performance.timing.responseStart / 1000,
                    finishDocumentLoadTime: performance.timing.domContentLoadedEventEnd / 1000,
                    finishLoadTime: performance.timing.loadEventEnd / 1000,
                    firstPaintTime: 0, firstPaintAfterLoadTime: 0,
                    navigationType: 'Other', wasFetchedViaSpdy: false,
                    wasNpnNegotiated: false, npnNegotiatedProtocol: 'h2',
                    wasAlternateProtocolAvailable: false, connectionInfo: 'h2',
                };
            };
        }
        if (!window.chrome.csi) {
            window.chrome.csi = function () {
                return {
                    startE: performance.timing.navigationStart,
                    onloadT: performance.timing.loadEventEnd,
                    pageT: performance.now(),
                    tran: 15,
                };
            };
        }
    } catch (e) {}

    // ── Notification.permission ───────────────────────────────────────────────
    // Headless Chromium defaults to 'denied'; real browsers default to 'default'.
    if (typeof Notification !== 'undefined') {
        try {
            Object.defineProperty(Notification, 'permission', {
                get: function () { return 'default'; },
                configurable: true,
            });
        } catch (e) {}
    }

    // ── navigator properties ──────────────────────────────────────────────────
    // Align with the Windows Chrome UA we declared in the context.
    try {
        Object.defineProperty(navigator, 'platform',          { get: function () { return 'Win32'; }, configurable: true });
        Object.defineProperty(navigator, 'hardwareConcurrency', { get: function () { return 8; },     configurable: true });
        Object.defineProperty(navigator, 'deviceMemory',      { get: function () { return 8; },       configurable: true });
    } catch (e) {}

    // navigator.connection.effectiveType
    try {
        if (navigator.connection) {
            Object.defineProperty(navigator.connection, 'effectiveType', {
                get: function () { return '4g'; }, configurable: true,
            });
        }
    } catch (e) {}

    // ── screen dimensions ─────────────────────────────────────────────────────
    // Ensure screen properties are consistent with a 1920×1080 consumer display.
    try {
        ['width','height','availWidth','availHeight','colorDepth','pixelDepth'].forEach(function (k) {
            var val = { width: 1920, height: 1080, availWidth: 1920, availHeight: 1040,
                        colorDepth: 24, pixelDepth: 24 }[k];
            if (screen[k] !== val) {
                Object.defineProperty(screen, k, { get: function () { return val; }, configurable: true });
            }
        });
        Object.defineProperty(window, 'outerWidth',  { get: function () { return 1920; }, configurable: true });
        Object.defineProperty(window, 'outerHeight', { get: function () { return 1040; }, configurable: true });
    } catch (e) {}

})();
"""


@dataclass
class PageResult:
    url: str
    text: str = ""
    screenshot_bytes: bytes | None = None
    error: str | None = None


class BrowserSession:
    """
    Context manager wrapping a single Playwright Chromium browser instance.

    Usage:
        with BrowserSession(headless=True) as session:
            results = session.run_watch(watch)
    """

    def __init__(
        self,
        headless: bool = True,
        stealth: bool = True,   # accepted for compat; fingerprint hardening is ALWAYS on (see below)
        persistent: bool = False,
        profile_dir: str | Path | None = None,
        show_cursor: bool = False,
        geolocation: tuple[float, float] | None = None,
    ) -> None:
        self._headless = headless
        # (lat, lon) to report via the Geolocation API — so sites that show "your area"
        # from location (OfferUp, store locators) show the WATCH's area, not a default
        # (a fresh automated browser has no location → OfferUp was serving Florida junk).
        self._geolocation = geolocation
        # NOTE: `stealth` is deliberately unused. The fingerprint patches (webdriver/UA/cores/
        # memory/screen) are applied to EVERY session — turning them off has no user benefit and
        # a bare Playwright fingerprint gets instantly flagged. What the Settings "stealth" flag
        # actually controls is human PACING (typing searches, scroll/pause jitter), and that is
        # read where the pacing happens: cfg.browser.stealth in scheduler.py/monitor.py.
        self._show_cursor = show_cursor
        # Persistent mode uses an on-disk Chrome profile (launch_persistent_context)
        # instead of an ephemeral context + storage_state snapshot. Used for
        # login-required sites so a one-time manual sign-in is reused on every run.
        self._persistent  = persistent
        self._profile_dir = Path(profile_dir) if profile_dir else _PROFILE_DIR
        self._profile_lock_held = False
        self._pw:      Playwright | None     = None
        self._browser: Browser | None        = None   # stays None in persistent mode
        self._context: BrowserContext | None = None
        # Per-session fingerprint choices, fixed for the session's lifetime so a single
        # browsing session is internally consistent (it doesn't change cores/screen
        # mid-session) while DIFFERING from run to run.
        self._viewport       = random.choice(_VIEWPORT_POOL)
        self._cores, self._mem_gb = random.choice(_HARDWARE_POOL)
        self._chrome_full    = _DEFAULT_CHROME_FULL   # overridden once the real build is known

    def _build_ctx_kwargs(self) -> dict:
        """Context options shared by ephemeral and persistent launch paths. Uses a UA
        matched to the ACTUAL bundled Chromium version (set in _enter_ephemeral) and a
        per-session randomized viewport so the fingerprint isn't a fixed constant."""
        major = (self._chrome_full.split(".")[0] or "124")
        ctx_kwargs: dict = {
            "locale": "en-US",
            # Real Windows Chrome UA whose version tracks the engine we're actually running,
            # so DataDome's UA/engine and platform cross-checks line up.
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{self._chrome_full} Safari/537.36"
            ),
            "extra_http_headers": {
                "Accept-Language":  "en-US,en;q=0.9",
                "Sec-CH-UA":        f'"Chromium";v="{major}", "Google Chrome";v="{major}", '
                                    f'"Not?A_Brand";v="24"',
                "Sec-CH-UA-Platform": '"Windows"',
                "Sec-CH-UA-Mobile": "?0",
            },
        }
        # In visible mode let --start-maximized own the window size; forcing a
        # fixed viewport would create a mismatch with the actual window frame.
        if self._headless:
            w, h = self._viewport
            ctx_kwargs["viewport"] = {"width": w, "height": h}
        else:
            ctx_kwargs["no_viewport"] = True
        # Report the watch's location so IP/geo-based sites (OfferUp, store locators) show
        # the right area instead of a default. Grant the permission so the API doesn't prompt.
        if self._geolocation:
            lat, lon = self._geolocation
            ctx_kwargs["geolocation"]  = {"latitude": float(lat), "longitude": float(lon)}
            ctx_kwargs["permissions"]  = ["geolocation"]
        return ctx_kwargs

    def _session_fingerprint_js(self) -> str:
        """A tiny per-session init script that overrides hardwareConcurrency / deviceMemory
        / screen size with this session's randomized-but-coherent values. Injected AFTER the
        static stealth script so these win, and applied before any site JS runs."""
        w, h = self._viewport
        return f"""
        (function () {{
            try {{
                Object.defineProperty(navigator, 'hardwareConcurrency',
                    {{ get: function () {{ return {self._cores}; }}, configurable: true }});
                Object.defineProperty(navigator, 'deviceMemory',
                    {{ get: function () {{ return {self._mem_gb}; }}, configurable: true }});
                Object.defineProperty(screen, 'width',  {{ get: function () {{ return {w}; }}, configurable: true }});
                Object.defineProperty(screen, 'height', {{ get: function () {{ return {h}; }}, configurable: true }});
            }} catch (e) {{}}
        }})();
        """

    def __enter__(self) -> "BrowserSession":
        self._pw = sync_playwright().start()
        if self._persistent:
            self._enter_persistent()
        else:
            self._enter_ephemeral()
        # Inject our supplemental stealth patches on every new page before site JS runs,
        # then this session's randomized fingerprint values (cores/memory/screen) on top.
        assert self._context is not None
        self._context.add_init_script(_EXTRA_STEALTH_JS)
        self._context.add_init_script(self._session_fingerprint_js())
        # Visible fake cursor (opt-in) so you can watch where the agent's mouse goes.
        if self._show_cursor:
            self._context.add_init_script(_CURSOR_JS)
        return self

    def _enter_persistent(self) -> None:
        """Launch a persistent-profile context (full user-data-dir on disk)."""
        # Serialize shared-profile access so concurrent login-profile watches don't
        # collide on Chromium's SingletonLock. Wait, then fail clearly if still busy.
        if not _PROFILE_LOCK.acquire(timeout=_PROFILE_ACQUIRE_TIMEOUT):
            raise RuntimeError(
                f"Login profile {self._profile_dir.name!r} is busy (another watch or the "
                "Connect-Facebook window is using it) — skipping this run."
            )
        self._profile_lock_held = True
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        ctx_kwargs = self._build_ctx_kwargs()
        last_exc: Exception | None = None
        for channel in ("chrome", "msedge", None):
            try:
                self._context = self._pw.chromium.launch_persistent_context(
                    user_data_dir=str(self._profile_dir),
                    headless=self._headless,
                    args=_STEALTH_ARGS,
                    **({"channel": channel} if channel else {}),
                    **ctx_kwargs,
                )
                log.info(
                    "Persistent browser launched (profile=%s) via channel=%r",
                    self._profile_dir.name, channel or "bundled",
                )
                return
            except Exception as exc:
                last_exc = exc
                log.debug("Persistent launch via channel=%r failed: %s", channel, exc)
        # All channels failed — release the profile lock before raising, since
        # __exit__ is NOT called when __enter__ raises (would otherwise leak the lock).
        if self._profile_lock_held:
            _PROFILE_LOCK.release()
            self._profile_lock_held = False
        raise RuntimeError(
            f"Could not launch persistent browser profile at {self._profile_dir} "
            f"(is it already in use by another window?): {last_exc}"
        )

    def _enter_ephemeral(self) -> None:
        """Launch an ephemeral context, reusing storage_state if present."""
        if not self._headless:
            for channel in ("chrome", "msedge", None):
                try:
                    self._browser = self._pw.chromium.launch(
                        headless=False,
                        args=_STEALTH_ARGS,
                        **( {"channel": channel} if channel else {} ),
                    )
                    log.info("Browser launched (visible) via channel=%r", channel or "bundled")
                    break
                except Exception as exc:
                    log.debug("Browser launch via channel=%r failed: %s", channel, exc)
        else:
            self._browser = self._pw.chromium.launch(
                headless=True,
                args=_STEALTH_ARGS,
            )
        if self._browser is None:
            raise RuntimeError("Could not launch any browser in visible mode")

        # Pin the UA to the ACTUAL bundled/installed Chromium version so the UA string and
        # the engine can't disagree (a stale hardcoded version was a real fingerprint tell).
        try:
            ver = (self._browser.version or "").strip()
            if ver and ver[0].isdigit():
                self._chrome_full = ver
        except Exception:
            pass

        # Build a context so cookies / localStorage survive across runs via the
        # storage_state snapshot. Sites recognise returning users and skip challenges.
        storage_state = str(_BROWSER_STATE) if _BROWSER_STATE.exists() else None
        ctx_kwargs = self._build_ctx_kwargs()
        if storage_state:
            ctx_kwargs["storage_state"] = storage_state
        try:
            self._context = self._browser.new_context(**ctx_kwargs)
        except Exception as exc:
            log.warning("Could not load saved browser state (%s) — starting fresh", exc)
            ctx_kwargs.pop("storage_state", None)
            self._context = self._browser.new_context(**ctx_kwargs)

    def __exit__(self, *_) -> None:
        if self._context:
            # Persistent mode: the profile dir on disk IS the source of truth, so
            # there is no storage_state to save and no block-page state to discard.
            # Just close the context to release the SingletonLock for the next run.
            if not self._persistent:
                try:
                    _DATA_DIR.mkdir(exist_ok=True)
                    if self._session_was_blocked():
                        # If we ended on a block/CAPTCHA page the entire session is
                        # tainted — Walmart embeds the blocked uuid/vid in session
                        # cookies so loading the state next time triggers an instant
                        # re-block regardless of which specific cookie we clear.
                        if _BROWSER_STATE.exists():
                            _BROWSER_STATE.unlink()
                        log.info("Session ended on block page — discarded browser state")
                    else:
                        self._clear_datadome_cookies()
                        self._context.storage_state(path=str(_BROWSER_STATE))
                        log.debug("Browser state saved to %s", _BROWSER_STATE)
                except Exception as exc:
                    log.warning("Could not save browser state: %s", exc)
            try:
                self._context.close()
            except Exception as exc:
                log.debug("Context close failed: %s", exc)
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()
        # Release the shared-profile lock (closing the context above freed the
        # on-disk SingletonLock; now let the next profile user proceed).
        if self._profile_lock_held:
            _PROFILE_LOCK.release()
            self._profile_lock_held = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def clear_datadome_cookies(self, domain: str = ".walmart.com") -> None:
        """Remove DataDome risk-scored cookies for a domain after a failed solve."""
        if self._context:
            try:
                self._context.clear_cookies(name="datadome", domain=domain)
                self._context.clear_cookies(name="datadome", domain=".captcha-delivery.com")
            except Exception as exc:
                log.debug("Could not clear datadome cookie: %s", exc)

    def _session_was_blocked(self) -> bool:
        """Return True if any open page in this context ended on a block/CAPTCHA URL."""
        if not self._context:
            return False
        try:
            import re as _re
            for page in self._context.pages:
                url = page.url or ''
                if _re.search(r'/blocked|captcha-delivery|datadome|geo\.captcha', url, _re.I):
                    log.info("Block URL detected in session: %s", url[:80])
                    return True
        except Exception:
            pass
        return False

    def _clear_datadome_cookies(self) -> None:
        """Strip all datadome cookies across any domain before persisting state."""
        if not self._context:
            return
        try:
            all_cookies = self._context.cookies()
            flagged = [c for c in all_cookies if c.get("name") == "datadome"]
            if flagged:
                log.info("Removing %d flagged datadome cookie(s) before saving state", len(flagged))
            for c in flagged:
                domain = c.get("domain", "")
                try:
                    self._context.clear_cookies(name="datadome", domain=domain)
                except Exception:
                    pass
        except Exception as exc:
            log.debug("datadome cookie sweep failed: %s", exc)

    def new_page(self) -> Page:
        """
        Open a new page in the shared browser context and apply stealth patches.

        Prefer this over accessing _browser or _context directly so that all
        pages — both run_watch and autonomous-agent pages — get the same stealth
        treatment and share the same cookie jar.
        """
        assert self._context is not None
        page = self._context.new_page()
        _apply_stealth(page)
        return page

    def wait_until_closed(self, poll_seconds: float = 1.0, timeout: float | None = None) -> None:
        """
        Block until the user closes the browser window (all pages gone) or timeout.

        Used by manual flows like the Facebook login: open a visible window, let the
        user sign in, and return once they close it (the persistent profile keeps the
        session on disk). Safe to call inside the context-manager `with` block.
        """
        import time as _t
        start = _t.monotonic()
        while True:
            try:
                if self._context is None or not self._context.pages:
                    return
            except Exception:
                return  # context/browser already torn down by the user closing it
            if timeout is not None and (_t.monotonic() - start) > timeout:
                log.info("wait_until_closed timed out after %.0fs", timeout)
                return
            _t.sleep(poll_seconds)

    def run_watch(self, watch: Watch, screenshot: bool = False) -> list[PageResult]:
        """
        Visit every URL in the watch, execute the click-path, then extract
        text (and optionally a screenshot) from the final page state.

        Returns one PageResult per URL. Errors are captured per-result;
        they never propagate to the caller.
        """
        results: list[PageResult] = []
        for url in watch.urls:
            results.append(self._process_url(url, watch.click_path, screenshot))
        return results

    def extract_text(self, page: Page) -> str:
        """Return visible inner text of the page body."""
        try:
            return page.inner_text("body", timeout=ACTION_TIMEOUT)
        except Exception as exc:
            log.warning("extract_text failed: %s", exc)
            return ""

    def screenshot(self, page: Page) -> bytes | None:
        """Return a full-page PNG screenshot as bytes, or None on failure."""
        try:
            return page.screenshot(full_page=True, timeout=SCREENSHOT_TIMEOUT)
        except Exception as exc:
            log.warning("screenshot failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _process_url(
        self,
        url: str,
        click_path: list[ClickStep],
        want_screenshot: bool,
    ) -> PageResult:
        page = self.new_page()
        result = PageResult(url=url)
        try:
            self._navigate(page, url)
            self._run_click_path(page, click_path)
            result.text = self.extract_text(page)
            if want_screenshot:
                result.screenshot_bytes = self.screenshot(page)
        except PWTimeoutError as exc:
            msg = f"Navigation timeout for {url}: {exc}"
            log.error(msg)
            result.error = msg
        except Exception as exc:
            msg = f"Unexpected error for {url}: {exc}"
            log.error(msg)
            result.error = msg
        finally:
            page.close()
        return result

    def _navigate(self, page: Page, url: str) -> None:
        log.debug("Navigating to %s", url)
        page.goto(url, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")

    def _run_click_path(self, page: Page, steps: list[ClickStep]) -> None:
        for i, step in enumerate(steps):
            try:
                self._execute_step(page, step)
            except PWTimeoutError as exc:
                log.warning("Click-path step %d timed out (%s %s): %s", i, step.action, step.target, exc)
                # Continue remaining steps rather than aborting
            except Exception as exc:
                log.warning("Click-path step %d failed (%s %s): %s", i, step.action, step.target, exc)

    def _execute_step(self, page: Page, step: ClickStep) -> None:
        action = step.action

        if action == "click":
            _locator(page, step.target).click(timeout=ACTION_TIMEOUT)

        elif action == "select":
            # target = "selector::value" — split on last "::"
            if "::" not in (step.target or ""):
                raise ValueError(f"'select' target must be 'selector::value', got: {step.target!r}")
            selector, value = step.target.rsplit("::", 1)
            page.select_option(selector, value, timeout=ACTION_TIMEOUT)

        elif action == "scroll":
            page.evaluate(f"window.scrollBy(0, {step.amount})")

        elif action == "wait_for_selector":
            page.wait_for_selector(step.target, timeout=ACTION_TIMEOUT)

        elif action == "wait_ms":
            page.wait_for_timeout(step.amount or 0)

        else:
            raise ValueError(f"Unknown action: {action!r}")


def _apply_stealth(page: Page) -> None:
    try:
        from playwright_stealth import stealth_sync
        stealth_sync(page)
    except ImportError:
        pass


def _locator(page: Page, target: str | None):
    """
    Build a Playwright locator from a target string.

    Supports:
      - "text=Foo"      -> page.get_by_text("Foo")
      - "role=button"   -> page.get_by_role("button")
      - anything else   -> page.locator(target)  (CSS / XPath)
    """
    if not target:
        raise ValueError("click/wait step has no target")
    if target.startswith("text="):
        return page.get_by_text(target[5:], exact=False)
    if target.startswith("role="):
        return page.get_by_role(target[5:])
    return page.locator(target)
