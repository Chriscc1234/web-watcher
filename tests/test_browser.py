"""
Browser controller tests.

Tests marked @pytest.mark.live hit the real NWS weather page — run them
intentionally:  pytest -m live

All other tests use a local Playwright page (data: URI) to avoid network
dependency in CI / offline runs.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from web_watcher.browser import BrowserSession, _locator
from web_watcher.config import AppConfig, ClickStep, Watch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SIMPLE_WATCH = Watch(
    name="test",
    urls=["about:blank"],
    interval_minutes=5,
    instruction="test",
)

HTML_PAGE = """
<html><body>
  <h1>Hello Web Watcher</h1>
  <p id="para">Some body text here.</p>
  <button id="btn">Click me</button>
  <p id="after-click" style="display:none">You clicked!</p>
  <select id="sel"><option value="a">A</option><option value="b">B</option></select>
</body></html>
"""


def _data_url(html: str) -> str:
    import urllib.parse
    return "data:text/html," + urllib.parse.quote(html)


# ---------------------------------------------------------------------------
# Context manager lifecycle
# ---------------------------------------------------------------------------

def test_session_enters_and_exits():
    with BrowserSession(headless=True) as s:
        assert s._browser is not None
    assert s._browser.is_connected() is False


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def test_extract_text_returns_body_content():
    with BrowserSession(headless=True) as s:
        assert s._browser is not None
        page = s._browser.new_page()
        page.goto(_data_url(HTML_PAGE))
        text = s.extract_text(page)
        page.close()
    assert "Hello Web Watcher" in text
    assert "Some body text here" in text


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------

def test_screenshot_returns_png_bytes():
    with BrowserSession(headless=True) as s:
        page = s._browser.new_page()
        page.goto(_data_url(HTML_PAGE))
        png = s.screenshot(page)
        page.close()
    assert png is not None
    assert png[:4] == b"\x89PNG"


# ---------------------------------------------------------------------------
# Click-path: click action
# ---------------------------------------------------------------------------

CLICK_HTML = """
<html><body>
  <button id="btn" onclick="document.getElementById('result').textContent='clicked'">Go</button>
  <span id="result"></span>
</body></html>
"""

def test_click_path_click_fires_action():
    steps = [ClickStep(action="click", target="#btn")]
    watch = Watch(
        name="t", urls=[_data_url(CLICK_HTML)], interval_minutes=1, instruction="x",
        click_path=steps,
    )
    with BrowserSession(headless=True) as s:
        results = s.run_watch(watch)
    assert results[0].error is None
    assert "clicked" in results[0].text


# ---------------------------------------------------------------------------
# Click-path: scroll action
# ---------------------------------------------------------------------------

def test_click_path_scroll_does_not_crash():
    steps = [ClickStep(action="scroll", amount=300)]
    watch = Watch(
        name="t", urls=[_data_url(HTML_PAGE)], interval_minutes=1, instruction="x",
        click_path=steps,
    )
    with BrowserSession(headless=True) as s:
        results = s.run_watch(watch)
    assert results[0].error is None


# ---------------------------------------------------------------------------
# Click-path: wait_ms action
# ---------------------------------------------------------------------------

def test_click_path_wait_ms_does_not_crash():
    steps = [ClickStep(action="wait_ms", amount=100)]
    watch = Watch(
        name="t", urls=[_data_url(HTML_PAGE)], interval_minutes=1, instruction="x",
        click_path=steps,
    )
    with BrowserSession(headless=True) as s:
        results = s.run_watch(watch)
    assert results[0].error is None


# ---------------------------------------------------------------------------
# Missing-selector: must log and continue, not crash
# ---------------------------------------------------------------------------

def test_missing_selector_captured_in_result_not_raised():
    steps = [ClickStep(action="click", target="#does-not-exist")]
    watch = Watch(
        name="t", urls=[_data_url(HTML_PAGE)], interval_minutes=1, instruction="x",
        click_path=steps,
    )
    with BrowserSession(headless=True) as s:
        results = s.run_watch(watch)
    # The click fails but text extraction still ran — no crash, result has text
    assert results[0].error is None          # page-level error is None
    assert "Hello Web Watcher" in results[0].text  # text still extracted


# ---------------------------------------------------------------------------
# Multiple URLs
# ---------------------------------------------------------------------------

def test_multiple_urls_returns_one_result_each():
    watch = Watch(
        name="t",
        urls=[_data_url(HTML_PAGE), _data_url("<html><body>Second</body></html>")],
        interval_minutes=1,
        instruction="x",
    )
    with BrowserSession(headless=True) as s:
        results = s.run_watch(watch)
    assert len(results) == 2
    assert "Hello Web Watcher" in results[0].text
    assert "Second" in results[1].text


# ---------------------------------------------------------------------------
# Navigation error
# ---------------------------------------------------------------------------

def test_unreachable_url_captured_not_raised():
    watch = Watch(
        name="t",
        urls=["http://localhost:19999/does-not-exist"],
        interval_minutes=1,
        instruction="x",
    )
    with BrowserSession(headless=True) as s:
        results = s.run_watch(watch)
    assert results[0].error is not None


# ---------------------------------------------------------------------------
# Live test — real NWS weather page (requires internet)
# ---------------------------------------------------------------------------

@pytest.mark.live
def test_live_nws_weather_extracts_text():
    url = "https://forecast.weather.gov/MapClick.php?CityName=Seattle&state=WA&site=SEW&textField1=47.6062&textField2=-122.3321"
    watch = Watch(name="nws", urls=[url], interval_minutes=30, instruction="frost warning")
    with BrowserSession(headless=True) as s:
        results = s.run_watch(watch)
    assert results[0].error is None
    text = results[0].text
    assert len(text) > 200, f"Expected substantial text, got {len(text)} chars"
    print(f"\nExtracted {len(text)} chars. First 300:\n{text[:300]}")
