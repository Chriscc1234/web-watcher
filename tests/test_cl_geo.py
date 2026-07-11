"""Tests for the craigslist URL refiner — the deterministic fix for the model stuffing
zips/prices/owner-words into the query text and guessing the wrong region subdomain
("anacortes" → seattle.craigslist.org when 98221 is served by skagit.craigslist.org)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from web_watcher.cl_geo import (
    nearest_region_host,
    refine_craigslist_url,
    zip_latlon,
)


def _parts(url):
    p = urlparse(url)
    return p.netloc, p.path, {k: v[0] for k, v in parse_qs(p.query).items()}


def test_zip_table_loads():
    ll = zip_latlon("98221")           # Anacortes, WA
    assert ll and 48 < ll[0] < 49 and -123 < ll[1] < -122


def test_nearest_region_for_anacortes_is_skagit():
    lat, lon = zip_latlon("98221")
    assert nearest_region_host(lat, lon) == "skagit"


def test_the_buddys_exact_broken_watch_url():
    """The real-world failure: literal query 'vehicles owner 98221 under 10k' on the
    wrong region. Must become a clean cars+trucks category search near Anacortes."""
    url = "https://seattle.craigslist.org/search/sss?query=vehicles+owner+98221+under+10k"
    host, path, q = _parts(refine_craigslist_url(url))
    assert host == "skagit.craigslist.org"
    assert path == "/search/cta"
    assert q["postal"] == "98221"
    assert q["max_price"] == "10000"
    assert q["purveyor"] == "owner"
    assert q["search_distance"] == "50"
    assert "query" not in q            # nothing left — pure category browse


def test_item_words_survive():
    url = "https://seattle.craigslist.org/search/cta?query=ford+f150+under+%248%2C500"
    host, path, q = _parts(refine_craigslist_url(url))
    assert q["query"] == "ford f150"
    assert q["max_price"] == "8500"
    assert host == "seattle.craigslist.org"   # no zip → region untouched


def test_min_price_and_k_suffix():
    url = "https://portland.craigslist.org/search/cta?query=truck+over+2k+under+10k"
    _, _, q = _parts(refine_craigslist_url(url))
    assert q["min_price"] == "2000"
    assert q["max_price"] == "10000"


def test_explicit_params_never_overwritten():
    url = ("https://skagit.craigslist.org/search/cta?query=vans+under+5k"
           "&max_price=7000&postal=98273&search_distance=25")
    host, _, q = _parts(refine_craigslist_url(url))
    assert q["max_price"] == "7000"        # explicit beats parsed
    assert q["postal"] == "98273"
    assert q["search_distance"] == "25"
    assert q["query"] == "vans"


def test_idempotent():
    url = "https://seattle.craigslist.org/search/sss?query=vehicles+owner+98221+under+10k"
    once = refine_craigslist_url(url)
    assert refine_craigslist_url(once) == once


def test_non_craigslist_untouched():
    url = "https://offerup.com/search?q=truck+98221+under+10k"
    assert refine_craigslist_url(url) == url


def test_garbage_returns_original():
    assert refine_craigslist_url("not a url at all") == "not a url at all"


def test_fake_zip_is_not_a_postal():
    """Five digits that aren't a real zip (e.g. a model number) stay in the query."""
    url = "https://seattle.craigslist.org/search/sss?query=kicker+00000+amp"
    _, _, q = _parts(refine_craigslist_url(url))
    assert "postal" not in q
    assert "00000" in q["query"]


def test_hallucinated_place_subdomain_resolves():
    """14b live failure: 'anacortes.craigslist.org' isn't a region. The town name must
    resolve to skagit + a postal filter near Anacortes."""
    url = "https://anacortes.craigslist.org/search/cta?max_price=10000&query=vehicles"
    host, path, q = _parts(refine_craigslist_url(url))
    assert host == "skagit.craigslist.org"
    assert q["postal"].startswith("982")
    assert q["search_distance"] == "50"
    assert "query" not in q          # "vehicles" is the category, not a term
    assert path == "/search/cta"


def test_in_place_phrase_moves_to_postal():
    url = "https://seattle.craigslist.org/search/sss?query=diesel+trucks+in+anacortes"
    host, path, q = _parts(refine_craigslist_url(url))
    assert host == "skagit.craigslist.org"
    assert q["postal"].startswith("982")
    assert q["query"] == "diesel"    # 'trucks' folded into the cta category
    assert path == "/search/cta"


def test_place_named_car_models_are_never_eaten():
    """toyota tacoma / chevy colorado / dodge dakota are QUERIES, not places — no
    preposition means no place extraction."""
    for query in ("toyota+tacoma", "chevy+colorado", "dodge+dakota+4x4"):
        url = f"https://seattle.craigslist.org/search/cta?query={query}"
        host, _, q = _parts(refine_craigslist_url(url))
        assert host == "seattle.craigslist.org"
        assert q["query"] == query.replace("+", " ")
        assert "postal" not in q


def test_generic_vehicle_word_on_wrong_category_switches_to_cta():
    """3b live failure: /search/sof (a jobs category!) for vehicles."""
    url = "https://seattle.craigslist.org/search/sof?query=vehicles&max_price=10000"
    _, path, q = _parts(refine_craigslist_url(url))
    assert path == "/search/cta"
    assert "query" not in q


def test_ambiguous_place_without_anchor_is_skipped():
    """'springfield' exists in a dozen states — with no valid region to anchor to, we
    must NOT guess one."""
    url = "https://springfield.craigslist.org/search/cta?query=couch"
    host, _, q = _parts(refine_craigslist_url(url))
    assert host == "springfield.craigslist.org"   # left for the user to fix
    assert "postal" not in q


def test_ambiguous_place_with_anchor_resolves_nearby():
    url = "https://seattle.craigslist.org/search/sss?query=kayak+in+vancouver"
    host, _, q = _parts(refine_craigslist_url(url))
    # Vancouver WA is ~130 mi from the Seattle anchor → resolves; region flips to portland
    assert q.get("postal", "").startswith("986")
    assert host == "portland.craigslist.org"
