"""
Site drill — prove we can actually USE a site before we trust a watch to run on it.

A watch is a big, slow, invisible thing: it either quietly works or quietly returns nothing, and
"nothing" looks the same whether the site changed, the login lapsed, or our vision model made
something up. The drill is the small, visible version — a handful of simple tasks a person could
do in twenty seconds — run against the REAL site so each capability passes or fails by name:

    reach → session → safety → see (vision) → comprehend → navigate to a section → find a fact

It exists because of Facebook. Facebook is the highest-stakes site we touch (a ban ends the
buddy's use case), so it does NOT get to be debugged by turning a watch loose on it. It gets
drilled first, read-only, one careful pass, with the halt armed — and only graduates into
HUMAN_FIRST_SITES once the drill actually passes. Nothing here is Facebook-specific though; any
site can be drilled, and every site should be before it's driven.

Two checks worth calling out, because they're the ones that catch silent failure:

  • VISION IS CROSS-EXAMINED, not trusted. The model is asked what it SEES, and every phrase it
    claims to see is then looked for in the DOM. A vision model that reports a plausible-sounding
    Facebook page it isn't actually looking at is worse than no vision at all — this is what
    distinguishes "the model can read the screen" from "the model can describe Facebook".

  • NAVIGATION IS BY CLICK, and verified. The drill finds the section link by its visible text and
    CLICKS it with the mouse, like a person — never a constructed URL (our biggest bot tell) — and
    then confirms the page actually changed. Clicking and hoping is how a sweep silently scrapes
    the wrong page for a week.

SAFETY. Facebook runs strictly read-only under fb_safety: a security checkpoint aborts the drill,
engages the global halt, and never gets clicked through; the halt is checked before we start, so a
flagged account is never poked again by a drill.

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  DRILLS            the per-site task list (what section, what fact to find)
  run_drill         the whole pass: open a browser, run each step, return the report
  _step_see         the vision cross-examination
  _step_navigate    click into a named section like a person, then verify it changed
  _step_find        answer one concrete question from the page we landed on
  render_report     plain-text summary
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

_REPORT_DIRNAME = "drills"

# What "using this site" means, per site. Deliberately SMALL tasks — a drill that needs a
# perfect sweep to pass tells you nothing about which capability broke.
DRILLS: dict[str, dict] = {
    "facebook.com": {
        "url": "https://www.facebook.com/",
        "section": "Marketplace",
        "section_hints": ["Marketplace"],
        "question": "What is the title and asking price of the first item shown in the feed?",
        "expect_login": True,
        "read_only": True,
    },
    "craigslist.org": {
        # A known-good control: if the Facebook drill fails, running this one says whether the
        # problem is Facebook or our browsing stack.
        "url": "https://seattle.craigslist.org/",
        # Hints must be SPECIFIC. A loose one ("for sale") matched "real estate for sale" on a
        # live run and the drill happily reported success on the wrong section — which is exactly
        # the silent wrong-page failure this whole file exists to catch.
        "section": "cars & trucks",
        "section_hints": ["cars & trucks", "cars+trucks"],
        "question": "What is the title and price of the first listing shown?",
        "expect_login": False,
        "read_only": False,
    },
}


def drill_for(url_or_site: str) -> dict:
    s = (url_or_site or "").lower()
    for key, spec in DRILLS.items():
        if key in s or key.split(".")[0] in s:
            return dict(spec)
    return {}


# ---------------------------------------------------------------------------
# Step bookkeeping
# ---------------------------------------------------------------------------

def _step(name: str, ok: bool | None, detail: str, **extra) -> dict:
    """ok True = passed, False = failed, None = skipped/not applicable."""
    return {"step": name, "ok": ok, "detail": detail, **extra}


def _page_text(page, limit: int = 6_000) -> str:
    try:
        return page.inner_text("body", timeout=5_000)[:limit]
    except Exception:
        return ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


# ---------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------

def _step_safety(page, spec: dict, watch_name: str = "drill") -> dict:
    """A checkpoint means the account is FLAGGED. Stop the drill, engage the global halt, and
    never try to solve it — clicking through a checkpoint is how a soft flag becomes a ban."""
    from web_watcher import fb_safety
    if not fb_safety.is_facebook(page.url):
        return _step("safety", None, "not Facebook — no checkpoint rules apply")
    if fb_safety.is_checkpoint(page):
        reason = fb_safety.checkpoint_reason(page)
        fb_safety.engage_halt(reason, watch_name)
        return _step("safety", False, f"SECURITY CHECKPOINT: {reason}. Halt engaged — all Facebook "
                                      "activity is stopped until you clear it by hand.", fatal=True)
    return _step("safety", True, "no checkpoint — page looks normal")


def _step_session(page, spec: dict) -> dict:
    from web_watcher import monitor
    try:
        walled = monitor.is_login_wall(page)
    except Exception as exc:
        return _step("session", None, f"could not tell ({exc})")
    if walled and spec.get("expect_login"):
        return _step("session", False,
                     "Logged OUT — this site needs the saved login profile. Use Connect Facebook "
                     "in the app, sign in by hand, then re-run the drill.", fatal=True)
    if walled:
        return _step("session", True, "logged out, but this site doesn't need a login")
    return _step("session", True, "logged in (no login wall)")


_SEE_SYSTEM = (
    "You are looking at a screenshot of a web page. Describe ONLY what is actually visible. "
    "Do not guess, do not describe what a page like this usually contains, and do not name "
    "anything you cannot literally read in the image.\n"
    "Return ONLY JSON: {\"page_kind\": \"a few words\", \"logged_in\": true|false, "
    "\"visible_text\": [\"exact phrases you can read, 4-8 of them, copied character for character\"], "
    "\"main_sections\": [\"names of navigation sections you can see\"]}"
)


def _step_see(page, cfg, session=None) -> dict:
    """Vision, CROSS-EXAMINED. The model says what it can read; we then check those phrases
    against the DOM. Agreement means it's reading the actual screen. Disagreement means it's
    describing a Facebook from memory — the failure mode that would poison every downstream
    judgement, and the one that is invisible unless you test for it."""
    import base64
    from web_watcher import llm

    try:
        shot = page.screenshot(type="png", full_page=False)
    except Exception as exc:
        return _step("see", False, f"could not take a screenshot: {exc}")
    if not shot:
        return _step("see", False, "screenshot was empty")

    model = getattr(getattr(cfg, "models", None), "vision", "") or "qwen2.5vl:7b"
    try:
        raw = llm.chat(
            [{"role": "system", "content": _SEE_SYSTEM},
             {"role": "user", "content": "What is on this screen?"}],
            role="drill", local_model=model, cfg=cfg, format_json=True,
            images=[base64.b64encode(shot).decode("ascii")],
            timeout=300.0, force_local=True,
        )
        seen = json.loads(raw)
    except Exception as exc:
        return _step("see", False, f"the vision model failed: {type(exc).__name__}: {exc}")

    claims = [str(t) for t in (seen.get("visible_text") or []) if str(t).strip()]
    body = _norm(_page_text(page, 20_000))
    # A claim counts as confirmed if it appears in the page text. Short claims are ignored —
    # a two-letter fragment matches by luck and would inflate the score.
    checkable = [c for c in claims if len(c.strip()) >= 4]
    confirmed = [c for c in checkable if _norm(c) in body]
    ratio = (len(confirmed) / len(checkable)) if checkable else 0.0

    detail = (f"vision read {len(checkable)} phrase(s); {len(confirmed)} confirmed in the page "
              f"({ratio:.0%}). It called the page: {seen.get('page_kind', '?')!r}")
    if not checkable:
        return _step("see", False, "the vision model named nothing readable — it isn't reading the screen.",
                     vision=seen)
    ok = ratio >= 0.5
    if not ok:
        made_up = [c for c in checkable if c not in confirmed][:4]
        detail += f". UNCONFIRMED (not found on the page): {made_up}"
    return _step("see", ok, detail, vision=seen, confirmed=confirmed, model=model)


def _step_comprehend(page, cfg) -> dict:
    """Does the app understand what kind of site this is and what its controls are for? This is
    what stops it typing a product name into a city box."""
    from web_watcher import comprehend
    struct = comprehend.scan_structure(page)
    if not struct:
        return _step("comprehend", False, "the structure scan returned nothing — the page may not "
                                          "have rendered, or it's a heavy SPA")
    try:
        u = comprehend.comprehend_from_structure(struct, cfg)
    except Exception as exc:
        return _step("comprehend", False, f"comprehension failed: {type(exc).__name__}: {exc}",
                     structure={"nav_links": struct.get("nav_links"), "title": struct.get("title")})
    detail = (f"site_kind={u.get('site_kind', '?')!r}, listings_site={u.get('is_listings_site')}, "
              f"viable_for_watch={u.get('viable_for_watch')}, "
              f"search box is for: {u.get('search_box_purpose', '?')!r}")
    return _step("comprehend", bool(u and not u.get("error")), detail, understanding=u,
                 nav_links=(struct.get("nav_links") or [])[:25])


def _find_section_link(page, labels: list[str]):
    """The section's own link, found by the text a PERSON would look for. Tries an exact-ish
    match first, then a contains match, and skips anything fb_safety says not to touch."""
    from web_watcher import fb_safety
    is_fb = fb_safety.is_facebook(page.url)
    for label in labels:
        for sel in (f'a:has-text("{label}")', f'[role=link]:has-text("{label}")',
                    f'[aria-label*="{label}" i]', f'span:has-text("{label}")'):
            try:
                loc = page.locator(sel).first
                if not loc.count():
                    continue
                try:
                    text = (loc.inner_text(timeout=1_500) or "")[:80]
                except Exception:
                    text = label
                if is_fb and fb_safety.is_blocked_action(text):
                    log.info("drill: refusing to click %r — blocked action on Facebook", text)
                    continue
                if loc.is_visible():
                    return loc, text
            except Exception:
                continue
    return None, ""


def _step_navigate(page, spec: dict) -> dict:
    """Go to the named section the way a person does: find its link by its visible text and CLICK
    it with the mouse. Never a constructed URL — that's the bot tell we're avoiding. Then verify
    the page actually changed, because a click that silently did nothing looks like success."""
    from web_watcher.navigate import _human_click, _pause

    section = spec.get("section") or ""
    labels = spec.get("section_hints") or ([section] if section else [])
    loc, text = _find_section_link(page, labels)
    if loc is None:
        return _step("navigate", False,
                     f"could not find a link for {section!r} on the page — the section's control "
                     "is not where we look for it (or the page didn't render).", fatal=True)

    before_url, before_text = page.url, _norm(_page_text(page, 2_000))
    _pause()
    clicked = _human_click(page, loc)
    if not clicked:
        return _step("navigate", False, f"found {text!r} but the click did not land", fatal=True)
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass
    _pause(0.8, 1.6)

    after_url, after_text = page.url, _norm(_page_text(page, 2_000))
    moved = (after_url != before_url) or (after_text != before_text)
    if not moved:
        return _step("navigate", False,
                     f"clicked {text!r} but nothing changed — the page is identical", fatal=True)
    # Landing SOMEWHERE is not landing in the right place. A click that moved us to a different
    # section is a failure, not a pass with a footnote — the drill's whole job is to notice that
    # we're reading the wrong page. Match on the section words, the URL, or the label we clicked
    # (a site may show "cars+trucks" in the link and "cars & trucks" nowhere else).
    words = [w for w in re.split(r"[^a-z0-9]+", _norm(section)) if len(w) > 2]
    hay = _norm(after_url) + " " + after_text + " " + _norm(text)
    hits = [w for w in words if w in hay]
    in_section = bool(words) and len(hits) >= max(1, len(words) // 2)
    if not in_section:
        return _step("navigate", False,
                     f"clicked {text!r} and the page moved to {after_url}, but this does NOT look "
                     f"like the {section!r} section — we'd be reading the wrong page.",
                     url=after_url, section_confirmed=False, fatal=True)
    return _step("navigate", True, f"clicked {text!r} like a person → {after_url}",
                 url=after_url, section_confirmed=True)


_FIND_SYSTEM = (
    "You are reading the text of one web page to answer ONE question. Answer ONLY from the text "
    "given. If the text does not contain the answer, say so plainly — do not guess, and do not "
    "use anything you know about this website from elsewhere.\n"
    "Return ONLY JSON: {\"answer\": \"the answer, or 'not found in the page text'\", "
    "\"quote\": \"the exact snippet you took it from, copied from the text\", "
    "\"found\": true|false}"
)


def _step_find(page, spec: dict, cfg) -> dict:
    """Answer one concrete question from the page we landed on, and verify the model's own quote
    really is in the page. This is the end-to-end proof: we navigated somewhere real and read a
    real fact off it."""
    from web_watcher import llm

    question = spec.get("question") or "What is the main content of this page?"
    text = _page_text(page, 8_000)
    if not text.strip():
        return _step("find", False, "the page had no readable text")

    model = getattr(getattr(cfg, "models", None), "text", "") or "qwen2.5:14b"
    try:
        raw = llm.chat(
            [{"role": "system", "content": _FIND_SYSTEM},
             {"role": "user", "content": f"QUESTION: {question}\n\nPAGE TEXT:\n{text}"}],
            role="drill", local_model=model, cfg=cfg, format_json=True,
            timeout=300.0, num_ctx=8_192, force_local=True,
        )
        got = json.loads(raw)
    except Exception as exc:
        return _step("find", False, f"the model failed to read the page: {type(exc).__name__}: {exc}")

    answer = str(got.get("answer") or "").strip()
    quote = str(got.get("quote") or "").strip()
    if not got.get("found") or not answer or "not found" in answer.lower():
        return _step("find", False, f"could not answer {question!r} from this page", answer=answer)
    grounded = bool(quote) and _norm(quote)[:60] in _norm(text)
    return _step("find", grounded,
                 f"Q: {question}\n     A: {answer}"
                 + ("" if grounded else "  ⚠ the model's supporting quote is NOT in the page text"),
                 answer=answer, quote=quote, grounded=grounded)


# ---------------------------------------------------------------------------
# The whole drill
# ---------------------------------------------------------------------------

def run_drill(site_or_url: str, cfg=None, progress: Optional[Callable[[str], None]] = None,
              headless: bool = False) -> dict:
    """Run the drill for a site and return a report. Opens a real browser with the saved login
    profile (so Facebook sees the account the user signed in as) and never mutates anything."""
    def say(msg: str) -> None:
        log.info("drill: %s", msg)
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    if cfg is None:
        from web_watcher.config import load as load_config
        cfg = load_config()

    spec = drill_for(site_or_url)
    if not spec:
        spec = {"url": site_or_url, "section": "", "section_hints": [],
                "question": "What is the main content of this page?", "expect_login": False}
    url = spec.get("url") or site_or_url
    started = time.time()
    steps: list[dict] = []

    # A halted Facebook is not poked again, not even by a drill.
    from web_watcher import fb_safety
    if fb_safety.is_facebook(url):
        state = fb_safety.halt_state()
        if state:
            return _finish(url, spec, started, [_step(
                "halt", False,
                f"Facebook is HALTED ({state.get('reason')}) since "
                f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(state.get('at', 0)))}. "
                "Check the account by hand, then clear the halt before drilling.", fatal=True)])

    from web_watcher.browser import BrowserSession
    say(f"opening {url}")
    try:
        with BrowserSession(headless=headless, stealth=cfg.browser.stealth,
                            persistent=True, profile_dir=cfg.browser.profile_dir) as session:
            page = session.new_page()
            try:
                page.goto(url, timeout=60_000, wait_until="domcontentloaded")
                steps.append(_step("reach", True, f"loaded {page.url}"))
            except Exception as exc:
                steps.append(_step("reach", False, f"could not load {url}: {exc}", fatal=True))
                return _finish(url, spec, started, steps)

            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass

            for name, fn in (("safety",     lambda: _step_safety(page, spec)),
                             ("session",    lambda: _step_session(page, spec)),
                             ("see",        lambda: _step_see(page, cfg)),
                             ("comprehend", lambda: _step_comprehend(page, cfg)),
                             ("navigate",   lambda: _step_navigate(page, spec)),
                             ("safety2",    lambda: _step_safety(page, spec)),
                             ("see2",       lambda: _step_see(page, cfg)),
                             ("find",       lambda: _step_find(page, spec, cfg))):
                if not spec.get("section") and name in ("navigate", "see2", "safety2"):
                    continue
                say(f"{name}…")
                try:
                    res = fn()
                except Exception as exc:
                    res = _step(name, False, f"step crashed: {type(exc).__name__}: {exc}")
                res["step"] = name
                steps.append(res)
                say(f"{name}: {'PASS' if res['ok'] else ('SKIP' if res['ok'] is None else 'FAIL')} — {res['detail'][:120]}")
                if res.get("fatal"):
                    break
    except Exception as exc:
        steps.append(_step("browser", False, f"the browser session failed: {type(exc).__name__}: {exc}"))

    return _finish(url, spec, started, steps)


def _finish(url: str, spec: dict, started: float, steps: list[dict]) -> dict:
    ran = [s for s in steps if s["ok"] is not None]
    passed = [s for s in ran if s["ok"]]
    report = {
        "generated_at": time.time(),
        "took_s": round(time.time() - started, 1),
        "url": url,
        "section": spec.get("section", ""),
        "question": spec.get("question", ""),
        "steps": steps,
        "passed": len(passed),
        "ran": len(ran),
        "ok": bool(ran) and len(passed) == len(ran),
    }
    save_report(report)
    return report


# ---------------------------------------------------------------------------
# Persistence + rendering
# ---------------------------------------------------------------------------

def _reports_dir(data_dir: Path | None = None) -> Path:
    if data_dir is None:
        from web_watcher import paths
        data_dir = paths.data_dir()
    return Path(data_dir) / _REPORT_DIRNAME


def save_report(report: dict, data_dir: Path | None = None) -> None:
    try:
        d = _reports_dir(data_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"drill_{int(report.get('generated_at', time.time()))}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (d / "latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    except Exception as exc:
        log.warning("drill: could not save the report: %s", exc)


def latest_report(data_dir: Path | None = None) -> dict | None:
    try:
        p = _reports_dir(data_dir) / "latest.json"
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else None
    except Exception:
        pass
    return None


_MARK = {True: "PASS", False: "FAIL", None: "skip"}


def render_report(report: dict) -> str:
    if not report:
        return "No drill has been run yet."
    head = (f"Drill — {report.get('url', '')}\n"
            f"{report.get('passed', 0)}/{report.get('ran', 0)} checks passed "
            f"in {report.get('took_s', 0)}s → {'READY' if report.get('ok') else 'NOT READY'}")
    lines = [head, ""]
    for s in report.get("steps") or []:
        lines.append(f"  [{_MARK[s['ok']]}] {s['step']}: {s['detail']}")
    return "\n".join(lines)
