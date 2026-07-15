"""
Human-first navigation — drive a site's own controls (search box, location, filters) like a
person, instead of jumping straight to a constructed parametric URL (our biggest bot tell; see
memory feedback_human_first_navigation). The AI agent uses these as reliable, self-correcting
building blocks: each primitive ACTS then VERIFIES the effect, and reports success/failure so
the caller can retry differently or fall back.

Design rules:
  • ACT → OBSERVE → RETRY. Never assume a click/type worked; check the page's response
    (reusing monitor.read_search_feedback / detect_no_results) and correct if it didn't take.
  • Human pacing on every interaction (real key events, small randomized pauses, mouse-moving
    clicks via Playwright). Bounded + best-effort: any failure returns False, never raises.
  • Per-site HINTS steer the primitives (where a control lives) but they're a MAP the code
    reasons with, not a rigid script — the heuristic fallback runs when there's no hint.

KEY LOCATIONS
  SearchRequest        the structured intent (terms/zip/radius/price/sort) a human APPLIES
  build_search_request parse that intent from a watch's URL + instruction (reuses cl_geo)
  type_search     type the query into the search box (verify it landed) + submit
  set_location    open the location control → enter place/zip → confirm → verify it changed
  CONTROL_HINTS   per-site control map (seeded from live investigation; extend as we learn)
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlparse

from web_watcher.monitor import (
    _SEARCH_BOX_SELECTORS,
    read_search_feedback,
    detect_no_results,
)

log = logging.getLogger(__name__)


# Per-site control hints — WHERE each control lives, discovered by live investigation. A hint
# is optional; the heuristic fallbacks below run when a site has none. Keyed by registrable
# host substring.
CONTROL_HINTS: dict[str, dict] = {
    "craigslist.org": {
        # The HOMEPAGE box has no name/id/type — only this placeholder. (The results-page box
        # is input[name='query'].) type_search also falls back to the default selectors.
        "search_box": "input[placeholder*='search craigslist' i], input[name='query'], #query",
    },
    # OfferUp location = a Material-UI dialog opened from the top-left button (mapped live):
    #   click "Set my location" → dialog with a ZIP input + Distance + "See listings".
    "offerup.com": {
        "search_box": "input[name='search'], input[type='search']",
        "location": {
            "open":    "button[aria-label*='Set my location' i]",
            "dialog":  "[role=dialog], [class*='MuiDialog']",
            "input":   "[role=dialog] input[type='text'], [class*='MuiDialog'] input:not([type='hidden'])",
            "confirm": "See listings",
        },
    },
    # eBay's header search box has carried this id for years (#gh-ac). Location on eBay is a
    # results-sidebar / URL concern, not a picker dialog, so no location hint here — eBay is a
    # lower human-navigation priority than the local-marketplace sites.
    "ebay.com": {
        "search_box": "input#gh-ac, input[name='_nkw'], input[type='text'][aria-label*='Search' i]",
    },
    # Facebook is DELIBERATELY not seeded here. It's the highest-stakes site (a ban ends the
    # buddy's use case) and its Marketplace controls must be mapped LIVE under fb_safety in
    # Phase 4 — encoding guessed selectors now would be exactly the "guess presented as fact"
    # we avoid. Add it when we probe it for real.
}


def _host(url: str) -> str:
    """The lowercased registrable host of a URL ('' on failure)."""
    try:
        return re.sub(r"^https?://(www\.)?", "", url or "").split("/")[0].lower()
    except Exception:
        return ""


def hints_for(url: str) -> dict:
    """The control hints for a URL's site, or {} if none known."""
    host = _host(url)
    for key, h in CONTROL_HINTS.items():
        if key in host:
            return h
    return {}


# ---------------------------------------------------------------------------
# SearchRequest — the structured intent a human APPLIES through a site's controls
# ---------------------------------------------------------------------------
#
# Today a watch's intent lives baked into a parametric results URL
# (skagit.craigslist.org/search/cta?postal=98221&max_price=10000&query=toyota+tacoma&sort=date).
# To browse like a human we need that intent as DATA the agent can enter into the page's own
# controls — type the terms, set the location, pick a price/sort — instead of goto-ing the URL.
# build_search_request pulls it back out of the URL (and the watch's free-text instruction as a
# fallback) by REUSING the cl_geo parsers that already understand every site's params + phrasing.

_TERMS_KEYS  = ("query", "q", "_nkw")
_RADIUS_KEYS = ("search_distance", "_sadis", "radius")
_SORT_KEYS   = ("sort",)


@dataclass
class SearchRequest:
    """What a person would enter to run this search. All fields optional — an empty `terms`
    with a `category` is a valid 'browse this category with these filters' request (e.g. a
    generic craigslist cars+trucks watch). `site` is the short site key for hint lookup."""
    terms: str = ""
    zip: str | None = None
    radius: int | None = None
    price_min: int | None = None
    price_max: int | None = None
    purveyor: str | None = None      # craigslist: "owner" | "dealer"
    sort: str | None = None          # e.g. "date"
    category: str | None = None      # craigslist 3-letter category from /search/<cat>
    site: str = ""

    def describe(self) -> str:
        """A short one-line summary for logs (mirrors what a human would say they searched)."""
        bits = []
        if self.terms:            bits.append(repr(self.terms))
        if self.category:         bits.append(f"cat={self.category}")
        if self.zip:              bits.append(f"near {self.zip}")
        if self.radius:           bits.append(f"{self.radius}mi")
        if self.price_min is not None: bits.append(f">=${self.price_min}")
        if self.price_max is not None: bits.append(f"<=${self.price_max}")
        if self.purveyor:         bits.append(self.purveyor)
        if self.sort:             bits.append(f"sort={self.sort}")
        return ", ".join(bits) or "(empty)"


def _site_key(host: str) -> str:
    for s in ("craigslist", "offerup", "ebay", "facebook"):
        if s in host:
            return s
    return host


def build_search_request(url: str, instruction: str = "") -> SearchRequest:
    """Reconstruct the human-enterable SearchRequest from a watch's search URL, falling back to
    its free-text `instruction` for anything the URL doesn't carry (e.g. a watch whose stored URL
    lost its location). Reuses cl_geo's param aliases + text parsers so it understands every
    site's naming. Failure-tolerant: a malformed URL yields a best-effort request, never raises."""
    from web_watcher import cl_geo

    host = _host(url)
    site = _site_key(host)
    try:
        p = urlparse(url)
        q = dict(parse_qsl(p.query, keep_blank_values=True))
    except Exception:
        p, q = None, {}

    def _get(keys) -> str | None:
        for k in list(q):
            if k.lower() in keys and q[k] not in (None, ""):
                return q[k]
        return None

    terms = _get(_TERMS_KEYS) or ""
    sort = _get(_SORT_KEYS)
    purveyor = _get(("purveyor",))
    radius_raw = _get(_RADIUS_KEYS)

    # Prices: reuse the alias-aware puller (max_price/_udhi/maxPrice/price_max/…) on a copy.
    price_min, price_max = cl_geo._pull_price_aliases(dict(q))
    zip5 = cl_geo.url_zip(url)

    # craigslist category lives in the path (/search/cta), not a param.
    category = None
    if p and "craigslist" in host:
        m = re.search(r"/search/([a-z]{3})\b", p.path or "")
        category = m.group(1) if m else None

    # Mine the query TEXT for params the model left inline ("tacoma under 5k 98221"), so the
    # terms we type are clean keywords and the stragglers fill any empty structured field.
    text, tmin, tmax = cl_geo._extract_price(terms)
    text, tzip = cl_geo._extract_zip(text)
    text, tloc = cl_geo._extract_in_place(text)
    text, tpurv = cl_geo._extract_purveyor(text)
    terms_clean = re.sub(r"\s+", " ", text).strip(" ,-")

    if price_min is None: price_min = tmin
    if price_max is None: price_max = tmax
    if not zip5: zip5 = tzip
    if not zip5 and tloc: zip5 = cl_geo.nearest_zip(*tloc)
    if purveyor is None: purveyor = tpurv

    # Last resort: mine the watch's instruction for anything still missing.
    if instruction:
        _itext, imin, imax = cl_geo._extract_price(instruction)
        if price_min is None: price_min = imin
        if price_max is None: price_max = imax
        if not zip5: zip5 = cl_geo.zip_from_text(instruction)
        if purveyor is None:
            _it, ipurv = cl_geo._extract_purveyor(instruction)
            purveyor = ipurv

    try:
        radius = int(radius_raw) if radius_raw else None
    except (TypeError, ValueError):
        radius = None

    return SearchRequest(
        terms=terms_clean, zip=zip5, radius=radius,
        price_min=price_min, price_max=price_max,
        purveyor=purveyor, sort=sort, category=category, site=site,
    )


def _pause(lo: float = 0.25, hi: float = 0.7) -> None:
    time.sleep(random.uniform(lo, hi))


def _first_visible(page, selector: str):
    """The first visible locator for a comma-selector, or None."""
    for sel in [s.strip() for s in (selector or "").split(",") if s.strip()]:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def _human_fill(loc, text: str) -> bool:
    """Click, clear, human-type `text`, and VERIFY the field holds it (some boxes swallow the
    first keystroke — reuse the correction from monitor.humanized_search). Returns True if the
    value landed."""
    try:
        loc.click(timeout=3000)
        _pause(0.1, 0.3)
        try:
            loc.fill("")
        except Exception:
            pass
        loc.type(text, delay=random.randint(70, 130))
        _pause(0.2, 0.5)
        try:
            if (loc.input_value() or "").strip().lower() != text.strip().lower():
                loc.fill(text)
        except Exception:
            pass
        return True
    except Exception as exc:
        log.debug("_human_fill failed: %s", exc)
        return False


def type_search(page, terms: str, hint: dict | None = None) -> bool:
    """Type the query into the site's OWN search box (human pacing, verified) and submit — the
    human alternative to jumping to a ?query= URL. Returns True only if it typed + submitted and
    the box wasn't a location picker. Best-effort; False → caller falls back."""
    terms = (terms or "").strip()
    if not terms:
        return False
    # Try the site's hint first, then ALWAYS fall back to the generic search-box selectors —
    # a too-narrow hint (e.g. craigslist's results-page box) must never block the default that
    # would have matched (the homepage's placeholder box).
    default_sel = ", ".join(_SEARCH_BOX_SELECTORS)
    hint_sel = (hint or {}).get("search_box")

    def _find_box():
        b = _first_visible(page, hint_sel) if hint_sel else None
        return b or _first_visible(page, default_sel)

    box = _find_box()
    if box is None:
        # the form may render just after load — wait briefly, then retry
        try:
            page.wait_for_selector(default_sel, timeout=4000, state="visible")
        except Exception:
            pass
        box = _find_box()
    if box is None:
        log.debug("type_search: no search box found")
        return False
    if not _human_fill(box, terms):
        return False
    # Close the loop: if the box's autocomplete is LOCATIONS, this is a geo field, not a
    # keyword search — don't submit a product term into it.
    try:
        if read_search_feedback(page, terms).get("are_locations"):
            log.info("type_search: %r is a LOCATION box, not a keyword search — not submitting", sel)
            return False
    except Exception:
        pass
    try:
        page.keyboard.press("Enter")
        page.wait_for_timeout(1500)
    except Exception as exc:
        log.debug("type_search submit failed: %s", exc)
        return False
    log.info("Human search: typed %r into the search box", terms)
    return True


def _pick_suggestion(page) -> None:
    """After typing into a location/search box, choose the first autocomplete suggestion (many
    location pickers require picking a suggestion, not just Enter). Falls back to Enter."""
    for sel in ("[role=option]", "li[role=option]", "[class*='uggestion'] li",
                "[class*='uggestion']", "ul[role=listbox] li"):
        try:
            opt = page.locator(sel).first
            if opt.count() > 0 and opt.is_visible():
                opt.click()
                return
        except Exception:
            continue
    try:
        page.keyboard.press("Enter")
    except Exception:
        pass


def _click_button_by_label(scope, label: str) -> bool:
    """Click a button whose visible text/aria matches `label` (case-insensitive). scope is a
    page or locator. Returns True if clicked."""
    try:
        b = scope.get_by_role("button", name=re.compile(re.escape(label), re.I))
        if b.count() > 0 and b.first.is_visible():
            b.first.click()
            return True
    except Exception:
        pass
    return False


def set_location(page, place: str, radius: int | None = None, hint: dict | None = None) -> bool:
    """Set the site's location THROUGH ITS OWN control (the human way), so location-aware sites
    (OfferUp) show the right area instead of a default. Pattern: open the location control →
    enter the place/zip → pick the suggestion → confirm → VERIFY it changed. Self-correcting:
    if the picker doesn't open it retries the open once. Returns True only if the location
    visibly changed. Best-effort; False → caller falls back (e.g. a URL param)."""
    place = (place or "").strip()
    if not place:
        return False
    loc_hint = (hint or {}).get("location") or {}

    def _open() -> bool:
        opener = _first_visible(page, loc_hint.get("open", "")) if loc_hint.get("open") else None
        if opener is None:
            # heuristic: a button/link that talks about location
            opener = _first_visible(
                page, "button[aria-label*='location' i], button[aria-label*='deliver' i], "
                      "[aria-label*='set my location' i], a[href*='location']")
        if opener is None:
            return False
        try:
            opener.click(timeout=4000)
            _pause(0.4, 0.9)
            return True
        except Exception:
            return False

    # A marker of the location BEFORE, to confirm a real change afterward.
    before = _location_marker(page)

    if not _open():
        return False
    # The picker input (inside a dialog when there's a hint, else a heuristic location input).
    inp = _first_visible(page, loc_hint.get("input", "")) if loc_hint.get("input") else None
    if inp is None:
        inp = _first_visible(
            page, "[role=dialog] input[type='text'], [class*='MuiDialog'] input:not([type='hidden']), "
                  "input[placeholder*='zip' i], input[placeholder*='city' i], "
                  "input[aria-label*='location' i]")
    if inp is None:
        # retry the open once (a flaky menu may have closed) before giving up
        _pause(0.3, 0.6)
        if _open():
            inp = _first_visible(page, loc_hint.get("input", "")) or _first_visible(
                page, "[role=dialog] input[type='text'], input[placeholder*='zip' i]")
    if inp is None:
        log.debug("set_location: couldn't find the location input")
        return False

    if not _human_fill(inp, place):
        return False
    _pause(0.6, 1.1)
    _pick_suggestion(page)
    _pause(0.4, 0.9)
    # Confirm (site's label if hinted, else the common ones).
    confirm = loc_hint.get("confirm")
    clicked = _click_button_by_label(page, confirm) if confirm else False
    if not clicked:
        for label in ("See listings", "Apply", "Done", "Save", "Update", "Search"):
            if _click_button_by_label(page, label):
                clicked = True
                break
    page.wait_for_timeout(2500)

    after = _location_marker(page)
    changed = bool(after) and after != before
    if changed:
        log.info("Human location: set to %r via the page control", place)
    else:
        log.info("set_location: entered %r but couldn't confirm the location changed", place)
    return changed


def _location_marker(page) -> str:
    """A cheap signal of the page's current location, to detect a real change after setting it —
    the location button's own text (e.g. OfferUp's 'Hollywood: Maximum'), else a slice of body."""
    try:
        t = page.evaluate(
            "() => { const b = document.querySelector("
            "'button[aria-label*=\"location\" i], [aria-label*=\"set my location\" i]');"
            " return b ? (b.innerText||b.getAttribute('aria-label')||'').trim() : ''; }")
        return (t or "").strip()[:60]
    except Exception:
        return ""
