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

from web_watcher import humanize
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
    # Facebook Marketplace — MAPPED LIVE 27 Aug 2026 against the real logged-in page, exactly
    # as the previous note here demanded (nothing below is guessed; every selector came off the
    # page in a read-only probe). Attributes observed:
    #   search box   input[aria-label="Search Marketplace"] — note the page ALSO has
    #                input[aria-label="Search Facebook"], and BOTH are type="search". A generic
    #                input[type=search] matches the GLOBAL one first, which would search all of
    #                Facebook for "macgregor sailboat". The label is the only discriminator.
    #   location     div[role=button][aria-label="Location: Anacortes, Washington, Within 100 mi"]
    #                → dialog with input[aria-label="Location"] (a combobox: you must PICK an
    #                li[role=option], typing alone does nothing) → div[role=button][aria-label=
    #                "Apply"]. Radius is a label[role=combobox] reading "Radius 100 miles".
    # NOT in HUMAN_FIRST_SITES yet: mapped ≠ verified. The flow has to be driven end-to-end
    # under supervision before Facebook is handed the wheel.
    "facebook.com": {
        "search_box": 'input[aria-label*="Search Marketplace" i]',
        # NEVER let the generic selector stand in for that box. Facebook's global "Search
        # Facebook" input is ALSO type="search", so the default matches it happily — and a
        # watch's query then goes to /search/top (posts, people, pages), not Marketplace.
        # Watched live: the Marketplace box renders a beat after the SPA loads, the generic
        # fallback won the race, and the sweep searched the whole of Facebook.
        "search_box_strict": True,
        "location": {
            "open":    'div[role=button][aria-label*="Location:" i], '
                       '[role=button][aria-label*="Location:" i]',
            "dialog":  "[role=dialog]",
            "input":   '[role=dialog] input[aria-label="Location" i]',
            "confirm": "Apply",
            "radius":  '[role=dialog] label[role=combobox]',
        },
    },
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
# facebook graduated 27 Aug 2026 after its flow was DRIVEN live, not merely mapped: the
# location picker was moved Anacortes → Bellingham → Anacortes with the site's own marker
# confirming each change, the already-correct case short-circuited without touching the
# control, and type_search typed into Marketplace's own box (not Facebook's global search)
# and returned 64 listings. The account was left exactly as it was found.
HUMAN_FIRST_SITES: set[str] = {"craigslist", "offerup", "facebook"}


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

    # craigslist category: the classic shape carries it in the path (/search/cta), the current
    # site as a ?cat= param (/search/area/skagit?cat=boo — the shape the hub's own links and our
    # human-first landings produce). Read BOTH, or a watch stored with a modern URL silently
    # loses its category — and a lost category is the golf-clubs-in-a-sailboat-watch bug again,
    # via the extraction side this time. The param wins when both disagree (it's the live one).
    category = None
    if p and "craigslist" in host:
        cat_param = _get(("cat",))
        if cat_param and re.fullmatch(r"[a-z]{3}", cat_param.strip().lower()):
            category = cat_param.strip().lower()
        else:
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
    teleport-and-click. The motor model lives in humanize.py (shared with the agent): bezier
    approach with overshoot-and-correct, off-center aim, hover dwell, human press duration.
    The old inline version here was the weak twin — 3-6 straight-line steps that always
    approached from the upper-left and never overshot; watched live, it read as a machine.

    The landing is still Playwright's hover()+click() actionability pipeline (reflow safety —
    see humanize.py's module docstring for the craigslist lesson). Falls back to loc.click()
    if the element has no stable box. Returns True if a click was delivered."""
    return humanize.click(page, loc, timeout)


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
        # ALREADY HOLDS THIS VALUE? LEAVE IT ALONE. The user watched the sweep click a
        # craigslist filter box, retype the exact value already in it, and walk away —
        # pointless, and re-entering your own zip every visit is a robot's habit, not a
        # person's. Same principle as set_location's already-correct short-circuit.
        try:
            if (loc.input_value() or "").strip().lower() == (text or "").strip().lower() \
                    and (text or "").strip():
                return True
        except Exception:
            pass
        _human_click(getattr(loc, "page", None), loc, 3000)   # Locator carries its own Page
        _pause(0.1, 0.3)
        try:
            loc.fill("")
        except Exception:
            pass
        # Per-keystroke human timing (humanize.type_text) — NOT loc.type(delay=N), which
        # samples one delay and repeats it for every key: a metronome, and a watched tell.
        pg = getattr(loc, "page", None)
        if pg is not None:
            humanize.type_text(pg, text)
        else:
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
    # Try the site's hint first, then fall back to the generic search-box selectors — a
    # too-narrow hint (e.g. craigslist's results-page box) must never block the default that
    # would have matched (the homepage's placeholder box).
    #
    # BUT WAIT FOR THE HINT FIRST. On an SPA the mapped box renders a beat after load, and
    # racing it straight into the generic fallback is how "macgregor sailboat" went into
    # Facebook's GLOBAL "Search Facebook" box instead of the Marketplace one — landing on
    # /search/top, an entirely different surface. The hints file warns that both boxes are
    # type="search"; the generic selector cannot tell them apart, so the only safe move is to
    # give the real one time to appear.
    default_sel = ", ".join(_SEARCH_BOX_SELECTORS)
    hint_sel = (hint or {}).get("search_box")
    strict = bool((hint or {}).get("search_box_strict"))

    box = None
    if hint_sel:
        try:
            page.wait_for_selector(hint_sel, timeout=5000, state="visible")
        except Exception:
            pass
        box = _first_visible(page, hint_sel)

    if box is None and strict:
        # Sites where the generic box does something ELSE entirely (Facebook's global search).
        # Typing into the wrong box is worse than not typing at all — the caller falls back to
        # the URL, which at least lands where the watch meant to be.
        log.info("type_search: %s's own search box hasn't appeared — refusing the generic box "
                 "(it searches something else); falling back", urlparse(page.url).netloc)
        return False

    if box is None:
        box = _first_visible(page, default_sel)
    if box is None:
        # the form may render just after load — wait briefly, then retry
        try:
            page.wait_for_selector(default_sel, timeout=4000, state="visible")
        except Exception:
            pass
        box = (_first_visible(page, hint_sel) if hint_sel else None) or \
              _first_visible(page, default_sel)
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
    page or locator. Returns True if clicked.

    ⚠ This called _human_click(page, …) while its only parameter is `scope` — an undefined
    name, so every call raised NameError into the bare except below and returned False. The
    confirm step of set_location has therefore NEVER clicked anything; it always fell through
    to its fallback label loop, which had the same bug. Found while mapping Facebook's
    location dialog, whose Apply is a div[role=button] (get_by_role matches ARIA roles, so it
    was never the element type that was wrong)."""
    try:
        b = scope.get_by_role("button", name=re.compile(re.escape(label), re.I))
        if b.count() > 0 and b.first.is_visible():
            _human_click(scope, b.first)
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

    # ALREADY THERE. Success is "the site is showing the right area", not "we changed
    # something" — and this returned False when the location was already correct, so a caller
    # treated a perfectly good state as a failure and fell back to a URL. Measured live on
    # Facebook: marker read "Location: Anacortes, Washington, Within 100 mi", the watch wanted
    # Anacortes, and set_location said False. Not touching a control that is already right is
    # also what a person does.
    #
    # A ZIP has to be recognised by the TOWN the site displays. The next live round of the
    # same bug: the control showed "Seattle" (set by the previous sweep), the watch wanted
    # 98121 — which IS Seattle — and "98121" is not a substring of "Seattle", so the picker
    # was reopened and re-set every sweep, and then reported failure because nothing changed.
    keys = [place.split(",")[0]]
    if re.fullmatch(r"\d{5}", place):
        try:
            from web_watcher.cl_geo import place_for_zip
            town = place_for_zip(place)
            if town:
                keys.append(town)
        except Exception:
            pass

    def _shows_target(marker: str) -> bool:
        # Letters only, and an EMPTY key never matches: "98121" strips to "" and `"" in hay`
        # is always True, which would call every location already-correct and never touch the
        # picker at all. (Caught by the test for a zip in a DIFFERENT town.)
        hay = re.sub(r"[^a-z]", "", (marker or "").lower())
        if not hay:
            return False
        for k in keys:
            kk = re.sub(r"[^a-z]", "", (k or "").lower())
            if kk and kk in hay:
                return True
        return False

    if _shows_target(before):
        log.info("Human location: already set to %r — leaving the control alone", place)
        return True

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
    # Success = the site is now showing the right area. Either the marker moved, OR it names
    # our target town (re-asserting an already-right location changes nothing and used to be
    # scored a failure — the caller then "fell back" from a state that was already correct).
    ok = _shows_target(after) or (bool(after) and after != before)
    if ok:
        log.info("Human location: set to %r via the page control", place)
    else:
        log.info("set_location: entered %r but couldn't confirm the location changed", place)
    return ok


def _location_marker(page) -> str:
    """A cheap signal of the page's current location, to detect a real change after setting it —
    the location button's own text (e.g. OfferUp's 'Hollywood: Maximum'), else a slice of body."""
    try:
        # Facebook's location control is a div[role=button] labelled "Location: Anacortes,
        # Washington, Within 100 mi" — a <button>-only selector found nothing, so a location
        # that HAD changed still reported unchanged and set_location returned False.
        t = page.evaluate(
            "() => { const b = document.querySelector("
            "'button[aria-label*=\"location\" i], [aria-label*=\"set my location\" i], "
            "[role=button][aria-label*=\"location\" i]');"
            " return b ? (b.getAttribute('aria-label')||b.innerText||'').trim() : ''; }")
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

    # POSTAL FIRST, and distance ONLY if the postal box actually exists. On craigslist a radius
    # is meaningless without a centre: typing "100" into miles with no zip applied filters
    # nothing, and submitting it just reloads the same page. Checking the VALUE isn't enough —
    # the box itself has to be there, because that's the case that failed live.
    postal_box = _first_visible(page, hint.get("postal", "")) if hint.get("postal") else None
    if req.zip and postal_box is None:
        log.info("inline filters: no postal box on this page — skipping distance too "
                 "(a radius with no centre filters nothing)")
    if req.zip and postal_box is not None:
        if _human_fill(postal_box, str(req.zip)):
            filled = True
            _fill("distance", req.radius or 50)
    _fill("price_min", req.price_min)
    _fill("price_max", req.price_max)
    if not filled:
        return False

    # ONE submit for ALL the fields, through the page's own Apply button. Pressing Enter after
    # each field is what made this look so unstable to watch — zip, Enter, nothing; miles, Enter,
    # reload — and each stray Enter can submit a half-filled form, so the filters land in stages
    # or not at all. Fill everything, apply once, then CHECK it took.
    _pause(0.2, 0.5)
    before = getattr(page, "url", "")
    applied = _click_selector(page, hint.get("apply", ""))
    if not applied:
        # No Apply button on this page — Enter in the last field is the fallback, used once.
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass
    try:
        page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass
    if not _wait_for_url_change(page, before, timeout_s=6.0):
        # craigslist swaps results in via the history API, so an unchanged URL is not proof of
        # failure — but it IS worth saying, because a silently-unapplied filter is the difference
        # between watching one town and watching a whole coast.
        log.info("inline filters: submitted but the URL didn't change — filters may not have "
                 "applied (page %s)", (before or "")[:70])
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
    # A watch with a CATEGORY is drivable only where we can CLICK that category the way a person
    # does; without the hint the URL path must handle it, or we'd browse the wrong section.
    # This deliberately does NOT exempt keyword watches: it used to read `not req.terms and
    # req.category and ...`, so a watch with a keyword AND a category passed the gate and then
    # dropped the category on the floor — cat=boo became cat=sss, and a MacGregor SAILBOAT watch
    # searched all of craigslist and found golf clubs. A dropped category is as wrong as a
    # dropped location or price, and is now refused on the same terms.
    if req.category and not hint.get("category_link"):
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

    # Category and keyword are INDEPENDENT, and the category goes FIRST — the way a person does
    # it: open Boats, then search within Boats. This used to be an either/or (`if terms ... elif
    # category`), so a watch that had BOTH — "MacGregor sailboat" in cat=boo — typed the keyword
    # and silently dropped the category, landing on cat=sss (all for sale). Searching all of
    # craigslist for "macgregor" is how a SAILBOAT watch filled up with MacGregor GOLF CLUBS.
    if req.category:
        applied["categorized"] = click_category(page, req.category, hint)
    if req.terms:
        applied["searched"] = type_search(page, req.terms, hint)

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
