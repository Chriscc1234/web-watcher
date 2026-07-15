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


# ── #87: locations on every site, not just craigslist ───────────────────────

from web_watcher.cl_geo import (
    refine_ebay_url,
    refine_facebook_url,
    refine_offerup_url,
    refine_search_url,
    url_zip,
)


def test_offerup_fabricated_city_path_is_canonicalized():
    """The live '/WA-Anacortes/search?priceMax=' URL 403s — must become the real
    /search endpoint with real params, location words dropped (IP geolocates)."""
    url = "https://www.offerup.com/WA-Anacortes/search?q=vehicles+in+anacortes&priceMax=10000"
    host, path, q = _parts(refine_offerup_url(url))
    assert host == "offerup.com"
    assert path == "/search"
    assert q["price_max"] == "10000"          # fake priceMax → real price_max
    assert q["radius"] == "50"                # location intent → explicit radius
    assert "anacortes" not in q.get("q", "").lower()
    assert q["q"] == "vehicles"


def test_offerup_price_words_in_query_become_params():
    url = "https://offerup.com/search?q=diesel+truck+under+8k"
    _, _, q = _parts(refine_offerup_url(url))
    assert q["q"] == "diesel truck"
    assert q["price_max"] == "8000"


def test_offerup_clean_url_untouched_shape():
    url = "https://offerup.com/search?q=kayak"
    host, path, q = _parts(refine_offerup_url(url))
    assert (host, path, q) == ("offerup.com", "/search", {"q": "kayak"})


def test_ebay_zip_and_price_from_query_text():
    url = "https://www.ebay.com/sch/i.html?_nkw=vehicles+98221+under+10k"
    host, path, q = _parts(refine_ebay_url(url))
    assert host == "www.ebay.com"
    assert q["_stpos"] == "98221"
    assert q["_sadis"] == "50"
    assert q["_udhi"] == "10000"
    assert "98221" not in q.get("_nkw", "")


def test_ebay_fallback_zip_from_sibling_url():
    url = "https://www.ebay.com/sch/i.html?_nkw=diesel+truck"
    _, _, q = _parts(refine_ebay_url(url, fallback_zip="98221"))
    assert q["_stpos"] == "98221"
    assert q["_sadis"] == "50"
    assert q["_nkw"] == "diesel truck"


def test_ebay_explicit_stpos_wins_over_fallback():
    url = "https://www.ebay.com/sch/i.html?_nkw=truck&_stpos=98273&_sadis=25"
    _, _, q = _parts(refine_ebay_url(url, fallback_zip="98221"))
    assert q["_stpos"] == "98273"
    assert q["_sadis"] == "25"


def test_facebook_price_moves_to_real_params():
    url = "https://www.facebook.com/marketplace/seattle/search?query=trucks+under+10k+in+anacortes"
    host, path, q = _parts(refine_facebook_url(url))
    assert path == "/marketplace/seattle/search"
    assert q["maxPrice"] == "10000"
    assert q["query"] == "trucks"


def test_dispatcher_routes_by_site():
    cl = refine_search_url("https://seattle.craigslist.org/search/sss?query=truck+98221")
    ou = refine_search_url("https://www.offerup.com/WA-X/search?q=truck+under+5k")
    eb = refine_search_url("https://www.ebay.com/sch/i.html?_nkw=truck", fallback_zip="98221")
    other = refine_search_url("https://example.com/search?q=truck+98221+under+5k")
    assert "postal=98221" in cl
    assert "price_max=5000" in ou and "/search" in ou
    assert "_stpos=98221" in eb
    assert other == "https://example.com/search?q=truck+98221+under+5k"  # unknown site untouched


def test_url_zip_reads_localized_urls():
    assert url_zip("https://skagit.craigslist.org/search/cta?postal=98221") == "98221"
    assert url_zip("https://www.ebay.com/sch/i.html?_stpos=98221") == "98221"
    assert url_zip("https://offerup.com/search?q=truck") is None
    assert url_zip("https://x.com/?postal=00000") is None   # fake zip rejected


# ── location self-heal from the watch instruction (0.31 — Las Vegas bug) ─────

from web_watcher.cl_geo import ensure_location, zip_from_text


def test_zip_from_text_resolves_town():
    assert zip_from_text("look for vehicles in anacortes under 10000") == "98221"
    assert zip_from_text("no location here") is None


def test_ensure_location_fixes_wrong_region_from_instruction():
    """A watch pointed at Las Vegas but asking for Anacortes must self-heal to skagit."""
    bad = "https://lasvegas.craigslist.org/search/cta?query=vehicles&max_price=10000"
    fixed = ensure_location(bad, "vehicles in anacortes for under 10000")
    host, _, q = _parts(fixed)
    assert host == "skagit.craigslist.org"
    assert q["postal"] == "98221"
    assert q["search_distance"] == "50"


def test_ensure_location_fixes_bare_craigslist():
    fixed = ensure_location("https://www.craigslist.org/search/cta?query=vehicles", "vehicles in anacortes")
    host, _, q = _parts(fixed)
    assert host == "skagit.craigslist.org" and q["postal"] == "98221"


def test_ensure_location_leaves_correct_url_alone():
    ok = "https://skagit.craigslist.org/search/cta?postal=98221&search_distance=50"
    host, _, q = _parts(ensure_location(ok, "vehicles in anacortes"))
    assert host == "skagit.craigslist.org" and q["postal"] == "98221"


def test_ensure_location_no_hint_still_refines():
    # No resolvable location in the instruction → just a plain refine, URL's own location kept.
    url = "https://seattle.craigslist.org/search/cta?query=truck"
    assert ensure_location(url, "just find trucks") == url


def test_craigslist_fallback_zip_localizes():
    from web_watcher.cl_geo import refine_craigslist_url
    fixed = refine_craigslist_url("https://lasvegas.craigslist.org/search/cta?query=vehicles",
                                  fallback_zip="98221")
    host, _, q = _parts(fixed)
    assert host == "skagit.craigslist.org" and q["postal"] == "98221"


# ── 0.33: trailing-period town extraction + wrong-zip heal + eBay condition strip ──

def test_zip_from_text_handles_trailing_period():
    # Instructions almost always end in a period; the period must not defeat the lookup.
    from web_watcher.cl_geo import zip_from_text
    assert zip_from_text("Search for any vehicles under $10,000 in Anacortes.") == "98221"
    assert zip_from_text("vehicles in Anacortes") == "98221"


def test_ensure_location_fixes_wrong_present_zip_and_region():
    # The user's real watch: seattle subdomain + a bogus/wrong postal (98210). The instruction
    # names Anacortes, so it must heal to skagit + 98221 (region AND the invalid postal).
    from web_watcher.cl_geo import ensure_location
    bad = "https://seattle.craigslist.org/search/cta?max_price=10000&postal=98210&search_distance=50"
    host, _, q = _parts(ensure_location(bad, "Search for any vehicles under $10,000 in Anacortes."))
    assert host == "skagit.craigslist.org"
    assert q["postal"] == "98221"


def test_refine_ebay_strips_new_only_condition():
    # A new-only condition filter excludes used cars and lets brand-new toys/parts flood in.
    from web_watcher.cl_geo import refine_ebay_url
    fixed = refine_ebay_url(
        "https://www.ebay.com/sch/i.html?_nkw=vehicles&_sacat=0&LH_ItemCondition=1000|1500")
    _, _, q = _parts(fixed)
    assert "LH_ItemCondition" not in q


def test_refine_ebay_keeps_used_condition():
    # A used-condition filter (3000) is exactly right — don't strip it.
    from web_watcher.cl_geo import refine_ebay_url
    fixed = refine_ebay_url(
        "https://www.ebay.com/sch/i.html?_nkw=truck&LH_ItemCondition=3000")
    _, _, q = _parts(fixed)
    assert q.get("LH_ItemCondition") == "3000"


def test_refine_ebay_routes_generic_vehicle_to_motors():
    from web_watcher.cl_geo import refine_ebay_url
    fixed = refine_ebay_url(
        "https://www.ebay.com/sch/i.html?_nkw=vehicles&_sacat=0&_dcat=9356")
    _, _, q = _parts(fixed)
    assert q.get("_sacat") == "6001"      # eBay Motors → Cars & Trucks
    assert "_dcat" not in q               # bogus dept category dropped
    assert "_nkw" not in q                # generic word folded into the category


def test_refine_ebay_keeps_specific_model_as_keyword():
    from web_watcher.cl_geo import refine_ebay_url
    fixed = refine_ebay_url("https://www.ebay.com/sch/i.html?_nkw=toyota+tacoma")
    _, _, q = _parts(fixed)
    assert q.get("_nkw") == "toyota tacoma"   # a real model keyword search works — leave it
    assert q.get("_sacat") != "6001"


# ── Out-of-area filter (the OfferUp nationwide-feed fix) ──────────────────────

def test_parse_city_state():
    from web_watcher.cl_geo import parse_city_state
    assert parse_city_state("2012 Ford F-150 $5,500 Visalia, CA") == ("Visalia", "CA")
    # The city can carry leading words (out_of_area word-trims them); the STATE is exact.
    c = parse_city_state("truck in West Covina, CA")
    assert c and c[1] == "CA" and c[0].endswith("West Covina")
    assert parse_city_state("no location here $7000") is None
    assert parse_city_state("weird, XX not a state") is None   # XX isn't a real state


def test_states_adjacent():
    from web_watcher.cl_geo import states_adjacent
    assert states_adjacent("WA", "WA") is True
    assert states_adjacent("WA", "OR") is True     # shared border
    assert states_adjacent("WA", "CA") is False
    assert states_adjacent("WA", "FL") is False


def test_out_of_area_drops_far_keeps_local():
    from web_watcher.cl_geo import out_of_area, zip_latlon, state_for_latlon
    anchor = zip_latlon("98221")           # Anacortes, WA
    ws = state_for_latlon(*anchor)
    assert ws == "WA"
    # Far (other states) → dropped.
    assert out_of_area("2007 BMW 328i $5,000 118k miles Burbank, CA", anchor, ws) is True
    assert out_of_area("Truck in Miami, FL $8000", anchor, ws) is True
    # Local / adjacent / unlocatable → kept.
    assert out_of_area("2010 Toyota Tacoma $9,000 Mount Vernon, WA", anchor, ws) is False
    assert out_of_area("Nice truck in Portland, OR $6500", anchor, ws) is False   # adjacent
    assert out_of_area("2008 Chevy Silverado clean title $7000", anchor, ws) is False  # no city


def test_out_of_area_conservative_without_anchor():
    from web_watcher.cl_geo import out_of_area
    # No anchor → never drop (we can't judge distance).
    assert out_of_area("Truck in Miami, FL", None, None) is False
