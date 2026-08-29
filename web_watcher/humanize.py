"""One motor cortex for every mouse move and keystroke the app makes.

Why this module exists: the app grew TWO humanization stacks. The agent's (agent.py) was the
good one — bezier paths, log-normal timing, overshoot-and-correct. The human-first nav stack
(navigate._human_click, monitor.humanized_search, _human_fill) was the weak twin: 3-6
STRAIGHT-line steps regardless of distance, an approach that always came from the upper-left,
no overshoot ever, and typing via Playwright's `.type(delay=N)` — which samples the delay ONCE
and applies it to every keystroke. A metronome. The user watched a sweep and called both out:
"superhuman quick... hitting the right buttons and not overshooting at all... the same
movements and patterns over and over." Every one of those observations mapped to the weak twin.

So the good models live here now, shared by both stacks, with the tells addressed:

  PATTERN-REPEAT   cursor position is remembered PER PAGE (page._ww_mouse) so each approach
                   starts where the last action ended — not from a fixed offset; with no
                   memory the start is a random direction, not always up-and-left.
  NO OVERSHOOT     long approaches overshoot the target (scaled by distance) and correct
                   back with 1-2 micro-moves — Fitts's-law landing dynamics.
  METRONOME KEYS   every keystroke gets its own log-normal delay, spaces/capitals cost a
                   touch more, ~5% of keys pause to think, and occasionally a real typo is
                   made and backspaced (the final text is always exact — callers verify too).

The SAFETY invariant carried over from navigate.py, unchanged and load-bearing: paths and
timing are cosmetic; the click itself is ALWAYS delivered by Playwright's hover()+click()
actionability pipeline, never by raw coordinates. Hand-rolled mouse.down/up at remembered
coordinates silently missed craigslist's category link when the page reflowed — a click that
lands on nothing is worse than one that's a shade too tidy.

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  move          curved multi-segment approach with overshoot; per-page memory
  click         move near, hover (actionability), press with human delay
  type_text     per-keystroke timing + think-pauses + occasional typo/fix
  _TYPO_NEIGHBOURS   QWERTY adjacency used for realistic wrong keys
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import math
import random
import time

log = logging.getLogger(__name__)

# Where the cursor rests, remembered on the Page object itself so every driver (agent, nav,
# search) shares one continuous cursor history per tab.
_MOUSE_ATTR = "_ww_mouse"


def _cursor(page) -> tuple[float, float] | None:
    pos = getattr(page, _MOUSE_ATTR, None)
    return tuple(pos) if pos else None


def _remember(page, x: float, y: float) -> None:
    try:
        setattr(page, _MOUSE_ATTR, (x, y))
    except Exception:
        pass


def move(page, tx: float, ty: float) -> None:
    """Move the mouse to (tx, ty) along a human path: 2-4 bezier segments with independent
    curvature, smooth-step easing, decaying jitter, log-normal inter-event timing, then a
    distance-scaled overshoot corrected by 1-2 micro-moves. Ends EXACTLY on (tx, ty)."""
    start = _cursor(page)
    if start is None:
        # No history (fresh page): approach from a random direction, not a fixed corner.
        ang = random.uniform(0, 2 * math.pi)
        d = random.uniform(140, 420)
        start = (max(0.0, tx + math.cos(ang) * d), max(0.0, ty + math.sin(ang) * d))
    sx, sy = start

    dist = math.hypot(tx - sx, ty - sy)
    if dist < 2:
        _remember(page, tx, ty)
        return

    n_segs = random.randint(2, 4) if dist > 90 else 1
    waypoints = [(float(sx), float(sy))]
    for i in range(1, n_segs):
        frac = i / n_segs
        waypoints.append((sx + (tx - sx) * frac + random.gauss(0, dist * 0.06),
                          sy + (ty - sy) * frac + random.gauss(0, dist * 0.06)))
    waypoints.append((float(tx), float(ty)))

    for seg in range(n_segs):
        p0x, p0y = waypoints[seg]
        p1x, p1y = waypoints[seg + 1]
        seg_dist = math.hypot(p1x - p0x, p1y - p0y)
        steps = max(4, min(int(seg_dist / 10), 30))     # step count scales with distance

        # A bezier control point perpendicular to the segment — each pass curves differently.
        mid_x, mid_y = (p0x + p1x) / 2, (p0y + p1y) / 2
        perp_x, perp_y = -(p1y - p0y), (p1x - p0x)
        plen = math.hypot(perp_x, perp_y) or 1
        arc = random.gauss(0, 0.15)
        cp_x = mid_x + (perp_x / plen) * seg_dist * arc
        cp_y = mid_y + (perp_y / plen) * seg_dist * arc

        for i in range(1, steps + 1):
            t = i / steps
            te = t * t * (3 - 2 * t)                    # ease-in/out: slow-fast-slow
            bx = (1 - te) ** 2 * p0x + 2 * (1 - te) * te * cp_x + te ** 2 * p1x
            by = (1 - te) ** 2 * p0y + 2 * (1 - te) * te * cp_y + te ** 2 * p1y
            noise = max(0.15, 1 - t) * 1.5              # jitter fades as the hand settles
            page.mouse.move(bx + random.gauss(0, noise), by + random.gauss(0, noise))
            gap_ms = math.exp(random.gauss(math.log(12), 0.4))   # right-skewed like real hands
            time.sleep(max(4, min(gap_ms, 80)) / 1000)

        if seg < n_segs - 1:
            time.sleep(max(0.01, random.gauss(0.018, 0.006)))

    # Overshoot scaled to how far the hand travelled — a 30px nudge barely overshoots, a
    # cross-screen flick sails past — then settle back with micro-corrections.
    overshoot = max(0.0, random.gauss(2 + dist * 0.02, 2))
    if overshoot > 0.5:
        dx, dy = tx - sx, ty - sy
        dl = math.hypot(dx, dy) or 1
        page.mouse.move(tx + (dx / dl) * overshoot, ty + (dy / dl) * overshoot)
        time.sleep(max(0.01, random.gauss(0.055, 0.015)))
    for _ in range(random.randint(1, 2)):
        page.mouse.move(tx + random.gauss(0, 0.8), ty + random.gauss(0, 0.8))
        time.sleep(max(0.01, random.gauss(0.025, 0.008)))

    page.mouse.move(tx, ty)
    _remember(page, tx, ty)


def click(page, loc, timeout: int = 5_000) -> bool:
    """Approach with move(), land with Playwright. The approach aims slightly off the element's
    dead center (people don't click centroids); hover() then re-verifies actionability and the
    press carries a human hold time. Falls back to a plain click when there's no page or no
    stable box. Returns True if a click was delivered."""
    try:
        loc.scroll_into_view_if_needed(timeout=timeout)
    except Exception:
        pass
    box = None
    try:
        box = loc.bounding_box()
    except Exception:
        box = None
    if page is None or not box or box.get("width", 0) < 1 or box.get("height", 0) < 1:
        try:
            loc.click(timeout=timeout)
            return True
        except Exception:
            return False

    try:
        # Aim inside the middle ~60% of the box, off-center.
        tx = box["x"] + box["width"] * random.uniform(0.30, 0.70)
        ty = box["y"] + box["height"] * random.uniform(0.32, 0.68)
        move(page, tx, ty)

        # Sometimes a person pauses to read the label before committing.
        if random.random() < 0.15:
            time.sleep(random.uniform(0.15, 0.5))

        # LAND VIA PLAYWRIGHT (see module docstring — reflow safety is load-bearing).
        loc.hover(timeout=timeout)
        try:
            hb = loc.bounding_box()
            if hb:
                _remember(page, hb["x"] + hb["width"] / 2, hb["y"] + hb["height"] / 2)
        except Exception:
            pass
        time.sleep(max(0.06, random.gauss(0.20, 0.05)))     # hover dwell before pressing
        loc.click(timeout=timeout, delay=random.randint(60, 130))
        return True
    except Exception:
        try:
            loc.click(timeout=timeout)
            return True
        except Exception:
            return False


# QWERTY neighbours for believable typos — a slipped finger hits an ADJACENT key, not a
# random one. Letters only; anything absent simply never fat-fingers.
_TYPO_NEIGHBOURS = {
    "q": "wa", "w": "qes", "e": "wrd", "r": "etf", "t": "ryg", "y": "tuh", "u": "yij",
    "i": "uok", "o": "ipl", "p": "ol", "a": "qsz", "s": "awdx", "d": "sefc", "f": "drgv",
    "g": "fthb", "h": "gyjn", "j": "hukm", "k": "jil", "l": "kop", "z": "asx", "x": "zsdc",
    "c": "xdfv", "v": "cfgb", "b": "vghn", "n": "bhjm", "m": "njk",
}


def _key_delay(ch: str, prev: str | None) -> float:
    """One keystroke's delay: log-normal base, nudged by what's being typed."""
    d = math.exp(random.gauss(math.log(0.085), 0.35))       # median ~85ms, long right tail
    if ch == " " or not ch.isalnum():
        d += abs(random.gauss(0.03, 0.015))                 # reaching for space/punctuation
    if ch.isupper():
        d += abs(random.gauss(0.04, 0.02))                  # shift chord
    if prev is not None and prev == ch:
        d *= random.uniform(0.55, 0.8)                      # double letters rattle out faster
    return min(max(d, 0.03), 0.4)


def type_text(page, text: str) -> None:
    """Type into the FOCUSED element, one key at a time, with per-keystroke timing (never the
    constant-delay metronome), ~5% think-pauses, and at most one adjacent-key typo that gets
    noticed, backspaced, and corrected. The final text is always exactly `text` — and callers
    keep their own verify-and-fill safety net for boxes that swallow keystrokes."""
    typo_at = -1
    if len(text) >= 6 and random.random() < 0.04:
        candidates = [i for i, c in enumerate(text) if c.lower() in _TYPO_NEIGHBOURS
                      and 1 <= i < len(text) - 1]
        if candidates:
            typo_at = random.choice(candidates)

    prev = None
    for i, ch in enumerate(text):
        if i == typo_at:
            wrong = random.choice(_TYPO_NEIGHBOURS[ch.lower()])
            page.keyboard.type(wrong if ch.islower() else wrong.upper())
            time.sleep(random.uniform(0.12, 0.3))           # ...notice it
            page.keyboard.press("Backspace")
            time.sleep(random.uniform(0.05, 0.15))
        page.keyboard.type(ch)
        delay = _key_delay(ch, prev)
        if random.random() < 0.05:
            delay += random.uniform(0.15, 0.45)             # glance up / think
        time.sleep(delay)
        prev = ch
