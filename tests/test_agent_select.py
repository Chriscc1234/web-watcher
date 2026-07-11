"""
Tests for the agent's 'select' action (#77 — drive sort/filter dropdowns).

A synthetic click on a native <select> opens the OS-rendered dropdown that no
automation can see, so 'select' (element_index + exact option text) is the ONLY way
the agent can change a sort order or filter implemented as a real select. These tests
run the full snapshot → _execute path against a local Playwright page.
"""

from __future__ import annotations

import urllib.parse

import pytest
from playwright.sync_api import sync_playwright

from web_watcher.agent import (
    _SYSTEM,
    AgentAction,
    _elements_text,
    _execute,
    _snapshot_elements,
)

SORT_PAGE = """
<html><body style="margin:40px">
  <h1>Results</h1>
  <button id="other">Filters</button>
  <label for="sortsel">sort</label>
  <select id="sortsel" name="sort">
    <option value="rel" selected>relevant</option>
    <option value="date">newest</option>
    <option value="priceasc">lowest price</option>
    <option value="pricedsc">highest price</option>
  </select>
  <div id="log"></div>
  <script>
    document.getElementById('sortsel').addEventListener('change', e => {
      document.getElementById('log').textContent = 'changed:' + e.target.value;
    });
  </script>
</body></html>
"""


@pytest.fixture(scope="module")
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page()
        yield pg
        browser.close()


def _load(pg):
    pg.goto("data:text/html," + urllib.parse.quote(SORT_PAGE))


def _sort_el(pg):
    els = _snapshot_elements(pg)
    el = next(e for e in els if e["tag"] == "select")
    return el, els


def test_snapshot_lists_select_options(page):
    _load(page)
    el, els = _sort_el(page)
    assert el["options"] == ["relevant", "newest", "lowest price", "highest price"]
    assert el["value"] == "relevant"          # selected TEXT, not value attr
    assert el["sel_id"]                        # stamped for the select action
    text = _elements_text(els)
    assert "DROPDOWN" in text and "newest" in text and "use select" in text


def test_select_action_changes_value_and_fires_change(page):
    _load(page)
    el, els = _sort_el(page)
    action = AgentAction(thought="sort by newest", action="select",
                         element_index=el["index"], text="newest")
    _execute(page, action, els)
    assert action.outcome == "selected 'newest'"
    assert page.eval_on_selector("#sortsel", "el => el.value") == "date"
    assert page.text_content("#log") == "changed:date"   # change event fired


def test_select_matches_case_insensitively_and_by_substring(page):
    _load(page)
    el, els = _sort_el(page)
    action = AgentAction(thought="", action="select",
                         element_index=el["index"], text="LOWEST")
    _execute(page, action, els)
    assert action.outcome == "selected 'lowest price'"


def test_select_unknown_option_rejected_with_choices(page):
    _load(page)
    el, els = _sort_el(page)
    action = AgentAction(thought="", action="select",
                         element_index=el["index"], text="cheapest first")
    _execute(page, action, els)
    assert action.outcome.startswith("REJECTED")
    assert "newest" in action.outcome         # tells the model what IS available


def test_select_on_non_dropdown_rejected(page):
    _load(page)
    _, els = _sort_el(page)
    link = next((e for e in els if e["tag"] != "select"), None)
    if link is None:
        pytest.skip("page has only the select")
    action = AgentAction(thought="", action="select",
                         element_index=link["index"], text="newest")
    _execute(page, action, els)
    assert action.outcome.startswith("REJECTED")


def test_system_prompt_documents_select():
    assert '"select"' in _SYSTEM
    assert "DROPDOWN" in _SYSTEM


def test_coerce_index_variants():
    from web_watcher.agent import _coerce_index
    assert _coerce_index(29) == 29
    assert _coerce_index(29.0) == 29
    assert _coerce_index("29") == 29
    assert _coerce_index("[29]") == 29       # model echoed the display format
    assert _coerce_index([29]) == 29
    assert _coerce_index("element 7") == 7
    assert _coerce_index(None) is None
    assert _coerce_index(True) is None
    assert _coerce_index("no digits") is None
