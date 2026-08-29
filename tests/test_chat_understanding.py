"""
The chat assistant understanding the conversation — not keyword-matching it.

From a user-shaped gauntlet against the live app: an ACTION request ("turn off all my
watches…") was hijacked by the status fast-path and answered with a status LIST; "the three
most recent" returned ten; "how much is the second one?" was met with "I don't have context"
one turn after the assistant itself showed the list; "why did you reject X?" got a guess when
the database held the recorded verdict.
"""

from __future__ import annotations

from web_watcher.dashboard import server as S


# --------------------------------------------------------------------------
# The status fast-path only fires for a PURE status question
# --------------------------------------------------------------------------

def test_pure_status_questions_still_intercept():
    for t in ("what watches do I have?", "which watches are running",
              "list my watches", "how many watches are there"):
        assert S._pure_status_request(t), t


def test_action_requests_never_intercept():
    """The live failure: this exact message got a status list instead of any action."""
    for t in ("Turn off all my watches. Actually wait, keep the MacGregor one on, kill the rest",
              "stop my watches", "delete the boats watch and my watches list",
              "start all my watches please"):
        assert not S._pure_status_request(t), t


def test_compound_status_plus_finds_falls_through():
    t = "Which of my watches are running, and did any of them find anything in the last day?"
    assert not S._pure_status_request(t)


# --------------------------------------------------------------------------
# Counts written as words
# --------------------------------------------------------------------------

def test_word_counts_are_understood():
    assert S._lookup_limit("Show me the three most recent macgregor matches") == 3
    assert S._lookup_limit("top five matches please") == 5
    assert S._lookup_limit("show me a couple of listings") == 2
    assert S._lookup_limit("a few recent finds") == 3


def test_digit_counts_still_work():
    assert S._lookup_limit("top 20 matches") == 20
    assert S._lookup_limit("show me 5 recent ones") == 5


def test_singular_latest_still_means_one():
    assert S._lookup_limit("show me the latest match... wait the newest listing") == 1


def test_no_count_returns_default():
    assert S._lookup_limit("show me the matches", default=10) == 10


# --------------------------------------------------------------------------
# The shown-listings memory
# --------------------------------------------------------------------------

def _mk_history(monkeypatch, history):
    monkeypatch.setattr(S, "_load_watcher_history", lambda owner=None: history)


def test_shown_block_resolves_the_second_one(monkeypatch):
    reply = "Here are 3 matches for your watches:"
    _mk_history(monkeypatch, [
        {"role": "user", "content": "show me matches"},
        {"role": "assistant", "content": reply,
         "shown": {"listings": [
             {"title": "MacGregor 26D Bellingham", "price": "$5,000", "rating": 4,
              "url": "https://x/1"},
             {"title": "MacGregor 26S Burien", "price": "$7,500", "rating": 4,
              "url": "https://x/2"},
         ]}},
    ])
    msgs = [{"role": "user", "content": "show me matches"},
            {"role": "assistant", "content": reply},
            {"role": "user", "content": "how much is the second one?"}]
    block = S._recent_shown_block(msgs, None)
    assert "1. MacGregor 26D" in block
    assert "2. MacGregor 26S" in block
    assert "the second one" in block


def test_shown_block_empty_on_first_turn(monkeypatch):
    _mk_history(monkeypatch, [
        {"role": "assistant", "content": "old reply",
         "shown": {"listings": [{"title": "Old boat"}]}}])
    msgs = [{"role": "user", "content": "hello"}]
    assert S._recent_shown_block(msgs, None) == ""


def test_shown_block_ignores_another_conversations_list(monkeypatch):
    """Saved history can hold OLDER threads — a fresh conversation must not inherit their
    listings and start answering 'the second one' about boats it never showed."""
    _mk_history(monkeypatch, [
        {"role": "assistant", "content": "reply from last week",
         "shown": {"listings": [{"title": "Stale boat"}]}}])
    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "different reply"},
            {"role": "user", "content": "the second one?"}]
    assert S._recent_shown_block(msgs, None) == ""


def test_shown_block_survives_a_broken_history(monkeypatch):
    monkeypatch.setattr(S, "_load_watcher_history",
                        lambda owner=None: (_ for _ in ()).throw(RuntimeError("gone")))
    msgs = [{"role": "assistant", "content": "x"}, {"role": "user", "content": "y"}]
    assert S._recent_shown_block(msgs, None) == ""


# --------------------------------------------------------------------------
# "Why did you reject…" reaches for the recorded verdict
# --------------------------------------------------------------------------

def test_why_verdict_detector():
    for t in ("Why did you reject the Macgregor in Bothell?",
              "why didn't it match", "Why wasn't the Ferndale one shown?",
              "why did you skip that listing"):
        assert S._WHY_VERDICT_RE.search(t), t
    for t in ("show me the matches", "what are my watches", "reject that one"):
        assert not S._WHY_VERDICT_RE.search(t), t


def test_why_stopwords_keep_the_content_words():
    text = "Why did you reject the Macgregor in Bothell?"
    import re
    toks = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9'’-]{3,}", text)
            if w.lower() not in S._WHY_STOPWORDS]
    assert "macgregor" in toks and "bothell" in toks
    assert "reject" not in toks and "watch" not in toks


# --------------------------------------------------------------------------
# The exception clause on a blanket action
# --------------------------------------------------------------------------

def test_gerunds_are_spelled():
    """'Stoping all 7 of your watches' went to a real user. Never again."""
    src = open("web_watcher/dashboard/server.py", encoding="utf-8").read()
    assert "act.title()}ing" not in src, "a bare act.title()+'ing' gerund survives (Stoping)"


# --------------------------------------------------------------------------
# The extract validator matches the extractor's OWN schema
# --------------------------------------------------------------------------

COMMIT_REPLY = "Got it — I'll set up a watch for a Ford F-150 under $15,000 near Burlington."


def test_a_real_create_extraction_passes_validation():
    """The burn loop: this exact (correct) shape 'failed' the old check, escalating to Haiku
    then Sonnet on EVERY committed turn — two paid calls per turn, buying nothing."""
    good = '{"intent": "create", "watches": [{"name": "F-150", "urls": ["https://x"]}]}'
    assert S._extract_result_usable(good, COMMIT_REPLY)


def test_singular_watch_key_passes():
    assert S._extract_result_usable('{"intent": "create", "watch": {"name": "x"}}', COMMIT_REPLY)


def test_actions_pass():
    assert S._extract_result_usable(
        '{"intent": "actions", "watch_actions": [{"action": "stop", "name": "x"}]}', COMMIT_REPLY)


def test_empty_extraction_on_a_committed_reply_fails():
    """The case the validator exists FOR: the reply promised a watch, the extraction is empty
    — that is the real escalation trigger."""
    assert not S._extract_result_usable('{"intent": "none"}', COMMIT_REPLY)


def test_empty_extraction_on_a_chatty_reply_passes():
    assert S._extract_result_usable('{"intent": "none"}', "Nothing new today — watches are off.")


def test_garbage_fails():
    assert not S._extract_result_usable("not json at all", COMMIT_REPLY)
    assert not S._extract_result_usable('["a", "list"]', COMMIT_REPLY)


# --------------------------------------------------------------------------
# A buddy must not be told about other people's watches
# --------------------------------------------------------------------------

def test_oversight_narration_is_scoped_in_source():
    """The Watcher's observation feed is GLOBAL ("All eyes on Anacortes boats and cars, but
    the TEST eBay watch is snoozing") and was injected into every chat turn. A buddy who owns
    no watches asked "what am I watching?" and was told he was watching the ADMIN's car watch,
    because the model answered from that narration."""
    import inspect
    src = inspect.getsource(S)
    i = src.index("WHAT YOU'VE RECENTLY OBSERVED")
    window = src[max(0, i - 1400):i]
    assert "_is_admin_owner" in window and "_watches_for_owner" in window, \
        "the narration is not scoped to the speaker's own watches"


# --------------------------------------------------------------------------
# Never promise an action that did not happen
# --------------------------------------------------------------------------

def test_commit_detector_catches_the_live_phrasing():
    """The exact sentence a buddy got twice, twelve minutes apart, with nothing created."""
    for reply in ("Sure thing! I’m setting up that watch on Craigslist now to find manual "
                  "transmission cars in Anacortes.",
                  "I'll create a watch for that.",
                  "Setting up a watch for you now."):
        assert S._reply_commits_to_action(reply), reply


def test_plain_answers_are_not_commitments():
    for reply in ("Nothing new today — your watches are off.",
                  "You don't have any watches assigned yet.",
                  "The second one is a 1974 MacGregor Venture 17, $3,500."):
        assert not S._reply_commits_to_action(reply), reply


def test_empty_promise_is_rewritten_in_source():
    import inspect
    src = inspect.getsource(S)
    assert "reply promised an action with none to perform" in src


# --------------------------------------------------------------------------
# An admin-set label outranks a Telegram profile name
# --------------------------------------------------------------------------

def test_nickname_is_appended_not_substituted(monkeypatch, tmp_path):
    """A throwaway account's profile name really can be "Nameless". Keep it — it is who the
    account IS — and append the nickname we use for them, which is also what
    "give the watch to Jordan" matches on."""
    monkeypatch.setattr(S, "_OWNER_NAMES_PATH", tmp_path / "names.json")
    monkeypatch.setattr(S, "_OWNER_LABELS_PATH", tmp_path / "labels.json")
    S.set_owner_label("8708818228", "Jordan")
    S._record_owner_name("8708818228", "Nameless")      # a later Telegram message
    assert S._load_owner_names()["8708818228"] == "Nameless (Jordan)"


def test_nickname_is_not_duplicated_when_it_matches_the_name(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "_OWNER_NAMES_PATH", tmp_path / "names.json")
    monkeypatch.setattr(S, "_OWNER_LABELS_PATH", tmp_path / "labels.json")
    S._record_owner_name("7", "Chris Colabella")
    S.set_owner_label("7", "Chris")
    assert S._load_owner_names()["7"] == "Chris Colabella"


def test_resolve_person_matches_the_nickname(monkeypatch, tmp_path):
    """The whole point of the nickname: 'give the boats watch to Jordan' must resolve."""
    monkeypatch.setattr(S, "_load_owner_names", lambda: {"8708818228": "Nameless (Jordan)"})
    import types as _t
    tg = _t.SimpleNamespace(chat_id="111", allowed_chat_ids=["8708818228"])
    cfg = _t.SimpleNamespace(notifications=_t.SimpleNamespace(telegram=tg))
    cid, label = S._resolve_person("Jordan", cfg, actor=None)
    assert cid == "8708818228"


def test_telegram_name_is_used_when_no_label_set(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "_OWNER_NAMES_PATH", tmp_path / "names.json")
    monkeypatch.setattr(S, "_OWNER_LABELS_PATH", tmp_path / "labels.json")
    S._record_owner_name("999", "Real Person")
    assert S._load_owner_names()["999"] == "Real Person"


# --------------------------------------------------------------------------
# The empty-promise rewrite fires on PROMISES, not descriptions
# --------------------------------------------------------------------------

def test_describing_an_existing_watch_is_not_a_promise():
    """The live failure: "Is the fiat watch continuous?" was answered correctly by the model,
    but the reply contained "watching for", the broad commit regex flagged it, and the canned
    "I haven't set anything up yet" replaced a true answer — twice in a row, about a watch
    that existed and had already briefed its owner."""
    for reply in (
        "Yes — the Fiat X19 Cars Watch is continuous. It's watching for new listings all day.",
        "The Fiat watch is running and watching the sites you asked for.",
        "It checks OfferUp, eBay and Craigslist continuously.",
        "That watch hasn't found anything new yet — should I widen it?",
    ):
        assert not S._CLAIMS_SETUP_NOW_RE.search(reply), reply


def test_actual_setup_claims_still_trip_the_rewrite():
    for reply in (
        "Sure thing! I'm setting up that watch on Craigslist now.",
        "I'll set it up right away.",
        "I've set it up — you're all set.",
        "Creating the watch now.",
    ):
        assert S._CLAIMS_SETUP_NOW_RE.search(reply), reply


# --------------------------------------------------------------------------
# Enabled continuous watches survive an app restart
# --------------------------------------------------------------------------

def test_running_set_is_remembered_and_resumed():
    """Every self-update re-registered continuous watches as stopped: a user's brand-new
    Fiat watch briefed him once, then silently sat dead through three updates."""
    src = open("web_watcher/scheduler.py", encoding="utf-8").read()
    assert "_remember_running(watch_name, True)" in src
    assert "_remember_running(watch_name, False)" in src
    assert "was running before the restart — resuming" in src


def test_remember_running_roundtrip(tmp_path):
    from web_watcher.scheduler import WatchScheduler
    class T(WatchScheduler):
        def __init__(self): pass          # no scheduler machinery — just the state file
        def _running_state_path(self): return tmp_path / "continuous_running.json"
    t = T()
    assert t._remembered_running() is None      # tri-state: nothing ever recorded
    t._remember_running("Fiat X19 Cars Watch", True)
    t._remember_running("Boats", True)
    assert t._remembered_running() == {"Fiat X19 Cars Watch", "Boats"}
    t._remember_running("Boats", False)
    assert t._remembered_running() == {"Fiat X19 Cars Watch"}


# --------------------------------------------------------------------------
# One authority for "is it actually running?", and desired state that is real
# --------------------------------------------------------------------------

def test_api_list_uses_the_merged_runtime():
    """The scheduler-only flag said running=False while the orchestrator was mid-sweep on the
    same watch — and the wrong answer propagated to the UI and the assistant."""
    src = open("web_watcher/dashboard/server.py", encoding="utf-8").read()
    assert "rt_map   = manager.runtime_map()" in src
    assert 'rt_map.get(w.name, {}).get("running"' in src


def test_start_under_orchestrator_records_intent():
    src = open("web_watcher/services.py", encoding="utf-8").read()
    assert "_remember_running(watch_name, True)" in src
    assert "joins its rotation" in src


def test_stop_under_orchestrator_is_a_real_stop():
    """Before: stop was a polite no-op while The Watcher drove — the rotation kept sweeping
    the watch the user had just been told was stopped."""
    src = open("web_watcher/services.py", encoding="utf-8").read()
    assert "_remember_running(watch_name, False)" in src
    o = open("web_watcher/orchestrator.py", encoding="utf-8").read()
    assert "_remembered_running" in o, "the rotation ignores desired state"


def test_missing_desired_state_falls_back_to_legacy():
    """A missing file means 'this install predates desired-state' — every enabled watch keeps
    running. An EMPTY set means 'the user stopped everything' and must be honoured."""
    import tempfile, pathlib
    from web_watcher.scheduler import WatchScheduler
    class T(WatchScheduler):
        def __init__(self, d): self._d = pathlib.Path(d)
        def _running_state_path(self): return self._d / "continuous_running.json"
    with tempfile.TemporaryDirectory() as d:
        t = T(d)
        assert t._remembered_running() is None          # never recorded → legacy
        t._remember_running("A", True)
        t._remember_running("A", False)
        assert t._remembered_running() == set()         # recorded-empty → honour the stop
