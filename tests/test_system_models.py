"""The Settings model selector: catalog, tier switching, and the delete guard.

The safety-critical rule is that the models Web Watcher is CURRENTLY using cannot be deleted.
Deleting the active model breaks chat and every watch until it is re-downloaded, and there is no
need for it: switching model sets first leaves the old model unused and therefore deletable.
"""

from __future__ import annotations

import types

import pytest
from fastapi.testclient import TestClient

from web_watcher import config as C
from web_watcher.dashboard import server as S


@pytest.fixture
def client(monkeypatch):
    cfg = C.load()
    cfg.models.text_model = "qwen2.5:14b"
    cfg.models.vision_model = "qwen2.5vl:7b"
    cfg.models.council_model = "qwen2.5:14b"
    C.save(cfg)

    # Pretend Ollama has these on disk, so the test never depends on a live daemon.
    monkeypatch.setattr(S, "_installed_models",
                        lambda: {"qwen2.5:14b": 9000, "qwen2.5vl:7b": 6000, "mixtral:latest": 26400})
    mgr = types.SimpleNamespace(get_job_info=lambda: [], oversight_snapshot=lambda **k: {"entries": []})
    return TestClient(S.create_app(mgr))


def test_catalog_marks_current_installed_and_oversized(client):
    d = client.get("/api/system/models").json()
    by_name = {t["tier_name"]: t for t in d["tiers"]}

    assert by_name["16GB"]["current"] is True          # matches the seeded config
    assert by_name["16GB"]["to_download_mb"] == 0      # both its models are "installed"
    # A tier whose models aren't present reports only the MISSING bytes.
    assert by_name["6GB"]["to_download_mb"] > 0
    # Every tier explains itself to a non-technical user.
    assert all(t["what"] and t["tradeoff"] for t in d["tiers"])
    # The active models are flagged so the UI can protect them.
    assert {m["name"] for m in d["installed"] if m["in_use"]} == {"qwen2.5:14b", "qwen2.5vl:7b"}
    assert d["installed_total_mb"] == 9000 + 6000 + 26400


def test_cannot_delete_a_model_in_use(client):
    r = client.post("/api/system/models/delete", json={"name": "qwen2.5:14b"})
    assert r.status_code == 409
    assert "switch to a different model set" in r.json()["detail"].lower()


def test_can_delete_an_unused_model(client, monkeypatch):
    called = {}

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def request(self, method, url, json=None, **k):
            called["name"] = json["name"]
            return types.SimpleNamespace(raise_for_status=lambda: None)
    monkeypatch.setattr(S.httpx, "Client", _Client)

    r = client.post("/api/system/models/delete", json={"name": "mixtral:latest"})
    assert r.status_code == 200
    assert called["name"] == "mixtral:latest"


def test_delete_requires_a_name(client):
    assert client.post("/api/system/models/delete", json={}).status_code == 400


def test_select_unknown_tier_404s(client):
    assert client.post("/api/system/models/select", json={"tier": "999GB"}).status_code == 404


def test_select_oversized_tier_is_allowed_but_warns(client, monkeypatch):
    monkeypatch.setattr(S, "_apply_new_models_bg", lambda rec: None)   # don't really pull
    monkeypatch.setattr(S.threading if hasattr(S, "threading") else S, "Thread",
                        lambda **k: types.SimpleNamespace(start=lambda: None), raising=False)
    r = client.post("/api/system/models/select", json={"tier": "48GB+"})
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "48GB+"
    assert body["warning"] and "slow" in body["warning"].lower()
