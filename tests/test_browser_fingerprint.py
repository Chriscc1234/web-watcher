"""
Fingerprint-hardening tests for BrowserSession. These exercise the pure context/JS
assembly (no real browser launch — __init__ and _build_ctx_kwargs don't start Playwright).
"""

from __future__ import annotations

from web_watcher.browser import (
    BrowserSession, _VIEWPORT_POOL, _HARDWARE_POOL, maybe_warm_homepage,
)


def test_session_picks_coherent_fingerprint():
    s = BrowserSession(headless=True)
    assert s._viewport in _VIEWPORT_POOL
    assert (s._cores, s._mem_gb) in _HARDWARE_POOL


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


def test_session_fingerprint_js_embeds_chosen_values():
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
