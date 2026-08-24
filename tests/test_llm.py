"""The central LLM provider layer: role-based routing local↔cloud with automatic fallback.

Local is the default and cloud is opt-in per role; a cloud failure must fall back to the
local model, never break the caller. No network here — the Anthropic and Ollama calls are
monkeypatched. See web_watcher/llm.py."""

from __future__ import annotations

import pytest

from web_watcher import llm
from web_watcher.config import AppConfig, CloudConfig, ModelsConfig, ProviderRoute


def _cfg(roles=None, key="") -> AppConfig:
    models = ModelsConfig(cloud=CloudConfig(anthropic_api_key=key, roles=roles or {}))
    # AppConfig needs at least the models block; watches default to empty.
    return AppConfig(models=models, watches=[])


# ── route resolution ───────────────────────────────────────────────────────────

def test_default_is_local(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    prov, model, key = llm.resolve_route(_cfg(), "judge")
    assert prov == "local" and model == "" and key == ""


def test_cloud_route_needs_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _cfg(roles={"judge": ProviderRoute(provider="anthropic", model="claude-haiku-4-5")})
    # Routed to anthropic but NO key anywhere → stays local (safe).
    assert llm.resolve_route(cfg, "judge")[0] == "local"


def test_cloud_route_with_config_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _cfg(roles={"judge": ProviderRoute(provider="anthropic", model="claude-haiku-4-5")}, key="sk-abc")
    prov, model, key = llm.resolve_route(cfg, "judge")
    assert prov == "anthropic" and model == "claude-haiku-4-5" and key == "sk-abc"


def test_cloud_route_uses_env_key_when_config_blank(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    cfg = _cfg(roles={"judge": ProviderRoute(provider="anthropic")})
    prov, model, key = llm.resolve_route(cfg, "judge")
    assert prov == "anthropic" and key == "sk-env"
    assert model == "claude-haiku-4-5"          # blank model => per-role default


def test_unlisted_role_is_local():
    cfg = _cfg(roles={"judge": ProviderRoute(provider="anthropic", model="x")}, key="sk-abc")
    assert llm.resolve_route(cfg, "chat")[0] == "local"


# ── chat() dispatch + fallback ───────────────────────────────────────────────────

def test_chat_local_path_calls_ollama(monkeypatch):
    seen = {}
    def fake_ollama(messages, model, *, format_json, images, timeout, base_url=llm.OLLAMA_BASE):
        seen.update(model=model, fmt=format_json)
        return '{"ratings": []}'
    monkeypatch.setattr(llm, "_ollama_chat", fake_ollama)
    monkeypatch.setattr(llm, "_anthropic_chat", lambda *a, **k: pytest.fail("cloud must not be called"))
    out = llm.chat([{"role": "user", "content": "hi"}], role="judge",
                   local_model="qwen2.5:14b", cfg=_cfg(), format_json=True)
    assert out == '{"ratings": []}'
    assert seen == {"model": "qwen2.5:14b", "fmt": True}


def test_chat_cloud_path_used_when_routed(monkeypatch):
    cfg = _cfg(roles={"judge": ProviderRoute(provider="anthropic", model="claude-haiku-4-5")}, key="sk-abc")
    monkeypatch.setattr(llm, "_anthropic_chat",
                        lambda system, msgs, model, key, **k: '{"ratings": [{"i":0,"r":5,"why":"ok"}]}')
    monkeypatch.setattr(llm, "_ollama_chat", lambda *a, **k: pytest.fail("should not fall back"))
    out = llm.chat([{"role": "system", "content": "rubric"}, {"role": "user", "content": "x"}],
                   role="judge", local_model="qwen2.5:14b", cfg=cfg, format_json=True)
    assert '"ratings"' in out


def test_force_local_bypasses_cloud_route(monkeypatch):
    # Role IS routed to cloud, but force_local keeps it local (smart escalation for easy turns).
    cfg = _cfg(roles={"chat": ProviderRoute(provider="anthropic", model="claude-haiku-4-5")}, key="sk-abc")
    monkeypatch.setattr(llm, "_anthropic_chat", lambda *a, **k: pytest.fail("force_local must stay local"))
    monkeypatch.setattr(llm, "_ollama_chat", lambda *a, **k: "LOCAL")
    out = llm.chat([{"role": "user", "content": "yes"}], role="chat",
                   local_model="qwen2.5:14b", cfg=cfg, force_local=True)
    assert out == "LOCAL"


def test_chat_falls_back_to_local_on_cloud_error(monkeypatch):
    cfg = _cfg(roles={"judge": ProviderRoute(provider="anthropic", model="claude-haiku-4-5")}, key="sk-abc")
    def boom(*a, **k):
        raise RuntimeError("api down")
    monkeypatch.setattr(llm, "_anthropic_chat", boom)
    monkeypatch.setattr(llm, "_ollama_chat", lambda *a, **k: "LOCAL")
    out = llm.chat([{"role": "user", "content": "x"}], role="judge",
                   local_model="qwen2.5:14b", cfg=cfg)
    assert out == "LOCAL"


def test_chat_cloud_with_images_falls_back_to_local(monkeypatch):
    cfg = _cfg(roles={"judge": ProviderRoute(provider="anthropic", model="claude-haiku-4-5")}, key="sk-abc")
    monkeypatch.setattr(llm, "_anthropic_chat", lambda *a, **k: pytest.fail("no cloud vision yet"))
    monkeypatch.setattr(llm, "_ollama_chat", lambda *a, **k: "LOCAL")
    out = llm.chat([{"role": "user", "content": "x"}], role="judge",
                   local_model="qwen2.5vl:7b", cfg=cfg, images=["b64"])
    assert out == "LOCAL"


# ── helpers ──────────────────────────────────────────────────────────────────────

def test_split_system_lifts_system_out():
    sys_text, rest = llm._split_system(
        [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}])
    assert sys_text == "S"
    assert rest == [{"role": "user", "content": "U"}]


def test_split_system_synthesizes_a_user_when_only_system():
    sys_text, rest = llm._split_system([{"role": "system", "content": "S"}])
    assert sys_text == "S" and rest and rest[0]["role"] == "user"


def test_extract_json_handles_fences_and_prose():
    assert llm._extract_json_text('```json\n{"a":1}\n```') == '{"a":1}'
    assert llm._extract_json_text('Sure! {"a": 1} done') == '{"a": 1}'
    assert llm._extract_json_text('{"a": 1}') == '{"a": 1}'


def test_usage_snapshot_shape():
    snap = llm.usage_snapshot()
    for k in ("calls", "input", "output", "cache_read", "cost_usd"):
        assert k in snap


# ── monthly spend cap ────────────────────────────────────────────────────────────

def _budget_cfg(cap):
    from web_watcher.config import AppConfig, CloudConfig, ModelsConfig, ProviderRoute
    return AppConfig(models=ModelsConfig(cloud=CloudConfig(
        anthropic_api_key="sk", monthly_budget_usd=cap,
        roles={"chat": ProviderRoute(provider="anthropic", model="claude-haiku-4-5")})),
        watches=[])


def test_month_spend_persists_and_accumulates(tmp_path):
    assert llm.month_spend(tmp_path) == 0.0
    llm._add_month_spend(1.25, tmp_path)
    llm._add_month_spend(0.75, tmp_path)
    assert round(llm.month_spend(tmp_path), 2) == 2.00


def test_budget_state_reports_remaining(tmp_path):
    llm._add_month_spend(12.0, tmp_path)
    st = llm.budget_state(_budget_cfg(40.0), tmp_path)
    assert st["spent"] == 12.0 and st["cap"] == 40.0 and st["remaining"] == 28.0 and st["over"] is False


def test_over_budget_trips_at_the_cap(tmp_path):
    llm._add_month_spend(40.0, tmp_path)
    assert llm.over_budget(_budget_cfg(40.0), tmp_path) is True
    assert llm.over_budget(_budget_cfg(0.0), tmp_path) is False    # 0 = no cap, never over


def test_chat_falls_back_to_local_when_over_budget(monkeypatch):
    cfg = _budget_cfg(40.0)
    monkeypatch.setattr(llm, "over_budget", lambda c, *a, **k: True)   # pretend the cap is hit
    monkeypatch.setattr(llm, "_anthropic_chat", lambda *a, **k: pytest.fail("must not spend over cap"))
    monkeypatch.setattr(llm, "_ollama_chat", lambda *a, **k: "LOCAL")
    out = llm.chat([{"role": "user", "content": "hi"}], role="chat",
                   local_model="qwen2.5:14b", cfg=cfg)
    assert out == "LOCAL"
