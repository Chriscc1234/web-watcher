"""The last of the developer-only abilities, promoted to real tools.

"fix it all": watch RENAME was so unsafe the editor disabled the name field (name-keyed
seen-history → a rename re-alerted everything the watch had ever found — why the FB watch
stayed misnamed for weeks); REMOVING a person meant editing config by hand; the sweep-issue
log lived in a .jsonl only the developer read. Each now has an endpoint, a migration where
one was needed, and a button.
"""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from web_watcher import issues, storage
from web_watcher.config import AppConfig, NotificationsConfig, TelegramConfig, Watch
from web_watcher.dashboard import server as S
from web_watcher.dashboard.server import create_app


# ── storage.rename_watch: the whole recorded life travels ────────────────────────

def _db(tmp_path):
    p = tmp_path / "t.db"
    storage.init_db(p)
    return p


def test_rename_migrates_name_keyed_tables(tmp_path):
    db = _db(tmp_path)
    storage.save_seen_listing("Old", "cl:1", "2026-01-01", "a boat", "https://x", db_path=db)
    storage.save_seen_listing("Other", "cl:2", "2026-01-01", "keep me", "https://y", db_path=db)

    counts = storage.rename_watch("Old", "New", watch_id="stable-id", db_path=db)

    assert counts["seen_listings"] == 1
    assert storage.has_seen_listing("New", "cl:1", db_path=db)       # dedup history intact...
    assert not storage.has_seen_listing("Old", "cl:1", db_path=db)
    assert storage.has_seen_listing("Other", "cl:2", db_path=db)     # ...and nobody else touched


def test_rename_migrates_id_columns_only_for_pre_id_watches(tmp_path):
    """Watches created before stable ids keyed observations by NAME in the watch_id column —
    those migrate; a watch with a real id must NOT have its id rewritten."""
    db = _db(tmp_path)
    conn = storage._connect(db)
    with conn:
        conn.execute("INSERT INTO observations (watch_id, watch_name, listing_key) "
                     "VALUES ('Old', 'Old', 'k1')")          # pre-id: name in both columns
        conn.execute("INSERT INTO observations (watch_id, watch_name, listing_key) "
                     "VALUES ('stable-id', 'Old', 'k2')")    # modern: stable id
    conn.close()

    storage.rename_watch("Old", "New", watch_id=None, db_path=db)    # pre-id watch
    conn = storage._connect(db)
    rows = {r["listing_key"]: (r["watch_id"], r["watch_name"])
            for r in conn.execute("SELECT * FROM observations")}
    conn.close()
    assert rows["k1"] == ("New", "New")
    assert rows["k2"] == ("stable-id", "New")    # display name follows; the stable id never


# ── the PUT endpoint: the editor's name field finally works ──────────────────────

@pytest.fixture
def rename_app(monkeypatch, tmp_path):
    cfg = AppConfig(
        notifications=NotificationsConfig(telegram=TelegramConfig(chat_id="111")),
        watches=[
            Watch(name="FB DRYRUN (logged out)", urls=["https://x"], instruction="x",
                  interval_minutes=30, mode="continuous"),
            Watch(name="Taken", urls=["https://y"], instruction="y", interval_minutes=30),
        ])
    import web_watcher.config as C
    monkeypatch.setattr(C, "load", lambda: cfg)
    monkeypatch.setattr(C, "save", lambda c: None)
    migrated = {}
    monkeypatch.setattr(storage, "rename_watch",
                        lambda old, new, watch_id=None, db_path=None:
                        migrated.setdefault("args", (old, new, watch_id)) or {})
    manager = MagicMock()
    return TestClient(create_app(manager)), cfg, migrated, manager


def test_put_with_changed_name_renames_and_migrates(rename_app):
    client, cfg, migrated, manager = rename_app
    r = client.put("/api/watches/FB DRYRUN (logged out)",
                   json={"name": "Facebook Marketplace"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "name": "Facebook Marketplace",
                        "renamed_from": "FB DRYRUN (logged out)"}
    assert cfg.watches[0].name == "Facebook Marketplace"
    assert migrated["args"][0] == "FB DRYRUN (logged out)"
    assert migrated["args"][1] == "Facebook Marketplace"
    manager.rename_continuous.assert_called_once_with(
        "FB DRYRUN (logged out)", "Facebook Marketplace")


def test_put_same_name_is_not_a_rename(rename_app):
    """Chat update cards carry name == the grounded watch name — that must never trigger
    the rename machinery."""
    client, cfg, migrated, manager = rename_app
    r = client.put("/api/watches/Taken", json={"name": "Taken", "instruction": "z"})
    assert r.status_code == 200 and r.json()["renamed_from"] is None
    assert "args" not in migrated
    manager.rename_continuous.assert_not_called()


def test_put_rename_collision_is_rejected(rename_app):
    client, cfg, migrated, _ = rename_app
    r = client.put("/api/watches/FB DRYRUN (logged out)", json={"name": "Taken"})
    assert r.status_code == 400
    assert cfg.watches[0].name == "FB DRYRUN (logged out)"   # untouched
    assert "args" not in migrated


# ── desired-state follows the rename ─────────────────────────────────────────────

def test_rename_continuous_migrates_the_desired_set(tmp_path):
    from web_watcher.scheduler import WatchScheduler
    from web_watcher.services import ServiceManager
    s = WatchScheduler.__new__(WatchScheduler)
    s._running_state_path = lambda: tmp_path / "cr.json"
    m = ServiceManager()
    m._scheduler = s

    s._remember_running("Old", True)
    m.rename_continuous("Old", "New")
    assert s._remembered_running() == {"New"}

    m.rename_continuous("NotRunning", "Whatever")            # absent name: no-op
    assert s._remembered_running() == {"New"}


# ── removing a person ────────────────────────────────────────────────────────────

@pytest.fixture
def people_app(monkeypatch, tmp_path):
    cfg = AppConfig(
        notifications=NotificationsConfig(telegram=TelegramConfig(
            chat_id="111", allowed_chat_ids=["222", "333"])),
        watches=[
            Watch(name="Theirs A", urls=["https://x"], instruction="x",
                  interval_minutes=30, owner="222", enabled=True),
            Watch(name="Theirs B", urls=["https://y"], instruction="y",
                  interval_minutes=30, owner="222", enabled=False),
            Watch(name="Mine", urls=["https://z"], instruction="z",
                  interval_minutes=30, owner="111", enabled=True),
        ])
    import web_watcher.config as C
    monkeypatch.setattr(C, "load", lambda: cfg)
    monkeypatch.setattr(C, "save", lambda c: None)
    monkeypatch.setattr(S, "_remove_access_request", lambda cid: None)
    manager = MagicMock()
    return TestClient(create_app(manager)), cfg, manager


def test_remove_person_revokes_and_disables_only_their_watches(people_app):
    client, cfg, manager = people_app
    r = client.delete("/api/people/222")
    assert r.status_code == 200
    assert r.json()["watches_disabled"] == ["Theirs A"]      # B was already off
    assert cfg.notifications.telegram.allowed_chat_ids == ["333"]
    by = {w.name: w for w in cfg.watches}
    assert by["Theirs A"].enabled is False
    assert by["Theirs A"].owner == "222"                     # kept — re-approval restores
    assert by["Mine"].enabled is True                        # someone else's untouched
    manager.restart_telegram.assert_called_once()


def test_remove_person_guards(people_app):
    client, _, _ = people_app
    assert client.delete("/api/people/111").status_code == 400     # the admin's own chat
    assert client.delete("/api/people/999").status_code == 404     # not on the list


# ── the issue log's clear button ─────────────────────────────────────────────────

def test_clear_issues(tmp_path):
    issues.record_issue("stuck", "W", "d1", data_dir=tmp_path)
    issues.record_issue("blocked", "W", "d2", data_dir=tmp_path)
    assert len(issues.issues(data_dir=tmp_path)) == 2
    assert issues.clear_issues(data_dir=tmp_path) == 2
    assert issues.issues(data_dir=tmp_path) == []
    assert issues.clear_issues(data_dir=tmp_path) == 0       # idempotent
