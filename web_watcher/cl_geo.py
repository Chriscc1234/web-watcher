"""
Craigslist URL refinement — deterministic query hygiene + region correction.

The chat model builds craigslist search URLs from what the user SAID, and small local
models reliably make two mistakes:

  1. They stuff everything into the query text: "vehicles owner 98221 under 10k" —
     a literal search that matches nothing. Craigslist wants those as PARAMS
     (postal=, max_price=, purveyor=) with only real item words in query=.
  2. They guess the region subdomain from fame, not geography: "anacortes" became
     seattle.craigslist.org when Anacortes is served by skagit.craigslist.org.
     (Bare craigslist.org can't be used instead — search paths 404 there.)

Both are fixable without any model: a bundled US zip→lat/lon table (Census ZCTA
gazetteer, public domain) plus craigslist's own area list (reference.craigslist.org/Areas,
bundled snapshot) resolve any zip to its true nearest region, and the query text can be
parsed for prices/zips/owner-words deterministically.

`refine_craigslist_url` is idempotent and failure-tolerant (any error returns the URL
unchanged), so it's safe to run on every create/update AND every sweep — stored bad URLs
self-heal the next time they're used.

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  zip_latlon          ~L60   zip5 → (lat, lon) from the bundled gazetteer (lazy, cached)
  nearest_region_host ~L80   lat/lon → closest craigslist region hostname
  refine_craigslist_url ~L110  the full clean-up: query → params + region rewrite
  _extract_price / _extract_zip / _extract_purveyor    query-text parsers
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


# ---------------------------------------------------------------------------
# The refinement
# ---------------------------------------------------------------------------

def refine_craigslist_url(url: str) -> str:
    """Clean a craigslist search URL: move zips/prices/owner-words from the query text
    into their real params, pick the vehicles category for generic vehicle searches, and
    rewrite the region subdomain to the one nearest the zip. Idempotent; on any error the
    original URL is returned unchanged."""
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
            m = _IN_PLACE_RE.search(text)
            if m:
                # Try the full captured phrase, then trim words off the end ("in anacortes
                # for cheap" → "anacortes for cheap" → "anacortes").
                words = re.findall(r"[a-z.'\-]+", m.group(1).lower())
                for n in range(min(3, len(words)), 0, -1):
                    cand = " ".join(words[:n])
                    ll = place_latlon(cand, anchor)
                    if ll:
                        loc = ll
                        text = re.sub(r"\b(?:in|near|around)\s+" + re.escape(cand),
                                      " ", text, count=1, flags=re.I)
                        break
        if loc is None and sub and sub not in _known_hosts():
            # "anacortes.craigslist.org" isn't a region — it's the town the user named.
            loc = place_latlon(sub.replace("-", " "))
        if loc:
            if not q.get("postal"):
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
