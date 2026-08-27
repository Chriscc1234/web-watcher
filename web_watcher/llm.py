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

import contextlib
import json
import logging
import os
import re
import threading
import time
from typing import Callable, Optional

import httpx

log = logging.getLogger(__name__)

OLLAMA_BASE = "http://localhost:11434"

# Process-wide serialization for LOCAL (Ollama) inference. One local GPU realistically does one
# call at a time; several sweep threads each running vision + reasoning in parallel just thrash it
# and time out. Every local call — here and the browsing agent's direct vision/reasoning calls in
# agent.py (which import this same lock) — acquires it, so local inference runs one-at-a-time at
# full speed. Held only for the inference HTTP call, so browsers still interleave. Cloud is exempt.
GPU_LOCK = threading.Lock()

# CHAT GOES FIRST. Sweeps run local inference constantly (vision + judging), so a person's message
# could sit behind a queue of background work and take a long time to answer. A chat turn raises a
# flag; background callers wait for it to clear before taking the GPU, so the human gets the next
# slot instead of the sweep. Bounded so background work can never be starved indefinitely.
_gpu_priority_waiting = 0
_gpu_cv = threading.Condition()
_GPU_YIELD_MAX_S = 45.0        # longest a background call will stand aside for chat


@contextlib.contextmanager
def gpu_slot(priority: bool = False):
    """Serialize local inference on the one GPU, letting interactive work jump the queue."""
    global _gpu_priority_waiting
    if priority:
        with _gpu_cv:
            _gpu_priority_waiting += 1
        try:
            with GPU_LOCK:
                yield
        finally:
            with _gpu_cv:
                _gpu_priority_waiting -= 1
                _gpu_cv.notify_all()
        return
    # Background work: stand aside while a person is waiting on a reply.
    deadline = time.monotonic() + _GPU_YIELD_MAX_S
    with _gpu_cv:
        while _gpu_priority_waiting > 0 and time.monotonic() < deadline:
            _gpu_cv.wait(0.25)
    with GPU_LOCK:
        yield

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
    force_local: bool = False,
    num_ctx:     int = 0,
) -> str:
    """Run one chat completion for `role`, returning the assistant text.

    messages: OpenAI/Ollama-style list; a leading {"role":"system"} is supported and, for the
      cloud path, is lifted into Anthropic's top-level `system` (and prompt-cached when
      cache_system=True — worthwhile for fixed-rubric roles like the judge).
    local_model: the Ollama model used when this role is local or cloud falls back.
    format_json: return a clean JSON string for either provider (callers json.loads it).
    images: base64 image strings (vision). Cloud vision isn't wired yet — a cloud route with
      images falls back to local automatically.
    force_local: run on the local model even when the role is routed to cloud. Used for
      smart escalation — the caller keeps easy/short turns local (fast + free) and only lets
      the hard ones reach cloud. A no-op when the role is already local.
    num_ctx: local-only context window override. Ollama defaults to a SMALL window (4096) and
      silently truncates anything longer, so any caller handing over a long document must say
      how much it needs to be read. 0 = leave Ollama's default alone.
    """
    if cfg is None:
        try:
            from web_watcher.config import load_config
            cfg = load_config()
        except Exception:
            cfg = None

    provider, model, key = ("local", "", "")
    if cfg is not None and not force_local:
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

    # A person is waiting on the "chat" role — let it jump the GPU queue ahead of sweep work.
    raw = _ollama_chat(messages, local_model, format_json=format_json, images=images,
                       timeout=timeout, priority=(role == "chat"), num_ctx=num_ctx)
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

# A screenshot is charged as image TOKENS, and a big one can exceed Ollama's whole default
# context (4096) by itself — the model then 400s with exceed_context_size before it has read a
# single word. That is not a rare edge: it's every vision call on a high-DPI monitor, and it
# fails the same way for the OCR fallback, the browsing agent and the drill. So we shrink the
# image to a size the model reads just as well, and give vision a bigger window besides.
_MAX_IMAGE_EDGE = 1280      # px on the longest edge — plenty for reading a page
_VISION_NUM_CTX = 8_192     # default window when images are present


def _shrink_image_b64(b64: str, max_edge: int = _MAX_IMAGE_EDGE) -> str:
    """Downscale an oversized base64 PNG. Returns the input unchanged if PIL is missing or the
    image is already small enough — never raises, because a failed resize must not lose the call."""
    try:
        import base64 as _b64
        import io
        from PIL import Image

        raw = _b64.b64decode(b64)
        img = Image.open(io.BytesIO(raw))
        if max(img.size) <= max_edge:
            return b64
        ratio = max_edge / float(max(img.size))
        img = img.convert("RGB").resize(
            (max(1, int(img.width * ratio)), max(1, int(img.height * ratio))), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return _b64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:
        log.debug("could not shrink an image for vision: %s", exc)
        return b64


def _ollama_chat(messages: list[dict], model: str, *, format_json: bool,
                 images: Optional[list[str]], timeout: float, base_url: str = OLLAMA_BASE,
                 priority: bool = False, num_ctx: int = 0) -> str:
    msgs = [dict(m) for m in messages]
    if images and msgs:
        msgs[-1]["images"] = [_shrink_image_b64(i) for i in images]
        if num_ctx <= 0:
            num_ctx = _VISION_NUM_CTX
    payload: dict = {"model": model, "messages": msgs, "stream": False}
    if format_json:
        payload["format"] = "json"
    # Ollama's DEFAULT context window is small (4096); a longer prompt is silently truncated
    # from the FRONT, so a big document read looks like it worked while the model only ever saw
    # the tail. Callers that hand over a lot of text (the chat reviewer) must state the window
    # they need. Left unset for everyone else so existing behaviour is unchanged.
    if num_ctx > 0:
        payload["options"] = {"num_ctx": int(num_ctx)}
    # Serialize on the shared GPU slot so this never runs a local inference at the same time as a
    # browsing agent's vision call — one local GPU does one call at a time; two at once just thrash
    # and time out. priority=True (a person is waiting on a reply) jumps ahead of sweep work.
    with gpu_slot(priority=priority), httpx.Client(timeout=timeout) as client:
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
    day = "day:" + _current_day()
    with _spend_lock:
        led = _read_ledger(data_dir)
        led[month] = round(float(led.get(month, 0.0)) + float(cost), 6)
        led[day] = round(float(led.get(day, 0.0)) + float(cost), 6)
        # Keep the file from growing forever: only the last ~40 daily rows are useful.
        days = sorted(k for k in led if k.startswith("day:"))
        for stale in days[:-40]:
            led.pop(stale, None)
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
    try:
        day_cap = float(getattr(cfg.models.cloud, "daily_budget_usd", 0.0) or 0.0)
    except Exception:
        day_cap = 0.0
    spent = month_spend(data_dir)
    today = day_spend(data_dir)
    remaining = round(cap - spent, 4) if cap > 0 else None
    return {"month": _current_month(), "spent": round(spent, 4), "cap": cap,
            "remaining": remaining, "over": bool(cap > 0 and spent >= cap),
            "today": round(today, 4), "day_cap": day_cap,
            "over_today": bool(day_cap > 0 and today >= day_cap)}


def over_budget(cfg, data_dir=None) -> bool:
    """True when this month's OR today's estimated spend has reached its cap (cap 0 = never)."""
    st = budget_state(cfg, data_dir)
    return bool(st["over"] or st.get("over_today"))


def _current_day() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")


def day_spend(data_dir=None) -> float:
    """Estimated cloud spend so far TODAY (USD)."""
    return float(_read_ledger(data_dir).get("day:" + _current_day(), 0.0))


# ---------------------------------------------------------------------------
# Auto routing — local first, cloud only when local demonstrably failed
# ---------------------------------------------------------------------------
#
# The money question is "when is Claude worth paying for?", and the tempting answer — have a
# model predict which turns are hard — is the wrong one twice over: it spends a GPU call BEFORE
# every decision, and a prediction is exactly as unreliable as the local answer it's trying to
# pre-empt. So we don't predict. We let the local model actually do the work, CHECK the result,
# and escalate only what objectively failed. A local model that's coping costs nothing at all,
# and we never pay for a guess.
#
# When we do escalate, we climb cheapest-first and stop at the first rung that produces a usable
# answer, so the expensive models are reached only by the handful of calls that truly need them.
CLOUD_LADDER: tuple[str, ...] = ("claude-haiku-4-5", "claude-sonnet-5")

# WHICH ROLES MAY ESCALATE. The rule is volume against consequence: escalate what is RARE and
# CONSEQUENTIAL, never what runs per-listing.
#
#   chat       a person is waiting; a handful of turns a day.
#   extract    turning "watch for manual trucks under 10k near Anacortes" into a real watch. Rare
#              (only when a watch is made or changed) and the most consequential call in the app —
#              a wrong extraction is a watch that quietly finds nothing for a week.
#   terms      the search terms a watch uses. Rare, and it decides whether the watch sees anything
#              at all.
#   comprehend understanding a NEW site — once per site, then cached forever.
#   vet        Deep Inspect, only when a person asks about one listing.
#   stuck      the browsing agent has failed on a page and is about to give up. Escalating a
#              FAILURE is free of volume risk by construction: it can only fire when local
#              already lost, which is the same principle as the cascade itself.
#
# JUDGE IS DELIBERATELY ABSENT and should stay that way. It rates every listing of every sweep —
# on a continuous watch that is thousands of calls a day, which is tens of dollars a month before
# anyone notices. A local probe also found the 14b's ratings as good as a 32b's, so there is
# nothing to buy. Vision is absent because the cloud vision path isn't wired. review/inspect are
# absent because they already run the biggest local model with no time limit, for free.
ESCALATABLE_ROLES: frozenset = frozenset(
    {"chat", "extract", "terms", "comprehend", "vet", "stuck"})


def cloud_ready(cfg, role: str) -> tuple[bool, str]:
    """(can this role escalate to cloud right now?, why not). Never raises."""
    try:
        cloud = cfg.models.cloud
    except Exception:
        return (False, "no cloud config")
    if not getattr(cloud, "auto", True):
        return (False, "auto routing is off")
    key = (getattr(cloud, "anthropic_api_key", "") or "").strip() or \
        os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return (False, "no API key")
    if role not in ESCALATABLE_ROLES:
        return (False, f"role {role!r} never escalates")
    if over_budget(cfg):
        return (False, "budget cap reached")
    return (True, "")


def looks_usable(text: str, format_json: bool = False) -> bool:
    """Did the model actually produce an answer? The cheap, deterministic check that decides
    whether we pay for a better one. Conservative on purpose: it must catch real failures
    (empty, truncated JSON, a leaked prompt) without calling a short-but-correct reply broken."""
    t = (text or "").strip()
    if len(t) < 2:
        return False
    if format_json:
        try:
            data = json.loads(_extract_json_text(t))
        except Exception:
            return False
        return isinstance(data, (dict, list))
    # Prose: reject a reply that's really machine output leaking into the chat, or one that
    # just parrots the instructions back.
    if t.startswith("{") or t.startswith("["):
        return False
    if re.match(r"^\s*(system|assistant|user)\s*:", t, re.I):
        return False
    return True


# ---------------------------------------------------------------------------
# The escalation log — every dollar, with the evidence for why it was spent
# ---------------------------------------------------------------------------
#
# Each escalation is appended here with BOTH answers: what the local model produced, why that
# failed the check, and what the cloud model said instead. That makes the log three things at
# once — an audit trail for the bill, a growing corpus of exactly what the local model can't do
# (which is what tells us where to fix a prompt or pull a better local model), and the evidence
# for whether paying is even buying anything.
#
# It is deliberately NOT wired back into routing. Feeding it into a predictor would quietly
# reinstate the guess-first design this replaced. It's for people to read and act on.
_ESC_FILENAME = "cloud_escalations.jsonl"
_ESC_KEEP = 500


def _esc_path(data_dir=None):
    from pathlib import Path
    if data_dir is not None:
        return Path(data_dir) / _ESC_FILENAME
    from web_watcher import paths
    return paths.data_dir() / _ESC_FILENAME


def record_escalation(role: str, why: str, used: str, cost: float,
                      local_text: str = "", cloud_text: str = "", prompt: str = "",
                      data_dir=None) -> None:
    """Append one escalation. Never raises — logging must not break a reply."""
    entry = {
        "ts": time.time(), "role": role, "why": why, "used": used,
        "cost_usd": round(float(cost), 6),
        "prompt": (prompt or "")[-400:],
        "local": (local_text or "")[:400],
        "cloud": (cloud_text or "")[:400],
    }
    try:
        p = _esc_path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        with _spend_lock:
            lines = []
            if p.exists():
                lines = p.read_text(encoding="utf-8").splitlines()[-(_ESC_KEEP - 1):]
            lines.append(json.dumps(entry, ensure_ascii=False))
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as exc:
        log.debug("could not record the escalation: %s", exc)


def escalations(limit: int = 50, data_dir=None) -> list[dict]:
    """The most recent escalations, newest first."""
    try:
        p = _esc_path(data_dir)
        if not p.exists():
            return []
        rows = []
        for line in p.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
        return list(reversed(rows))
    except Exception:
        return []


def escalation_summary(data_dir=None) -> dict:
    """What has actually needed Claude, aggregated — the answer to "is this worth paying for,
    and what should I fix so it isn't?"."""
    rows = escalations(_ESC_KEEP, data_dir)
    by_reason: dict[str, int] = {}
    by_role: dict[str, int] = {}
    by_model: dict[str, int] = {}
    total = 0.0
    for r in rows:
        by_reason[r.get("why", "?")] = by_reason.get(r.get("why", "?"), 0) + 1
        by_role[r.get("role", "?")] = by_role.get(r.get("role", "?"), 0) + 1
        by_model[r.get("used", "?")] = by_model.get(r.get("used", "?"), 0) + 1
        total += float(r.get("cost_usd", 0.0) or 0.0)
    return {"count": len(rows), "cost_usd": round(total, 4), "by_reason": by_reason,
            "by_role": by_role, "by_model": by_model,
            "avg_cost_usd": round(total / len(rows), 6) if rows else 0.0}


def chat_smart(messages: list[dict], *, role: str, local_model: str, cfg=None,
               validate: Optional[Callable[[str], bool]] = None, format_json: bool = False,
               timeout: float = 90.0, num_ctx: int = 0, max_tokens: int = 4096,
               cache_system: bool = False, images: Optional[list[str]] = None) -> dict:
    """Run a call the thrifty way: LOCAL first, then climb the cloud ladder only if the local
    answer fails the check.

    Returns {"text", "used", "escalated", "why"} — `used` is "local" or the cloud model id, so
    callers and the UI can show what actually answered and the log can explain every dollar.

    validate: text -> bool. Defaults to looks_usable(). Pass a stricter one when the caller knows
    what a good answer looks like (required JSON fields, a non-empty list) — the stricter this
    check, the better the escalation decision, because it's judging the REAL answer.
    """
    if cfg is None:
        try:
            from web_watcher.config import load as load_config
            cfg = load_config()
        except Exception:
            cfg = None

    check = validate or (lambda t: looks_usable(t, format_json))

    # 1. Local always gets first refusal. It's free and, on this hardware, usually right.
    local_text = ""
    try:
        local_text = chat(messages, role=role, local_model=local_model, cfg=cfg,
                          format_json=format_json, images=images, timeout=timeout,
                          cache_system=cache_system, max_tokens=max_tokens,
                          force_local=True, num_ctx=num_ctx)
        if check(local_text):
            return {"text": local_text, "used": "local", "escalated": False, "why": ""}
        why = "the local answer failed the check"
    except Exception as exc:
        why = f"the local model errored ({type(exc).__name__})"
        log.warning("chat_smart: local failed for role %r: %s", role, exc)

    ok, blocked = cloud_ready(cfg, role)
    if not ok:
        log.info("chat_smart: %s for role %r but not escalating — %s", why, role, blocked)
        return {"text": local_text, "used": "local", "escalated": False, "why": blocked}
    if images:                       # cloud vision isn't wired — nothing to escalate TO
        return {"text": local_text, "used": "local", "escalated": False, "why": "no cloud vision"}

    # 2. Climb the ladder, cheapest first, stopping at the first rung that works.
    _, _, key = ("", "", (getattr(cfg.models.cloud, "anthropic_api_key", "") or "").strip()
                 or os.environ.get("ANTHROPIC_API_KEY", "").strip())
    system_text, user_messages = _split_system(messages)
    for model in CLOUD_LADDER:
        if over_budget(cfg):
            log.warning("chat_smart: stopping at the budget cap mid-escalation")
            break
        try:
            before = usage_snapshot()["cost_usd"]
            text = _anthropic_chat(system_text, user_messages, model, key,
                                   max_tokens=max_tokens, timeout=timeout,
                                   cache_system=cache_system)
            text = _extract_json_text(text) if format_json else text
            cost = round(usage_snapshot()["cost_usd"] - before, 6)
            if check(text):
                log.info("chat_smart: role %r escalated to %s (%s) — $%.4f", role, model, why, cost)
                record_escalation(role, why, model, cost, local_text, text,
                                  prompt=(user_messages[-1]["content"] if user_messages else ""))
                return {"text": text, "used": model, "escalated": True, "why": why,
                        "cost_usd": cost}
            log.info("chat_smart: %s also failed the check for role %r — trying the next rung",
                     model, role)
        except Exception as exc:
            log.warning("chat_smart: %s failed for role %r: %s", model, role, exc)

    # 3. Nothing did better than local. Return what we have rather than nothing — and say so
    # LOUDLY: when local, Haiku AND Sonnet all "fail" the same check, the most likely broken
    # thing is the CHECK, and every occurrence costs real money. This exact pattern burned
    # $0.40 in an afternoon through a validator that looked for a key the extractor never
    # emits. The issue log makes a repeat visible instead of a silent line item on a bill.
    log.warning("chat_smart: role %r — local AND every cloud rung failed the check; "
                "paid for nothing. If this recurs, the validator is probably wrong.", role)
    try:
        from web_watcher import issues
        issues.record_issue("cloud_ladder_futile", f"role:{role}",
                            "local + all cloud rungs failed the same validation check — "
                            "paid cloud calls bought nothing (suspect the validator)")
    except Exception:
        pass
    return {"text": local_text, "used": "local", "escalated": False,
            "why": "cloud could not do better"}
