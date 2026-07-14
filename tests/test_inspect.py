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
