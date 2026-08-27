"""
What the deep-read actually hands the judge, and where it is allowed to harvest from.

Both defects here were found by running the new deep-read against live eBay. It read twelve
listings successfully — and every body was eight thousand characters of eBay's global category
menu, because the seller's description lives in a cross-origin iframe, nothing matched, and the
fallback took `document.body.innerText`. That failure is invisible from the outside: no error,
a healthy-looking length, pure navigation inside. The same run also banked fifteen eBay HOME
page promo tiles (flip-flops, an iPhone, earbuds) into a MacGregor sailboat watch.
"""

from __future__ import annotations

import pytest

from web_watcher import monitor, scheduler


BODY_JS = monitor._BODY_JS


# --------------------------------------------------------------------------
# The fallback must not return page furniture
# --------------------------------------------------------------------------

def _run_body_js(html: str) -> str:
    """Execute the real extraction JS against `html` in a real browser."""
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(html)
            return (page.evaluate(BODY_JS) or "").strip()
        finally:
            browser.close()


EBAY_LIKE = """
<body>
  <header id="gh">
    <nav><a href="/1">Antiques</a><a href="/2">Art</a><a href="/3">Baby</a>
         <a href="/4">Books</a><a href="/5">Business &amp; Industrial</a>
         <a href="/6">Cameras &amp; Photo</a><a href="/7">Cell Phones</a>
         <a href="/8">Clothing, Shoes &amp; Accessories</a><a href="/9">Coins</a>
         <a href="/10">Collectibles</a><a href="/11">Computers</a>
         <a href="/12">Consumer Electronics</a><a href="/13">Crafts</a></nav>
  </header>
  <main>
    <div id="realad">1985 MacGregor 25 swing keel sailboat with galvanized trailer.
      New standing rigging in 2023, main and 110 jib in good shape, 9.9hp Yamaha
      outboard that starts on the first pull. Kept on a trailer under cover every
      winter. Clean title in hand. Serious buyers only please, no trades.</div>
  </main>
  <footer id="glbfooter"><a href="/a">About</a><a href="/b">Help</a></footer>
</body>
"""


@pytest.mark.live
def test_fallback_returns_the_ad_not_the_category_menu():
    text = _run_body_js(EBAY_LIKE)
    assert "MacGregor 25 swing keel" in text, "the actual ad was not extracted"
    assert "Business & Industrial" not in text, "returned the global category menu"
    assert "Antiques" not in text and "Collectibles" not in text


@pytest.mark.live
def test_fallback_prefers_prose_over_a_link_farm():
    html = """
    <body><div>
      <div id="links">""" + "".join(
        f'<a href="/{i}">Category number {i} in the list</a>' for i in range(60)
    ) + """</div>
      <div id="ad">MacGregor 26X in excellent condition, one owner since new, always
        stored indoors, trailer included with new tires and bearings, ready to sail
        away this weekend. Call for details and to arrange a viewing.</div>
    </div></body>"""
    text = _run_body_js(html)
    assert "MacGregor 26X" in text
    assert "Category number 30" not in text


@pytest.mark.live
def test_known_selector_still_wins_over_the_fallback():
    html = """<body><nav><a href=/>junk</a></nav>
              <div id="postingbody">MacGregor 26 for sale, runs great, must see today.</div>
              </body>"""
    text = _run_body_js(html)
    assert text.startswith("MacGregor 26 for sale")


@pytest.mark.live
def test_body_stays_bounded():
    html = "<body><div>" + ("word " * 40_000) + "</div></body>"
    assert len(_run_body_js(html)) <= 8000


@pytest.mark.live
def test_empty_page_yields_empty_not_a_crash():
    assert _run_body_js("<body></body>") == ""


# --------------------------------------------------------------------------
# The description iframe (eBay serves it cross-origin)
# --------------------------------------------------------------------------

class FakeFrame:
    def __init__(self, name="", url="", text=""):
        self.name, self.url, self._text = name, url, text

    def evaluate(self, js):
        return self._text


class FakePage:
    def __init__(self, frames):
        self.main_frame = frames[0]
        self.frames = frames

    def evaluate(self, js):
        return ""       # main document has nothing — the eBay case


def test_reads_the_ebay_description_iframe():
    main = FakeFrame(url="https://www.ebay.com/itm/123")
    desc = FakeFrame(name="desc_ifr", url="https://vi.vipr.ebaydesc.com/itmdesc/123",
                     text="MacGregor 26X, 50hp Honda, trailer included. " * 4)
    body = monitor.extract_listing_body(FakePage([main, desc]))
    assert "MacGregor 26X" in body


def test_ignores_unrelated_iframes():
    main = FakeFrame(url="https://www.ebay.com/itm/123")
    ads  = FakeFrame(name="google_ads_iframe", url="https://doubleclick.net/x",
                     text="Buy now! " * 40)
    assert monitor.extract_listing_body(FakePage([main, ads])) == ""


def test_never_duplicates_a_description_already_in_the_main_document():
    text = "MacGregor 26X with trailer and outboard, ready to sail. " * 3

    class MainHasIt(FakePage):
        def evaluate(self, js):
            return text

    main = FakeFrame(url="https://www.ebay.com/itm/1")
    desc = FakeFrame(name="desc_ifr", url="https://ebaydesc.com/x", text=text)
    assert monitor.extract_listing_body(MainHasIt([main, desc])).count("ready to sail") == 3


def test_a_broken_frame_does_not_break_extraction():
    class Exploding(FakeFrame):
        def evaluate(self, js):
            raise RuntimeError("cross-origin")

    main = FakeFrame(url="https://x/itm/1")
    monitor.extract_listing_body(FakePage([main, Exploding(name="desc_ifr", url="d")]))


def test_page_with_no_frames_attribute_is_survivable():
    class NoFrames:
        def evaluate(self, js):
            return "an ad body long enough to count as real content here"

        @property
        def frames(self):
            raise RuntimeError("detached")

        main_frame = None

    assert "an ad body" in monitor.extract_listing_body(NoFrames())


# --------------------------------------------------------------------------
# Never harvest the site's home page
# --------------------------------------------------------------------------

def test_page_kind_separates_home_from_search():
    from web_watcher.agent import page_kind
    assert page_kind("https://www.ebay.com/") == "home"
    assert page_kind("https://www.ebay.com") == "home"
    assert page_kind("https://www.ebay.com/sch/i.html?_nkw=macgregor") == "search"
    assert page_kind("https://www.facebook.com/marketplace/seattle/search?query=x") == "search"


def test_home_page_promos_are_not_harvested(monkeypatch):
    """The live failure: an error page sent the agent to ebay.com, and its promo tiles —
    flip-flops, an iPhone, earbuds — were banked as findings on a sailboat watch."""
    from web_watcher.agent import page_kind

    promos = [monitor.Listing(key="p1", url="https://www.ebay.com/itm/1",
                              title="Crocs Adult Bayaband Flip Flop Sandals $19.99")]
    calls = []

    def fake_extract(pg, **kw):
        calls.append(pg.url)
        return promos

    monkeypatch.setattr(scheduler, "extract_listings", fake_extract)

    # Mirror of the guard in scheduler._harvest.
    harvested = {}

    def harvest(pg):
        if page_kind(pg.url) == "home":
            return
        for l in scheduler.extract_listings(pg):
            harvested[l.key] = l

    class Pg:
        def __init__(self, url):
            self.url = url

    harvest(Pg("https://www.ebay.com/"))
    assert harvested == {}, "harvested the home page"
    assert calls == [], "the extractor should not even have been asked"

    harvest(Pg("https://www.ebay.com/sch/i.html?_nkw=macgregor+sailboat"))
    assert "p1" in harvested, "a real results page must still be harvested"


def test_listing_pages_are_still_harvestable():
    """The guard targets the HOME page only — an ad page the agent opened is fair game."""
    from web_watcher.agent import page_kind
    assert page_kind("https://www.ebay.com/itm/168629102521") != "home"
