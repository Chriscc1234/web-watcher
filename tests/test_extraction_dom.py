"""
DOM-level test for the listing-card extractor's climb guard. Uses a real headless
Chromium with synthetic HTML — the only way to exercise the JS card-climb.

Reproduces the bug where many different listings inside one results list were all
labelled with the FIRST result's text (the "2002 Mazda Miata" that showed 74 bogus
dupes): a card whose <li> has no inline price used to climb all the way up into the
shared <ol>, whose innerText is dominated by the first result.
"""

from __future__ import annotations

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

from web_watcher.monitor import extract_listings  # noqa: E402


# A results list where each <li> holds ONE listing (title anchor, no inline price text —
# price lives in a separate column), all wrapped in a shared <ol>. The old climb overshot
# into the <ol> and stamped the first title onto every row.
_HTML = """
<!doctype html><html><body>
<ol class="results">
  <li class="result">
    <a class="img" href="https://seattle.craigslist.org/see/ctd/d/mazda/7000000001.html"></a>
    <div class="meta"><a class="title" href="https://seattle.craigslist.org/see/ctd/d/mazda/7000000001.html">2002 Mazda Miata Roadster Wonderful Little Car</a></div>
  </li>
  <li class="result">
    <a class="img" href="https://seattle.craigslist.org/tac/ctd/d/toyota/7000000002.html"></a>
    <div class="meta"><a class="title" href="https://seattle.craigslist.org/tac/ctd/d/toyota/7000000002.html">2020 Toyota Tacoma Double Cab 4x4</a></div>
  </li>
  <li class="result">
    <a class="img" href="https://seattle.craigslist.org/est/ctd/d/ford/7000000003.html"></a>
    <div class="meta"><a class="title" href="https://seattle.craigslist.org/est/ctd/d/ford/7000000003.html">2025 Ford F150 XLT 4WD Pickup</a></div>
  </li>
</ol>
</body></html>
"""


def test_climb_guard_keeps_titles_distinct():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_HTML)
        listings = extract_listings(page, max_items=20)
        browser.close()

    by_key = {l.key: l.title for l in listings}
    assert len(by_key) == 3, f"expected 3 distinct listings, got {by_key}"
    # Each listing must carry ITS OWN title — not the first result's text stamped onto all.
    assert "Mazda Miata" in by_key["craigslist:7000000001"]
    assert "Toyota Tacoma" in by_key["craigslist:7000000002"]
    assert "Ford F150" in by_key["craigslist:7000000003"]
    # And crucially, the three titles are all different (the bug made them identical).
    assert len(set(by_key.values())) == 3
