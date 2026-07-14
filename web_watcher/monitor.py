"""
Continuous-monitor helpers: listing extraction, search variation, scrolling.

Continuous-mode watches sweep a search results / feed page repeatedly and alert on
NEW listings only. These helpers do the site-facing work that the sweep loop in
scheduler.py orchestrates:

  extract_listings()  — pull listing cards (stable key + url + title + price) from
                        the DOM by URL pattern (eBay /itm/, FB /marketplace/item/,
                        generic /item|/listing). Stable keys make dedup reliable.
  vary_search()       — rotate sort order / price band each sweep so a non-deterministic
                        feed (Facebook Marketplace especially) surfaces different
                        inventory across passes instead of hiding the same set.
  human_scroll()      — scroll the feed in human-paced bursts to lazy-load more cards.
  is_login_wall()     — detect that a login-required site logged us out so the caller
                        can notify the user to reconnect instead of scraping a login page.

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  Listing            ~L40   Dataclass: key, url, title, price
  infer_listing_pattern ~L160 Deterministic listing-URL regex inference (learn-a-site)
  _listing_key       ~L240  URL → stable dedup key (built-ins + learned profiles)
  extract_listings   ~L290  DOM scrape → list[Listing] (dedup within a sweep; profile-aware)
  extract_listing_body ~L190 Ad body + attributes off a single listing-detail page
  vary_search        ~L150  Rotate sort/price params per sweep (anti-algorithm)
  dismiss_popups     ~L259  Close FB login modal / cookie banners that block scraping
  has_blocking_overlay ~L316 Fast no-wait probe: is an overlay in the way? (agent gate)
  humanized_search   ~L390  Type the query into the search box (keyboard+mouse) vs URL-jump
  human_scroll       ~L460  Human-paced scroll bursts
  is_login_wall      ~L360  Login-wall / logged-out detection
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from playwright.sync_api import Page

log = logging.getLogger(__name__)


@dataclass
class Listing:
    key:     str          # stable dedup key (listing id, else normalized URL)
    url:     str          # absolute listing URL
    title:   str = ""     # listing title / card text (trimmed)
    price:   str = ""     # first price found in the card text, e.g. "$165"
    details: str = ""     # ad body / attributes, captured by visiting the listing page
    image:   str = ""     # thumbnail image URL from the card (best-effort), for the Results view
    posted_at: str = ""   # when the listing was POSTED (from the ad page's <time>), if read
    rating:  int | None = None   # 1-5 graded match, set by the rating judge
    judge_reason: str = ""       # the judge's / prefilter's one-line verdict


# ---------------------------------------------------------------------------
# Listing extraction
# ---------------------------------------------------------------------------

# JS that returns candidate listing anchors with their card text. We resolve
# absolute URLs in the browser (a.href is already absolute) and climb to the
# enclosing "card" so the returned text includes both the title AND the price
# (many listing grids wrap the image and the title in separate anchors that share
# the same href, so we capture the surrounding card rather than the bare anchor).
_EXTRACT_JS = """() => {
    // Count how many DISTINCT listing links an element contains — used to stop the card
    // climb before it ascends into a container holding MORE THAN ONE listing (e.g. the
    // whole results list), which would pull a neighbour's text and mislabel this listing.
    const listingHrefs = (el) => {
        const seen = new Set();
        el.querySelectorAll('a[href]').forEach(x => {
            const h = (x.href || '').split('?')[0];
            if (/\\/\\d{7,}(?:\\.html|\\/|$)|\\/(?:itm|item|listing|offer|marketplace\\/item)\\/|\\/view\\/[dp]\\//i.test(h))
                seen.add(h);
        });
        return seen.size;
    };
    const out = [];
    const anchors = Array.from(document.querySelectorAll('a[href]'));
    for (const a of anchors) {
        const href = a.href || '';
        if (!href) continue;
        // Climb until the card text includes BOTH a title and a price (the price is
        // usually a sibling of the title, one level up), up to 6 levels. Stop BEFORE
        // ascending into a container that holds more than one listing — that overshoot
        // was pulling the first result's text onto every card (mislabelled listings +
        // false "N dupes"). A length cap also guards runaway climbing on pages with no price.
        let card = a;
        for (let i = 0; i < 6 && card.parentElement; i++) {
            const t = (card.innerText || '').trim();
            const hasPrice = /[$£€]\\s?\\d/.test(t);
            if (hasPrice && t.length >= 25) break;   // ideal: title + price captured
            if (t.length >= 160) break;              // runaway guard
            if (listingHrefs(card.parentElement) > 1) break;  // don't merge neighbours
            card = card.parentElement;
        }
        const text = ((card.innerText || a.innerText || a.getAttribute('aria-label') || '')
                        .replace(/\\s+/g, ' ')
                        .trim())
                        .slice(0, 240);
        // Image alt text is a good title fallback for image-only anchors; the image src
        // gives the Results view a thumbnail. Prefer the actually-rendered photo
        // (currentSrc handles <img srcset>/lazy-load) and ignore data:/svg placeholders.
        let alt = '', image = '';
        const img = a.querySelector('img') || (card.querySelector ? card.querySelector('img') : null);
        if (img) {
            alt = (img.getAttribute('alt') || '').trim().slice(0, 160);
            const src = img.currentSrc || img.src ||
                        img.getAttribute('data-src') || img.getAttribute('data-srcset') || '';
            if (src && /^https?:/i.test(src)) image = src;
        }
        out.push({ href, text, alt, image });
    }
    return out;
}"""

# Per-site listing-detail URL patterns (capture group = the stable listing id).
# ID-length minimums skip placeholder/example links (real ids are long).
_PAT_EBAY    = re.compile(r"/itm/(?:[^/]+/)?(\d{10,})")          # ebay item ids ~12 digits
_PAT_FB      = re.compile(r"/marketplace/item/(\d{10,})")        # fb marketplace ids are long
# Craigslist listing ids. NEW format (2026): /view/d/<slug>/<alphanumeric-id>, e.g.
# www.craigslist.org/view/d/seattle-2015-ford/fvjPbmYUxaNP8qvENeUHUn — no ".html", the id is
# a base62-ish token. LEGACY format: /<numeric-id>.html. We match new first, then legacy, so
# both current pages and old stored URLs resolve. (Dropping ".html" broke extraction entirely.)
_PAT_CL_NEW  = re.compile(r"/view/(?:d|p)/[^/]+/([A-Za-z0-9_-]{8,})")
_PAT_CL      = re.compile(r"/(\d{8,})\.html")                    # legacy craigslist post ids
_PAT_GENERIC = re.compile(r"/(?:item|listing|product|offer)/[^?#]*?(\d{7,})")
# Last-resort for UNKNOWN hosts only: a path segment that is a long numeric id.
_PAT_NUMERIC = re.compile(r"/(\d{9,})(?:[/?#]|$)")

_PRICE_RE = re.compile(r"[$£€]\s?\d[\d,]*(?:\.\d{2})?")
# eBay/marketplace UI badges that prefix card text but aren't part of the title.
_BADGE_RE = re.compile(r"^(?:NEW LISTING|SPONSORED|TOP RATED PLUS|BRAND NEW)\s*", re.I)
# Leading separator/glyph junk that prefixes card text — most notably Craigslist's
# image-carousel dots ("• • • • …") which otherwise become the start of the title.
_LEADING_JUNK_RE = re.compile(r"^[\s•·‣▪◦●○|–—\-]+")
# Relative "posted X ago" stamps Craigslist appends to cards (e.g. "<1hr ago", "2h ago",
# "35 mins ago"). These CHANGE between sightings of the SAME listing, so they must be
# stripped from both titles and fingerprints — otherwise one listing looks like many.
_REL_TIME = r"<?\s*\d+\s*(?:mins?|minutes?|hours?|hrs?|hr|h|days?|d|weeks?|w|mo)\s*ago"
# Everything from the time stamp onward in a card is metadata (time/mileage/price/city),
# not the listing title — cut the title there.
_META_RE = re.compile(r"\s*" + _REL_TIME + r".*$", re.I)


# A path segment that "looks like a listing id": contains at least one digit, is built
# from id-safe characters, and is long enough to not be a word like "cta" or a year.
# Captures an optional file extension (Craigslist ids end in ".html") separately.
_ID_SEG_RE = re.compile(r"^(?P<core>[A-Za-z0-9_.-]*\d[A-Za-z0-9_.-]*?)(?P<ext>\.[a-z]{1,5})?$")


def _looks_idlike(seg: str) -> bool:
    m = _ID_SEG_RE.match(seg or "")
    return bool(m) and len(m.group("core")) >= 5


def infer_listing_pattern(urls: list[str]) -> dict | None:
    """
    Infer a site's listing-URL shape from a sample of listing links — the core of
    "point Web Watcher at a new site and it learns the layout".

    Returns {listing_url_regex, key_prefix, id_kind, sample_path, support} or None when
    there isn't enough signal. The regex has ONE capture group = the stable listing id.

    Method (no LLM, fully deterministic so it's testable and reliable):
      1. Group sample URLs by path-segment count; take the dominant group.
      2. Per segment position, a value that is constant across the sample is a literal;
         one that varies is a wildcard. The listing id = the LAST varying, id-like column.
      3. Numeric ids become (\\d{N,}); mixed alphanumeric ids become a bounded id charclass.
    """
    parsed: list[list[str]] = []
    raw_for_prefix = ""
    for u in urls:
        try:
            path = urlparse(u).path.rstrip("/")
        except Exception:
            continue
        segs = [s for s in path.split("/") if s]
        if segs and any(_looks_idlike(s) for s in segs):
            parsed.append(segs)
            raw_for_prefix = raw_for_prefix or (urlparse(u).netloc or "")
    if not parsed:
        return None

    from collections import Counter
    target_len = Counter(len(s) for s in parsed).most_common(1)[0][0]
    group = [s for s in parsed if len(s) == target_len]
    n = target_len

    colvals: list[set[str]] = [set() for _ in range(n)]
    for segs in group:
        for i in range(n):
            colvals[i].add(segs[i])

    # The id column: the LAST column that varies (or, with a single sample, is id-like)
    # and whose values look like listing ids.
    id_col = None
    single = len(group) == 1
    for i in range(n):
        varies = len(colvals[i]) > 1 or single
        if not varies:
            continue
        idlike = sum(1 for v in colvals[i] if _looks_idlike(v))
        if idlike >= max(1, len(colvals[i]) // 2):
            id_col = i
    if id_col is None:
        return None

    cores, exts = [], set()
    for v in colvals[id_col]:
        m = _ID_SEG_RE.match(v)
        if m:
            cores.append(m.group("core")); exts.add(m.group("ext") or "")
        else:
            cores.append(v); exts.add("")
    # Keep a trailing extension (e.g. ".html") only when the id is the final segment.
    ext = exts.pop() if (len(exts) == 1 and id_col == n - 1) else ""

    all_digits = bool(cores) and all(c.replace(".", "").isdigit() for c in cores)
    if all_digits:
        minlen = min(len(c) for c in cores)
        idcap = r"(\d{%d,})" % max(5, minlen - 1)
    else:
        idcap = r"([A-Za-z0-9_.-]{5,})"

    parts = []
    for i in range(n):
        if i == id_col:
            parts.append(idcap)
        elif len(colvals[i]) <= 1:
            parts.append(re.escape(next(iter(colvals[i]))))
        else:
            parts.append(r"[^/]+")
    regex = "/" + "/".join(parts) + (re.escape(ext) if ext else "")

    # Prefix: a meaningful static path word if present, else the registrable site label.
    static_words = [next(iter(colvals[i])) for i in range(n)
                    if len(colvals[i]) == 1 and colvals[i] and next(iter(colvals[i])).isalpha()]
    prefix = (static_words[0] if static_words else (raw_for_prefix.split(".")[-2]
              if raw_for_prefix.count(".") >= 1 else raw_for_prefix) or "listing")
    sample_path = "/" + "/".join(
        (sorted(colvals[i])[0] if len(colvals[i]) >= 1 else "") for i in range(n))

    return {
        "listing_url_regex": regex,
        "key_prefix":        prefix,
        "id_kind":           "numeric" if all_digits else "alnum",
        "support":           len(group),
        "sample_path":       sample_path,
    }


def _listing_key(url: str, profiles: list[dict] | None = None) -> str | None:
    """
    Return a stable dedup key for a listing URL, or None if it is not a listing.

    Host-scoped: on a known site we accept ONLY that site's listing pattern, so e.g.
    Facebook's footer nav links (Messenger/Meta Pay/… — which carry long numeric ids)
    are never mistaken for marketplace listings. LEARNED site profiles extend this to any
    site you've pointed Web Watcher at. The broad numeric fallback applies only to hosts
    we have neither a built-in nor a learned pattern for.
    """
    try:
        parts = urlparse(url)
        host  = (parts.netloc or "").lower()
        path  = parts.path or ""
    except Exception:
        return None

    if "ebay." in host:
        m = _PAT_EBAY.search(path);  return f"ebay:{m.group(1)}" if m else None
    if "facebook." in host:
        m = _PAT_FB.search(path);    return f"fb:{m.group(1)}" if m else None
    if "craigslist." in host:
        m = _PAT_CL_NEW.search(path) or _PAT_CL.search(path)
        return f"craigslist:{m.group(1)}" if m else None

    # Learned profiles: a regex captured when this site was explored. Matched by
    # registrable site key so subdomains (e.g. www.) all share one profile.
    if profiles:
        from web_watcher.storage import site_key
        sk = site_key(host)
        for p in profiles:
            rgx = p.get("listing_url_regex")
            if not rgx or p.get("domain") != sk:
                continue
            try:
                m = re.search(rgx, path)
            except re.error:
                continue
            if m:
                gid = m.group(1) if m.groups() else m.group(0)
                return f"{(p.get('key_prefix') or sk.split('.')[0])}:{gid}"
        # A profile WITH A REGEX exists for this site but the URL didn't match its listing
        # shape — it's site chrome (nav/footer), not a listing. Don't fall through to
        # guessing. (An empty-regex profile is not authoritative and is ignored here.)
        if any(p.get("domain") == sk and p.get("listing_url_regex") for p in profiles):
            return None

    # Unknown host: keyword pattern first, then a conservative long-numeric fallback.
    m = _PAT_GENERIC.search(path)
    if m:
        return f"listing:{m.group(1)}"
    m = _PAT_NUMERIC.search(path)
    if m:
        return f"listing:{m.group(1)}"
    return None


def extract_listings(page: Page, max_items: int = 200,
                     profiles: list[dict] | None = None) -> list[Listing]:
    """
    Scrape listing cards from the current page. Returns de-duplicated Listings
    (by stable key) in DOM order. Site-agnostic: matches eBay, Facebook
    Marketplace, Craigslist, generic /item|/listing|/product URLs, and any site you've
    taught Web Watcher via a learned profile (passed in `profiles`).
    """
    try:
        raw = page.evaluate(_EXTRACT_JS) or []
    except Exception as exc:
        log.warning("Listing extraction failed: %s", exc)
        return []

    # Group anchors by stable key, keeping the richest text/alt per listing. The
    # same listing often appears as multiple anchors (image + title) sharing a href;
    # the longest card text is the most useful for a title/price.
    best: dict[str, dict] = {}
    order: list[str] = []
    for entry in raw:
        href = (entry.get("href") or "").strip()
        key = _listing_key(href, profiles)
        if not key:
            continue
        text = (entry.get("text") or "").strip()
        alt  = (entry.get("alt") or "").strip()
        image = (entry.get("image") or "").strip()
        cur = best.get(key)
        if cur is None:
            best[key] = {"href": href, "text": text, "alt": alt, "image": image}
            order.append(key)
        else:
            if len(text) > len(cur["text"]):
                cur["text"] = text
            if alt and not cur["alt"]:
                cur["alt"] = alt
            if image and not cur["image"]:
                cur["image"] = image

    listings: list[Listing] = []
    for key in order:
        info = best[key]
        text = _LEADING_JUNK_RE.sub("", _BADGE_RE.sub("", info["text"])).strip()
        price_m = _PRICE_RE.search(text)
        price = price_m.group(0).replace(" ", "") if price_m else ""
        # Title: card text with a leading price token stripped; fall back to img alt.
        title = text
        if price and title.startswith(price):
            title = title[len(price):].strip(" -–·•|")
        title = _LEADING_JUNK_RE.sub("", title)   # strip junk again if price exposed more
        title = _META_RE.sub("", title).strip()   # drop trailing "Xhr ago …" metadata
        if not title:
            title = _LEADING_JUNK_RE.sub("", _BADGE_RE.sub("", info["alt"])).strip()
        listings.append(Listing(key=key, url=info["href"], title=title[:160],
                                price=price, image=info.get("image", "")))
        if len(listings) >= max_items:
            break

    log.info("Extracted %d unique listing(s) from %s", len(listings), urlparse(page.url).netloc)
    return listings


# JS that pulls the meaningful text off a single listing-DETAIL page: the structured
# attributes (year/make/transmission/odometer/…) and the seller's description. Site
# selectors are tried first (Craigslist's #postingbody + .attrgroup, eBay's item
# specifics, generic article/description containers), then a trimmed body fallback.
# This is what lets the judge match on things that live in the AD, not the card title
# — e.g. "manual transmission", "4x4", mileage, condition.
_BODY_JS = r"""() => {
    const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const parts = [];
    const sel = [
        '.attrgroup',                 // craigslist structured attributes
        '#postingbody',               // craigslist description
        '[data-testid="x-item-description"], .x-item-description',  // ebay
        '#viewad-description, .ad-description',                     // generic classifieds
        'article', '[itemprop="description"]', '.description',
    ];
    const seen = new Set();
    for (const s of sel) {
        document.querySelectorAll(s).forEach(el => {
            const t = clean(el.innerText);
            if (t && t.length > 15 && !seen.has(t)) { seen.add(t); parts.push(t); }
        });
    }
    let text = parts.join(' — ');
    if (text.length < 40) {            // nothing matched → trimmed page body fallback
        text = clean(document.body ? document.body.innerText : '');
    }
    // 8000, not 2000: sellers put phone numbers and "call me at…" at the END of long ads, so a
    // tight cap deleted exactly the contact info the user saves the ad for. Still bounded so the
    // whole-page fallback above can't store megabytes.
    return text.slice(0, 8000);
}"""


def extract_listing_body(page: Page) -> str:
    """
    Return the meaningful text of a single listing-detail page (attributes +
    description), so the judge can match on what's IN the ad rather than just the card
    title. Best-effort and bounded; returns '' on any failure.
    """
    try:
        return (page.evaluate(_BODY_JS) or "").strip()
    except Exception as exc:
        log.debug("Listing body extraction failed: %s", exc)
        return ""


# When the listing was POSTED. The card only carries a relative "2h ago" that changes
# between sightings; the ad page carries an absolute timestamp. Most classifieds use a
# <time datetime="…"> (Craigslist, many others); fall back to common "posted" meta tags.
_POSTED_JS = r"""() => {
    const t = document.querySelector('time[datetime]');
    if (t && t.getAttribute('datetime')) return t.getAttribute('datetime');
    const m = document.querySelector(
        'meta[property="og:updated_time"], meta[itemprop="datePosted"], meta[name="date"]');
    if (m && m.getAttribute('content')) return m.getAttribute('content');
    return '';
}"""


def extract_listing_posted_at(page: Page) -> str:
    """Best-effort absolute posted-date for a listing-detail page (ISO-ish string), or ''."""
    try:
        return (page.evaluate(_POSTED_JS) or "").strip()[:40]
    except Exception as exc:
        log.debug("Posted-date extraction failed: %s", exc)
        return ""


# Patterns for pulling a few high-value attributes out of a listing's title + ad body,
# so they can be stored as queryable columns (filter "manual under 150k", sort by price).
_RE_PRICE     = re.compile(r"[$£€]?\s*([\d][\d,]{1,})")
_RE_YEAR      = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")
_RE_ODOMETER  = re.compile(r"odometer:\s*([\d,]{3,})", re.I)
_RE_MILES     = re.compile(r"\b([\d,]{4,})\s*(?:miles|mi)\b", re.I)
_RE_TRANS     = re.compile(r"transmission:\s*(manual|automatic|other)", re.I)
_RE_DRIVE     = re.compile(r"drive:\s*(4wd|awd|fwd|rwd)", re.I)


def _to_int(s: str) -> int | None:
    try:
        return int(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_listing_attributes(title: str = "", price_text: str = "", details: str = "") -> dict:
    """
    Pull a few structured attributes out of a listing's title/price/ad-body so they can
    be stored as filterable columns: price_value, year, mileage, transmission, drivetrain.
    Tuned for vehicle classifieds (Craigslist's explicit "transmission: …"/"drive: …"/
    "odometer: …" lines) with looser keyword fallbacks. Missing fields are simply omitted.
    """
    title = title or ""
    body  = f"{title}\n{details or ''}"
    low   = body.lower()
    attrs: dict = {}

    pm = _RE_PRICE.search(price_text or "")
    if pm:
        v = _to_int(pm.group(1))
        if v:
            attrs["price_value"] = v

    ym = _RE_YEAR.search(title) or _RE_YEAR.search(body)
    if ym:
        attrs["year"] = int(ym.group(1))

    mm = _RE_ODOMETER.search(body) or _RE_MILES.search(body)
    if mm:
        miles = _to_int(mm.group(1))
        if miles:
            attrs["mileage"] = miles

    tm = _RE_TRANS.search(body)
    if tm:
        attrs["transmission"] = tm.group(1).lower()
    elif re.search(r"\bmanual\b|\bstick shift\b|\b5-?speed\b|\b6-?speed\b", low):
        attrs["transmission"] = "manual"

    dm = _RE_DRIVE.search(body)
    if dm:
        attrs["drivetrain"] = dm.group(1).lower()
    elif re.search(r"\b4x4\b|\b4wd\b|four[- ]wheel", low):
        attrs["drivetrain"] = "4wd"
    elif re.search(r"\bawd\b|all[- ]wheel", low):
        attrs["drivetrain"] = "awd"

    return attrs


# Volatile bits that change between sightings of the SAME post (relative timestamps,
# the mileage badge CL appends to cards) and so must be stripped before fingerprinting.
_FP_NOISE_RE = re.compile(_REL_TIME + r"|\bjust now\b|\b\d+k?\s*(?:mi|miles)\b", re.I)


def listing_fingerprint(title: str, price_value=None, year=None) -> str:
    """
    A content fingerprint used to catch RE-POSTS: the same item relisted under a new
    listing id (or cross-posted) looks "new" by id but shares this signature. Built from
    year + price + a normalized title (lowercased, volatile timestamp/mileage badges
    removed, punctuation dropped, first ~10 meaningful tokens). Conservative on purpose
    — includes price so a genuine price-drop repost is still treated as news. Empty when
    there's nothing stable to hash.

    Weak-signal guard: a fingerprint is only emitted when the title is DISTINCTIVE enough
    to identify a specific item — at least 4 title tokens, OR a year plus ≥2 tokens. A
    terse generic title with no year (e.g. "chevy silverado" at $5,600) would otherwise
    merge DIFFERENT trucks into one; per this module's philosophy ("better to show a
    possible dup than to hide a real listing") we emit no fingerprint and treat each as
    unique instead of over-merging distinct listings.
    """
    t = _FP_NOISE_RE.sub(" ", (title or "").lower())
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    toks = [w for w in t.split() if len(w) > 1]
    if len(toks) < 4 and not (year and len(toks) >= 2):
        return ""
    sig = " ".join(toks[:10])
    return f"{year or ''}|{price_value or ''}|{sig}"


# ---------------------------------------------------------------------------
# Search variation — fight non-deterministic feed ranking
# ---------------------------------------------------------------------------

# Per-domain sort-order rotations. Each sweep picks the next variant so repeated
# passes surface different slices of inventory the feed algorithm would otherwise
# keep hidden. Values are (param, value) pairs merged into the query string.
_EBAY_SORTS    = ["10", "12", "15", "1"]    # newest, best match, price+ship low, ending soonest
_FB_SORTS      = ["creation_time_descend", "best_match", "price_ascend", "distance_ascend"]
_CL_SORTS      = ["date", "rel", "priceasc", "pricedsc"]   # craigslist: newest, relevant, price↑, price↓


def vary_search(base_url: str, sweep_index: int, enabled: bool = True) -> str:
    """
    Return a variant of base_url for this sweep. When enabled, rotates the sort
    order (and, for eBay, occasionally nudges a price band) so the feed shows
    different inventory each pass. When disabled, returns base_url unchanged (a
    plain refresh). sweep_index increments every sweep.
    """
    # Self-heal stored marketplace URLs every sweep (idempotent): junk query text →
    # real params, wrong/invented locations corrected (craigslist region-from-zip,
    # OfferUp's fabricated city paths → the real /search, eBay _stpos). Watches created
    # before the refiners existed get fixed without the user touching anything.
    try:
        from web_watcher.cl_geo import refine_search_url
        base_url = refine_search_url(base_url)
    except Exception:
        pass
    if not enabled:
        return base_url
    try:
        parts = urlparse(base_url)
        host = (parts.netloc or "").lower()
        query = dict(parse_qsl(parts.query, keep_blank_values=True))

        if "ebay." in host:
            query["_sop"] = _EBAY_SORTS[sweep_index % len(_EBAY_SORTS)]
        elif "facebook." in host:
            query["sortBy"] = _FB_SORTS[sweep_index % len(_FB_SORTS)]
            # Every other sweep, restrict to recently-listed to catch fresh posts.
            if sweep_index % 2 == 1:
                query["daysSinceListed"] = "1"
            else:
                query.pop("daysSinceListed", None)
        elif "craigslist." in host:
            # Rotate the sort so each term surfaces a different slice (newest, relevant,
            # cheapest, priciest) instead of always the default order; every other sweep
            # also bundle duplicate posts so we cover more distinct listings per pass.
            query["sort"] = _CL_SORTS[sweep_index % len(_CL_SORTS)]
            if sweep_index % 2 == 1:
                query["bundleDuplicates"] = "1"
            else:
                query.pop("bundleDuplicates", None)
        else:
            # Unknown site: append a cache-busting no-op so the page genuinely reloads.
            query["_ww"] = str(sweep_index)

        new_query = urlencode(query)
        return urlunparse(parts._replace(query=new_query))
    except Exception as exc:
        log.debug("vary_search failed for %s: %s", base_url, exc)
        return base_url


# ---------------------------------------------------------------------------
# Popup / modal dismissal
# ---------------------------------------------------------------------------

# Close/decline controls for the overlays that block logged-out marketplace browsing:
# Facebook's "Log in or sign up" modal, cookie-consent banners, generic dialogs.
# Cookie choices prefer the privacy-preserving option (decline/essential-only).
_DISMISS_SELECTORS = [
    '[aria-label="Decline optional cookies"]',
    '[aria-label="Only allow essential cookies"]',
    'div[role="dialog"] [aria-label="Close"]',
    '[aria-label="Close"]',
    '[aria-label="close"]',
    'div[role="button"][aria-label="Close"]',
]
# Text-based fallbacks for buttons that lack a stable aria-label.
_DISMISS_TEXTS = [
    "Decline optional cookies",
    "Only allow essential cookies",
    "Not now",
    "Close",
]


def dismiss_popups(page: Page, settle_ms: int = 800) -> int:
    """
    Best-effort close of login/cookie/consent overlays that block scraping (most
    importantly Facebook Marketplace's logged-out "Log in or sign up" modal, which
    otherwise intercepts scrolling and hides the listings). Returns how many controls
    were clicked. Safe to call repeatedly — absent overlays are no-ops.

    settle_ms: how long to wait for late-rendering modals to appear before trying to
    close them. The continuous sweep just navigated, so it uses the default 800ms to
    let FB inject its modal; the autonomous agent already settled the page between
    steps and passes 0 to skip the wait so the per-step blocker gate stays cheap.
    """
    clicked = 0
    # Give late-rendering modals (FB injects them ~1s after load) a moment to appear.
    if settle_ms > 0:
        try:
            page.wait_for_timeout(settle_ms)
        except Exception:
            pass

    for sel in _DISMISS_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            if not loc.is_visible():
                continue
            loc.click(timeout=1500)
            clicked += 1
            log.info("Dismissed overlay via %s", sel)
            page.wait_for_timeout(400)
        except Exception as exc:
            log.debug("Dismiss selector %s failed: %s", sel, exc)

    for txt in _DISMISS_TEXTS:
        try:
            loc = page.get_by_text(txt, exact=True).first
            if loc.count() == 0 or not loc.is_visible():
                continue
            loc.click(timeout=1500)
            clicked += 1
            log.info("Dismissed overlay via text %r", txt)
            page.wait_for_timeout(400)
        except Exception as exc:
            log.debug("Dismiss text %r failed: %s", txt, exc)

    # Last resort: if a modal dialog is still up, press Escape (closes many of them).
    try:
        if page.locator('div[role="dialog"]').first.count() > 0:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
    except Exception:
        pass

    return clicked


# A visible modal dialog or one of the known dismiss controls = a blocking overlay.
# Kept fast (no waits) so the agent can poll it every step before spending the cost
# of dismiss_popups(). Mirrors the controls dismiss_popups() knows how to close.
_OVERLAY_PROBE = 'div[role="dialog"], ' + ', '.join(_DISMISS_SELECTORS)


def has_blocking_overlay(page: Page) -> bool:
    """
    Fast, no-wait check for a login/cookie/consent overlay sitting on top of the
    page. Used by the autonomous agent's blocker gate so it only calls dismiss_popups
    when something is actually in the way (avoids taxing every step). Conservative —
    returns False on any error rather than risk a false block.
    """
    try:
        loc = page.locator(_OVERLAY_PROBE).first
        return loc.count() > 0 and loc.is_visible()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Humanized search — type the query like a person instead of URL-jumping
# ---------------------------------------------------------------------------

# Query-string params that hold the user's free-text search term across common sites.
_SEARCH_TERM_PARAMS = ("query", "_nkw", "q", "keywords", "search_query", "st")
# Where the search box tends to live. Tried in order; first visible one wins.
_SEARCH_BOX_SELECTORS = (
    'input[type="search"]',
    'input[name="query"]', 'input[name="_nkw"]', 'input[name="q"]',
    'input[aria-label*="search" i]',
    'input[placeholder*="search" i]',
    'input[id*="search" i]',
)
_HUMAN_NAV_TIMEOUT = 30_000


# ---------------------------------------------------------------------------
# Closing the loop: read what the page says BACK after we type/search, so the
# agent (and the scraper) UNDERSTAND the effect of an action instead of acting
# blind. The weather-site failure — typing product terms into a box whose
# autocomplete only offers cities — is exactly what this catches.
# ---------------------------------------------------------------------------

_SUGGESTION_SELECTORS = (
    "[role='listbox'] [role='option']",
    "[role='option']",
    "ul[role='listbox'] li",
    ".autocomplete-item, .ui-autocomplete li, .tt-suggestion, .typeahead li",
    ".search-suggestions li, .suggestions li, .suggestion, .awesomplete li, .pac-item",
    "datalist option",
)

_US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS","KY","LA",
    "ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK",
    "OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","DC",
}

_NO_RESULTS_RE = re.compile(
    r"\bno results\b|\b0 results\b|\bno (?:listings|items|matches|matching|ads|products)\b|"
    r"nothing (?:matched|found)|we (?:couldn't|could not) find|did not match any|found 0\b|"
    r"your search .{0,30}\b(?:did not|didn't) match", re.I,
)


def looks_like_location(text: str) -> bool:
    """Heuristic: does this autocomplete suggestion look like a PLACE (city/town/region)?
    'City, ST', a bare nationally-known town (via the gazetteer), or a name + state."""
    t = (text or "").strip()
    if not t or len(t) > 60:
        return False
    m = re.match(r"^([A-Za-z .'\-]{2,}),\s*([A-Za-z]{2,})", t)
    if m and (m.group(2).upper() in _US_STATES or len(m.group(2)) >= 4):
        return True
    try:
        from web_watcher.cl_geo import place_latlon
        head = re.split(r"[,(]", t)[0].strip().lower()
        if 3 <= len(head) <= 30 and place_latlon(head):
            return True
    except Exception:
        pass
    return False


def suggestions_are_locations(suggestions: list) -> bool:
    """True when most non-empty suggestions look like places (>=60%, min 2) — the tell that
    a 'search' box is really a location/geo picker, not a keyword search."""
    vals = [s for s in (suggestions or []) if (s or "").strip()]
    if len(vals) < 2:
        return False
    hits = sum(1 for s in vals if looks_like_location(s))
    return hits / len(vals) >= 0.6


def text_says_no_results(text: str) -> bool:
    """Does this page text explicitly say the search returned nothing?"""
    return bool(_NO_RESULTS_RE.search(text or ""))


def read_suggestions(page, limit: int = 8) -> list:
    """The visible autocomplete/typeahead option texts currently on screen, bounded + deduped."""
    out: list[str] = []
    seen: set[str] = set()
    for sel in _SUGGESTION_SELECTORS:
        try:
            texts = page.eval_on_selector_all(
                sel,
                "els => els.filter(e => e.offsetParent !== null)"
                ".map(e => (e.innerText || e.value || '').trim())",
            ) or []
        except Exception:
            continue
        for t in texts:
            t = (t or "").strip()
            if t and t not in seen and len(t) < 80:
                seen.add(t)
                out.append(t)
                if len(out) >= limit:
                    return out
    return out


def read_search_feedback(page, typed_term: str = "") -> dict:
    """After typing into a search box, what is the page telling us? Returns
    {suggestions, are_locations}. are_locations is True when the box's suggestions are
    places AND the typed term isn't itself a place — i.e. this is a geo picker, not a
    keyword search (the weather-site case)."""
    sugg = read_suggestions(page)
    are_loc = bool(sugg) and suggestions_are_locations(sugg) and not looks_like_location(typed_term)
    return {"suggestions": sugg, "are_locations": are_loc}


def detect_no_results(page) -> bool:
    """True when the page shows an explicit 'no results' state, so a caller reports that
    honestly instead of treating an empty page as 'found nothing' legitimately."""
    try:
        body = page.evaluate("() => document.body ? document.body.innerText.slice(0, 4000) : ''") or ""
    except Exception:
        return False
    return text_says_no_results(body)


def humanized_search(page: Page, url: str) -> bool:
    """
    Navigate like a person instead of jumping straight to a query URL: land on the
    search page WITHOUT the search term, then TYPE the term into the search box (real
    key events, human pacing) and press Enter. Playwright's click moves the mouse to
    the box, so this exercises both keyboard and mouse — useful on sites that watch for
    bot-like direct-URL access.

    Returns True if it typed a search and submitted; False if it couldn't (no term in
    the URL, or no search box found) so the caller can fall back to a direct goto.
    Best-effort: any failure returns False rather than raising.
    """
    try:
        parts = urlparse(url)
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
    except Exception:
        return False

    term = None
    for key in _SEARCH_TERM_PARAMS:
        if params.get(key):
            term = params.pop(key)
            break
    if not term:
        return False  # no free-text term to type — let the caller goto directly

    landing = urlunparse(parts._replace(query=urlencode(params)))
    try:
        page.goto(landing, timeout=_HUMAN_NAV_TIMEOUT, wait_until="domcontentloaded")
    except Exception as exc:
        log.debug("humanized_search landing nav failed: %s", exc)
        return False
    dismiss_popups(page, settle_ms=0)

    # The search form often renders just after domcontentloaded — wait briefly for any
    # search box to appear rather than checking too early and giving up.
    try:
        page.wait_for_selector(", ".join(_SEARCH_BOX_SELECTORS), timeout=4_000, state="visible")
    except Exception:
        pass

    box = None
    for sel in _SEARCH_BOX_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                box = loc
                break
        except Exception:
            continue
    if box is None:
        return False

    try:
        box.click(timeout=3_000)          # moves the mouse to the box and focuses it
        try:
            box.fill("")                   # clear any prefilled text
        except Exception:
            pass
        # Type at a human, slightly-random pace so the site sees real key events.
        box.type(term, delay=random.randint(70, 130))
        # Some sites (Craigslist) swallow the first keystroke — verify the box value
        # and correct it before submitting so the query is always exactly right.
        try:
            if (box.input_value() or "").strip().lower() != term.strip().lower():
                box.fill(term)
        except Exception:
            pass
        # Close the loop: is this a keyword search box or a location picker? Log the tell so
        # the Live feed explains WHY a sweep on a mis-typed site finds nothing.
        try:
            if read_search_feedback(page, term).get("are_locations"):
                log.warning("Search box on %s autocompletes LOCATIONS, not keywords — typing %r "
                            "here won't search for items; this may not be a keyword-searchable "
                            "marketplace.", urlparse(url).netloc, term)
        except Exception:
            pass
        page.keyboard.press("Enter")
        page.wait_for_timeout(1_500)
        log.info("Humanized search: typed %r into the search box", term)
        return True
    except Exception as exc:
        log.debug("humanized_search typing failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Scrolling
# ---------------------------------------------------------------------------

def human_scroll(page: Page, passes: int = 4) -> None:
    """
    Scroll down in human-paced bursts to lazy-load more listings, ADAPTIVELY: keep going
    until the page stops growing (we've reached the bottom / no more cards are appended),
    instead of a fixed number of passes that leaves long results pages half-read.

    `passes` is the MINIMUM effort (always scroll at least this many bursts); a safety cap
    (max(passes, 14)) bounds infinite feeds so we never scroll forever. We stop early once
    the scroll height has been stable for two consecutive checks AND the minimum is met.
    """
    cap = max(int(passes), 14)
    height_js = ("() => Math.max("
                 "document.body ? document.body.scrollHeight : 0, "
                 "document.documentElement ? document.documentElement.scrollHeight : 0)")
    last_h, stable = 0, 0
    for i in range(cap):
        try:
            # Burst of small wheel deltas rather than one big jump (more human,
            # and lets lazy-loaders fire between deltas).
            for _ in range(random.randint(3, 6)):
                page.mouse.wheel(0, random.randint(280, 520))
                time.sleep(max(0.02, random.gauss(0.12, 0.04)))
            # Occasionally scroll BACK UP a little, like a person re-reading a card they
            # scrolled past, then continue down. A feed that only ever scrolls one
            # direction at constant speed is a bot tell; this breaks that pattern (and it's
            # what the user sees on-screen — the agent no longer only ever goes down).
            if i > 0 and random.random() < 0.25:
                for _ in range(random.randint(1, 3)):
                    page.mouse.wheel(0, -random.randint(120, 300))
                    time.sleep(max(0.02, random.gauss(0.14, 0.05)))
                time.sleep(max(0.15, random.gauss(0.5, 0.2)))   # dwell, as if reading
            # Settle so new cards load before we measure / the next pass.
            time.sleep(max(0.3, random.gauss(0.9, 0.25)))
            h = page.evaluate(height_js) or 0
            if h <= last_h:                       # page didn't grow → likely at the bottom
                stable += 1
                if stable >= 2 and (i + 1) >= passes:
                    break
            else:
                stable, last_h = 0, h
        except Exception as exc:
            log.debug("Scroll pass %d failed: %s", i, exc)
            break


# ---------------------------------------------------------------------------
# Login-wall detection (for use_login_profile sites)
# ---------------------------------------------------------------------------

_LOGIN_URL_RE  = re.compile(r"/login|/checkpoint|/authentication|signin|/recover", re.I)
_LOGIN_TEXT_RE = re.compile(
    r"log in to continue|log into facebook|you must log in|please log in|"
    r"create new account|enter your (?:email|password)|sign in to continue",
    re.I,
)


def is_login_wall(page: Page) -> bool:
    """
    Heuristic: True if the page looks like a login / logged-out wall. Used so a
    use_login_profile watch can notify the user to reconnect instead of scraping
    a login page. Conservative — only trips on clear login indicators.
    """
    try:
        url = page.url or ""
        if _LOGIN_URL_RE.search(urlparse(url).path or ""):
            return True
        body = page.inner_text("body", timeout=2_000)[:3000].lower()
        if _LOGIN_TEXT_RE.search(body):
            # Guard against false positives on pages that merely have a "Log in"
            # link in a header: require the login cue AND little useful content.
            has_password_field = False
            try:
                has_password_field = page.locator('input[type="password"]').count() > 0
            except Exception:
                pass
            return has_password_field or len(body) < 800
        return False
    except Exception:
        return False
