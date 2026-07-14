"""
Marketplace search-URL refinement — deterministic query hygiene + location correction.

The chat model builds search URLs from what the user SAID, and small local models
reliably make the same mistakes on EVERY site:

  1. They stuff everything into the query text: "vehicles owner 98221 under 10k" —
     a literal search that matches nothing. Each site wants those as its real PARAMS.
  2. They invent locations: "anacortes" became seattle.craigslist.org (wrong region —
     skagit serves Anacortes), and OfferUp got a fabricated "/WA-Anacortes/search"
     path that 403s (VERIFIED live — that's the "all over the place on OfferUp" bug).

All fixable without any model: a bundled US zip→lat/lon table + places gazetteer
(Census, public domain) plus craigslist's own area list resolve any location, and the
query text is parsed for prices/zips/"in <town>" phrases deterministically.

Per-site facts (established by live probing — do not guess new params):
  craigslist  postal= & search_distance= & max_price/min_price & purveyor; region is
              the SUBDOMAIN (bare craigslist.org 404s search paths).
  offerup     q, radius, price_min, price_max ONLY. Location CANNOT be set by URL —
              OfferUp geolocates the requester's IP (which is the user's real area, so
              that's exactly right). Any other path/param (e.g. /WA-Anacortes/search,
              priceMax=) is fabricated and 403s.
  ebay        _nkw (query), _stpos (zip) + _sadis (radius mi), _udlo/_udhi (price).
  facebook    /marketplace/<city>/search with query, minPrice/maxPrice.

`refine_search_url` dispatches by site; every refiner is idempotent and failure-
tolerant (any error returns the URL unchanged), so they run on every create/update AND
every sweep — stored bad URLs self-heal the next time they're used. Watch-level zip
propagation (a craigslist postal feeding eBay's _stpos) happens in the server's
_normalize_marketplace_urls, which sees the whole URL list.

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  zip_latlon / place_latlon / nearest_zip / nearest_region_host   geo lookups
  refine_search_url        dispatcher (craigslist/offerup/ebay/facebook)
  refine_craigslist_url    query → params + region-from-zip rewrite
  refine_offerup_url       canonicalize to /search, alias fake params, drop location
  refine_ebay_url          _stpos/_sadis/_udlo/_udhi from query text (+fallback zip)
  refine_facebook_url      minPrice/maxPrice from query text
  _extract_price / _extract_zip / _extract_purveyor / _extract_in_place   parsers
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import csv
import gzip
import json
import logging
import math
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

log = logging.getLogger(__name__)

_ASSETS = Path(__file__).parent / "assets"

# Only ever rewrite hosts under this suffix; never invent hosts for other sites.
_CL_SUFFIX = ".craigslist.org"


# ---------------------------------------------------------------------------
# Geo data (lazy-loaded once per process)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _zip_table() -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    try:
        with gzip.open(_ASSETS / "us_zips.csv.gz", "rt", newline="") as f:
            for row in csv.reader(f):
                out[row[0]] = (float(row[1]), float(row[2]))
    except Exception as exc:
        log.warning("could not load zip table: %s", exc)
    return out


@lru_cache(maxsize=1)
def _areas() -> list[dict]:
    try:
        return json.loads((_ASSETS / "cl_areas.json").read_text())
    except Exception as exc:
        log.warning("could not load craigslist areas: %s", exc)
        return []


@lru_cache(maxsize=1)
def _known_hosts() -> frozenset[str]:
    """Every real craigslist region hostname — anything else in the subdomain slot is a
    model hallucination (usually the town the user actually named)."""
    return frozenset(a["host"] for a in _areas())


@lru_cache(maxsize=1)
def _place_table() -> dict[str, list[tuple[str, float, float]]]:
    """City/town name → [(state, lat, lon), …] from the Census places gazetteer."""
    out: dict[str, list[tuple[str, float, float]]] = {}
    try:
        with gzip.open(_ASSETS / "us_places.csv.gz", "rt", newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                out.setdefault(row[0], []).append((row[1], float(row[2]), float(row[3])))
    except Exception as exc:
        log.warning("could not load places table: %s", exc)
    return out


def zip_latlon(zip5: str) -> tuple[float, float] | None:
    return _zip_table().get(zip5)


def place_latlon(name: str, anchor: tuple[float, float] | None = None) -> tuple[float, float] | None:
    """Coordinates for a US city/town name. Ambiguous names (many states share them)
    resolve to the candidate nearest `anchor` when one is close enough; with no anchor,
    only a nationally-unique name resolves — guessing a state would silently watch the
    wrong coast."""
    cands = _place_table().get((name or "").strip().lower())
    if not cands:
        return None
    if len(cands) == 1:
        return (cands[0][1], cands[0][2])
    if anchor:
        coslat = math.cos(math.radians(anchor[0]))
        best = min(cands, key=lambda c: math.hypot(c[1] - anchor[0], (c[2] - anchor[1]) * coslat))
        d = math.hypot(best[1] - anchor[0], (best[2] - anchor[1]) * coslat)
        if d < 2.5:   # ~170 miles — same metro/region, safe to assume
            return (best[1], best[2])
    return None


def nearest_zip(lat: float, lon: float) -> str | None:
    """The zip whose centroid is closest to (lat, lon) — turns a resolved place into a
    postal= filter craigslist understands."""
    best, best_d = None, float("inf")
    coslat = math.cos(math.radians(lat))
    for z, (la, lo) in _zip_table().items():
        d = math.hypot(la - lat, (lo - lon) * coslat)
        if d < best_d:
            best, best_d = z, d
    return best


def nearest_region_host(lat: float, lon: float) -> str | None:
    """The craigslist region hostname geographically closest to (lat, lon)."""
    best, best_d = None, float("inf")
    coslat = math.cos(math.radians(lat))
    for a in _areas():
        d = math.hypot(a["lat"] - lat, (a["lon"] - lon) * coslat)
        if d < best_d:
            best, best_d = a["host"], d
    return best


# ---------------------------------------------------------------------------
# Query-text parsers
# ---------------------------------------------------------------------------

# "under 10k", "below $8,500", "less than 12000", "max 5k", "$3000 max"
_MAX_RE = re.compile(
    r"\b(?:under|below|less\s+than|max(?:imum)?|up\s+to)\s*\$?\s*([\d,]+)\s*(k)?\b"
    r"|\$?\s*([\d,]+)\s*(k)?\s+max\b", re.I)
_MIN_RE = re.compile(
    r"\b(?:over|above|more\s+than|min(?:imum)?|at\s+least)\s*\$?\s*([\d,]+)\s*(k)?\b", re.I)
_ZIP_RE = re.compile(r"\b(\d{5})\b")
_OWNER_RE = re.compile(r"\b(?:by[-\s])?owners?\b|\bfsbo\b", re.I)
_DEALER_RE = re.compile(r"\bdealers?(?:ships?)?\b", re.I)

# FULLY generic vehicle words — always mean "the cars+trucks CATEGORY", never a term.
_GENERIC_VEHICLE = re.compile(r"\b(?:vehicles?|autos?|automobiles?|cars?\s+(?:and|&)\s+trucks?)\b", re.I)
# Narrowing vehicle words — generic on the everything category, deliberate on cta
# ("truck" on /search/cta filters out sedans; leave it there).
_NARROW_VEHICLE = re.compile(r"\b(?:cars?|trucks?)\b", re.I)

# "in anacortes" / "near mount vernon" — a location the model left in the query text.
# PREPOSITION-GATED on purpose: bare place-word matching would eat car models named
# after places (toyota TACOMA, chevy COLORADO, dodge DAKOTA, pontiac, sedona…).
_IN_PLACE_RE = re.compile(r"\b(?:in|near|around)\s+([a-z][a-z .'\-]{2,40})", re.I)


def _num(m_digits: str, m_k: str | None) -> int:
    n = int(m_digits.replace(",", ""))
    return n * 1000 if m_k else n


def _extract_price(text: str) -> tuple[str, int | None, int | None]:
    """Pull 'under 10k' / 'over $2000' style limits out of the text."""
    max_p = min_p = None
    m = _MAX_RE.search(text)
    if m:
        max_p = _num(m.group(1) or m.group(3), m.group(2) or m.group(4))
        text = _MAX_RE.sub(" ", text, count=1)
    m = _MIN_RE.search(text)
    if m:
        min_p = _num(m.group(1), m.group(2))
        text = _MIN_RE.sub(" ", text, count=1)
    return text, min_p, max_p


def _extract_zip(text: str) -> tuple[str, str | None]:
    """Pull the first REAL zip (must exist in the gazetteer) out of the text."""
    for m in _ZIP_RE.finditer(text):
        if zip_latlon(m.group(1)):
            return text[:m.start()] + " " + text[m.end():], m.group(1)
    return text, None


def _extract_purveyor(text: str) -> tuple[str, str | None]:
    if _OWNER_RE.search(text):
        return _OWNER_RE.sub(" ", text), "owner"
    if _DEALER_RE.search(text):
        return _DEALER_RE.sub(" ", text), "dealer"
    return text, None


def _extract_in_place(text: str, anchor: tuple[float, float] | None = None,
                      ) -> tuple[str, tuple[float, float] | None]:
    """Pull an 'in/near/around <town>' phrase out of the text and resolve it. Tries the
    full captured phrase then trims words off the end ('in anacortes for cheap' →
    'anacortes'). Preposition-gated — see _IN_PLACE_RE."""
    m = _IN_PLACE_RE.search(text)
    if not m:
        return text, None
    words = re.findall(r"[a-z.'\-]+", m.group(1).lower())
    for n in range(min(3, len(words)), 0, -1):
        # Strip sentence punctuation off the ends — an instruction almost always ends in a
        # period ("...in Anacortes.") and a trapped trailing '.' fails the gazetteer lookup.
        cand = " ".join(words[:n]).strip(" .'-")
        if not cand:
            continue
        ll = place_latlon(cand, anchor)
        if ll:
            text = re.sub(r"\b(?:in|near|around)\s+" + re.escape(cand),
                          " ", text, count=1, flags=re.I)
            return text, ll
    return text, None


# ---------------------------------------------------------------------------
# The refinement
# ---------------------------------------------------------------------------

def refine_craigslist_url(url: str, fallback_zip: str | None = None) -> str:
    """Clean a craigslist search URL: move zips/prices/owner-words from the query text
    into their real params, pick the vehicles category for generic vehicle searches, and
    rewrite the region subdomain to the one nearest the zip. `fallback_zip` localizes a URL
    that carries NO location of its own (used to self-heal a watch from its instruction).
    Idempotent; on any error the original URL is returned unchanged."""
    try:
        p = urlparse(url)
        host = (p.netloc or "").lower()
        if not host.endswith(_CL_SUFFIX) and host != "craigslist.org":
            return url

        q = dict(parse_qsl(p.query, keep_blank_values=True))
        text = q.get("query", "")
        path = p.path or ""

        # Where the model THINKS this search is (valid region subdomain) — used to
        # disambiguate place names ("the Anacortes near Seattle, not one in Kansas").
        sub = host[: -len(_CL_SUFFIX)] if host.endswith(_CL_SUFFIX) else ""
        sub = sub.removeprefix("www.").split(".")[0] if sub else ""
        anchor = None
        if sub in _known_hosts():
            a = next((a for a in _areas() if a["host"] == sub), None)
            if a:
                anchor = (a["lat"], a["lon"])

        # 1. Query hygiene: params hiding in the text.
        text, min_p, max_p = _extract_price(text)
        text, zip5 = _extract_zip(text)
        text, purveyor = _extract_purveyor(text)
        if max_p is not None and "max_price" not in q:
            q["max_price"] = str(max_p)
        if min_p is not None and "min_price" not in q:
            q["min_price"] = str(min_p)
        if zip5 and "postal" not in q:
            q["postal"] = zip5
        if purveyor and "purveyor" not in q:
            q["purveyor"] = purveyor

        # 2. Location. Resolution order: explicit postal param > zip found in the query >
        #    "in <place>" in the query > a hallucinated place-name subdomain.
        loc = zip_latlon(q["postal"]) if q.get("postal") else None
        if loc is None:
            text, loc = _extract_in_place(text, anchor)
        if loc is None and sub and sub not in _known_hosts():
            # "anacortes.craigslist.org" isn't a region — it's the town the user named.
            loc = place_latlon(sub.replace("-", " "))
        if loc is None and fallback_zip and zip_latlon(fallback_zip):
            # No location anywhere in the URL — localize from the caller's hint (the watch's
            # instruction). This is what corrects an existing "vehicles in anacortes" watch
            # whose stored URL points at the wrong region with no postal.
            loc = zip_latlon(fallback_zip)
        if loc:
            # Fill a missing postal — or REPLACE one that doesn't resolve (a model-hallucinated
            # zip like 98210-for-Anacortes), so search_distance has a real anchor.
            if not q.get("postal") or not zip_latlon(q["postal"]):
                z = nearest_zip(*loc)
                if z:
                    q["postal"] = z
            right = nearest_region_host(*loc)
            if right and sub != right:
                host = right + _CL_SUFFIX

        # 3. Category: a generic vehicle search ("vehicles", "autos") belongs in
        #    cars+trucks whatever category the model picked; bare "cars"/"trucks" are only
        #    generic on the everything category — on /search/cta they're deliberate
        #    narrowing and stay.
        stripped_path = path.rstrip("/")
        cat = re.search(r"/search/([a-z]{3})$", stripped_path)
        cat = cat.group(1) if cat else ("sss" if stripped_path.endswith("/search") else None)
        if _GENERIC_VEHICLE.search(text):
            # "vehicles"/"autos" are never a useful term — the CATEGORY says it. Fold the
            # narrowing words in too and land on cars+trucks whatever category was picked.
            text = _GENERIC_VEHICLE.sub(" ", text)
            text = _NARROW_VEHICLE.sub(" ", text)
            if cat not in ("cta", "cto", "ctd"):
                path = re.sub(r"/search(/[a-z]{3})?/?$", "/search/cta", path)
        elif _NARROW_VEHICLE.search(text) and cat == "sss":
            # Bare "cars"/"trucks" on the everything category = the vehicles category.
            # On /search/cta they're deliberate narrowing and stay.
            text = _NARROW_VEHICLE.sub(" ", text)
            path = re.sub(r"/search(/[a-z]{3})?/?$", "/search/cta", path)

        text = re.sub(r"\s+", " ", text).strip(" ,-")
        if text:
            q["query"] = text
        else:
            q.pop("query", None)

        # 4. A postal filter without a radius filters nothing useful — default 50 miles.
        if q.get("postal") and not q.get("search_distance"):
            q["search_distance"] = "50"

        # 5. A bare craigslist.org / www host can't serve a search (404) — if we resolved
        #    a location it was fixed above; otherwise leave the URL for the model/user to fix.
        return urlunparse(p._replace(netloc=host, path=path, query=urlencode(q)))
    except Exception as exc:
        log.debug("refine_craigslist_url failed for %s: %s", url, exc)
        return url


# Fake price-param names the model invents, per canonical target. Seen live:
# OfferUp got "priceMax=10000" (no such param) on a fabricated city path that 403'd.
_MAX_ALIASES = ("max_price", "price_max", "pricemax", "maxprice", "_udhi", "priceceiling")
_MIN_ALIASES = ("min_price", "price_min", "pricemin", "minprice", "_udlo", "pricefloor")


def _pull_price_aliases(q: dict) -> tuple[int | None, int | None]:
    """Remove every price param variant from q, returning (min, max) if any parse."""
    lo = hi = None
    for k in list(q):
        kl = k.lower()
        try:
            if kl in _MAX_ALIASES:
                hi = hi or int(float(str(q.pop(k)).replace(",", "").lstrip("$")))
            elif kl in _MIN_ALIASES:
                lo = lo or int(float(str(q.pop(k)).replace(",", "").lstrip("$")))
        except (ValueError, TypeError):
            q.pop(k, None)
    return lo, hi


def refine_offerup_url(url: str) -> str:
    """Canonicalize an OfferUp search URL. VERIFIED live: offerup.com/search honors ONLY
    q, radius, price_min, price_max; location cannot be set by URL (OfferUp geolocates
    the requester's IP — the user's real area, which is what a local watch wants). The
    model's invented city paths ("/WA-Anacortes/search") 403 — everything is rewritten
    onto the real /search endpoint and location words are dropped from q."""
    try:
        p = urlparse(url)
        host = (p.netloc or "").lower()
        if not host.endswith("offerup.com"):
            return url

        q = dict(parse_qsl(p.query, keep_blank_values=True))
        text = q.get("q") or q.get("query") or ""
        q.pop("query", None)
        lo, hi = _pull_price_aliases(q)

        # Clean the query text with the shared parsers. The zip/place is DISCARDED
        # (nowhere to put it — IP geolocation localizes instead), but its presence
        # signals local intent → keep the default 50-mile radius explicit.
        text, min_p, max_p = _extract_price(text)
        text, zip5 = _extract_zip(text)
        text, loc = _extract_in_place(text)
        had_location = bool(zip5 or loc)
        text = re.sub(r"\s+", " ", text).strip(" ,-")

        out = {}
        if text:
            out["q"] = text
        if (hi or max_p) is not None:
            out["price_max"] = str(hi or max_p)
        if (lo or min_p) is not None:
            out["price_min"] = str(lo or min_p)
        radius = q.get("radius")
        if radius or had_location:
            out["radius"] = str(radius or 50)

        return urlunparse(p._replace(netloc="offerup.com", path="/search",
                                     query=urlencode(out), fragment=""))
    except Exception as exc:
        log.debug("refine_offerup_url failed for %s: %s", url, exc)
        return url


def refine_ebay_url(url: str, fallback_zip: str | None = None) -> str:
    """Clean an eBay search URL: prices → _udlo/_udhi, zips/'in <town>' phrases in the
    _nkw text → _stpos (zip) + _sadis (radius, miles). `fallback_zip` lets a location
    known from ANOTHER url of the same watch (e.g. craigslist's postal) localize eBay
    too — 'vehicles in anacortes on craigslist and ebay' localizes both."""
    try:
        p = urlparse(url)
        host = (p.netloc or "").lower()
        if not host.endswith("ebay.com"):
            return url

        q = dict(parse_qsl(p.query, keep_blank_values=True))
        text = q.pop("_nkw", "") or q.pop("q", "") or q.pop("query", "")
        lo, hi = _pull_price_aliases(q)

        text, min_p, max_p = _extract_price(text)
        text, zip5 = _extract_zip(text)
        text, loc = _extract_in_place(text)
        if not zip5 and loc:
            zip5 = nearest_zip(*loc)
        if not zip5 and fallback_zip:
            zip5 = fallback_zip
        text = re.sub(r"\s+", " ", text).strip(" ,-")

        # A generic vehicle search ("vehicles", "autos", "cars and trucks") as a plain eBay
        # keyword returns die-cast toys, parts, and accessories — never actual cars. Route it
        # into eBay Motors → Cars & Trucks (_sacat=6001), where only real vehicles live, and
        # fold the generic words in (the category IS the filter). Drop any bogus _dcat.
        if _GENERIC_VEHICLE.search(text):
            q["_sacat"] = "6001"
            q.pop("_dcat", None)
            text = _GENERIC_VEHICLE.sub(" ", text)
            text = _NARROW_VEHICLE.sub(" ", text)
            text = re.sub(r"\s+", " ", text).strip(" ,-")

        if text:
            q["_nkw"] = text
        else:
            q.pop("_nkw", None)
        if (hi or max_p) is not None and "_udhi" not in q:
            q["_udhi"] = str(hi or max_p)
        if (lo or min_p) is not None and "_udlo" not in q:
            q["_udlo"] = str(lo or min_p)
        if zip5 and "_stpos" not in q:
            q["_stpos"] = zip5
        if q.get("_stpos") and not q.get("_sadis"):
            q["_sadis"] = "50"

        # This app monitors USED marketplace goods. An eBay "new-only" condition filter
        # (LH_ItemCondition new-family codes < 3000: New / New-other / refurbished) excludes
        # exactly the used items we want AND lets brand-new toys/parts/accessories flood the
        # feed — that's how a "vehicles" search returned Hot Wheels + antifreeze. Drop it.
        cond = q.get("LH_ItemCondition")
        if cond:
            codes = [c.strip() for c in re.split(r"[|,]", cond) if c.strip()]
            if codes and all(c.isdigit() and int(c) < 3000 for c in codes):
                q.pop("LH_ItemCondition", None)

        path = p.path if p.path.startswith("/sch/") else "/sch/i.html"
        return urlunparse(p._replace(netloc="www.ebay.com", path=path, query=urlencode(q)))
    except Exception as exc:
        log.debug("refine_ebay_url failed for %s: %s", url, exc)
        return url


def refine_facebook_url(url: str) -> str:
    """Clean a Facebook Marketplace search URL: prices in the query text → the real
    minPrice/maxPrice params; zips/'in <town>' phrases are stripped from the query (the
    CITY PATH segment carries location on FB, and it's already there when the model
    followed the URL-format rules)."""
    try:
        p = urlparse(url)
        host = (p.netloc or "").lower()
        if not host.endswith("facebook.com") or "/marketplace" not in p.path:
            return url

        q = dict(parse_qsl(p.query, keep_blank_values=True))
        text = q.get("query", "")
        lo, hi = _pull_price_aliases(q)

        text, min_p, max_p = _extract_price(text)
        text, _zip5 = _extract_zip(text)
        text, _loc = _extract_in_place(text)
        text = re.sub(r"\s+", " ", text).strip(" ,-")

        if text:
            q["query"] = text
        else:
            q.pop("query", None)
        if (hi or max_p) is not None:
            q["maxPrice"] = str(hi or max_p)
        if (lo or min_p) is not None:
            q["minPrice"] = str(lo or min_p)

        return urlunparse(p._replace(query=urlencode(q)))
    except Exception as exc:
        log.debug("refine_facebook_url failed for %s: %s", url, exc)
        return url


def refine_search_url(url: str, fallback_zip: str | None = None) -> str:
    """Site-dispatching URL refiner — the one entry point callers should use."""
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return url
    if "craigslist" in host:
        return refine_craigslist_url(url, fallback_zip=fallback_zip)
    if "offerup" in host:
        return refine_offerup_url(url)
    if "ebay" in host:
        return refine_ebay_url(url, fallback_zip=fallback_zip)
    if "facebook" in host:
        return refine_facebook_url(url)
    return url


def zip_from_text(text: str) -> str | None:
    """A US zip resolved from free text (a watch's instruction): a literal 5-digit zip, an
    'in/near <town>' phrase, or a bare known town name. Lets a watch be localized from what
    the user asked for when its URL carries no location. None if nothing resolves."""
    if not text:
        return None
    for m in re.finditer(r"\b(\d{5})\b", text):
        if zip_latlon(m.group(1)):
            return m.group(1)
    _, loc = _extract_in_place(text)
    if loc is None:
        # A bare town name anywhere in the text — only accept a nationally-UNIQUE one
        # (place_latlon returns None for ambiguous names without an anchor), so we never
        # guess the wrong Springfield.
        for w in re.findall(r"[a-z][a-z.'\-]{3,}", (text or "").lower()):
            w = w.strip(" .'-")            # drop a trapped sentence-ending period ("anacortes.")
            ll = place_latlon(w)
            if ll:
                loc = ll
                break
    return nearest_zip(*loc) if loc else None


def ensure_location(url: str, hint_text: str) -> str:
    """Refine a search URL and, if it still carries no location, localize it from hint_text
    (the watch's instruction). Existing watches created before the location fixes — whose
    stored URL points at the wrong craigslist region — self-heal on their next sweep with
    no recreation needed. Falls back to a plain refine on any failure."""
    try:
        host = (urlparse(url).netloc or "").lower()
        if ("craigslist" in host or "ebay" in host) and url_zip(url) is None:
            z = zip_from_text(hint_text)
            if z:
                return refine_search_url(url, fallback_zip=z)
        return refine_search_url(url)
    except Exception:
        return url


def url_zip(url: str) -> str | None:
    """The zip a refined URL is localized to, if any (craigslist postal / eBay _stpos).
    Used to propagate one site's location to sites in the same watch that lack one."""
    try:
        q = dict(parse_qsl(urlparse(url).query))
        z = q.get("postal") or q.get("_stpos")
        return z if z and zip_latlon(z) else None
    except Exception:
        return None
