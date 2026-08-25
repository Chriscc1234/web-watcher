"""Two deterministic gates that decide what may ever count as a match: the BUDGET and the AREA.

Both exist because of the same live failure — a $15,000 boat watch centred on Anacortes reported
$30,000, $29,000 and $28,000 boats, some of them in British Columbia. Whether $30k exceeds $15k
is arithmetic, and whether Vancouver is within 150 miles of Anacortes is geometry; neither is a
judgement call, so neither is left to a 14b. The site's own filters can't be relied on either —
the agent sorts and scrolls its way onto pages where they no longer apply.

See cl_geo.watch_price_cap / cl_geo.place_from_text / scheduler._keyword_prefilter."""

from __future__ import annotations

from web_watcher.cl_geo import place_from_text, price_cap_from_text, watch_price_cap
from web_watcher.config import Watch
from web_watcher.scheduler import _keyword_prefilter, _price_cap_for, _watch_geolocation


# ── reading the budget ───────────────────────────────────────────────────────────

def test_the_url_param_is_the_cap():
    assert watch_price_cap(["https://skagit.craigslist.org/search/boo?max_price=15000"], "") == 15000


def test_param_aliases_are_understood():
    for u in ("https://x/s?price_max=8000", "https://x/s?_udhi=8000", "https://x/s?maxPrice=8000"):
        assert watch_price_cap([u], "") == 8000


def test_plain_words_state_a_budget():
    assert price_cap_from_text("manual cars under 8k") == 8000
    assert price_cap_from_text("trucks under $9,500 within 100 miles") == 9500
    assert price_cap_from_text("up to 10,500 dollars") == 10500
    assert price_cap_from_text("cars under 12000") == 12000


def test_a_size_is_not_a_budget():
    """The whole reason this is careful: read as a $30 cap, "under 30-foot motor boats" would
    reject every boat ever posted."""
    assert price_cap_from_text("under 30-foot motor boats") is None
    assert price_cap_from_text("boats less than 25 feet") is None


def test_other_units_are_not_budgets():
    assert price_cap_from_text("trucks under 100000 miles") is None
    assert price_cap_from_text("cars under 150 hp") is None
    assert price_cap_from_text("campers up to 3000 lbs") is None


def test_a_size_and_a_budget_together_reads_the_budget():
    assert price_cap_from_text(
        "under 30-foot motor boats with outboard motors, priced within $15,000") == 15000


def test_no_budget_stated_is_no_cap():
    assert price_cap_from_text("any nice boat") is None
    assert watch_price_cap([], "") is None


# ── the budget gate ──────────────────────────────────────────────────────────────

class _L:
    def __init__(self, title, price=None):
        self.title, self.price_value, self.details = title, price, ""
        self.judge_reason = ""


def _watch(**kw):
    base = dict(name="Boats", urls=["https://skagit.craigslist.org/search/boo?max_price=15000"],
                instruction="under 30-foot motor boats", interval_minutes=30)
    base.update(kw)
    return Watch(**base)


def test_over_budget_listings_are_dropped_before_the_judge():
    kept, dropped = _keyword_prefilter(
        [_L("Sea Ray", 14500), _L("Larson LX", 29000), _L("Koffler jet boat", 30000)], _watch())
    assert [l.title for l in kept] == ["Sea Ray"]
    assert len(dropped) == 2
    assert "over budget" in dropped[0].judge_reason
    assert "$29,000" in dropped[0].judge_reason and "$15,000" in dropped[0].judge_reason


def test_a_listing_exactly_at_the_cap_is_kept():
    kept, _ = _keyword_prefilter([_L("At the cap", 15000)], _watch())
    assert len(kept) == 1


def test_a_listing_with_no_price_is_never_dropped_on_price():
    """A missing price is unknown, not expensive — the judge should still get to look."""
    kept, dropped = _keyword_prefilter([_L("Boat, call for price", None)], _watch())
    assert len(kept) == 1 and not dropped


def test_a_watch_with_no_budget_drops_nothing():
    w = _watch(urls=["https://skagit.craigslist.org/search/boo"], instruction="any nice boat")
    kept, dropped = _keyword_prefilter([_L("Expensive", 999_999)], w)
    assert len(kept) == 1 and not dropped


def test_the_cap_is_read_once_per_watch():
    w = _watch()
    assert _price_cap_for(w) == 15000
    assert _price_cap_for(w) == 15000          # cached, not re-parsed


def test_price_and_keyword_gates_work_together():
    w = _watch(antikeywords=["kayak"])
    kept, dropped = _keyword_prefilter(
        [_L("Nice skiff", 9000), _L("Kayak", 500), _L("Yacht", 90000)], w)
    assert [l.title for l in kept] == ["Nice skiff"]
    assert len(dropped) == 2


# ── the area anchor ──────────────────────────────────────────────────────────────
# Every zip rung looks for five digits. People write "within 150 miles of Anacortes".

def test_a_town_named_in_words_resolves():
    assert place_from_text("Anacortes Manual Transmission Cars Watch") is not None
    assert place_from_text("Look for boats within 150 miles of Anacortes") is not None


def test_watch_words_are_not_mistaken_for_towns():
    for text in ("manual transmission cars only", "under 30-foot motor boats", "best used trucks"):
        assert place_from_text(text) is None, text


def test_an_ambiguous_town_name_is_refused():
    """Many states have a Mount Vernon — guessing one would quietly watch the wrong coast."""
    assert place_from_text("Mount Vernon trucks under 8k") is None


def test_a_watch_with_a_bogus_zip_still_finds_its_anchor():
    """The live bug: postal=98214 is not a real zip, so every zip rung failed and the watch had
    NO anchor — which silently disabled the out-of-area filter entirely."""
    w = Watch(name="Anacortes Manual Transmission Cars Watch",
              urls=["https://seattle.craigslist.org/search/cta?postal=98214&max_price=8000"],
              instruction="manual transmission cars", interval_minutes=30)
    anchor = _watch_geolocation(w)
    assert anchor is not None
    assert 48.0 < anchor[0] < 49.0 and -123.5 < anchor[1] < -122.0     # Anacortes, not Seattle


def test_a_real_zip_in_the_url_still_wins():
    w = Watch(name="Boats", urls=["https://skagit.craigslist.org/search/boo?postal=98221"],
              instruction="boats", interval_minutes=30)
    assert _watch_geolocation(w) is not None


def test_a_watch_naming_nowhere_has_no_anchor():
    w = Watch(name="Boats", urls=["https://x/search/boo"], instruction="boats",
              interval_minutes=30)
    assert _watch_geolocation(w) is None
