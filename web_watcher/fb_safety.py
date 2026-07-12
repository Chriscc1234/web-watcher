"""
Facebook safety harness — the guardrails that keep an automated browser from getting the
user's (or their buddy's) Facebook account restricted or banned.

The buddy's throwaway account is one he actually USES, so the rules here are strict and
conservative. This module is pure, side-effect-free logic (easy to unit-test); the agent
loop and the continuous scheduler enforce it at the two points that matter:

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

Pacing (a per-session action cap + longer idles for Facebook watches) is applied by the
caller using `SESSION_ACTION_CAP` / `is_facebook`.

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  is_facebook            host check
  is_blocked_action      visible-label → is this a social/transactional action to block?
  is_checkpoint          page → is this a security checkpoint / block we must STOP on?
  SESSION_ACTION_CAP     max agent actions per Facebook sweep (pacing)
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

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
