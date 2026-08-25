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
