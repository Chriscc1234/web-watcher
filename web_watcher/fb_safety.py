"""
Facebook safety harness — the guardrails that keep an automated browser from getting the
user's (or their buddy's) Facebook account restricted or banned.

The buddy's throwaway account is one he actually USES, so the rules here are strict and
conservative. The agent loop and the continuous scheduler enforce it at the points that
matter:

  1. READ-ONLY, ALWAYS. On Facebook the agent may ONLY read/scroll/search/filter. It must
     NEVER take a social or transactional action — message a seller, make an offer, buy,
     like, comment, share, post, save, follow, add a friend, report, mark sold, delete.
     `is_blocked_action(label)` catches those by the control's visible text; the agent
     rejects the click before it happens. (Credentials are already a hard no elsewhere.)

  2. STOP-DON'T-SOLVE on a checkpoint. If Facebook throws a security checkpoint, identity
     confirmation, "unusual activity", CAPTCHA, or a temporary block, we STOP immediately,
     alert the user, and BACK OFF — we never try to click through or solve it (that's what
     escalates a soft flag into a ban). `is_checkpoint(page)` detects it; the sweep bails
     and records a cooldown so we don't hammer a flagged account.

  3. THE HALT (emergency brake). A checkpoint doesn't just pause ONE watch for a few hours —
     it stops ALL Facebook activity, app-wide, and stays stopped until a HUMAN clears it.
     An auto-expiring per-watch cooldown was too weak for an account we can't afford to
     lose: it resumed on its own, left other FB watches poking the same flagged account,
     and forgot everything on restart. The halt is persisted to disk so an app restart
     cannot silently resume. `engage_halt` / `halt_state` / `clear_halt`.

Pacing (a per-session action cap + longer idles for Facebook watches) is applied by the
caller using `SESSION_ACTION_CAP` / `is_facebook`.

Everything here is pure logic EXCEPT the halt section, which reads/writes one small JSON
file under the user's data dir (that persistence is the entire point of it).

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  is_facebook            host check
  is_blocked_action      visible-label → is this a social/transactional action to block?
  is_checkpoint          page → is this a security checkpoint / block we must STOP on?
  SESSION_ACTION_CAP     max agent actions per Facebook sweep (pacing)
  engage_halt/halt_state/clear_halt ~L140  The global, human-cleared emergency brake
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# A Facebook sweep is capped to this many agent actions, then it wraps up — far fewer than
# a human session, but enough to scroll/sort/filter a feed. Low ceilings look less botlike
# and bound the blast radius if something goes wrong.
SESSION_ACTION_CAP = 12


def is_facebook(url: str) -> bool:
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return False
    return host.endswith("facebook.com") or host.endswith("fb.com")


# Visible-text patterns for controls the agent must NEVER activate on Facebook. Matched with
# word boundaries against the element's label, case-insensitively. Ordered roughly by risk.
_BLOCKED_ACTION_RE = re.compile(
    r"\b("
    r"message|send\s*message|send|contact\s*seller|"          # messaging a seller
    r"make\s*(an\s*)?offer|buy(\s*now)?|add\s*to\s*cart|check\s*out|checkout|place\s*order|pay\b|"  # buying
    r"like|react|love|comment|repl(y|ies)|share|"             # social reactions
    r"post|publish|create\s*(new\s*)?listing|sell\s*something|"  # posting/selling
    r"save\b|add\s*to\s*(collection|favorites?)|"             # saving (leaves a trace)
    r"follow|add\s*friend|friend\s*request|join\s*group|"     # social graph
    r"report|block\b|"                                        # reporting/blocking
    r"mark\s*as\s*sold|delete|remove\s*listing"               # seller-side mutations
    r")\b",
    re.I,
)

# Controls that CONTAIN a blocked word but are actually safe read-only navigation — never
# block these (avoid over-blocking legit browsing). Checked first.
_ALLOW_RE = re.compile(
    r"\b("
    r"see\s*more|show\s*more|view\s*more|more\s*(like\s*this|results?|options?|filters?)|"
    r"see\s*all|view\s*all|load\s*more|"
    r"marketplace|category|categories|search|filter|sort|price|condition|date\s*listed|"
    r"newest|nearest|distance|relevance|"
    r"messages?\s*·|messenger\s*·"    # a nav LABEL mentioning messages, not the Message button
    r")\b",
    re.I,
)


def is_blocked_action(label: str) -> bool:
    """True if clicking a control with this visible label would take a social/transactional
    action on Facebook (message, offer, buy, like, comment, post, save, follow…). Read-only
    navigation that merely contains a keyword ('See more', 'Sort', 'Marketplace') is allowed."""
    if not label:
        return False
    text = " ".join(label.split()).strip()
    if _ALLOW_RE.search(text):
        return False
    return bool(_BLOCKED_ACTION_RE.search(text))


# A REAL security checkpoint / block — distinct from an ordinary logged-out login wall
# (that's handled by monitor.is_login_wall). These mean the account is FLAGGED; we must
# stop and let the human deal with it, never automate through it.
_CHECKPOINT_URL_RE = re.compile(r"/checkpoint|/confirm|/disabled|/help/contact", re.I)
_CHECKPOINT_TEXT_RE = re.compile(
    r"we('| ha)ve (temporarily )?(restricted|limited|disabled|locked)|"
    r"temporarily blocked|you'?re temporarily blocked|"
    r"confirm your identity|confirm it'?s you|verify (your|it'?s you)|"
    r"unusual activity|suspicious activity|we noticed|"
    r"security check|are you a robot|prove you'?re (a )?human|"
    r"enter the (code|characters)|complete this security check|"
    r"your account has been (disabled|restricted)|action blocked|"
    r"you can'?t use this feature",
    re.I,
)


def is_checkpoint(page) -> bool:
    """True if the page is a Facebook security checkpoint / block / CAPTCHA / identity
    challenge — the STOP-AND-ALERT signal. Conservative: needs a clear checkpoint cue,
    not just any occurrence of the word 'blocked' in unrelated content."""
    try:
        url = getattr(page, "url", "") or ""
        if _CHECKPOINT_URL_RE.search(urlparse(url).path or ""):
            return True
        try:
            body = page.inner_text("body", timeout=2_000)[:3000]
        except Exception:
            body = ""
        return bool(_CHECKPOINT_TEXT_RE.search(body))
    except Exception:
        return False


def checkpoint_reason(page) -> str:
    """A short human phrase describing the checkpoint, for the user's alert."""
    try:
        body = page.inner_text("body", timeout=1_500)[:1500]
    except Exception:
        body = ""
    m = _CHECKPOINT_TEXT_RE.search(body or "")
    return (m.group(0).strip().capitalize() if m else "Facebook security checkpoint")


# ---------------------------------------------------------------------------
# The halt — a global, persistent, human-cleared emergency brake
# ---------------------------------------------------------------------------
#
# When Facebook shows a security checkpoint we stop ALL Facebook activity, not just the
# watch that tripped it: the flag is on the ACCOUNT, so letting a second watch keep browsing
# is exactly how a soft flag becomes a ban. It persists to disk so restarting the app cannot
# silently resume, and only a person can clear it — deliberately, after checking the account.

_HALT_FILENAME = "fb_halt.json"


def _halt_path(data_dir: Path | None = None) -> Path:
    if data_dir is not None:
        return Path(data_dir) / _HALT_FILENAME
    from web_watcher import paths
    return paths.data_dir() / _HALT_FILENAME


def engage_halt(reason: str, watch_name: str = "", data_dir: Path | None = None) -> dict:
    """Stop all Facebook activity until a human clears it. Idempotent: re-tripping keeps the
    ORIGINAL reason/time (the first checkpoint is the informative one) and counts the hits."""
    path = _halt_path(data_dir)
    state = halt_state(data_dir) or {}
    if state:
        state["hits"] = int(state.get("hits", 1)) + 1
        state["last_at"] = time.time()
    else:
        state = {"reason": reason or "Facebook security checkpoint",
                 "watch": watch_name, "at": time.time(), "last_at": time.time(), "hits": 1}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        log.warning("FACEBOOK HALTED (%s) — all Facebook activity stopped until cleared by hand",
                    state["reason"])
    except Exception as exc:
        # Even if we can't persist, the caller still stops this sweep.
        log.error("could not persist the Facebook halt: %s", exc)
    return state


def halt_state(data_dir: Path | None = None) -> dict | None:
    """The active halt, or None. Never raises — an unreadable file is treated as NOT halted
    so a corrupt file can't permanently wedge the app."""
    try:
        path = _halt_path(data_dir)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("reason") else None
    except Exception:
        return None


def is_halted(data_dir: Path | None = None) -> bool:
    return halt_state(data_dir) is not None


def clear_halt(data_dir: Path | None = None) -> bool:
    """Human says the account is healthy again. Returns True if a halt was actually cleared."""
    try:
        path = _halt_path(data_dir)
        if not path.exists():
            return False
        path.unlink()
        log.info("Facebook halt cleared by the user — Facebook watches may run again")
        return True
    except Exception as exc:
        log.error("could not clear the Facebook halt: %s", exc)
        return False


# ---------------------------------------------------------------------------
# The site-agnostic read-only rule
# ---------------------------------------------------------------------------
#
# Facebook's guard was written first because the stakes there are an account ban, but the
# principle isn't Facebook's: A WATCH ONLY EVER READS. Every other site was left unguarded, and
# craigslist showed why — the agent clicked "hide posting", which removes a listing from the feed.
# A watch that can hide its own results quietly shrinks what it will ever find, and "favourite",
# "flag" and "delete" are all sitting on the same card.
#
# Deliberately narrower than the Facebook list: no social graph, no reactions — just the controls
# that CHANGE something. Anything that merely reveals more of the page stays allowed, because
# over-blocking here would stop the agent browsing at all.
_MUTATING_ACTION_RE = re.compile(
    r"\b("
    r"hide|unhide|restore\s*(this|post)|banish|"                  # craigslist hide/restore
    r"flag(ged)?(\s*as)?|report\b|block\b|"                       # flagging/reporting
    r"favou?rite|add\s*to\s*(favou?rites?|list|cart|watchlist)|save\s*(this|post|search|item)|"
    r"delete|remove\s*(this|post|listing|item)|renew|repost|edit\s*(this|post|listing)|"
    r"buy(\s*(it\s*)?now)?|check\s*out|place\s*(an?\s*)?(order|bid)|bid\b|make\s*(an?\s*)?offer|"
    r"contact\s*(the\s*)?(seller|poster)|reply\b|send\s*(a\s*)?(message|email)|message\b|"
    r"subscribe|sign\s*up|register|create\s*(an?\s*)?account|"
    r"post\s*(an?\s*)?(ad|listing)|publish"
    r")\b",
    re.I,
)

# Controls that contain a blocked word but only reveal more of the page. Checked first.
_READONLY_ALLOW_RE = re.compile(
    r"\b("
    r"see\s*more|show\s*more|view\s*more|load\s*more|see\s*all|view\s*all|next\s*page|"
    r"saved\s*searches?|show\s*hidden|hidden\s*posts?|"     # navigating TO saved/hidden, not saving
    # NB: no bare "search" here — it rescued "save this search", which creates a saved search on
    # the account. Labels that are only a search box carry no mutating word and need no rescue.
    r"search\s*(results?|craigslist|for)|filter|sort|price|newest|nearest|relevance|distance|"
    r"condition|category|categories|"
    r"gallery|list\s*view|map\s*view|thumb|photos?"
    r")\b",
    re.I,
)


def is_mutating_action(label: str) -> bool:
    """True if a control with this visible label would CHANGE something rather than just show it.

    Applies on every site. A watch reads; it does not hide, flag, favourite, save, delete, buy,
    bid, message, or post. Navigation that merely contains a keyword ('Show more', 'Sort',
    'Saved searches') is allowed — a guard that stops the agent browsing is worse than no guard."""
    if not label:
        return False
    text = " ".join(label.split()).strip()
    if _READONLY_ALLOW_RE.search(text):
        return False
    return bool(_MUTATING_ACTION_RE.search(text))
