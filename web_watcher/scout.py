"""The Scarcity Scout — when a watch runs thin, the BOT looks wider and asks.

The admin's design, verbatim: "since there is literally 1 result, wouldn't this be the
right time to ask him about updating the distance or something? have we looked further?
we should let him know if there is anything in the area outside his search distance.
don't do it yourself. the app/bot needs to do it."

So: after a sweep of a THIN watch (few alerts despite plenty of runs), the app probes a
WIDER version of the watch's own searches — eBay nationally (ships anywhere, logged-out
safe), craigslist at a big radius — counts what exists beyond the stated area, and the
Watcher messages the owner in its own voice: what's out there, the closest examples, and
the exact words to reply with to widen the watch (which the normal chat pipeline then
applies — no new machinery).

Paced hard: at most one scout note per watch per 3 days, only when notifications are on.

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  maybe_scout        the post-sweep hook: thin? cooled down? → probe + message
  widened_urls       deterministic wider variants of the watch's own searches
  _is_thin           the trigger: alerts ≤1 despite runs ≥ threshold
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

log = logging.getLogger(__name__)

_COOLDOWN_S = 3 * 24 * 3600
_MIN_RUNS = 6
_WIDE_CL_MILES = 1000
_FILENAME = "scout_notes.json"


def _notes_path():
    from web_watcher import paths
    return paths.data_dir() / _FILENAME


def _load_notes() -> dict:
    try:
        return json.loads(_notes_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_notes(d: dict) -> None:
    try:
        _notes_path().write_text(json.dumps(d), encoding="utf-8")
    except Exception as exc:
        log.debug("could not save scout notes: %s", exc)


def widened_urls(watch) -> list[str]:
    """Wider variants of the watch's OWN searches — deterministic, no model.
    eBay national (drop the zip/distance pins) and craigslist at a big radius. At most one
    per site; OfferUp/Facebook are skipped (their radii are account/UI-bound)."""
    out, seen_sites = [], set()
    for u in (getattr(watch, "urls", None) or []):
        p = urlparse(u)
        host = p.netloc.lower()
        q = dict(parse_qsl(p.query, keep_blank_values=True))
        if "ebay." in host and "ebay" not in seen_sites:
            q.pop("_stpos", None)
            q.pop("_sadis", None)
            out.append(urlunparse(p._replace(query=urlencode(q))))
            seen_sites.add("ebay")
        elif "craigslist." in host and "craigslist" not in seen_sites:
            if q.get("search_distance"):
                q["search_distance"] = str(_WIDE_CL_MILES)
                out.append(urlunparse(p._replace(query=urlencode(q))))
                seen_sites.add("craigslist")
    # A watch with no eBay url still deserves the national look — build one from its term.
    if "ebay" not in seen_sites:
        term = _primary_term(watch)
        if term:
            from urllib.parse import quote_plus
            out.append("https://www.ebay.com/sch/i.html?_nkw=" + quote_plus(term))
    return out[:2]


def _primary_term(watch) -> str:
    from urllib.parse import unquote_plus
    counts: dict = {}
    for u in (getattr(watch, "urls", None) or []):
        for k, v in parse_qsl(urlparse(u).query):
            if k in ("q", "query", "_nkw") and v.strip():
                t = unquote_plus(v).strip().lower()
                counts[t] = counts.get(t, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def _is_thin(watch, db_path) -> bool:
    """Thin = the owner has heard almost nothing despite the watch genuinely trying."""
    try:
        from web_watcher.storage import watch_stats, alerted_count
        st = watch_stats(getattr(watch, "id", None) or watch.name, watch.name)
        if (st.get("runs") or 0) < _MIN_RUNS:
            return False
        return alerted_count(getattr(watch, "id", None) or watch.name,
                             db_path=db_path) <= 1
    except Exception:
        return False


def _relevant(listings, watch) -> list:
    """Probe results that actually contain the watch's item words — a national search page
    is full of noise; count only what the owner would recognize."""
    term = _primary_term(watch)
    kws = [k.lower() for k in (getattr(watch, "keywords", None) or []) if k.strip()]
    words = kws or [w for w in term.split() if len(w) > 3][:2]
    if not words:
        return []
    out = []
    for l in listings:
        t = (l.title or "").lower()
        if any(w in t for w in words):
            out.append(l)
    return out


def maybe_scout(watch, cfg, db_path, page, stop_event=None) -> bool:
    """The post-sweep hook. Returns True when a scout note was sent."""
    try:
        if page is None or not getattr(watch, "owner", "") and False:
            pass
        notify = getattr(watch, "notify", None)
        if not (getattr(notify, "telegram", False) or getattr(notify, "email", False)):
            return False                       # data-only watches don't nag anyone
        if not _is_thin(watch, db_path):
            return False
        notes = _load_notes()
        last = float(notes.get(watch.name, 0) or 0)
        if time.time() - last < _COOLDOWN_S:
            return False
        probes = widened_urls(watch)
        if not probes or page is None:
            return False

        from web_watcher.monitor import extract_listings, dismiss_popups
        found = []
        for u in probes:
            if stop_event is not None and stop_event.is_set():
                return False
            try:
                page.goto(u, timeout=30_000, wait_until="domcontentloaded")
                dismiss_popups(page, settle_ms=0)
                found.extend(_relevant(extract_listings(page), watch))
            except Exception as exc:
                log.debug("scout probe failed for %s: %s", u[:60], exc)
        # De-dup by key and drop anything the watch already saw locally.
        from web_watcher.storage import has_seen_listing
        uniq, seen_keys = [], set()
        for l in found:
            if l.key in seen_keys:
                continue
            seen_keys.add(l.key)
            try:
                if has_seen_listing(watch.name, l.key, db_path):
                    continue
            except Exception:
                pass
            uniq.append(l)
        # Record that we looked, whatever we found — a dry wider look is an answer too,
        # and it must not re-run every sweep.
        notes[watch.name] = time.time()
        _save_notes(notes)
        if not uniq:
            log.info("Scout: %r is thin and the wider look found nothing new either",
                     watch.name)
            return False

        samples = "\n".join(f"  • {(l.title or '?')[:70]}"
                            + (f" — {l.price}" if l.price else "")
                            for l in uniq[:3])
        term = _primary_term(watch) or "it"
        # SUGGEST ONLY WHAT WOULD ACTUALLY CHANGE THIS WATCH. The template used to offer
        # “add ebay” to watches that already search eBay — the bot must never propose
        # a no-op. Derived from the same urls the probe widened.
        hosts = " ".join(u.lower() for u in (getattr(watch, "urls", None) or []))
        offers = []
        if "ebay." not in hosts:
            offers.append(f"“add ebay to the {watch.name}”")
        if "search_distance=" in hosts or "_sadis=" in hosts:
            offers.append(f"“widen the {watch.name} to 500 miles”")
        if not offers:
            offers.append(f"“broaden the {watch.name}”")
        ask = " or ".join(offers[:2])
        msg = (f"🧭 Your “{watch.name}” has been quiet — not much "
               f"{term} inside your search area right now. I took a wider look and found "
               f"{len(uniq)} beyond it, for example:\n{samples}\n\n"
               f"Want me to search wider? Reply {ask} and I’ll set it up.")

        from web_watcher.notify import send_plain_telegram, _mirror_to_thread
        owner = str(getattr(watch, "owner", "") or "")
        ok = send_plain_telegram(msg, cfg.notifications,
                                 chat_id_override=owner or None)
        if ok:
            try:
                _mirror_to_thread(owner or str(cfg.notifications.telegram.chat_id or ""),
                                  msg)
            except Exception:
                pass
            log.info("Scout: told %s about %d wider find(s) for %r",
                     owner or "the admin", len(uniq), watch.name)
        return bool(ok)
    except Exception as exc:
        log.debug("scout pass failed for %r: %s", getattr(watch, "name", "?"), exc)
        return False
