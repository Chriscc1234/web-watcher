"""Offline tests for Deep Inspect helpers — verdict normalization + model resolution.
The verdict QUALITY is validated live against Ollama, not here."""

from __future__ import annotations

from web_watcher import inspect as I
from web_watcher.config import AppConfig


def test_normalize_verdict_coerces_messy_model_output():
    v = I._normalize_verdict(
        {"deal_quality": 9, "scam_risk": "VERY HIGH", "red_flags": "wire transfer only",
         "deal_reason": "cheap", "summary": "sketchy"}, "m")
    assert v["deal_quality"] == 5                     # clamped 1-5
    assert v["scam_risk"] == "high"                   # normalized
    assert v["red_flags"] == ["wire transfer only"]   # string -> list
    assert v["model"] == "m"


def test_normalize_verdict_defaults_are_safe():
    v = I._normalize_verdict({}, "m")
    assert v["deal_quality"] == 3
    assert v["scam_risk"] == "low"
    assert v["red_flags"] == []


def test_normalize_verdict_medium_risk_words():
    assert I._normalize_verdict({"scam_risk": "moderate"}, "m")["scam_risk"] == "medium"
    assert I._normalize_verdict({"scam_risk": "med"}, "m")["scam_risk"] == "medium"


def test_resolve_inspect_model_prefers_biggest_installed(monkeypatch):
    cfg = AppConfig.model_validate({})
    monkeypatch.setattr(I, "_installed_model_names",
                        lambda: {"qwen2.5:14b", "qwen2.5:32b", "qwen2.5-coder:32b"})
    # 72b not installed -> next general preference (32b), NEVER the coder tune
    assert I.resolve_inspect_model(cfg) == "qwen2.5:32b"


def test_resolve_inspect_model_honors_explicit_pin(monkeypatch):
    cfg = AppConfig.model_validate({"models": {"inspect_model": "qwen2.5:72b"}})
    monkeypatch.setattr(I, "_installed_model_names", lambda: {"qwen2.5:72b", "qwen2.5:14b"})
    assert I.resolve_inspect_model(cfg) == "qwen2.5:72b"


def test_resolve_inspect_model_falls_back_to_council(monkeypatch):
    cfg = AppConfig.model_validate({"models": {"council_model": "qwen2.5:14b"}})
    monkeypatch.setattr(I, "_installed_model_names", lambda: set())   # nothing detectable
    assert I.resolve_inspect_model(cfg) == "qwen2.5:14b"


def test_dead_page_detection():
    assert I._looks_like_dead_page("Error Page | eBay", "SORRY Something went wrong on our end. Please go back and try again or go to eBay Homepage.")
    assert I._looks_like_dead_page("", "tiny")                       # too short
    assert I._looks_like_dead_page("Blocked", "Please verify you are a human to continue browsing this site right now okay")
    # a real, ordinary posting is NOT dead
    real = ("Selling my 2009 Toyota Tacoma, 158k miles, 4x4, V6 automatic. Clean title, well "
            "maintained, new tires. $11,500 obo, cash on pickup in Mount Vernon. Text to see it.")
    assert not I._looks_like_dead_page("2009 Toyota Tacoma", real)


# ── known facts belong in the prompt ─────────────────────────────────────────────
# The live miss: a listing whose price appears only in its TITLE was judged "price not
# mentioned", because the model was handed the ad BODY and nothing else. Everything we already
# know is now stated up front, plainly labelled.

def test_the_prompt_states_the_price_even_when_the_body_never_mentions_it(monkeypatch):
    from web_watcher import inspect as I
    sent = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"message": {"content": '{"deal_quality":4,"scam_risk":"low"}'}}

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None):
            sent.update(json or {})
            return _Resp()

    monkeypatch.setattr(I.httpx, "Client", _Client)
    I.verdict_from_text("1998 Toyota Tacoma - $8,500", "Runs great. Clean title.", "trucks",
                        cfg=None, model="m",
                        known={"price_text": "$8,500", "source": "craigslist.org",
                               "posted_at": "2026-08-20"})
    prompt = sent["messages"][-1]["content"]
    assert "PRICE: $8,500" in prompt
    assert "SOURCE: craigslist.org" in prompt
    assert "Never say the price is unknown" in prompt


def test_blank_known_fields_are_left_out_of_the_prompt(monkeypatch):
    from web_watcher import inspect as I
    sent = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"message": {"content": '{"deal_quality":3}'}}

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None):
            sent.update(json or {})
            return _Resp()

    monkeypatch.setattr(I.httpx, "Client", _Client)
    I.verdict_from_text("A title", "body text here", "", cfg=None, model="m",
                        known={"price_text": "", "year": 0, "source": None})
    prompt = sent["messages"][-1]["content"]
    assert "PRICE:" not in prompt and "YEAR:" not in prompt and "SOURCE:" not in prompt
