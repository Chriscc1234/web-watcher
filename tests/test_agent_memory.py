"""The agent's memory of its OWN actions — "have I already pushed this button, and what did it do?"

Element indices are positional: the snapshot re-sorts by viewport + y and reassigns 0..N on EVERY
step, so they shift with every scroll. The action history used to render an old action's index
against the CURRENT element list, which produced a FABRICATED memory — "step 3: click [12]
'condition'" for a click that actually hit 'boat type'. Being lied to about what it had already
tried, the agent re-clicked the same dead craigslist sort controls over and over (933 scrolls and
141 stuck-events in one real session).

Two guarantees are tested here:
  1. The label is captured AT ACTION TIME and never re-resolved (AgentAction.element_label).
  2. _tried_ledger gives a whole-run, per-CONTROL view that outlives the 10-step history window,
     and calls out controls that did nothing.

See agent.py."""

from __future__ import annotations

from web_watcher.agent import AgentAction, _tried_ledger


def _click(label, outcome="page unchanged", idx=12):
    return AgentAction(thought="", action="click", element_index=idx,
                       element_label=label, outcome=outcome)


# ── the ledger ────────────────────────────────────────────────────────────────────

def test_dead_control_is_called_out_as_pointless():
    led = _tried_ledger([_click("boat type"), _click("condition")])
    assert "boat type" in led and "condition" in led
    assert "NOTHING CHANGED" in led
    assert "do not click it again" in led.lower() or "do NOT try these again" in led


def test_repeat_count_is_shown():
    # The real failure: the same control clicked several times across a run.
    led = _tried_ledger([_click("boat type"), _click("boat type"), _click("boat type")])
    assert "3" in led and "boat type" in led


def test_a_control_that_worked_keeps_its_real_outcome():
    led = _tried_ledger([AgentAction(thought="", action="type", element_index=0,
                                     element_label="search craigslist", text="MacGregor",
                                     outcome="navigated -> results page")])
    assert "navigated -> results page" in led
    assert "NOTHING CHANGED" not in led


def test_a_real_effect_wins_over_page_unchanged_for_the_same_control():
    # Same control, two outcomes: the informative one is what the agent needs to remember.
    led = _tried_ledger([_click("sort", outcome="page unchanged"),
                         _click("sort", outcome="navigated -> sorted by price")])
    assert "navigated -> sorted by price" in led
    assert "NOTHING CHANGED" not in led


def test_scrolls_and_unlabelled_actions_are_not_in_the_ledger():
    # Scroll isn't a control you can "already have pushed", and an action with no captured label
    # can't be described truthfully — better absent than guessed.
    led = _tried_ledger([
        AgentAction(thought="", action="scroll", direction="down"),
        AgentAction(thought="", action="click", element_index=5, element_label=None,
                    outcome="page unchanged"),
    ])
    assert led == ""


def test_empty_history_yields_no_block():
    assert _tried_ledger([]) == ""


def test_ledger_is_capped_so_it_cannot_swamp_the_prompt():
    led = _tried_ledger([_click(f"control {i}") for i in range(40)])
    assert len([ln for ln in led.splitlines() if ln.strip().startswith("- ")]) <= 18


# ── the label is captured, not re-resolved ────────────────────────────────────────

def test_action_carries_the_label_it_was_chosen_against():
    # The regression: an action must remember the label from the page it acted on, so that a later
    # step (after scrolling shuffled every index) cannot rewrite what it thinks it clicked.
    a = _click("boat type", idx=12)
    b = _click("condition", idx=12)          # SAME index, different control, one scroll later
    assert a.element_label == "boat type"
    assert b.element_label == "condition"
    led = _tried_ledger([a, b])
    assert "boat type" in led and "condition" in led


def test_default_label_is_none_for_a_plain_action():
    assert AgentAction(thought="", action="scroll").element_label is None


# ── the site tree: knowledge that outlives a single sweep ─────────────────────────

def test_page_kind_branches_the_tree():
    from web_watcher.agent import page_kind
    assert page_kind("https://skagit.craigslist.org/search/boo?query=x") == "search"
    assert page_kind("https://skagit.craigslist.org/view/d/a-boat/12345678.html") == "listing"
    assert page_kind("https://skagit.craigslist.org/") == "home"


def test_learned_controls_shapes_a_tree_by_page_kind():
    from web_watcher.agent import learned_controls
    h = [_click("sort"), _click("reply", outcome="opened the reply form")]
    tree = learned_controls(h, {0: "search", 1: "listing"})
    assert tree["search"]["sort"]["dead"] is True          # no-op on results
    assert tree["listing"]["reply"]["dead"] is False       # did something on an ad
    assert "reply" not in tree["search"]                   # kinds stay separate


def test_prior_knowledge_is_recalled_with_no_history_at_all():
    # The whole point: a fresh sweep starts already knowing what the last one proved.
    prior = {"search": {"boat type": {"outcome": "page unchanged", "n": 3, "dead": True}}}
    led = _tried_ledger([], prior, "search")
    assert "boat type" in led and "NOTHING CHANGED" in led
    assert "earlier visit" in led


def test_prior_knowledge_for_another_page_kind_is_not_shown():
    prior = {"listing": {"reply": {"outcome": "opened form", "n": 1, "dead": False}}}
    assert _tried_ledger([], prior, "search") == ""


def test_live_observation_overrides_a_stale_memory():
    # Remembered as dead, but it worked just now → the live page wins and the tag is dropped.
    prior = {"search": {"sort": {"outcome": "page unchanged", "n": 5, "dead": True}}}
    led = _tried_ledger([_click("sort", outcome="navigated -> sorted by price")], prior, "search")
    assert "navigated -> sorted by price" in led
    assert "earlier visit" not in led


# ── the search lock ───────────────────────────────────────────────────────────────

def test_search_lock_refuses_an_invented_search_url():
    from web_watcher.agent import _search_lock_violation
    # The exact real-world failure: a precise regional query replaced by a made-up one.
    assert _search_lock_violation(
        "https://skagit.craigslist.org/search/boo?query=MacGregor&postal=98221",
        "https://www.craigslist.org/search/city/anacortes-wa?cat=boo") is True


def test_search_lock_allows_opening_a_listing_and_same_page_anchors():
    from web_watcher.agent import _search_lock_violation
    cur = "https://skagit.craigslist.org/search/boo?query=MacGregor"
    assert _search_lock_violation(cur, "https://skagit.craigslist.org/view/d/x/12345678.html") is False
    assert _search_lock_violation(cur, cur + "#search=2~list~0") is False
    assert _search_lock_violation(cur, "") is False


# ── the context window that caused the amnesia in the first place ─────────────────

def test_agent_context_window_is_set_and_generous():
    # Unset meant Ollama's 4096 default, which silently truncates the SYSTEM prompt away.
    from web_watcher.agent import _AGENT_NUM_CTX, _HISTORY_STEPS
    assert _AGENT_NUM_CTX >= 16_384
    assert _HISTORY_STEPS >= 25


# ── invented filter values (the silent-narrowing bug) ─────────────────────────────
# Live: on a watch with NO budget the agent typed "13000" into OfferUp's max-price box,
# hiding every boat above it. A value the goal never stated silently shrinks the user's search.

def test_a_price_typed_into_a_no_budget_watch_is_rejected():
    from web_watcher.agent import _invented_filter_value as f
    no_budget = "MacGregor sailboats of any model within 300 miles of Anacortes, no price limit."
    assert f("max", "13000", no_budget)          # the exact live failure
    assert f("min", "0", no_budget)
    assert f("year", "2010", no_budget)


def test_a_filter_the_goal_actually_states_is_allowed():
    from web_watcher.agent import _invented_filter_value as f
    with_budget = "manual transmission cars within 100 miles, under $8000"
    assert not f("max price", "8000", with_budget)
    # A radius the goal states is legitimate even on a no-budget watch.
    assert not f("miles", "300", "MacGregor sailboats within a 300-mile radius of Anacortes")


def test_keyword_text_is_never_treated_as_an_invented_filter():
    from web_watcher.agent import _invented_filter_value as f
    assert not f("search for sale", "macgregor sailboat", "anything")
    assert not f("zip", "98221", "boats near Anacortes")     # not a guarded numeric filter


# ── eBay search pages are not listings (leaky search lock) ────────────────────────

def test_an_ebay_results_page_is_a_search_not_a_listing():
    from web_watcher.agent import is_listing_url, page_kind, _search_lock_violation
    results = "https://www.ebay.com/sch/i.html?_nkw=macgregor+sailboat"
    assert is_listing_url(results) is False      # `.html` alone used to make this a "listing"
    assert page_kind(results) == "search"
    assert is_listing_url("https://www.ebay.com/itm/123456789012") is True
    # ...so the lock actually holds on eBay: another search is refused, an item is allowed.
    assert _search_lock_violation(results, "https://www.ebay.com/sch/i.html?_nkw=golf") is True
    assert _search_lock_violation(results, "https://www.ebay.com/itm/999999999999") is False


def test_offerup_uuid_listings_are_extracted():
    # OfferUp ids are UUIDs, not digits, so the digits-only generic pattern rejected every card —
    # the harvest returned 0 while vision could plainly see listings on the page.
    from web_watcher.monitor import _listing_key
    key = _listing_key("https://offerup.com/item/detail/f9c730b0-02c9-3382-a7b3-5271d033fb79")
    assert key == "offerup:f9c730b0-02c9-3382-a7b3-5271d033fb79"
    assert _listing_key("https://offerup.com/search?q=sailboat") is None


# ── the setup budget: stop re-clicking sort/filter, scroll to harvest ─────────────
# Reproduced live on BOTH craigslist and Facebook: a harvest sweep re-clicked sort controls
# 7-8 times, KNOWING it had already sorted ("I have already changed the sorting to 'oldest',
# but need to..."), instead of scrolling to load listings. The agent has no phase model; the
# budget is the deterministic backstop.

def test_setup_budget_limits_are_sane():
    from web_watcher.agent import _SETUP_SOFT_LIMIT, _SETUP_HARD_LIMIT
    # A dropdown legitimately costs two clicks (open, choose), so the soft limit must exceed 2
    # or it would fire mid-dropdown; the hard limit must be above the soft one.
    assert _SETUP_SOFT_LIMIT >= 3
    assert _SETUP_HARD_LIMIT > _SETUP_SOFT_LIMIT


# ── the Vehicles-sidebar click: a feed swap gets detected ───────────────────────

def test_abandoned_search_feed_detects_the_category_swap():
    """The live case, verbatim URLs: el=26 ("Vehicles") turned the MacGregor search into a
    feed of sedans — 70 banked, 60 judged, all junk."""
    from web_watcher.agent import _abandoned_search_feed
    pre = "https://www.facebook.com/marketplace/seattle/search?query=macgregor%20sailboat&exact=false"
    post = ("https://www.facebook.com/marketplace/seattle/search/"
            "?category_id=546583916084032&query=Vehicles&referral_ui_component=category_menu_item")
    assert _abandoned_search_feed(pre, post) is True


def test_abandoned_search_feed_allows_legitimate_moves():
    from web_watcher.agent import _abandoned_search_feed
    pre = "https://www.facebook.com/marketplace/seattle/search?query=macgregor%20sailboat"
    for post in (
        pre,                                                          # unchanged
        "https://www.facebook.com/marketplace/item/123456/",          # opening a listing
        pre + "&sortBy=creation_time_descend",                        # re-sorting same query
        "https://www.facebook.com/marketplace/seattle/search?query=macgregor%2026",  # refined, term kept
    ):
        assert _abandoned_search_feed(pre, post) is False, post
    # A category-only sweep has no terms to abandon.
    assert _abandoned_search_feed(
        "https://tacoma.craigslist.org/search/cta",
        "https://tacoma.craigslist.org/search/boa") is False
