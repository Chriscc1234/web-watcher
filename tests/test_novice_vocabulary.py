"""The novice phrasebook — people who were HANDED the bot don't know our lingo.

The user's observation, verbatim: "it's easy for me since we're the ones creating the
vocabulary but for others it might be hard to learn the lingo." We invented "watch",
"sweep", "match"; a buddy says "any luck?", "quit sending me stuff", "is it working?".
This file runs the realistic-phrasing battery through every deterministic layer and pins
both halves: what MUST be caught, and what must NOT misfire.

The welcome message literally teaches "what am I watching?" — before this review, that
exact phrase fell through to the 14b. Anything onboarding advertises must be guaranteed.
"""

from __future__ import annotations

import pytest

from web_watcher.dashboard import server as S


# ── stop: how people actually tell a texter to knock it off ──────────────────────

@pytest.mark.parametrize("msg", [
    "Please stop watching", "stop", "turn it off", "pause it",
    "stop sending me stuff", "quit sending me stuff", "quiet down", "unsubscribe",
    "mute", "leave me alone", "knock it off", "stop texting me",
    "don't message me anymore", "no more alerts", "turn off the notifications",
    "cancel my alerts", "enough already", "give it a rest",
])
def test_novice_stop_phrasings_classify_as_pause(msg):
    verb, scope = S._classify_lifecycle(msg)
    assert verb == "pause", msg


@pytest.mark.parametrize("msg", [
    "start", "keep looking", "keep going", "keep searching", "go again",
    "look again", "turn it back on", "start it up again",
])
def test_novice_start_phrasings_classify_as_resume(msg):
    verb, scope = S._classify_lifecycle(msg)
    assert verb == "resume", msg


# ── status: no "watch" vocabulary required ───────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "what am i watching", "is it working?", "are you still looking?",
    "what are you searching for?", "what am i signed up for?", "you still there?",
    "is it still running", "whats the status", "still watching?",
])
def test_novice_status_phrasings(msg):
    assert S._is_watch_status_request(msg), msg


# ── finds: checking in with a friend who's keeping an eye out ────────────────────

@pytest.mark.parametrize("msg", [
    "any luck?", "any news?", "any updates?", "whats new", "see anything?",
    "did you catch anything", "anything?", "got anything?", "find anything?",
])
def test_novice_finds_phrasings_are_lookups(msg):
    assert S._is_lookup_request(msg), msg


# ── help: a lost novice, whole-message forms ─────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "help", "what do i do", "im confused", "i don't understand", "huh?",
    "what should i say", "instructions", "what are you?", "what now",
])
def test_lost_novice_gets_the_explainer(msg):
    assert S._is_help_request(msg), msg


# ── the misfire guards: loose matching must not eat real requests ────────────────

def test_price_constraint_is_not_a_stop():
    # "no more" needs an alerts-ish object — "$5000" is a budget, not a shutdown.
    verb, scope = S._classify_lifecycle("no more than $5000 please")
    assert verb is None


def test_status_question_is_not_the_help_explainer():
    # "\bwhat are you\b" unanchored used to swallow this into the generic explainer.
    assert not S._is_help_request("what are you searching for?")
    assert S._is_watch_status_request("what are you searching for?")


def test_specific_confusion_goes_to_the_model():
    # Whole-message confusion → explainer; confusion ABOUT something → the model.
    assert not S._is_help_request("I don't understand the price part")


def test_cancel_that_mid_conversation_is_not_a_shutdown():
    verb, scope = S._classify_lifecycle("cancel that")
    assert verb is None


def test_quiet_inside_a_create_request_is_not_a_stop():
    verb, scope = S._classify_lifecycle("can you watch for a quiet dishwasher")
    assert verb is None


def test_bare_anything_is_a_lookup_but_not_inside_sentences():
    assert S._is_lookup_request("anything?")
    assert not S._is_lookup_request("do you need anything from me")


# ── whatever onboarding advertises must actually be guaranteed ───────────────────

def test_welcome_taught_phrases_are_deterministic():
    assert S._is_watch_status_request("what am I watching?")
    assert S._is_lookup_request("any luck?")
    assert S._classify_lifecycle("stop")[0] == "pause"
    assert S._is_help_request("help")


def test_the_bare_noun_is_the_tersest_status_question():
    """Sent live: "Watches" — improvised prose, no cards. The lone noun IS the question."""
    for t in ("Watches", "watches?", "my watches", "watch"):
        assert S._is_watch_status_request(t), t
        assert not S._is_lookup_request(t), t
    # ...but the noun inside a sentence still needs the normal patterns to decide.
    assert not S._is_watch_status_request("that watch band is nice")
