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
    def fake_ollama(messages, model, *, format_json, images, timeout,
                    base_url=llm.OLLAMA_BASE, priority=False, num_ctx=0):
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


# ── context window ───────────────────────────────────────────────────────────────
# Ollama defaults to a SMALL window and silently truncates a longer prompt from the front, so a
# "read all of this" call can quietly become "read the tail of this". Callers must be able to
# state the window they need — and everyone else must keep Ollama's default untouched.

def test_num_ctx_is_sent_only_when_asked_for(monkeypatch):
    sent = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"message": {"content": "ok"}}

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None):
            sent.clear(); sent.update(json or {})
            return _Resp()

    monkeypatch.setattr(llm.httpx, "Client", _Client)

    llm._ollama_chat([{"role": "user", "content": "x"}], "m",
                     format_json=False, images=None, timeout=5.0)
    assert "options" not in sent                      # default behaviour unchanged

    llm._ollama_chat([{"role": "user", "content": "x"}], "m",
                     format_json=False, images=None, timeout=5.0, num_ctx=8192)
    assert sent["options"] == {"num_ctx": 8192}


# ── auto routing: local first, cloud only on a real failure ──────────────────────
# The money rule. A model that predicts which turns are hard spends a GPU call to make a guess;
# checking the ACTUAL local answer costs nothing and is judged on the real thing. So: local runs
# first, always, and cloud is reached only when the local answer objectively fails.

def _auto_cfg(cap=0.0, day_cap=0.0, key="sk-abc", auto=True):
    from web_watcher.config import AppConfig, CloudConfig, ModelsConfig
    return AppConfig(models=ModelsConfig(cloud=CloudConfig(
        anthropic_api_key=key, monthly_budget_usd=cap, daily_budget_usd=day_cap, auto=auto)),
        watches=[])


def test_a_good_local_answer_never_reaches_cloud(monkeypatch):
    monkeypatch.setattr(llm, "_ollama_chat", lambda *a, **k: "You have two watches running.")
    monkeypatch.setattr(llm, "_anthropic_chat", lambda *a, **k: pytest.fail("must not spend"))
    got = llm.chat_smart([{"role": "user", "content": "how many watches?"}],
                         role="chat", local_model="qwen2.5:14b", cfg=_auto_cfg())
    assert got["used"] == "local" and got["escalated"] is False


def test_a_failed_local_answer_escalates_to_the_cheapest_rung(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "_esc_path", lambda data_dir=None: tmp_path / "esc.jsonl")
    monkeypatch.setattr(llm, "_ollama_chat", lambda *a, **k: "")          # empty = failed
    monkeypatch.setattr(llm, "_anthropic_chat",
                        lambda system, msgs, model, key, **k: f"answer from {model}")
    got = llm.chat_smart([{"role": "user", "content": "x"}], role="chat",
                         local_model="m", cfg=_auto_cfg())
    assert got["escalated"] is True
    assert got["used"] == llm.CLOUD_LADDER[0]          # cheapest first, not the biggest


def test_it_climbs_to_the_next_rung_only_if_the_cheap_one_also_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "_esc_path", lambda data_dir=None: tmp_path / "esc.jsonl")
    monkeypatch.setattr(llm, "_ollama_chat", lambda *a, **k: "")
    seen = []

    def fake(system, msgs, model, key, **k):
        seen.append(model)
        return "" if model == llm.CLOUD_LADDER[0] else "a real answer"

    monkeypatch.setattr(llm, "_anthropic_chat", fake)
    got = llm.chat_smart([{"role": "user", "content": "x"}], role="chat",
                         local_model="m", cfg=_auto_cfg())
    assert seen == list(llm.CLOUD_LADDER)              # tried cheap, then dear
    assert got["used"] == llm.CLOUD_LADDER[1]


def test_the_judge_never_escalates_however_badly_it_does(monkeypatch):
    """The per-sweep judge runs on every listing of every sweep — routing it to cloud is how a
    budget vanishes overnight."""
    monkeypatch.setattr(llm, "_ollama_chat", lambda *a, **k: "")
    monkeypatch.setattr(llm, "_anthropic_chat", lambda *a, **k: pytest.fail("judge must stay local"))
    got = llm.chat_smart([{"role": "user", "content": "x"}], role="judge",
                         local_model="m", cfg=_auto_cfg())
    assert got["escalated"] is False and "never escalates" in got["why"]


def test_no_key_no_escalation(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(llm, "_ollama_chat", lambda *a, **k: "")
    monkeypatch.setattr(llm, "_anthropic_chat", lambda *a, **k: pytest.fail("no key"))
    got = llm.chat_smart([{"role": "user", "content": "x"}], role="chat",
                         local_model="m", cfg=_auto_cfg(key=""))
    assert got["why"] == "no API key"


def test_a_local_crash_still_escalates_rather_than_failing(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "_esc_path", lambda data_dir=None: tmp_path / "esc.jsonl")

    def boom(*a, **k):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(llm, "_ollama_chat", boom)
    monkeypatch.setattr(llm, "_anthropic_chat", lambda *a, **k: "cloud saved it")
    got = llm.chat_smart([{"role": "user", "content": "x"}], role="chat",
                         local_model="m", cfg=_auto_cfg())
    assert got["text"] == "cloud saved it" and got["escalated"] is True


def test_vision_calls_never_escalate(monkeypatch):
    monkeypatch.setattr(llm, "_ollama_chat", lambda *a, **k: "")
    monkeypatch.setattr(llm, "_anthropic_chat", lambda *a, **k: pytest.fail("no cloud vision"))
    got = llm.chat_smart([{"role": "user", "content": "x"}], role="chat",
                         local_model="m", cfg=_auto_cfg(), images=["b64"])
    assert got["why"] == "no cloud vision"


# ── what counts as a failed answer ───────────────────────────────────────────────

def test_looks_usable_accepts_short_real_answers_and_rejects_junk():
    assert llm.looks_usable("Yes — both are running.") is True
    assert llm.looks_usable("") is False
    assert llm.looks_usable('{"message": "leaked"}') is False       # machine output in the chat
    assert llm.looks_usable("assistant: ") is False
    assert llm.looks_usable('{"a": 1}', format_json=True) is True
    assert llm.looks_usable('{"a": 1', format_json=True) is False   # truncated JSON


# ── caps ─────────────────────────────────────────────────────────────────────────

def test_a_daily_cap_stops_spending_without_losing_the_month(tmp_path, monkeypatch):
    llm._add_month_spend(3.0, tmp_path)
    st = llm.budget_state(_auto_cfg(cap=20.0, day_cap=1.0), tmp_path)
    assert st["over"] is False              # the month is fine…
    assert st["over_today"] is True         # …but today is done
    assert st["today"] == 3.0


def test_over_budget_trips_on_either_cap(tmp_path):
    llm._add_month_spend(2.0, tmp_path)
    assert llm.over_budget(_auto_cfg(cap=100.0, day_cap=1.0), tmp_path) is True
    assert llm.over_budget(_auto_cfg(cap=1.0, day_cap=100.0), tmp_path) is True
    assert llm.over_budget(_auto_cfg(cap=100.0, day_cap=100.0), tmp_path) is False


# ── the escalation log ───────────────────────────────────────────────────────────

def test_escalations_are_logged_with_both_answers_and_the_cost(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "_esc_path", lambda data_dir=None: tmp_path / "esc.jsonl")
    llm.record_escalation("chat", "the local answer failed the check", "claude-haiku-4-5",
                          0.0012, local_text="", cloud_text="a proper answer", prompt="watch fb")
    rows = llm.escalations()
    assert len(rows) == 1
    assert rows[0]["role"] == "chat" and rows[0]["cloud"] == "a proper answer"
    assert rows[0]["cost_usd"] == 0.0012

    summary = llm.escalation_summary()
    assert summary["count"] == 1 and summary["cost_usd"] == 0.0012
    assert summary["by_reason"]["the local answer failed the check"] == 1
    assert summary["by_model"]["claude-haiku-4-5"] == 1


def test_the_escalation_log_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "_esc_path", lambda data_dir=None: tmp_path / "esc.jsonl")
    monkeypatch.setattr(llm, "_ESC_KEEP", 5)
    for i in range(12):
        llm.record_escalation("chat", "why", "m", 0.001, cloud_text=f"n{i}")
    rows = llm.escalations(100)
    assert len(rows) <= 5
    assert rows[0]["cloud"] == "n11"        # newest first, oldest dropped
