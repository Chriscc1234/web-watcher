"""
Tests for _normalize_turn — the repair layer that keeps the local model's shape mistakes
from leaking into The Watcher's chat as "garbled gook". Pure/offline: no Ollama, no DB.

The bug these guard against: qwen sometimes returns a BARE watch object (no
{"message", "watch_suggestion"} envelope), and the old code fell back to dumping the raw
JSON string into the chat bubble. It also emitted watch_suggestion_2/_3 for multi-watch
updates, which the UI silently dropped.
"""

from __future__ import annotations

from web_watcher.dashboard.server import _normalize_turn, _watch_search_terms


def test_bare_watch_object_becomes_a_suggestion_with_a_message():
    # exactly the shape seen in watcher_history.json that rendered as raw JSON
    bare = {"action": "update", "name": "Manual Sports Cars (Seattle)",
            "urls": ["https://x?query=miata"], "mode": "continuous", "judgment_prompt": "x"}
    out = _normalize_turn(bare)
    assert out.get("message")                       # never empty / never the raw object
    assert "action" not in out                      # the bare keys aren't leaked at top level
    assert out["watch_suggestion"]["name"] == "Manual Sports Cars (Seattle)"
    assert out["watch_suggestions"] == [bare]


def test_multiple_suggestions_are_collected():
    data = {"message": "both", "watch_suggestion": {"name": "A"},
            "watch_suggestion_2": {"name": "B"}}
    out = _normalize_turn(data)
    assert [s["name"] for s in out["watch_suggestions"]] == ["A", "B"]
    assert out["watch_suggestion"]["name"] == "A"   # first stays primary (back-compat)


def test_missing_message_is_synthesized_not_raw():
    out = _normalize_turn({"watch_suggestion": {"name": "Trucks", "action": "update"}})
    assert out["message"] and "{" not in out["message"]


def test_plain_message_is_untouched():
    out = _normalize_turn({"message": "hi", "listing_query": None})
    assert out["message"] == "hi"
    assert "watch_suggestions" not in out          # nothing invented when there's no watch


def test_non_dict_never_crashes():
    out = _normalize_turn(["unexpected"])
    assert isinstance(out.get("message"), str)


# ---------------------------------------------------------------------------
# _watch_search_terms — decode the actual terms out of a watch's URLs so The Watcher
# can answer "what are our search terms?" from plain text instead of misreading URLs.
# ---------------------------------------------------------------------------

def test_watch_search_terms_decodes_and_dedups():
    from web_watcher.config import Watch
    w = Watch.model_validate({
        "name": "cars", "mode": "continuous", "instruction": "x",
        "urls": [
            "https://seattle.craigslist.org/search/cta?query=Miata",
            "https://seattle.craigslist.org/search/cta?query=Mustang+GT&sort=date",
            "https://seattle.craigslist.org/search/cta?query=Miata",   # dup → collapsed
        ],
    })
    assert _watch_search_terms(w) == ["Miata", "Mustang GT"]


def test_watch_search_terms_empty_for_non_search_url():
    from web_watcher.config import Watch
    w = Watch.model_validate({
        "name": "feed", "mode": "continuous", "instruction": "x",
        "urls": ["https://www.facebook.com/marketplace/"],
    })
    assert _watch_search_terms(w) == []


def test_normalize_marketplace_urls_strips_bogus_city_subdomain():
    from web_watcher.dashboard.server import _normalize_marketplace_urls
    urls = [
        "https://seattle.offerup.com/search?q=diesel+truck",   # bogus city subdomain
        "https://offerup.com/search?q=kayak",                   # already correct
        "https://seattle.craigslist.org/search/cta?query=truck",# craigslist DOES use cities
        "https://www.ebay.com/sch/i.html?_nkw=rtx",             # www is fine
        "https://boston.ebay.com/sch/i.html?_nkw=gpu",          # bogus city on ebay
    ]
    out, changes = _normalize_marketplace_urls(urls)
    assert out[0] == "https://offerup.com/search?q=diesel+truck"
    assert out[1] == "https://offerup.com/search?q=kayak"
    assert out[2] == "https://seattle.craigslist.org/search/cta?query=truck"  # untouched
    assert out[3] == "https://www.ebay.com/sch/i.html?_nkw=rtx"               # untouched
    assert out[4] == "https://www.ebay.com/sch/i.html?_nkw=gpu"  # canonical search host
    assert len(changes) == 2   # offerup + ebay city subdomains fixed


def _mock_two_phase(monkeypatch, phase1_text, phase2_obj):
    """Mock Ollama for the two-phase turn: phase 1 (no 'format') returns natural prose,
    phase 2 ('format'=='json') returns the extraction object."""
    import json as _json
    from web_watcher.dashboard import server as S

    def _content_for(payload):
        if payload.get("format") == "json":
            return _json.dumps(phase2_obj)
        return phase1_text

    class _R:
        def __init__(self, payload): self._p = payload
        def raise_for_status(self): pass
        def json(self):
            return {"message": {"content": _content_for(self._p)},
                    "eval_count": 1, "prompt_eval_count": 1, "eval_duration": 1}
    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **k): return _R(k.get("json", {}))
    monkeypatch.setattr(S.httpx, "Client", _C)


def test_asking_a_question_holds_create_suggestion(monkeypatch):
    import types
    from web_watcher.dashboard import server as S
    cfg = types.SimpleNamespace(watches=[])

    # Phase 1 asks a question; even if phase 2 proposes a create, the "?" guard holds it back.
    _mock_two_phase(monkeypatch,
                    "Do you want trucks or SUVs, and what's your budget?",
                    {"intent": "create",
                     "watch": {"name": "X",
                               "urls": ["https://seattle.craigslist.org/search/cta?query=truck"],
                               "instruction": "i", "mode": "schedule", "interval_minutes": 30}})
    out = S._complete_assistant_turn("sys", [{"role": "user", "content": "hi"}], cfg, "m")
    assert out["message"].endswith("?")
    assert out["watch_suggestion"] is None

    # A confident, non-question turn ships the watch that phase 2 extracts.
    _mock_two_phase(monkeypatch,
                    "Setting that up now.",
                    {"intent": "create",
                     "watch": {"name": "X",
                               "urls": ["https://seattle.craigslist.org/search/cta?query=truck"],
                               "instruction": "i", "mode": "schedule", "interval_minutes": 30}})
    out2 = S._complete_assistant_turn("sys", [{"role": "user", "content": "hi"}], cfg, "m")
    assert out2["message"] == "Setting that up now."
    assert out2["watch_suggestion"] is not None
    assert out2["watch_suggestion"]["action"] == "create"


def test_long_history_is_capped_before_the_model(monkeypatch):
    """The chat UI never clears, so the client sends the whole transcript every turn. The turn
    must only feed the RECENT tail to the model, not hundreds of stale messages."""
    import types
    from web_watcher.dashboard import server as S

    seen = {}
    def _fake_reply(system, messages, model):
        seen["n"] = len(messages)
        return ("ok", 1, 1, 1)
    monkeypatch.setattr(S, "_chat_reply_natural", _fake_reply)
    monkeypatch.setattr(S, "_extract_watch_action", lambda *a, **k: {})

    cfg = types.SimpleNamespace(watches=[])
    huge = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(200)]
    S._complete_assistant_turn("sys", huge, cfg, "m")
    assert seen["n"] <= S._CHAT_CONTEXT_MESSAGES        # only the recent window reached the model


def test_prompts_guard_against_example_item_leakage():
    """Regression: a tester's fresh install hallucinated a 'Miata' watch he never asked for —
    the word was an EXAMPLE inside the system prompt, and small models (qwen2.5:3b) copy prompt
    examples into their answers. Reproduced live: old prompt leaked in 2/6 neutral chats, fixed
    prompt 0/6. Keep concrete items out of the commit example, and keep the explicit rule that
    examples are not requests in BOTH phases."""
    from web_watcher.dashboard import server as S
    # The phase-1 commit example must not name a real, wantable item.
    assert "Miata" not in S._CONVERSE_OVERRIDE
    # Both phases must carry the anti-leak rule.
    assert "EXAMPLES ARE NOT REQUESTS" in S._CONVERSE_OVERRIDE
    assert "EXAMPLES ARE NOT REQUESTS" in S._EXTRACT_SYSTEM


def test_urlless_create_is_suppressed(monkeypatch):
    """Regression (the 'Anacortes clam digger' bug): the model sometimes proposes a create with
    an empty urls list for a request it couldn't turn into a real page. A watch with no URL can't
    monitor anything, so the card must be dropped (the assistant asks for the link instead)."""
    import types
    from web_watcher.dashboard import server as S
    cfg = types.SimpleNamespace(watches=[])
    _mock_two_phase(monkeypatch,
                    "Sure — what's the web address of the page you want me to check?",
                    {"intent": "create",
                     "watch": {"name": "Anacortes Clam Digger", "urls": [],
                               "instruction": "clam digging status", "mode": "schedule",
                               "interval_minutes": 60}})
    out = S._complete_assistant_turn(
        "sys", [{"role": "user", "content": "watch the anacortes clam digger every week"}], cfg, "m")
    assert out["watch_suggestion"] is None          # no URL -> no card
    assert "?" in out["message"]                     # …it asked for the link instead


def test_committed_create_with_trailing_question_still_ships(monkeypatch):
    """Regression (the Miata bug): the user clearly asked to set up a watch and gave details;
    the assistant commits AND tacks on an optional question. The create card must still ship —
    the '?' guard only holds BACK genuine clarifying questions, not a committed create."""
    import types
    from web_watcher.dashboard import server as S
    cfg = types.SimpleNamespace(watches=[types.SimpleNamespace(name="Trucks (Craigslist)")])
    _mock_two_phase(monkeypatch,
                    "Sure — setting up a Miata watch on Craigslist under $8k. "
                    "Want me to also check OfferUp?",
                    {"intent": "create",
                     "watch": {"name": "Miata (Craigslist)",
                               "urls": ["https://seattle.craigslist.org/search/cta?query=miata"],
                               "instruction": "Mazda Miata under $8000", "mode": "schedule",
                               "interval_minutes": 30}})
    out = S._complete_assistant_turn(
        "sys", [{"role": "user", "content": "set up a miata watch under 8k on craigslist"}],
        cfg, "m")
    assert "?" in out["message"]                       # it did ask an optional follow-up…
    assert out["watch_suggestion"] is not None         # …but the watch still shipped
    assert out["watch_suggestion"]["action"] == "create"
    assert out["watch_suggestion"]["name"] == "Miata (Craigslist)"


def test_create_with_existing_watches_stays_a_create(monkeypatch):
    """Regression: a NEW-watch request must produce a 'create' even when other watches exist —
    it must not be snapped onto an existing watch. Guards the plumbing; the create-vs-update
    decision itself lives in the extraction prompt (verified live)."""
    import types
    from web_watcher.dashboard import server as S
    cfg = types.SimpleNamespace(watches=[
        types.SimpleNamespace(name="Diesel Vehicles (OfferUp)"),
        types.SimpleNamespace(name="Manual Sports Cars (Seattle)"),
    ])
    _mock_two_phase(monkeypatch,
                    "Sure — I'll set up a new watch for canoes.",
                    {"intent": "create",
                     "watch": {"name": "Canoes (Craigslist)",
                               "urls": ["https://seattle.craigslist.org/search/boa?query=canoe"],
                               "instruction": "used canoes", "mode": "schedule",
                               "interval_minutes": 30}})
    out = S._complete_assistant_turn(
        "sys", [{"role": "user", "content": "also watch craigslist for a canoe"}], cfg, "m")
    assert out["watch_suggestion"] is not None
    assert out["watch_suggestion"]["action"] == "create"
    assert out["watch_suggestion"]["name"] == "Canoes (Craigslist)"   # NOT an existing name


def test_intent_none_produces_no_card(monkeypatch):
    """Plain conversation (no concrete action) → prose reply, no watch card."""
    import types
    from web_watcher.dashboard import server as S
    cfg = types.SimpleNamespace(watches=[])
    _mock_two_phase(monkeypatch, "Sure, happy to help — what would you like to watch?",
                    {"intent": "none", "watch": None})
    out = S._complete_assistant_turn("sys", [{"role": "user", "content": "hi"}], cfg, "m")
    assert out["watch_suggestion"] is None
    assert "watch" in out["message"].lower()


def test_spurious_edit_cards_are_dropped(monkeypatch):
    """The reported bug: a plain, non-editing message makes the small extract model propose
    'update' actions for existing watches the user never mentioned → unwanted edit cards. The
    deterministic grounding guard drops them (the user's message asked for no change)."""
    import types
    from web_watcher.dashboard import server as S
    cfg = types.SimpleNamespace(watches=[
        types.SimpleNamespace(name="Anacortes Vehicles (Craigslist)",
                              urls=["https://skagit.craigslist.org/search/cta?query=truck"]),
        types.SimpleNamespace(name="Flame Resistant Pants Restock",
                              urls=["https://thriveworkwear.com/products/pants"]),
    ])
    # Phase 1 just chats; phase 2 (over-eagerly) proposes edits to BOTH watches.
    _mock_two_phase(monkeypatch,
                    "Both watches are running and looking good!",
                    {"intent": "update", "watches": [
                        {"action": "update", "name": "Anacortes Vehicles (Craigslist)",
                         "urls": ["https://skagit.craigslist.org/search/cta?query=truck"],
                         "instruction": "trucks"},
                        {"action": "update", "name": "Flame Resistant Pants Restock",
                         "urls": ["https://thriveworkwear.com/products/pants"],
                         "instruction": "pants"}]})
    out = S._complete_assistant_turn(
        "sys", [{"role": "user", "content": "how are my watches doing?"}], cfg, "m")
    assert out["watch_suggestion"] is None            # neither spurious edit card survives
    assert not out["watch_suggestions"]


def test_named_edit_request_still_produces_a_card(monkeypatch):
    """A REAL edit — the user names the watch and asks for a change — must still ship its card."""
    import types
    from web_watcher.dashboard import server as S
    cfg = types.SimpleNamespace(watches=[
        types.SimpleNamespace(name="Trucks (Craigslist)",
                              urls=["https://seattle.craigslist.org/search/cta?query=truck"]),
    ])
    _mock_two_phase(monkeypatch,
                    "Sure — adding OfferUp to your trucks watch.",
                    {"intent": "update", "watch": {
                        "action": "update", "name": "Trucks (Craigslist)",
                        "urls": ["https://seattle.craigslist.org/search/cta?query=truck",
                                 "https://offerup.com/search?q=truck"],
                        "instruction": "trucks"}})
    out = S._complete_assistant_turn(
        "sys", [{"role": "user", "content": "also add offerup to the trucks watch"}], cfg, "m")
    assert out["watch_suggestion"] is not None
    assert out["watch_suggestion"]["action"] == "update"
    assert out["watch_suggestion"]["name"] == "Trucks (Craigslist)"


def test_pronoun_edit_with_focus_from_history_ships(monkeypatch):
    """'cap it at 5k' is a real edit when earlier turns established which watch 'it' is."""
    import types
    from web_watcher.dashboard import server as S
    cfg = types.SimpleNamespace(watches=[
        types.SimpleNamespace(name="Trucks (Craigslist)",
                              urls=["https://seattle.craigslist.org/search/cta?query=truck"]),
    ])
    _mock_two_phase(monkeypatch,
                    "Done — capping that watch at $5,000.",
                    {"intent": "update", "watch": {
                        "action": "update", "name": "Trucks (Craigslist)",
                        "urls": ["https://seattle.craigslist.org/search/cta?query=truck&max_price=5000"],
                        "instruction": "trucks under 5k"}})
    msgs = [
        {"role": "user", "content": "how's the trucks watch doing?"},
        {"role": "assistant", "content": "It's found a few."},
        {"role": "user", "content": "cap it at 5k"},
    ]
    out = S._complete_assistant_turn("sys", msgs, cfg, "m")
    assert out["watch_suggestion"] is not None
    assert out["watch_suggestion"]["action"] == "update"


def test_spurious_delete_action_is_dropped(monkeypatch):
    """Same bug class as the edit cards, scarier: the extractor must not surface a 'delete'
    action card when the user never asked to delete anything."""
    import types
    from web_watcher.dashboard import server as S
    cfg = types.SimpleNamespace(watches=[types.SimpleNamespace(name="Trucks (Craigslist)")])
    _mock_two_phase(monkeypatch,
                    "Your trucks watch is running well!",
                    {"intent": "actions",
                     "watch_actions": [{"action": "delete", "name": "Trucks (Craigslist)"}]})
    out = S._complete_assistant_turn(
        "sys", [{"role": "user", "content": "how's the trucks watch going?"}], cfg, "m")
    assert not out["watch_actions"]                   # no unasked-for delete card


def test_real_delete_action_survives(monkeypatch):
    """A genuine delete request (the user says 'delete') still produces the action card."""
    import types
    from web_watcher.dashboard import server as S
    cfg = types.SimpleNamespace(watches=[types.SimpleNamespace(name="Trucks (Craigslist)")])
    _mock_two_phase(monkeypatch,
                    "Okay, deleting the trucks watch.",
                    {"intent": "actions",
                     "watch_actions": [{"action": "delete", "name": "Trucks (Craigslist)"}]})
    out = S._complete_assistant_turn(
        "sys", [{"role": "user", "content": "delete the trucks watch"}], cfg, "m")
    assert out["watch_actions"] == [{"action": "delete", "name": "Trucks (Craigslist)"}]


def test_focused_watch_name_tracks_last_mentioned():
    import types
    from web_watcher.dashboard.server import _focused_watch_name
    cfg = types.SimpleNamespace(watches=[
        types.SimpleNamespace(name="Diesel Vehicles (OfferUp)"),
        types.SimpleNamespace(name="Manual Sports Cars (Seattle)"),
    ])
    msgs = [
        {"role": "user", "content": "how's the diesel vehicles (offerup) watch?"},
        {"role": "assistant", "content": "It's found 3 so far."},
        {"role": "user", "content": "change something else on it"},
    ]
    # "it" refers to the last-named existing watch.
    assert _focused_watch_name(msgs, cfg) == "Diesel Vehicles (OfferUp)"


def test_focus_ignores_site_name_collision():
    """'also look on offer up' must NOT resolve to a watch merely NAMED '(OfferUp)';
    focus stays on the sports-car watch just discussed."""
    import types
    from web_watcher.dashboard.server import _focused_watch_name
    cfg = types.SimpleNamespace(watches=[
        types.SimpleNamespace(name="Diesel Vehicles (OfferUp)"),
        types.SimpleNamespace(name="Manual Sports Cars (Seattle)"),
    ])
    msgs = [
        {"role": "user", "content": "expand the sports car watch"},
        {"role": "assistant", "content": "Sure, I'll broaden the Manual Sports Cars watch."},
        {"role": "user", "content": "also look on offer up as well"},
    ]
    # Newest message has no watch tokens → falls back to the sports-car mention, NOT diesel.
    assert _focused_watch_name(msgs, cfg) == "Manual Sports Cars (Seattle)"


def test_focus_matches_paraphrase_tokens():
    import types
    from web_watcher.dashboard.server import _focused_watch_name
    cfg = types.SimpleNamespace(watches=[
        types.SimpleNamespace(name="Manual Sports Cars (Seattle)"),
        types.SimpleNamespace(name="Refrigerators under $800 (up to 36\" wide)"),
    ])
    assert _focused_watch_name(
        [{"role": "user", "content": "how's my sports cars watch?"}], cfg
    ) == "Manual Sports Cars (Seattle)"


def test_resolve_watch_name_tolerates_dropped_site_suffix():
    """The model naming a watch 'Diesel Vehicles' must resolve to 'Diesel Vehicles (OfferUp)'
    so an update lands instead of 404ing."""
    import types
    from web_watcher.dashboard.server import _resolve_watch_name
    cfg = types.SimpleNamespace(watches=[
        types.SimpleNamespace(name="Diesel Vehicles (OfferUp)"),
        types.SimpleNamespace(name="Manual Sports Cars (Seattle)"),
    ])
    assert _resolve_watch_name("Diesel Vehicles", cfg) == "Diesel Vehicles (OfferUp)"
    assert _resolve_watch_name("diesel vehicles (offerup)", cfg) == "Diesel Vehicles (OfferUp)"
    assert _resolve_watch_name("sports cars", cfg) == "Manual Sports Cars (Seattle)"
    assert _resolve_watch_name("Totally Unknown", cfg) is None


def test_prose_extracts_message_from_json_blob():
    from web_watcher.dashboard.server import _prose
    assert _prose('{"message": "hello there", "watch_suggestion": null}') == "hello there"
    assert _prose('{"name": "Trucks", "action": "update", "urls": ["x"]}').startswith("(I updated")
    assert _prose("just plain text") == "just plain text"


def test_merge_watch_update_preserves_mode_and_id():
    from web_watcher.config import Watch
    from web_watcher.dashboard.server import _merge_watch_update
    existing = Watch.model_validate({
        "id": "abc123", "name": "Sports Cars", "mode": "continuous",
        "urls": ["https://seattle.craigslist.org/search/cta?query=miata"],
        "instruction": "manual sports cars",
    })
    # A partial assistant update: new urls/terms, NO mode/interval, plus extra keys.
    body = {"action": "update", "name": "Sports Cars",
            "search_terms": ["porsche 911 manual"],
            "urls": ["https://seattle.craigslist.org/search/cta?query=porsche+911"],
            "instruction": "manual sports cars in good shape"}
    w = _merge_watch_update(existing, body, "Sports Cars")
    assert w.mode == "continuous"          # preserved (was defaulting to schedule → error)
    assert w.id == "abc123"                # stable id kept
    assert w.urls == body["urls"]          # applied
    assert w.instruction == "manual sports cars in good shape"


def test_two_items_become_two_watch_cards(monkeypatch):
    """#68: 'watch for a <thing A> and a <thing B>' must ship BOTH watches, not just one."""
    import types
    from web_watcher.dashboard import server as S
    cfg = types.SimpleNamespace(watches=[])
    _mock_two_phase(monkeypatch,
                    "Setting both up now.",
                    {"intent": "create", "watches": [
                        {"name": "A", "urls": ["https://x.org/search?q=a"], "instruction": "a",
                         "mode": "continuous"},
                        {"name": "B", "urls": ["https://x.org/search?q=b"], "instruction": "b",
                         "mode": "continuous"}]})
    monkeypatch.setattr(S, "_expand_watch_search", lambda *a, **k: [])
    out = S._complete_assistant_turn("sys", [{"role": "user", "content": "hi"}], cfg, "m")
    assert [s["name"] for s in out["watch_suggestions"]] == ["A", "B"]
    assert all(s["action"] == "create" for s in out["watch_suggestions"])
    assert out["watch_suggestion"]["name"] == "A"           # legacy single-card field intact


def test_watch_config_in_wrong_slot_is_adopted(monkeypatch):
    """The model sometimes builds a valid config but drops it into listing_query with
    watches:null. That must be repaired into a suggestion, not silently produce nothing."""
    import types
    from web_watcher.dashboard import server as S
    stored = types.SimpleNamespace(name="Existing Watch", urls=["https://x.org/s?q=1"])
    cfg = types.SimpleNamespace(watches=[stored])
    _mock_two_phase(monkeypatch,
                    "Updating that now.",
                    {"intent": "update", "watches": None, "watch_actions": None,
                     "listing_query": {"name": "Existing Watch", "instruction": "i",
                                       "judgment_prompt": "j", "max_agent_steps": 15}})
    monkeypatch.setattr(S, "_resolve_watch_name", lambda name, cfg: "Existing Watch")
    monkeypatch.setattr(S, "_expand_watch_search", lambda *a, **k: [])
    # A REAL edit request that names the watch (so the grounding guard keeps it) — the point of
    # this test is the wrong-slot repair, not the grounding.
    out = S._complete_assistant_turn(
        "sys", [{"role": "user", "content": "change the existing watch instruction"}], cfg, "m")
    sug = out["watch_suggestion"]
    assert sug and sug["action"] == "update" and sug["name"] == "Existing Watch"
    # urls omitted by the model = "unchanged" -> backfilled from the stored watch, so the
    # no-URL safety net can't eat a legitimate edit.
    assert sug["urls"] == ["https://x.org/s?q=1"]
    assert out["listings"] is None                          # not treated as a lookup


def test_single_watch_object_still_supported(monkeypatch):
    """Older 'watch': {...} singular shape keeps working after the schema moved to a list."""
    import types
    from web_watcher.dashboard import server as S
    cfg = types.SimpleNamespace(watches=[])
    _mock_two_phase(monkeypatch,
                    "Setting that up now.",
                    {"intent": "create",
                     "watch": {"name": "Solo", "urls": ["https://x.org/s?q=1"],
                               "instruction": "i", "mode": "continuous"}})
    monkeypatch.setattr(S, "_expand_watch_search", lambda *a, **k: [])
    out = S._complete_assistant_turn("sys", [{"role": "user", "content": "hi"}], cfg, "m")
    assert out["watch_suggestion"]["name"] == "Solo"


# ─────────────────────────────────────────────────────────────────────────────
# Focus tracking with a create in flight (#65 — the fridge/sports-car hijack)
# ─────────────────────────────────────────────────────────────────────────────

def _cfg_with(*names):
    import types
    return types.SimpleNamespace(watches=[types.SimpleNamespace(name=n) for n in names])


def test_pending_create_wins_focus_over_older_watch_words():
    """Real failure: mid-fridge-creation, 'i would prefer it to be black' scanned past the
    fridge talk and locked onto 'Manual Sports Cars' from six messages earlier."""
    from web_watcher.dashboard import server as S
    cfg = _cfg_with("Manual Sports Cars (Seattle)", "Craigslist - 4x4 Trucks (Seattle)")
    msgs = [
        {"role": "user", "content": "lets expand the sports car watch terms"},
        {"role": "assistant", "content": "Done - widened the sports car searches."},
        {"role": "user", "content": "create a watch for refrigerators under $800"},
        {"role": "assistant", "content": "Sure - setting up a refrigerator watch now."},
        {"role": "user", "content": "i would prefer it to be black but not critical"},
    ]
    assert S._focused_watch_name(msgs, cfg) == S.PENDING_CREATE


def test_existing_watch_reference_beats_older_pending_create():
    """Newest-first: naming an existing watch AFTER the create talk moves focus to it."""
    from web_watcher.dashboard import server as S
    cfg = _cfg_with("Manual Sports Cars (Seattle)")
    msgs = [
        {"role": "user", "content": "create a watch for refrigerators under $800"},
        {"role": "assistant", "content": "Sure - setting up a refrigerator watch now."},
        {"role": "user", "content": "actually, first widen the manual sports cars watch"},
    ]
    assert S._focused_watch_name(msgs, cfg) == "Manual Sports Cars (Seattle)"


def test_applied_create_self_heals_to_the_real_watch():
    """Once the create is applied the same words match the real watch by name tokens, so the
    conversation stops being 'pending' without any state to clear."""
    from web_watcher.dashboard import server as S
    cfg = _cfg_with("Refrigerators under $800 (up to 36\" wide)")
    msgs = [
        {"role": "user", "content": "create a watch for refrigerators under $800"},
    ]
    assert S._focused_watch_name(msgs, cfg) == "Refrigerators under $800 (up to 36\" wide)"


def test_pending_create_sentinel_never_becomes_a_watch_name(monkeypatch):
    """An update extracted while focus is the sentinel must not get '__pending_create__' as
    its name."""
    import types
    from web_watcher.dashboard import server as S
    cfg = types.SimpleNamespace(watches=[])
    _mock_two_phase(monkeypatch,
                    "Updating that now.",
                    {"intent": "update",
                     "watches": [{"name": "Something", "instruction": "i",
                                  "urls": ["https://x.org/s?q=1"]}]})
    monkeypatch.setattr(S, "_expand_watch_search", lambda *a, **k: [])
    monkeypatch.setattr(S, "_focused_watch_name", lambda msgs, cfg: S.PENDING_CREATE)
    out = S._complete_assistant_turn("sys", [{"role": "user", "content": "hi"}], cfg, "m")
    assert out["watch_suggestion"]["name"] == "Something"   # model's name kept, sentinel never used


def test_update_naming_no_real_watch_flips_to_create(monkeypatch):
    """3b canary failure: mid-setup the model labels the still-unbuilt watch an 'update'.
    With no matching stored watch (and no focus), the suggestion must flip to a create —
    so the card doesn't say Edit and Apply doesn't 404 a PUT."""
    import types
    from web_watcher.dashboard import server as S
    cfg = types.SimpleNamespace(watches=[])

    _mock_two_phase(monkeypatch,
                    "Adding OfferUp and eBay too — setting that up now.",
                    {"intent": "update",
                     "watch": {"name": "manual cars under 8000",
                               "urls": ["https://seattle.craigslist.org/search/cta?query=manual"],
                               "instruction": "manual cars under $8000"}})
    monkeypatch.setattr(S, "_expand_watch_search", lambda *a, **k: [])
    out = S._complete_assistant_turn("sys", [{"role": "user", "content": "hi"}], cfg, "m")
    s = out["watch_suggestion"]
    assert s is not None
    assert s["action"] == "create"
    assert s["interval_minutes"] == 30   # schedule backfill applies to the flipped create


def test_watcher_health_line_flags_dry_watch(tmp_path, monkeypatch):
    """The Watcher's per-watch health context flags a watch that has seen a lot but matched
    nothing, so it can proactively offer a fix (#94 diagnose)."""
    import types
    from web_watcher.dashboard import server as S
    from web_watcher import storage
    db = tmp_path / "h.db"
    storage.init_db(db)
    wid = "wid-dry"
    for i in range(30):
        k = f"k{i}"
        storage.upsert_listing(k, source="cl", url=f"https://x/{k}", title=k,
                               ts="2026-07-01T00:00:00+00:00", db_path=db)
        storage.record_observation(wid, "Dry Watch", k, "2026-07-01T00:00:00+00:00",
                                   matched=False, db_path=db)
    # Point storage default at this db so _build_watches_context reads it.
    monkeypatch.setattr(storage, "_resolve", lambda p=None: db)

    from web_watcher.config import Watch
    w = Watch.model_validate({"name": "Dry Watch", "id": wid, "urls": ["https://x/s"],
                              "instruction": "diesels", "mode": "continuous",
                              "continuous_idle_seconds": 45})
    cfg = types.SimpleNamespace(watches=[w], models=types.SimpleNamespace())
    mgr = types.SimpleNamespace(get_job_info=lambda: [])
    ctx = S._build_watches_context(cfg, mgr)
    assert "0 matched of 30 seen" in ctx
    assert "DIAGNOSIS" in ctx
