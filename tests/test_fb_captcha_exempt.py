"""Facebook is exempt from CAPTCHA solving — always.

fb_safety's design is STOP-DON'T-SOLVE, but that was enforced only by is_checkpoint(), which
needs a recognisable checkpoint URL or phrase. A PLAIN CAPTCHA matching none of those cues
reached the solver. On Facebook we are LOGGED IN, so a defeated challenge is attributed to the
user's real account — and reliably beating a human check is the clearest automation signal there
is. Everywhere else the solver is unchanged.
"""
from __future__ import annotations
from web_watcher import agent as A
from web_watcher import fb_safety


class _Page:
    def __init__(self, url): self.url = url
    def evaluate(self, *a, **k): return []
    def inner_text(self, *a, **k): return ""
    @property
    def frames(self): return []


def test_the_solver_refuses_on_facebook_even_if_called_directly(monkeypatch):
    # Defence in depth: a future caller added without the gate must still not defeat a check.
    called = []
    monkeypatch.setattr(A, "_solve_press_hold", lambda p: called.append("press") or True)
    monkeypatch.setattr(A, "_click_recaptcha_checkbox", lambda p: called.append("box") or True)
    monkeypatch.setattr(A, "_emit_focus_events", lambda p: None)
    for url in ("https://www.facebook.com/marketplace/seattle/search?query=boat",
                "https://facebook.com/checkpoint/",
                "https://m.facebook.com/marketplace/"):
        assert A._solve_captcha(_Page(url)) is False
    assert called == []          # not one solving step was attempted


def test_the_solver_still_runs_on_other_sites(monkeypatch):
    monkeypatch.setattr(A, "_emit_focus_events", lambda p: None)
    monkeypatch.setattr(A, "_solve_press_hold", lambda p: True)
    monkeypatch.setattr(A, "_detect_captcha", lambda p: False)   # cleared after the hold
    assert A._solve_captcha(_Page("https://www.boattrader.com/boats-for-sale/")) is True


def test_facebook_is_recognised_across_its_hosts():
    for url in ("https://www.facebook.com/marketplace", "https://m.facebook.com/x",
                "https://web.facebook.com/y", "https://facebook.com/z"):
        assert fb_safety.is_facebook(url) is True
    assert fb_safety.is_facebook("https://www.craigslist.org/search/boo") is False


def test_a_captcha_on_facebook_engages_the_global_halt(tmp_path):
    # The halt is the emergency brake: it stops ALL Facebook activity app-wide and only a human
    # clears it. A CAPTCHA must trip it, not just pause one watch.
    assert fb_safety.is_halted(tmp_path) is False
    fb_safety.engage_halt("Facebook showed a CAPTCHA / human check", "TestWatch", tmp_path)
    assert fb_safety.is_halted(tmp_path) is True
    state = fb_safety.halt_state(tmp_path)
    assert "CAPTCHA" in state["reason"]
    assert fb_safety.clear_halt(tmp_path) is True        # ...and only a human clears it
    assert fb_safety.is_halted(tmp_path) is False


def test_the_halt_survives_a_restart(tmp_path):
    # Persisted to disk on purpose: an app restart must not silently resume a flagged account.
    fb_safety.engage_halt("checkpoint", "W", tmp_path)
    assert fb_safety.halt_state(tmp_path) is not None     # re-read from disk, no memory involved


# ── the Connect Facebook window is HANDS OFF ──────────────────────────────────────
# A person types their own password into Facebook's own page there. Everything we inject is
# useless to them and can break the page: a non-configurable window.print override from the
# dialog guard threw inside Facebook's login bundle and left a WHITE SCREEN.

def test_dialog_guard_never_makes_print_non_configurable():
    from web_watcher.browser import _NO_NATIVE_DIALOGS_JS as js
    assert "configurable: true" in js and "writable: true" in js
    assert "configurable: false" not in js
    # every patch independently guarded, so one throw can't take the whole script
    assert js.count("try {") >= 3


def test_browser_session_supports_hands_off_mode():
    import inspect as _i
    from web_watcher.browser import BrowserSession
    sig = _i.signature(BrowserSession.__init__)
    assert "inject_patches" in sig.parameters
    assert sig.parameters["inject_patches"].default is True     # normal sweeps unchanged


def test_connect_facebook_opens_a_clean_window():
    # The flow that hands the browser to a human must ask for no injections at all.
    import inspect as _i
    from web_watcher.services import ServiceManager
    src = _i.getsource(ServiceManager.connect_facebook)
    assert "inject_patches=False" in src


def test_hands_off_mode_uses_vanilla_launch_flags():
    """The Connect window gets plain Chrome flags — no automation-hiding, no engine surgery.

    --disable-features=IsolateOrigins,site-per-process switches OFF Chrome's site isolation, and
    Facebook's two-factor step runs in cross-origin iframes: it rendered as a blank page with a
    lone Meta logo. --disable-blink-features=AutomationControlled exists only to hide automation,
    which is meaningless when the automation is a person."""
    from web_watcher.browser import BrowserSession
    hands_off = BrowserSession(headless=False, persistent=True, inject_patches=False)._launch_args()
    assert not any("IsolateOrigins" in a for a in hands_off)
    assert not any("AutomationControlled" in a for a in hands_off)
    assert "--no-first-run" in hands_off            # harmless first-run nags still suppressed
    # Normal sweeps are UNCHANGED — this is a carve-out, not a policy change.
    normal = BrowserSession(headless=False, persistent=True)._launch_args()
    assert any("IsolateOrigins" in a for a in normal)
    assert any("AutomationControlled" in a for a in normal)


# ── the User-Agent must not lie about the engine ──────────────────────────────────
# The persistent path launches the user's INSTALLED Chrome but advertised a hardcoded
# constant: Chrome 124 from an actual Chrome 151 — ~2 years and 27 majors adrift. Facebook
# handed that stale UA a legacy bundle and the two-factor page rendered blank.

def test_hands_off_sends_no_user_agent_override():
    from web_watcher.browser import BrowserSession
    kw = BrowserSession(headless=False, persistent=True,
                        inject_patches=False)._build_ctx_kwargs()
    assert "user_agent" not in kw              # the real Chrome speaks for itself
    assert "extra_http_headers" not in kw      # ...including its own Sec-CH-UA


def test_normal_sessions_still_get_a_consistent_ua():
    from web_watcher.browser import BrowserSession
    kw = BrowserSession(headless=False, persistent=True)._build_ctx_kwargs()
    assert "Chrome/" in kw["user_agent"]
    major = kw["user_agent"].split("Chrome/")[1].split(".")[0]
    # The UA major and the Sec-CH-UA major must agree — a mismatch is its own tell.
    assert f'"Google Chrome";v="{major}"' in kw["extra_http_headers"]["Sec-CH-UA"]


def test_installed_chrome_version_is_read_or_falls_back_safely():
    from web_watcher.browser import _installed_chrome_version
    import re as _re
    v = _installed_chrome_version()
    assert v == "" or _re.fullmatch(r"\d+(\.\d+){1,3}", v)   # never garbage


def test_persistent_launch_asks_for_the_real_chrome_sandbox():
    """Playwright defaults chromium_sandbox to False, which passes --no-sandbox: Chrome warns
    about an unsupported flag and runs the renderer UNSANDBOXED. Facebook's two-factor step
    will not render in that state (the login page survives it; 2FA does not), and it is a plain
    automation tell. Sandbox=False remains only as a fallback."""
    import inspect as _i
    from web_watcher.browser import BrowserSession
    for fn in (BrowserSession._enter_persistent, BrowserSession._enter_ephemeral):
        src = _i.getsource(fn)
        assert "chromium_sandbox=sandbox" in src, fn.__name__
        # Sandboxed attempts are ordered BEFORE unsandboxed ones, on BOTH paths. The two used
        # to disagree — only the persistent path asked for the sandbox, so every ordinary sweep
        # still ran with --no-sandbox.
        assert "[(ch, True) for ch in channels] + [(ch, False) for ch in channels]" in src, fn.__name__


# ── the three concerns that used to be one boolean ────────────────────────────────
# `inject_patches` silently steered init scripts, launch flags AND UA spoofing at once, so a fix
# aimed at one kept changing the other two by accident. They are separable now.

def test_the_three_session_concerns_are_independent():
    from web_watcher.browser import BrowserSession
    s = BrowserSession(headless=False, persistent=True,
                       inject_scripts=True, clean_launch=True, spoof_ua=False)
    assert s._inject_scripts is True and s._clean_launch is True and s._spoof_ua is False
    assert "--no-first-run" in s._launch_args()
    assert not any("AutomationControlled" in a for a in s._launch_args())
    assert "user_agent" not in s._build_ctx_kwargs()


def test_inject_patches_still_works_as_the_shorthand():
    from web_watcher.browser import BrowserSession
    off = BrowserSession(headless=False, persistent=True, inject_patches=False)
    assert (off._inject_scripts, off._clean_launch, off._spoof_ua) == (False, True, False)
    on = BrowserSession(headless=False, persistent=True)
    assert (on._inject_scripts, on._clean_launch, on._spoof_ua) == (True, False, True)


def test_geolocation_survives_every_mode():
    """It used to be dropped along with the UA on the hands-off path — silently un-setting the
    watch's location, which is the exact defect that had OfferUp serving Florida results."""
    from web_watcher.browser import BrowserSession
    for kw in ({}, {"inject_patches": False}, {"spoof_ua": False}):
        k = BrowserSession(headless=True, geolocation=(48.5, -122.6), **kw)._build_ctx_kwargs()
        assert k["geolocation"] == {"latitude": 48.5, "longitude": -122.6}, kw
        assert "geolocation" in k.get("permissions", []), kw


def test_ua_and_client_hint_majors_never_disagree():
    from web_watcher.browser import BrowserSession
    s = BrowserSession(headless=True)
    s._chrome_full = "151.0.7922.172"
    k = s._build_ctx_kwargs()
    assert "Chrome/151.0.7922.172" in k["user_agent"]
    assert '"Google Chrome";v="151"' in k["extra_http_headers"]["Sec-CH-UA"]


def test_the_last_resort_ua_is_not_years_stale():
    # It sat at 124 while the machine ran 151. A fallback that lies by 27 majors is worse than
    # no fallback: sites feature-gate on it.
    from web_watcher.browser import _DEFAULT_CHROME_FULL
    assert int(_DEFAULT_CHROME_FULL.split(".")[0]) >= 150
