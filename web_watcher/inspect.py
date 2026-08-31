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
from pathlib import Path as pathlib_Path
from urllib.parse import urlparse
from typing import Optional

import httpx

log = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434"

# Preference order for the deep model when the user hasn't pinned one: a GENERAL reasoning
# model (NOT the coder tune — coding models are the wrong tool for reading an ad and judging a
# scam), QUALITY first — but quality the machine can actually deliver. qwen3:14b leads: its
# thinking mode gives deep per-listing reasoning (~30s, fine for an on-demand vet) while fitting
# a 16GB card entirely on GPU. The huge models come AFTER it deliberately: on this hardware a
# 47GB qwen2.5:72b runs mostly on CPU and a "quality tier" that takes minutes per listing is
# worse than a thoughtful one that takes thirty seconds. Bigger-is-better only when it fits.
_INSPECT_PREFERENCE = ("qwen3:14b", "qwen2.5:72b", "qwen2.5:32b", "llama3.3:70b", "qwen2.5:14b")

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
                      model: Optional[str] = None, timeout: float = 300.0,
                      known: Optional[dict] = None) -> dict:
    """Run the deal/scam model over already-fetched listing text. Separated from the browser
    fetch so it's unit-testable and reusable. Returns the verdict dict (see INSPECT_SCHEMA),
    always including `model`. Raises on transport/JSON errors so the caller can report them."""
    model = model or resolve_inspect_model(cfg)
    # KNOWN FACTS FIRST. The price, the source site and the posting date usually live in the
    # listing's TITLE and metadata, not its prose — so a model given only the ad body concludes
    # "no price mentioned" about a listing whose price we have known all along. Everything we
    # already stored is stated up front, plainly labelled, ahead of the free text.
    lines = []
    if title:
        lines.append(f"TITLE: {title}")
    for label, key in (("PRICE", "price_text"), ("SOURCE", "source"),
                       ("POSTED", "posted_at"), ("LOCATION", "location"),
                       ("YEAR", "year"), ("MILEAGE", "mileage")):
        val = str((known or {}).get(key) or "").strip()
        if val and val.lower() not in ("none", "0"):
            lines.append(f"{label}: {val}")
    lines.append(f"\nFULL POSTING:\n{(body or '').strip()[:8000]}")
    listing = "\n".join(lines)
    user_msg = (f"Buyer's criteria: {criteria or '(any)'}\n\nListing:\n{listing}\n\n"
                "The TITLE/PRICE/SOURCE lines above are facts already confirmed about this "
                "listing — treat them as true even if the posting text never repeats them. "
                "Never say the price is unknown when a PRICE line is given.\n\n"
                "Give your verdict.")
    from web_watcher import llm
    messages = [
        {"role": "system", "content": _INSPECT_SYSTEM},
        {"role": "user",   "content": user_msg},
    ]

    def _usable(text: str) -> bool:
        # A real verdict, not any JSON: it must carry a deal rating. A local model that returns
        # junk here is exactly when a person asked about ONE listing and it's worth a cloud dollar.
        try:
            d = json.loads(llm._extract_json_text(text))
            return isinstance(d, dict) and d.get("deal_quality") is not None
        except Exception:
            return False

    # Deep Inspect runs the big local model first, and escalates to Claude only if that verdict
    # fails the check — vetting is on-demand (a person asked about this one listing), so it's rare
    # and consequential, which is exactly what the cloud budget is for.
    res = llm.chat_smart(messages, role="vet", local_model=model, cfg=cfg,
                         format_json=True, timeout=timeout, validate=_usable)
    data = json.loads(llm._extract_json_text(res.get("text") or "{}"))
    used = res.get("used") or "local"
    return _normalize_verdict(data, model if used == "local" else used)


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


def _needs_login_profile(url: str) -> bool:
    """Sites where a fresh browser means a login wall, not a listing."""
    host = urlparse(url).netloc.lower()
    return "facebook." in host


def _archived_text(known: dict) -> str:
    """Readable text out of the frozen MHTML copy, if one was kept. Crude on purpose —
    quoted-printable decode the html part, strip tags — the judge needs prose, not DOM."""
    try:
        import quopri, re as _re
        path = str((known or {}).get("archive_path") or "")
        if not path:
            return ""
        raw = pathlib_Path(path).read_text(encoding="utf-8", errors="ignore")
        i = raw.lower().find("content-type: text/html")
        if i < 0:
            return ""
        chunk = raw[i:i + 400_000]
        j = chunk.find("\n\n")
        body = chunk[j:] if j > 0 else chunk
        body = quopri.decodestring(body.encode("utf-8", "ignore")).decode("utf-8", "ignore")
        body = _re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=_re.S | _re.I)
        text = _re.sub(r"<[^>]+>", " ", body)
        text = _re.sub(r"\s+", " ", text).strip()
        return text[:8000] if len(text) >= 200 else ""
    except Exception as exc:
        log.debug("could not read archive copy: %s", exc)
        return ""


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
    r"unusual (?:traffic|activity)|sign in to continue|log in to see|"
    # Facebook's logged-out interstitials — one of these slipped past and the model judged
    # WALL TEXT, confidently calling a real listing a scam.
    r"log in or sign up|you must log in|create new account|join facebook",
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

    # What we already stored when the watch first found it. This is the difference between a
    # dead link being a dead end and a dead link still being useful.
    known = {}
    try:
        from web_watcher import storage
        known = storage.get_listing_by_url(url) or {}
    except Exception as exc:
        log.debug("Deep Inspect: no stored record for %s: %s", url, exc)

    # OFFLINE FIRST. The user's question, verbatim: "why does the vetter have to open the
    # page again, when the page has already been scraped? shouldn't we have a full copy
    # offline?" We do — the deep-read stored the ad body, and matches carry a frozen MHTML
    # archive. Reading our own copy is instant, burns no browser, can't hit a login wall,
    # and can't get the account flagged. The live page is only for listings we never read.
    # On LOGIN sites (facebook) we NEVER live-fetch here: a fresh vetter browser is
    # logged-out by design (the login profile belongs to the sweeps), so a live visit
    # yields a wall — which is exactly how a real listing got vetted as "a scam".
    got = {"title": "", "body": "", "images": []}
    stored_body = str(known.get("details") or "").strip()
    if len(stored_body) >= 120:
        got = {"title": str(known.get("title") or ""), "body": stored_body, "images": []}
        log.info("Deep Inspect: judging %s from the stored ad body (%d chars, no browser)",
                 url[:60], len(stored_body))
    else:
        arch = _archived_text(known)
        if arch:
            got = {"title": str(known.get("title") or ""), "body": arch, "images": []}
            log.info("Deep Inspect: judging %s from the frozen archive copy", url[:60])
        elif _needs_login_profile(url):
            log.info("Deep Inspect: %s is on a login site and we hold no copy — not "
                     "opening a logged-out browser at it", url[:60])
        else:
            got = fetch_listing_text(url, cfg)
    fetched = bool(got.get("body")) and not _looks_like_dead_page(got.get("title", ""), got["body"])
    if not fetched:
        # The page is gone (or gated, or blocking us). Report that plainly AND hand back
        # everything we saved — a listing that 404s is exactly when the saved copy matters most,
        # and answering "couldn't read it" while sitting on the title, price and photo is the
        # least useful thing we could do.
        out = {"url": url, "fetched": False, "model": model,
               "error": "Couldn't open the listing page — it looks removed, login-gated, or it's "
                        "blocking automated access.",
               "deal_quality": None, "scam_risk": None, "red_flags": [], "summary": ""}
        if known:
            out["known"] = known
            out["summary"] = ("This listing is no longer readable, but here's what was saved when "
                              "it was found.")
        return out
    try:
        v = verdict_from_text(got.get("title", "") or str(known.get("title") or ""),
                              got["body"], criteria, cfg, model=model, known=known)
    except Exception as exc:
        log.warning("Deep Inspect verdict failed for %s: %s", url, exc)
        return {"url": url, "fetched": True, "model": model,
                "error": f"The analysis model could not be reached: {exc}",
                "deal_quality": None, "scam_risk": None, "red_flags": [], "summary": ""}
    v["url"] = url
    v["fetched"] = True
    v["images_found"] = len(got.get("images") or [])
    if known:
        v["known"] = known
    return v
