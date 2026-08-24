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


# ── per-person check-in cadence, set from the bot ────────────────────────────────

def test_classify_checkin_parses_cadence():
    assert S._classify_checkin("check in every 6 hours") == 6.0
    assert S._classify_checkin("update me twice a day") == 12.0
    assert S._classify_checkin("ping me once a day") == 24.0
    assert S._classify_checkin("stop checking in") == 0.0
    assert S._classify_checkin("no more check-ins") == 0.0
    assert S._classify_checkin("how are my watches?") is None      # not a settings request


def test_checkin_pref_is_per_person(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "_CHECKIN_PREFS_PATH", tmp_path / "checkin_prefs.json")
    S._set_checkin_pref("111", 6.0)
    S._set_checkin_pref("222", 0.0)
    prefs = S._load_checkin_prefs()
    assert prefs == {"111": 6.0, "222": 0.0}


# ── a bare yes must not spawn a watch (the "show me the matches" hijack) ─────────

def test_bare_confirmation_detection():
    for yes in ("Yes", "no", "ok", "sure", "yep."):
        assert S._is_bare_confirmation(yes) is True, yes
    for not_bare in ("yes please show me the matches", "watch for trucks", "ok now find a boat"):
        assert S._is_bare_confirmation(not_bare) is False, not_bare


# ── the owner on their phone is an admin (sees every watch) ──────────────────────

def test_admin_on_telegram_sees_all_watches():
    cfg = _cfg_with("999", "")            # one owned by a buddy, one unassigned
    assert len(S._watches_for_owner(cfg, "111")) == 2      # 111 == configured chat_id (admin)
    assert len(S._watches_for_owner(cfg, "999")) == 1      # a buddy sees only their own
    assert S._is_owned("W1", cfg, "111") is True           # admin may act on an unassigned watch
    assert S._is_owned("W1", cfg, "999") is False


# ── no stale second instance (the "6 copies running" bug) ───────────────────────

def test_second_instance_does_not_start_a_telegram_bridge(monkeypatch):
    """Two live instances = two bridges racing for the same bot, and an OUTDATED build can answer
    messages (that's how literal <b> tags kept appearing after the fix shipped). A second instance
    must decline to start a bridge."""
    from web_watcher.services import ServiceManager
    m = ServiceManager()
    monkeypatch.setattr(m, "_another_instance_owns_the_port", lambda: True)
    called = []
    monkeypatch.setattr("web_watcher.telegram_bot.TelegramBridge",
                        lambda *a, **k: called.append(1))
    m._start_telegram()
    assert called == []                      # declined


def test_restart_arms_a_hard_exit(monkeypatch):
    """A restart must guarantee this process dies, or the launcher's new instance stacks on top
    of a still-running old one."""
    from web_watcher.services import ServiceManager
    m = ServiceManager()
    armed = {}
    class _T:
        def __init__(self, delay, fn): armed["delay"] = delay
        def start(self): armed["started"] = True
        daemon = True
    monkeypatch.setattr("threading.Timer", _T)
    m._force_exit_soon(grace=5.0)
    assert armed.get("started") is True and armed.get("delay") == 5.0
