"""Two deterministic gates that decide what may ever count as a match: the BUDGET and the AREA.

Both exist because of the same live failure — a $15,000 boat watch centred on Anacortes reported
$30,000, $29,000 and $28,000 boats, some of them in British Columbia. Whether $30k exceeds $15k
is arithmetic, and whether Vancouver is within 150 miles of Anacortes is geometry; neither is a
judgement call, so neither is left to a 14b. The site's own filters can't be relied on either —
the agent sorts and scrolls its way onto pages where they no longer apply.

See cl_geo.watch_price_cap / cl_geo.place_from_text / scheduler._keyword_prefilter."""

from __future__ import annotations

from web_watcher.cl_geo import (is_placeholder_price, place_from_text, price_cap_from_text,
                                watch_price_cap)
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


def test_a_listing_slightly_over_the_cap_is_kept():
    """The "on the edge" grace band: a $16k boat on a $15k watch is worth seeing — a seller often
    takes a bit less. Within 10% of the cap comes through on PURPOSE now, not by luck."""
    kept, dropped = _keyword_prefilter(
        [_L("Just over", 16000), _L("Right at the edge", 16500)], _watch())   # cap*1.10 = 16,500
    assert [l.title for l in kept] == ["Just over", "Right at the edge"]
    assert not dropped


def test_a_listing_past_the_grace_band_is_still_dropped():
    """Grace is small: $17k is past the 10% band on a $15k watch, and $30k is nowhere near."""
    kept, dropped = _keyword_prefilter(
        [_L("Too far over", 17000), _L("Way over", 30000)], _watch())
    assert kept == []
    assert len(dropped) == 2 and "over budget" in dropped[0].judge_reason


# ── placeholder "make me an offer" prices ─────────────────────────────────────────

def test_placeholder_prices_are_recognised():
    for junk in (12345, 123456, 1234567, 99999, 11111, 555555, 0, -1, "12345", "99,999"):
        assert is_placeholder_price(junk) is True, junk


def test_real_prices_are_not_mistaken_for_placeholders():
    for real in (2345, 9500, 15000, 8800, 1234, 1111, 12000, 4250, 6200, 750):
        assert is_placeholder_price(real) is False, real
    assert is_placeholder_price(None) is False and is_placeholder_price("call") is False


def test_a_placeholder_price_is_not_dropped_as_over_budget():
    """A '$12,345 — obo' post on a $15k watch used to survive by luck; on a lower-budget watch it
    was WRONGLY dropped as over budget. Now it's treated as unknown and the judge gets to look."""
    w = _watch(urls=["https://skagit.craigslist.org/search/boo?max_price=8000"],
               instruction="cars under 8k")
    kept, dropped = _keyword_prefilter(
        [_L("Make offer boat", 12345), _L("Real over-budget", 20000)], w)
    assert [l.title for l in kept] == ["Make offer boat"]     # placeholder kept
    assert [l.title for l in dropped] == ["Real over-budget"]  # a real over-budget price still goes


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


# ── the area gate ────────────────────────────────────────────────────────────────
# The surprise from the live data: craigslist's radius filter was working perfectly. 100 miles
# from Anacortes reaches Metro Vancouver, which is CLOSER than Seattle (62 vs 60 miles). Those
# listings are genuinely in range and still wrong — another country, another currency, a border.
# The US gazetteer alone can't settle it: it maps Surrey to North Dakota and Vancouver to
# Washington, both real and both nowhere near. Resolving the name against the ANCHOR does.

from web_watcher.cl_geo import city_is_near, listing_city, url_radius

_ANACORTES = (48.4542, -122.6039)


def test_the_town_is_read_from_the_listing_url():
    assert listing_city(
        "https://www.craigslist.org/view/d/port-moody-sea-ray-200-select/iq82a5USvpykg17TtzgwWf"
    ).startswith("port moody")
    assert listing_city("https://example.com/not-a-listing") == ""


def test_nearby_washington_towns_are_near():
    for slug in ("tacoma-19ft-ocean-going-boat", "olympia-1990-searay-21-open-bow",
                 "mercer-island-cobalt-bowrider", "shelton-ft-hewscraft-sea-runner"):
        city = slug.replace("-", " ")
        assert city_is_near(city, _ANACORTES, 150) is True, slug


def test_british_columbia_towns_are_not_near():
    """The reason a hand-written border list had to go: it missed Metchosin the first time it was
    tested. These now resolve through a real gazetteer and fail on COUNTRY, not on a lookup."""
    for slug in ("surrey immaculate 2014 larson", "port moody sea ray 200",
                 "burnaby boat trailer", "coquitlam skiff",
                 "metchosin 2006 volvo v70r wagon", "victoria bc sailboat"):
        assert city_is_near(slug, _ANACORTES, 150) is False, slug


def test_a_near_but_foreign_town_fails_on_country_not_distance():
    """Vancouver BC is 62 miles from Anacortes — closer than Seattle. It passes any distance
    test and is still wrong, so country is checked first and a mismatch fails outright."""
    from web_watcher.cl_geo import resolve_town, country_at
    cc, lat, lon = resolve_town("vancouver", _ANACORTES)
    assert cc == "CA"
    assert country_at(*_ANACORTES) == "US"
    assert city_is_near("vancouver something", _ANACORTES, 500) is False   # generous radius


def test_towns_are_disambiguated_by_the_anchor():
    """Victoria is in British Columbia, Texas and Argentina. Near Anacortes it is the BC one."""
    from web_watcher.cl_geo import resolve_town
    cc, lat, lon = resolve_town("victoria", _ANACORTES)
    assert cc == "CA" and 48.0 < lat < 49.0


def test_a_far_away_same_named_us_town_is_not_near():
    """Vancouver WA is a real US city ~200 miles off — outside a 100-mile watch."""
    assert city_is_near("vancouver", _ANACORTES, 100) is False


def test_an_unplaceable_town_is_kept():
    """Absence of evidence isn't evidence of absence — an unknown town must not be dropped."""
    assert city_is_near("zzzqqq widget", _ANACORTES, 150) is None
    assert city_is_near("", _ANACORTES, 150) is None


def test_only_the_leading_slug_words_name_the_town():
    """A word from the ITEM's title must not vouch for a listing posted somewhere else."""
    assert city_is_near("surrey boat with bellingham trailer", _ANACORTES, 150) is False


def test_the_radius_comes_from_the_url():
    assert url_radius("https://x/s?search_distance=150") == 150
    assert url_radius("https://x/s?radius=50") == 50
    assert url_radius("https://x/s") is None


def test_out_of_area_listings_are_dropped_by_the_prefilter():
    w = Watch(name="Anacortes Boats", instruction="boats near Anacortes", interval_minutes=30,
              urls=["https://skagit.craigslist.org/search/boo?postal=98221&search_distance=150"])

    class _U:
        def __init__(self, title, url, price=None):
            self.title, self.url, self.price_value = title, url, price
            self.details, self.judge_reason = "", ""

    kept, dropped = _keyword_prefilter([
        _U("Sea Ray", "https://www.craigslist.org/view/d/tacoma-sea-ray/aaaaaaaa", 9000),
        _U("Larson", "https://www.craigslist.org/view/d/surrey-larson-lx/bbbbbbbb", 9000),
    ], w)
    assert [l.title for l in kept] == ["Sea Ray"]
    assert "outside the" in dropped[0].judge_reason


def test_a_far_us_town_is_out_of_range_but_domestic():
    """Miami is in the right country and hopelessly far — distance still has a job."""
    from web_watcher.cl_geo import resolve_town
    assert resolve_town("miami", _ANACORTES)[0] == "US"
    assert city_is_near("miami speedboat", _ANACORTES, 150) is False


def test_the_home_country_is_read_from_the_anchor_not_assumed():
    """A watch anchored in Canada should treat Canadian towns as home and US ones as foreign —
    the rule is 'a different country from yours', not 'not America'."""
    from web_watcher.cl_geo import country_at
    vancouver = (49.2497, -123.1193)
    assert country_at(*vancouver) == "CA"
    assert city_is_near("burnaby boat trailer", vancouver, 100) is True
    assert city_is_near("bellingham skiff", vancouver, 100) is False
