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
    assert out[4] == "https://ebay.com/sch/i.html?_nkw=gpu"
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
