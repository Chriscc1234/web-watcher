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
