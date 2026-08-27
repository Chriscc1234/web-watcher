"""Per-site challenge cooldown — back off from a site that just challenged us.

KEY LOCATIONS
  _cool_path()        ~L40   where the cooldown state lives (data_dir/site_cooldowns.json)
  record_challenge()  ~L60   a challenge appeared: note it, lengthen the backoff
  cooling_for()       ~L95   seconds still to wait for a host (0 = clear to go)
  clear_site()        ~L120  human says it's fine again / a clean sweep landed

WHY THIS EXISTS
A site showing a CAPTCHA is telling us, in the only language it has, "you're going too fast /
you look automated". The previous behaviour when a challenge could NOT be cleared was to convene
a recovery council and keep poking the same challenged page, then come back on the next sweep
sixty seconds later. That is the single most bot-like thing the app could do: a person who fails
a check wanders off for a while, they do not retry it eight times a minute forever.

So: when a site challenges us and we do not get through, that host goes on a cooldown that
DOUBLES each consecutive time (30m → 1h → 2h → 4h, capped at 8h) and the user is told. Other
watches keep running; only the host that complained is rested. A sweep that completes cleanly
clears the streak, so one bad afternoon doesn't punish a site forever.

This is a back-off, not a bypass: it makes the app do LESS when a site pushes back. See also
fb_safety, which is the stricter, human-cleared-only version of the same idea for Facebook.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_FILENAME = "site_cooldowns.json"

# Backoff ladder. First challenge rests the site for 30 minutes; each consecutive one doubles.
_BASE_COOLDOWN_S = 30 * 60
_MAX_COOLDOWN_S = 8 * 60 * 60


def host_of(url: str) -> str:
    """The registrable-ish host a cooldown is keyed by. Empty string for junk input."""
    try:
        net = (urlparse(url or "").netloc or "").lower()
    except Exception:
        return ""
    if not net:
        return ""
    if net.startswith("www."):
        net = net[4:]
    parts = net.split(".")
    # Key on the last two labels so skagit.craigslist.org and www.craigslist.org rest together —
    # a challenge from one subdomain means the SITE is unhappy, not just that hostname.
    return ".".join(parts[-2:]) if len(parts) >= 2 else net


def _cool_path(data_dir: Path | None = None) -> Path:
    if data_dir is not None:
        return Path(data_dir) / _FILENAME
    from web_watcher import paths
    return paths.data_dir() / _FILENAME


def _load(data_dir: Path | None = None) -> dict:
    """All cooldown state. Never raises — unreadable state means 'nothing is cooling', so a
    corrupt file can never permanently wedge every watch."""
    try:
        path = _cool_path(data_dir)
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(state: dict, data_dir: Path | None = None) -> None:
    try:
        path = _cool_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as exc:
        log.error("could not persist site cooldowns: %s", exc)


def record_challenge(url: str, reason: str = "", solved: bool = False,
                     watch_name: str = "", data_dir: Path | None = None) -> dict:
    """Note that `url`'s host challenged us, and put it to sleep for a while.

    `solved=True` (we got through) still records the event and still rests the site briefly —
    being challenged at all is the site telling us we look automated, and charging on at full
    speed is what turns one challenge into a block. An unsolved challenge escalates harder.
    Returns the host's new cooldown record."""
    host = host_of(url)
    if not host:
        return {}
    state = _load(data_dir)
    rec = state.get(host) or {}
    streak = int(rec.get("streak", 0)) + 1
    # A cleared challenge is a lighter touch than one that stopped us dead.
    span = _BASE_COOLDOWN_S * (2 ** (streak - 1))
    if solved:
        span = max(5 * 60, span // 4)
    span = min(int(span), _MAX_COOLDOWN_S)
    now = time.time()
    rec = {
        "host": host,
        "streak": streak,
        "until": now + span,
        "at": now,
        "reason": reason or "challenge shown",
        "solved": bool(solved),
        "watch": watch_name,
    }
    state[host] = rec
    _save(state, data_dir)
    log.warning("Site cooldown: %s challenged us (%s, solved=%s) — resting it for %d minute(s)",
                host, rec["reason"], solved, span // 60)
    return rec


def cooling_for(url: str, data_dir: Path | None = None) -> int:
    """Seconds still to wait before touching this host again. 0 means go ahead."""
    host = host_of(url)
    if not host:
        return 0
    rec = _load(data_dir).get(host) or {}
    try:
        left = int(float(rec.get("until", 0)) - time.time())
    except (TypeError, ValueError):
        return 0
    return max(0, left)


def is_cooling(url: str, data_dir: Path | None = None) -> bool:
    return cooling_for(url, data_dir) > 0


def note_clean_sweep(url: str, data_dir: Path | None = None) -> None:
    """A sweep finished without a challenge — forget the streak so one bad spell doesn't
    permanently inflate the backoff. Leaves an active cooldown alone."""
    host = host_of(url)
    if not host:
        return
    state = _load(data_dir)
    rec = state.get(host)
    if rec and not rec.get("streak"):
        return
    if rec and int(float(rec.get("until", 0))) <= time.time():
        state.pop(host, None)
        _save(state, data_dir)


def clear_site(url_or_host: str, data_dir: Path | None = None) -> bool:
    """Human says this site is fine again. Returns True if something was actually cleared."""
    host = host_of(url_or_host) or (url_or_host or "").strip().lower()
    if not host:
        return False
    state = _load(data_dir)
    if host in state:
        state.pop(host, None)
        _save(state, data_dir)
        log.info("Site cooldown cleared for %s", host)
        return True
    return False


def active(data_dir: Path | None = None) -> list[dict]:
    """Every host currently resting, soonest-to-wake first — for the UI and the bot."""
    now = time.time()
    out = [r for r in _load(data_dir).values()
           if isinstance(r, dict) and float(r.get("until", 0)) > now]
    return sorted(out, key=lambda r: float(r.get("until", 0)))
