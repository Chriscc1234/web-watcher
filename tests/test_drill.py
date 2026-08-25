"""The site drill — prove we can USE a site before trusting a watch to it.

The two checks that matter most are the ones that catch SILENT failure, so they're the ones
tested hardest: vision must be cross-examined against the DOM (a model describing Facebook from
memory has to fail), and a navigation click must be verified to have changed the page (a click
that did nothing must not read as success). Facebook safety is absolute — a checkpoint aborts and
halts, and a blocked control is never clicked. No browser or model here; both are faked.
See web_watcher/drill.py."""

from __future__ import annotations

import json

import pytest

from web_watcher import drill as D


# ── fakes ────────────────────────────────────────────────────────────────────────

class FakePage:
    def __init__(self, text="", url="https://www.facebook.com/", shot=b"png",
                 link_text="Marketplace"):
        self.text, self.url, self._shot = text, url, shot
        self.link_text = link_text          # the visible label of whatever link is found
        self.clicked = []

    def inner_text(self, sel, timeout=0):
        return self.text

    def screenshot(self, **kw):
        return self._shot

    def wait_for_load_state(self, *a, **k):
        pass

    def locator(self, sel):
        return FakeLoc(self, sel, text=self.link_text)


class FakeLoc:
    def __init__(self, page, sel, present=True, text="Marketplace"):
        self.page, self.sel, self.present, self.text = page, sel, present, text

    def count(self):
        return 1 if self.present else 0

    def first_or_self(self):
        return self

    @property
    def first(self):
        return self

    def inner_text(self, timeout=0):
        return self.text

    def is_visible(self):
        return self.present

    def scroll_into_view_if_needed(self, timeout=0):
        pass

    def bounding_box(self):
        return None                      # forces _human_click's plain-click fallback

    def click(self, timeout=0):
        self.page.clicked.append(self.text)


def _cfg():
    from web_watcher.config import AppConfig
    return AppConfig(watches=[])


# ── the drill spec ───────────────────────────────────────────────────────────────

def test_facebook_drill_is_read_only_and_expects_a_login():
    spec = D.drill_for("https://www.facebook.com/marketplace")
    assert spec["section"] == "Marketplace"
    assert spec["expect_login"] is True and spec["read_only"] is True


def test_unknown_site_gets_a_generic_drill():
    assert D.drill_for("https://example.com") == {}


# ── vision, cross-examined ───────────────────────────────────────────────────────

def _vision(monkeypatch, payload):
    from web_watcher import llm
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps(payload))


def test_vision_passes_when_what_it_reads_is_really_on_the_page(monkeypatch):
    page = FakePage(text="Marketplace  Today's picks  1998 Toyota Tacoma  $8,500  Anacortes, WA")
    _vision(monkeypatch, {"page_kind": "marketplace feed", "logged_in": True,
                          "visible_text": ["Today's picks", "1998 Toyota Tacoma", "$8,500"]})
    res = D._step_see(page, _cfg())
    assert res["ok"] is True and "3 confirmed" in res["detail"]


def test_vision_fails_when_it_describes_a_page_it_is_not_looking_at(monkeypatch):
    """The silent poisoner: a plausible Facebook description that matches nothing on screen."""
    page = FakePage(text="Log in to Facebook   Email or phone number   Password")
    _vision(monkeypatch, {"page_kind": "marketplace feed", "logged_in": True,
                          "visible_text": ["Today's picks", "Vehicles", "Free stuff", "Sell"]})
    res = D._step_see(page, _cfg())
    assert res["ok"] is False
    assert "UNCONFIRMED" in res["detail"]


def test_vision_fails_when_it_names_nothing_readable(monkeypatch):
    _vision(monkeypatch, {"page_kind": "a website", "visible_text": []})
    assert D._step_see(FakePage(text="anything"), _cfg())["ok"] is False


def test_vision_ignores_fragments_too_short_to_mean_anything(monkeypatch):
    page = FakePage(text="Marketplace listings here")
    _vision(monkeypatch, {"visible_text": ["a", "of", "Marketplace"]})
    res = D._step_see(page, _cfg())
    assert res["ok"] is True                     # scored on "Marketplace" alone, not luck matches


# ── navigation by click, verified ────────────────────────────────────────────────

def test_navigate_fails_when_the_click_changes_nothing(monkeypatch):
    page = FakePage(text="same page", url="https://www.facebook.com/")
    res = D._step_navigate(page, {"section": "Marketplace", "section_hints": ["Marketplace"]})
    assert res["ok"] is False and res["fatal"] is True
    assert "nothing changed" in res["detail"]


def test_navigate_passes_when_the_page_actually_moves(monkeypatch):
    page = FakePage(text="home feed", url="https://www.facebook.com/")

    def click(self, timeout=0):
        page.text = "Marketplace  Today's picks"
        page.url = "https://www.facebook.com/marketplace/"
    monkeypatch.setattr(FakeLoc, "click", click)

    res = D._step_navigate(page, {"section": "Marketplace", "section_hints": ["Marketplace"]})
    assert res["ok"] is True and res["section_confirmed"] is True


def test_navigate_fails_clearly_when_the_section_link_is_missing():
    page = FakePage(text="home")
    monkeypatch_absent = FakeLoc(page, "x", present=False)
    page.locator = lambda sel: monkeypatch_absent
    res = D._step_navigate(page, {"section": "Marketplace", "section_hints": ["Marketplace"]})
    assert res["ok"] is False and "could not find a link" in res["detail"]


def test_navigation_never_clicks_a_blocked_facebook_control():
    """The drill browses; it must never message, offer, buy, or post."""
    page = FakePage(url="https://www.facebook.com/marketplace/item/1", link_text="Message seller")
    loc, _ = D._find_section_link(page, ["Message seller"])
    assert loc is None                            # refused, not clicked

    # …but an ordinary navigation label that merely CONTAINS a keyword is still fine.
    page2 = FakePage(url="https://www.facebook.com/marketplace/", link_text="See more")
    loc2, _ = D._find_section_link(page2, ["See more"])
    assert loc2 is not None


# ── reading a fact off the page ──────────────────────────────────────────────────

def test_find_passes_when_the_answer_is_grounded_in_the_page(monkeypatch):
    from web_watcher import llm
    page = FakePage(text="1998 Toyota Tacoma  $8,500  Anacortes, WA")
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps(
        {"answer": "1998 Toyota Tacoma for $8,500", "quote": "1998 Toyota Tacoma  $8,500", "found": True}))
    res = D._step_find(page, {"question": "first listing?"}, _cfg())
    assert res["ok"] is True and res["grounded"] is True


def test_find_flags_an_answer_whose_quote_is_not_in_the_page(monkeypatch):
    from web_watcher import llm
    page = FakePage(text="Nothing relevant here")
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps(
        {"answer": "2015 Ford F-150 for $12,000", "quote": "2015 Ford F-150", "found": True}))
    res = D._step_find(page, {"question": "first listing?"}, _cfg())
    assert res["ok"] is False and "NOT in the page text" in res["detail"]


def test_find_reports_honestly_when_the_page_has_no_answer(monkeypatch):
    from web_watcher import llm
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps(
        {"answer": "not found in the page text", "quote": "", "found": False}))
    assert D._step_find(FakePage(text="unrelated"), {"question": "q"}, _cfg())["ok"] is False


# ── Facebook safety ──────────────────────────────────────────────────────────────

def test_checkpoint_aborts_the_drill_and_engages_the_halt(monkeypatch, tmp_path):
    from web_watcher import fb_safety
    monkeypatch.setattr(fb_safety, "is_checkpoint", lambda p: True)
    monkeypatch.setattr(fb_safety, "checkpoint_reason", lambda p: "Confirm your identity")
    engaged = {}
    monkeypatch.setattr(fb_safety, "engage_halt",
                        lambda reason, watch="", data_dir=None: engaged.update(reason=reason))
    res = D._step_safety(FakePage(url="https://www.facebook.com/checkpoint/"), {})
    assert res["ok"] is False and res["fatal"] is True
    assert engaged["reason"] == "Confirm your identity"      # halted, never clicked through


def test_a_halted_facebook_is_not_drilled(monkeypatch):
    from web_watcher import fb_safety
    monkeypatch.setattr(fb_safety, "halt_state", lambda data_dir=None: {"reason": "restricted", "at": 0})
    monkeypatch.setattr(D, "save_report", lambda *a, **k: None)
    report = D.run_drill("facebook.com", _cfg())
    assert report["ok"] is False
    assert report["steps"][0]["step"] == "halt"              # bailed before opening a browser


def test_logged_out_of_facebook_fails_with_the_fix(monkeypatch):
    from web_watcher import monitor
    monkeypatch.setattr(monitor, "is_login_wall", lambda p: True)
    res = D._step_session(FakePage(), {"expect_login": True})
    assert res["ok"] is False and "Connect Facebook" in res["detail"]


def test_logged_out_is_fine_where_no_login_is_needed(monkeypatch):
    from web_watcher import monitor
    monkeypatch.setattr(monitor, "is_login_wall", lambda p: True)
    assert D._step_session(FakePage(), {"expect_login": False})["ok"] is True


# ── the report ───────────────────────────────────────────────────────────────────

def test_report_is_not_ready_unless_every_run_check_passed():
    r = D._finish("u", {}, 0.0, [D._step("a", True, ""), D._step("b", None, ""),
                                 D._step("c", False, "")])
    assert r["ok"] is False and r["ran"] == 2 and r["passed"] == 1
    assert "NOT READY" in D.render_report(r)


def test_render_report_handles_no_drill_yet():
    assert "No drill" in D.render_report({})
