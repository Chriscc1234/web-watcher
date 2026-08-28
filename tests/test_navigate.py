"""Offline tests for the human-first navigation layer. The browser-driving primitives
(type_search / set_location) are proven LIVE (Craigslist / OfferUp); here we test the pure
hint lookup and that the module's control map is well-formed."""

from __future__ import annotations

from web_watcher import navigate as N


def test_hints_for_matches_site():
    assert N.hints_for("https://seattle.craigslist.org")["search_box"]
    assert N.hints_for("https://www.craigslist.org/area/seattle")["search_box"]
    assert "location" in N.hints_for("https://offerup.com/search?q=truck")
    assert N.hints_for("https://example.com/anything") == {}
    assert N.hints_for("") == {}


def test_craigslist_hint_covers_homepage_placeholder_box():
    # The homepage box has only a placeholder — the hint must cover it (the bug that made the
    # first live proof fail was a hint that only matched the results-page box).
    assert "placeholder" in N.hints_for("https://craigslist.org")["search_box"].lower()


def test_offerup_location_hint_is_complete():
    loc = N.hints_for("https://offerup.com/search")["location"]
    assert loc["open"] and loc["input"] and loc["confirm"]   # the mapped dialog flow


def test_control_hints_are_well_formed():
    for host, h in N.CONTROL_HINTS.items():
        assert isinstance(host, str) and isinstance(h, dict)
        if "location" in h:
            assert isinstance(h["location"], dict) and h["location"].get("open")


# ── SearchRequest: the structured intent pulled back out of a watch's URL ──────

def test_build_from_refined_craigslist_url():
    r = N.build_search_request(
        "https://skagit.craigslist.org/search/cta"
        "?postal=98221&search_distance=50&max_price=10000&query=toyota%20tacoma&sort=date")
    assert r.site == "craigslist"
    assert r.terms == "toyota tacoma"
    assert r.zip == "98221"
    assert r.radius == 50
    assert r.price_max == 10000
    assert r.sort == "date"
    assert r.category == "cta"


def test_build_from_offerup_url():
    r = N.build_search_request("https://offerup.com/search?q=truck&price_max=15000&radius=50")
    assert r.site == "offerup"
    assert r.terms == "truck"
    assert r.price_max == 15000
    assert r.radius == 50


def test_build_from_ebay_motors_url():
    r = N.build_search_request(
        "https://www.ebay.com/sch/i.html?_nkw=tacoma&_stpos=98221&_sadis=50&_udhi=10000&_sacat=6001")
    assert r.site == "ebay"
    assert r.terms == "tacoma"
    assert r.zip == "98221"
    assert r.radius == 50
    assert r.price_max == 10000


def test_build_pulls_inline_params_out_of_query_text():
    # A model that stuffed price + zip into the keyword box — the terms we'd TYPE must be clean.
    r = N.build_search_request("https://skagit.craigslist.org/search/sss?query=tacoma%20under%205k%2098221")
    assert r.terms == "tacoma"
    assert r.price_max == 5000
    assert r.zip == "98221"


def test_build_falls_back_to_instruction_for_missing_location_and_price():
    # URL carries no location/price; the watch's instruction does.
    r = N.build_search_request(
        "https://skagit.craigslist.org/search/cta?query=tacoma",
        instruction="find a toyota tacoma in anacortes under 8k")
    assert r.terms == "tacoma"
    assert r.price_max == 8000
    assert r.zip is not None            # resolved from "in anacortes"


def test_build_generic_vehicle_category_has_empty_terms():
    # A generic cars+trucks watch is 'browse this category', not a keyword search.
    r = N.build_search_request("https://skagit.craigslist.org/search/cta?postal=98221&search_distance=50")
    assert r.category == "cta"
    assert r.terms == ""
    assert r.zip == "98221"
    assert "cat=cta" in r.describe()


def test_build_tolerates_garbage_url():
    r = N.build_search_request("not a url", instruction="")
    assert isinstance(r, N.SearchRequest)   # best-effort, never raises


# ── can_fully_drive: the gate that stops us silently dropping a location/price ─

def test_can_fully_drive_craigslist_with_full_hints():
    # Craigslist hints include postal + price controls → a zip+price request is fully drivable.
    req = N.build_search_request(
        "https://skagit.craigslist.org/search/cta?postal=98221&search_distance=50&max_price=10000&query=tacoma")
    assert N.can_fully_drive(req, N.hints_for("https://skagit.craigslist.org")) is True


def test_can_fully_drive_refuses_when_location_cannot_be_driven():
    # eBay's hint is search-box only. A request WITH a zip must NOT be human-driven there (we'd
    # type the terms but drop the location) — the gate returns False so the URL path is used.
    req = N.build_search_request(
        "https://www.ebay.com/sch/i.html?_nkw=tacoma&_stpos=98221&_sadis=50")
    assert req.zip == "98221"
    assert N.can_fully_drive(req, N.hints_for("https://www.ebay.com/sch/i.html")) is False


def test_can_fully_drive_refuses_when_price_cannot_be_driven():
    req = N.SearchRequest(terms="tacoma", price_max=10000)
    assert N.can_fully_drive(req, {"search_box": "input"}) is False   # no price control in hint


def test_can_fully_drive_allows_terms_only_anywhere():
    # A pure keyword search (no location/price to lose) is drivable with just a search box.
    req = N.SearchRequest(terms="tacoma")
    assert N.can_fully_drive(req, {"search_box": "input"}) is True


def test_can_fully_drive_false_on_empty_request():
    assert N.can_fully_drive(N.SearchRequest(), {"search_box": "input"}) is False


# ── HUMAN_FIRST_SITES: only live-verified sites are actually driven ────────────

def test_human_first_enabled_sites():
    # Craigslist, OfferUp and (as of 27 Aug 2026) Facebook are live-verified and driven.
    # eBay is not: its location is a URL/sidebar concern with no picker to drive.
    assert N.is_human_first_enabled("https://skagit.craigslist.org/search/cta?query=x") is True
    assert N.is_human_first_enabled("craigslist") is True
    assert N.is_human_first_enabled("https://offerup.com/search?q=truck") is True
    assert N.is_human_first_enabled("https://www.facebook.com/marketplace") is True
    assert N.is_human_first_enabled("https://www.ebay.com/sch/i.html?_nkw=x") is False


def test_category_browse_is_drivable_on_craigslist():
    # The shape the REAL watches have: a category browse with NO keyword ("cars+trucks in
    # Anacortes under $10k"). This used to be refused (the gate required terms), so every such
    # watch fell back to goto-ing the deep parametric URL — the exact bot tell we avoid.
    req = N.build_search_request(
        "https://seattle.craigslist.org/search/cta"
        "?min_price=0&max_price=10000&postal=98210&search_distance=50",
        instruction="Search for any vehicles under $10,000 in Anacortes.")
    assert req.terms == "" and req.category == "cta"
    assert N.can_fully_drive(req, N.hints_for("https://seattle.craigslist.org")) is True


def test_category_hint_is_templated_per_code():
    # The link a person clicks carries the category code (mapped live: 'cars + trucks' -> cat=cta).
    tmpl = N.hints_for("https://seattle.craigslist.org")["category_link"]
    assert "{cat}" in tmpl
    assert "cat=cta" in tmpl.replace("{cat}", "cta")


def test_category_without_a_link_hint_is_refused():
    # A category we have no way to CLICK must fall back to the URL rather than land on the
    # homepage and quietly browse the wrong thing.
    req = N.SearchRequest(category="cta")
    assert N.can_fully_drive(req, {"search_box": "input"}) is False


def test_click_category_needs_a_template_and_code():
    assert N.click_category(object(), "cta", {}) is False          # no hint
    assert N.click_category(object(), "", {"category_link": "a"}) is False   # no category


def test_offerup_is_fully_drivable():
    # OfferUp has a location dialog + inline price hints → a zip+price request is fully drivable.
    req = N.build_search_request("https://offerup.com/search?q=truck&price_max=10000&radius=50",
                                 instruction="trucks in anacortes under 10k")
    assert req.zip                                   # localized from the instruction
    assert N.can_fully_drive(req, N.hints_for("https://offerup.com/search")) is True


# ── category must never be silently dropped (the golf-club bug) ───────────────────
# A live sweep drove the search box for a "MacGregor sailboat" watch in cat=boo (boats) and
# landed on cat=sss (ALL for sale), because apply_search_request treated category and keyword as
# either/or and can_fully_drive only demanded a category link when there was NO keyword. Searching
# all of craigslist for "macgregor" is how a SAILBOAT watch filled with MacGregor GOLF CLUBS.

def test_a_keyword_watch_with_a_category_needs_the_category_link():
    from web_watcher import navigate as N
    url = ("https://skagit.craigslist.org/search/boo?query=MacGregor+sailboat"
           "&search_distance=300&postal=98221")
    req = N.build_search_request(url, "MacGregor sailboats within 300 miles of Anacortes")
    assert req.category == "boo" and req.terms          # both present — the bug's precondition
    hint = N.hints_for(url)
    assert N.can_fully_drive(req, hint) is True         # craigslist can click the category
    # Strip the ability to click the category: driving must now be REFUSED, not silently widened.
    crippled = {k: v for k, v in hint.items() if k != "category_link"}
    assert N.can_fully_drive(req, crippled) is False


def test_category_and_keyword_are_both_applied_not_either_or():
    """Category is clicked AND the keyword typed — category first, the way a person narrows to
    Boats and then searches within it."""
    from web_watcher import navigate as N

    calls = []
    # The REAL request object, built from a real URL — a hand-rolled stub drifts from the schema.
    req = N.build_search_request(
        "https://skagit.craigslist.org/search/boo?query=MacGregor+sailboat", "MacGregor sailboat")
    assert req.category == "boo" and req.terms

    def _fake_click(page, category, hint):
        calls.append(("category", category)); return True

    def _fake_type(page, terms, hint):
        calls.append(("search", terms)); return True

    orig_click, orig_type = N.click_category, N.type_search
    try:
        N.click_category, N.type_search = _fake_click, _fake_type
        applied = N.apply_search_request(object(), req, {"category_link": "a[href*='cat={cat}']"})
    finally:
        N.click_category, N.type_search = orig_click, orig_type

    assert applied["categorized"] is True and applied["searched"] is True
    assert [c[0] for c in calls] == ["category", "search"]   # category FIRST


def test_category_extracts_from_both_url_shapes_including_for_sale():
    """The classic path shape (/search/boo) and the modern param shape (?cat=sss — what the hub's
    own links and human-first landings produce) must BOTH yield the category. A watch that says
    'look in for sale' (sss) or any other section drives that section, not the default."""
    from web_watcher import navigate as N
    assert N.build_search_request(
        "https://skagit.craigslist.org/search/boo?query=x", "x").category == "boo"
    assert N.build_search_request(
        "https://www.craigslist.org/search/area/skagit?cat=sss&query=x", "x").category == "sss"
    assert N.build_search_request(
        "https://www.craigslist.org/search/city/anacortes-wa?query=x", "x").category is None
    # 'for sale' is clickable on the hub (139 category links incl. cat=sss, verified live
    # 26 Aug 2026), so a for-sale watch passes the drive gate like any other category.
    req = N.build_search_request(
        "https://www.craigslist.org/search/area/skagit?cat=sss&query=macgregor", "x")
    assert N.can_fully_drive(req, N.hints_for("https://craigslist.org")) is True


# ---------------------------------------------------------------------------
# Facebook Marketplace controls (mapped live 27 Aug 2026, not guessed)
# ---------------------------------------------------------------------------

def test_facebook_hints_exist_and_are_shaped_right():
    from web_watcher.navigate import CONTROL_HINTS
    fb = CONTROL_HINTS.get("facebook.com")
    assert fb, "Facebook Marketplace controls are not mapped"
    loc = fb["location"]
    for key in ("open", "dialog", "input", "confirm"):
        assert loc.get(key), f"location hint missing {key!r}"
    assert loc["confirm"] == "Apply"


def test_facebook_search_box_targets_marketplace_not_global():
    """The live page has TWO type=search inputs: 'Search Facebook' and 'Search Marketplace'.
    A generic input[type=search] matches the GLOBAL one first, which would search all of
    Facebook for the product terms. The aria-label is the only discriminator."""
    from web_watcher.navigate import CONTROL_HINTS
    sel = CONTROL_HINTS["facebook.com"]["search_box"].lower()
    assert "search marketplace" in sel
    assert "type=\"search\"" not in sel and "type='search'" not in sel


def test_facebook_is_human_first_after_live_verification():
    """Graduated only after the flow was DRIVEN, not merely mapped: the picker moved
    Anacortes -> Bellingham -> Anacortes with the site's own marker confirming each change."""
    from web_watcher.navigate import HUMAN_FIRST_SITES, is_human_first_enabled
    assert "facebook" in HUMAN_FIRST_SITES
    assert is_human_first_enabled("https://www.facebook.com/marketplace/")


def test_already_correct_location_is_success_not_failure():
    """set_location returned False when the site was ALREADY showing the target area, so a
    caller treated a perfectly good state as a failure and fell back to a URL. Measured live:
    marker read 'Location: Anacortes...', the watch wanted Anacortes, result was False."""
    import inspect
    from web_watcher import navigate
    src = inspect.getsource(navigate.set_location)
    assert "already set to" in src, "the already-there short-circuit is gone"


def test_click_button_by_label_has_no_undefined_name():
    """It called _human_click(page, …) with only `scope` in scope — a NameError swallowed by
    its own bare except, so the confirm step of set_location never clicked anything."""
    import ast, inspect
    from web_watcher import navigate
    tree = ast.parse(inspect.getsource(navigate._click_button_by_label))
    fn = tree.body[0]
    params = {a.arg for a in fn.args.args}
    used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    undefined = used - params - set(dir(navigate)) - set(dir(__builtins__))
    assert "page" not in undefined, "still references an undefined 'page'"


def test_location_marker_sees_a_div_role_button():
    """Facebook's location control is a div[role=button], not a <button>. A button-only
    selector found nothing, so a location that HAD changed reported unchanged."""
    import inspect
    from web_watcher import navigate
    src = inspect.getsource(navigate._location_marker)
    assert "role=button" in src
