"""
SQLite run-history helpers.

The database is the only stateful store Web Watcher owns beyond config.yaml.
All config lives in config.yaml; all run records live here.

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  run_history table   ~L28   DDL for per-run records (RunRecord)
  seen_listings table ~L50   DDL for per-watch listing dedup (continuous mode)
  listings table      ~L67   GLOBAL listing-centric store (Phase 2), parsed attributes
  observations table  ~L90   Watch<->listing link: matched + judge reason (stable watch_id)
  init_db             ~L130  Creates all tables/indexes (idempotent migration point)
  upsert_listing      ~L240  Insert/update a listing (preserves richer prior data)
  record_observation  ~L290  Insert/update a watch's observation of a listing
  query_listings      ~L320  Filter the store by attributes/match (assistant + Results view)
  site_profiles table ~L135  DDL for learned-site profiles (listing-URL regex, search/sort)
  site_key            ~L530  Normalize a URL/host to a registrable site key (profile key)
  upsert/get/list/del site_profile ~L560  Learned-site CRUD
  _connect/_resolve   ~L420  Connection (check_same_thread=False) + path resolution
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from web_watcher import paths

DB_PATH          = paths.db_path()
SCREENSHOTS_DIR  = paths.screenshots_dir()

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS run_history (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_name           TEXT    NOT NULL,
    run_timestamp        TEXT    NOT NULL,
    found                BOOLEAN NOT NULL,
    summary              TEXT,
    link                 TEXT,
    confidence           TEXT,
    perception_mode_used TEXT,
    error                TEXT,
    screenshot_path      TEXT
)
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_watch_name ON run_history (watch_name, id DESC)
"""

# seen_listings — remembers individual listings already seen per watch so that
# continuous-mode watches alert ONLY on genuinely new items (not the same listings
# every sweep). listing_key is the stable id/url derived from each listing.
_CREATE_SEEN_TABLE = """
CREATE TABLE IF NOT EXISTS seen_listings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_name  TEXT NOT NULL,
    listing_key TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    summary     TEXT,
    link        TEXT,
    UNIQUE(watch_name, listing_key)
)
"""

_CREATE_SEEN_INDEX = """
CREATE INDEX IF NOT EXISTS idx_seen_listing ON seen_listings (watch_name, listing_key)
"""

# listings — the GLOBAL, listing-centric store (Phase 2). One row per real-world
# listing, keyed by its stable listing_key (e.g. "craigslist:7891234567") and DEDUPED
# ACROSS WATCHES. This persists OUTSIDE any watch: it survives watch rename/delete, and
# a listing surfaced by two watches is one row here. Parsed attributes (price/year/
# mileage/transmission/drivetrain) are stored as columns for filtering + sorting; the
# raw ad body is kept in `details`, and the full parsed dict in `attributes` (JSON).
_CREATE_LISTINGS_TABLE = """
CREATE TABLE IF NOT EXISTS listings (
    listing_key   TEXT PRIMARY KEY,
    source        TEXT,
    url           TEXT,
    title         TEXT,
    price_text    TEXT,
    price_value   INTEGER,
    year          INTEGER,
    mileage       INTEGER,
    transmission  TEXT,
    drivetrain    TEXT,
    details       TEXT,
    attributes    TEXT,
    fingerprint   TEXT,
    image         TEXT,
    posted_at     TEXT,
    first_seen    TEXT,
    last_seen     TEXT
)
"""

# Index the content fingerprint so repost/duplicate lookups are fast.
_CREATE_LISTINGS_FP_INDEX = """
CREATE INDEX IF NOT EXISTS idx_listings_fp ON listings (fingerprint)
"""

# observations — the watch <-> listing link (Phase 2). One row per (watch, listing):
# which watch surfaced the listing, when (first/last), whether it matched the watch's
# criteria, and the judge's reason. Keyed by STABLE watch_id so a rename doesn't orphan
# history; watch_name is denormalised for display. Survives watch deletion.
_CREATE_OBSERVATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS observations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id     TEXT NOT NULL,
    watch_name   TEXT,
    listing_key  TEXT NOT NULL,
    first_seen   TEXT,
    last_seen    TEXT,
    matched      INTEGER,
    rating       INTEGER,
    judge_reason TEXT,
    UNIQUE(watch_id, listing_key)
)
"""

_CREATE_OBSERVATIONS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_obs_watch ON observations (watch_id, last_seen DESC)
"""

# term_expansions — the LEARNING synonym log. Maps a normalized shopping intent
# ("manual sports cars") to the set of effective search terms a savvy shopper would use
# ("miata", "corvette", "mustang gt", …). Generated once per concept by an LLM, then
# CACHED + reused (and grown) so the app gets better at "other ways to refer to a thing".
_CREATE_TERM_EXPANSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS term_expansions (
    concept    TEXT PRIMARY KEY,
    terms      TEXT,
    updated_at TEXT
)
"""

# site_profiles — the LEARNED-SITE store. When you point Web Watcher at a NEW site it
# explores the layout once and records here how that site is shaped: how its listing
# URLs look (so dedup keys are stable), where its search box lives + which query param
# carries the term (so search-term swapping works), and any sort options (so sweeps can
# vary the feed). Keyed by a normalized site key (e.g. "craigslist.org", "offerup.com").
# This is what makes "point it at a site and it learns" possible — without a profile the
# extractor only knows the 3 built-in sites and falls back to fuzzy guessing elsewhere.
_CREATE_SITE_PROFILES_TABLE = """
CREATE TABLE IF NOT EXISTS site_profiles (
    domain              TEXT PRIMARY KEY,
    display_name        TEXT,
    key_prefix          TEXT,
    listing_url_regex   TEXT,
    card_selector       TEXT,
    search_url_template TEXT,
    search_param        TEXT,
    sort_param          TEXT,
    sort_values         TEXT,
    sample_listing_url  TEXT,
    notes               TEXT,
    learned_at          TEXT
)
"""


# ---------------------------------------------------------------------------
# Record type
# ---------------------------------------------------------------------------

@dataclass
class RunRecord:
    watch_name:           str
    run_timestamp:        str          # ISO-8601
    found:                bool
    summary:              Optional[str] = None
    link:                 Optional[str] = None
    confidence:           Optional[str] = None
    perception_mode_used: Optional[str] = None
    error:                Optional[str] = None
    screenshot_path:      Optional[str] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init_db(db_path: Path | None = None) -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    path = _resolve(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    with _connect(path) as conn:
        conn.execute(_CREATE_TABLE)
        conn.execute(_CREATE_INDEX)
        conn.execute(_CREATE_SEEN_TABLE)
        conn.execute(_CREATE_SEEN_INDEX)
        conn.execute(_CREATE_LISTINGS_TABLE)
        conn.execute(_CREATE_OBSERVATIONS_TABLE)
        conn.execute(_CREATE_OBSERVATIONS_INDEX)
        conn.execute(_CREATE_TERM_EXPANSIONS_TABLE)
        conn.execute(_CREATE_SITE_PROFILES_TABLE)
        # Migration: add `fingerprint` to a pre-existing listings table that lacks it.
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(listings)").fetchall()]
        if "fingerprint" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN fingerprint TEXT")
        if "image" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN image TEXT")
        if "posted_at" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN posted_at TEXT")
        conn.execute(_CREATE_LISTINGS_FP_INDEX)
        # Migration: add `rating` (1-5 graded match) to a pre-existing observations table.
        obs_cols = [r["name"] for r in conn.execute("PRAGMA table_info(observations)").fetchall()]
        if "rating" not in obs_cols:
            conn.execute("ALTER TABLE observations ADD COLUMN rating INTEGER")


def save_run(record: RunRecord, db_path: Path | None = None) -> int:
    """Insert a run record and return the new row id."""
    path = _resolve(db_path)
    with _connect(path) as conn:
        cur = conn.execute(
            """INSERT INTO run_history
               (watch_name, run_timestamp, found, summary, link, confidence,
                perception_mode_used, error, screenshot_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.watch_name, record.run_timestamp, int(record.found),
                record.summary, record.link, record.confidence,
                record.perception_mode_used, record.error, record.screenshot_path,
            ),
        )
        return cur.lastrowid


# ---------------------------------------------------------------------------
# Seen-listing dedup (continuous mode)
# ---------------------------------------------------------------------------

def save_seen_listing(
    watch_name:  str,
    listing_key: str,
    first_seen:  str,
    summary:     str | None = None,
    link:        str | None = None,
    db_path:     Path | None = None,
) -> bool:
    """
    Record a listing as seen for a watch. Returns True if it was newly inserted,
    False if it was already present (UNIQUE constraint → INSERT OR IGNORE no-op).
    """
    path = _resolve(db_path)
    with _connect(path) as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO seen_listings
               (watch_name, listing_key, first_seen, summary, link)
               VALUES (?, ?, ?, ?, ?)""",
            (watch_name, listing_key, first_seen, summary, link),
        )
        return cur.rowcount > 0


def has_seen_listing(
    watch_name: str,
    listing_key: str,
    db_path: Path | None = None,
) -> bool:
    """Return True if this listing was already recorded for the watch."""
    path = _resolve(db_path)
    if not path.exists():
        return False
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_listings WHERE watch_name=? AND listing_key=? LIMIT 1",
            (watch_name, listing_key),
        ).fetchone()
    return row is not None


def get_seen_listings(
    watch_name: str,
    limit: int = 500,
    db_path: Path | None = None,
) -> list[dict]:
    """Return recorded listings for a watch, newest-first."""
    path = _resolve(db_path)
    if not path.exists():
        return []
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM seen_listings WHERE watch_name=? ORDER BY id DESC LIMIT ?",
            (watch_name, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def count_seen_listings(watch_name: str, db_path: Path | None = None) -> int:
    """Return how many listings have been recorded for a watch."""
    path = _resolve(db_path)
    if not path.exists():
        return 0
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM seen_listings WHERE watch_name=?",
            (watch_name,),
        ).fetchone()
    return int(row["n"]) if row else 0


# ---------------------------------------------------------------------------
# Listing-centric store (Phase 2): global listings + per-watch observations
# ---------------------------------------------------------------------------

def upsert_listing(
    listing_key: str,
    *,
    source:     str,
    url:        str,
    title:      str,
    price_text:  str = "",
    attributes:  dict | None = None,
    details:     str = "",
    fingerprint: str = "",
    image:       str = "",
    posted_at:   str = "",
    ts:          str,
    db_path:     Path | None = None,
) -> None:
    """
    Insert or update a listing in the global store. On re-sighting we keep first_seen,
    bump last_seen, and only overwrite a field with NEW non-empty info (so a cheap
    re-sighting without a deep-read never wipes previously-captured details/attributes).
    """
    attributes = attributes or {}
    path = _resolve(db_path)
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO listings
                (listing_key, source, url, title, price_text, price_value, year,
                 mileage, transmission, drivetrain, details, attributes, fingerprint,
                 image, posted_at, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(listing_key) DO UPDATE SET
                last_seen    = excluded.last_seen,
                title        = COALESCE(NULLIF(excluded.title,''), listings.title),
                price_text   = COALESCE(NULLIF(excluded.price_text,''), listings.price_text),
                price_value  = COALESCE(excluded.price_value, listings.price_value),
                year         = COALESCE(excluded.year, listings.year),
                mileage      = COALESCE(excluded.mileage, listings.mileage),
                transmission = COALESCE(NULLIF(excluded.transmission,''), listings.transmission),
                drivetrain   = COALESCE(NULLIF(excluded.drivetrain,''), listings.drivetrain),
                details      = CASE WHEN length(excluded.details) > length(COALESCE(listings.details,''))
                                    THEN excluded.details ELSE listings.details END,
                attributes   = COALESCE(NULLIF(excluded.attributes,''), listings.attributes),
                fingerprint  = COALESCE(NULLIF(excluded.fingerprint,''), listings.fingerprint),
                image        = COALESCE(NULLIF(excluded.image,''), listings.image),
                posted_at    = COALESCE(NULLIF(excluded.posted_at,''), listings.posted_at)
            """,
            (
                listing_key, source, url, title, price_text,
                attributes.get("price_value"), attributes.get("year"),
                attributes.get("mileage"), attributes.get("transmission"),
                attributes.get("drivetrain"), details,
                json.dumps(attributes) if attributes else "", fingerprint, image,
                posted_at, ts, ts,
            ),
        )


def find_duplicate(
    watch_id:    str,
    fingerprint: str,
    source:      str,
    exclude_key: str | None = None,
    db_path:     Path | None = None,
) -> dict | None:
    """
    Find the CANONICAL (earliest-seen) listing this watch already observed that is the
    same item as an incoming one — same content fingerprint AND same source. Returns
    {listing_key, matched} or None.

    Conservatism is deliberate: we require a same-source fingerprint match, so we never
    merge listings across different sites (two sellers posting the same common truck on
    different sites are kept SEPARATE). Better to show a possible dup than to hide a real
    listing. Empty fingerprint/source → None.
    """
    if not fingerprint or not source:
        return None
    path = _resolve(db_path)
    if not path.exists():
        return None
    with _connect(path) as conn:
        row = conn.execute(
            """SELECT l.listing_key, o.matched
               FROM observations o JOIN listings l ON l.listing_key = o.listing_key
               WHERE o.watch_id = ? AND l.fingerprint = ? AND l.source = ? AND l.listing_key != ?
               ORDER BY l.first_seen ASC LIMIT 1""",
            (watch_id, fingerprint, source, exclude_key or ""),
        ).fetchone()
    return dict(row) if row else None


def record_observation(
    watch_id:     str,
    watch_name:   str,
    listing_key:  str,
    ts:           str,
    matched:      bool,
    judge_reason: str | None = None,
    rating:       int | None = None,
    db_path:      Path | None = None,
) -> None:
    """
    Record (or refresh) that a watch surfaced a listing: first/last seen, the match
    verdict, the 1-5 rating, and the judge's reason. Keyed by stable watch_id so renames
    don't orphan it.
    """
    path = _resolve(db_path)
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO observations
                (watch_id, watch_name, listing_key, first_seen, last_seen, matched, rating, judge_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(watch_id, listing_key) DO UPDATE SET
                last_seen    = excluded.last_seen,
                watch_name   = excluded.watch_name,
                matched      = excluded.matched,
                rating       = COALESCE(excluded.rating, observations.rating),
                judge_reason = COALESCE(excluded.judge_reason, observations.judge_reason)
            """,
            (watch_id, watch_name, listing_key, ts, ts, int(matched), rating, judge_reason),
        )


def query_listings(
    watch_id:     str | None = None,
    matched:      bool | None = None,
    transmission: str | None = None,
    drivetrain:   str | None = None,
    min_year:     int | None = None,
    max_price:    int | None = None,
    max_mileage:  int | None = None,
    text:         str | None = None,
    limit:        int = 200,
    db_path:      Path | None = None,
) -> list[dict]:
    """
    Query the listing store. `text` is a general, item-agnostic search over the title +
    ad body (so it works for ANY kind of thing, not just vehicles). The vehicle-specific
    attribute filters (transmission/drivetrain/year/mileage) remain available for when
    they apply. Each row carries `watches` (which watch(es) surfaced it) and `dup_count`.
    When watch_id is given, joins observations (filter by matched, order by last seen).
    """
    path = _resolve(db_path)
    if not path.exists():
        return []
    where: list[str] = []
    args:  list = []
    if transmission: where.append("l.transmission = ?"); args.append(transmission)
    if drivetrain:   where.append("l.drivetrain = ?");   args.append(drivetrain)
    if min_year is not None:    where.append("l.year >= ?");        args.append(min_year)
    if max_price is not None:   where.append("l.price_value <= ?"); args.append(max_price)
    if max_mileage is not None: where.append("l.mileage <= ?");     args.append(max_mileage)
    if text:
        where.append("(l.title LIKE ? OR l.details LIKE ?)")
        args.append(f"%{text}%"); args.append(f"%{text}%")

    # dup_count: how many listings share this content fingerprint+source (note dups, don't
    # hide). watches: which watch(es) surfaced this listing — so the UI can delineate.
    dupc = ("(SELECT COUNT(*) FROM listings d WHERE d.fingerprint = l.fingerprint "
            "AND d.source = l.source AND COALESCE(l.fingerprint,'') <> '')")
    whoq = ("(SELECT GROUP_CONCAT(DISTINCT o2.watch_name) FROM observations o2 "
            "WHERE o2.listing_key = l.listing_key)")
    # best_url: for a re-posted item (many listings share one fingerprint), link to the
    # MOST RECENTLY SEEN repost, not the canonical (earliest-seen) one — that original post
    # is usually already deleted by the time the seller has reposted it, so its link would
    # 404 / redirect ("links to a different page"). Falls back to this row's own url.
    fresh = ("(SELECT d.url FROM listings d WHERE d.fingerprint = l.fingerprint "
             "AND d.source = l.source AND COALESCE(l.fingerprint,'') <> '' "
             "ORDER BY d.last_seen DESC, d.first_seen DESC LIMIT 1)")
    with _connect(path) as conn:
        if watch_id:
            sql = (f"SELECT l.*, o.matched, o.rating, o.judge_reason, o.last_seen AS observed_at, "
                   f"{dupc} AS dup_count, {whoq} AS watches, {fresh} AS best_url "
                   "FROM observations o JOIN listings l ON l.listing_key = o.listing_key "
                   "WHERE o.watch_id = ?")
            args2 = [watch_id]
            if matched is not None:
                sql += " AND o.matched = ?"; args2.append(int(matched))
            if where:
                sql += " AND " + " AND ".join(where); args2 += args
            sql += " ORDER BY o.last_seen DESC LIMIT ?"; args2.append(limit)
            rows = conn.execute(sql, args2).fetchall()
        else:
            sql = f"SELECT l.*, {dupc} AS dup_count, {whoq} AS watches, {fresh} AS best_url FROM listings l"
            conds = list(where)
            # "Matches only" across ALL watches: keep a listing only if SOME watch judged it a
            # match. Without this, the all-watches Results view showed every listing the crawler
            # ever bumped into (including judge-rejected junk), not the actual finds.
            if matched:
                conds.append("EXISTS (SELECT 1 FROM observations o "
                             "WHERE o.listing_key = l.listing_key AND o.matched = 1)")
            if conds:
                sql += " WHERE " + " AND ".join(conds)
            sql += " ORDER BY l.last_seen DESC LIMIT ?"; args.append(limit)
            rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        bu = d.pop("best_url", None)
        if bu:                     # point the link at the freshest live repost
            d["url"] = bu
        out.append(d)
    return out


def _norm_concept(intent: str) -> str:
    """Normalize an intent phrase to a stable cache key ('Manual Sports Cars!' → 'manual sports cars')."""
    import re as _re
    return " ".join(_re.sub(r"[^a-z0-9 ]+", " ", (intent or "").lower()).split())


def get_term_expansion(intent: str, db_path: Path | None = None) -> list[str]:
    """Return cached search terms for an intent, or [] if none learned yet."""
    path = _resolve(db_path)
    if not path.exists():
        return []
    key = _norm_concept(intent)
    if not key:
        return []
    try:
        with _connect(path) as conn:
            row = conn.execute("SELECT terms FROM term_expansions WHERE concept=?", (key,)).fetchone()
        return list(json.loads(row["terms"])) if row else []
    except Exception:
        return []   # table not created yet (pre-migration DB) or bad JSON


def save_term_expansion(intent: str, terms: list[str], db_path: Path | None = None,
                        replace: bool = False) -> None:
    """Learn/grow the search terms for an intent. By default UNIONs with anything already
    cached (learning grows over time). Pass replace=True to OVERWRITE the cache with exactly
    `terms` — used when the user explicitly refreshes/sets terms and wants the old set gone."""
    path = _resolve(db_path)
    key = _norm_concept(intent)
    if not key or not terms:
        return
    prior: list[str] = [] if replace else list(get_term_expansion(intent, db_path))
    merged: list[str] = []
    seen = set()
    for t in prior + list(terms):
        t = (t or "").strip()
        k = t.lower()
        if t and k not in seen:
            seen.add(k); merged.append(t)
    try:
        with _connect(path) as conn:
            conn.execute(
                """INSERT INTO term_expansions (concept, terms, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(concept) DO UPDATE SET terms=excluded.terms, updated_at=excluded.updated_at""",
                (key, json.dumps(merged), ""),
            )
    except Exception:
        pass   # table not created yet (pre-migration DB)


# ---------------------------------------------------------------------------
# Site profiles (learned-site store)
# ---------------------------------------------------------------------------

# Second-level "public suffix"-ish labels that aren't the registrable name on their own
# (so seattle.craigslist.org → craigslist.org, www.gumtree.com.au → gumtree.com.au).
_MULTI_TLD_SLD = frozenset((
    "co", "com", "org", "net", "gov", "edu", "ac", "go",
))

# Columns persisted for a site profile, in a stable order for INSERT/SELECT mapping.
_SITE_PROFILE_COLS = (
    "domain", "display_name", "key_prefix", "listing_url_regex", "card_selector",
    "search_url_template", "search_param", "sort_param", "sort_values",
    "sample_listing_url", "notes", "learned_at",
)


def site_key(url_or_host: str) -> str:
    """
    Normalize a URL or host to a stable registrable site key used as the profile key.

      https://seattle.craigslist.org/search/cta  -> craigslist.org
      www.offerup.com                              -> offerup.com
      foo.gumtree.com.au                           -> gumtree.com.au

    Returns "" when nothing host-like can be extracted.
    """
    s = (url_or_host or "").strip().lower()
    if not s:
        return ""
    if "://" in s:
        from urllib.parse import urlparse
        s = urlparse(s).netloc or ""
    s = s.split("/")[0].split("@")[-1].split(":")[0]   # strip path/creds/port
    if s.startswith("www."):
        s = s[4:]
    labels = [p for p in s.split(".") if p]
    if len(labels) <= 2:
        return ".".join(labels)
    # If the second-to-last label is a country/registry SLD (co.uk, com.au), keep 3 labels.
    if labels[-2] in _MULTI_TLD_SLD and len(labels[-1]) <= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def upsert_site_profile(profile: dict, db_path: Path | None = None) -> None:
    """Insert or replace a learned site profile. `profile['domain']` is normalized via
    site_key; sort_values may be a list (stored as JSON) or a string."""
    domain = site_key(profile.get("domain") or profile.get("url") or "")
    if not domain:
        return
    sort_values = profile.get("sort_values")
    if isinstance(sort_values, (list, tuple)):
        sort_values = json.dumps(list(sort_values))
    row = {
        "domain":              domain,
        "display_name":        profile.get("display_name") or domain,
        "key_prefix":          profile.get("key_prefix") or domain.split(".")[0],
        "listing_url_regex":   profile.get("listing_url_regex") or "",
        "card_selector":       profile.get("card_selector") or "",
        "search_url_template": profile.get("search_url_template") or "",
        "search_param":        profile.get("search_param") or "",
        "sort_param":          profile.get("sort_param") or "",
        "sort_values":         sort_values or "",
        "sample_listing_url":  profile.get("sample_listing_url") or "",
        "notes":               profile.get("notes") or "",
        "learned_at":          profile.get("learned_at") or "",
    }
    path = _resolve(db_path)
    placeholders = ", ".join("?" for _ in _SITE_PROFILE_COLS)
    with _connect(path) as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO site_profiles ({', '.join(_SITE_PROFILE_COLS)}) "
            f"VALUES ({placeholders})",
            tuple(row[c] for c in _SITE_PROFILE_COLS),
        )


def get_site_profile(url_or_host: str, db_path: Path | None = None) -> dict | None:
    """Return the learned profile for a URL/host (matched by registrable site key), or None.
    sort_values is decoded back into a list."""
    domain = site_key(url_or_host)
    if not domain:
        return None
    path = _resolve(db_path)
    if not path.exists():
        return None
    try:
        with _connect(path) as conn:
            row = conn.execute(
                "SELECT * FROM site_profiles WHERE domain=?", (domain,)
            ).fetchone()
    except Exception:
        return None   # table not created yet (pre-migration DB)
    return _row_to_profile(row) if row else None


def list_site_profiles(db_path: Path | None = None) -> list[dict]:
    """Return all learned site profiles (newest learned first)."""
    path = _resolve(db_path)
    if not path.exists():
        return []
    try:
        with _connect(path) as conn:
            rows = conn.execute(
                "SELECT * FROM site_profiles ORDER BY learned_at DESC, domain ASC"
            ).fetchall()
    except Exception:
        return []
    return [_row_to_profile(r) for r in rows]


def delete_site_profile(url_or_host: str, db_path: Path | None = None) -> bool:
    """Delete a learned profile. Returns True if a row was removed."""
    domain = site_key(url_or_host)
    if not domain:
        return False
    path = _resolve(db_path)
    if not path.exists():
        return False
    with _connect(path) as conn:
        cur = conn.execute("DELETE FROM site_profiles WHERE domain=?", (domain,))
    return cur.rowcount > 0


def _row_to_profile(row) -> dict:
    d = dict(row)
    raw = d.get("sort_values") or ""
    try:
        d["sort_values"] = json.loads(raw) if raw else []
    except Exception:
        d["sort_values"] = []
    return d


def count_matches(watch_id: str, db_path: Path | None = None) -> int:
    """How many listings this watch has observed that MATCHED its criteria — a quick
    health/review signal for the assistant ("found N so far")."""
    path = _resolve(db_path)
    if not path.exists():
        return 0
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM observations WHERE watch_id=? AND matched=1",
            (watch_id,),
        ).fetchone()
    return int(row["n"]) if row else 0


def count_observations(watch_id: str, db_path: Path | None = None) -> int:
    """Total listings this watch has observed (matched or not) — paired with
    count_matches this gives the oversight agent its data-quality signal: a watch
    with many observations but zero matches is searching the wrong terms."""
    path = _resolve(db_path)
    if not path.exists():
        return 0
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM observations WHERE watch_id=?",
            (watch_id,),
        ).fetchone()
    return int(row["n"]) if row else 0


def get_history(
    watch_name: str | None = None,
    limit: int = 50,
    db_path: Path | None = None,
) -> list[dict]:
    """Return run records newest-first, optionally filtered by watch name."""
    path = _resolve(db_path)
    if not path.exists():
        return []
    with _connect(path) as conn:
        if watch_name:
            rows = conn.execute(
                "SELECT * FROM run_history WHERE watch_name=? ORDER BY id DESC LIMIT ?",
                (watch_name, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM run_history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_run_by_id(run_id: int, db_path: Path | None = None) -> dict | None:
    """Return a single run record by primary key, or None."""
    path = _resolve(db_path)
    if not path.exists():
        return None
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM run_history WHERE id=?", (run_id,)
        ).fetchone()
    return dict(row) if row else None


def get_last_run(watch_name: str, db_path: Path | None = None) -> dict | None:
    """Return the most recent run record for a watch, or None."""
    path = _resolve(db_path)
    if not path.exists():
        return None
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM run_history WHERE watch_name=? ORDER BY id DESC LIMIT 1",
            (watch_name,),
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _resolve(db_path: Path | None) -> Path:
    return Path(db_path) if db_path else DB_PATH


def _connect(path: Path) -> sqlite3.Connection:
    # timeout + busy_timeout: wait for a lock instead of erroring immediately, and WAL
    # mode lets the dashboard/assistant READ while a continuous sweep WRITES (the data
    # layer now does many writes per sweep). Without this, concurrent access can raise
    # "database is locked".
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass
    return conn
