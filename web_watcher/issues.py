"""The sweep-issue log — a structured, aggregated record of what goes WRONG while watching.

This is the browsing/judging counterpart to llm.py's escalation log, and it exists for the same
reason: the raw rotating .log captures everything once and scrolls away, so a recurring problem —
a watch that keeps getting stuck, a site that keeps challenging us, a judge that keeps having to
undo its own false positives — is invisible until someone happens to be reading the log at the
right moment. Aggregated here, "what is failing, on which watch, how often" becomes a question you
can answer.

Each entry is one issue: a kind, the watch it happened on, a short human detail, and when. The
KINDS are deliberately a small, fixed vocabulary so the summary means something:

  stuck            the agent hit its get-unstuck council on a page (repeated same action)
  no_listings      a sweep completed but harvested nothing (extractor blind, or an empty feed)
  forced_scroll    the setup budget had to scroll FOR the agent (it kept re-fiddling controls)
  search_lock      the agent tried to navigate away from the driven search and was refused
  false_positive   the judge's verify pass REMOVED a listing the batch pass had wrongly kept
  challenge        a site showed a CAPTCHA/checkpoint and we backed off
  nav_failed       navigation to the sweep's start URL failed
  blind_escalation the scraper saw nothing so we escalated the site to the agent
  cloud_ladder_futile local + every cloud rung failed one validation check — paid for nothing
                   (when this recurs, the VALIDATOR is probably wrong, not the models)

Like the escalation log, this is DELIBERATELY NOT wired back into any automated decision. It is a
record for a person to read and act on — pull a better term list, fix a selector, rest a site —
not a controller. Feeding it back into routing would quietly rebuild the guess-first design the
rest of the app was careful to avoid.

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  record_issue     append one issue (best-effort; never raises into a sweep)
  issues           the most recent issues, newest first
  issue_summary    aggregated counts by kind / by watch — the "what's failing" answer
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

# The fixed vocabulary. A record with any other kind is still stored, but keeping callers to this
# set is what makes the by-kind summary legible instead of a pile of one-off strings.
KINDS = frozenset({
    "stuck", "no_listings", "forced_scroll", "search_lock", "false_positive",
    "challenge", "nav_failed", "blind_escalation", "cloud_ladder_futile",
})

_FILENAME = "sweep_issues.jsonl"
_KEEP = 500                      # newest N; a debug record must not grow without bound
_lock = threading.Lock()


def _path(data_dir=None) -> Path:
    if data_dir is not None:
        return Path(data_dir) / _FILENAME
    from web_watcher import paths
    return paths.data_dir() / _FILENAME


def record_issue(kind: str, watch: str = "", detail: str = "", data_dir=None) -> None:
    """Append one sweep issue. Best-effort — logging a problem must never CAUSE one."""
    entry = {
        "ts": time.time(),
        "kind": str(kind or "other")[:40],
        "watch": str(watch or "")[:120],
        "detail": str(detail or "")[:300],
    }
    try:
        p = _path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            lines = []
            if p.exists():
                lines = p.read_text(encoding="utf-8").splitlines()[-(_KEEP - 1):]
            lines.append(json.dumps(entry, ensure_ascii=False))
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as exc:
        log.debug("could not record sweep issue: %s", exc)


def issues(limit: int = 100, data_dir=None) -> list[dict]:
    """The most recent issues, newest first."""
    try:
        p = _path(data_dir)
        if not p.exists():
            return []
        rows = []
        for line in p.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
        return list(reversed(rows))
    except Exception:
        return []


def issue_summary(data_dir=None) -> dict:
    """Aggregated: what is failing, and which watches are struggling most.

    The point of aggregation is that ONE stuck event is noise and forty on the same watch is a
    watch to fix. Returns totals plus by-kind and by-watch counts (worst first), over the retained
    window."""
    rows = issues(_KEEP, data_dir)
    by_kind: dict[str, int] = {}
    by_watch: dict[str, int] = {}
    for r in rows:
        k = r.get("kind", "other")
        by_kind[k] = by_kind.get(k, 0) + 1
        w = r.get("watch") or "(none)"
        by_watch[w] = by_watch.get(w, 0) + 1
    return {
        "total": len(rows),
        "by_kind": dict(sorted(by_kind.items(), key=lambda kv: kv[1], reverse=True)),
        "by_watch": dict(sorted(by_watch.items(), key=lambda kv: kv[1], reverse=True)[:20]),
        "recent": rows[:30],
    }
