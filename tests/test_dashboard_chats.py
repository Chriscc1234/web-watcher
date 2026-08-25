"""The Chats admin console: the per-person conversation index, owner-name memory, and the
Telegram access-request store (a stranger is parked here for one-click approval, never let in
automatically). Pure filesystem logic — no server needed. See dashboard/server.py."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from web_watcher.config import AppConfig, NotificationsConfig, TelegramConfig, Watch
from web_watcher.dashboard import server as S
from web_watcher.dashboard.server import create_app


@pytest.fixture()
def isolated(monkeypatch, tmp_path):
    """Redirect every on-disk chat artifact into a temp dir so tests never touch real data."""
    hist = tmp_path / "watcher_history.json"
    monkeypatch.setattr(S, "_WATCHER_HISTORY_PATH", hist)
    monkeypatch.setattr(S, "_OWNER_NAMES_PATH", hist.with_name("watcher_owners.json"))
    monkeypatch.setattr(S, "_ACCESS_REQ_PATH", hist.with_name("telegram_access_requests.json"))
    return tmp_path


# ── owner display names ──────────────────────────────────────────────────────────

def test_owner_name_is_remembered_and_deduped(isolated):
    S._record_owner_name("555", "Buddy")
    S._record_owner_name("555", "Buddy")          # same — no churn
    S._record_owner_name("", "Nobody")            # blank owner ignored
    assert S._load_owner_names() == {"555": "Buddy"}


# ── access requests ──────────────────────────────────────────────────────────────

def test_access_request_records_dedupes_and_removes(isolated):
    S._record_access_request("999", "Stranger")
    S._record_access_request("999", "Stranger Renamed")   # same id → updated in place, not duplicated
    S._record_access_request("888", "")
    reqs = S._load_access_requests()
    assert {r["chat_id"] for r in reqs} == {"999", "888"}
    assert next(r for r in reqs if r["chat_id"] == "999")["name"] == "Stranger Renamed"
    S._remove_access_request("999")
    assert {r["chat_id"] for r in S._load_access_requests()} == {"888"}


# ── the conversation index ───────────────────────────────────────────────────────

def test_thread_index_lists_desktop_first_then_people(isolated):
    # Desktop (main) thread + one Telegram person's thread.
    S._save_watcher_history([{"role": "user", "content": "hello from desktop", "ts": 100}], None)
    S._save_watcher_history(
        [{"role": "user", "content": "hi", "ts": 200},
         {"role": "assistant", "content": "the last line here", "ts": 201}], "555")
    S._record_owner_name("555", "Buddy")

    cfg = AppConfig(watches=[
        Watch(name="Buddy's trucks", urls=["https://x"], instruction="trucks",
              interval_minutes=30, owner="555"),
        Watch(name="My boats", urls=["https://y"], instruction="boats",
              interval_minutes=30, owner=""),
    ])
    threads = S._list_conversation_threads(cfg)

    assert threads[0]["owner"] is None                       # desktop always first
    assert threads[0]["watches"] == 2                        # admin sees every watch
    person = next(t for t in threads if t["owner"] == "555")
    assert person["label"] == "Buddy"                        # name, not the raw id
    assert person["messages"] == 2
    assert person["last_snippet"] == "the last line here"
    assert person["watches"] == 1                            # only the watch they own


# ── admin steps into a person's thread ───────────────────────────────────────────

def test_admin_message_delivers_to_telegram_and_records(isolated, monkeypatch):
    calls = {}

    class _Resp:
        def json(self):
            return {"ok": True}

    def fake_post(url, **kw):
        calls["url"] = url
        calls["json"] = kw.get("json")
        return _Resp()

    monkeypatch.setattr(S.httpx, "post", fake_post)
    cfg = AppConfig(notifications=NotificationsConfig(
        telegram=TelegramConfig(bot_token="111:TOK", chat_id="12345")))
    monkeypatch.setattr(S, "_load_cfg", lambda: cfg)
    client = TestClient(create_app(MagicMock()))

    r = client.post("/api/oversight/threads/message", json={"owner": "555", "text": "Hi Dave, fixed it."})
    assert r.json() == {"ok": True}
    assert calls["json"]["chat_id"] == "555"                  # delivered to the person, not the admin
    assert calls["json"]["text"] == "Hi Dave, fixed it."
    hist = S._load_watcher_history("555")
    assert hist[-1]["content"] == "Hi Dave, fixed it." and hist[-1].get("admin") is True


def test_admin_message_needs_owner_and_token(isolated, monkeypatch):
    client = TestClient(create_app(MagicMock()))
    monkeypatch.setattr(S, "_load_cfg", lambda: AppConfig(
        notifications=NotificationsConfig(telegram=TelegramConfig(bot_token="", chat_id=""))))
    assert client.post("/api/oversight/threads/message",
                       json={"owner": "", "text": "x"}).json()["ok"] is False   # no target
    assert client.post("/api/oversight/threads/message",
                       json={"owner": "555", "text": "x"}).json()["ok"] is False  # no token


# ── smart local/cloud escalation ─────────────────────────────────────────────────

def _um(text):
    return [{"role": "user", "content": text}]


def test_easy_turns_stay_local():
    for msg in ["yes", "hi", "status?", "how are my watches?", "what watches do I have?", "thanks"]:
        assert S._is_hard_chat_turn(_um(msg), None) is False, msg


def test_hard_turns_escalate():
    for msg in ["watch craigslist for 4x4 trucks under 15k",
                "find me a diesel Tacoma around Anacortes",
                "change the price cap to 8000",
                "make my watch always run",
                "look on facebook marketplace for boats"]:
        assert S._is_hard_chat_turn(_um(msg), None) is True, msg


def test_pending_create_is_always_hard():
    assert S._is_hard_chat_turn(_um("make it black"), S.PENDING_CREATE) is True


def test_always_run_counts_as_a_change_request():
    # The exact miss from the logs: "…always run" must register as asking to change the watch.
    assert bool(S._CHANGE_SIGNAL_RE.search("my watch I just created always run")) is True
    assert bool(S._CHANGE_SIGNAL_RE.search("can you make it run continuously?")) is True


# ── history stores what was SAID, not how it was formatted ───────────────────────
# Markup belongs to the delivery channel. A reply saved with <b> tags is read back to the model
# as its own past work, and it starts writing tags into its prose — which is how a literal
# "<i>Change yours:</i>" reached a phone.

def test_stored_replies_carry_no_markup(isolated):
    S._persist_chat_turn(
        [{"role": "user", "content": "settings"}],
        {"message": "⚙️ <b>Your settings</b>\n• Quiet check-ins: <b>twice a day</b>\n"
                    "<i>Change yours:</i> “twice a day”"},
        "555")
    saved = S._load_watcher_history("555")[-1]["content"]
    assert "<b>" not in saved and "<i>" not in saved
    assert "Your settings" in saved and "twice a day" in saved


def test_strip_html_unescapes_entities_and_leaves_plain_text_alone():
    assert S._strip_html("Tacoma &amp; Hilux") == "Tacoma & Hilux"
    assert S._strip_html("boats under 30 feet") == "boats under 30 feet"
    assert S._strip_html("") == ""


def test_an_emptied_thread_is_not_listed(isolated):
    """Clearing a conversation leaves its file behind; a person with no messages and no watches
    is just a mystery entry in the console."""
    S._save_watcher_history([], "999888777")
    cfg = AppConfig(watches=[])
    labels = [t["label"] for t in S._list_conversation_threads(cfg)]
    assert not any("999888777" in l for l in labels)


def test_a_thread_with_watches_is_still_listed_when_empty(isolated):
    S._save_watcher_history([], "555")
    cfg = AppConfig(watches=[Watch(name="Theirs", urls=["https://x"], instruction="x",
                                   interval_minutes=30, owner="555")])
    assert any("555" in t["label"] for t in S._list_conversation_threads(cfg))


# ── "show me the match" must SHOW the match ──────────────────────────────────────
# From the real log: "Show me the one match" → "It's an under 30-foot motor boat with an outboard
# motor, priced reasonably within $15,000." That's the watch's CRITERIA read back — what was
# asked for, not what was found. The 14b's extractor keeps missing this intent, so it's decided
# in code, like settings and start/stop before it.

def test_asking_to_see_finds_is_recognised():
    for msg in ["show me the one match", "Show me the matches", "list them",
                "what did you find?", "anything on the boats?", "show me the top 10",
                "let's see the listings", "any new results?", "show me the best ones"]:
        assert S._is_lookup_request(msg) is True, msg


def test_making_or_changing_a_watch_is_not_a_lookup():
    for msg in ["show me how to set up a watch", "create a watch for boats",
                "stop the boats watch", "change the price to 5000",
                "delete the truck watch", "make a new watch"]:
        assert S._is_lookup_request(msg) is False, msg


def test_ordinary_chat_is_not_a_lookup():
    for msg in ["hi", "thanks", "how are you", "settings", "yes"]:
        assert S._is_lookup_request(msg) is False, msg


def test_lookup_limit_reads_the_number_asked_for():
    assert S._lookup_limit("show me the top 20") == 20
    assert S._lookup_limit("show me 5") == 5
    assert S._lookup_limit("show me the matches") == 10        # default
    assert S._lookup_limit("top 9999") == 30                   # bounded — no whole-DB dump


# ── a vague lookup gets completed, not obeyed literally ──────────────────────────
# Live: the model DID produce a listing_query for "show me the matches from the boats watch",
# but with no watch, no matched-only and no limit — so it returned 200 rows of everything ever
# seen, Brooklyn sofas included. Filling only a MISSING query wasn't enough.

def _lookup_turn(monkeypatch, text, model_lq, cfg, owner=None, focus=None):
    """Run one turn with the extractor stubbed, and report the query that actually ran."""
    ran = {}
    monkeypatch.setattr(S, "_chat_reply_natural", lambda *a, **k: ("ok", 0, 0, 0))
    monkeypatch.setattr(S, "_extract_watch_action",
                        lambda *a, **k: ({"listing_query": model_lq} if model_lq else {}))
    monkeypatch.setattr(S, "_run_listing_query", lambda p, **k: ran.update(p) or [])
    monkeypatch.setattr(S, "_persist_chat_turn", lambda *a, **k: None)
    monkeypatch.setattr(S, "_load_cfg", lambda: cfg)
    S._complete_assistant_turn("sys", [{"role": "user", "content": text}], cfg, "m", owner=owner)
    return ran


def _one_watch_cfg():
    return AppConfig(watches=[Watch(name="Boats Watch", urls=["https://x"], instruction="boats",
                                    interval_minutes=30, owner="")])


def test_an_unscoped_model_lookup_is_scoped_and_capped(monkeypatch):
    ran = _lookup_turn(monkeypatch, "show me the matches from the boats watch",
                       {"text": "boats"}, _one_watch_cfg())
    assert ran["watch"] == "Boats Watch"        # was missing — the sofas came from this
    assert ran["matched_only"] is True          # "the matches" means the matches
    assert ran["limit"] == 10                   # not the 200-row default


def test_a_missing_lookup_is_created(monkeypatch):
    ran = _lookup_turn(monkeypatch, "show me the one match", None, _one_watch_cfg())
    assert ran["watch"] == "Boats Watch" and ran["matched_only"] is True


def test_what_the_model_did_decide_is_respected(monkeypatch):
    ran = _lookup_turn(monkeypatch, "show me the top 20",
                       {"watch": "Boats Watch", "matched_only": False, "limit": 20},
                       _one_watch_cfg())
    assert ran["matched_only"] is False and ran["limit"] == 20   # not overridden


def test_ordinary_chat_does_not_trigger_a_lookup(monkeypatch):
    assert _lookup_turn(monkeypatch, "thanks!", None, _one_watch_cfg()) == {}


# ── a blank template is not an answer ────────────────────────────────────────────
# Live: "show me a full list of the matches" was answered with fill-in-the-blanks —
# "**Title:** [Boat Title] … **Price:** $XX,XXX … **URL:** [Link to Listing]" — with the real
# listings arriving underneath it. A screenful of scaffolding before anything useful.

def test_a_placeholder_template_is_recognised():
    assert S._looks_like_a_blank_template(
        "1. **Title:** [Boat Title]\n **Price:** $XX,XXX\n **URL:** [Link to Listing]") is True


def test_real_prose_is_never_mistaken_for_a_template():
    for good in ("Here are the 10 matches. The Sea Ray at $14,500 looks like the best value.",
                 "I found 3 boats — a 1999 Hewscraft ($15,950), a Bayliner and a Reinell.",
                 "Nothing new yet [I'll keep looking].",
                 "You have two watches running."):
        assert S._looks_like_a_blank_template(good) is False, good


def test_one_bracketed_phrase_is_not_scaffolding():
    """A single bracket is ordinary writing; a repeated pattern of them is a template."""
    assert S._looks_like_a_blank_template("The best one is [see the list below].") is False


# ── the model does not narrate listings we already hold ──────────────────────────
# The worst failure this assistant has produced: asked for the matches, it wrote out plausible
# listings it INVENTED — "20ft Motor Boat with Outboard Engine, $12,000, Everett" — while the
# genuine rows travelled alongside. Blank placeholders are obviously wrong to a reader;
# convincing fabrications are not, and someone could drive to Everett for a boat that never was.

_FABRICATED = """Sure, here's a full list of all the matches found so far:

- **Match 1:**
  - Title: "20ft Motor Boat with Outboard Engine"
  - Price: $12,000
  - Location: Everett

- **Match 2:**
  - Title: "Used 25ft Motor Boat for Sale"
  - Price: $14,500
"""


def test_invented_listings_are_recognised_as_an_enumeration():
    assert S._prose_enumerates_listings(_FABRICATED) is True


def test_a_blank_template_is_also_an_enumeration():
    assert S._prose_enumerates_listings(
        "1. **Title:** [Boat Title]\n   **Price:** $XX,XXX\n"
        "2. **Title:** [Boat Title]\n   **Price:** $XX,XXX") is True


def test_ordinary_prose_about_the_finds_is_kept():
    for good in ("Here are the 10 matches — the Sea Ray at $14,500 looks like the best value.",
                 "I pulled up 10 boats. A couple are over your budget.",
                 "The best one is the 1983 Bayliner at $15,000.",
                 "Nothing new since yesterday."):
        assert S._prose_enumerates_listings(good) is False, good


def test_a_plain_bulleted_answer_is_not_an_enumeration():
    """Listing the WATCHES is fine — the guard is about narrating listing rows."""
    assert S._prose_enumerates_listings(
        "You have two watches:\n- Anacortes cars\n- Anacortes boats") is False


def test_the_guard_only_applies_when_we_hold_the_real_rows(monkeypatch):
    """With no listings to show, the model's prose is all there is — never discard it."""
    ran = {}
    monkeypatch.setattr(S, "_chat_reply_natural", lambda *a, **k: (_FABRICATED, 0, 0, 0))
    monkeypatch.setattr(S, "_extract_watch_action", lambda *a, **k: {})
    monkeypatch.setattr(S, "_run_listing_query", lambda p, **k: ran.update(p) or [])
    monkeypatch.setattr(S, "_persist_chat_turn", lambda *a, **k: None)
    cfg = AppConfig(watches=[Watch(name="Boats", urls=["https://x"], instruction="boats",
                                   interval_minutes=30, owner="")])
    monkeypatch.setattr(S, "_load_cfg", lambda: cfg)
    out = S._complete_assistant_turn("sys", [{"role": "user", "content": "show me the matches"}],
                                     cfg, "m", owner=None)
    assert out["message"] == _FABRICATED          # nothing better to offer, so it stands


def test_fabricated_prose_is_replaced_when_rows_exist(monkeypatch):
    rows = [{"title": "1983 Bayliner Explorer 2070", "price_text": "$15,000", "url": "https://x/1"},
            {"title": "1998 Scarab 22'", "price_text": "$12,500", "url": "https://x/2"}]
    monkeypatch.setattr(S, "_chat_reply_natural", lambda *a, **k: (_FABRICATED, 0, 0, 0))
    monkeypatch.setattr(S, "_extract_watch_action", lambda *a, **k: {})
    monkeypatch.setattr(S, "_run_listing_query", lambda p, **k: rows)
    monkeypatch.setattr(S, "_persist_chat_turn", lambda *a, **k: None)
    cfg = AppConfig(watches=[Watch(name="Boats", urls=["https://x"], instruction="boats",
                                   interval_minutes=30, owner="")])
    monkeypatch.setattr(S, "_load_cfg", lambda: cfg)
    out = S._complete_assistant_turn("sys", [{"role": "user", "content": "show me the matches"}],
                                     cfg, "m", owner=None)
    assert "Everett" not in out["message"]        # the invented boat is gone
    assert "12,000" not in out["message"]
    assert "2 matches" in out["message"] and "Boats" in out["message"]
    assert out["listings"] == rows                # the real rows are what's shown


# ── "the matches for boats" means the boats watch ────────────────────────────────
# With two watches and no focus set, the lookup ran unscoped and returned boats mixed with a
# Nissan Rogue from the cars watch — and the lead-in read "your watches" instead of naming it.

def _two_watches():
    return AppConfig(watches=[
        Watch(name="Anacortes Manual Transmission Cars Watch", urls=["https://a"],
              instruction="cars", interval_minutes=30, owner=""),
        Watch(name="Anacortes Under 30' Motor Boats Watch", urls=["https://b"],
              instruction="boats", interval_minutes=30, owner=""),
    ])


def test_a_watch_is_matched_on_a_distinctive_word():
    cfg = _two_watches()
    assert S._watch_named_in("show me the matches for boats", cfg, None) == \
        "Anacortes Under 30' Motor Boats Watch"
    assert S._watch_named_in("anything on the cars?", cfg, None) == \
        "Anacortes Manual Transmission Cars Watch"


def test_singular_and_plural_both_match():
    cfg = _two_watches()
    assert S._watch_named_in("show me the boat matches", cfg, None).endswith("Boats Watch")


def test_a_word_every_watch_shares_matches_nothing():
    """"Anacortes" is in both names — acting on it would show the wrong watch half the time."""
    assert S._watch_named_in("show me the anacortes matches", _two_watches(), None) == ""


def test_noise_words_never_identify_a_watch():
    assert S._watch_named_in("show me all the watches", _two_watches(), None) == ""
    assert S._watch_named_in("show me the matches", _two_watches(), None) == ""


def test_the_named_watch_scopes_the_lookup(monkeypatch):
    ran = {}
    monkeypatch.setattr(S, "_chat_reply_natural", lambda *a, **k: ("ok", 0, 0, 0))
    monkeypatch.setattr(S, "_extract_watch_action", lambda *a, **k: {})
    monkeypatch.setattr(S, "_run_listing_query", lambda p, **k: ran.update(p) or [])
    monkeypatch.setattr(S, "_persist_chat_turn", lambda *a, **k: None)
    cfg = _two_watches()
    monkeypatch.setattr(S, "_load_cfg", lambda: cfg)
    S._complete_assistant_turn(
        "sys", [{"role": "user", "content": "Show me a full list of all of the matches for boats"}],
        cfg, "m", owner=None)
    assert ran["watch"] == "Anacortes Under 30' Motor Boats Watch"


# ── when we hold the rows, the model writes a LEAD-IN ────────────────────────────
# Detecting each fabrication shape one at a time was a losing game: the first guard caught blank
# placeholders, the next caught invented listings, and the one after slipped through by a single
# marker ("- [Match Details]" twice where three were needed). Inverted: a lead-in is short.

_SLIPPED = """Sure, here's the full list of matches for boats that I've found so far:

**Anacortes Under 30' Motor Boats Watch**
- **Site:** Craigslist (Skagit County)
- **Search Distance:** 150 miles from Anacortes
- **Price Range:** Under $15,000

Matches Found:
- [Match Details]
- [Match Details]

If you need more specific information, let me know!"""


def test_a_long_re_description_of_the_search_is_replaced():
    assert S._should_replace_prose(_SLIPPED) is True


def test_a_short_genuine_remark_survives():
    for keep in ("Here are the 10 matches — the Sea Ray at $14,500 looks like the best value.",
                 "I pulled up 10 boats. A couple are just over your budget.",
                 "Found 6. The GLASPLY at $9,500 is the cheapest with a real outboard."):
        assert S._should_replace_prose(keep) is False, keep


def test_an_empty_reply_is_replaced():
    assert S._should_replace_prose("") is True
    assert S._should_replace_prose("   ") is True


def test_the_shapes_caught_before_are_still_caught():
    assert S._should_replace_prose(_FABRICATED) is True
    assert S._should_replace_prose("1. **Title:** [Boat Title]\n   **Price:** $XX,XXX") is True


# ── a model-emitted lookup on a non-lookup message must not dump the store ────────
# Live: "What's currently on my watchlist?" — not a lookup — was answered with "200 matches for
# 'your watches'". The classifier correctly said not-a-lookup, so the scoping code was skipped and
# the model's own raw, unscoped listing_query ran with the 200-row default. Both paths must scope.

def _lookup_ran(monkeypatch, text, model_lq, cfg, owner=None):
    ran = {"called": False, "params": None}
    monkeypatch.setattr(S, "_chat_reply_natural", lambda *a, **k: ("ok", 0, 0, 0))
    monkeypatch.setattr(S, "_extract_watch_action",
                        lambda *a, **k: ({"listing_query": model_lq} if model_lq else {}))
    def run(p, **k):
        ran["called"] = True; ran["params"] = p; return []
    monkeypatch.setattr(S, "_run_listing_query", run)
    monkeypatch.setattr(S, "_persist_chat_turn", lambda *a, **k: None)
    monkeypatch.setattr(S, "_load_cfg", lambda: cfg)
    S._complete_assistant_turn("sys", [{"role": "user", "content": text}], cfg, "m", owner=owner)
    return ran


def test_a_watchlist_question_does_not_trigger_a_listing_dump(monkeypatch):
    """The exact live case: not a lookup + a raw model query + two watches → drop it."""
    cfg = _two_watches()
    ran = _lookup_ran(monkeypatch, "What's currently on my watchlist?", {}, cfg)
    assert ran["called"] is False          # no unscoped 200-row dump


def test_a_watchlist_question_is_not_a_lookup():
    assert S._is_lookup_request("What's currently on my watchlist?") is False
    assert S._is_lookup_request("what watches do I have") is False
    assert S._is_lookup_request("are there any watches running") is False


def test_a_model_lookup_naming_a_watch_still_runs(monkeypatch):
    """If the user's own words name a watch, a model lookup is legitimate even off a loose phrasing."""
    cfg = _two_watches()
    ran = _lookup_ran(monkeypatch, "anything from the boats?", {}, cfg)
    assert ran["called"] is True
    assert ran["params"]["watch"] == "Anacortes Under 30' Motor Boats Watch"
    assert ran["params"]["limit"] == 10        # capped, not the 200 default


def test_a_real_lookup_still_works_after_the_restructure(monkeypatch):
    cfg = _two_watches()
    ran = _lookup_ran(monkeypatch, "show me the matches for boats", {}, cfg)
    assert ran["called"] is True
    assert ran["params"]["watch"] == "Anacortes Under 30' Motor Boats Watch"


# ── the model gets an unambiguous run-state to repeat ────────────────────────────
# Two answers in one session contradicted each other — "two watches running right now" (both were
# OFF) and "all turned off". The model was inferring run-state from terse per-watch jargon
# ("DISABLED, stopped") and guessing. It now gets a plain pre-computed STATUS line and plain
# per-watch state ("OFF — not watching").

def _ctx(watches, paused=False, running=None):
    from unittest.mock import MagicMock
    mgr = MagicMock()
    mgr.is_paused.return_value = paused
    mgr.get_job_info.return_value = [{"watch_name": n, "continuous_running": True}
                                     for n in (running or [])]
    return S._build_watches_context(AppConfig(watches=watches), mgr)


def _off(name):
    return Watch(name=name, urls=["https://x"], instruction="x", interval_minutes=30,
                 mode="continuous", enabled=False)


def _on(name):
    return Watch(name=name, urls=["https://x"], instruction="x", interval_minutes=30,
                 mode="continuous", enabled=True)


def test_all_off_says_nothing_is_watching():
    ctx = _ctx([_off("Boats"), _off("Cars")])
    assert "ALL are turned OFF" in ctx and "nothing is being watched" in ctx
    assert "OFF — not watching" in ctx
    assert "are ON" not in ctx        # nothing is claimed to be on


def test_some_on_is_counted_exactly():
    ctx = _ctx([_on("Boats"), _off("Cars")], running=["Boats"])
    assert "1 of 2 watch(es) are ON" in ctx


def test_paused_master_switch_overrides_watch_state():
    ctx = _ctx([_on("Boats")], paused=True, running=["Boats"])
    assert "PAUSED" in ctx and "NOTHING is being watched" in ctx


def test_a_disabled_watch_is_never_described_as_running():
    ctx = _ctx([_off("Boats")])
    boats = [ln for ln in ctx.splitlines() if "health:" in ln][0]
    assert "OFF" in boats and "watching now" not in boats
