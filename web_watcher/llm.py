r"""
Central LLM provider layer — one chat() every caller can route to a LOCAL model (Ollama)
or a CLOUD model (Anthropic), with automatic fallback to local so the app never dies when
the network, an API key, or a quota is missing.

Why this exists
---------------
LLM calls were scattered across the app, each POSTing to Ollama directly. To let a paying
user spend cloud tokens on the roles that benefit most (a sharper per-sweep judge, snappier
chat) WITHOUT giving up the fully-local default, every model call should go through one seam
that decides provider per ROLE from config. This is that seam.

Design rules (non-negotiable)
  • LOCAL IS THE DEFAULT. With no cloud config and no key, behaviour is exactly as before.
  • CLOUD IS OPT-IN PER ROLE. config.models.cloud.roles maps a role → {provider, model}.
  • FALLBACK IS AUTOMATIC. Any cloud failure (missing SDK, missing key, API/network error,
    empty response) falls back to the local model and logs it — a cloud hiccup never breaks a
    sweep. The user's OWN key lives in their config/env; we never bundle a key.
  • JSON CALLERS KEEP WORKING. format_json=True returns a clean JSON string for BOTH providers
    (Ollama's native format:"json"; for Anthropic we extract the JSON object), so callers can
    json.loads() the return value unchanged.

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  chat()              ~L120  The one entry point: resolve route → cloud (try) → local (fallback)
  resolve_route()     ~L90   (provider, model, key) for a role — pure, unit-tested
  _anthropic_chat()   ~L190  Anthropic SDK call (prompt-cached system, usage recorded)
  _ollama_chat()      ~L250  The original local /api/chat call
  usage_snapshot()    ~L300  Running token/cost tally for a future spend meter
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Optional

import httpx

log = logging.getLogger(__name__)

OLLAMA_BASE = "http://localhost:11434"

# Cloud model a role defaults to when it is routed to Anthropic without an explicit model.
# Chosen for the plan's cost/latency tiers: Haiku for the hot/cheap roles, Sonnet for chat,
# Opus for the slow thorough vet. A user can override any of these per role in config.
_CLOUD_DEFAULTS = {
    "judge":   "claude-haiku-4-5",
    "terms":   "claude-haiku-4-5",
    "reason":  "claude-haiku-4-5",
    "chat":    "claude-sonnet-5",
    "inspect": "claude-opus-5",
}
_CLOUD_FALLBACK_MODEL = "claude-haiku-4-5"

# Anthropic list price, USD per 1M tokens (input, output). Cache reads bill ~0.1x input.
# Only used for the local spend estimate shown to the user — not billing truth.
_PRICE = {
    "claude-haiku-4-5":  (1.0, 5.0),
    "claude-sonnet-5":   (3.0, 15.0),
    "claude-opus-5":     (5.0, 25.0),
    "claude-opus-4-8":   (5.0, 25.0),
}

# Log each distinct fallback reason once, not per call, so a cloud outage doesn't flood logs.
_warned: set[str] = set()
_warn_lock = threading.Lock()


def _warn_once(key: str, msg: str) -> None:
    with _warn_lock:
        if key in _warned:
            return
        _warned.add(key)
    log.warning(msg)


# ---------------------------------------------------------------------------
# Route resolution (pure — unit tested)
# ---------------------------------------------------------------------------

def resolve_route(cfg, role: str) -> tuple[str, str, str]:
    """Return (provider, model, api_key) for a role.

    provider is "anthropic" only when config routes this role there AND a usable key exists
    (config value, else $ANTHROPIC_API_KEY); otherwise "local". model is the cloud model id
    for the anthropic path, or "" for local (the caller supplies the local model). Never raises.
    """
    try:
        cloud = cfg.models.cloud
    except Exception:
        return ("local", "", "")

    route = (cloud.roles or {}).get(role)
    if route is None or (getattr(route, "provider", "local") or "local").lower() != "anthropic":
        return ("local", "", "")

    key = (getattr(cloud, "anthropic_api_key", "") or "").strip() or os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        _warn_once(f"nokey:{role}",
                   f"LLM role {role!r} is routed to Anthropic but no API key is set "
                   "(config.models.cloud.anthropic_api_key or $ANTHROPIC_API_KEY) — using the "
                   "local model instead.")
        return ("local", "", "")

    model = (getattr(route, "model", "") or "").strip() or _CLOUD_DEFAULTS.get(role, _CLOUD_FALLBACK_MODEL)
    return ("anthropic", model, key)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def chat(
    messages:    list[dict],
    *,
    role:        str,
    local_model: str,
    cfg=None,
    format_json: bool = False,
    images:      Optional[list[str]] = None,
    timeout:     float = 90.0,
    cache_system: bool = False,
    max_tokens:  int = 4096,
) -> str:
    """Run one chat completion for `role`, returning the assistant text.

    messages: OpenAI/Ollama-style list; a leading {"role":"system"} is supported and, for the
      cloud path, is lifted into Anthropic's top-level `system` (and prompt-cached when
      cache_system=True — worthwhile for fixed-rubric roles like the judge).
    local_model: the Ollama model used when this role is local or cloud falls back.
    format_json: return a clean JSON string for either provider (callers json.loads it).
    images: base64 image strings (vision). Cloud vision isn't wired yet — a cloud route with
      images falls back to local automatically.
    """
    if cfg is None:
        try:
            from web_watcher.config import load_config
            cfg = load_config()
        except Exception:
            cfg = None

    provider, model, key = ("local", "", "")
    if cfg is not None:
        provider, model, key = resolve_route(cfg, role)

    # Hard monthly cap: once the estimate hits the ceiling, stop spending — run local for the
    # rest of the month. Logged once per month so it doesn't flood.
    if provider == "anthropic" and cfg is not None and over_budget(cfg):
        _warn_once(f"budget:{_current_month()}",
                   "Monthly cloud spend cap reached — using the local model until next month.")
        provider = "local"

    if provider == "anthropic" and not images:
        try:
            system_text, user_messages = _split_system(messages)
            text = _anthropic_chat(system_text, user_messages, model, key,
                                   max_tokens=max_tokens, timeout=timeout, cache_system=cache_system)
            if text and text.strip():
                return _extract_json_text(text) if format_json else text
            _warn_once(f"empty:{role}", f"Anthropic returned empty content for role {role!r} — "
                                        "falling back to the local model.")
        except Exception as exc:
            _warn_once(f"err:{role}:{type(exc).__name__}",
                       f"Anthropic call failed for role {role!r} ({type(exc).__name__}: {exc}) — "
                       "falling back to the local model.")

    raw = _ollama_chat(messages, local_model, format_json=format_json, images=images, timeout=timeout)
    return _extract_json_text(raw) if format_json else raw


# ---------------------------------------------------------------------------
# Anthropic path
# ---------------------------------------------------------------------------

def _anthropic_chat(system_text: str, user_messages: list[dict], model: str, key: str,
                    *, max_tokens: int, timeout: float, cache_system: bool) -> str:
    """One Anthropic Messages call. Raises on any failure so chat() can fall back to local."""
    import anthropic  # may be absent on a local-only install — ImportError → caller falls back

    client = anthropic.Anthropic(api_key=key, timeout=timeout)

    kwargs: dict = {"model": model, "max_tokens": max_tokens, "messages": user_messages}
    if system_text:
        block: dict = {"type": "text", "text": system_text}
        if cache_system:
            block["cache_control"] = {"type": "ephemeral"}   # cache the fixed prefix (>=~1024 tok)
        kwargs["system"] = [block]

    resp = client.messages.create(**kwargs)

    try:
        _record_usage(model, resp.usage)
    except Exception:
        pass

    return "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")


# ---------------------------------------------------------------------------
# Local path (Ollama) — the original call, unchanged in behaviour
# ---------------------------------------------------------------------------

def _ollama_chat(messages: list[dict], model: str, *, format_json: bool,
                 images: Optional[list[str]], timeout: float, base_url: str = OLLAMA_BASE) -> str:
    msgs = [dict(m) for m in messages]
    if images and msgs:
        msgs[-1]["images"] = images
    payload: dict = {"model": model, "messages": msgs, "stream": False}
    if format_json:
        payload["format"] = "json"
    with httpx.Client(timeout=timeout) as client:
        r = client.post(f"{base_url.rstrip('/')}/api/chat", json=payload)
        r.raise_for_status()
        return r.json()["message"]["content"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    """Split a chat list into (system_text, non-system messages) for Anthropic, which keeps the
    system prompt in a separate top-level field. Concatenates multiple system messages."""
    system_parts, rest = [], []
    for m in messages:
        if (m.get("role") == "system"):
            system_parts.append(str(m.get("content", "")))
        else:
            rest.append({"role": m.get("role", "user"), "content": str(m.get("content", ""))})
    if not rest:                       # Anthropic requires at least one message
        rest = [{"role": "user", "content": ""}]
    return ("\n\n".join(p for p in system_parts if p), rest)


def _extract_json_text(text: str) -> str:
    """Return the clean JSON object/array from model text. Ollama's format:'json' is already
    clean; cloud models may wrap it in prose or ``` fences. Idempotent on already-clean JSON."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-z]*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t.strip())
    try:
        json.loads(t)
        return t
    except Exception:
        pass
    m = re.search(r"[\{\[].*[\}\]]", t, re.DOTALL)
    return m.group(0) if m else t


# ---------------------------------------------------------------------------
# Usage / spend tally (foundation for a UI spend meter)
# ---------------------------------------------------------------------------

_USAGE = {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost_usd": 0.0}
_usage_lock = threading.Lock()


def _record_usage(model: str, usage) -> None:
    ib = int(getattr(usage, "input_tokens", 0) or 0)
    ob = int(getattr(usage, "output_tokens", 0) or 0)
    cr = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cw = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    in_price, out_price = _PRICE.get(model, (0.0, 0.0))
    # Cache reads bill ~0.1x input; cache writes ~1.25x. Fresh input billed at input rate.
    cost = (ib * in_price + ob * out_price + cr * in_price * 0.1 + cw * in_price * 1.25) / 1_000_000
    with _usage_lock:
        _USAGE["calls"] += 1
        _USAGE["input"] += ib
        _USAGE["output"] += ob
        _USAGE["cache_read"] += cr
        _USAGE["cache_write"] += cw
        _USAGE["cost_usd"] += cost
    _add_month_spend(cost)


def usage_snapshot() -> dict:
    """A copy of the running cloud-usage tally since process start (tokens + estimated USD)."""
    with _usage_lock:
        return dict(_USAGE)


# ---------------------------------------------------------------------------
# Monthly spend ledger + hard cap
# ---------------------------------------------------------------------------
#
# The in-memory tally above resets on restart, so it can't enforce a MONTHLY ceiling. This
# ledger persists estimated spend per calendar month to a small JSON file, survives restarts,
# and rolls over on the 1st. chat() checks it before every cloud call: once the month's spend
# reaches the configured cap, cloud is skipped and the local model runs — the bill cannot pass
# the number the user set. The cap is on OUR estimate, not a live Anthropic account balance
# (the API exposes no balance), so treat it as a safety ceiling, not accounting truth.

_spend_lock = threading.Lock()


def _current_month() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m")


def _spend_path(data_dir=None):
    from pathlib import Path
    if data_dir is not None:
        return Path(data_dir) / "llm_spend.json"
    from web_watcher import paths
    return paths.data_dir() / "llm_spend.json"


def _read_ledger(data_dir=None) -> dict:
    try:
        p = _spend_path(data_dir)
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _add_month_spend(cost: float, data_dir=None) -> None:
    if cost <= 0:
        return
    month = _current_month()
    with _spend_lock:
        led = _read_ledger(data_dir)
        led[month] = round(float(led.get(month, 0.0)) + float(cost), 6)
        try:
            p = _spend_path(data_dir)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(led), encoding="utf-8")
        except Exception as exc:
            log.debug("could not persist spend ledger: %s", exc)


def month_spend(data_dir=None) -> float:
    """Estimated cloud spend so far THIS calendar month (USD)."""
    return float(_read_ledger(data_dir).get(_current_month(), 0.0))


def budget_state(cfg, data_dir=None) -> dict:
    """Spend/cap summary for the UI and for the bot to read back: {spent, cap, remaining,
    over, month}. cap 0 / remaining None means no cap set."""
    try:
        cap = float(getattr(cfg.models.cloud, "monthly_budget_usd", 0.0) or 0.0)
    except Exception:
        cap = 0.0
    spent = month_spend(data_dir)
    remaining = round(cap - spent, 4) if cap > 0 else None
    return {"month": _current_month(), "spent": round(spent, 4), "cap": cap,
            "remaining": remaining, "over": bool(cap > 0 and spent >= cap)}


def over_budget(cfg, data_dir=None) -> bool:
    """True when this month's estimated spend has reached the configured cap (cap 0 = never)."""
    return budget_state(cfg, data_dir)["over"]
