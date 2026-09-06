# -*- coding: utf-8 -*-
"""SHARED WATCHES — "share it with your buddy and any updates either of you make sync
across." One watch + shared_with: alerts fan out, sharers may edit/start/stop, only the
primary owner deletes/shares/un-shares, a guest can leave."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from web_watcher.config import AppConfig, NotificationsConfig, TelegramConfig, Watch
from web_watcher.dashboard import server as S


def _cfg():
    return AppConfig(
        notifications=NotificationsConfig(
            telegram=TelegramConfig(chat_id="111", allowed_chat_ids=["222", "333"])),
        watches=[Watch(name="Cars Watch", urls=["https://x?q=cars"], instruction="cars",
                       interval_minutes=30, mode="continuous",
                       owner="111", shared_with=["222"])])


def test_a_sharer_sees_and_may_use_but_never_destroy():
    cfg = _cfg()
    assert [w.name for w in S._watches_for_owner(cfg, "222")] == ["Cars Watch"]
    assert S._is_owned("Cars Watch", cfg, "222") is True          # edit/start/stop: yes
    assert S._is_primary_owner("Cars Watch", cfg, "222") is False # delete/share: no
    assert S._is_primary_owner("Cars Watch", cfg, "111") is True
    assert S._watches_for_owner(cfg, "333") == []                 # a stranger sees nothing


def test_alerts_fan_out_to_owner_and_sharers(monkeypatch):
    from datetime import datetime, timezone
    from web_watcher import notify as N
    from web_watcher.notify import NotificationPayload, send_notifications
    from web_watcher.reasoning import ReasoningResult
    sent = []
    monkeypatch.setattr(N, "send_telegram",
                        lambda payload, cfg, chat_id_override="":
                        sent.append(chat_id_override) or True)
    payload = NotificationPayload(
        watch_name="Cars Watch",
        result=ReasoningResult(found=True, summary="hit", confidence="high", link=None),
        timestamp=datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc))
    cfg = NotificationsConfig(telegram=TelegramConfig(bot_token="t", chat_id="111"))
    send_notifications(payload, cfg, use_email=False,
                       owner_chat_id="111", extra_chat_ids=["222", "222", "111"])
    assert sent == ["111", "222"]                     # everyone once, nobody twice


def test_share_verb_is_co_watching_not_a_move(monkeypatch):
    from tests.test_dashboard_turn import _mock_two_phase
    cfg = _cfg()
    cfg.watches[0].shared_with = []
    shared, moved = [], []
    monkeypatch.setattr(S, "_resolve_person",
                        lambda who, cfg, owner: ("222", "Jordan"))
    monkeypatch.setattr(S, "_perform_share",
                        lambda w, o, actor, remove=False:
                        shared.append((w, o, remove)) or (True, ""))
    monkeypatch.setattr(S, "_perform_transfer",
                        lambda w, to, actor: moved.append(w) or (True, ""))
    _mock_two_phase(monkeypatch, "Sure.", {"intent": "none"})
    out = S._complete_assistant_turn(
        "sys", [{"role": "user", "content": "share my cars watch with Jordan"}],
        cfg, "m", owner="111")
    assert shared == [("Cars Watch", "222", False)] and not moved
    assert "shared with Jordan" in out["message"] and "stays yours" in out["message"]

    # A SHARER can't share it onward; they CAN leave.
    shared.clear()
    cfg.watches[0].shared_with = ["222"]
    _mock_two_phase(monkeypatch, "Sure.", {"intent": "none"})
    monkeypatch.setattr(S, "_resolve_person", lambda who, cfg, owner: ("333", "Charlie"))
    out2 = S._complete_assistant_turn(
        "sys", [{"role": "user", "content": "share the cars watch with Charlie"}],
        cfg, "m", owner="222")
    assert not shared and "Only the owner" in out2["message"]
    monkeypatch.setattr(S, "_resolve_person", lambda who, cfg, owner: ("222", "you"))
    _mock_two_phase(monkeypatch, "Sure.", {"intent": "none"})
    out3 = S._complete_assistant_turn(
        "sys", [{"role": "user", "content": "stop sharing the cars watch with me"}],
        cfg, "m", owner="222")
    assert shared == [("Cars Watch", "222", True)]
    assert "you've left" in out3["message"]


def test_perform_share_adds_notifies_and_removes(monkeypatch, tmp_path):
    import contextlib
    cfg = _cfg()
    cfg.watches[0].shared_with = []
    monkeypatch.setattr("web_watcher.config.load", lambda path=None: cfg)
    monkeypatch.setattr("web_watcher.config.save", lambda c, path=None: None)
    monkeypatch.setattr("web_watcher.config.lock",
                        lambda: contextlib.nullcontext(), raising=False)
    notes = []
    monkeypatch.setattr("web_watcher.notify.send_plain_telegram",
                        lambda msg, notif, chat_id_override=None:
                        notes.append((chat_id_override, msg)) or True)
    ok, err = S._perform_share("Cars Watch", "222", "111")
    assert ok and cfg.watches[0].shared_with == ["222"]
    assert notes and notes[0][0] == "222"
    assert "shared the watch" in notes[0][1] and "stop sharing" in notes[0][1]
    assert S._perform_share("Cars Watch", "222", "111") == (False, "it's already shared with them")
    assert S._perform_share("Cars Watch", "999", "111")[0] is False   # stranger: refused
    ok2, _ = S._perform_share("Cars Watch", "222", "111", remove=True)
    assert ok2 and cfg.watches[0].shared_with == []
