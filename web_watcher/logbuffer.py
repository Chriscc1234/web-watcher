"""
In-memory log ring buffer + categorizer for the live Activity tab.

The dashboard's Activity view is a real-time, filterable feed of what the app is actually
doing — sweeps, AI ratings, alerts, errors, skips. Rather than tail the on-disk session
log (encoding/locking/rotation headaches, and it holds third-party noise), we attach ONE
lightweight handler to the root logger that keeps the most recent N records in memory with
a monotonic sequence number, so the UI can poll `GET /api/logs?after=<seq>` and stream only
what's new. Each record is tagged with a CATEGORY (search / ai / alert / skipped / error /
login / system) and, when detectable, the watch name — so the UI's filter chips work without
the UI having to parse free-text log lines.

Design notes:
  • Bounded (collections.deque(maxlen)) → never grows without limit.
  • Thread-safe (a lock around append/snapshot) → the scheduler/agent/oversight threads all
    log concurrently.
  • Categorization is a first-match-wins rule list over (level, logger, message). Errors win
    over everything so a failure is never mis-filed as a routine search.
  • Zero third-party noise: httpx/apscheduler etc. are already quieted to WARNING in main;
    we additionally skip records from those loggers so the feed stays about OUR activity.

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  RingHandler        the logging.Handler installed on the root logger
  install()          idempotent: attach the handler once, return the shared ring
  LogRing.snapshot   filtered/paginated read for GET /api/logs
  _categorize        (level, logger, msg) → category string
  _watch_of          best-effort watch-name extraction from a message
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import re
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Optional

_MAXLEN = 600  # ~ the last few minutes of activity at a busy sweep cadence

# Loggers whose records never belong in the Activity feed (HTTP plumbing, schedulers).
_SKIP_LOGGERS = ("httpx", "httpcore", "urllib3", "apscheduler", "uvicorn", "asyncio",
                 "web_watcher.logbuffer")

# Category rules: (category, compiled-pattern-over-lowercased-message). First match wins.
# Order matters — more specific/severe categories come first.
_CATEGORY_RULES: list[tuple[str, re.Pattern]] = [
    ("login",   re.compile(r"login wall|logged out|reconnect|sign in|session expired|checkpoint")),
    ("alert",   re.compile(r"\balert|notif|new match|sent to|pushed|telegram|email")),
    ("skipped", re.compile(r"reject|excluded by keyword|no required keyword|skipp|baselin|"
                           r"primed|no listings|dedup|already seen|duplicate|repost")),
    ("ai",      re.compile(r"rating judge|judgment|judge|agent action|agent step|council|"
                           r"get-unstuck|rated|expand|vision|perception")),
    ("search",  re.compile(r"sweep|search|harvest|extract|scroll|navigat|scheduled watch|"
                           r"exploring|browsing|continuous")),
]

_WATCH_RE = re.compile(r"(?:for|watch|of|'s)\s+[\"']([^\"']{1,60})[\"']", re.I)


def _categorize(levelno: int, logger_name: str, msg: str) -> str:
    if levelno >= logging.ERROR:
        return "error"
    low = msg.lower()
    # A WARNING that reads like a failure is an error to the user's eye.
    if levelno >= logging.WARNING and re.search(r"fail|error|could not|couldn't|unable|timeout", low):
        return "error"
    for cat, pat in _CATEGORY_RULES:
        if pat.search(low):
            return cat
    return "system"


def _watch_of(msg: str) -> str:
    m = _WATCH_RE.search(msg)
    return m.group(1) if m else ""


class LogRing:
    """The shared bounded store of recent categorized log records."""

    def __init__(self, maxlen: int = _MAXLEN) -> None:
        self._buf: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0

    def add(self, levelno: int, level: str, logger_name: str, msg: str) -> None:
        entry = {
            "ts":       datetime.now(timezone.utc).isoformat(),
            "level":    level,
            "category": _categorize(levelno, logger_name, msg),
            "watch":    _watch_of(msg),
            "logger":   logger_name.replace("web_watcher.", ""),
            "message":  msg,
        }
        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            self._buf.append(entry)

    def snapshot(self, after: int = 0, category: Optional[str] = None,
                 watch: Optional[str] = None, text: Optional[str] = None,
                 limit: int = 300) -> dict:
        """Return records with seq > `after`, optionally filtered. `last_seq` lets the
        client poll incrementally (pass it back as `after`)."""
        cat  = (category or "").strip().lower() or None
        wl   = (watch or "").strip().lower() or None
        tl   = (text or "").strip().lower() or None
        with self._lock:
            rows = [e for e in self._buf if e["seq"] > after]
            last_seq = self._seq
        out = []
        for e in rows:
            if cat and e["category"] != cat:
                continue
            if wl and wl not in (e["watch"] or "").lower():
                continue
            if tl and tl not in e["message"].lower():
                continue
            out.append(e)
        if len(out) > limit:
            out = out[-limit:]
        return {"entries": out, "last_seq": last_seq}


class RingHandler(logging.Handler):
    def __init__(self, ring: LogRing) -> None:
        super().__init__(level=logging.INFO)
        self._ring = ring

    def emit(self, record: logging.LogRecord) -> None:
        name = record.name or ""
        if any(name == s or name.startswith(s + ".") or name.startswith(s) for s in _SKIP_LOGGERS):
            return
        try:
            self._ring.add(record.levelno, record.levelname, name, record.getMessage())
        except Exception:
            pass   # logging must never raise


_ring: Optional[LogRing] = None
_installed = False


def get_ring() -> LogRing:
    """The process-wide ring (created on first use). Reading is safe even before install()."""
    global _ring
    if _ring is None:
        _ring = LogRing()
    return _ring


def install() -> LogRing:
    """Attach the ring handler to the root logger exactly once. Returns the shared ring."""
    global _installed
    ring = get_ring()
    if not _installed:
        logging.getLogger().addHandler(RingHandler(ring))
        _installed = True
    return ring
