"""
Deep Inspect — a slow, thorough evaluation of ONE listing for the "is this a good deal / is
this a scam?" question. Distinct from the fast per-sweep rating judge: this opens the actual
listing, reads the FULL posting, and a big LOCAL model synthesises a deal + scam-risk verdict.

Runs on a handful of candidates on demand, not every listing on every sweep — so it can
afford a large, slow local model (the user's "don't care how long it takes" quality tier).

Phased:
  • Phase 1 (this file): fetch the full posting text + a big-model deal/scam verdict.
  • Phase 2 (later): vision model reads the photos + reverse-image-search (browser, free).
  • Phase 3 (later): price-comp web search to ground "too cheap = bait" vs "genuine deal".

KEY LOCATIONS
  resolve_inspect_model   pick the biggest suitable INSTALLED local model (fallback = council)
  fetch_listing_text      open the listing in a browser, return full posting text + image urls
  deep_inspect_listing    the whole Phase-1 pass: fetch → verdict
  _INSPECT_SYSTEM         the deal/scam analysis prompt
  INSPECT_SCHEMA          the structured verdict shape (documented; the model returns this JSON)
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

log = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434"

# Preference order for the deep model when the user hasn't pinned one: a GENERAL reasoning
# model (NOT the coder tune — coding models are the wrong tool for reading an ad and judging a
# scam), biggest first. Whichever of these is installed wins; else we fall back to the council
# model (always present). qwen2.5:72b is the quality tier the user opted into.
_INSPECT_PREFERENCE = ("qwen2.5:72b", "qwen2.5:32b", "llama3.3:70b", "qwen2.5:14b")

# Documented shape of the verdict the model returns (enforced via format=json + prompt).
INSPECT_SCHEMA = {
    "deal_quality": "int 1-5 (5 = great deal)",
    "deal_reason":  "one sentence on the price/value",
    "scam_risk":    "low | medium | high",
    "red_flags":    "list of specific concrete red flags found (empty if none)",
    "summary":      "2-3 sentence plain-English verdict for the buyer",
}

_INSPECT_SYSTEM = (
    "You are a careful, experienced used-marketplace buyer helping someone decide whether to "
    "pursue a listing. You are given the FULL text of one listing and the buyer's criteria. "
    "Assess two things:\n"
    "1. DEAL: does it match what the buyer wants, and is the price fair/good for what it is? "
    "Rate 1-5 (1 = wrong item or bad value, 5 = genuinely great deal).\n"
    "2. SCAM RISK: judge from CONCRETE textual red flags only. Common ones: asking to move "
    "off-platform or pay by wire/Zelle/Venmo/gift cards; 'shipping only', 'I'm out of town / "
    "military / overseas, a third party will deliver'; refusing to meet, call, or show the "
    "item; a price far below market with a flimsy reason; urgency/pressure; a vague or "
    "copy-pasted description that doesn't match the title; no VIN/serial/plates when those "
    "would be normal; requests for a deposit or personal/financial info up front.\n"
    "Be fair: MOST listings are legitimate. Do NOT call something a scam without a specific "
    "signal — a plain, ordinary ad with a normal price is low risk. Only escalate to "
    "medium/high when you can NAME the red flags, and put each in red_flags.\n"
    "Return ONLY a JSON object: {\"deal_quality\": <1-5>, \"deal_reason\": \"...\", "
    "\"scam_risk\": \"low|medium|high\", \"red_flags\": [\"...\"], \"summary\": \"...\"}. "
    "No other text."
)


def _installed_model_names() -> set[str]:
    try:
        with httpx.Client(timeout=5.0) as c:
            r = c.get(f"{OLLAMA_URL}/api/tags")
            r.raise_for_status()
            return {m.get("name", "") for m in (r.json().get("models") or [])}
    except Exception as exc:
        log.debug("could not list installed models: %s", exc)
        return set()


def resolve_inspect_model(cfg) -> str:
    """The model Deep Inspect should use: an explicit cfg.models.inspect_model if set and
    installed; else the biggest general model from _INSPECT_PREFERENCE that IS installed;
    else the council/judge model (always available). Never picks the coder tune."""
    installed = _installed_model_names()
    pinned = getattr(cfg.models, "inspect_model", "") or ""
    if pinned and (not installed or pinned in installed):
        return pinned
    for name in _INSPECT_PREFERENCE:
        if name in installed:
            return name
    return cfg.models.effective_council_model


def verdict_from_text(title: str, body: str, criteria: str, cfg,
                      model: Optional[str] = None, timeout: float = 300.0) -> dict:
    """Run the deal/scam model over already-fetched listing text. Separated from the browser
    fetch so it's unit-testable and reusable. Returns the verdict dict (see INSPECT_SCHEMA),
    always including `model`. Raises on transport/JSON errors so the caller can report them."""
    model = model or resolve_inspect_model(cfg)
    listing = f"TITLE: {title}\n\nFULL POSTING:\n{(body or '').strip()[:8000]}"
    user_msg = f"Buyer's criteria: {criteria or '(any)'}\n\nListing:\n{listing}\n\nGive your verdict."
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _INSPECT_SYSTEM},
            {"role": "user",   "content": user_msg},
        ],
        "stream": False,
        "format": "json",
    }
    with httpx.Client(timeout=timeout) as client:
        r = client.post(f"{OLLAMA_URL}/api/chat", json=payload)
        r.raise_for_status()
    data = json.loads(r.json()["message"]["content"])
    return _normalize_verdict(data, model)


def _normalize_verdict(data: dict, model: str) -> dict:
    """Coerce the model's JSON into the documented shape — defensive against a small model
    returning a string for red_flags, an out-of-range rating, or an odd risk word."""
    try:
        dq = int(data.get("deal_quality", 3))
    except (TypeError, ValueError):
        dq = 3
    dq = max(1, min(5, dq))
    risk = str(data.get("scam_risk", "low")).strip().lower()
    if risk not in ("low", "medium", "high"):
        risk = "high" if "high" in risk else "medium" if ("med" in risk or "mod" in risk) else "low"
    flags = data.get("red_flags") or []
    if isinstance(flags, str):
        flags = [flags]
    flags = [str(f).strip() for f in flags if str(f).strip()]
    return {
        "deal_quality": dq,
        "deal_reason":  str(data.get("deal_reason", "")).strip(),
        "scam_risk":    risk,
        "red_flags":    flags,
        "summary":      str(data.get("summary", "")).strip(),
        "model":        model,
    }


def fetch_listing_text(url: str, cfg) -> dict:
    """Open the listing in a real browser and return {title, body, images}. Best-effort and
    bounded; on any failure returns empty strings so the caller can still report cleanly."""
    from web_watcher.browser import BrowserSession
    from web_watcher.monitor import extract_listing_body, dismiss_popups
    out = {"title": "", "body": "", "images": []}
    try:
        with BrowserSession(headless=cfg.browser.headless, stealth=cfg.browser.stealth) as sess:
            page = sess.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                dismiss_popups(page)
            except Exception:
                pass
            out["body"] = extract_listing_body(page)
            try:
                out["title"] = (page.title() or "").strip()
            except Exception:
                pass
            try:
                imgs = page.eval_on_selector_all(
                    "img",
                    "els => els.map(e => e.src).filter(s => s && s.startsWith('http'))",
                ) or []
                # keep a bounded, de-duped set for the (future) vision pass
                seen, kept = set(), []
                for s in imgs:
                    if s not in seen:
                        seen.add(s); kept.append(s)
                    if len(kept) >= 12:
                        break
                out["images"] = kept
            except Exception:
                pass
    except Exception as exc:
        log.warning("Deep Inspect fetch failed for %s: %s", url, exc)
    return out


_DEAD_PAGE_RE = __import__("re").compile(
    r"something went wrong|page not found|no longer available|this listing (?:was|has) ended|"
    r"item is no longer|been removed|404|access denied|are you a human|verify you are|"
    r"unusual (?:traffic|activity)|sign in to continue|log in to see",
    __import__("re").I,
)


def _looks_like_dead_page(title: str, body: str) -> bool:
    """True when the fetched text is an error / removed / bot-wall page rather than a real
    listing — so Deep Inspect reports 'couldn't read it' instead of judging error text."""
    b = (body or "").strip()
    if len(b) < 120:                       # a real posting always has more than a stub
        return True
    head = (title + " " + b[:400])
    return bool(_DEAD_PAGE_RE.search(head))


def deep_inspect_listing(url: str, criteria: str, cfg, model: Optional[str] = None) -> dict:
    """Phase-1 Deep Inspect: fetch the full posting, then a big local model returns a deal +
    scam verdict. Returns the verdict dict plus `url`, `fetched` (bool), and — on failure —
    `error`. Never raises; a failed fetch/model call is reported, not thrown."""
    model = model or resolve_inspect_model(cfg)
    got = fetch_listing_text(url, cfg)
    fetched = bool(got.get("body")) and not _looks_like_dead_page(got.get("title", ""), got["body"])
    if not fetched:
        return {"url": url, "fetched": False, "model": model,
                "error": "Couldn't read the listing page (it may be removed, login-gated, or "
                         "blocking automated access).",
                "deal_quality": None, "scam_risk": None, "red_flags": [], "summary": ""}
    try:
        v = verdict_from_text(got.get("title", ""), got["body"], criteria, cfg, model=model)
    except Exception as exc:
        log.warning("Deep Inspect verdict failed for %s: %s", url, exc)
        return {"url": url, "fetched": True, "model": model,
                "error": f"The analysis model could not be reached: {exc}",
                "deal_quality": None, "scam_risk": None, "red_flags": [], "summary": ""}
    v["url"] = url
    v["fetched"] = True
    v["images_found"] = len(got.get("images") or [])
    return v
