"""Regression tests for the watch create/update endpoints.

The 0.22.x "error 500 on create" bug: chat suggestions often propose a watch with no
interval/cron at all → Watch's must_have_schedule model_validator raises → the handler
did `HTTPException(400, detail=exc.errors())`, but pydantic v2 embeds the raw ValueError
object under ctx in errors(), FastAPI can't JSON-serialize it, and the intended 400
exploded into a 500. Fixed by (a) `_validation_detail` (JSON-safe error rendering) and
(b) `_backfill_schedule` (schedule-less creates default to every 30 min instead of
erroring at the user).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from web_watcher.dashboard.server import (
    _backfill_schedule,
    _normalize_turn,
    _validation_detail,
    create_app,
)


@pytest.fixture()
def client():
    manager = MagicMock()
    app = create_app(manager)
    return TestClient(app)


def _cleanup(client, name):
    client.delete(f"/api/watches/{name}")


def _chat_client(monkeypatch, tmp_path, turn_result):
    """A TestClient wired for the Watcher chat endpoint: history isolated to a temp file, the
    model turn + preamble stubbed to return `turn_result`."""
    from web_watcher.dashboard import server as S
    from web_watcher.config import AppConfig
    monkeypatch.setattr(S, "_WATCHER_HISTORY_PATH", tmp_path / "watcher_history.json")
    monkeypatch.setattr(S, "_load_cfg", lambda: AppConfig())
    monkeypatch.setattr(S, "_build_watches_context", lambda cfg, manager, owner=None: "(watches)")
    monkeypatch.setattr(S, "_complete_assistant_turn", lambda *a, **k: dict(turn_result))
    manager = MagicMock()
    manager.oversight_snapshot.return_value = {"entries": []}
    return TestClient(create_app(manager)), S


def _cred_client(monkeypatch):
    """A TestClient with an in-memory AppConfig for the credentials endpoints — no disk, no
    network, isolated per test (the suite's temp config is session-wide and would leak state)."""
    from web_watcher.dashboard import server as S
    from web_watcher import config as C
    from web_watcher.config import AppConfig
    state = {"cfg": AppConfig()}
    monkeypatch.setattr(S, "_load_cfg", lambda: state["cfg"])
    monkeypatch.setattr(C, "load", lambda path=None: state["cfg"])
    monkeypatch.setattr(C, "save", lambda cfg, path=None: state.__setitem__("cfg", cfg))
    return TestClient(create_app(MagicMock())), state


def test_credentials_default_shape(monkeypatch):
    client, _ = _cred_client(monkeypatch)
    d = client.get("/api/credentials").json()
    assert {"telegram", "email", "anthropic", "spend"} <= set(d)
    assert d["anthropic"]["judge_enabled"] is False
    assert d["telegram"]["token_set"] is False


def test_credentials_save_masks_secrets_and_enables_judge(monkeypatch):
    client, _ = _cred_client(monkeypatch)
    r = client.post("/api/credentials", json={
        "telegram": {"chat_id": "12345", "bot_token": "111:SECRET"},
        "anthropic": {"api_key": "sk-ant-SECRET", "judge_enabled": True, "judge_model": "claude-haiku-4-5"},
    })
    assert r.status_code == 200 and r.json().get("ok")
    d = client.get("/api/credentials").json()
    assert d["telegram"]["chat_id"] == "12345" and d["telegram"]["token_set"] is True
    assert d["anthropic"]["judge_enabled"] is True and d["anthropic"]["key_set"] is True
    # Secrets are NEVER echoed back in any field.
    assert "111:SECRET" not in str(d) and "sk-ant-SECRET" not in str(d)


def test_credentials_blank_secret_keeps_existing(monkeypatch):
    client, state = _cred_client(monkeypatch)
    client.post("/api/credentials", json={"telegram": {"bot_token": "keep-me", "chat_id": "9"}})
    client.post("/api/credentials", json={"telegram": {"bot_token": "", "chat_id": "9"}})   # blank
    assert state["cfg"].notifications.telegram.bot_token == "keep-me"


def test_credentials_disable_judge_removes_route(monkeypatch):
    client, state = _cred_client(monkeypatch)
    client.post("/api/credentials", json={"anthropic": {"api_key": "sk", "judge_enabled": True}})
    assert "judge" in state["cfg"].models.cloud.roles
    client.post("/api/credentials", json={"anthropic": {"judge_enabled": False}})
    assert "judge" not in state["cfg"].models.cloud.roles


def test_telegram_test_requires_credentials(monkeypatch):
    client, _ = _cred_client(monkeypatch)
    r = client.post("/api/telegram/test", json={})       # nothing configured → no network attempt
    assert r.status_code == 200 and r.json()["ok"] is False


def test_chat_turn_is_persisted(monkeypatch, tmp_path):
    """A normal Watcher chat turn saves BOTH the user message and the reply to history."""
    client, S = _chat_client(monkeypatch, tmp_path,
                             {"message": "Sure, on it.", "raw": "Sure, on it."})
    r = client.post("/api/oversight/chat",
                    json={"messages": [{"role": "user", "content": "watch trucks"}]})
    assert r.status_code == 200
    saved = S._load_watcher_history()
    assert [m["role"] for m in saved] == ["user", "assistant"]
    assert saved[0]["content"] == "watch trucks"
    assert saved[1]["content"] == "Sure, on it."


def test_degraded_turn_still_logs_history(monkeypatch, tmp_path):
    """Regression (the 'chat stopped logging' report): a turn that errored/degraded returns NO
    private 'raw' key. The old gate saved only when 'raw' was present, so such turns silently
    dropped both the user's message AND the reply. Now the exchange is persisted regardless."""
    client, S = _chat_client(monkeypatch, tmp_path,
                             {"message": "Assistant error: Ollama timed out"})  # no 'raw'
    r = client.post("/api/oversight/chat",
                    json={"messages": [{"role": "user", "content": "hello?"}]})
    assert r.status_code == 200
    saved = S._load_watcher_history()
    assert [m["role"] for m in saved] == ["user", "assistant"]      # NOT dropped
    assert saved[0]["content"] == "hello?"
    assert "Ollama timed out" in saved[1]["content"]               # the error reply is recorded


def test_create_without_schedule_defaults_to_30_min(client):
    """A suggestion-shaped body (no interval, no cron) must create, not 500."""
    r = client.post("/api/watches", json={
        "name": "no schedule watch",
        "instruction": "look for trucks",
        "urls": ["https://craigslist.org"],
    })
    try:
        assert r.status_code == 201, r.text
        from web_watcher.config import load
        w = next(w for w in load().watches if w.name == "no schedule watch")
        assert w.interval_minutes == 30
    finally:
        _cleanup(client, "no schedule watch")


def test_invalid_watch_returns_400_not_500(client):
    """Model-validator failures must come back as a clean 400 with JSON detail."""
    r = client.post("/api/watches", json={
        "name": "bad idle",
        "instruction": "x",
        "urls": ["https://example.com"],
        "mode": "continuous",
        "continuous_idle_seconds": 0,   # trips must_have_schedule's ValueError
    })
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]        # must be JSON-serializable
    assert any("continuous_idle_seconds" in d["msg"] for d in detail)


def test_missing_urls_returns_400(client):
    r = client.post("/api/watches", json={"name": "no urls", "instruction": "x", "urls": []})
    assert r.status_code == 400, r.text
    r.json()  # serializable


def test_validation_detail_is_json_safe():
    import json

    from pydantic import ValidationError

    from web_watcher.config import Watch
    with pytest.raises(ValidationError) as ei:
        Watch.model_validate({"name": "x", "instruction": "y", "urls": ["https://e.com"]})
    detail = _validation_detail(ei.value)
    json.dumps(detail)  # must not raise
    assert all({"loc", "msg", "type"} <= set(d) for d in detail)


def test_backfill_schedule_defaults_create_only():
    assert _backfill_schedule({"name": "a"})["interval_minutes"] == 30
    # continuous watches need no schedule — untouched
    assert "interval_minutes" not in _backfill_schedule({"name": "a", "mode": "continuous"})
    # explicit schedules are preserved
    assert _backfill_schedule({"interval_minutes": 5})["interval_minutes"] == 5
    assert "interval_minutes" not in _backfill_schedule({"cron_expression": "0 * * * *"})


def test_normalize_turn_backfills_creates_not_updates():
    out = _normalize_turn({
        "message": "ok",
        "watch_suggestions": [
            {"name": "new one", "instruction": "x", "urls": ["https://e.com"]},
            {"name": "old one", "action": "update", "instruction": "y"},
        ],
    })
    create, update = out["watch_suggestions"]
    assert create["interval_minutes"] == 30
    assert "interval_minutes" not in update


def test_normalize_urls_propagates_zip_across_sites():
    """'vehicles in anacortes on craigslist and ebay' — the craigslist postal must
    localize the eBay search too (eBay gets _stpos from the sibling URL's zip)."""
    from web_watcher.dashboard.server import _normalize_marketplace_urls
    out, changes = _normalize_marketplace_urls([
        "https://seattle.craigslist.org/search/sss?query=vehicles+in+anacortes+under+10k",
        "https://www.ebay.com/sch/i.html?_nkw=vehicles",
    ])
    cl, eb = out
    assert "skagit.craigslist.org" in cl and "postal=" in cl
    assert "_stpos=" in eb and "_sadis=50" in eb
    assert changes  # both rewrites reported


def test_normalize_urls_fixes_offerup_fabricated_path():
    from web_watcher.dashboard.server import _normalize_marketplace_urls
    out, _ = _normalize_marketplace_urls(
        ["https://www.offerup.com/WA-Anacortes/search?q=vehicles&priceMax=10000"])
    assert out[0].startswith("https://offerup.com/search?")
    assert "price_max=10000" in out[0]
    assert "WA-Anacortes" not in out[0]


# ── watch ownership: each Telegram person sees/acts on only their own ────────────

def _cfg_with_owners():
    from web_watcher.config import AppConfig, Watch
    return AppConfig(watches=[
        Watch(name="Mine", urls=["https://x.co"], instruction="a", interval_minutes=30, owner="111"),
        Watch(name="Buddys", urls=["https://y.co"], instruction="b", interval_minutes=30, owner="222"),
        Watch(name="Shared", urls=["https://z.co"], instruction="c", interval_minutes=30, owner=""),
    ])


def test_watches_for_owner_scopes_to_that_person():
    from web_watcher.dashboard import server as S
    cfg = _cfg_with_owners()
    assert [w.name for w in S._watches_for_owner(cfg, None)] == ["Mine", "Buddys", "Shared"]  # desktop = all
    assert [w.name for w in S._watches_for_owner(cfg, "222")] == ["Buddys"]                   # buddy = his
    assert S._watches_for_owner(cfg, "999") == []                                             # stranger = none


def test_is_owned_is_the_action_guard():
    from web_watcher.dashboard import server as S
    cfg = _cfg_with_owners()
    assert S._is_owned("Mine", cfg, None) is True        # desktop may act on anything
    assert S._is_owned("Buddys", cfg, "222") is True     # buddy may act on his own
    assert S._is_owned("Mine", cfg, "222") is False      # but NOT on yours
    assert S._is_owned("Shared", cfg, "222") is False    # nor an unassigned one


def test_context_tells_a_person_with_no_watches(monkeypatch):
    from web_watcher.dashboard import server as S
    from unittest.mock import MagicMock
    mgr = MagicMock(); mgr.get_job_info.return_value = []
    txt = S._build_watches_context(_cfg_with_owners(), mgr, owner="999")
    assert "none assigned to you" in txt.lower()


def test_history_is_separate_per_owner(monkeypatch, tmp_path):
    client, S = _chat_client(monkeypatch, tmp_path,
                             {"message": "ok", "raw": "ok"})
    client.post("/api/oversight/chat",
                json={"messages": [{"role": "user", "content": "buddy msg"}], "owner": "222"})
    # The buddy's turn lands in HIS thread, not the shared/desktop one.
    assert S._load_watcher_history("222")[0]["content"] == "buddy msg"
    assert S._load_watcher_history() == []
