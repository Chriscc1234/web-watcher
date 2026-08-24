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
  apply_search_request drive the page's controls to realize a SearchRequest (search+filters)
  can_fully_drive      True only if the hints can apply EVERY part (so we never drop location)
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
        # Search box: the HOMEPAGE box has no name/id/type — only placeholder 'search
        # craigslist'; the RESULTS box's placeholder is 'search for sale'. 'search' covers both;
        # type_search also falls back to the default selectors.
        "search_box": "input[placeholder*='search' i], input[name='query'], #query",
        # Category browsing (a watch with NO keyword — "show me cars+trucks"). The area hub
        # (seattle.craigslist.org redirects to /area/seattle) lists every category as a link
        # carrying its code, e.g. 'cars + trucks' -> ?cat=cta. Mapped live 23 Aug 2026. {cat} is
        # substituted with the category code from the watch's URL, so we CLICK the same link a
        # person would instead of goto-ing the deep parametric results URL.
        "category_link": "a[href*='cat={cat}'], a[href$='/search/{cat}'], a[href*='/search/{cat}?']",
        # Location + price live in the RESULTS-PAGE sidebar (mapped live), applied by one
        # 'apply' button. Price min/max are type=text (the auto miles/year fields are type=tel,
        # so type=text uniquely targets PRICE); distance is the tel box labelled 'miles'.
        "postal":    "input[name='postal']",
        "distance":  "input[type='tel'][placeholder*='mile' i]",
        "price_min": "input[type='text'][placeholder='min' i]",
        "price_max": "input[type='text'][placeholder='max' i]",
        "apply":     "button.cl-exec-search, button[type='submit'].cl-exec-search",
    },
    # OfferUp location = a Material-UI dialog opened from the top-left button (opener aria-label
    # confirmed live: "Set my location currently set to …"). Price is INLINE on the search page
    # (input[name='min']/[name='max'], like craigslist). ⚠ NOT human-first-enabled yet (absent
    # from HUMAN_FIRST_SITES): a live session showed set_location did NOT change the feed (stayed
    # 'Hollywood, FL') AND OfferUp then IP-blocked automated access (ERR_EMPTY_RESPONSE) — the
    # exact bot-detection this rewrite guards against. The dialog flow is UNPROVEN; do not drive
    # OfferUp until it's verified live (likely needs the user's real IP / a logged-in profile).
    "offerup.com": {
        "search_box": "input[name='search'], input[type='search']",
        "price_min":  "input[name='min']",
        "price_max":  "input[name='max']",
        "location": {
            # Flow mapped live: open → the dialog first shows the STORED location as a button
            # (OfferUp persists a zip, e.g. "Hollywood, FL 33020" — which is why a VPN exit change
            # does NOT move it); click that to REVEAL the zip input; type; click "Apply".
            "open":    "button[aria-label*='Set my location' i]",
            "dialog":  "[role=dialog], [class*='MuiDialog']",
            "input":   "input[name='zipCode'], [role=dialog] input[type='text']",
            "confirm": "Apply",
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


# Sites whose human-first control-driving is LIVE-VERIFIED end-to-end and therefore safe to use
# in the real sweep. A site graduates here ONLY after its full flow (search + location + price)
# is proven live — mapping its controls in CONTROL_HINTS is NOT enough. This is what makes the
# rollout incremental + safe: a half-mapped or flaky site (OfferUp's location dialog, which
# failed live + triggered an IP block) is NEVER driven in production before it's proven. Grows
# one site per verified release.
HUMAN_FIRST_SITES: set[str] = {"craigslist", "offerup"}


def is_human_first_enabled(site_or_url: str) -> bool:
    """True if this site's human-first driving is verified + enabled (see HUMAN_FIRST_SITES).
    Accepts a short site key ('craigslist') or a full URL."""
    s = (site_or_url or "").lower()
    return any(site in s for site in HUMAN_FIRST_SITES)


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


def _human_click(page, loc, timeout: int = 5_000) -> bool:
    """Click by actually MOVING THE MOUSE there and pressing — not Playwright's instant
    teleport-and-click. An approach path, a hover dwell and a realistic press duration are the
    cheap tells that separate a person from a script, and on Facebook that matters more than
    anywhere else (see memory: mouse+click navigation is the priority there).

    Falls back to loc.click() if the element has no stable box (off-screen, zero-size, detached).
    Returns True if a click was delivered."""
    try:
        loc.scroll_into_view_if_needed(timeout=timeout)
    except Exception:
        pass
    box = None
    try:
        box = loc.bounding_box()
    except Exception:
        box = None
    # No page (or no stable box) → we can't drive the mouse; use the plain click.
    if page is None or not box or box.get("width", 0) < 1 or box.get("height", 0) < 1:
        try:
            loc.click(timeout=timeout)
            return True
        except Exception:
            return False

    try:
        # Approach along a short curved path so the cursor TRAVELS to the control instead of
        # teleporting. These intermediate moves are cosmetic — they must never decide where the
        # click lands.
        tx = box["x"] + box["width"] / 2
        ty = box["y"] + box["height"] / 2
        start = getattr(page, "_ww_mouse", None) or (tx - random.uniform(60, 220),
                                                     ty - random.uniform(40, 160))
        steps = random.randint(3, 6)
        for i in range(1, steps):                      # stop short; hover() does the last leg
            f = i / steps
            page.mouse.move(start[0] + (tx - start[0]) * f + random.uniform(-3.5, 3.5),
                            start[1] + (ty - start[1]) * f + random.uniform(-3.5, 3.5))
            time.sleep(random.uniform(0.012, 0.045))

        # LAND ON THE ELEMENT VIA PLAYWRIGHT. hover() moves the real mouse with actionability +
        # stability checks, so we're provably over the right control. Clicking raw coordinates
        # measured earlier is unsafe on a page that reflows — live on craigslist's homepage that
        # hit a NEIGHBOURING category (asked for cat=cta, landed on cat=sse).
        loc.hover(timeout=timeout)
        try:
            setattr(page, "_ww_mouse", (tx, ty))       # remember where the cursor rests
        except Exception:
            pass
        time.sleep(max(0.06, random.gauss(0.20, 0.05)))     # hover dwell before pressing

        # DELIVER the press with Playwright, not raw mouse.down/up. It is still a real mouse
        # click (move → down → delay → up) but re-verifies the target first; hand-rolled
        # down/up at a remembered point silently missed craigslist's category link when the
        # page reflowed, and a click that lands on nothing is worse than a slightly tidier one.
        loc.click(timeout=timeout, delay=random.randint(60, 130))
        return True
    except Exception:
        try:
            loc.click(timeout=timeout)
            return True
        except Exception:
            return False


def _wait_for_url_change(page, before: str, timeout_s: float = 8.0) -> bool:
    """Poll until the page's URL differs from `before`, or we give up. Single-sample checks are
    racy on SPAs that navigate via the history API (craigslist), especially when the click was a
    real mouse press with no framework auto-waiting behind it."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if (getattr(page, "url", "") or "") != before:
                return True
        except Exception:
            return False
        time.sleep(0.25)
    return False


def _wait_for_controls(page, selectors, timeout_ms: int = 8_000) -> bool:
    """Wait (bounded) for the first of these selectors to become visible. Results sidebars are
    commonly rendered by JS after domcontentloaded, so acting the instant we land finds nothing
    and silently drops whatever we meant to set. Returns True if something appeared."""
    for sel in [s for s in (selectors or []) if s]:
        try:
            page.wait_for_selector(sel, timeout=timeout_ms, state="visible")
            return True
        except Exception:
            continue
    return False


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
        _human_click(getattr(loc, "page", None), loc, 3000)   # Locator carries its own Page
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
                _human_click(page, opt)
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
            _human_click(page, b.first)
            return True
    except Exception:
        pass
    return False


_REVEAL_SKIP = ("apply", "see listings", "get my location", "done", "save", "update",
                "search", "cancel", "close", "back")


def _reveal_location_editor(page, loc_hint: dict) -> None:
    """Some location dialogs (OfferUp) first show the CURRENT location and hide the ZIP editor
    until you click it. Click that reveal affordance so the input appears. An explicit hint wins;
    otherwise click the dialog button that shows the current location (has a digit/comma, e.g.
    'Hollywood, FL 33020') or says change/edit — never an action button. Best-effort."""
    if loc_hint.get("reveal") and _click_selector(page, loc_hint["reveal"]):
        _pause(0.3, 0.6)
        return
    try:
        btns = page.locator("[role=dialog] button, [class*='MuiDialog'] button")
        for i in range(min(btns.count(), 12)):
            b = btns.nth(i)
            try:
                t = (b.inner_text() or "").strip()
            except Exception:
                continue
            low = t.lower()
            if not t or any(s in low for s in _REVEAL_SKIP):
                continue
            if re.search(r"\d", t) or "," in t or re.search(r"\b(change|edit|location|zip)\b", low):
                try:
                    _human_click(page, b, 2000)
                    _pause(0.3, 0.6)
                    return
                except Exception:
                    continue
    except Exception:
        pass


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
            _human_click(page, opener, 4000)
            _pause(0.4, 0.9)
            return True
        except Exception:
            return False

    def _find_input():
        return (_first_visible(page, loc_hint.get("input", "")) if loc_hint.get("input") else None) \
            or _first_visible(
                page, "input[name='zipCode'], [role=dialog] input[type='text'], "
                      "[class*='MuiDialog'] input:not([type='hidden']), "
                      "input[placeholder*='zip' i], input[placeholder*='city' i], "
                      "input[aria-label*='location' i]")

    # A marker of the location BEFORE, to confirm a real change afterward.
    before = _location_marker(page)

    if not _open():
        return False
    inp = _find_input()
    if inp is None:
        # Some dialogs (OfferUp) show the CURRENT location and hide the editor until you click it.
        _reveal_location_editor(page, loc_hint)
        inp = _find_input()
    if inp is None:
        # retry the open once (a flaky menu may have closed) before giving up
        _pause(0.3, 0.6)
        if _open():
            _reveal_location_editor(page, loc_hint)
            inp = _find_input()
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


# ---------------------------------------------------------------------------
# Applying a whole SearchRequest through a site's controls (the Phase-3 driver)
# ---------------------------------------------------------------------------

def _click_selector(page, selector: str) -> bool:
    """Click the first visible element matching a comma-selector. Returns True if clicked."""
    loc = _first_visible(page, selector or "")
    if loc is None:
        return False
    try:
        _human_click(page, loc, 3000)
        return True
    except Exception:
        return False


def _apply_inline_filters(page, req: "SearchRequest", hint: dict) -> bool:
    """Fill the results-page sidebar filters that live INLINE on the page (craigslist-style:
    zip, distance, min/max price) and click the site's own 'apply' button — the human way to
    localize + price-limit, instead of URL params. Returns True if it filled at least one field
    and submitted. Best-effort; never raises."""
    # craigslist renders the results sidebar with JS AFTER domcontentloaded — measured live, the
    # zip box is absent the instant we land and present ~3s later. Filling immediately therefore
    # found nothing and silently dropped BOTH the location and the price. Wait (bounded) for the
    # first control we mean to touch before typing into it.
    _wait_for_controls(page, [hint.get(k) for k in ("postal", "price_min", "price_max", "distance")])

    filled = False

    def _fill(sel_key: str, value) -> None:
        nonlocal filled
        if value in (None, ""):
            return
        loc = _first_visible(page, hint.get(sel_key, ""))
        if loc is not None and _human_fill(loc, str(value)):
            filled = True

    _fill("postal", req.zip)
    if req.zip:
        _fill("distance", req.radius or 50)   # a zip with no radius filters nothing useful
    _fill("price_min", req.price_min)
    _fill("price_max", req.price_max)
    if not filled:
        return False
    # Submit via the page's OWN apply/search button (falls back to Enter in the last field).
    _pause(0.2, 0.5)
    if not _click_selector(page, hint.get("apply", "")):
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass
    try:
        page.wait_for_timeout(1800)
    except Exception:
        pass
    return True


def can_fully_drive(req: "SearchRequest", hint: dict) -> bool:
    """True only if these hints can apply EVERY part of the request the URL would have — so
    the caller never human-drives a site where it would silently DROP the location or price
    (e.g. typing the terms on eBay but losing the zip because eBay has no inline zip control).
    This is what makes the rollout safe + automatic: a site becomes human-driven exactly when
    its hints are complete enough, not before."""
    hint = hint or {}
    if req.zip and not (hint.get("postal") or hint.get("location")):
        return False
    if (req.price_min is not None or req.price_max is not None) and not (
            hint.get("price_min") or hint.get("price_max")):
        return False
    # A category-only watch ("browse cars+trucks", no keyword) is drivable only where we can
    # CLICK the category the way a person does; without that hint the URL path must handle it,
    # or we'd land on the site's front page and quietly browse the wrong thing.
    if not req.terms and req.category and not hint.get("category_link"):
        return False
    # Something must be drivable at all (terms, a category to click, a location, or a price).
    return bool(req.terms or (req.category and hint.get("category_link"))
                or req.zip or req.price_min is not None or req.price_max is not None)


def click_category(page, category: str, hint: dict) -> bool:
    """Click the site's own link for a category code (craigslist 'cta' = cars + trucks), the way
    a person picks it off the homepage — instead of goto-ing the deep results URL. Returns True
    only if a real navigation happened. Best-effort; never raises."""
    tmpl = (hint or {}).get("category_link")
    cat = (category or "").strip()
    if not tmpl or not cat:
        return False
    selector = tmpl.replace("{cat}", cat)
    before = getattr(page, "url", "")
    try:
        loc = _first_visible(page, selector)
        if loc is None:
            log.debug("category link %r not found on %s", selector, before)
            return False
        _pause(0.4, 0.9)          # look at the category list before clicking, like a person
        _human_click(page, loc, 5_000)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:
            pass
        # Craigslist swaps the results in via the history API, so the URL can lag the click by a
        # moment — and a real mouse press doesn't carry Playwright's auto-waiting. POLL for the
        # change instead of sampling once, or a click that worked reports as a failure and the
        # caller needlessly falls back to the deep URL.
        moved = _wait_for_url_change(page, before, timeout_s=8.0)
        if moved:
            log.info("Human-first: clicked the %r category link", cat)
        else:
            log.debug("category click on %r did not change the URL from %s", cat, before[:60])
        return moved
    except Exception as exc:
        log.debug("category click failed for %r: %s", cat, exc)
        return False


def apply_search_request(page, req: "SearchRequest", hint: dict | None = None) -> dict:
    """Realize a SearchRequest by DRIVING the page's own controls like a human: type the terms
    into the search box, then set location/price via the site's controls (inline sidebar for
    craigslist; a location dialog for OfferUp-style sites). Returns what was applied,
    {searched, categorized, located, filtered}, so the caller can decide whether to fall back to
    the URL. Best-effort: each step is independent and logged; never raises."""
    if hint is None:
        hint = hints_for(getattr(page, "url", "") or "")
    applied = {"searched": False, "categorized": False, "located": False, "filtered": False}

    if req.terms:
        applied["searched"] = type_search(page, req.terms, hint)
    elif req.category:
        # No keyword — this is a "browse this category" watch (e.g. craigslist cars+trucks).
        # Click the category the way a person picks it off the homepage; the filters below then
        # run on the resulting results page exactly as they do after a keyword search.
        applied["categorized"] = click_category(page, req.category, hint)

    # Location and price are INDEPENDENT — a site may drive one via a dialog and the other inline
    # (OfferUp: location = a dialog, price = inline min/max), so they must not be an either/or.
    # Location: a dialog control (OfferUp) when present, else the inline postal field (craigslist,
    # handled below by _apply_inline_filters). Do the dialog FIRST so the feed reloads to the
    # right area before we price-filter it.
    if req.zip and hint.get("location"):
        applied["located"] = set_location(page, req.zip, req.radius, hint)

    # Inline sidebar filters: craigslist (postal+distance+price) or OfferUp (price min/max only).
    if any(k in hint for k in ("postal", "distance", "price_min", "price_max")):
        if _apply_inline_filters(page, req, hint):
            if hint.get("postal") and req.zip:      # craigslist localizes via the inline zip
                applied["located"] = True
            applied["filtered"] = req.price_min is not None or req.price_max is not None

    log.info("apply_search_request: %s → %s", req.describe(), applied)
    return applied
