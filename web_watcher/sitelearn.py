"""
Site learner — "point Web Watcher at a new site and it learns the layout".

You give it a URL (ideally a search-results or category page). It drives a real browser
to explore the page the way the agent would, harvests structural signals, and persists a
reusable SITE PROFILE (storage.site_profiles):

  • listing-URL shape  — a regex (one capture = the listing id) so dedup keys are stable
  • search box         — which query param carries the term (so term-swapping works)
  • sort options       — so continuous sweeps can vary the feed
  • a human-readable note about the layout (optional LLM polish, offline)

The id-pattern inference (monitor.infer_listing_pattern) and the profile assembly
(synthesize_profile) are deterministic and unit-tested without a browser; learn_site is
the thin live-exploration wrapper around them.

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  synthesize_profile  ~L70   Pure: (url, sample listing urls, search/sort info) -> profile
  learn_site          ~L130  Live: drive a browser, harvest signals, persist a profile
  _SEARCH_SORT_JS     ~L250  DOM scrape for the search input + sort options
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import httpx

from web_watcher.browser import BrowserSession, NAV_TIMEOUT
from web_watcher.monitor import (
    _EXTRACT_JS, _PRICE_RE, _looks_idlike, infer_listing_pattern,
    dismiss_popups, human_scroll, is_login_wall,
)
from web_watcher.storage import site_key, upsert_site_profile, get_site_profile

log = logging.getLogger(__name__)

OLLAMA_URL     = "http://localhost:11434"
OLLAMA_TIMEOUT = 45.0

# Query-param names that commonly carry a sort/order option.
_SORT_PARAMS = ("sort", "sortby", "sort_by", "order", "orderby", "srt", "_sop")

# Sites Web Watcher can read WITHOUT a learned profile — the extractor has built-in
# URL patterns for these (see monitor._listing_key).
BUILTIN_SITES = frozenset({"craigslist.org", "ebay.com", "facebook.com"})


def site_status(url: str, db_path=None) -> dict:
    """Does Web Watcher already know how to read this site? Returns
    {domain, known, kind}: kind is 'builtin' | 'learned' | 'unknown'."""
    sk = site_key(url)
    if not sk:
        return {"domain": "", "known": False, "kind": "unknown"}
    if sk in BUILTIN_SITES:
        return {"domain": sk, "known": True, "kind": "builtin"}
    if get_site_profile(url, db_path):
        return {"domain": sk, "known": True, "kind": "learned"}
    return {"domain": sk, "known": False, "kind": "unknown"}


def unknown_sites(urls, db_path=None) -> list[str]:
    """The distinct registrable domains among `urls` that Web Watcher has NOT explored yet
    (not built-in, no learned profile) — i.e. the sites a new watch should explore first."""
    seen, out = set(), []
    for u in urls or []:
        st = site_status(u, db_path)
        if st["domain"] and not st["known"] and st["domain"] not in seen:
            seen.add(st["domain"])
            out.append(st["domain"])
    return out


def first_url_for_domain(urls, domain: str) -> str:
    """The first URL in `urls` whose registrable site key matches `domain` (the page the
    learner should explore for that site)."""
    for u in urls or []:
        if site_key(u) == domain:
            return u
    return ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _path_has_idlike(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        segs = [s for s in (urlparse(url).path or "").split("/") if s]
    except Exception:
        return False
    return any(_looks_idlike(s) for s in segs)


# ---------------------------------------------------------------------------
# Pure profile assembly (no browser — unit tested)
# ---------------------------------------------------------------------------

def synthesize_profile(
    url:           str,
    listing_urls:  list[str],
    search_info:   dict | None = None,
    sort_info:     dict | None = None,
    display_name:  str | None = None,
    notes:         str = "",
    learned_at:    str = "",
) -> dict:
    """
    Assemble a site profile from harvested signals. Deterministic; safe to unit-test.

    listing_urls — a sample of listing links seen on the page (more is better).
    search_info  — {"param": <query-param name>, "action": <form action url>} or None.
    sort_info    — {"param": <name>, "values": [...]} or None.
    """
    sk = site_key(url)
    profile: dict = {
        "domain":       sk,
        "display_name": display_name or sk,
        "key_prefix":   sk.split(".")[0] if sk else "listing",
        "notes":        notes,
        "learned_at":   learned_at or _now_iso(),
    }

    same_site = [u for u in (listing_urls or []) if site_key(u) == sk]
    pat = infer_listing_pattern(same_site)
    if pat:
        profile["listing_url_regex"] = pat["listing_url_regex"]
        profile["key_prefix"]        = pat.get("key_prefix") or profile["key_prefix"]
        profile["sample_listing_url"] = next(iter(same_site), "")

    if search_info:
        profile["search_param"]        = (search_info.get("param") or "").strip()
        profile["search_url_template"] = (search_info.get("action") or "").strip()

    if sort_info:
        profile["sort_param"]  = (sort_info.get("param") or "").strip()
        vals = sort_info.get("values") or []
        profile["sort_values"] = [str(v) for v in vals if str(v).strip()][:8]

    return profile


def profile_confirms(profile: dict, sample_url: str) -> bool:
    """Sanity check: does the inferred regex actually produce a key for a sample listing
    URL? Used by learn_site to report whether the learned shape is trustworthy."""
    import re
    rgx = profile.get("listing_url_regex")
    if not rgx or not sample_url:
        return False
    try:
        from urllib.parse import urlparse
        return bool(re.search(rgx, urlparse(sample_url).path or ""))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Live exploration
# ---------------------------------------------------------------------------

def learn_site(
    url:          str,
    model:        str = "",
    db_path=None,
    headless:     bool = True,
    persistent:   bool = False,
    profile_dir=None,
    page=None,
    scroll_passes: int = 3,
) -> dict:
    """
    Explore a site once and persist a learned profile. Returns:
      {"ok": bool, "domain": str, "profile": dict|None, "confirmed": bool,
       "listing_samples": [...], "error": str|None}

    Pass an already-open `page` (e.g. from the continuous browser) to reuse it; otherwise
    a temporary BrowserSession is opened and closed. Never enters credentials: if the page
    is a login wall it stops and reports that a login profile is needed.
    """
    sk = site_key(url)
    own_session = page is None
    session = None
    try:
        if own_session:
            session = BrowserSession(
                headless=headless, stealth=True,
                persistent=persistent, profile_dir=profile_dir,
            )
            session.__enter__()
            page = session.new_page()

        log.info("learn_site: exploring %s", url)
        page.goto(url, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
        dismiss_popups(page)
        if is_login_wall(page):
            return {"ok": False, "domain": sk, "profile": None, "confirmed": False,
                    "listing_samples": [],
                    "error": "Site is behind a login wall — needs a logged-in profile to explore."}

        human_scroll(page, passes=scroll_passes)
        dismiss_popups(page, settle_ms=0)

        # ── Harvest candidate listing URLs ──────────────────────────────────
        try:
            raw = page.evaluate(_EXTRACT_JS) or []
        except Exception as exc:
            log.warning("learn_site: card extraction failed: %s", exc)
            raw = []
        priced = [(e.get("href") or "") for e in raw if _PRICE_RE.search(e.get("text") or "")]
        priced_urls = list(dict.fromkeys(
            h for h in priced if site_key(h) == sk and _path_has_idlike(h)))
        # PRICED cards are a strong "this is a real listing" signal — a results page shows
        # many. The id-like-anchor fallback below also catches CATEGORY/nav links, so it's
        # only a weak hint used to round out a page that already had some priced listings.
        strong = len(priced_urls) >= 3
        listing_urls = list(priced_urls)
        if len(listing_urls) < 3:
            try:
                all_hrefs = page.evaluate(
                    "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)"
                ) or []
            except Exception:
                all_hrefs = []
            extra = [h for h in all_hrefs if site_key(h) == sk and _path_has_idlike(h)]
            listing_urls = list(dict.fromkeys(listing_urls + extra))

        # ── Harvest search box + sort options ───────────────────────────────
        try:
            ss = page.evaluate(_SEARCH_SORT_JS) or {}
        except Exception as exc:
            log.warning("learn_site: search/sort scrape failed: %s", exc)
            ss = {}
        search_info = _resolve_search(ss.get("search"), page.url)
        sort_info   = _resolve_sort(ss.get("sort"), page.url)

        profile = synthesize_profile(
            url, listing_urls, search_info, sort_info, learned_at=_now_iso())

        sample = profile.get("sample_listing_url") or next(iter(listing_urls), "")
        confirmed = profile_confirms(profile, sample)

        # ── Optional offline LLM polish: a friendly name + a layout note ─────
        if model:
            try:
                polish = _polish_profile(model, url, profile, raw)
                if polish.get("display_name"):
                    profile["display_name"] = polish["display_name"]
                if polish.get("notes"):
                    profile["notes"] = polish["notes"]
            except Exception as exc:
                log.debug("learn_site: LLM polish skipped: %s", exc)

        # Only persist when we actually inferred a listing-URL shape. A profile with an
        # empty regex would POISON the site: _listing_key treats "a profile exists for
        # this host" as authoritative and returns None for every URL, killing detection.
        # A failed learn must leave the site on the built-in/generic fallback instead.
        # Persist only a TRUSTWORTHY profile: we saw enough priced listing cards (a real
        # results page, not a category landing page) AND the inferred shape confirms against
        # a sample. Otherwise leave the site on the built-in/generic fallback and tell the
        # user to point the learner at an actual results page.
        if profile.get("listing_url_regex") and strong and confirmed:
            upsert_site_profile(profile, db_path=db_path)
            log.info("learn_site: learned %s (regex=%r, %d priced samples)",
                     sk, profile["listing_url_regex"], len(priced_urls))
            return {
                "ok": True, "domain": sk,
                "profile": get_site_profile(url, db_path),
                "confirmed": True,
                "listing_samples": priced_urls[:5],
                "error": None,
            }
        log.info("learn_site: no trustworthy pattern for %s (regex=%s strong=%s confirmed=%s)"
                 " — not saved", sk, bool(profile.get("listing_url_regex")), strong, confirmed)
        if profile.get("listing_url_regex"):
            return {
                "ok": False, "domain": sk, "profile": None, "confirmed": False,
                "listing_samples": list(dict.fromkeys(listing_urls))[:5],
                "error": "I explored the page but didn't see enough individual listings with "
                         "prices to be confident — this looks like a category/landing page. "
                         "Point me at a SEARCH-RESULTS page that shows listings (with prices) "
                         "and I'll learn it.",
            }
        return {
            "ok": False, "domain": sk, "profile": None, "confirmed": False,
            "listing_samples": list(dict.fromkeys(listing_urls))[:5],
            "error": "Explored the page but could not infer a listing-URL pattern. This "
                     "site likely renders listings dynamically (a JS/SPA site). Point the "
                     "learner at a search-results page that shows listings, or use an "
                     "autonomous watch for it.",
        }
    except Exception as exc:
        log.warning("learn_site failed for %s: %s", url, exc)
        return {"ok": False, "domain": sk, "profile": None, "confirmed": False,
                "listing_samples": [], "error": str(exc)}
    finally:
        if own_session and session is not None:
            try:
                session.__exit__(None, None, None)
            except Exception:
                pass


def _resolve_search(search: dict | None, landing_url: str) -> dict | None:
    """Prefer a recognized search param already present in the landing URL (that's the
    param a results URL actually uses); fall back to the form input's name."""
    from urllib.parse import urlparse, parse_qsl
    from web_watcher.monitor import _SEARCH_TERM_PARAMS
    try:
        q = dict(parse_qsl(urlparse(landing_url).query))
    except Exception:
        q = {}
    for k in _SEARCH_TERM_PARAMS:
        if k in q:
            return {"param": k, "action": landing_url}
    if search and (search.get("name") or "").strip():
        return {"param": search["name"].strip(), "action": (search.get("action") or "").strip()}
    return None


def _resolve_sort(sort: dict | None, landing_url: str) -> dict | None:
    if sort and (sort.get("param") or "").strip() and (sort.get("values") or []):
        return {"param": sort["param"].strip(), "values": sort["values"]}
    return None


# DOM scrape: locate the page's search input and any sort control.
_SEARCH_SORT_JS = r"""() => {
    const res = {search: null, sort: null};
    const SEARCHY = /search|query|keyword|find|nkw/i;

    const inputs = Array.from(document.querySelectorAll('input'));
    for (const el of inputs) {
        const type = (el.getAttribute('type') || '').toLowerCase();
        if (type && !['search', 'text', ''].includes(type)) continue;
        const name = el.getAttribute('name') || '';
        const ctx  = (el.getAttribute('placeholder') || '') + ' '
                   + (el.getAttribute('aria-label') || '') + ' ' + name;
        if (type === 'search' || SEARCHY.test(ctx)) {
            const form = el.closest('form');
            res.search = {
                name:   name,
                type:   type,
                action: form ? form.action : '',
                method: form ? (form.method || 'get') : 'get',
            };
            break;
        }
    }

    // <select name~=sort>
    const SORTP = /^(sort|sortby|sort_by|order|orderby|srt|_sop)$/i;
    for (const s of Array.from(document.querySelectorAll('select'))) {
        const name = s.getAttribute('name') || '';
        if (SORTP.test(name) || /sort|order/i.test(name)) {
            const vals = Array.from(s.options).map(o => o.value).filter(Boolean).slice(0, 8);
            if (vals.length) { res.sort = {param: name, values: vals}; break; }
        }
    }
    // fallback: anchors carrying a sort-ish query param
    if (!res.sort) {
        const found = {};
        for (const a of document.querySelectorAll('a[href]')) {
            let u; try { u = new URL(a.href); } catch (e) { continue; }
            u.searchParams.forEach((v, k) => {
                if (SORTP.test(k) && v) { (found[k] = found[k] || []).push(v); }
            });
        }
        const keys = Object.keys(found);
        if (keys.length) {
            const k = keys[0];
            res.sort = {param: k, values: Array.from(new Set(found[k])).slice(0, 8)};
        }
    }
    return res;
}"""


_POLISH_SYSTEM = """\
You are given facts about a classifieds/marketplace website that an automated tool just
explored. Write a SHORT friendly display name and a one-sentence note describing the page
layout (where listings and the search box are). Return ONLY JSON:
{"display_name": "...", "notes": "..."}  — no other text."""


def _polish_profile(model: str, url: str, profile: dict, raw_cards: list) -> dict:
    sample_titles = [
        (c.get("text") or c.get("alt") or "").strip()[:60]
        for c in raw_cards[:6] if (c.get("text") or c.get("alt"))
    ]
    facts = (
        f"URL: {url}\n"
        f"Site: {profile.get('domain')}\n"
        f"Listing-URL pattern: {profile.get('listing_url_regex') or '(none inferred)'}\n"
        f"Search param: {profile.get('search_param') or '(unknown)'}\n"
        f"Example listing titles: {sample_titles}\n"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _POLISH_SYSTEM},
            {"role": "user",   "content": facts},
        ],
        "stream": False,
        "format": "json",
    }
    with httpx.Client(timeout=OLLAMA_TIMEOUT) as client:
        r = client.post(f"{OLLAMA_URL}/api/chat", json=payload)
        r.raise_for_status()
    raw = r.json()["message"]["content"]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
    return {
        "display_name": (data.get("display_name") or "").strip()[:60],
        "notes":        (data.get("notes") or "").strip()[:300],
    }
