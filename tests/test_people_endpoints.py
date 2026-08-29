"""The People endpoints — person management for a server runner, not just the developer.

Session history: Jordan's nickname ("Nameless (Jordan)") was set BY HAND through a side
effect of the send endpoint, and his watch was transferred by a raw API call. Anyone else
running this server had no way to do either. GET /api/people lists everyone the bot talks
to; POST /api/people/{chat_id}/label sets/clears a nickname; the transfer endpoint already
existed — the dashboard now has buttons for all three.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from web_watcher.config import AppConfig, NotificationsConfig, TelegramConfig, Watch
from web_watcher.dashboard import server as S
from web_watcher.dashboard.server import create_app


@pytest.fixture
def app(monkeypatch, tmp_path):
    cfg = AppConfig(
        notifications=NotificationsConfig(telegram=TelegramConfig(
            chat_id="111", allowed_chat_ids=["222", "333"])),
        watches=[
            Watch(name="Jordan's cars", urls=["https://x"], instruction="x",
                  interval_minutes=30, owner="222"),
            Watch(name="Mine", urls=["https://y"], instruction="y",
                  interval_minutes=30, owner="111"),
        ])
    import web_watcher.config as C
    monkeypatch.setattr(C, "load", lambda: cfg)
    monkeypatch.setattr(C, "save", lambda c: None)
    # Redirect the name/label stores into tmp so tests never touch real data.
    names = tmp_path / "owner_names.json"
    names.write_text(json.dumps({"222": "Nameless"}), encoding="utf-8")
    monkeypatch.setattr(S, "_OWNER_NAMES_PATH", names)
    monkeypatch.setattr(S, "_OWNER_LABELS_PATH", tmp_path / "owner_labels.json")
    return TestClient(create_app(MagicMock()))


def test_people_lists_main_and_allowed_with_watch_counts(app):
    people = app.get("/api/people").json()
    by_id = {p["chat_id"]: p for p in people}
    assert set(by_id) == {"111", "222", "333"}
    assert by_id["111"]["is_main"] is True
    assert by_id["222"]["is_main"] is False
    assert by_id["222"]["watches"] == 1
    assert by_id["333"]["watches"] == 0
    assert by_id["222"]["name"] == "Nameless"          # what Telegram reports


def test_label_set_shows_both_names_and_clear_restores(app):
    r = app.post("/api/people/222/label", json={"label": "Jordan"})
    assert r.status_code == 200
    # The display keeps the real profile name AND the nickname — recognisable both ways.
    assert r.json()["display"] == "Nameless (Jordan)"
    assert {p["chat_id"]: p["label"] for p in app.get("/api/people").json()}["222"] == "Jordan"

    r = app.post("/api/people/222/label", json={"label": ""})    # empty clears
    assert r.status_code == 200
    assert r.json()["display"] == "Nameless"


def test_label_rejects_unknown_people_and_long_names(app):
    assert app.post("/api/people/999/label", json={"label": "X"}).status_code == 404
    assert app.post("/api/people/222/label",
                    json={"label": "x" * 61}).status_code == 400
