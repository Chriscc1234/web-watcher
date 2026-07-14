"""
AI-driven autonomous browser agent with multi-agent offline council.

When the single worker agent gets stuck (CAPTCHA failure, repeated actions,
consecutive errors) it convenes a council of local Ollama experts that discuss
the situation in sequence — each expert sees what the others said — and a
synthesiser turns their conclusions into a single recommended action.

Agent loop per step:
  1. Snapshot interactive elements from the DOM
  2. Ask the LLM for the next action (JSON)
  3. Execute it with human-like timing
  4. If stuck for COUNCIL_TRIGGER steps → convene offline Ollama council
  5. Repeat until LLM signals "done" or max_steps is reached

No vision model required — works with any Ollama text model.

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  _SYSTEM              ~L55    System prompt sent to the agent model each step
  COUNCIL_TRIGGER      ~L45    How many repeat actions before the get-unstuck pass
  run_agent()          ~L205   Main public entry point + agent loop (overlay + CAPTCHA gates)
  _UNSTUCK_SYSTEM      ~L575   System prompt for the offline get-unstuck recovery pass
  _convene_council()   ~L620   Single context-rich recovery pass (sees the element list)
  _detect_captcha()    ~L720   CAPTCHA detection heuristics
  _SNAPSHOT_JS         ~L1125  JavaScript that scrapes interactive DOM elements
  _NON_TEXT_INPUT_TYPES~L1230  Input types excluded from "text field" category
  _elements_text()     ~L1235  Formats element list for the LLM prompt
  _query_llm()         ~L1280  Builds the full prompt and calls Ollama
  _execute()           ~L1430  Translates AgentAction → Playwright calls
  _describe_page()     ~L1630  Vision scan: llava screenshot description
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import math
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx
from playwright.sync_api import Page, TimeoutError as PWTimeoutError

from web_watcher import fb_safety
from web_watcher.monitor import dismiss_popups, has_blocking_overlay, read_search_feedback

log = logging.getLogger(__name__)

OLLAMA_URL      = "http://localhost:11434"
OLLAMA_TIMEOUT  = 60.0
# Vision models tokenize a screenshot into image tokens that scale with resolution.
# At a large window (e.g. 2560x1440) a single screenshot can exceed qwen2.5vl's
# default 4096 context → Ollama returns HTTP 400 ("exceeds the available context
# size"). Give the vision calls explicit headroom so they don't fail on big monitors.
_VISION_NUM_CTX = 8192

# ---- human-pace timing ----
ACTION_PAUSE    = (0.4, 1.1)   # pause between agent actions (seconds)
PAGE_SETTLE     = (0.9, 1.8)   # pause after navigation / page change (seconds)
CAPTCHA_POLL_S  = 2.0           # how often to re-check if CAPTCHA cleared
CAPTCHA_WAIT_S  = 30.0          # how long to wait before giving up on CAPTCHA

# After this many consecutive identical actions, summon the council
COUNCIL_TRIGGER = 3

FALLBACK_SEARCH = "https://duckduckgo.com/?q="

_SYSTEM = """\
You are a browser agent. You control a real web browser step by step to complete a goal.

══ WHAT YOU RECEIVE EACH STEP ══════════════════════════════════════════════════

  URL and page title
  Visual description — a screenshot analysis of the current viewport
  Interactive elements — numbered list split into two groups:
      ON SCREEN   visible right now, can interact immediately
      BELOW FOLD  off-screen below the current view, must scroll down first
    >>> TEXT INPUT <<<  a text/search field you can type into — use 'type' with its index
    BUTTON              a clickable button
    LINK                a navigation link — use 'click', NOT 'type'
    CHECKBOX / RADIO    a toggleable option — use 'click' to check/uncheck
    [CHECKED]/[unchecked] shows checkbox or radio state
    [FOCUSED] means the element already has keyboard focus
  Text fields are listed at the TOP so you can find them easily.
  Page text — raw visible text extracted from the DOM
  Working memory — facts you saved earlier with the 'remember' action
  Action history — every action you took and its RESULT:
      navigated → <url>       a new page loaded (click, form submit, link, etc.)
      title changed            page content updated without a full navigation
      auto-Enter → navigated  the system pressed Enter after your type — a page loaded
      page unchanged           the action had no visible effect — do something different
      ERROR: <message>         the action failed — read the error and adjust

══ RESPONSE FORMAT ══════════════════════════════════════════════════════════════

Respond with exactly one JSON object — no markdown, no text outside it.
Always fill in the "thought" field first, describing what you see and why you
chose the action. A filled-in thought leads to better decisions.

{
  "thought":       "<REQUIRED — restate what you see and why you chose this action. Never leave empty.>",
  "action":        "click" | "type" | "select" | "press" | "navigate" | "scroll" | "remember" | "done",
  "element_index": <integer from the element list — REQUIRED for click, type, and select>,
  "text":          "<text to type, the exact option text for select, OR a complete https:// URL for navigate>",
  "key":           "<key name for press: Enter, Tab, Escape, ArrowDown, etc.>",
  "direction":     "down" | "up",
  "amount":        <pixels to scroll, e.g. 400>,
  "memory_key":    "<short label for remember, e.g. 'price_1'>",
  "memory_value":  "<value to store — the item name and price exactly as the page shows it>",
  "summary":       "<for done: complete report of everything you found>"
}

══ HOW TO USE EACH ACTION ═══════════════════════════════════════════════════════

  click   — Click a button, link, checkbox, tab, or any interactive element.
             Requires element_index. Use this to submit forms, follow links, open
             dropdowns, toggle checkboxes, and select filter options.

  type    — Type text into a >>> TEXT INPUT <<< element. Requires element_index.
             ONLY use 'type' on elements marked >>> TEXT INPUT <<<.
             Do NOT use 'type' on BUTTON, LINK, CHECKBOX, or any other element type.
             After typing a search query, the system will automatically press Enter
             if the page does not navigate. You do not need to press Enter yourself.

  select  — Choose an option in a DROPDOWN element. Requires element_index of the
             DROPDOWN and text = the option to pick, copied EXACTLY from that
             element's "options:" list. This is the ONLY way to change a DROPDOWN
             (sort order, category, price filter) — clicking one does nothing.

  press   — Send a keyboard key without targeting an element.
             Use when you need Enter, Tab, Escape, or arrow keys globally.

  navigate — Go to a specific URL. The text field must be a full URL starting with
             https:// or http://. Do NOT put search queries here — use type for that.

  scroll   — Scroll the page. Use direction "down" to reveal content below the fold,
             "up" to return to the top. Only scroll when the content you need is listed
             under BELOW FOLD or is not yet visible. Do not scroll as a first action —
             read what is ON SCREEN first.

  remember — Save a fact to your working memory so you don't forget it.
             REQUIRED fields: memory_key (short label) AND memory_value (the data).
             Use this immediately when you spot a price, name, or key data point.
             Example: {"action":"remember","memory_key":"price_1","memory_value":"<item name and price, copied from the page>"}

  done    — End the session with a full summary. Only use done after you have actually
             found and read the information the goal asks for and saved it with remember.
             Include all saved facts and your conclusion in the summary field.

══ NAVIGATION PATTERNS ══════════════════════════════════════════════════════════

  Searching a site:
    1. Look at the TOP of the ON SCREEN list for a >>> TEXT INPUT <<< element.
       The element list always puts text inputs first.
    2. Use 'type' with that element's index and your search query as the text.
    3. The page will navigate to search results — wait for the next step.
    4. On the results page, read what is ON SCREEN. Do not scroll first.

  Reading search results:
    1. The page text contains the listing titles, prices, and details.
    2. Read the page text to extract what you need.
    3. Use 'remember' to save each item you find (e.g. price_1, price_2, …).
    4. Scroll down only after reading everything currently visible.

  Using filters and checkboxes:
    1. Filters are usually in a left sidebar or under a "Filter" panel.
    2. Find the relevant checkbox or link in the element list and 'click' it.
    3. After clicking, check the RESULT — if the page navigated or title changed,
       the filter was applied. If unchanged, the click may have missed — try again.

  Sorting and filtering results:
    - An element marked DROPDOWN lists its choices after "options:". Use the 'select'
      action with that element's index and the option text — e.g. to sort by newest,
      select the option that says "newest". Never 'click' a DROPDOWN; it does nothing.
    - Menus that are BUTTONs or LINKs (common on Facebook: "Sort by", "Date listed")
      work in two steps: click the button, then on the NEXT step the open menu's
      choices appear as new elements — click the one you want.
    - Filter panels: click "Filters", then set the relevant controls (a price field is
      a >>> TEXT INPUT <<< — type the number; a category is a LINK or DROPDOWN).

  Tabs and sections:
    Click the tab or section heading to reveal its content.

══ STRICT RULES ═════════════════════════════════════════════════════════════════

  - 'thought' is REQUIRED. Always describe what you currently see before choosing an action.
  - When the goal specifies a search term in quotes, type EXACTLY that text — do not
    substitute, paraphrase, or use a different product name.
  - element_index is REQUIRED for 'click', 'type', and 'select'. Always pick a real
    index from the numbered list. Never guess or invent an index.
  - DROPDOWN elements only respond to 'select' (with exact option text) — never
    'click' or 'type' on them.
  - 'type' ONLY works on >>> TEXT INPUT <<< elements. Using 'type' on a LINK or BUTTON
    will fail and waste a step. If you need to search, find the >>> TEXT INPUT <<< in
    the list and use its index.
  - 'navigate' only accepts a full URL. Never put search keywords into navigate.
  - If a result was "page unchanged", that action did nothing. Do not repeat it.
    Try: a different element, pressing Enter, clicking a submit button, or scrolling.
  - Do not scroll as your first action on a new page. Read ON SCREEN content first.
  - Do not call 'done' while still on the starting page or with an empty scratchpad.
    You must have actually gathered the requested information before finishing.
  - If blocked by a CAPTCHA or access denial, call 'done' with a clear explanation.
"""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class AgentAction:
    thought:       str
    action:        str
    element_index: Optional[int]   = None
    text:          Optional[str]   = None
    key:           Optional[str]   = None
    direction:     Optional[str]   = "down"
    amount:        int             = 300
    summary:       Optional[str]   = None
    memory_key:    Optional[str]   = None
    memory_value:  Optional[str]   = None
    outcome:       Optional[str]   = None  # what observably changed after this action


@dataclass
class AgentResult:
    final_text:    str
    steps_taken:   int
    summary:       Optional[str]         = None
    error:         Optional[str]         = None
    history:       list[AgentAction]     = field(default_factory=list)
    scratchpad:    dict[str, str]        = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_agent(
    page:          Page,
    instruction:   str,
    model:         str,
    max_steps:     int = 15,
    council_model: Optional[str] = None,
    vision_model:  Optional[str] = None,
    ocr_threshold: int = 200,
    on_step:       Optional[Callable[[Page], None]] = None,
    should_stop:   Optional[Callable[[Page], bool]] = None,
    exploration_mode: bool = False,
) -> AgentResult:
    """
    Run the agent loop on an already-loaded page.

    Returns an AgentResult with the final page text and action summary.
    When the agent is stuck (same action repeated COUNCIL_TRIGGER times, or
    CAPTCHA solver failed), a council of local Ollama experts is convened to
    recommend the next move.

    on_step: optional callback invoked with the current page once before the loop
    and again after every successful action. The agent-driven continuous watch uses
    it to harvest listings (extract_listings) from every page the agent visits while
    it browses; it never influences the agent's decisions. Exceptions raised by the
    callback are swallowed so a harvest hiccup can't derail the agent.

    should_stop: optional guardrail checked at the top of every step. When it returns
    True the loop ends immediately. The continuous watch uses it to bail the instant
    the page leaves the target site or hits a login wall — so the agent never
    interacts with a login form (it must never enter credentials) or wanders off-site.
    """
    history:        list[AgentAction] = []
    scratchpad:     dict[str, str]    = {}
    submitted_queries: set[str]       = set()   # search terms already typed — block repeats
    consecutive_same = 0
    last_sig:        str | None       = None
    consecutive_errs = 0
    _council_model   = council_model or model
    page_context:     str              = ""   # vision description, refreshed on path change
    last_vision_path: str              = ""   # netloc+path at which page_context was captured
    start_url:        str              = page.url  # used to detect premature done

    def _harvest() -> None:
        if on_step is None:
            return
        try:
            on_step(page)
        except Exception as exc:
            log.debug("on_step harvest callback failed: %s", exc)

    # Emit focus events so DataDome sees a normal tab-activation sequence
    _emit_focus_events(page)
    _harvest()   # harvest the initial page before the agent moves

    for step in range(max_steps):
        log.info("Agent step %d/%d  url=%s", step + 1, max_steps, page.url)

        # ── Stop guardrail ──────────────────────────────────────────────────
        # Bail the instant the caller says the page is out of bounds (left the
        # target site, or hit a login wall). Prevents the agent from ever touching
        # a login form or wandering off-site.
        if should_stop is not None:
            try:
                if should_stop(page):
                    log.info("Agent stop guardrail tripped at %s — ending browse", page.url)
                    break
            except Exception as exc:
                log.debug("should_stop guardrail check failed: %s", exc)

        # ── Overlay gate ────────────────────────────────────────────────────
        # A login / cookie / consent modal (Facebook Marketplace's "Log in or sign
        # up" being the worst offender) sits on top of the page and intercepts every
        # click and scroll — the agent would otherwise burn its whole step budget
        # poking at controls behind the overlay and get stuck. This is the same class
        # of blocker as a CAPTCHA, so it gets the same treatment: a gate at the top of
        # the loop that clears the obstruction before the agent tries to act. Cheap to
        # probe (no wait); only dismiss when something is actually in the way.
        if has_blocking_overlay(page):
            n = dismiss_popups(page, settle_ms=0)
            if n:
                log.info("Overlay gate: dismissed %d blocking overlay control(s)", n)
                _human_pause(*ACTION_PAUSE)

        # ── CAPTCHA gate ────────────────────────────────────────────────────
        if _detect_captcha(page):
            log.warning("CAPTCHA detected on %s", page.url)
            solved = _solve_captcha(page)
            if not solved:
                log.warning("CAPTCHA solver pipeline exhausted — running get-unstuck pass")
                # Snapshot once so the recovery pass and the execute below choose from
                # (and act on) the exact same element list.
                captcha_elements = _snapshot_elements(page)
                rec = _convene_council(
                    page,
                    "CAPTCHA not solved after full pipeline (press-hold, checkbox, audio)",
                    history,
                    _council_model,
                    captcha_elements,
                )
                if rec and rec.action != "done":
                    history.append(rec)
                    try:
                        _execute(page, rec, captcha_elements)
                    except Exception as exc:
                        log.warning("Recovery action failed: %s", exc)
                    _human_pause(*PAGE_SETTLE)
                    continue
                else:
                    log.error("Recovery pass gave up — stopping agent")
                    break
            _human_pause(*PAGE_SETTLE)

        # ── Vision page description (fires on path change, not query-param change) ──
        # Changing filters/sorting only changes query params — same page layout,
        # no need to re-scan. Vision fires on structural transitions only:
        # homepage → search results → product page, etc.
        _cur_path = _url_path(page.url)
        if vision_model and _cur_path != last_vision_path:
            page_context      = _describe_page(page, vision_model)
            last_vision_path  = _cur_path

        # ── DOM snapshot ────────────────────────────────────────────────────
        elements = _snapshot_elements(page)
        if not elements:
            log.warning("Agent: no interactive elements found — stopping")
            break
        log.debug("Agent elements (%d): %s", len(elements),
                  "; ".join(f"[{e['index']}]{e.get('tag','?')} {e.get('text','')[:20]!r}"
                            for e in elements[:12]))

        # ── LLM decision ────────────────────────────────────────────────────
        try:
            action = _query_llm(model, instruction, page, elements, history, scratchpad,
                                vision_model, ocr_threshold, page_context)
            consecutive_errs = 0
        except Exception as exc:
            log.error("Agent LLM call failed: %s", exc)
            consecutive_errs += 1
            if consecutive_errs >= 3:
                log.error("3 consecutive LLM errors — stopping")
                break
            continue

        history.append(action)
        _detail = ""
        if action.action == "type":
            _el = f" el={action.element_index}" if action.element_index is not None else ""
            _detail = f"{_el} text={action.text[:50]!r}" if action.text else _el
        elif action.action == "navigate" and action.text:
            _detail = f" text={action.text[:50]!r}"
        elif action.action == "click" and action.element_index is not None:
            _detail = f" el={action.element_index}"
        elif action.action == "select":
            _detail = f" el={action.element_index} option={(action.text or '')[:30]!r}"
        log.info("Agent action: %s%s  (thought: %s)", action.action, _detail, action.thought[:80])

        # ── Facebook read-only guard ──────────────────────────────────────────
        # On Facebook the agent may only browse — never message/offer/buy/like/comment/
        # post/save/follow. Reject a click on any such control BEFORE it happens; this is
        # the hard rule that keeps an account from being flagged for automated activity.
        if action.action == "click" and action.element_index is not None and fb_safety.is_facebook(page.url):
            _tgt = next((e for e in elements if e["index"] == action.element_index), None)
            if _tgt and fb_safety.is_blocked_action(_tgt.get("label", "")):
                log.warning("Agent tried a blocked Facebook action %r — refusing (read-only)",
                            _tgt.get("label", "")[:40])
                action.outcome = (
                    f"REFUSED: '{_tgt.get('label','')[:40]}' takes a social/transactional action "
                    "on Facebook. This watch is READ-ONLY — only scroll, search, sort, filter, "
                    "and open listings to read them. Never message, offer, buy, like, comment, "
                    "post, save, or follow. Choose a browsing action instead."
                )
                _human_pause(*ACTION_PAUSE)
                continue

        # ── Validate element_index for click / type / select ─────────────────
        # Reject the action immediately rather than failing silently deeper in
        # the stack, and feed clear corrective feedback into the history.
        if action.action in ("click", "type", "select") and action.element_index is None:
            log.warning("Agent returned %r with no element_index — rejecting", action.action)
            action.outcome = (
                f"REJECTED: '{action.action}' requires element_index. "
                "Look at the numbered element list above and set element_index "
                "to the integer index of the element you want to interact with."
            )
            _human_pause(*ACTION_PAUSE)
            continue

        # ── Block repeated searches ───────────────────────────────────────────
        # The agent tends to re-type the SAME query it already searched (different
        # element index, same text), thrashing instead of progressing. Reject a
        # repeat and steer it to scroll for more results or finish.
        if action.action == "type" and action.text and action.text.strip():
            q = " ".join(action.text.lower().split())
            if q in submitted_queries:
                log.info("Agent re-typed an already-used search %r — rejecting", action.text)
                action.outcome = (
                    f"REJECTED: you already searched {action.text!r}. Do NOT repeat the "
                    "same search. Either SCROLL down to load more of the current results, "
                    "or finish if you have seen enough."
                )
                _human_pause(*ACTION_PAUSE)
                continue
            submitted_queries.add(q)

        if action.action == "remember":
            if action.memory_key and action.memory_key.strip():
                scratchpad[action.memory_key] = action.memory_value or ""
                log.info("Agent remembered: %s = %r", action.memory_key, action.memory_value)
                action.outcome = f"saved: {action.memory_key!r} = {action.memory_value!r}"
            else:
                log.warning("Agent called 'remember' with no memory_key — rejecting")
                action.outcome = (
                    "REJECTED: 'remember' requires memory_key AND memory_value. "
                    'Example: {"action":"remember","memory_key":"price_1","memory_value":"<item name and price, copied from the page>"}'
                )
            _human_pause(*ACTION_PAUSE)
            continue

        if action.action == "done":
            # Exploration mode (continuous sweep): the agent's only job is to LOAD
            # listings by scrolling — there is no "navigate + gather facts" goal, and
            # scrolling only changes the URL hash so the schedule-mode checks below would
            # reject every 'done' forever. Allow finishing after a couple of actions.
            if exploration_mode:
                if step < 2:
                    log.info("Exploration 'done' at step %d — scroll a bit more first", step + 1)
                    action.outcome = ("REJECTED — scroll a couple more times to load listings, "
                                      "then you may finish.")
                    _human_pause(*ACTION_PAUSE)
                    continue
                return AgentResult(
                    final_text  = _extract_text(page),
                    steps_taken = step + 1,
                    summary     = action.summary,
                    history     = history,
                    scratchpad  = scratchpad,
                )

            still_at_start = _url_path(page.url) == _url_path(start_url)
            no_facts = not scratchpad and not (action.summary and len(action.summary) > 80)

            # Case 1: still on starting page — agent hasn't even tried to navigate
            if still_at_start and no_facts:
                log.warning(
                    "Agent returned 'done' at step %d — still on start URL with no data — rejecting",
                    step + 1,
                )
                # Build directive feedback: tell the model exactly what to do next
                text_inputs = [
                    e for e in elements
                    if e.get("tag") in ("input", "textarea")
                    and (e.get("type") or "").lower() not in _NON_TEXT_INPUT_TYPES
                    and e.get("inViewport")
                ]
                if text_inputs:
                    inp = text_inputs[0]
                    next_step = (
                        f"Your NEXT action must be: "
                        f"type with element_index={inp['index']} "
                        f"and text=<your search query from the goal>."
                    )
                else:
                    next_step = "Find the search box in the element list and type your query."
                action.outcome = (
                    "REJECTED — you are still on the starting page and have not searched yet. "
                    + next_step
                )
                _human_pause(*ACTION_PAUSE)
                continue

            # Case 2: navigated somewhere but collected nothing (too early to quit)
            # Allow done at later steps or when a real summary was written.
            early_quit_limit = max_steps // 3   # e.g. step 6 for max_steps=20
            if no_facts and step < early_quit_limit:
                log.warning(
                    "Agent returned 'done' at step %d with empty scratchpad — rejecting",
                    step + 1,
                )
                action.outcome = (
                    "REJECTED — you navigated to the page but have not collected any data yet. "
                    "Read the page text, use 'remember' to save prices/items/facts, "
                    "then call 'done' with a summary of what you found."
                )
                _human_pause(*ACTION_PAUSE)
                continue
            else:
                return AgentResult(
                    final_text  = _extract_text(page),
                    steps_taken = step + 1,
                    summary     = action.summary,
                    history     = history,
                    scratchpad  = scratchpad,
                )

        # ── Stuck detection ─────────────────────────────────────────────────
        # For type actions, ignore element_index in the sig: autocomplete dropdowns
        # change element indices between steps (same search text, different element),
        # which would otherwise reset consecutive_same and prevent force-Enter.
        if action.action == "type":
            sig = f"type|{action.text}"
        else:
            sig = f"{action.action}|{action.element_index}|{action.text}|{action.key}"
        if sig == last_sig:
            consecutive_same += 1
        else:
            consecutive_same = 0
            last_sig = sig

        if consecutive_same >= COUNCIL_TRIGGER:
            # Special case: stuck on type with actual text = search typed but Enter
            # never pressed. Only fire if there's text to submit — don't force-Enter
            # on a blank type action (model returned type with no text/element).
            if action.action == "type" and action.text and consecutive_same == COUNCIL_TRIGGER:
                log.info(
                    "Agent stuck on type (%dx) — refocusing input and force-pressing Enter",
                    consecutive_same,
                )
                _pre_url = page.url
                # Autocomplete / lost-focus scenarios can leave no input focused.
                # Re-focus the nearest visible text input before pressing Enter so
                # the keystroke goes to the search form rather than scrolling the page.
                try:
                    page.evaluate("""() => {
                        const active = document.activeElement;
                        if (!active || !['INPUT','TEXTAREA'].includes(active.tagName)) {
                            const inp = document.querySelector(
                                'input[type="search"],input[type="text"],textarea'
                            );
                            if (inp) inp.focus();
                        }
                    }""")
                except Exception:
                    pass
                page.keyboard.press("Enter")
                _wait_for_settle(page)
                _post_url = page.url
                action.outcome = (
                    f"auto-submitted Enter → navigated to {_post_url[:60]}"
                    if _post_url != _pre_url
                    else "auto-submitted Enter → page unchanged"
                )
                consecutive_same = 0
                last_sig = None
                _human_pause(*ACTION_PAUSE)
                continue

            log.warning(
                "Agent stuck (%dx same action: %s) — running get-unstuck pass",
                consecutive_same, action.action,
            )
            rec = _convene_council(
                page,
                f"Repeated the same action {consecutive_same} times: "
                f"{action.action} (thought: {action.thought[:80]})",
                history,
                _council_model,
                elements,
            )
            if rec and rec.action != "done":
                log.info("Council recommends: %s — %s", rec.action, rec.thought[:80])
                _pre_url  = page.url
                _pre_ttl  = page.title() or ""
                history.append(rec)
                try:
                    _execute(page, rec, elements)
                except Exception as exc:
                    log.warning("Council action failed: %s", exc)
                    rec.outcome = f"ERROR: {exc}"
                if rec.outcome is None:
                    _post_url = page.url
                    _post_ttl = page.title() or ""
                    rec.outcome = _action_outcome(_pre_url, _post_url, _pre_ttl, _post_ttl)
                consecutive_same = 0
                last_sig = None
                _human_pause(*ACTION_PAUSE)
                _harvest()   # harvest after a recovery action moved the page
                continue

        # ── Execute ─────────────────────────────────────────────────────────
        _pre_url = page.url
        _pre_ttl = page.title() or ""
        try:
            _execute(page, action, elements)
            consecutive_errs = 0
        except Exception as exc:
            log.warning("Agent action failed (%s): %s", action.action, exc)
            consecutive_errs += 1
            action.outcome = f"ERROR: {exc}"

        if action.outcome is None:
            _post_url = page.url
            _post_ttl = page.title() or ""
            action.outcome = _action_outcome(_pre_url, _post_url, _pre_ttl, _post_ttl)

        # A click that opens a custom dropdown/panel changes no URL or title, so it
        # reads as "page unchanged" — which tells the model its click DID NOTHING and
        # steers it away from the menu it just opened (real failure: craigslist's
        # sort menu). Diff the interactive elements and report what appeared instead.
        if action.action == "click" and action.outcome == "page unchanged":
            try:
                after = _snapshot_elements(page)
                before_keys = {(e["tag"], e["label"]) for e in elements}
                fresh = [e for e in after
                         if (e["tag"], e["label"]) not in before_keys
                         and e.get("inViewport") and (e.get("label") or "").strip()]
                if fresh:
                    names = " | ".join(f'"{e["label"][:40]}"' for e in fresh[:8])
                    action.outcome = (
                        f"the click OPENED A MENU/PANEL — new choices are now on screen: {names}. "
                        "NEXT STEP: click the choice you want (look it up in the new element list "
                        "by its label — indexes have changed)."
                    )
            except Exception:
                pass
        # ── Read what the box said BACK: is it a keyword search or a geo picker? ──
        # The weather-site failure: typing product terms into a box whose autocomplete only
        # offers cities. Tell the model what it's looking at instead of letting it press on.
        _was_unchanged = action.outcome == "page unchanged"
        _typed_location_box = False
        if action.action == "type" and (action.text or "").strip():
            try:
                fb = read_search_feedback(page, action.text or "")
                if fb.get("suggestions"):
                    sug = " | ".join(fb["suggestions"][:6])
                    if fb.get("are_locations"):
                        _typed_location_box = True
                        action.outcome = (
                            (action.outcome or "") +
                            f" — the box suggested LOCATIONS ({sug}). This is a place/geo picker, "
                            "NOT a keyword search: do NOT type product keywords here. Pick the "
                            "intended location, or find the real listings search elsewhere on the "
                            "page — and if this site has no keyword search for items, it is the "
                            "wrong kind of site to monitor."
                        )
                    else:
                        action.outcome = (action.outcome or "") + f" — the box suggested: {sug}"
            except Exception:
                pass
        log.debug("Action outcome: %s", action.outcome)

        # ── Auto-submit: type landed in an input but page didn't move ─────────
        # The model typed into a real input but didn't press Enter.
        # Only fires when a text input is actually focused after typing —
        # prevents submitting an empty search if the type went to the wrong element.
        # NOT when the box is a location picker — auto-Entering a product keyword there
        # just navigates to a nonsense geo page and the model would think it "worked".
        if action.action == "type" and _was_unchanged and not _typed_location_box:
            try:
                focused_tag = page.evaluate(
                    "() => document.activeElement ? document.activeElement.tagName : ''"
                )
            except Exception:
                focused_tag = ""
            if focused_tag in ("INPUT", "TEXTAREA"):
                log.info("Type landed in %s but page unchanged — auto-pressing Enter", focused_tag)
                _pre_enter_url = page.url
                page.keyboard.press("Enter")
                _wait_for_settle(page)
                _post_enter_url = page.url
                if _post_enter_url != _pre_enter_url:
                    action.outcome = f"auto-Enter → navigated to {_post_enter_url[:60]}"
                else:
                    action.outcome = "auto-Enter → page unchanged"
                log.info("Auto-Enter outcome: %s", action.outcome)
            else:
                log.info(
                    "Type had no effect and no input is focused (active=%r) — "
                    "skipping auto-Enter to avoid empty submit",
                    focused_tag or "body",
                )

        _human_pause(*ACTION_PAUSE)
        _harvest()   # harvest listings from the page this action landed on

    return AgentResult(
        final_text  = _extract_text(page),
        steps_taken = len(history),
        history     = history,
        scratchpad  = scratchpad,
    )


# ---------------------------------------------------------------------------
# Offline "get unstuck" reasoning pass (formerly a 3-expert council)
# ---------------------------------------------------------------------------
#
# Why this is a single call, not three sequential "experts":
#   The old design ran three personas + a synthesiser — four sequential calls to
#   the SAME local model. Three calls to one 14B model do not approximate a larger
#   model; they mostly add 30-60s of latency and drift. Worse, none of the experts
#   nor the synthesiser ever saw the numbered element list, yet the synthesiser was
#   asked to emit an `element_index` — so it guessed an integer blind, and that
#   guess was then executed against the real list. That mismatch was the main reason
#   "the council isn't smart".
#
# What actually makes a small local model punch above its weight is CONTEXT and
# STRUCTURE, not repetition: give it the exact indexed elements it must choose from,
# and have it reason through the failure modes (overlay → captcha → navigation →
# fallback) in one structured pass before committing to a valid action. That is this
# function. The multi-perspective checklist now lives inside one prompt.

_UNSTUCK_SYSTEM = """\
You are a browser-automation recovery specialist. An autonomous agent is STUCK and
you must choose the single best next action to get it moving toward its goal.

You are given the goal, what went wrong, recent history, the page text, and — most
importantly — the NUMBERED list of interactive elements currently on the page. Any
"click" or "type" you choose MUST use an element_index that appears in that list.
Never invent an index that is not shown.

Reason through these failure modes in order, then commit to ONE action:
  1. OVERLAY / MODAL — Is a login, cookie, or "sign up" popup covering the page?
     Prefer clicking its Close / "X" / "Not now" / "Decline" control (find it in the
     element list). This is the most common cause of a stuck agent.
  2. CAPTCHA / BOT BLOCK — Does the page text mention robots, verification, or
     "press and hold"? If so and there is no actionable control, choose "done" and
     report that the site blocked automation (offline solving already failed).
  3. WRONG PATH — Did the agent keep clicking the wrong thing or typing into the
     wrong field? Pick the correct element from the list (text inputs are marked
     ">>> TEXT INPUT <<<"). To submit a typed search, use press with key "Enter".
  4. FALLBACK — If the direct path is dead, navigate to a simpler URL or a different
     search, OR choose "done" with a summary of whatever useful data is already
     visible. A graceful exit beats spinning.

OFFLINE ONLY: never suggest paid APIs, 2Captcha, Anti-Captcha, reCAPTCHA services,
or any cloud tool. Everything must run locally.

Output ONLY this JSON object — no prose, no code fences:
{
  "diagnosis":     "<one sentence: which failure mode above applies and why>",
  "thought":       "<why this action, max 60 chars>",
  "action":        "click" | "type" | "select" | "press" | "navigate" | "scroll" | "done",
  "element_index": <int from the element list, or null>,
  "text":          "<text to type, exact DROPDOWN option text for select, or URL for navigate, else null>",
  "key":           "<key for press, e.g. \\"Enter\\", else null>",
  "direction":     "down",
  "amount":        300,
  "summary":       "<plain-English result, only when action is done>"
}
"""


def _convene_council(
    page:          Page,
    problem:       str,
    history:       list[AgentAction],
    council_model: str,
    elements:      Optional[list[dict]] = None,
) -> Optional[AgentAction]:
    """
    Run a single, context-rich "get unstuck" reasoning pass on the local model and
    return the recommended next AgentAction (or None if it fails).

    Unlike the old multi-call council, this sees the actual numbered element list, so
    the element_index it returns refers to a real element rather than a blind guess.
    `elements` is the same snapshot the main loop will execute against; when omitted
    (e.g. the CAPTCHA branch before the per-step snapshot) the caller should pass a
    fresh _snapshot_elements(page).
    """
    elements = elements or []
    page_text = _extract_text(page)[:1500]
    history_text = "\n".join(
        f"  step {i+1}: {a.action} — {a.thought[:60]} → {(a.outcome or '')[:50]}"
        for i, a in enumerate(history[-8:])
    )
    elements_block = _elements_text(elements) if elements else "  (no interactive elements detected)"

    situation = (
        f"GOAL/PROBLEM: {problem}\n"
        f"URL: {page.url}\n"
        f"Title: {page.title() or '(no title)'}\n\n"
        f"Recent action history (most recent last):\n{history_text or '  (none)'}\n\n"
        f"INTERACTIVE ELEMENTS (choose element_index from these):\n{elements_block}\n\n"
        f"Page text (excerpt):\n{page_text}\n\n"
        f"Choose the single best next action to get unstuck."
    )

    log.info("Get-unstuck pass for: %s", problem[:60])
    try:
        payload = {
            "model":    council_model,
            "messages": [
                {"role": "system", "content": _UNSTUCK_SYSTEM},
                {"role": "user",   "content": situation},
            ],
            "stream": False,
            "format": "json",
        }
        with httpx.Client(timeout=OLLAMA_TIMEOUT) as client:
            r = client.post(f"{OLLAMA_URL}/api/chat", json=payload)
            r.raise_for_status()
        raw = r.json()["message"]["content"]
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            start = raw.find("{"); end = raw.rfind("}")
            data = json.loads(raw[start:end + 1])

        rec = AgentAction(
            thought       = data.get("thought") or data.get("diagnosis") or "recovery action",
            action        = data.get("action", "done"),
            element_index = _coerce_index(data.get("element_index")),
            text          = data.get("text"),
            key           = data.get("key"),
            direction     = data.get("direction", "down"),
            amount        = _coerce_amount(data.get("amount")),
            summary       = data.get("summary"),
        )
        log.info("Get-unstuck: %s (el=%s) — %s | %s",
                 rec.action, rec.element_index, rec.thought[:50],
                 (data.get("diagnosis") or "")[:80])
        return rec
    except Exception as exc:
        log.error("Get-unstuck pass failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# CAPTCHA detection and auto-solving
# ---------------------------------------------------------------------------

def _detect_captcha(page: Page) -> bool:
    """Return True if any CAPTCHA or bot-check is blocking the page."""
    try:
        # DataDome full-page interstitial — URL-based, always reliable
        if re.search(r'captcha-delivery\.com|geo\.captcha', page.url, re.I):
            return True

        # Page title keywords — strong signal, check early
        title = (page.title() or "").lower()
        if any(kw in title for kw in (
            "captcha", "robot", "unusual traffic", "are you human",
            "access denied", "bot check", "just a moment",
            "robot or human", "please confirm",
            "security check", "verify your identity", "let us know you're human",
        )):
            return True

        # Body text keywords — explicit challenge text always present on real CAPTCHA pages
        body = ""
        try:
            body = page.locator("body").inner_text(timeout=1_000).lower()
            if any(kw in body for kw in (
                "i'm not a robot", "verify you are human",
                "press & hold", "press and hold", "hold the button",
                "checking your browser", "confirm you're not a robot",
                "unusual activity", "security measure", "verify that you",
            )):
                return True
        except Exception:
            pass

        # reCAPTCHA / hCaptcha / DataDome iframes — only flag these when the page
        # has sparse content. Real CAPTCHA walls show almost nothing else; legitimate
        # pages (eBay results, etc.) embed reCAPTCHA for ad tracking and have thousands
        # of chars of content alongside.
        page_is_sparse = len(body) < 500
        if page_is_sparse:
            for frame in page.frames:
                if re.search(r'captcha|datadome|geo\.', frame.url or '', re.I):
                    return True
            for sel in (
                'iframe[src*="recaptcha"]',
                'iframe[src*="hcaptcha"]',
                'iframe[title*="captcha" i]',
            ):
                if page.locator(sel).count() > 0:
                    return True

        return False
    except Exception:
        return False


def _solve_captcha(page: Page) -> bool:
    """
    Offline CAPTCHA resolution pipeline:
      1. DataDome / generic press-and-hold
      2. reCAPTCHA / hCaptcha checkbox
      3. Audio challenge via Whisper (reCAPTCHA only)
      4. Passive wait for self-clearing CAPTCHAs (v3, Turnstile, Cloudflare)
    Returns True when CAPTCHA clears, False if we give up.
    """
    # Simulate blur → focus cycle that DataDome expects before interaction
    _emit_focus_events(page)
    time.sleep(max(0.01, random.gauss(1.2, 0.2)))

    # 1. Press-and-hold
    if _solve_press_hold(page):
        time.sleep(max(0.01, random.gauss(2.0, 0.3)))
        if not _detect_captcha(page):
            log.info("Press-and-hold CAPTCHA solved")
            return True
        # DataDome often shows a checkbox step after a successful hold
        log.info("CAPTCHA still present after hold — checking for follow-up checkbox")
        if _click_recaptcha_checkbox(page):
            time.sleep(max(0.01, random.gauss(2.0, 0.3)))
            if not _detect_captcha(page):
                log.info("Two-stage CAPTCHA (hold + checkbox) solved")
                return True

    # 2. reCAPTCHA / hCaptcha checkbox
    if _click_recaptcha_checkbox(page):
        log.info("Clicked CAPTCHA checkbox — waiting for evaluation")
        time.sleep(max(0.01, random.gauss(2.5, 0.4)))
        if not _detect_captcha(page):
            log.info("CAPTCHA passed on checkbox click")
            return True
        log.info("Challenge appeared after checkbox — trying audio route")

    # 3. Audio CAPTCHA via Whisper (reCAPTCHA only — guard against DataDome waste)
    if any(re.search(r'recaptcha', f.url or '') for f in page.frames):
        if _solve_audio_captcha(page):
            return True

    # 4. Passive wait (Cloudflare Turnstile, reCAPTCHA v3 self-clearing)
    deadline = time.time() + CAPTCHA_WAIT_S
    while time.time() < deadline:
        time.sleep(CAPTCHA_POLL_S)
        if not _detect_captcha(page):
            log.info("CAPTCHA self-cleared")
            return True

    log.error("CAPTCHA not resolved within %.0fs", CAPTCHA_WAIT_S)
    return False


def _solve_press_hold(page: Page) -> bool:
    """
    Handle DataDome-style 'press and hold' challenges.

    Waits up to 8 s for the challenge iframe to appear (it loads asynchronously
    after body-text detection fires), then searches every frame — not just
    URL-matched ones — so first-party proxied DataDome deployments (e.g.
    captcha.walmart.com) are covered.
    """
    # ── Wait up to 8 s for a captcha-specific sub-frame to appear ───────────
    captcha_frame = None
    deadline = time.time() + 8.0
    while time.time() < deadline:
        frames = page.frames
        log.info("Press-hold: %d frame(s): %s", len(frames), [f.url[:60] for f in frames])
        for frame in frames:
            if re.search(r'captcha|datadome|geo\.', frame.url or '', re.I):
                captcha_frame = frame
                break
        if captcha_frame:
            break
        time.sleep(0.5)

    # ── Scan the main frame first — Walmart /blocked serves challenge inline ─
    # Then scan any matched captcha sub-frame, then remaining non-blank frames.
    candidate_frames = [page.main_frame]
    if captcha_frame and captcha_frame is not page.main_frame:
        candidate_frames.append(captcha_frame)
    for frame in page.frames:
        if frame not in candidate_frames and (frame.url or '') not in ('', 'about:blank'):
            candidate_frames.append(frame)

    for frame in candidate_frames:
        log.info("Press-hold: scanning frame %s", (frame.url or '')[:80])
        time.sleep(max(0.01, random.gauss(0.5, 0.08)))
        try:
            # Broader selector — DataDome's hold element is often a div, not a button
            elems = frame.locator('button, [role="button"], div[class], a[class]').all()
            log.info("Press-hold: %d candidate elements in frame", len(elems))
            hold_el = None

            for el in elems:
                try:
                    if not el.is_visible(timeout=300):
                        continue
                    cls = (el.get_attribute('class') or '').lower()
                    txt = (el.inner_text() or '').strip().lower()
                    log.info("Press-hold candidate: tag=%s class=%r text=%r",
                             el.evaluate('e => e.tagName'), cls[:50], txt[:50])
                    # Match on text content (most reliable across DataDome versions)
                    if re.search(r'hold|press|click.+hold|press.+hold', txt):
                        hold_el = el
                        break
                    # Match on class name patterns
                    if re.search(r'hold|press|captcha|challenge', cls):
                        hold_el = el
                        break
                except Exception:
                    continue

            # Last resort: use text-selector Playwright built-in
            if not hold_el:
                for text_fragment in ('Hold', 'Press', 'Click & Hold', 'Press & Hold'):
                    try:
                        candidate = frame.get_by_text(text_fragment, exact=False).first
                        if candidate.is_visible(timeout=500):
                            hold_el = candidate
                            log.info("Press-hold: matched by text %r", text_fragment)
                            break
                    except Exception:
                        continue

            # Absolute last resort: largest visible element in the frame
            if not hold_el:
                largest_area = 0
                for el in elems:
                    try:
                        box = el.bounding_box()
                        if box and box['width'] * box['height'] > largest_area:
                            largest_area = box['width'] * box['height']
                            hold_el = el
                    except Exception:
                        continue
                if hold_el:
                    log.info("Press-hold: falling back to largest element (area=%.0f)", largest_area)

            if hold_el:
                box = hold_el.bounding_box()
                if box and box['width'] > 10 and box['height'] > 10:
                    _register_datadome_observers(page, frame)
                    time.sleep(max(0.01, random.gauss(0.4, 0.08)))
                    return _do_press_hold_coords(
                        page,
                        box["x"] + box["width"]  / 2,
                        box["y"] + box["height"] / 2,
                    )
        except Exception as exc:
            log.info("Press-hold: frame scan failed: %s", exc)

    # ── Diagnostic dump — log all visible text on the page ───────────────────
    try:
        body_text = page.inner_text('body', timeout=2_000)
        log.info("Press-hold: page body text (first 400 chars): %s", body_text[:400])
    except Exception:
        pass

    log.warning("Press-hold: no hold element found anywhere")
    return False


def _register_datadome_observers(page: Page, frame: Any) -> None:
    """
    Register ResizeObserver and IntersectionObserver on the challenge iframe.
    DataDome 4.x checks that these callbacks have fired before scoring the
    session — their complete absence is a bot signal.
    """
    try:
        page.evaluate("""(iframeEl) => {
            if (!iframeEl) return;
            if (window.ResizeObserver) {
                new ResizeObserver(() => {}).observe(iframeEl);
            }
            if (window.IntersectionObserver) {
                new IntersectionObserver(() => {}).observe(iframeEl);
            }
        }""", page.locator('iframe').first.element_handle())
    except Exception:
        pass


def _do_press_hold_coords(page: Page, cx: float, cy: float) -> bool:
    """
    Click and hold at (cx, cy) with a three-phase tremor model:
      Phase 1 (first 20%) — deliberate grip, small amplitude
      Phase 2 (middle 60%) — fatigue onset, growing amplitude
      Phase 3 (last 20%)  — anticipating release, settling down

    Inter-event timing uses log-normal distribution (right-skewed like real
    humans) with quiet periods of 200–500 ms.
    """
    try:
        _human_mouse_move(page, int(cx), int(cy))
        time.sleep(max(0.01, random.gauss(0.28, 0.06)))
        page.mouse.down()

        hold_s   = random.gauss(5.0, 0.5)   # DataDome/Walmart typically needs 4-6 s
        deadline = time.time() + hold_s
        elapsed  = 0.0
        drift_x  = cx
        drift_y  = cy

        while time.time() < deadline:
            frac = elapsed / hold_s

            # Three-phase amplitude
            if frac < 0.20:
                amp = random.gauss(0.4, 0.10)
            elif frac < 0.80:
                amp = random.gauss(0.4 + 1.4 * ((frac - 0.20) / 0.60), 0.20)
            else:
                amp = random.gauss(0.6, 0.15)
            amp = max(0.1, amp)

            # Occasional quiet period (no movement) — log-normal
            quiet_prob = 0.15
            if random.random() < quiet_prob:
                quiet_ms = random.expovariate(1 / 350)   # mean 350 ms
                time.sleep(min(quiet_ms / 1000, 0.5))
                elapsed += min(quiet_ms / 1000, 0.5)
                continue

            # Drift center slightly over time (natural hand drift)
            drift_x += random.gauss(0, 0.08)
            drift_y += random.gauss(0, 0.08)
            jx = drift_x + random.gauss(0, amp)
            jy = drift_y + random.gauss(0, amp)
            page.mouse.move(jx, jy)

            # Log-normal inter-event gap (right-skewed, median ~60 ms)
            gap_ms = math.exp(random.gauss(math.log(60), 0.4))
            gap_ms = max(25, min(gap_ms, 200))
            time.sleep(gap_ms / 1000)
            elapsed += gap_ms / 1000

        # Brief micro-drift before release (hand recoil)
        for _ in range(random.randint(2, 4)):
            page.mouse.move(
                drift_x + random.gauss(0, 1.5),
                drift_y + random.gauss(0, 1.5),
            )
            time.sleep(max(0.01, random.gauss(0.04, 0.01)))

        page.mouse.move(cx, cy)
        page.mouse.up()
        log.info("Press-and-hold: held for %.1fs", hold_s)
        return True
    except Exception as exc:
        log.debug("Press-and-hold failed: %s", exc)
        try:
            page.mouse.up()
        except Exception:
            pass
        return False


def _click_recaptcha_checkbox(page: Page) -> bool:
    """Click the 'I'm not a robot' checkbox inside the reCAPTCHA anchor iframe."""
    for sel in (
        'iframe[src*="recaptcha/api2/anchor"]',
        'iframe[src*="recaptcha"][src*="anchor"]',
        'iframe[title*="reCAPTCHA" i]',
    ):
        try:
            frame    = page.frame_locator(sel).first
            checkbox = frame.locator('#recaptcha-anchor').first
            if not checkbox.is_visible(timeout=1_500):
                continue
            time.sleep(max(0.01, random.gauss(0.6, 0.15)))
            checkbox.click()
            return True
        except Exception:
            continue
    return False


# Whisper model singleton — loaded once, stays in memory
_whisper_model = None


def _get_whisper() -> "Any":
    global _whisper_model
    if _whisper_model is None:
        import whisper
        log.info("Loading Whisper 'base' model for audio CAPTCHA solving…")
        _whisper_model = whisper.load_model("base")
        log.info("Whisper model ready")
    return _whisper_model


def _solve_audio_captcha(page: Page) -> bool:
    """
    Switch to the audio CAPTCHA challenge, download the clip, transcribe with
    Whisper, and submit the answer. Works for reCAPTCHA v2 — guard the caller
    with a frame-URL check before calling this for DataDome targets.
    """
    import tempfile, os, urllib.request

    challenge_sels = (
        'iframe[src*="recaptcha/api2/bframe"]',
        'iframe[src*="recaptcha"][src*="bframe"]',
        'iframe[title*="recaptcha challenge" i]',
    )

    for sel in challenge_sels:
        try:
            frame = page.frame_locator(sel).first

            for audio_btn_sel in (
                '#recaptcha-audio-button', '.rc-button-audio', '[aria-label*="audio" i]'
            ):
                try:
                    btn = frame.locator(audio_btn_sel).first
                    if btn.is_visible(timeout=1_500):
                        btn.click()
                        time.sleep(max(0.01, random.gauss(1.2, 0.2)))
                        break
                except Exception:
                    continue

            src = None
            for audio_sel in (
                '#audio-source', '.rc-audiochallenge-tdownload-link', '[href*=".mp3"]'
            ):
                try:
                    el  = frame.locator(audio_sel).first
                    src = (
                        el.get_attribute('src',  timeout=2_000)
                        or el.get_attribute('href', timeout=500)
                    )
                    if src:
                        break
                except Exception:
                    continue

            if not src:
                continue

            log.info("Audio CAPTCHA: downloading clip from %s", src[:60])
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            tmp.close()
            try:
                req = urllib.request.Request(
                    src,
                    headers={"User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.6367.243 Safari/537.36"
                    )},
                )
                with urllib.request.urlopen(req) as resp:
                    tmp_path = tmp.name
                    with open(tmp_path, 'wb') as f:
                        f.write(resp.read())
                model  = _get_whisper()
                result = model.transcribe(tmp_path, language="en", fp16=False)
                answer = result["text"].strip().lower()
                answer = "".join(c for c in answer if c.isalnum() or c == " ").strip()
                log.info("Whisper transcription: %r", answer)
            finally:
                try:
                    os.unlink(tmp.name)
                except Exception:
                    pass

            if not answer:
                continue

            for input_sel in (
                '#audio-response',
                '.rc-audiochallenge-response-field input',
                '[aria-label*="answer" i]',
            ):
                try:
                    inp = frame.locator(input_sel).first
                    if inp.is_visible(timeout=1_500):
                        inp.fill(answer)
                        time.sleep(max(0.01, random.gauss(0.5, 0.1)))
                        break
                except Exception:
                    continue

            for verify_sel in (
                '#recaptcha-verify-button', '.rc-button-default', 'button[type="submit"]'
            ):
                try:
                    btn = frame.locator(verify_sel).first
                    if btn.is_visible(timeout=1_000):
                        btn.click()
                        break
                except Exception:
                    continue

            time.sleep(max(0.01, random.gauss(2.5, 0.4)))
            if not _detect_captcha(page):
                log.info("Audio CAPTCHA solved")
                return True

        except Exception as exc:
            log.debug("Audio CAPTCHA attempt failed (%s): %s", sel, exc)
            continue

    return False


# ---------------------------------------------------------------------------
# DOM snapshot
# ---------------------------------------------------------------------------

_SNAPSHOT_JS = """() => {
    const tags = [
        'input','button','select','textarea','a',
        '[role=button]','[role=link]','[role=checkbox]',
        '[role=tab]','[role=menuitem]','[role=option]','[role=radio]',
    ];
    const seen = new Set();
    const raw  = [];
    let selSeq = 0;

    document.querySelectorAll(tags.join(',')).forEach(el => {
        if (seen.has(el)) return;
        seen.add(el);
        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) return;
        if (window.getComputedStyle(el).display === 'none') return;

        // Build a label: prefer ARIA/placeholder, then visible text, then DOM attrs
        let label = (
            el.getAttribute('aria-label') ||
            el.getAttribute('placeholder') ||
            el.getAttribute('title') ||
            el.innerText?.trim().slice(0, 80) ||
            el.getAttribute('name') ||
            el.getAttribute('id') || ''
        ).trim();

        // For checkboxes/radios: also grab the associated <label> text as context
        if (!label && (el.type === 'checkbox' || el.type === 'radio')) {
            const assoc = el.id
                ? document.querySelector('label[for="' + el.id + '"]')
                : el.closest('label');
            if (assoc) label = assoc.innerText?.trim().slice(0, 80) || '';
        }

        const viewH  = window.innerHeight;
        const viewW  = window.innerWidth;
        // Is the element's centre currently visible in the viewport?
        const cx = r.x + r.width  / 2;
        const cy = r.y + r.height / 2;
        const inViewport = cx >= 0 && cx <= viewW && cy >= 0 && cy <= viewH;
        // Is this element inside a header/nav band (top 80px of viewport)?
        const inNav = r.y < 80 && !!el.closest('header, nav, [role=banner], [role=navigation]');

        // Native <select>: record its options + selected text and stamp a stable id so
        // the 'select' action can drive it with select_option (a synthetic click opens
        // the OS-rendered dropdown, which no automation can see — clicks CANNOT work).
        let options = null, selectedText = '', selId = null;
        if (el.tagName === 'SELECT') {
            options = [...el.options].map(o => (o.text || '').trim()).filter(Boolean).slice(0, 12);
            selectedText = (el.selectedOptions[0]?.text || '').trim();
            selId = String(++selSeq);   // restamped fresh every snapshot — the select
            el.dataset.wwSel = selId;   // action executes against THIS snapshot's ids
        }

        raw.push({
            tag:        el.tagName.toLowerCase(),
            type:       el.getAttribute('type') || el.getAttribute('role') || '',
            label:      label,
            value:      (el.type === 'checkbox' || el.type === 'radio')
                            ? String(el.checked)
                            : (el.tagName === 'SELECT' ? selectedText : (el.value || '')),
            href:       el.getAttribute('href') || '',
            cx:         Math.round(cx),
            cy:         Math.round(cy),
            focused:    el === document.activeElement,
            inViewport: inViewport,
            inNav:      inNav,
            options:    options,
            sel_id:     selId,
        });
    });

    // Sort priority:
    //   0 — visible text/search inputs (always first so the model finds them at index 0-3)
    //   1 — visible non-nav interactive elements
    //   2 — visible nav elements (header/banner)
    //   3 — off-screen / below fold
    const NON_TEXT_INPUT_TYPES = new Set([
        'checkbox','radio','submit','button','hidden','file','image','color','range',
    ]);
    raw.sort((a, b) => {
        const aIsText = (a.tag === 'input' || a.tag === 'textarea')
            && a.inViewport && !NON_TEXT_INPUT_TYPES.has(a.type);
        const bIsText = (b.tag === 'input' || b.tag === 'textarea')
            && b.inViewport && !NON_TEXT_INPUT_TYPES.has(b.type);
        if (aIsText !== bIsText) return aIsText ? -1 : 1;

        const rankA = a.inViewport ? (a.inNav ? 1 : 0) : 2;
        const rankB = b.inViewport ? (b.inNav ? 1 : 0) : 2;
        if (rankA !== rankB) return rankA - rankB;
        return a.cy - b.cy;   // top-to-bottom within each group
    });

    // Assign final indices after sort, drop sort keys
    return raw.slice(0, 60).map((el, i) => {
        el.index = i;
        delete el.inNav;
        return el;
    });
}"""


def _snapshot_elements(page: Page) -> list[dict]:
    try:
        return page.evaluate(_SNAPSHOT_JS) or []
    except Exception as exc:
        log.warning("Snapshot failed: %s", exc)
        return []


_NON_TEXT_INPUT_TYPES = frozenset(
    ("checkbox", "radio", "submit", "button", "hidden", "file", "image", "color", "range")
)


def _elements_text(elements: list[dict]) -> str:
    lines = []
    last_group: str | None = None
    for e in elements:
        group = "ON SCREEN" if e.get("inViewport") else "BELOW FOLD (scroll to reach)"
        if group != last_group:
            lines.append(f"\n  -- {group} --")
            last_group = group

        tag   = e["tag"]
        etype = (e.get("type") or "").lower()
        label = e["label"] or "(no label)"
        href  = f"  → {e['href']}" if e["href"] else ""
        focused = "  [FOCUSED]" if e.get("focused") else ""

        if etype in ("checkbox", "radio"):
            val  = " [CHECKED]" if e.get("value") == "true" else " [unchecked]"
            lines.append(f"  [{e['index']}] {etype.upper()}  {label}{val}{focused}")

        elif tag in ("input", "textarea") and etype not in _NON_TEXT_INPUT_TYPES:
            # Prominently mark text fields — this is what the agent types into
            cur = f'  (current: {e["value"]!r})' if e.get("value") else ""
            lines.append(f"  [{e['index']}] >>> TEXT INPUT <<<  \"{label}\"{cur}{focused}  (use type + this index)")

        elif tag == "button" or etype in ("submit", "button"):
            lines.append(f"  [{e['index']}] BUTTON  \"{label}\"{focused}")

        elif tag == "a":
            lines.append(f"  [{e['index']}] LINK  \"{label}\"{href}{focused}")

        elif tag == "select":
            cur = f"  (selected: {e['value']!r})" if e.get("value") else ""
            opts = e.get("options") or []
            opt_str = ("  options: " + " | ".join(opts)) if opts else ""
            lines.append(f"  [{e['index']}] DROPDOWN  \"{label}\"{cur}{opt_str}"
                         f"{focused}  (use select + this index + exact option text)")

        else:
            kind = f"{tag}[{etype}]" if etype else tag
            lines.append(f"  [{e['index']}] {kind}  \"{label}\"{href}{focused}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM interaction
# ---------------------------------------------------------------------------

def _query_llm(
    model:         str,
    instruction:   str,
    page:          Page,
    elements:      list[dict],
    history:       list[AgentAction],
    scratchpad:    dict[str, str] | None = None,
    vision_model:  str | None = None,
    ocr_threshold: int = 200,
    page_context:  str = "",
) -> AgentAction:
    history_text = ""
    for i, a in enumerate(history[-10:]):
        history_text += f"  step {i+1}: {a.action}"
        if a.action == "remember":
            history_text += f" {a.memory_key!r}={a.memory_value!r}"
        elif a.element_index is not None:
            el = next((e for e in elements if e["index"] == a.element_index), None)
            if el:
                history_text += f" [{a.element_index}] {el['label']!r}"
        if a.text:
            history_text += f" text={a.text!r}"
        if a.key:
            history_text += f" key={a.key!r}"
        if a.thought:
            history_text += f"  — thought: {a.thought[:60]}"
        if a.outcome:
            history_text += f"\n          ↳ RESULT: {a.outcome}"
        history_text += "\n"

    # Page text snippet — what a human would read on screen
    try:
        page_text_raw = page.inner_text("body", timeout=2_000) or ""
        page_snippet  = " ".join(page_text_raw.split())[:1500]
    except Exception:
        page_text_raw = ""
        page_snippet  = ""

    # OCR fallback: if DOM text is sparse AND we don't already have vision context,
    # use the vision model to read the page content directly
    ocr_used = False
    if len(page_snippet) < ocr_threshold and vision_model and not page_context:
        try:
            log.info("Page text sparse (%d chars) — using vision model for OCR", len(page_snippet))
            screenshot_bytes = page.screenshot(type="jpeg", quality=80)
            import base64
            img_b64 = base64.b64encode(screenshot_bytes).decode()
            ocr_payload = {
                "model":    vision_model,
                "messages": [{
                    "role":    "user",
                    "content": (
                        "Read this web page screenshot and list all text you can clearly see. "
                        "Include: prices, product names, headings, button labels, form field labels, "
                        "error messages, and any other readable text. "
                        "Also note: is there a CAPTCHA, login wall, or access-denied message? "
                        "Format as a bullet list. Only include text you can clearly read."
                    ),
                    "images":  [img_b64],
                }],
                "stream": False,
                "options": {"num_ctx": _VISION_NUM_CTX},
            }
            with httpx.Client(timeout=60.0) as client:
                r = client.post(f"{OLLAMA_URL}/api/chat", json=ocr_payload)
                r.raise_for_status()
            ocr_text = r.json()["message"]["content"].strip()
            page_snippet = f"[OCR via {vision_model}]: {ocr_text[:1500]}"
            ocr_used = True
            log.info("OCR result (%d chars): %s…", len(ocr_text), ocr_text[:120])
        except Exception as exc:
            log.warning("Vision OCR fallback failed: %s", exc)

    # Scratchpad section
    if scratchpad:
        mem_lines = "\n".join(f"  {k}: {v}" for k, v in scratchpad.items())
        scratchpad_section = f"Your working memory (facts saved so far):\n{mem_lines}\n\n"
    else:
        scratchpad_section = "Your working memory: (empty — use 'remember' to save key facts)\n\n"

    page_text_label = "Page text (from visual OCR):" if ocr_used else "Page text (excerpt):"
    vision_section = (
        f"Page layout and content (from visual scan):\n{page_context}\n\n"
        if page_context else ""
    )

    # Detect visible text/search inputs and call them out explicitly.
    # This prevents the model from guessing a link element when a real input exists.
    _text_inputs = [
        e for e in elements
        if e.get("tag") in ("input", "textarea")
        and (e.get("type") or "").lower() not in _NON_TEXT_INPUT_TYPES
        and e.get("inViewport")
    ]
    if _text_inputs:
        _tf_lines = [
            f"  element_index={e['index']} — \"{e.get('label') or '(text field)'}\""
            + (" [FOCUSED]" if e.get("focused") else "")
            for e in _text_inputs[:4]
        ]
        text_field_hint = (
            "\nTEXT FIELDS ON THIS PAGE (use 'type' with one of these indices to enter text):\n"
            + "\n".join(_tf_lines) + "\n"
        )
    else:
        text_field_hint = ""

    user_msg = (
        f"Goal: {instruction}\n\n"
        f"Current page: {page.title() or '(no title)'}\n"
        f"URL: {page.url}\n\n"
        + vision_section
        + f"{page_text_label}\n{page_snippet}\n\n"
        f"Interactive elements:\n{_elements_text(elements)}\n\n"
        + scratchpad_section
        + (f"Previous actions:\n{history_text}" if history_text else "No previous actions yet.")
        + text_field_hint
        + "\n\nWhat is your next action?"
    )

    payload = {
        "model":    model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user",   "content": user_msg},
        ],
        "stream": False,
        # format=json constrains output to a single valid JSON object. This is the
        # reliable path across all model tiers (qwen2.5:14b down to qwen2.5:3b on
        # weaker machines). Capable instruct models still fill the 'thought' field
        # in this mode; weaker ones at least produce parseable output every time.
        "format": "json",
    }

    with httpx.Client(timeout=OLLAMA_TIMEOUT) as client:
        r = client.post(f"{OLLAMA_URL}/api/chat", json=payload)
        r.raise_for_status()

    raw = r.json()["message"]["content"]
    # Defensive parse: format=json should give clean JSON, but if a model wraps it
    # in prose or code fences, extract the outermost {...} object.
    try:
        data: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find('{')
        end   = raw.rfind('}')
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"No JSON object in LLM response: {raw[:200]!r}")
        data = json.loads(raw[start:end + 1])

    return AgentAction(
        thought       = data.get("thought", ""),
        action        = data.get("action", "done"),
        element_index = _coerce_index(data.get("element_index")),
        text          = data.get("text"),
        key           = data.get("key"),
        direction     = data.get("direction", "down"),
        amount        = _coerce_amount(data.get("amount")),
        summary       = data.get("summary"),
        memory_key    = data.get("memory_key"),
        memory_value  = str(data.get("memory_value", "")) if data.get("memory_value") is not None else None,
    )


def _coerce_amount(v, default: int = 300) -> int:
    """Best-effort scroll amount → int. The model sometimes returns a percentage ('50%'),
    a float, or junk — pull the digits, default when there are none. NEVER raises (a bad
    value here used to crash the whole agent step with int('50%'))."""
    if isinstance(v, bool) or v is None:
        return default
    if isinstance(v, (int, float)):
        return int(v)
    m = re.search(r"\d+", str(v))
    return int(m.group()) if m else default


def _coerce_index(v) -> Optional[int]:
    """Best-effort element_index → int. The model sometimes echoes the display format
    back ("[29]", "29", 29.0, [29]) — all of those mean 29; only true garbage is None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, list) and len(v) == 1:
        return _coerce_index(v[0])
    if isinstance(v, str):
        m = re.search(r"\d+", v)
        if m:
            return int(m.group())
    return None


# ---------------------------------------------------------------------------
# Action execution with human-like timing
# ---------------------------------------------------------------------------

def _execute(page: Page, action: AgentAction, elements: list[dict]) -> None:
    a = action.action

    if a == "click":
        el = _require_element(action, elements)
        _human_mouse_move(page, el["cx"], el["cy"])
        # Hover dwell before pressing down — humans pause 80-300 ms over targets
        tag = el.get("tag", "")
        dwell = (
            random.gauss(0.22, 0.05) if tag in ("button", "a")
            else random.gauss(0.12, 0.03)
        )
        time.sleep(max(0.06, dwell))
        page.mouse.down()
        time.sleep(max(0.01, random.gauss(0.095, 0.020)))   # realistic down-duration
        page.mouse.up()
        _wait_for_settle(page)

    elif a == "type":
        if action.element_index is not None:
            el = _require_element(action, elements)
            el_tag = el.get("tag", "").lower()
            # Only click to focus if the target is a text input or textarea.
            # Clicking links (<a>) or buttons navigates/submits rather than
            # focusing for input, which would cause unwanted navigation.
            is_text_input = el_tag in ("input", "textarea")
            already_focused = el.get("focused", False) or not is_text_input
            if not is_text_input:
                log.warning(
                    "Agent 'type' targeting non-input element (tag=%r, label=%r) "
                    "— skipping focus click to avoid unintended navigation",
                    el_tag, el.get("label", "")[:40],
                )
                # Set outcome immediately so the main loop's auto-Enter check
                # sees a non-"page unchanged" outcome and doesn't fire Enter.
                # Also give the model direct corrective feedback.
                action.outcome = (
                    f"REJECTED: element_index={action.element_index} is a {el_tag.upper()}, "
                    f"not a text field. 'type' only works on >>> TEXT INPUT <<< elements. "
                    "Look at the element list for an element marked '>>> TEXT INPUT <<<' and use its index."
                )
                return  # skip the actual type — nothing useful to type into
            if not already_focused:
                _human_mouse_move(page, el["cx"], el["cy"])
                time.sleep(max(0.01, random.gauss(0.15, 0.03)))
                page.mouse.down()
                time.sleep(max(0.01, random.gauss(0.08, 0.02)))
                page.mouse.up()
                time.sleep(max(0.01, random.gauss(0.12, 0.03)))
        # Clear whatever is already in the field — but ONLY if an input/textarea
        # is actually focused. If focus was lost, Ctrl+A would select the whole page.
        focused_tag = page.evaluate(
            "() => document.activeElement ? document.activeElement.tagName : ''"
        )
        if focused_tag in ("INPUT", "TEXTAREA"):
            page.keyboard.press("Control+a")
            time.sleep(max(0.01, random.gauss(0.04, 0.01)))
            page.keyboard.press("Delete")
            time.sleep(max(0.01, random.gauss(0.04, 0.01)))
        _human_type(page, action.text or "")

    elif a == "press":
        key = action.key or "Enter"
        page.keyboard.press(key)
        _wait_for_settle(page)

    elif a == "navigate":
        url = (action.text or "").strip()
        if url and not url.startswith("http"):
            # Model sent a search query instead of a URL — reject it cleanly
            raise ValueError(
                f"navigate requires a full URL starting with http(s)://, got: {url[:60]!r}. "
                "Use the 'type' action to enter a search query."
            )
        if url:
            log.info("Agent navigating to: %s", url)
            page.goto(url, timeout=30_000, wait_until="domcontentloaded")
            _emit_focus_events(page)
            _wait_for_settle(page)

    elif a == "select":
        # Choose an option in a native <select>. A synthetic click on one opens the
        # OS-rendered dropdown that no automation can see, so this is the ONLY way the
        # agent can change a sort order / category filter implemented as a real select.
        el = _require_element(action, elements)
        if el.get("tag") != "select" or not el.get("sel_id"):
            action.outcome = (
                f"REJECTED: element_index={action.element_index} is not a DROPDOWN. "
                "'select' only works on elements marked DROPDOWN."
            )
            return
        want = (action.text or "").strip()
        if not want:
            action.outcome = "REJECTED: 'select' needs text = the exact option text to choose."
            return
        # Look like a person reaching for the control before the value changes.
        _human_mouse_move(page, el["cx"], el["cy"])
        time.sleep(max(0.06, random.gauss(0.18, 0.04)))
        picked = page.evaluate(
            """([selId, want]) => {
                const sel = document.querySelector('select[data-ww-sel="' + selId + '"]');
                if (!sel) return null;
                const opts = [...sel.options];
                const match = opts.find(o => (o.text || '').trim() === want)
                    || opts.find(o => (o.text || '').trim().toLowerCase() === want.toLowerCase())
                    || opts.find(o => (o.text || '').toLowerCase().includes(want.toLowerCase()));
                if (!match) return null;
                sel.value = match.value;
                sel.dispatchEvent(new Event('input',  { bubbles: true }));
                sel.dispatchEvent(new Event('change', { bubbles: true }));
                return (match.text || '').trim();
            }""",
            [el["sel_id"], want],
        )
        if picked is None:
            opts = ", ".join(el.get("options") or [])
            action.outcome = (f"REJECTED: no option matching {want!r} in this dropdown. "
                              f"Its options are: {opts}")
            return
        action.outcome = f"selected {picked!r}"
        _wait_for_settle(page)

    elif a == "scroll":
        dist = action.amount if action.direction == "down" else -action.amount
        _inertia_scroll(page, dist)

    else:
        log.warning("Agent: unknown action %r — skipping", a)


def _require_element(action: AgentAction, elements: list[dict]) -> dict:
    idx = action.element_index
    if idx is None:
        raise ValueError("action requires element_index but none provided")
    el = next((e for e in elements if e["index"] == idx), None)
    if el is None:
        raise ValueError(f"element index {idx} not found in snapshot")
    return el


def _human_mouse_move(page: Page, tx: int, ty: int) -> None:
    """
    Move mouse along a multi-segment curved path with:
    - Log-normal inter-event timing (right-skewed like real humans)
    - Quadratic bezier per segment with independent control points
    - Micro-pause between segments
    - Overshoot + 1-2 correction moves before landing
    """
    sx = getattr(_human_mouse_move, "_x", 640)
    sy = getattr(_human_mouse_move, "_y", 400)

    dist = math.hypot(tx - sx, ty - sy)
    if dist < 2:
        return

    # Split into 2-4 sub-segments
    n_segs = random.randint(2, 4)
    # Generate intermediate waypoints along a gently curved overall path
    waypoints = [(float(sx), float(sy))]
    for i in range(1, n_segs):
        frac = i / n_segs
        wx = sx + (tx - sx) * frac + random.gauss(0, dist * 0.06)
        wy = sy + (ty - sy) * frac + random.gauss(0, dist * 0.06)
        waypoints.append((wx, wy))
    waypoints.append((float(tx), float(ty)))

    for seg in range(n_segs):
        p0x, p0y = waypoints[seg]
        p1x, p1y = waypoints[seg + 1]
        seg_dist  = math.hypot(p1x - p0x, p1y - p0y)

        steps = max(6, min(int(seg_dist / 10), 30))

        # Bezier control point for this segment
        mid_x = (p0x + p1x) / 2
        mid_y = (p0y + p1y) / 2
        perp_x = -(p1y - p0y)
        perp_y  =  (p1x - p0x)
        plen    = math.hypot(perp_x, perp_y) or 1
        arc     = random.gauss(0, 0.15)
        cp_x    = mid_x + (perp_x / plen) * seg_dist * arc
        cp_y    = mid_y + (perp_y / plen) * seg_dist * arc

        for i in range(1, steps + 1):
            t  = i / steps
            te = t * t * (3 - 2 * t)   # smooth-step ease-in/out
            bx = (1-te)**2 * p0x + 2*(1-te)*te * cp_x + te**2 * p1x
            by = (1-te)**2 * p0y + 2*(1-te)*te * cp_y + te**2 * p1y
            noise = max(0.15, 1 - t) * 1.5
            page.mouse.move(bx + random.gauss(0, noise), by + random.gauss(0, noise))
            # Log-normal timing: median ~12 ms, right-skewed long tail
            gap_ms = math.exp(random.gauss(math.log(12), 0.4))
            time.sleep(max(4, min(gap_ms, 80)) / 1000)

        # Micro-pause between segments
        if seg < n_segs - 1:
            time.sleep(max(0.01, random.gauss(0.018, 0.006)))

    # Overshoot target and correct back (Fitts-Law approach dynamics)
    overshoot = random.gauss(5, 2)
    dx = tx - sx
    dy = ty - sy
    dl = math.hypot(dx, dy) or 1
    ox = tx + (dx / dl) * overshoot
    oy = ty + (dy / dl) * overshoot
    page.mouse.move(ox, oy)
    time.sleep(max(0.01, random.gauss(0.055, 0.015)))

    # 1-2 correction micro-moves back to the exact target
    for _ in range(random.randint(1, 2)):
        fx = tx + random.gauss(0, 0.8)
        fy = ty + random.gauss(0, 0.8)
        page.mouse.move(fx, fy)
        time.sleep(max(0.01, random.gauss(0.025, 0.008)))

    page.mouse.move(tx, ty)
    _human_mouse_move._x = tx   # type: ignore[attr-defined]
    _human_mouse_move._y = ty   # type: ignore[attr-defined]


def _human_type(page: Page, text: str) -> None:
    """
    Type one key at a time with human-like, slightly irregular timing — fast enough
    not to stall a run, varied enough not to look like an instant paste (a bot tell).
    Roughly 100 ms/key with jitter, plus the occasional brief 'think' pause. A short
    search term lands in about a second; this is bounded so it never stalls.
    """
    for ch in text:
        page.keyboard.type(ch)
        delay = min(max(abs(random.gauss(0.10, 0.04)), 0.03), 0.28)  # ~30–280 ms
        if random.random() < 0.06:                 # ~6% of keys: a brief pause
            delay += random.uniform(0.15, 0.40)
        time.sleep(delay)


def _inertia_scroll(page: Page, dist: int) -> None:
    """
    Scroll with an inertia-decay burst — like a trackpad or inertia wheel.
    Emits a series of wheel events with decreasing deltas instead of a single
    large pulse, which is what DataDome's behavioral model expects.
    """
    if dist == 0:
        return
    direction = 1 if dist > 0 else -1
    target    = abs(dist)
    velocity  = random.uniform(80, 160)   # px/event initial velocity
    decay     = random.uniform(0.70, 0.85)
    delta     = velocity
    emitted   = 0

    while delta >= 4 and emitted < target:
        tick = min(delta, target - emitted)
        page.mouse.wheel(0, direction * tick)
        emitted += tick
        gap_ms   = math.exp(random.gauss(math.log(22), 0.3))
        time.sleep(max(8, gap_ms) / 1000)
        delta   *= decay


def _emit_focus_events(page: Page) -> None:
    """
    Dispatch focus / visibilitychange events that DataDome expects before
    a session begins interacting.  Called after navigation and before CAPTCHA.
    """
    try:
        page.evaluate("""() => {
            try {
                Object.defineProperty(document, 'visibilityState',
                    {get: () => 'visible', configurable: true});
                window.dispatchEvent(new Event('focus'));
                document.dispatchEvent(new Event('visibilitychange'));
            } catch(e) {}
        }""")
    except Exception:
        pass


def _describe_page(page: Page, vision_model: str) -> str:
    """
    Take a screenshot and ask the vision model to describe the page layout and
    interactive controls. Called once per navigation so the agent understands
    what it's looking at before deciding what to do.
    """
    import base64
    try:
        log.info("Vision scan: describing page at %s", page.url[:60])
        screenshot_bytes = page.screenshot(type="jpeg", quality=75)
        img_b64 = base64.b64encode(screenshot_bytes).decode()
        payload = {
            "model":    vision_model,
            "messages": [{
                "role":    "user",
                "content": (
                    "Answer these 5 questions about this web page screenshot. "
                    "Be brief and specific. Only describe what you can clearly see.\n\n"
                    "1. PAGE TYPE: Is this a homepage, search results, product listing, "
                    "login page, CAPTCHA, error page, or something else?\n"
                    "2. SEARCH BOX: Is there a visible search or text input field? "
                    "If yes, where is it and what placeholder text does it show?\n"
                    "3. PRICES / NUMBERS: List any prices, ratings, or quantities you can clearly read. "
                    "Quote exact text (e.g. '$165.00', '4.5 stars', '247 results').\n"
                    "4. HEADINGS / TITLES: What are the main headings or product/listing titles visible?\n"
                    "5. BLOCKERS: Is there a popup, cookie banner, CAPTCHA, or overlay covering the page?\n\n"
                    "Answer each numbered question in 1-2 lines. "
                    "If you cannot clearly read something, write 'not visible'."
                ),
                "images":  [img_b64],
            }],
            "stream": False,
            "options": {"num_ctx": _VISION_NUM_CTX},
        }
        with httpx.Client(timeout=60.0) as client:
            r = client.post(f"{OLLAMA_URL}/api/chat", json=payload)
            r.raise_for_status()
        description = r.json()["message"]["content"].strip()
        log.info("Vision description (%d chars): %s…", len(description), description[:150])
        # Discard descriptions that are vague or admit uncertainty — they add noise
        # rather than helping the agent, and confuse decisions more than no context.
        _uncertainty = (
            "not possible", "cannot determine", "unable to determine",
            "it is unclear", "cannot be determined", "not clear",
        )
        if any(ph in description.lower() for ph in _uncertainty) or len(description) < 80:
            log.info("Vision description too vague — discarding to avoid confusing agent")
            return ""
        return description
    except Exception as exc:
        log.warning("Vision page description failed: %s", exc)
        return ""


def _url_path(url: str) -> str:
    """Return netloc + path, stripping query params and fragments."""
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        return f"{p.netloc}{p.path}".rstrip("/")
    except Exception:
        return url


def _action_outcome(pre_url: str, post_url: str, pre_title: str, post_title: str) -> str:
    """Summarise what observably changed after an action."""
    if post_url != pre_url:
        return f"navigated → {post_url[:80]}"
    if post_title != pre_title:
        return f"title changed: {pre_title[:30]!r} → {post_title[:30]!r}"
    return "page unchanged"


def _wait_for_settle(page: Page) -> None:
    """Wait briefly for page navigation or DOM updates to settle."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5_000)
    except PWTimeoutError:
        pass
    time.sleep(random.uniform(*PAGE_SETTLE))


def _human_pause(min_s: float = 0.4, max_s: float = 1.1) -> None:
    """Short random pause between agent actions for natural pacing."""
    mid = (min_s + max_s) / 2
    std = (max_s - min_s) / 4
    time.sleep(max(min_s, random.gauss(mid, std)))


def _extract_text(page: Page) -> str:
    try:
        return page.inner_text("body", timeout=10_000)
    except Exception:
        return ""
