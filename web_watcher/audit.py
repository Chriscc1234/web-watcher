"""The Watch Auditor — a slow, thorough review of every watch's health.

Born from an afternoon of hand-audits that kept finding the same classes of weirdness the
live narrator can't see: a watch whose instruction named three sites while every url hit
one; a rotation burning half its sweeps on junk search terms; fifteen matches recorded and
never pushed to anyone; a briefing suppressed and never re-offered; a prefilter dropping
100% of a site's harvest forever. The admin's ask, verbatim: "is there an agent that
reviews the watches and looks at issues like this? takes a long time is ok."

Two passes:
  1. DETERMINISTIC — every check below is arithmetic over config + the DB + the data dir.
     These are the seed checks, each one a bug actually found by hand first.
  2. LLM (optional) — the big local model reads the evidence bundle and writes up anything
     odd the rules didn't anticipate. Best-effort: no model, no second pass, findings stand.

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  run_audit          the whole thing: evidence → findings → optional LLM pass → persist
  gather_evidence    one watch → a plain dict of facts (config, ledger, activity)
  deterministic_findings   the seed checks over one watch's evidence
  latest / watermark load the persisted report / last-run time
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, unquote_plus

log = logging.getLogger(__name__)

_DIRNAME = "watch_audits"
_KNOWN_SITES = ("craigslist", "offerup", "ebay", "facebook", "boattrader",
                "cargurus", "autotrader")


def _dir() -> Path:
    from web_watcher import paths
    return paths.data_dir() / _DIRNAME


def _url_terms(urls) -> list[str]:
    out = []
    for u in urls or []:
        for k, v in parse_qsl(urlparse(u).query):
            if k in ("q", "query", "_nkw", "keywords") and v.strip():
                out.append(unquote_plus(v).strip())
    return out


def gather_evidence(watch, db_path=None) -> dict:
    """Plain facts about one watch. Every number here is read, not inferred."""
    from web_watcher.storage import watch_stats, unalerted_matches, count_seen_listings
    wid = getattr(watch, "id", None) or watch.name
    ev = {
        "name": watch.name,
        "enabled": bool(getattr(watch, "enabled", True)),
        "mode": getattr(watch, "mode", ""),
        "owner": str(getattr(watch, "owner", "") or ""),
        "instruction": getattr(watch, "instruction", "") or "",
        "judgment_prompt": getattr(watch, "judgment_prompt", "") or "",
        "urls": list(getattr(watch, "urls", None) or []),
        "url_hosts": sorted({urlparse(u).netloc.lower().replace("www.", "")
                             for u in (getattr(watch, "urls", None) or [])}),
        "url_terms": _url_terms(getattr(watch, "urls", None)),
        "keywords": list(getattr(watch, "keywords", None) or []),
        "notify_on": bool(getattr(getattr(watch, "notify", None), "telegram", True)
                          or getattr(getattr(watch, "notify", None), "email", False)),
        "min_rating": getattr(watch, "min_rating", None),
    }
    try:
        st = watch_stats(wid, watch.name, db_path=db_path) if db_path else \
             watch_stats(wid, watch.name)
        ev["stats"] = st
    except Exception:
        ev["stats"] = {}
    try:
        ev["seen_count"] = count_seen_listings(watch.name, db_path)
    except Exception:
        ev["seen_count"] = None
    try:
        # min_rating floor mirrors the drip's logic — these are matches nobody was shown.
        floor = ev["min_rating"] if ev["min_rating"] else 4
        ev["unalerted"] = [
            {"title": r.get("title"), "rating": r.get("rating")}
            for r in unalerted_matches(wid, min_rating=floor, limit=25, db_path=db_path)]
    except Exception:
        ev["unalerted"] = []
    try:
        from web_watcher import paths
        briefs = json.loads((paths.data_dir() / "baseline_briefings.json")
                            .read_text(encoding="utf-8"))
        ev["briefed"] = watch.name in briefs if isinstance(briefs, dict) else None
    except Exception:
        ev["briefed"] = None
    try:
        # WHAT THE USER ACTUALLY ASKED FOR, from their chat thread — so the audit can hold
        # the watch against its origin story ("under $1000" that never became a price cap,
        # a named site that never became a url). The 14b writes the cards; the audit reads
        # the receipts.
        from web_watcher import paths
        owner = str(getattr(watch, "owner", "") or "")
        hist_file = (paths.data_dir() /
                     (f"watcher_history_{owner}.json" if owner else "watcher_history.json"))
        toks = [t for t in __import__("re").findall(r"[A-Za-z0-9]{4,}", watch.name.lower())
                if t not in ("watch", "cars", "watches")]
        lines = []
        if hist_file.exists():
            hist = json.loads(hist_file.read_text(encoding="utf-8"))
            for m in hist:
                c = str(m.get("content") or "")
                if m.get("role") == "user" and any(t in c.lower() for t in toks):
                    lines.append(c[:220])
        ev["origin_chat"] = lines[-8:]
    except Exception:
        ev["origin_chat"] = []
    try:
        from web_watcher import paths
        f = paths.data_dir() / "continuous_running.json"
        desired = set(json.loads(f.read_text(encoding="utf-8")) or []) if f.exists() else None
        ev["desired_running"] = None if desired is None else (watch.name in desired)
    except Exception:
        ev["desired_running"] = None
    return ev


def deterministic_findings(ev: dict) -> list[dict]:
    """The seed checks. Each one is a bug that was found by hand first."""
    F = []

    def add(kind, severity, finding, fix=""):
        F.append({"watch": ev["name"], "kind": kind, "severity": severity,
                  "finding": finding, "fix": fix})

    naming = f"{ev['instruction']} {ev['judgment_prompt']}".lower()
    hosts = " ".join(ev["url_hosts"])

    # 1. Named-site coverage (Charlie's create: 3 sites named, 1 site searched).
    missing = [s for s in _KNOWN_SITES if s in naming and s not in hosts]
    if missing and ev["urls"]:
        add("site_coverage", "high",
            f"instruction names {', '.join(missing)} but no url searches there",
            "add a canonical search url per missing site (the create/update guards do this "
            "for new cards; older watches need a one-time edit)")

    # 2. Junk terms in the rotation (modifier/city variants of a sibling term).
    try:
        from web_watcher.search_terms import _drop_modifier_variants
        terms = ev["url_terms"]
        kept = _drop_modifier_variants(list(dict.fromkeys(terms)))
        junk = [t for t in dict.fromkeys(terms) if t not in kept]
        if junk:
            add("junk_rotation", "medium",
                f"rotation burns sweeps on junk terms: {junk}",
                "remove those urls — each is a guaranteed-thin search and a bot-tell")
    except Exception:
        pass

    # 3. Found-but-never-pushed (the 15-MacGregor hole). A watch with notifications OFF
    #    banks matches as market data BY DESIGN — that's not a delivery failure.
    if ev["unalerted"] and ev.get("notify_on", True):
        add("unalerted_matches", "high",
            f"{len(ev['unalerted'])} match(es) recorded and never sent "
            f"(e.g. {ev['unalerted'][0]['title']!r})",
            "the per-sweep drip should drain these; if it isn't, something upstream "
            "is gating it")

    # 4. Ledger shape: many seen, matches exist, but alerts stopped long ago is visible
    #    through unalerted above; here catch the DEAD watch instead — enabled and desired
    #    but its stats never move.
    st = ev.get("stats") or {}
    if ev["enabled"] and ev.get("desired_running") and not st.get("runs"):
        add("never_ran", "high", "enabled and wanted running, but it has never run",
            "check the engine (orchestrator rotation / scheduler thread) and the logs")

    # 5. Dry watch: plenty seen, nothing ever matched.
    if (st.get("observations") or 0) >= 50 and not (st.get("matches") or 0):
        add("dry_watch", "medium",
            f"{st['observations']} listings seen, zero matched — search may be too narrow "
            f"or the judge too strict",
            "review the search terms and the judge criteria together")

    # 6. Briefing accounting: a primed watch whose owner never got a first look.
    if ev.get("briefed") is False and (ev.get("seen_count") or 0) > 0:
        add("no_briefing", "medium",
            "listings were primed but no baseline briefing was ever sent",
            "the owner has no idea what the first sweep found")

    # 7. Keyword sanity: a keyword that never appears in any search term is usually a
    #    leftover; a watch with NO keywords whose instruction names a brand relies purely
    #    on the judge (fine, but worth knowing).
    kws = [k.lower() for k in ev["keywords"]]
    terms_l = " ".join(ev["url_terms"]).lower()
    stale = [k for k in kws if k not in terms_l and k not in naming]
    if stale:
        add("stale_keywords", "low",
            f"keyword(s) {stale} appear in neither the searches nor the instruction",
            "probably left over from an earlier edit")

    # 8. The origin cross-check: a budget the user stated that never became a cap.
    import re as _re
    origin = " ".join(ev.get("origin_chat") or [])
    m = _re.search(r"under\s+\$?(\d[\d,]*)|\$\s?(\d[\d,]*)\s*(?:or less|max|budget)",
                   origin, _re.I)
    if m:
        stated = (m.group(1) or m.group(2) or "").replace(",", "")
        cfg_text = f"{ev['instruction']} {ev['judgment_prompt']} " + " ".join(ev["urls"])
        if stated and stated not in cfg_text:
            add("origin_budget_missing", "high",
                f"the user asked for under ${stated} in chat, but no such cap appears in "
                f"the watch's instruction or urls",
                "add the price cap — the 14b's card dropped a stated constraint")

    return F


def _llm_pass(evidence: list[dict], findings: list[dict]) -> str:
    """The big model reads the evidence and writes up anything odd the rules missed.
    Best-effort — an audit with no model still ships its deterministic findings."""
    try:
        from web_watcher.config import load as load_config
        from web_watcher import llm
        cfg = load_config()
        model = cfg.models.effective_council_model
        sys_p = (
            "You are auditing a marketplace-watching app's WATCHES. You get one JSON "
            "evidence bundle per watch plus the deterministic findings already made. "
            "Write up to 5 SHORT bullet findings for anything genuinely odd the rules "
            "missed — delivery gaps, config that contradicts itself, activity that makes "
            "no sense. Each watch's origin_chat holds what the user ACTUALLY asked for "
            "in their own words — hold the config against it and flag any dropped or "
            "twisted constraint. Skip anything already in the findings list. If nothing "
            "else is "
            "odd, say exactly: nothing further.")
        user = json.dumps({"evidence": evidence, "existing_findings": findings},
                          ensure_ascii=False)[:24000]
        out = llm.chat([{"role": "system", "content": sys_p},
                        {"role": "user", "content": user}],
                       role="audit", local_model=model, cfg=cfg,
                       force_local=True, timeout=600.0, max_tokens=800)
        return (out or "").strip()
    except Exception as exc:
        log.info("watch audit: LLM pass skipped (%s)", exc)
        return ""


def run_audit(cfg=None, db_path=None, use_llm: bool = True) -> dict:
    """Audit every watch; persist and return the report."""
    started = time.time()
    if cfg is None:
        from web_watcher.config import load as load_config
        cfg = load_config()
    evidence, findings = [], []
    for w in getattr(cfg, "watches", []) or []:
        try:
            ev = gather_evidence(w, db_path)
            evidence.append(ev)
            findings.extend(deterministic_findings(ev))
        except Exception as exc:
            log.warning("watch audit: could not audit %r: %s", w.name, exc)
    notes = _llm_pass(evidence, findings) if use_llm else ""
    report = {
        "ran_at": started,
        "duration_s": round(time.time() - started, 1),
        "watches": len(evidence),
        "findings": findings,
        "llm_notes": notes,
    }
    try:
        d = _dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    except Exception as exc:
        log.warning("watch audit: could not persist report: %s", exc)
    log.info("Watch audit: %d watch(es), %d finding(s)%s in %.0fs",
             len(evidence), len(findings),
             " + LLM notes" if notes and "nothing further" not in notes.lower() else "",
             report["duration_s"])
    return report


def latest() -> dict | None:
    try:
        return json.loads((_dir() / "latest.json").read_text(encoding="utf-8"))
    except Exception:
        return None
