"""Filling a results-page filter form — the part that looked so unstable to watch.

Observed live on craigslist: it typed the zip, pressed Enter (nothing happened, the zip was
still sitting there), typed the miles, pressed Enter, and only then did the page update. Two
causes, fixed together: the agent's force-Enter recovery — which exists for an unsubmitted
SEARCH box — was firing on filter boxes, and the deterministic filler submitted per-field
instead of filling everything and clicking the form's own Apply button once.

See navigate._apply_inline_filters and agent._is_filter_field / _click_apply_button."""

from __future__ import annotations

from web_watcher import agent as A
from web_watcher import navigate as N


# ── telling a filter box from a search box ───────────────────────────────────────

class _Page:
    def __init__(self, focused=None, url="https://x/search/cta"):
        self._focused, self.url = focused, url
        self.clicked = []

    def evaluate(self, js):
        return self._focused

    def locator(self, sel):
        return _Loc(self, sel)


class _Loc:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel
        self.present = sel in page_apply_present

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self.present else 0

    def is_visible(self):
        return self.present

    def click(self, timeout=0):
        self.page.clicked.append(self.sel)


page_apply_present: set = set()


def test_filter_boxes_are_recognised():
    for focused in ("postal  Enter zip  ", "  miles from zip  distance",
                    "min  Min price ", "max  Max ", "search_distance  radius "):
        assert A._is_filter_field(_Page(focused)) is True, focused


def test_a_search_box_is_not_a_filter_box():
    for focused in ("query  search craigslist  ", "q  Search for anything ",
                    "  What are you looking for?  ", "_nkw  Search eBay "):
        assert A._is_filter_field(_Page(focused)) is False, focused


def test_nothing_focused_is_treated_as_a_search_box():
    """Unknown → behave exactly as before, so this can only ever remove a wrong Enter."""
    assert A._is_filter_field(_Page(None)) is False


def test_an_unreadable_page_never_raises():
    class _Boom:
        def evaluate(self, js):
            raise RuntimeError("detached")
    assert A._is_filter_field(_Boom()) is False


# ── clicking Apply instead ───────────────────────────────────────────────────────

def test_the_apply_button_is_clicked_when_present():
    global page_apply_present
    page_apply_present = {"button.cl-exec-search"}
    p = _Page("postal")
    assert A._click_apply_button(p) is True
    assert p.clicked == ["button.cl-exec-search"]


def test_no_apply_button_reports_false():
    global page_apply_present
    page_apply_present = set()
    assert A._click_apply_button(_Page("postal")) is False


# ── the deterministic filler ─────────────────────────────────────────────────────

class _FillPage:
    """Records what got filled and how the form was submitted."""
    def __init__(self, has_postal=True):
        self.has_postal = has_postal
        self.filled, self.enters, self.clicks = {}, 0, []
        self.url = "https://skagit.craigslist.org/search/cta"

    class _KB:
        def __init__(self, outer): self.outer = outer
        def press(self, key): self.outer.enters += 1

    @property
    def keyboard(self):
        return self._KB(self)

    def wait_for_load_state(self, *a, **k): pass
    def wait_for_timeout(self, *a, **k): pass
    def wait_for_selector(self, *a, **k): pass


def _stub_navigate(monkeypatch, page):
    """Wire navigate's DOM helpers to the fake page."""
    boxes = {}

    def first_visible(pg, selector):
        if not selector:
            return None
        key = ("postal" if "postal" in selector else
               "distance" if "tel" in selector or "mile" in selector else
               "price_min" if "min" in selector else
               "price_max" if "max" in selector else None)
        if key == "postal" and not page.has_postal:
            return None
        if key is None:
            return None
        boxes.setdefault(key, key)
        return key

    monkeypatch.setattr(N, "_first_visible", first_visible)
    monkeypatch.setattr(N, "_wait_for_controls", lambda *a, **k: True)
    monkeypatch.setattr(N, "_human_fill", lambda loc, val: page.filled.update({loc: val}) or True)
    monkeypatch.setattr(N, "_pause", lambda *a, **k: None)
    monkeypatch.setattr(N, "_wait_for_url_change", lambda *a, **k: True)
    monkeypatch.setattr(N, "_click_selector",
                        lambda pg, sel: bool(sel) and (page.clicks.append(sel) or True))


_HINT = {"postal": "input[name='postal']", "distance": "input[type='tel'][placeholder*='mile']",
         "price_min": "input[placeholder='min']", "price_max": "input[placeholder='max']",
         "apply": "button.cl-exec-search"}


def test_everything_is_filled_then_applied_once(monkeypatch):
    page = _FillPage()
    _stub_navigate(monkeypatch, page)
    req = N.SearchRequest(zip="98221", radius=100, price_max=8000, site="craigslist")
    assert N._apply_inline_filters(page, req, _HINT) is True
    assert page.filled == {"postal": "98221", "distance": "100", "price_max": "8000"}
    assert page.clicks == ["button.cl-exec-search"]     # ONE submit, via the form's own button
    assert page.enters == 0                             # never Enter-per-field


def test_distance_is_skipped_when_the_page_has_no_zip_box(monkeypatch):
    """A radius with no centre filters nothing — and typing into it is what failed live."""
    page = _FillPage(has_postal=False)
    _stub_navigate(monkeypatch, page)
    req = N.SearchRequest(zip="98221", radius=100, price_max=8000, site="craigslist")
    N._apply_inline_filters(page, req, _HINT)
    assert "distance" not in page.filled
    assert "postal" not in page.filled
    assert page.filled == {"price_max": "8000"}         # price still applies


def test_enter_is_only_a_fallback_when_there_is_no_apply_button(monkeypatch):
    page = _FillPage()
    _stub_navigate(monkeypatch, page)
    hint = dict(_HINT); hint["apply"] = ""
    N._apply_inline_filters(page, N.SearchRequest(zip="98221", radius=50), hint)
    assert page.enters == 1                             # once, not per field


def test_nothing_to_fill_reports_false(monkeypatch):
    page = _FillPage()
    _stub_navigate(monkeypatch, page)
    assert N._apply_inline_filters(page, N.SearchRequest(site="craigslist"), _HINT) is False
    assert page.clicks == [] and page.enters == 0
