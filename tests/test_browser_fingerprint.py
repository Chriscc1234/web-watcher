"""
Fingerprint-hardening tests for BrowserSession. These exercise the pure context/JS
assembly (no real browser launch — __init__ and _build_ctx_kwargs don't start Playwright).
"""

from __future__ import annotations

from web_watcher.browser import (
    BrowserSession, _VIEWPORT_POOL, _HARDWARE_POOL, maybe_warm_homepage,
)


def test_session_reports_the_real_machine_not_a_pooled_lie():
    """Cores/memory come from the REAL machine where it can be read.

    The pool was a downgrade in every sense: measured on this box it reported 8 cores / 8 GB on
    a 32/32 machine, while WebGL sat alongside reporting the genuine RTX card. An invented pair
    is only as coherent as the table it came from; the truth is coherent by construction."""
    from web_watcher.browser import _real_hardware
    s = BrowserSession(headless=True)
    assert s._viewport in _VIEWPORT_POOL
    real = _real_hardware()
    if real:
        assert (s._cores, s._mem_gb) == real
    else:
        assert (s._cores, s._mem_gb) in _HARDWARE_POOL      # fallback only when unreadable


def test_ua_tracks_chrome_full_version():
    s = BrowserSession(headless=True)
    s._chrome_full = "131.0.6778.86"
    kw = s._build_ctx_kwargs()
    assert "Chrome/131.0.6778.86" in kw["user_agent"]
    # Client-hint major version must agree with the UA major (no UA/engine mismatch).
    assert 'v="131"' in kw["extra_http_headers"]["Sec-CH-UA"]
    assert kw["extra_http_headers"]["Sec-CH-UA-Platform"] == '"Windows"'


def test_headless_uses_pooled_viewport():
    s = BrowserSession(headless=True)
    kw = s._build_ctx_kwargs()
    vp = (kw["viewport"]["width"], kw["viewport"]["height"])
    assert vp in _VIEWPORT_POOL


def test_visible_uses_no_viewport():
    s = BrowserSession(headless=False)
    kw = s._build_ctx_kwargs()
    assert kw.get("no_viewport") is True
    assert "viewport" not in kw


def test_no_fingerprint_overrides_are_injected():
    """The per-session override block is intentionally empty now.

    It used to force screen.width/height to the session viewport, which produced an impossible
    browser: screen 1920x1080 with inner 1280x720 and outer 1920x1040 — a window claiming more
    chrome than exists, inside a screen it cannot fit. We drive the user's real Chrome; it
    already reports self-consistent values, so any override can only add a contradiction."""
    s = BrowserSession(headless=True)
    js = s._session_fingerprint_js()
    assert js.strip() == ""
    assert "screen" not in js


def _obsolete_test_session_fingerprint_js_embeds_chosen_values():
    s = BrowserSession(headless=True)
    js = s._session_fingerprint_js()
    assert f"return {s._cores};" in js
    assert f"return {s._mem_gb};" in js
    assert "hardwareConcurrency" in js and "deviceMemory" in js


def test_warm_homepage_prob_zero_is_noop():
    calls = []

    class FakePage:
        def goto(self, *a, **k): calls.append(a)
        def wait_for_timeout(self, *a, **k): pass

    maybe_warm_homepage(FakePage(), "https://x.com/search?q=truck", prob=0.0)
    assert calls == []


# ── the stealth layer must not CREATE anomalies ───────────────────────────────────
# Audited against a bare channel="chrome" launch. Every item below was a case of the
# "stealth" patch making the browser stand out MORE than doing nothing would have.

def test_no_hardcoded_hardware_lies_in_the_stealth_script():
    """hardwareConcurrency/deviceMemory were pinned to 8 on a machine with 32/32, while WebGL
    reported the genuine RTX card alongside. The contradiction is a stronger signal than either
    number; the browser's own values are self-consistent."""
    from web_watcher.browser import _EXTRA_STEALTH_JS as js
    # Look for the OVERRIDE, not the word — the surrounding comment explains why it is gone.
    assert "defineProperty(navigator, 'hardwareConcurrency'" not in js
    assert "defineProperty(navigator, 'deviceMemory'" not in js


def test_screen_dimensions_are_never_forced():
    """Forcing screen to 1920x1080 and outer to 1920x1040 while inner stayed 1280x720 described
    an impossible browser — 640px of chrome that does not exist, in a window larger than its
    own screen."""
    from web_watcher.browser import _EXTRA_STEALTH_JS as js
    assert "defineProperty(window, 'outerWidth'" not in js
    assert "defineProperty(window, 'outerHeight'" not in js
    assert "defineProperty(screen, k" not in js


def test_chrome_runtime_is_not_fabricated():
    """Real Chrome reports chrome.runtime === undefined on an ordinary page; adding it made
    window.chrome's key list read 'loadTimes,csi,app,runtime' where a real browser says
    'loadTimes,csi,app'. The patch was the tell."""
    from web_watcher.browser import _EXTRA_STEALTH_JS as js
    assert "chrome.runtime = {" not in js


def test_notification_api_is_never_removed():
    """--disable-notifications does not suppress prompts, it DELETES the API. Measured:
    `Notification is not defined`. No real browser lacks it, and a site that calls it throws
    instead of rendering — a functional break, not just a fingerprint issue."""
    from web_watcher.browser import _STEALTH_ARGS
    assert not any("disable-notifications" in a for a in _STEALTH_ARGS)


# ── session video recording (supervised-run review aid) ──────────────────────────

def test_recording_is_off_by_default_and_opt_in():
    from web_watcher.browser import BrowserSession
    assert BrowserSession(headless=True)._record_video_dir is None
    s = BrowserSession(headless=True, record_video_dir="/tmp/x")
    assert s._record_video_dir is not None


def test_record_video_is_a_watch_field_defaulting_off():
    from web_watcher.config import Watch
    w = Watch(name="w", urls=["https://x"], instruction="x", interval_minutes=30)
    assert w.record_video is False
    w2 = Watch(name="w", urls=["https://x"], instruction="x", interval_minutes=30,
               record_video=True)
    assert w2.record_video is True


def test_recordings_are_capped():
    from web_watcher.browser import BrowserSession
    assert BrowserSession._MAX_RECORDINGS <= 50   # a debug aid must not fill the disk
