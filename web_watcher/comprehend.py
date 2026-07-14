"""
Site comprehension — make the agent UNDERSTAND a site instead of reacting to it blind.

Two layers, exactly as designed:
  • STRUCTURE SCAN (underneath, deterministic): harvest the page's real structural signals —
    title, nav/section outline, every search box WITH its own placeholder/aria/label, and
    whether the page shows a grid of priced listing-like cards. This is the evidence.
  • COMPREHENSION PASS (on top, the big local model): reason over that evidence + a text
    sample to produce a structured UNDERSTANDING — what kind of site this is, what the search
    box is actually FOR, and whether it's even a place to monitor listings.

The understanding is cached on the site profile (storage.save/get_site_understanding) and is
used to (a) gate watch creation (reject a weather site with a plain reason) and (b) guide the
agent from comprehension ("this box is a LOCATION field") rather than a gazetteer heuristic.

KEY LOCATIONS
  scan_structure       ~L70   deterministic DOM harvest → structural evidence
  comprehend_site      ~L150  structure + text → the LLM understanding (uses the inspect model)
  understanding_for    ~L210  cached: return stored understanding or compute + persist it
  _COMPREHEND_SYSTEM   the reasoning prompt
  UNDERSTANDING_SCHEMA the documented output shape
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

log = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434"

UNDERSTANDING_SCHEMA = {
    "site_kind":         "marketplace | classifieds | store | weather | news | forum | social | other",
    "is_listings_site":  "bool — can a person find individual items/listings for sale here?",
    "search_box": {
        "present": "bool",
        "purpose": "keyword-items | location | site-search | none",
        "evidence": "the box's own label/placeholder, quoted",
    },
    "how_to_find_listings": "one plain sentence on how you'd find listings here (or why you can't)",
    "viable_for_watch":  "bool — is this a sensible site to monitor for listings?",
    "reason":            "one sentence justifying viable_for_watch",
}

_COMPREHEND_SYSTEM = (
    "You are analysing a website to decide whether it's a place to MONITOR CLASSIFIED/"
    "MARKETPLACE LISTINGS (used cars, furniture, gear for sale) and how its search works. "
    "You are given structured evidence scraped from the page: its title, its nav/section "
    "outline, each search box WITH its own placeholder/label, and whether it shows a grid of "
    "priced item cards.\n"
    "Reason from that EVIDENCE — especially each search box's own label:\n"
    "• A box labelled 'Search city or ZIP', 'Enter location', 'Find a store' is a LOCATION "
    "picker (purpose = location), NOT a keyword item search.\n"
    "• A box labelled 'Search for anything', 'Find cars, trucks…', 'Search listings' is a "
    "keyword item search (purpose = keyword-items).\n"
    "• If the nav is Forecast/Radar/Sports/News and there are no priced item cards, it is NOT "
    "a listings site (a weather or news site), even if it has a search box.\n"
    "Be decisive and honest: if it can't be used to find items for sale, say is_listings_site "
    "false and viable_for_watch false with a plain reason.\n"
    "Return ONLY a JSON object with keys: site_kind, is_listings_site (bool), search_box "
    "{present, purpose, evidence}, how_to_find_listings, viable_for_watch (bool), reason. "
    "No other text."
)

# ── Structure scan (deterministic DOM harvest) ──────────────────────────────

_STRUCTURE_JS = r"""() => {
    const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const take = (arr, n) => arr.slice(0, n);

    // Nav / section outline — the site's own labels for its parts (a light structural tree).
    const navLinks = take([...document.querySelectorAll(
        'header a, nav a, [role=navigation] a, [role=banner] a')]
        .map(a => clean(a.innerText)).filter(t => t && t.length < 30), 20);
    const headings = take([...document.querySelectorAll('h1, h2, h3')]
        .map(h => clean(h.innerText)).filter(Boolean), 15);

    // Every search-ish box, WITH its own label — the key evidence for "what is this box for".
    const boxes = [];
    document.querySelectorAll(
        'input[type=search], input[type=text], input[role=combobox], input[name*="search" i], '
        + 'input[name*="query" i], input[name*="location" i], input[name*="city" i]'
    ).forEach(el => {
        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) return;
        if (getComputedStyle(el).display === 'none' || getComputedStyle(el).visibility === 'hidden') return;
        let lbl = el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
        if (!lbl && el.id) { const l = document.querySelector('label[for="' + el.id + '"]'); if (l) lbl = l.innerText; }
        if (!lbl) { const l = el.closest('label'); if (l) lbl = l.innerText; }
        boxes.push({
            placeholder: clean(el.getAttribute('placeholder') || ''),
            aria_label:  clean(el.getAttribute('aria-label') || ''),
            name:        clean(el.getAttribute('name') || ''),
            label:       clean(lbl),
            in_search_role: !!el.closest('[role=search], form[role=search]'),
        });
        if (boxes.length >= 6) return;
    });

    // Listings signal: are there many priced item cards? (a grid of things for sale)
    const bodyText = clean(document.body ? document.body.innerText : '');
    const priceHits = (bodyText.match(/\$[\d,]{2,}/g) || []).length;
    // repeated card-like blocks: links that sit inside list/grid items
    const cardish = document.querySelectorAll(
        'li a, [class*="card"] a, [class*="listing"] a, [class*="result"] a, [data-testid] a').length;

    return {
        title: clean(document.title),
        meta:  clean((document.querySelector('meta[name=description]') || {}).content || ''),
        nav_links: navLinks,
        headings: headings,
        search_boxes: take(boxes, 6),
        price_count: priceHits,
        cardish_links: cardish,
        text_sample: bodyText.slice(0, 1200),
    };
}"""


def scan_structure(page) -> dict:
    """Deterministic DOM harvest of a page's structural evidence. Best-effort; returns {} on
    failure. No LLM — this is the raw material the comprehension pass reasons over."""
    try:
        return page.evaluate(_STRUCTURE_JS) or {}
    except Exception as exc:
        log.debug("scan_structure failed: %s", exc)
        return {}


def _evidence_block(struct: dict) -> str:
    """Render the structural scan into a compact, model-readable evidence string."""
    boxes = struct.get("search_boxes") or []
    box_lines = "\n".join(
        f"  - label={ (b.get('label') or b.get('placeholder') or b.get('aria_label') or '(none)')!r }"
        f" name={b.get('name','')!r}"
        f"{' [in a search landmark]' if b.get('in_search_role') else ''}"
        for b in boxes
    ) or "  (no search boxes found)"
    return (
        f"TITLE: {struct.get('title','')}\n"
        f"META: {struct.get('meta','')}\n"
        f"NAV / SECTIONS: {', '.join(struct.get('nav_links') or []) or '(none)'}\n"
        f"HEADINGS: {', '.join(struct.get('headings') or []) or '(none)'}\n"
        f"SEARCH BOXES (with their own labels):\n{box_lines}\n"
        f"PRICE-LIKE VALUES ON PAGE: {struct.get('price_count', 0)}  |  "
        f"CARD-LIKE LINKS: {struct.get('cardish_links', 0)}\n"
        f"TEXT SAMPLE: {(struct.get('text_sample') or '')[:900]}"
    )


def comprehend_from_structure(struct: dict, cfg, model: Optional[str] = None,
                              timeout: float = 300.0) -> dict:
    """Run the comprehension model over an already-scanned structure. Separated so it's
    unit-testable with a canned structure. Returns the understanding dict (see schema)."""
    from web_watcher.inspect import resolve_inspect_model
    model = model or resolve_inspect_model(cfg)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _COMPREHEND_SYSTEM},
            {"role": "user",   "content": "Evidence:\n" + _evidence_block(struct)},
        ],
        "stream": False,
        "format": "json",
    }
    with httpx.Client(timeout=timeout) as client:
        r = client.post(f"{OLLAMA_URL}/api/chat", json=payload)
        r.raise_for_status()
    data = json.loads(r.json()["message"]["content"])
    return _normalize_understanding(data, model)


def _normalize_understanding(data: dict, model: str) -> dict:
    """Coerce the model's JSON into the documented shape, defensively."""
    sb = data.get("search_box") or {}
    if not isinstance(sb, dict):
        sb = {}
    purpose = str(sb.get("purpose", "none")).strip().lower().replace(" ", "-")
    if purpose not in ("keyword-items", "location", "site-search", "none"):
        purpose = ("location" if "location" in purpose or "city" in purpose
                   else "keyword-items" if "keyword" in purpose or "item" in purpose
                   else "site-search" if "site" in purpose
                   else "none")
    kind = str(data.get("site_kind", "other")).strip().lower()
    return {
        "site_kind": kind or "other",
        "is_listings_site": bool(data.get("is_listings_site")),
        "search_box": {
            "present": bool(sb.get("present", bool(sb.get("evidence") or sb.get("purpose")))),
            "purpose": purpose,
            "evidence": str(sb.get("evidence", "")).strip(),
        },
        "how_to_find_listings": str(data.get("how_to_find_listings", "")).strip(),
        "viable_for_watch": bool(data.get("viable_for_watch")),
        "reason": str(data.get("reason", "")).strip(),
        "model": model,
    }


def comprehend_site(url: str, cfg, model: Optional[str] = None) -> dict:
    """Open a site, scan its structure, and return the LLM's understanding of it. Never
    raises; returns {'error': ...} on failure so callers can degrade gracefully."""
    from web_watcher.browser import BrowserSession
    from web_watcher.monitor import dismiss_popups
    struct = {}
    try:
        with BrowserSession(headless=cfg.browser.headless, stealth=cfg.browser.stealth) as sess:
            page = sess.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                dismiss_popups(page)
            except Exception:
                pass
            struct = scan_structure(page)
    except Exception as exc:
        log.warning("comprehend_site fetch failed for %s: %s", url, exc)
        return {"url": url, "error": f"couldn't open the site: {exc}"}
    if not struct:
        return {"url": url, "error": "couldn't read the site's structure"}
    try:
        u = comprehend_from_structure(struct, cfg, model=model)
    except Exception as exc:
        log.warning("comprehend_site reasoning failed for %s: %s", url, exc)
        return {"url": url, "error": f"the comprehension model could not be reached: {exc}"}
    u["url"] = url
    return u


def understanding_for(url: str, cfg, refresh: bool = False) -> dict:
    """Cached comprehension: return the stored understanding for this site, or compute it once
    (and persist it). Pass refresh=True to force a re-comprehension."""
    from web_watcher.storage import get_site_understanding, save_site_understanding
    if not refresh:
        cached = get_site_understanding(url)
        if cached:
            return cached
    u = comprehend_site(url, cfg)
    if not u.get("error"):
        try:
            save_site_understanding(url, u)
        except Exception as exc:
            log.debug("could not persist understanding for %s: %s", url, exc)
    return u
