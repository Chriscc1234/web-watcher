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


def _cfg_one_macgregor(owner="111"):
    ws = [Watch(name="Anacortes MacGregor Sailboats Watch", urls=["https://x"], instruction="x",
                interval_minutes=30, owner=owner)]
    return AppConfig(notifications=NotificationsConfig(telegram=TelegramConfig(chat_id="111")),
                     watches=ws)


def test_named_lifecycle_action_helper():
    # A plain "start/stop the <name> watch" is decided in code — the 14b whiffed on it (it answered
    # with an edit card), and the bare handler then asked the admin "whole Watcher or just yours?".
    cfg = _cfg_one_macgregor("111")
    NAME = "Anacortes MacGregor Sailboats Watch"
    assert S._named_lifecycle_action("Start up the macgregor watch", cfg, "111") == {"action": "start", "name": NAME}
    assert S._named_lifecycle_action("stop the macgregor watch", cfg, "111") == {"action": "stop", "name": NAME}
    assert S._named_lifecycle_action("Macgregor watch start", cfg, "111") == {"action": "start", "name": NAME}
    # Not a command / not scoped to one named owned watch → leave it for the other handlers.
    assert S._named_lifecycle_action("is the macgregor watch running?", cfg, "111") is None   # question
    assert S._named_lifecycle_action("start everything", cfg, "111") is None                  # program
    assert S._named_lifecycle_action("start all my watches", cfg, "111") is None              # all-mine
    assert S._named_lifecycle_action("stop and restart the macgregor watch", cfg, "111") is None  # restart
    assert S._named_lifecycle_action("start up the macgregor watch", cfg, "999") is None      # not owner


def test_named_watch_start_fires_even_when_llm_emits_nothing(monkeypatch):
    # The real bug: 14b produced no action, so "Start up the macgregor watch" got treated as a bare
    # start and the admin was asked to disambiguate instead of the watch just starting.
    out = _turn(monkeypatch, "Start up the macgregor watch", "111", _cfg_one_macgregor("111"))
    assert out["watch_actions"] == [{"action": "start", "name": "Anacortes MacGregor Sailboats Watch"}]
    assert "whole" not in out["message"].lower()          # did NOT fall through to the bare question


def test_named_watch_running_question_takes_no_action(monkeypatch):
    out = _turn(monkeypatch, "Is the macgregor watch running?", "111", _cfg_one_macgregor("111"))
    assert not out["watch_actions"]


def test_lookup_limit_counts_a_leading_number():
    # "5 most recent" put the count BEFORE the noun, which the keyword-first regex missed — it used
    # to collapse to 1 (singular rule) or the model's guessed 10.
    assert S._lookup_limit("The 5 most recent", default=None) == 5
    assert S._lookup_limit("Let's see the 5 most recent macgregor matches", default=None) == 5
    assert S._lookup_limit("show me 3 recent ones", default=None) == 3
    assert S._lookup_limit("top 20", default=None) == 20
    assert S._lookup_limit("the latest match", default=None) == 1
    assert S._lookup_limit("show me the matches", default=None) is None


def test_global_status_detection_and_counts_only_render():
    assert S._is_global_status_request("Is the whole watcher running?")
    assert S._is_global_status_request("Globally how many watches are there?")
    assert S._is_global_status_request("are any other watches running?")
    assert not S._is_global_status_request("what watches do I have")
    assert not S._is_global_status_request("start up the macgregor watch")
    # Counts only — never another user's watch titles (a buddy must not learn what others watch).
    cfg = _cfg_with("111", "999", "111")                  # 3 watches across two people, all enabled
    mgr = MagicMock(); mgr.is_paused.return_value = False
    msg = S._render_global_running(cfg, mgr)
    assert "3 of 3" in msg
    assert "W0" not in msg and "W1" not in msg and "W2" not in msg
    mgr.is_paused.return_value = True
    assert "paused" in S._render_global_running(cfg, mgr).lower()


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


# ── level 3: ONE PERSON'S whole set, from the server console ──────────────────────
#
# The server app groups watches by user and gives each group a start/stop button. The lever is
# `enabled` (not the running loops) because when the orchestrator is driving it services every
# ENABLED continuous watch — stopping a loop alone would be undone on its next pass.

@pytest.fixture()
def group_app(monkeypatch, tmp_path):
    """A client whose config lives in a temp file, so the endpoint's save() is real but isolated."""
    cfg = AppConfig(watches=[
        Watch(name="Buddy A", urls=["https://a"], instruction="a", interval_minutes=30,
              mode="continuous", owner="555", enabled=True),
        Watch(name="Buddy B", urls=["https://b"], instruction="b", interval_minutes=30,
              mode="continuous", owner="555", enabled=False),     # deliberately OFF
        Watch(name="Mine", urls=["https://c"], instruction="c", interval_minutes=30,
              owner="", enabled=True),
    ])
    monkeypatch.setattr(S, "_SUSPENDED_PATH", tmp_path / "owner_suspended.json")
    import web_watcher.config as C
    monkeypatch.setattr(C, "load", lambda: cfg)
    monkeypatch.setattr(C, "save", lambda c: None)
    return TestClient(create_app(MagicMock())), cfg


def _by(cfg, name):
    return next(w for w in cfg.watches if w.name == name)


def test_group_stop_turns_off_only_that_persons_watches(group_app):
    client, cfg = group_app
    r = client.post("/api/owners/action", json={"owner": "555", "action": "stop"}).json()
    assert r["changed"] == 1 and r["names"] == ["Buddy A"]     # B was already off
    assert _by(cfg, "Buddy A").enabled is False
    assert _by(cfg, "Mine").enabled is True                    # someone else's group untouched


def test_group_start_restores_only_what_the_stop_turned_off(group_app):
    """The whole point of remembering: a watch the person deactivated on purpose must NOT come
    back just because the group was stopped and started."""
    client, cfg = group_app
    client.post("/api/owners/action", json={"owner": "555", "action": "stop"})
    r = client.post("/api/owners/action", json={"owner": "555", "action": "start"}).json()
    assert r["changed"] == 1
    assert _by(cfg, "Buddy A").enabled is True
    assert _by(cfg, "Buddy B").enabled is False                # stayed off, as intended


def test_group_start_with_nothing_remembered_enables_the_whole_group(group_app):
    client, cfg = group_app
    _by(cfg, "Buddy A").enabled = False
    r = client.post("/api/owners/action", json={"owner": "555", "action": "start"}).json()
    assert r["changed"] == 2                                   # explicit "Start all" on a visible group
    assert _by(cfg, "Buddy A").enabled and _by(cfg, "Buddy B").enabled


def test_group_action_reports_the_master_switch(group_app):
    client, _ = group_app
    r = client.post("/api/owners/action", json={"owner": "555", "action": "start"}).json()
    assert "paused" in r          # so the UI can warn that nothing sweeps while paused


def test_unknown_group_action_and_unknown_owner(group_app):
    client, _ = group_app
    assert client.post("/api/owners/action", json={"owner": "555", "action": "nope"}).status_code == 400
    r = client.post("/api/owners/action", json={"owner": "nobody", "action": "stop"}).json()
    assert r["ok"] is True and r["changed"] == 0


# ── "stop my watches" has to actually stop them ──────────────────────────────────
# Live: the bot replied "Stoping all 2 of your watches" and both kept running. Stopping a
# per-watch loop is a no-op whenever the orchestrator is driving — it owns every ENABLED
# continuous watch and sweeps it again on the next pass. `enabled` is the lever that works in
# both execution modes, and it's the state the Active/Inactive lists already show.

@pytest.fixture()
def stop_app(monkeypatch):
    cfg = AppConfig(watches=[
        Watch(name="Boats", urls=["https://x"], instruction="boats", interval_minutes=30,
              mode="continuous", enabled=True, owner="555"),
        Watch(name="Scheduled one", urls=["https://y"], instruction="cars", interval_minutes=30,
              enabled=True, owner="555"),
    ])
    import web_watcher.config as C
    monkeypatch.setattr(C, "load", lambda: cfg)
    monkeypatch.setattr(C, "save", lambda c: None)
    return TestClient(create_app(MagicMock())), cfg


def _w(cfg, name):
    return next(x for x in cfg.watches if x.name == name)


def test_stopping_a_continuous_watch_disables_it(stop_app):
    client, cfg = stop_app
    r = client.post("/api/watches/Boats/action", json={"action": "stop"}).json()
    assert r["ok"] is True and r["enabled"] is False
    assert _w(cfg, "Boats").enabled is False          # the driver will not pick it up again


def test_stopping_a_scheduled_watch_still_disables_it(stop_app):
    client, cfg = stop_app
    client.post("/api/watches/Scheduled one/action", json={"action": "stop"})
    assert _w(cfg, "Scheduled one").enabled is False


def test_starting_it_again_turns_it_back_on(stop_app):
    client, cfg = stop_app
    client.post("/api/watches/Boats/action", json={"action": "stop"})
    client.post("/api/watches/Boats/action", json={"action": "start"})
    assert _w(cfg, "Boats").enabled is True


def test_stop_is_idempotent(stop_app):
    client, cfg = stop_app
    client.post("/api/watches/Boats/action", json={"action": "stop"})
    assert client.post("/api/watches/Boats/action", json={"action": "stop"}).json()["ok"] is True
    assert _w(cfg, "Boats").enabled is False
