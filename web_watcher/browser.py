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
import re
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
    # NOT --disable-blink-features=AutomationControlled. It was here to hide automation, and
    # in current Chrome it does the OPPOSITE: --disable-blink-features is on Chrome's
    # bad-flags list, so the browser hangs a yellow "You are using an unsupported
    # command-line flag" banner across the top — a banner no ordinary browser shows, seen on
    # screen during a live Facebook run. The flag was also redundant: navigator.webdriver is
    # already forced to false by an init script (_EXTRA_STEALTH_JS), which is the property
    # the flag existed to influence, and that patch runs before any page script. Removing it
    # loses nothing and removes the most visible tell on the page.
    # The automation infobar the flag was often paired against is handled properly instead —
    # see ignore_default_args=["--enable-automation"] on both launch paths.
    # NOT --disable-features=IsolateOrigins,site-per-process. Three strikes:
    #   1. Chrome shows a yellow "You are using an unsupported command-line flag" banner for it,
    #      because it switches off a SECURITY feature — a banner no ordinary browser displays,
    #      which makes it a bot tell in its own right (the user spotted it on screen twice).
    #   2. It breaks cross-origin iframes, which is why the Connect-Facebook window already
    #      dropped it: Facebook's two-factor step rendered as a blank page with a lone Meta logo.
    #      The sweep path kept it — and the sweep path is now the one browsing Facebook.
    #   3. Nothing needs it any more: the one thing that wanted cross-origin reach (eBay's
    #      description iframe) is read through Playwright's frame API, which is unaffected by
    #      site isolation.
    "--no-first-run",
    "--no-default-browser-check",
    # NOT --disable-infobars: it has been a no-op since Chrome 76 (the switch was removed).
    # It cannot suppress the flag banner it was there to suppress, and an unknown switch is
    # one more thing to explain.
    # NOT --disable-notifications: that does not merely suppress prompts, it REMOVES the
    # Notification API entirely (measured: `Notification is not defined`). Every real browser
    # has it, so its absence is a glaring anomaly — and a site that calls it throws instead of
    # rendering. Permission prompts are already handled by never granting the permission.
    "--start-maximized",
    "--lang=en-US",
    # The app's own update-restarts (and any hard shutdown) kill Chrome mid-run, so the
    # persistent profile records a crash and every next launch wears the "Restore pages?"
    # bubble — the user watched it sit permanently in the corner, and a "person" whose
    # browser crashes every twenty minutes is its own tell. Belt (this flag hides the
    # bubble) and suspenders (_heal_profile_exit rewrites the crash flag before launch).
    "--hide-crash-restore-bubble",
]
# The launch flags for a window a PERSON is driving (Connect Facebook). No automation-hiding,
# no engine surgery — just the two flags that stop Chrome nagging a first-run profile.
# Crucially this DROPS --disable-features=IsolateOrigins,site-per-process: that switches off
# Chrome's site isolation, and Facebook's two-factor step runs in cross-origin iframes, which is
# exactly the kind of thing that then fails to render. It also drops
# --disable-blink-features=AutomationControlled, which exists only to hide automation and has no
# business in a window where the automation is a human being.
_CLEAN_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
]

ACTION_TIMEOUT = 10_000
SCREENSHOT_TIMEOUT = 15_000

# LAST-RESORT Chrome version for the UA, used only when we cannot read the real build. It was
# hardcoded to 124.0.6367.243 and left there: by Aug 2026 that was ~2 years and 27 majors behind
# the installed browser (151), so every persistent session announced a browser far older than the
# engine running it. Sites feature-gate on this — Facebook served that UA a legacy bundle that
# rendered blank. Both launch paths now detect the REAL version; this constant exists so a
# detection failure degrades to something plausible rather than crashing, and any session using
# it says so in the log.
_DEFAULT_CHROME_FULL = "151.0.0.0"


def _heal_profile_exit(profile_dir) -> None:
    """Mark the persistent profile as having exited cleanly. Chrome writes
    exit_type/exited_cleanly into Preferences; after our update-restarts kill it mid-run it
    records Crashed, and the restore bubble haunts every later session. Best-effort."""
    import json as _json
    from pathlib import Path as _P
    try:
        pref = _P(profile_dir) / "Default" / "Preferences"
        if not pref.exists():
            return
        d = _json.loads(pref.read_text(encoding="utf-8"))
        prof = d.setdefault("profile", {})
        if prof.get("exit_type") != "Normal" or not prof.get("exited_cleanly", True):
            prof["exit_type"] = "Normal"
            prof["exited_cleanly"] = True
            pref.write_text(_json.dumps(d), encoding="utf-8")
            log.info("Healed the profile's crash flag (no more 'Restore pages?' bubble)")
    except Exception as exc:
        log.debug("could not heal profile exit flag: %s", exc)


def _usable(browser) -> bool:
    """Is this freshly-launched browser actually alive? A launch can SUCCEED and then have its
    target die on first real use (measured: bundled Chromium with the sandbox enabled), which no
    static "these flags work" rule can predict. Cheap: open a context, close it."""
    try:
        ctx = browser.new_context()
        ctx.close()
        return True
    except Exception:
        return False


def _installed_chrome_version() -> str:
    """The REAL version of the Chrome we're about to drive, or "" if it can't be read.

    The persistent path launches the user's INSTALLED Chrome but used to advertise the stale
    constant above — 124 from an actual 151 on this machine, ~2 years and 27 majors adrift. A UA
    that disagrees with the engine is both a fingerprint tell and a functional hazard: sites
    feature-gate on it, and Facebook handed that UA a legacy bundle that rendered as a blank page.
    Read once per process; a failure just falls back to the constant."""
    global _INSTALLED_CHROME_CACHE
    if _INSTALLED_CHROME_CACHE is not None:
        return _INSTALLED_CHROME_CACHE
    version = ""
    try:
        import subprocess, os as _os
        for base in (_os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                     _os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                     _os.environ.get("LOCALAPPDATA", "")):
            if not base:
                continue
            exe = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if not exe.exists():
                continue
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-Item '{exe}').VersionInfo.ProductVersion"],
                capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            cand = (out.stdout or "").strip()
            if re.fullmatch(r"\d+(\.\d+){1,3}", cand):
                version = cand
                break
    except Exception as exc:
        log.debug("could not read the installed Chrome version: %s", exc)
    _INSTALLED_CHROME_CACHE = version
    if version:
        log.info("Detected installed Chrome %s (UA will match the real engine)", version)
    return version


_INSTALLED_CHROME_CACHE: str | None = None

# A small pool of real, common desktop viewport sizes. Using a FIXED 1920x1080 every
# session is itself a (weak) signal; rotating across realistic sizes is more human.
_VIEWPORT_POOL = [
    (1920, 1080), (1536, 864), (1600, 900), (1680, 1050), (1440, 900), (1366, 768),
]

# Self-consistent (cores, memory-GB) pairs, used ONLY when we cannot read the real machine.
# Overriding these with a random pair was worse than leaving them alone: measured on this box,
# navigator reported 8 cores / 8 GB while the machine has 32 / 32 — a lie with no upside, and
# one that contradicts every other signal (WebGL happily reports the real RTX card). Real values
# are self-consistent by construction; invented ones are only as good as the pairing table.
_HARDWARE_POOL = [(4, 8), (6, 8), (8, 8), (8, 16), (12, 16), (16, 16)]


def _real_hardware() -> tuple[int, int] | None:
    """(cores, memory-GB) for THIS machine, or None. Preferred over the pool: the truth is
    always self-consistent, and it cannot contradict the GPU/UA/screen we report alongside."""
    try:
        import os as _os
        cores = _os.cpu_count() or 0
        mem_gb = 0
        try:
            import ctypes

            class _MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = _MS()
            st.dwLength = ctypes.sizeof(_MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                mem_gb = int(st.ullTotalPhys / (1024 ** 3))
        except Exception:
            pass
        if cores and mem_gb:
            # navigator.deviceMemory is capped at 8 by the spec and reported in powers of two.
            capped = min(8, 2 ** (mem_gb.bit_length() - 1))
            return cores, capped
    except Exception:
        pass
    return None


# A visible fake cursor injected into the agent's pages so you can SEE where its synthetic
# mouse is. It follows the real mousemove events Playwright dispatches (page.mouse.move), and
# pulses on press. pointer-events:none so it never intercepts. Re-ensures itself on SPA nav.
# Native dialogs are the one thing that can wedge an unattended browser: they are drawn by the
# browser/OS rather than the page, so no amount of automation can dismiss them. window.print() is
# the real-world offender (craigslist puts a 'print' link on every listing). Neutered here rather
# than handled after the fact, because once the dialog is up it is already too late.
# Playwright auto-dismisses alert/confirm/prompt, but we stub them too so a page that BLOCKS on
# one can't stall a sweep waiting for the auto-dismiss.
_NO_NATIVE_DIALOGS_JS = r"""
(() => {
  const noop = () => undefined;
  // EACH patch is independently guarded. A single throw used to take the whole script — and
  // with it whatever the page did next. Facebook's login bundle went to a WHITE SCREEN because
  // 'print' was defined non-writable/non-configurable here and their code assigns to it, which
  // throws a TypeError in strict mode. Stay writable + configurable: the goal is to stop an
  // ACCIDENTAL native dialog, not to win a fight with the page.
  try {
    Object.defineProperty(window, 'print', {value: noop, writable: true, configurable: true});
  } catch (_) { try { window.print = noop; } catch (__) {} }
  try {
    window.alert   = noop;
    window.confirm = () => false;
    window.prompt  = () => null;
  } catch (_) {}
  try {
    // Some sites call print() from a beforeprint/keyboard handler; swallow the event too.
    window.addEventListener('beforeprint', e => { e.preventDefault && e.preventDefault(); }, true);
    // Ctrl/Cmd+P from a stray synthetic keystroke would open the same dialog.
    window.addEventListener('keydown', e => {
      if ((e.ctrlKey || e.metaKey) && (e.key === 'p' || e.key === 'P')) {
        e.preventDefault(); e.stopImmediatePropagation();
      }
    }, true);
  } catch (_) { /* a page that fights this is not worth breaking the sweep over */ }
})();
"""

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
    // Real Chrome exposes chrome.loadTimes and chrome.csi; when we drive the INSTALLED Chrome
    // both are already there and these blocks are no-ops.
    //
    // chrome.runtime is DELIBERATELY NOT FABRICATED. It was, and that was backwards: measured
    // against a bare channel="chrome" launch, real Chrome reports chrome.runtime === undefined
    // on an ordinary page (it appears for extension pages). Adding it made window.chrome's key
    // list read "loadTimes,csi,app,runtime" where a real browser says "loadTimes,csi,app" — so
    // the stealth patch was the thing that stood out. Absent is correct.
    try {
        if (!window.chrome) window.chrome = {};
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
    // Align with the Windows Chrome UA we declared in the context. platform only: we run a real
    // Windows Chrome, so 'Win32' is what it already says and the line is a harmless no-op.
    //
    // hardwareConcurrency and deviceMemory were HARDCODED to 8 here. Measured against a bare
    // channel="chrome" launch on this machine, the real values are 32 and 32 — so the "stealth"
    // was announcing a weaker machine than the one running, while WebGL sat right beside it
    // reporting the genuine RTX card. That contradiction is a stronger signal than either
    // number alone. The browser's own values are self-consistent; leave them.
    try {
        Object.defineProperty(navigator, 'platform', { get: function () { return 'Win32'; }, configurable: true });
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
    // DELIBERATELY NOT OVERRIDDEN. This block used to force screen to 1920x1080 and
    // outerWidth/Height to 1920x1040 regardless of the real window. Measured, that produced an
    // impossible browser: screen 1920x1080 and outer 1920x1040 while innerWidth/Height stayed
    // 1280x720 — 640px of browser chrome that does not exist, in a window bigger than the
    // screen it claims to sit on. Any page doing the arithmetic sees a contradiction; the
    // browser's own numbers already agree with each other.

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
        inject_patches: bool = True,
        # The three concerns `inject_patches` used to conflate. None = follow inject_patches.
        inject_scripts: bool | None = None,
        clean_launch:   bool | None = None,
        spoof_ua:       bool | None = None,
        record_video_dir: str | Path | None = None,
    ) -> None:
        self._headless = headless
        # `inject_patches` USED TO MEAN THREE UNRELATED THINGS AT ONCE — whether to add init
        # scripts, which launch flags to use, and whether to spoof the User-Agent. One boolean
        # silently steering three concerns is how this file drifted: a fix aimed at one of them
        # kept changing the other two by accident. It is now the shorthand that sets three
        # explicit knobs, each of which a caller can also set on its own.
        #
        #   inject_scripts — our init scripts (fingerprint patches, dialog guard, cursor)
        #   clean_launch   — plain Chrome flags instead of the anti-fingerprint set
        #   spoof_ua       — override the User-Agent / Sec-CH-UA headers
        #
        # HANDS-OFF (inject_patches=False) turns all three off. That is for the Connect Facebook
        # window, where a REAL PERSON types their own password into Facebook's own page: a plain
        # profile with a human driving is the most legitimate thing we can present, and every
        # patch is one more thing that can break a heavy login bundle (one did — a
        # non-configurable window.print override sent the login page to a white screen).
        self._inject_scripts = inject_scripts if inject_scripts is not None else inject_patches
        self._clean_launch   = clean_launch   if clean_launch   is not None else not inject_patches
        self._spoof_ua       = spoof_ua       if spoof_ua       is not None else inject_patches
        # (lat, lon) to report via the Geolocation API — so sites that show "your area"
        # from location (OfferUp, store locators) show the WATCH's area, not a default
        # (a fresh automated browser has no location → OfferUp was serving Florida junk).
        self._geolocation = geolocation
        # Record the session to video for LATER REVIEW. Playwright writes one .webm per page,
        # finalised when the context closes. Off by default (it costs disk and a little CPU);
        # switched on for supervised runs on a site where we want a record of exactly what the
        # agent did — Facebook above all, where "what did it actually click?" is the question
        # that matters and a log line is a poor substitute for seeing it.
        self._record_video_dir = Path(record_video_dir) if record_video_dir else None
        # Every page's video, captured AS THE PAGE IS CREATED. Collecting them at close time
        # from context.pages misses any tab that was already closed — which is every deep-read
        # tab, since reading an ad opens a tab and closes it. Those recordings survived on disk
        # under Playwright's opaque page@<hash>.webm name, unrenamed and unpruned.
        self._tracked_videos: list = []
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
        # Prefer the REAL cores/memory. See _real_hardware: an invented pair contradicted the
        # actual machine (8/8 reported on a 32/32 box) while WebGL cheerfully reported the real
        # GPU next to it, so the override created the inconsistency it was meant to prevent.
        self._cores, self._mem_gb = _real_hardware() or random.choice(_HARDWARE_POOL)
        self._chrome_full    = _DEFAULT_CHROME_FULL   # overridden once the real build is known

    def _ua_major(self) -> str:
        """The Chrome major version to advertise. Prefers the version actually detected for this
        session; the module fallback is a last resort and is deliberately NOT a hardcoded old
        build any more — see _DEFAULT_CHROME_FULL."""
        return (self._chrome_full.split(".")[0] or "").strip()

    def _ua_headers(self, major: str) -> dict:
        """UA + client-hint headers that agree with each other AND with the engine.

        Kept in one place because the failure mode is always the same: the UA string and the
        Sec-CH-UA major drifting apart, or both drifting from the browser actually running."""
        return {
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

    def _build_ctx_kwargs(self) -> dict:
        """Context options shared by ephemeral and persistent launch paths. Uses a UA
        matched to the ACTUAL bundled Chromium version (set in _enter_ephemeral) and a
        per-session randomized viewport so the fingerprint isn't a fixed constant."""
        ctx_kwargs: dict = {"locale": "en-US"}
        # UA spoofing is OPT-IN. When a person is driving we send nothing: that window runs the
        # user's REAL Chrome, which already reports a correct, current User-Agent — overriding it
        # could only ever make it wrong, and it did. The persistent path never refreshed
        # _chrome_full, so we announced Chrome 124 from an actual Chrome 151 (measured on this
        # machine) and Facebook handed that stale UA a legacy bundle.
        if self._spoof_ua:
            major = self._ua_major()
            ctx_kwargs.update(self._ua_headers(major))
        if self._record_video_dir:
            self._record_video_dir.mkdir(parents=True, exist_ok=True)
            ctx_kwargs["record_video_dir"] = str(self._record_video_dir)
            # A fixed recording size keeps the file sane and the playback steady; without it
            # Playwright follows the (maximised, variable) window.
            ctx_kwargs["record_video_size"] = {"width": 1280, "height": 720}
        # Viewport, geolocation and permissions below apply in EVERY mode. They used to be
        # skipped along with the UA on the hands-off path, which silently dropped the watch's
        # location — the exact defect that had OfferUp serving Florida results.
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
        """Per-session navigator overrides — now deliberately EMPTY, and that is the fix.

        This block used to rewrite hardwareConcurrency, deviceMemory and screen.width/height.
        Measured against a bare channel="chrome" launch, every one of those overrides made the
        browser LESS coherent, not more:

          • screen was forced to the session viewport, producing an impossible browser —
            screen 1920x1080 with innerWidth/Height 1280x720 and outerWidth/Height 1920x1040,
            i.e. a window claiming more chrome than exists inside a screen it cannot fit.
          • cores/memory were drawn from a pool and reported 8/8 on a machine with 32/32,
            while WebGL sat alongside happily reporting the real RTX card.

        We drive the user's REAL Chrome, which already reports real, self-consistent values. An
        override can only introduce a contradiction — including a well-meant one: capping
        deviceMemory to the spec's 8 would itself disagree with the 32 this browser actually
        reports. The honest number is the one the browser already has.

        Kept as a method (returning nothing to inject) so the call site and tests stay stable.
        """
        return ""

    def __enter__(self) -> "BrowserSession":
        self._pw = sync_playwright().start()
        if self._persistent:
            self._enter_persistent()
        else:
            self._enter_ephemeral()
        # Inject our supplemental stealth patches on every new page before site JS runs,
        # then this session's randomized fingerprint values (cores/memory/screen) on top.
        assert self._context is not None
        if not self._inject_scripts:
            log.info("Browser: no init scripts injected for this session (a person is driving)")
            return self
        self._context.add_init_script(_EXTRA_STEALTH_JS)
        _fp = self._session_fingerprint_js()
        if _fp.strip():
            self._context.add_init_script(_fp)
        # Never let a page open a NATIVE dialog. window.print() draws an OS-level print dialog
        # that Playwright cannot see or dismiss, so the browser hangs until a human clicks it —
        # observed live, a sweep froze for minutes after the agent clicked craigslist's 'print'.
        # beforeprint/alert/confirm/prompt are the same hazard. The label guard in fb_safety
        # stops the agent CHOOSING these; this stops the page from doing it regardless.
        self._context.add_init_script(_NO_NATIVE_DIALOGS_JS)
        # Visible fake cursor (opt-in) so you can watch where the agent's mouse goes.
        if self._show_cursor:
            self._context.add_init_script(_CURSOR_JS)
        return self

    @property
    def context(self):
        """The live BrowserContext (or None). Lets a caller reuse the persistent context's own
        default page instead of opening a second tab beside it."""
        return self._context

    def _launch_args(self) -> list[str]:
        """Chrome flags for this session. Hands-off mode gets a VANILLA set: the stealth args
        exist to make automation look human, which is pointless when a human is driving, and
        actively harmful here — disabling site isolation broke Facebook's two-factor step."""
        return list(_CLEAN_ARGS if self._clean_launch else _STEALTH_ARGS)

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
        # Match the UA to the Chrome we're actually launching. Without this the persistent path
        # kept advertising the stale module constant while driving a far newer browser.
        real = _installed_chrome_version()
        if real:
            self._chrome_full = real
        ctx_kwargs = self._build_ctx_kwargs()
        last_exc: Exception | None = None
        # Ask for the REAL Chrome sandbox first. Playwright defaults chromium_sandbox to False,
        # which passes --no-sandbox: Chrome then warns "You are using an unsupported
        # command-line flag" and, far more importantly, runs the renderer UNSANDBOXED —
        # Facebook's two-factor step will not render in that state (the login page survives it,
        # 2FA does not). It is a plain automation tell too. Sandbox=False stays as a fallback so
        # a machine that genuinely cannot sandbox still gets a browser instead of an error.
        # Same ordering as the ephemeral path: a REAL Chrome with its sandbox on is the first
        # choice, and every combination is verified before it is accepted (a persistent context
        # can come back "launched" and then be dead). Kept identical on purpose — the two paths
        # drifting apart is what produced the --no-sandbox split in the first place.
        channels = ("chrome", "msedge", None)
        attempts = [(ch, True) for ch in channels] + [(ch, False) for ch in channels]
        for channel, sandbox in attempts:
            ctx = None
            try:
                _heal_profile_exit(self._profile_dir)
                ctx = self._pw.chromium.launch_persistent_context(
                    user_data_dir=str(self._profile_dir),
                    headless=self._headless,
                    args=self._launch_args(),
                    # Playwright adds --enable-automation, which paints Chrome's "controlled by
                    # automated test software" infobar. Dropping the DEFAULT arg is the clean
                    # way to be rid of it — unlike a bad-flags switch, this leaves no banner of
                    # its own behind.
                    ignore_default_args=["--enable-automation"],
                    chromium_sandbox=sandbox,
                    **({"channel": channel} if channel else {}),
                    **ctx_kwargs,
                )
                # A persistent context IS the context — prove it by touching its pages.
                _ = ctx.pages
                self._context = ctx
                self._watch_videos()
                log.info(
                    "Persistent browser launched (profile=%s) via channel=%r, sandbox=%s",
                    self._profile_dir.name, channel or "bundled", sandbox,
                )
                if not sandbox:
                    log.warning("Chrome sandbox unavailable — running with --no-sandbox. "
                                "Some protected pages (Facebook's 2FA) may not render.")
                return
            except Exception as exc:
                last_exc = exc
                log.debug("Persistent launch channel=%r sandbox=%s failed: %s",
                          channel, sandbox, exc)
                if ctx is not None:
                    try:
                        ctx.close()
                    except Exception:
                        pass
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
        # Ask for the REAL Chrome sandbox first, exactly as the persistent path does. This used
        # to be set on the persistent path ONLY, so every ordinary sweep still ran with
        # --no-sandbox: an unsandboxed renderer, a visible "unsupported command-line flag"
        # banner, and a plain automation tell. Two launch paths that disagree is how this file
        # drifted; they now make the same request with the same fallback.
        # Try (channel, sandbox) combinations, PREFERRING a real installed Chrome with its
        # sandbox on, and VERIFY each one before accepting it. Measured on this machine:
        #   channel="chrome" + sandbox=True  -> works
        #   bundled Chromium + sandbox=True  -> launches, then the target dies on first use
        # A launch that "succeeds" and then dies is why a static rule was wrong here; the only
        # reliable test is to use the browser. _usable() does that cheaply.
        channels = ("chrome", "msedge", None) if not self._headless else ("chrome", None)
        attempts = [(ch, True) for ch in channels] + [(ch, False) for ch in channels]
        last_exc: Exception | None = None
        for channel, sandbox in attempts:
            browser = None
            try:
                browser = self._pw.chromium.launch(
                    headless=self._headless,
                    args=self._launch_args(),
                    ignore_default_args=["--enable-automation"],   # see the persistent path
                    chromium_sandbox=sandbox,
                    **({"channel": channel} if channel else {}),
                )
                if not _usable(browser):
                    raise RuntimeError("browser closed immediately after launch")
                self._browser = browser
                log.info("Browser launched (%s) via channel=%r, sandbox=%s",
                         "headless" if self._headless else "visible",
                         channel or "bundled", sandbox)
                if not sandbox:
                    log.info("Running unsandboxed (this build could not sandbox).")
                break
            except Exception as exc:
                last_exc = exc
                log.debug("Browser launch channel=%r sandbox=%s failed: %s",
                          channel, sandbox, exc)
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
        if self._browser is None:
            raise RuntimeError(f"Could not launch any browser: {last_exc}")

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
        self._watch_videos()


    def _watch_videos(self) -> None:
        """Track every page's video from the moment it opens, so a tab that is closed before
        the session ends still gets a human-readable name (and still counts for pruning)."""
        if not self._record_video_dir or not self._context:
            return
        def _track(pg):
            try:
                v = getattr(pg, "video", None)
                if v is not None:
                    self._tracked_videos.append(v)
            except Exception:
                pass
        try:
            self._context.on("page", _track)
            for pg in self._context.pages:      # the context's own initial page
                _track(pg)
        except Exception as exc:
            log.debug("could not subscribe to page videos: %s", exc)

    _MAX_RECORDINGS = 20                       # newest N .webm per watch folder
    _MAX_RECORD_BYTES = 400 * 1024 * 1024      # ...AND at most ~400MB per folder. A count cap
    # alone is no cap at all when each file's size is unbounded — one long supervised FB session
    # produced a 47MB video; twenty of those in each of a few watch folders is gigabytes. Same
    # dual-bound philosophy as thumbs.py (1000 files / 200MB): count for tidiness, bytes for disk.

    def _finalize_recordings(self, videos) -> None:
        """Rename this session's videos to a sortable, human name and prune old ones.

        Playwright names each file by an internal hash; after the context closes we move it to
        <recordings>/<YYYYmmdd-HHMMSS>-<n>.webm so a run is findable by when it happened. Then we
        keep only the newest _MAX_RECORDINGS in the folder — a supervised debugging aid should not
        silently fill the disk over weeks of sweeps. Best-effort throughout; a rename that fails
        just leaves the original file in place."""
        import time as _t
        d = self._record_video_dir
        try:
            stamp = _t.strftime("%Y%m%d-%H%M%S")
            for i, v in enumerate(videos or []):
                try:
                    src = Path(v.path())
                except Exception:
                    continue
                if not src.exists():
                    continue
                dst = d / (f"{stamp}.webm" if i == 0 else f"{stamp}-{i+1}.webm")
                try:
                    src.replace(dst)
                    log.info("Saved session recording: %s (%d KB)", dst.name, dst.stat().st_size // 1024)
                except Exception as exc:
                    log.debug("could not rename recording %s: %s", src.name, exc)
            # Prune: newest _MAX_RECORDINGS survive, and the survivors must fit the byte
            # budget — oldest evicted first until they do.
            webms = sorted(d.glob("*.webm"), key=lambda f: f.stat().st_mtime, reverse=True)
            for old in webms[self._MAX_RECORDINGS:]:
                try:
                    old.unlink()
                except OSError:
                    pass
            kept = webms[:self._MAX_RECORDINGS]
            total = sum(f.stat().st_size for f in kept if f.exists())
            while total > self._MAX_RECORD_BYTES and kept:
                victim = kept.pop()            # list is newest-first; pop() takes the oldest
                try:
                    total -= victim.stat().st_size
                    victim.unlink()
                    log.info("Pruned old recording %s (byte budget)", victim.name)
                except OSError:
                    pass
        except Exception as exc:
            log.debug("recording finalisation failed: %s", exc)

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
            # Playwright finalises the .webm on context close, so grab each page's video path
            # BEFORE closing, then rename the opaque hashes to a timestamped, watch-legible name
            # once the files exist. This is what makes "the 09:05 Facebook run" findable later.
            _video_paths = list(self._tracked_videos)
            if self._record_video_dir:
                try:
                    for pg in self._context.pages:      # belt and braces
                        v = getattr(pg, "video", None)
                        if v is not None and v not in _video_paths:
                            _video_paths.append(v)
                except Exception as exc:
                    log.debug("could not enumerate videos: %s", exc)
            try:
                self._context.close()
            except Exception as exc:
                log.debug("Context close failed: %s", exc)
            if self._record_video_dir:
                self._finalize_recordings(_video_paths)
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

    def wait_until_closed(self, poll_seconds: float = 1.0,
                          timeout: float | None = None) -> bool:
        """
        Block until the user closes the browser window (all pages gone) or timeout.

        Used by manual flows like the Facebook login: open a visible window, let the
        user sign in, and return once they close it (the persistent profile keeps the
        session on disk). Safe to call inside the context-manager `with` block.

        Returns True if the window is actually CLOSED, False if we merely timed out — so a
        caller can poll in short slices (sampling the page in between) instead of blocking for
        the whole session with no visibility into what it is doing.
        """
        import time as _t
        start = _t.monotonic()
        while True:
            try:
                if self._context is None or not self._context.pages:
                    return True
            except Exception:
                return True  # context/browser already torn down by the user closing it
            if timeout is not None and (_t.monotonic() - start) > timeout:
                return False
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


def clear_site_local_storage(host_substring: str, state_path=None) -> dict:
    """Drop a site's saved localStorage from the browser-state snapshot, keeping its cookies.

    Craigslist remembers hidden postings in localStorage, and that snapshot is reloaded into
    every sweep — so a single stray click on "hide posting" would quietly remove that listing
    from everything the watch ever sees again. The agent can no longer make that click, but a
    snapshot taken before the guard existed still carries the damage.

    Cookies are deliberately kept: they're what makes us look like a returning visitor rather
    than a fresh one on every sweep. Returns {"cleared": n, "origins": [...]}. Never raises.
    """
    import json as _json
    path = Path(state_path) if state_path else _BROWSER_STATE
    out = {"cleared": 0, "origins": []}
    try:
        if not path.exists():
            return out
        data = _json.loads(path.read_text(encoding="utf-8"))
        origins = data.get("origins") or []
        kept = []
        for o in origins:
            origin = str(o.get("origin") or "")
            if host_substring.lower() in origin.lower() and (o.get("localStorage") or []):
                out["cleared"] += len(o.get("localStorage") or [])
                out["origins"].append(origin)
                continue                      # drop this origin's localStorage entirely
            kept.append(o)
        if out["cleared"]:
            data["origins"] = kept
            path.write_text(_json.dumps(data), encoding="utf-8")
            log.info("Cleared %d saved localStorage item(s) for %r from the browser state",
                     out["cleared"], host_substring)
    except Exception as exc:
        log.warning("could not clear saved local storage for %r: %s", host_substring, exc)
    return out
