"""The Market Map — what's actually out there, from everything every watch has ever seen.

The user's idea, verbatim: "do we keep a master list/learned list of all the vehicles and
items we search for? a kind of history map, so things can be compared... perhaps knowing
what kind of particular vehicle is on the market in reality? we can keep details on things
even if they aren't what we're looking for. matches should take priority though."

The storage layer already lives by that rule: EVERY listing any sweep ever saw is in the
global `listings` table with parsed attributes (price, year, mileage, transmission), and
`observations` records which watch saw it and how it was judged. This module is the missing
VIEW: aggregate reality — how many, what they cost, how old, where they're posted, how
fresh — for any query or any watch, matches first.

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  market_summary   one query/watch → counts, price percentiles, year spread, sources
  _sample          representative rows, MATCHED FIRST, then newest
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def _pct(values: list, p: float):
    if not values:
        return None
    values = sorted(values)
    i = min(len(values) - 1, max(0, int(round(p * (len(values) - 1)))))
    return values[i]


def market_summary(q: str = "", watch_id: str = "", limit_sample: int = 12,
                   db_path: Path | None = None) -> dict:
    """Aggregate the recorded market for a free-text query (title match, every word must
    appear) or for one watch's observations. Numbers over everything ever seen; the sample
    puts MATCHES first — the user's priority rule — then the freshest of the rest."""
    from web_watcher.storage import _connect, _resolve
    conn = _connect(_resolve(db_path))
    try:
        params: list = []
        if watch_id:
            base = ("FROM listings l JOIN observations o ON o.listing_key = l.listing_key "
                    "WHERE o.watch_id = ?")
            params.append(watch_id)
        else:
            base = "FROM listings l WHERE 1=1"
            for w in (q or "").split():
                base += " AND l.title LIKE ? COLLATE NOCASE"
                params.append(f"%{w}%")

        rows = conn.execute(
            f"SELECT l.listing_key, l.source, l.title, l.url, l.price_value, l.year, "
            f"l.mileage, l.transmission, l.first_seen, l.last_seen, l.image "
            f"{base}", params).fetchall()
        rows = [dict(r) for r in rows]

        # Which of these are MATCHES (for anyone) — priority rows for the sample.
        matched_keys = set()
        if rows:
            keys = [r["listing_key"] for r in rows]
            CH = 500
            for i in range(0, len(keys), CH):
                chunk = keys[i:i + CH]
                qmarks = ",".join("?" * len(chunk))
                for r in conn.execute(
                        f"SELECT DISTINCT listing_key FROM observations "
                        f"WHERE matched=1 AND COALESCE(excluded,0)=0 "
                        f"AND listing_key IN ({qmarks})", chunk):
                    matched_keys.add(r["listing_key"])

        from web_watcher.cl_geo import is_placeholder_price
        prices = [r["price_value"] for r in rows
                  if isinstance(r["price_value"], (int, float)) and r["price_value"] > 0
                  and not is_placeholder_price(r["price_value"])]
        years = [r["year"] for r in rows if isinstance(r["year"], int) and 1900 < r["year"] < 2030]
        by_source: dict = {}
        by_trans: dict = {}
        for r in rows:
            src = (r["source"] or "?").replace("www.", "")
            by_source[src] = by_source.get(src, 0) + 1
            t = (r["transmission"] or "").lower()
            if t:
                by_trans[t] = by_trans.get(t, 0) + 1
        by_year: dict = {}
        for y in years:
            decade = f"{(y // 10) * 10}s"
            by_year[decade] = by_year.get(decade, 0) + 1

        rows.sort(key=lambda r: ((r["listing_key"] not in matched_keys),
                                 -(len(r["first_seen"] or ""))), reverse=False)
        rows.sort(key=lambda r: (r["listing_key"] not in matched_keys,
                                 str(r["first_seen"] or "")), reverse=False)
        # matched first (False sorts first), then oldest-first within — flip fresh-first:
        matched = [r for r in rows if r["listing_key"] in matched_keys]
        rest = [r for r in rows if r["listing_key"] not in matched_keys]
        rest.sort(key=lambda r: str(r["first_seen"] or ""), reverse=True)
        sample = (matched + rest)[:limit_sample]

        return {
            "query": q, "watch_id": watch_id,
            "total": len(rows),
            "matched": len(matched_keys),
            "priced": len(prices),
            "price": None if not prices else {
                "min": min(prices), "p25": _pct(prices, 0.25),
                "median": _pct(prices, 0.50), "p75": _pct(prices, 0.75),
                "max": max(prices),
            },
            "by_source": dict(sorted(by_source.items(), key=lambda kv: -kv[1])),
            "by_decade": dict(sorted(by_year.items())),
            "by_transmission": dict(sorted(by_trans.items(), key=lambda kv: -kv[1])),
            "sample": [{
                "title": r["title"], "url": r["url"], "price": r["price_value"],
                "year": r["year"], "source": (r["source"] or "").replace("www.", ""),
                "matched": r["listing_key"] in matched_keys,
                "first_seen": r["first_seen"], "image": r["image"],
            } for r in sample],
        }
    finally:
        conn.close()
