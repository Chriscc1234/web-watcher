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
import re
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import httpx

from web_watcher.monitor import _SEARCH_TERM_PARAMS
from web_watcher.storage import get_term_expansion, save_term_expansion

log = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434"
_MAX_TERMS = 6

# A "search term" that's really a LOCATION, DISTANCE or QUALIFIER fragment, not the item — the
# model keeps slicing these out of an instruction ("MacGregor sailboats … within a 300-mile radius
# of Anacortes" → "any model", "within 300 miles", "near Anacortes"). Searched literally, they pull
# in every random boat that merely mentions "Anacortes" or "300 miles", so they must never become a
# search URL. Dropped on BOTH generation and cache-read, so a watch already carrying junk self-heals.
_JUNK_TERM_RE = re.compile(
    r"\b(?:miles?|radius|within|near(?:by)?|around|nearest|budget|"
    r"any\s+(?:model|make|type|price|year|color|colour|size|condition)|"
    r"no\s+(?:price|limit|max)|price\s+(?:limit|range|cap))\b"
    r"|(?:under|over)\s*\$?\d",          # numeric constraints end in a digit — no trailing \b
    re.I)


# Modifier-plus-item junk: the expander dressed Charlie's Sailrite up three ways —
# "affordable Sailrite" (a price adjective is not a thing), "northwest Sailrite" (a compass
# region), "Seattle Sailrite" (a CITY typed as a keyword — the exact bot-tell the agent's
# location-box guard exists for). BUT a modifier word can also be part of a BRAND ("Western
# Flyer" bicycles), so this is only junk RELATIVE TO SIBLINGS: "northwest Sailrite" is junk
# because "Sailrite ..." stands beside it in the same expansion. See _drop_modifier_variants;
# _is_junk_term stays absolute (a lone user-typed term must never be eaten by a compass word).
_MODIFIER_JUNK_RE = re.compile(
    r"^(?:affordable|cheap|bargain|inexpensive|discount(?:ed)?|low[- ]?cost|budget|"
    r"local|nearby|"
    r"north(?:ern|west(?:ern)?|east(?:ern)?)?|south(?:ern|west(?:ern)?|east(?:ern)?)?|"
    r"east(?:ern)?|west(?:ern)?)\s+(\S.*)$", re.I)


def _leading_place_rest(term: str) -> str | None:
    """The term minus its leading town name ("Seattle Sailrite" → "Sailrite"), else None."""
    words = (term or "").split()
    if len(words) < 2 or len(words) > 3 or any(ch.isdigit() for ch in term):
        return None
    try:
        from web_watcher.cl_geo import place_latlon
        if place_latlon(words[0]) is not None:
            return " ".join(words[1:])
    except Exception:
        pass
    return None


def _drop_modifier_variants(terms: list[str]) -> list[str]:
    """Drop modifier+item variants whose ITEM already lives in a sibling term. Junk only in
    context: with "Sailrite sewing machine" present, "Seattle Sailrite" / "northwest
    Sailrite" / "affordable Sailrite" add nothing but a bot-tell; alone, "western flyer
    bicycle" is a brand and survives untouched."""
    sibling_words = {}
    for t in terms:
        sibling_words[t] = {w.lower() for w in t.split()}
    out = []
    for t in terms:
        rest = None
        m = _MODIFIER_JUNK_RE.match(t or "")
        if m:
            rest = m.group(1)
        else:
            rest = _leading_place_rest(t)
        if rest:
            rest_words = {w.lower() for w in rest.split()}
            covered = any(rest_words <= ws for o, ws in sibling_words.items() if o != t)
            if covered:
                log.info("Dropping junk search term %r (modifier variant of a sibling term)", t)
                continue
        out.append(t)
    return out


def _is_junk_term(term: str) -> bool:
    """True for a 'term' that describes WHERE or a CONSTRAINT, not WHAT — it would poison the feed."""
    t = (term or "").strip()
    return len(t) < 2 or bool(_JUNK_TERM_RE.search(t))

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

The items named above are EXAMPLES of the technique only. Expand ONLY the request you are
given — never emit a term for an example item unless it genuinely matches that request.

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
            cached = [t for t in cached if not _is_junk_term(t)]   # scrub junk from old caches too
            if cached:
                log.info("Search terms for %r served from learning cache (%d)",
                         intent[:50], len(cached))
                return cached[:_MAX_TERMS]

    avoid = [t for t in (avoid or []) if t and t.strip()]
    user_msg = f"Request: {intent}\n"
    if avoid:
        user_msg += ("Already tried these terms — return a DIFFERENT, broader set that does NOT "
                     f"just repeat them: {', '.join(avoid)}\n")
    user_msg += "Search terms:"

    try:
        from web_watcher import llm
        messages = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user_msg},
        ]

        def _usable(text: str) -> bool:
            # A usable answer is a NON-EMPTY terms list — empty/garbage here means the watch would
            # search for nothing, which is exactly when this (rare, per-watch) role earns a cloud
            # call.
            try:
                d = json.loads(llm._extract_json_text(text))
                return isinstance(d, dict) and any(str(t).strip() for t in (d.get("terms") or []))
            except Exception:
                return False

        raw = llm.chat_smart(messages, role="terms", local_model=model, cfg=None,
                             format_json=True, timeout=60.0, validate=_usable).get("text") or "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        terms = [str(t).strip() for t in (data.get("terms") or []) if str(t).strip()]
        # de-dup, drop location/qualifier junk, cap
        seen, clean = set(), []
        for t in _drop_modifier_variants(list(terms)):
            if _is_junk_term(t):
                log.info("Dropping junk search term %r (a place/constraint, not the item)", t)
                continue
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
