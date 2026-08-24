"""The Chats admin console: the per-person conversation index, owner-name memory, and the
Telegram access-request store (a stranger is parked here for one-click approval, never let in
automatically). Pure filesystem logic — no server needed. See dashboard/server.py."""

from __future__ import annotations

import json

import pytest

from web_watcher.config import AppConfig, Watch
from web_watcher.dashboard import server as S


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
