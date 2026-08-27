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
