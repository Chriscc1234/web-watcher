"""
Search-term intelligence: turn a shopping INTENT into the set of effective search
terms a savvy shopper would actually use, and build one search URL per term.

Why this exists
---------------
A keyword search matches words literally. Searching "sports car" on Craigslist returns
SUVs ("Sport Utility"); "couch" misses "sofa"/"sectional". The local model is NOT
reliable at expanding terms inside a big watch-suggestion JSON (verified), so this does
it as a FOCUSED, single-purpose call — and caches the result in a learning store
(storage.term_expansions) so the app gets better at "other ways to refer to a thing"
over time and reuses past work.

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  expand_search_terms  ~L40   intent → [terms]  (cache-first, else LLM, then cache)
  build_search_urls    ~L95   a base search URL + terms → one URL per term
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import httpx

from web_watcher.monitor import _SEARCH_TERM_PARAMS
from web_watcher.storage import get_term_expansion, save_term_expansion

log = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434"
_MAX_TERMS = 6

_SYSTEM = """\
You expand a shopper's request into the EXACT search terms they should type into a
classifieds/marketplace search box to actually find it. A search matches words
literally, so:
- Include specific brands/models/types that ARE the thing (sports car → Miata, Corvette,
  Mustang GT, 350Z, MX-5).
- Include common synonyms and alternate names (couch → sofa, sectional, loveseat).
- Include a couple of obvious misspellings if common.
- Do NOT include words that match the WRONG thing (e.g. for "sports car" avoid bare
  "sport", which matches "Sport Utility"/SUVs).
- Keep each term short (1-3 words), the way a person types into a search box.

Return ONLY JSON: {"terms": ["...", "..."]} with 3-6 terms, best first. No other text."""


def expand_search_terms(intent: str, model: str, db_path=None,
                        force: bool = False, avoid: list[str] | None = None) -> list[str]:
    """
    Return effective search terms for an intent. Checks the learning cache first; on a
    miss, makes a focused LLM call and caches the result (the cache grows over time).
    Returns [] on failure so callers can fall back to the original term.

    force=True SKIPS the cache and regenerates fresh — for when the user explicitly asks to
    change/refresh the terms (otherwise the same intent always returns the same cached set).
    avoid=[...] are terms already tried; they're shown to the model so it returns a genuinely
    DIFFERENT set. When forcing, the fresh set REPLACES the cache.
    """
    intent = (intent or "").strip()
    if not intent:
        return []

    if not force:
        cached = get_term_expansion(intent, db_path)
        if cached:
            log.info("Search terms for %r served from learning cache (%d)", intent[:50], len(cached))
            return cached[:_MAX_TERMS]

    avoid = [t for t in (avoid or []) if t and t.strip()]
    user_msg = f"Request: {intent}\n"
    if avoid:
        user_msg += ("Already tried these terms — return a DIFFERENT, broader set that does NOT "
                     f"just repeat them: {', '.join(avoid)}\n")
    user_msg += "Search terms:"

    try:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            "stream": False,
            "format": "json",
        }
        with httpx.Client(timeout=60.0) as client:
            r = client.post(f"{OLLAMA_URL}/api/chat", json=payload)
            r.raise_for_status()
        raw = r.json()["message"]["content"]
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        terms = [str(t).strip() for t in (data.get("terms") or []) if str(t).strip()]
        # de-dup, cap
        seen, clean = set(), []
        for t in terms:
            k = t.lower()
            if k not in seen:
                seen.add(k); clean.append(t)
        clean = clean[:_MAX_TERMS]
        if clean:
            # On a forced refresh, REPLACE the cache so the old set is gone; otherwise grow it.
            save_term_expansion(intent, clean, db_path, replace=force)
            log.info("Expanded %r → %s%s", intent[:50], clean, " (forced refresh)" if force else "")
        return clean
    except Exception as exc:
        log.warning("Search-term expansion failed for %r: %s", intent[:50], exc)
        return []


def _search_param(query: dict) -> str | None:
    for k in _SEARCH_TERM_PARAMS:
        if k in query:
            return k
    return None


def build_search_urls(base_url: str, terms: list[str]) -> list[str]:
    """
    Given one search-results URL and a list of terms, return one URL per term (the search
    param swapped to each term, other filters preserved). Returns [base_url] if the URL
    has no recognizable search param or there are no terms.
    """
    if not terms:
        return [base_url]
    try:
        parts = urlparse(base_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        param = _search_param(query)
        if not param:
            return [base_url]
        urls = []
        for t in terms:
            q = dict(query)
            q[param] = t
            urls.append(urlunparse(parts._replace(query=urlencode(q))))
        return urls
    except Exception as exc:
        log.debug("build_search_urls failed: %s", exc)
        return [base_url]
