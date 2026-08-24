"""Start/stop semantics — the two levels the user talks to:

  1. The WHOLE Watcher (a global master switch) — admin-only.
  2. A person's OWN watches — owner-scoped.

These are classified deterministically (a start/stop of everything is too consequential to leave
to the 14b), and a bare "stop" from the admin asks which they mean. See dashboard/server.py and
services.ServiceManager.pause_all/resume_all."""

from __future__ import annotations

import types
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from web_watcher.config import (AppConfig, NotificationsConfig, TelegramConfig, Watch)
from web_watcher.dashboard import server as S
from web_watcher.dashboard.server import create_app


# ── the classifier ───────────────────────────────────────────────────────────────

def test_classify_program_vs_scope():
    assert S._classify_lifecycle("stop everything") == ("pause", "program")
    assert S._classify_lifecycle("pause the whole watcher") == ("pause", "program")
    assert S._classify_lifecycle("resume the watcher") == ("resume", "program")
    assert S._classify_lifecycle("stop all my watches") == ("pause", "all_mine")
    assert S._classify_lifecycle("stop") == ("pause", "bare")
    assert S._classify_lifecycle("start") == ("resume", "bare")
    assert S._classify_lifecycle("how are my watches doing?") == (None, None)


def test_admin_owner_check():
    cfg = AppConfig(notifications=NotificationsConfig(telegram=TelegramConfig(chat_id="111")))
    assert S._is_admin_owner(None, cfg) is True          # desktop dashboard
    assert S._is_admin_owner("111", cfg) is True          # the main Telegram chat
    assert S._is_admin_owner("999", cfg) is False         # a buddy


# ── lifecycle injection inside a full turn (LLM stubbed out) ──────────────────────

def _turn(monkeypatch, text, owner, cfg, reply="ok"):
    monkeypatch.setattr(S, "_chat_reply_natural",
                        lambda system, messages, model, force_local=False: (reply, 1, 1, 1))
    monkeypatch.setattr(S, "_extract_watch_action", lambda *a, **k: {})   # no LLM-proposed action
    return S._complete_assistant_turn("sys", [{"role": "user", "content": text}],
                                      cfg, "m", owner=owner)


def _cfg_with(*owners):
    ws = [Watch(name=f"W{i}", urls=["https://x"], instruction="x", interval_minutes=30, owner=o)
          for i, o in enumerate(owners)]
    return AppConfig(notifications=NotificationsConfig(telegram=TelegramConfig(chat_id="111")),
                     watches=ws)


def test_admin_stop_everything_sets_program_pause(monkeypatch):
    out = _turn(monkeypatch, "stop everything", None, _cfg_with("555"))
    assert out["program_action"] == "pause"
    assert not out["watch_suggestions"] and not out["watch_actions"]


def test_buddy_cannot_pause_the_whole_watcher(monkeypatch):
    out = _turn(monkeypatch, "stop everything", "555", _cfg_with("555"))
    assert out["program_action"] is None
    assert "owner" in out["message"].lower()              # told only the owner can


def test_buddy_bare_stop_scopes_to_their_own_watches(monkeypatch):
    cfg = _cfg_with("555", "999", "555")                  # two owned by 555, one by 999
    out = _turn(monkeypatch, "stop", "555", cfg)
    assert out["program_action"] is None
    names = {a["name"] for a in out["watch_actions"]}
    assert names == {"W0", "W2"} and all(a["action"] == "stop" for a in out["watch_actions"])


def test_admin_bare_stop_asks_which(monkeypatch):
    out = _turn(monkeypatch, "stop", None, _cfg_with("555"))
    assert out["program_action"] is None and not out["watch_actions"]
    assert "whole" in out["message"].lower() and "?" in out["message"]


def test_all_my_watches_start(monkeypatch):
    out = _turn(monkeypatch, "start all my watches", "555", _cfg_with("555", "555"))
    assert {a["name"] for a in out["watch_actions"]} == {"W0", "W1"}
    assert all(a["action"] == "start" for a in out["watch_actions"])


# ── the endpoints ─────────────────────────────────────────────────────────────────

def test_watcher_pause_resume_endpoints_hit_the_manager():
    manager = MagicMock()
    manager.pause_all.return_value = None
    manager.resume_all.return_value = False
    client = TestClient(create_app(manager))
    assert client.post("/api/watcher/pause").json()["paused"] is True
    assert manager.pause_all.called
    assert client.post("/api/watcher/resume").json()["running"] is True
    assert manager.resume_all.called


def test_watcher_status_endpoint():
    manager = MagicMock()
    manager.watcher_status.return_value = {"running": True, "paused": False,
                                           "driver_running": False, "continuous_running": []}
    client = TestClient(create_app(manager))
    assert client.get("/api/watcher/status").json()["running"] is True
