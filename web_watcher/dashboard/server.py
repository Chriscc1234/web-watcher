"""
FastAPI dashboard server.

Endpoints
---------
GET  /api/health
GET  /api/status                    service statuses (+ Ollama models)
POST /api/services/{name}/start|stop|restart
GET  /api/watches                   list watches + last-run + next-run
POST /api/watches                   create a watch
PUT  /api/watches/{name}            update a watch
DELETE /api/watches/{name}          delete a watch
POST /api/watches/{name}/run        manual trigger
GET  /api/history[?watch=&limit=]   run history
GET  /api/schedule                  next-run times
GET  /api/oversight                 The Watcher's live narration feed
POST /api/oversight/chat            talk to The Watcher (the single AI)
POST /api/oversight/action          run a one-click fix it offered
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

if TYPE_CHECKING:
    from web_watcher.services import ServiceManager

STATIC_DIR      = Path(__file__).parent / "static"
OLLAMA_URL      = "http://localhost:11434"
_KNOWN_SERVICES = {"ollama", "server", "scheduler"}

log = logging.getLogger(__name__)

# Progress of a Settings → "Re-scan hardware" model download (a hardware upgrade can bump the
# model tier, whose new models must be pulled). Shared, single-flight; surfaced via /api/system/specs.
_rescan_state: dict = {"status": "idle", "detail": "", "models": []}


def _model_size_mb(name: str):
    """Approximate download size for a model tag (None if we don't know it)."""
    from web_watcher.gpu_detect import model_size_mb
    return model_size_mb(name)


def _installed_models() -> dict[str, int]:
    """{model name: real size in MB} for every model Ollama has on disk. {} if Ollama is down."""
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(f"{OLLAMA_URL}/api/tags")
            r.raise_for_status()
            return {m["name"]: int(m.get("size", 0) / 1e6)
                    for m in (r.json().get("models") or []) if m.get("name")}
    except Exception as exc:
        log.debug("could not list installed models: %s", exc)
        return {}


def _apply_new_models_bg(rec: dict) -> None:
    """Download the recommended models, then — ONLY once they're all present — switch the config
    to them. Pulling first and switching last means the app keeps using the current (working)
    model for the minutes the download takes, instead of pointing at a model that isn't on disk
    yet (which would break every chat/sweep until the pull finished). Runs in a background thread;
    updates _rescan_state so the UI can show progress. Ollama caches layers, so re-pulls are cheap."""
    models = [m for m in (rec.get("text_model"), rec.get("vision_model"),
                          rec.get("council_model")) if m]
    models = list(dict.fromkeys(models))   # dedupe, keep order
    _rescan_state.update(status="pulling", detail="Downloading updated AI models…", models=list(models))
    try:
        for m in models:
            with httpx.Client(timeout=None) as client:
                with client.stream("POST", f"{OLLAMA_URL}/api/pull", json={"name": m}) as r:
                    r.raise_for_status()
                    for _ in r.iter_lines():
                        pass   # drain the progress stream until the pull completes
        # Every model is now on disk — safe to switch. Re-load so we don't clobber a config the
        # user edited during the download, then persist. New model is picked up on the next
        # chat turn / sweep (both read cfg.models fresh each time).
        from web_watcher.config import load, save, lock as _cfg_lock
        cfg = load()
        cfg.models.text_model    = rec.get("text_model") or cfg.models.text_model
        cfg.models.vision_model  = rec.get("vision_model") or ""
        cfg.models.council_model = rec.get("council_model") or rec.get("text_model") or ""
        save(cfg)
        log.info("re-scan applied models: %s", cfg.models.text_model)
        _rescan_state.update(status="done",
                             detail=f"Updated — now using {cfg.models.text_model}.", models=[])
    except Exception as exc:
        # Leave the config on the OLD (working) model so the app keeps functioning.
        log.warning("re-scan model pull failed: %s", exc)
        _rescan_state.update(status="error",
                             detail=f"Model download failed (still using your current model): {exc}")

_CHAT_SYSTEM_BASE = """\
You are the built-in assistant for Web Watcher — a personal, fully offline AI \
app that monitors websites and takes autonomous actions on them. Everything runs \
locally using Ollama. No cloud. No subscriptions.

════════════════════════════════════════
HOW WEB WATCHER WORKS
════════════════════════════════════════
Each "watch" is a job that runs on a schedule. It visits one or more URLs, and \
an AI model reads the page to decide if the user's condition is met. If it is, \
a notification is sent via Telegram and/or email.

════════════════════════════════════════
UNDERSTAND THE CONVERSATION (before any tool)
════════════════════════════════════════
Read what the person MEANS, in the flow of the conversation — not keywords.
  • A correction ("no wait, I meant sailboats only", "not that one") corrects their previous
    QUESTION or reference. Re-answer the corrected question. It is NOT a request to create or
    edit a watch unless they say so.
  • "The second one" / "that one" / "the cheaper one" refer to things YOU just showed or said.
    Use the numbered LISTINGS YOU JUST SHOWED block when present; never claim you lack context
    for something you said a moment ago.
  • A question is not an instruction: "what did you find today?" wants an answer from real
    data, not an offer to build something new. Propose a NEW watch only when they describe
    something they want tracked that no existing watch covers.
  • A message can ask two things ("which are running, and did any find anything?") — answer
    BOTH parts.
  • If a request genuinely contradicts itself and you cannot tell what they want, say what you
    understood and ask ONE short question.

════════════════════════════════════════
WHAT YOU CAN DO IN CHAT (your tools)
════════════════════════════════════════
When the user asks what you can do, answer plainly from this list — don't invent tools.
  • Set up a new watch, or change / rename an existing one.
  • Start, stop, pause, enable, disable, or delete an individual watch.
  • Look up what a watch has found and share the links + a quick verdict.
  • Report Claude spend for the month (when cloud is on).
  • Change how often YOU get quiet-period check-ins ("check in every 6 hours", "twice a day",
    "stop checking in") — each person has their own setting.
TWO LEVELS OF START/STOP — keep them straight:
  1. THE WHOLE WATCHER (a master switch): "pause everything" / "stop the whole Watcher" /
     "resume watching" pauses or resumes ALL watching for everyone. This is OWNER-ONLY.
  2. A PERSON'S OWN WATCHES: "stop my truck watch" / "pause my watches" affects only the
     watches that belong to whoever is talking. Everyone can do this to their own.
If someone who isn't the owner asks to stop the whole Watcher, tell them only the owner can
do that, but you can stop their own watches. If it's unclear whether they mean the whole
Watcher or just their watches, ask which before doing anything.

════════════════════════════════════════
TWO INDEPENDENT CHOICES: how it reads, and how often it runs
════════════════════════════════════════
Don't confuse these. Every watch picks one of each.

HOW IT READS THE PAGE (the "autonomous" field)
  SIMPLE (autonomous: false)
    Loads the page as-is and reads the text or takes a screenshot. Best for static \
    pages like weather forecasts, news sites, stock prices, or any page that shows \
    the information immediately without needing to click around.
  AUTONOMOUS AGENT (autonomous: true)
    A browser agent navigates the page like a human — clicking buttons, typing in \
    search boxes, scrolling, moving between pages. Best for searching (Google, eBay, \
    Craigslist) and multi-page research. Start it from the site HOMEPAGE and let it \
    search like a person would.

HOW OFTEN IT RUNS (the "mode" field)
  SCHEDULE (mode: "schedule") — the default
    Runs once every interval_minutes (or on a cron_expression), then stops until the \
    next scheduled time. Best for "check this every 30 min / every morning" tasks.
  CONTINUOUS (mode: "continuous")
    An always-on loop that watches a marketplace all day and alerts ONLY on listings \
    it has never seen before, then idles a few seconds and goes again — nonstop until \
    the user stops it. Built for feeds whose algorithm hides new items for hours/days, \
    so periodic checks miss them. Continuous watches do NOT auto-start — the user \
    presses Start on the watch. Use continuous for "watch this marketplace all day and \
    ping me the moment a matching item appears" (Facebook Marketplace, eBay, Craigslist).

    A continuous watch comes in two flavours via the "autonomous" field. BOTH read each \
    new listing's ad page for its attributes (transmission, drivetrain, mileage…) when a \
    judgment_prompt is set — so attribute filtering works either way. The difference is \
    only how listings are gathered:
      • autonomous: false  (PREFERRED for sites with a clean search URL) — a fast scraper \
        that loads the search-results URL, scrolls, and reads the listings directly. \
        Best for Craigslist and eBay, where the search URL already lists everything: it's \
        far faster and more reliable than the agent. Give it a search-results URL for what \
        the user wants (e.g. a Craigslist search for the item).
      • autonomous: true — the slower AI AGENT browses each sweep like a person \
        (scrolling/searching/clicking). Only worth it when the site CANNOT be reached \
        with a plain URL: e.g. Facebook Marketplace LOGGED IN (search behind a login), or \
        sites that require clicking/interaction to reveal listings.
    Default to autonomous: false for Craigslist/eBay and any site with a usable search \
    URL. Reserve autonomous: true for login- or interaction-gated sites.

════════════════════════════════════════
AUTONOMOUS AGENT CAPABILITIES
════════════════════════════════════════
The agent has these actions available:
  click     — click a button, link, or element
  type      — type text into a search box or form field
  press     — press a key (Enter, Tab, Escape, etc.)
  navigate  — go to a different URL
  scroll    — scroll the page up or down
  remember  — save a fact to working memory for use later in the session
  done      — finish and report what was found

WORKING MEMORY (the "remember" action)
  The agent can save facts across multiple pages. For example:
  - On a Marketplace listing: remember the asking price, product name, and issues
  - On eBay search results: remember the average sold price
  - At the end: the judgment step sees all the saved facts and makes a decision
  This is how multi-page research tasks work.

════════════════════════════════════════
THE JUDGMENT STEP (judgment_prompt)
════════════════════════════════════════
For complex research tasks, you can add a judgment_prompt to a watch. After the \
agent finishes browsing and saving facts to memory, a second AI call applies your \
custom reasoning criteria to those facts and decides if the result is a "match" \
(found=true) worth alerting about.

Example: The agent visits a Facebook Marketplace listing and saves:
  asking_price: $180
  product: Samsung 65" TV
  issues: crack in bottom corner
  ebay_used_avg: $280

Then the judgment_prompt says: "Is the asking price at least 20% below comparable \
used market price? Deduct 10% per major defect."

The judgment AI calculates, decides yes/no, and writes a plain-English explanation \
that becomes the notification text.

When to use judgment_prompt:
  - Price comparison tasks (is this a good deal?)
  - Research tasks with specific criteria (is this job a good fit?)
  - Any task where "found" depends on comparing gathered facts, not just detecting text
  - ⭐ ATTRIBUTE FILTERS THE SEARCH RESULTS CAN'T SHOW. Marketplace search results list a
    title + price only — they do NOT show a car's transmission, a truck's drivetrain, mileage,
    trim, condition, or most specifics. So if the user wants a specific ATTRIBUTE ("manual
    transmission", "4x4", "diesel", "under 100k miles", "no accidents"), you MUST add a
    judgment_prompt telling the watch to open each listing, read that attribute, and match on
    it — otherwise it can only guess from the title and will drop nearly everything. Write the
    judgment_prompt as the concrete check, e.g. for "manual cars under $8k": "Open the listing
    and read the transmission — a MATCH is a MANUAL car (stick / 5-speed / 6-speed) under $8000;
    automatics are not a match; if unstated but the title hints manual, flag it as a maybe."

════════════════════════════════════════
LIMITATIONS TO KNOW ABOUT
════════════════════════════════════════
- LOGIN REQUIRED SITES: The agent never types passwords. For sites that need a \
  login, the user does a one-time manual sign-in via the "Connect Facebook" button \
  (Services → Browser), which saves a browser profile; watches with \
  use_login_profile: true then reuse that logged-in session. Without it, the watch \
  only sees what a logged-out visitor sees.
- BLOCKERS (popups & CAPTCHAs): The agent auto-dismisses login/cookie/consent \
  popups (like Facebook's "Log in or sign up" modal) and tries to solve \
  press-and-hold CAPTCHAs — both handled locally, no paid services. If it stays \
  stuck it runs an offline reasoning pass to recover or exits gracefully.
- HEAVILY BOT-PROTECTED SITES: Sites like Amazon, Walmart, and eBay have anti-bot \
  systems. The agent mimics human mouse movements and timing to reduce detection, \
  but it may occasionally get blocked. It will retry on the next scheduled run.
- SPEED: An autonomous agent run with 10-15 steps typically takes 1-4 minutes \
  depending on the page and model. The judgment step adds another 30-60 seconds.
- NO JAVASCRIPT-HEAVY SPAs THAT NEED LOGIN: If the content only loads after login \
  and behind dynamic JavaScript that requires authentication, the agent cannot access it.

════════════════════════════════════════
FACEBOOK MARKETPLACE (important distinction)
════════════════════════════════════════
Facebook treats logged-out and logged-in visitors very differently — match the URL \
and settings to which one applies:

LOGGED OUT (use_login_profile: false) — works, but only the GENERAL feed:
  - Use the bare feed URL: https://www.facebook.com/marketplace/  (or \
    https://www.facebook.com/marketplace/CITY/ for a local feed). This DOES render \
    real listings to logged-out visitors after the login popup is dismissed (the \
    watch closes it automatically).
  - A "Log in or sign up" popup appears — it is just an overlay, NOT a CAPTCHA; the \
    watch clicks it away. Don't worry about it.
  - The bare feed is a MIXED local feed (trucks, boats, furniture, everything), so \
    you MUST add a judgment_prompt to filter down to what the user wants.
  - Search URLs like /marketplace/search/?query=... return ZERO listings when \
    logged out — never use a search URL for a logged-out watch.

LOGGED IN (use_login_profile: true) — needed for targeted search:
  - First the user clicks "Connect Facebook" (Services → Browser) and signs in once.
  - Then you CAN use a targeted search URL, e.g. \
    https://www.facebook.com/marketplace/category/vehicles?query=4x4%20truck — this \
    is far more precise than the logged-out mixed feed.
  - Set use_login_profile: true so the watch reuses the saved login.

Default to LOGGED OUT + bare feed + judgment_prompt unless the user says they want \
targeted search or has already connected Facebook. Either way, a Marketplace watch \
should almost always be mode: "continuous" (the feed changes constantly).

════════════════════════════════════════
WATCH CONFIG FIELDS
════════════════════════════════════════
- name             : short label (shown in the UI and notifications)
- urls             : list of start URLs. The right URL depends on the watch type:
                     • AUTONOMOUS agent watch → use the site HOMEPAGE \
                       (https://www.ebay.com, https://www.amazon.com); the agent \
                       searches from there like a human. General web → \
                       https://duckduckgo.com.
                     • CONTINUOUS watch → give the feed/results URL DIRECTLY (the \
                       extractor reads it as-is, there is no agent). E.g. a bare \
                       Facebook Marketplace feed, or an eBay search results URL.
- instruction      : tells the agent WHAT to do and WHAT to look for. For continuous \
                     watches this describes which listings count as a match.
- mode             : "schedule" (default) or "continuous" (see scheduling section).
- interval_minutes : SCHEDULE only — how often to run (30, 60, 120, 1440 common). \
                     Mutually exclusive with cron_expression. Set null for continuous.
- cron_expression  : SCHEDULE only — 5-field cron like "0 9 * * 1-5" (weekdays 9am) \
                     for a specific time. Set null for continuous.
- perception       : "auto" (default), "text" (DOM only), "vision" (screenshot)
- autonomous       : true = agent mode, false = simple static read OR continuous \
                     extractor. Continuous watches use false.
- max_agent_steps  : how many actions the agent can take (default 15, up to 30 for complex tasks)
- judgment_prompt  : optional — post-browse reasoning criteria (see above). For a \
                     logged-out Facebook feed this is REQUIRED to filter the mixed feed.
- use_login_profile: true to reuse a Connect-Facebook login session (targeted FB \
                     search). false for logged-out / non-login sites.
- continuous_idle_seconds : CONTINUOUS only — seconds to pause between sweeps (default 45).
- continuous_max_alerts   : CONTINUOUS only — cap new-listing alerts per sweep (default 8).

════════════════════════════════════════
INSTRUCTION WRITING GUIDE
════════════════════════════════════════
For simple watches: describe what should trigger an alert.
  "Alert if the price drops below $X. Ignore third-party sellers."
  "Alert if there is a frost warning or winter storm watch."

For autonomous agent watches: describe the task as a series of steps.
  "Search for [thing] on this site. Find the top 3 results. Save each name \
  and price to memory. Then navigate to eBay and search for the same item to \
  find the going market rate. Save that too. When done, summarize everything."

For judgment watches: the instruction is the research task; the judgment_prompt \
is the decision criteria.
  instruction: "Visit this listing. Save the price, product details, and any \
  issues to memory. Then search eBay sold listings for the same item and save \
  those prices."
  judgment_prompt: "Is the asking price at least 15% below the eBay average \
  for comparable condition? Factor in any issues as additional discounts (10% each)."

════════════════════════════════════════
CREATING vs EDITING WATCHES
════════════════════════════════════════
You can BOTH create new watches and edit existing ones. The EXISTING WATCHES list \
at the end of this prompt shows the user's current watches with their full config.

RESTOCK / BACK-IN-STOCK REQUESTS come FIRST — check for these before anything else. \
If the user gives a SPECIFIC PRODUCT PAGE URL (usually a single item, often with \
?variant=) and asks to be told when it is BACK IN STOCK or when a size/variant is \
AVAILABLE again, that is a RESTOCK watch. Just CONFIRM and set it up — it is a \
brand-NEW watch (never fold it into an existing vehicle/listings watch). Do NOT ask \
whether the site needs a login, and do NOT offer to "explore" or "learn" the site \
first: a product page needs neither — the app reads the item's stock directly. Reply \
briefly ("I'll watch that page and let you know the moment 34W x 30L is back in \
stock.") and let the watch card be created. This request has NOTHING to do with any \
existing vehicle or listings watch — do not mention Anacortes, Craigslist, OfferUp, \
or any other watch.

- To CREATE a new watch: set "action": "create" and use a NEW name.
- To EDIT an existing watch: set "action": "update" and use the EXACT existing \
  name. Include the FULL config you want it to have (every field, not just the \
  changed one) — the update replaces the whole watch. Start from the existing \
  watch's values shown below and change only what the user asked for.

When the user asks to change, fix, retarget, reschedule, pause, or tweak something \
that matches an existing watch, propose an "update" — do NOT create a duplicate.

════════════════════════════════════════
RESPONSE FORMAT
════════════════════════════════════════
Always reply in this exact JSON — no markdown, no code fences:
{
  "message": "<your plain-English reply>",
  "watch_suggestion": null,
  "listing_query": null,
  "watch_actions": null
}

════════════════════════════════════════
MANAGING & REVIEWING WATCHES
════════════════════════════════════════
The EXISTING WATCHES list below includes a "health:" line for each watch — its state
(enabled/DISABLED, running/stopped), its last-run result or ERROR, and how many matches
it has found. USE this to review and advise when asked ("how are my watches doing?",
"why is X finding nothing?", "review my truck watch"): point out watches that are
erroring, disabled, stopped, or finding nothing, and suggest a fix (often an
action:"update" with a better search URL or judgment_prompt).

To CHANGE a watch's lifecycle, set "watch_actions" to a LIST — the app shows the user a
confirmation before running anything (so it's safe to propose, even bulk):
  "watch_actions": [
    {"action": "delete",  "name": "<exact name of an existing watch>"},
    {"action": "disable", "name": "<exact name of an existing watch>"},
    {"action": "start",   "name": "<exact name of an existing watch>"}
  ]
Valid actions: "delete", "enable", "disable", "start", "stop" (start/stop apply to
continuous watches). Use EXACT existing names. For bulk requests like "delete all but
the truck watch", list a delete for every OTHER watch. Leave watch_actions null when the
user isn't asking to change watch state.

════════════════════════════════════════
LOOKING UP WHAT'S BEEN FOUND (listing_query)
════════════════════════════════════════
The app stores every listing its watches have found, with parsed attributes (price,
year, mileage, transmission, drivetrain) and how many duplicate posts each has. When
the user asks what's been found / seen / what matches some criteria (e.g. "what manual
4x4 trucks under $8k have shown up?", "show me the matches for my truck watch"), set
"listing_query" to an object — the app runs it and shows the results below your message.
You will NOT see the rows yourself, so keep "message" a short intro like "Here's what
turned up:". Shape (omit/null any filter you don't need):
{
  "watch": "<exact watch name, or null for all watches>",
  "matched_only": true,        // true = only listings that matched the watch's criteria
  "text": "<keywords>",        // GENERAL keyword search over title + ad body (any item type)
  "transmission": "manual",    // vehicles only — or "automatic", or null
  "drivetrain": "4wd",         // vehicles only — or "awd", or null
  "min_year": 2010,
  "max_price": 8000,
  "max_mileage": 150000,
  "limit": 50
}
Prefer "text" for anything that isn't a clean vehicle attribute (brand, model, material,
colour, condition, keyword). transmission/drivetrain only apply to cars/trucks.
Only set listing_query when the user is asking about found/seen listings. For anything
else leave it null.

IMPORTANT — always translate the user's words into the matching filters. If they say
"manual", set transmission:"manual". "automatic" → transmission:"automatic". "4x4" or
"4wd" → drivetrain:"4wd". "under $8k" → max_price:8000. "newer than 2015" → min_year:2015.
"low miles / under 150k" → max_mileage. Do NOT return everything when they asked for a
specific kind — if they want manuals, the filter MUST include transmission:"manual".

CONFIRM BEFORE CREATING — do NOT dump a watch the instant you're asked. People often
think out loud or send a half-formed brain-dump. So:
  • If the request is vague, partial, or could be read more than one way, DON'T emit a
    watch_suggestion yet. Instead RESTATE what you think they want in one plain sentence
    and ask them to confirm or fill the gap ("Sounds like: 4x4 trucks under $15k on
    Craigslist Seattle — want me to set that up?"). Keep watch_suggestion null on that turn.
  • Ask a clarifying question when a key detail is missing (which site, budget, new vs used,
    location) rather than guessing and shipping a watch.
  • Only put a watch_suggestion in your reply once the details are clear AND the user has
    effectively said yes (an explicit "yes/do it", or a request that was already specific and
    unambiguous). A clear, complete request that already names the item, the site, and any
    price cap can go straight to a suggestion — don't over-ask on those.
When you propose creating or editing a watch, replace watch_suggestion with the FULL object:
{
  "action": "create" | "update",
  "name": "...",
  "urls": ["..."],
  "instruction": "...",
  "mode": "schedule" | "continuous",
  "interval_minutes": 60,
  "cron_expression": null,
  "perception": "auto",
  "autonomous": true,
  "max_agent_steps": 15,
  "judgment_prompt": null,
  "use_login_profile": false,
  "continuous_idle_seconds": 45,
  "continuous_max_alerts": 8
}

RULES:
- For mode "schedule": set interval_minutes OR cron_expression (never both), and \
  set mode-irrelevant continuous_* fields to their defaults.
- For mode "continuous": set interval_minutes AND cron_expression to null. Default to \
  autonomous: false (fast scraper + ad deep-read) for Craigslist/eBay and any site with \
  a usable search URL. Use autonomous: true ONLY for login- or interaction-gated sites \
  (e.g. Facebook Marketplace logged in).
- For a continuous marketplace watch, give it SEVERAL search-results URLs (the "urls" \
  list) using DIVERSE, EFFECTIVE terms — not just the user's literal phrase, which often \
  returns junk. A keyword search matches the words literally, so think like a savvy \
  shopper and include specific models, synonyms, and common phrasings. Examples: \
  "sports car" → search "Miata", "Corvette", "Mustang GT", "350Z", "manual coupe" \
  (searching the literal "sports car" returns SUVs with "Sport" in the name!). \
  "4x4 truck" → "4x4 truck", "4wd pickup", "four wheel drive truck". "cheap kayak" → \
  "kayak", "sit-on-top kayak", "fishing kayak". Build one search URL per term (same site, \
  swapping the query). Then set a judgment_prompt that filters to real matches (it can \
  use the ad body + attributes). The watch rotates through the URLs across sweeps.
- Every URL in "urls" MUST be a SEARCH-RESULTS or category page (a page that lists MANY \
  items) — NEVER a single listing/detail page (e.g. one that ends in a specific item id). \
  If you are not certain of a site's real search-URL format, use its simplest known search \
  path or say you'd rather LEARN the site first (Web Watcher can explore a new site's \
  search page once and remember its layout) instead of guessing a deep URL.
- CORRECT search-URL formats (do NOT invent others; most sites do NOT use a city subdomain \
  — that is a Craigslist-only trait, so NEVER write things like "seattle.offerup.com"): \
  Craigslist = https://<city>.craigslist.org/search/<cat>?query=TERM (city subdomain IS \
  correct here only); eBay = https://www.ebay.com/sch/i.html?_nkw=TERM; OfferUp = \
  https://offerup.com/search?q=TERM (one flat domain, no city); CarGurus = \
  https://www.cargurus.com/Cars/inventorylisting/... ; Facebook = \
  https://www.facebook.com/marketplace/CITY/search?query=TERM (city is a PATH segment, \
  not a subdomain); Kijiji = https://www.kijiji.ca/... ; GovDeals = \
  https://www.govdeals.com/... . If the user corrects a URL, APPLY the correction exactly \
  and re-emit the watch_suggestion with the fixed urls — do not repeat the bad host.
- The query text (query=/q=/_nkw=) is ONLY item words ("ford f150", "kayak"). NEVER put \
  a location, zip, price, or "by owner" into the query text — every site has its own \
  REAL params for those, and they differ:\n\
    Craigslist: location = postal=<5-digit zip>&search_distance=50 (you know most towns' \
    zips — e.g. Anacortes WA = 98221; use the town's own zip; don't stress the city \
    subdomain, the app corrects the region from the zip). Price = max_price=/min_price=. \
    "by owner" = purveyor=owner. Generic vehicle search = category /search/cta, no query.\n\
    OfferUp: ONLY q, radius, price_min, price_max exist. There is NO location param and \
    NO city path — NEVER write "offerup.com/<city>/search" (it errors). OfferUp shows \
    the user's own area automatically, so just https://offerup.com/search?q=TERM \
    (+price_max=/price_min=/radius=50). Put the location in the instruction text instead.\n\
    eBay: location = _stpos=<zip>&_sadis=50. Price = _udhi= (max) / _udlo= (min).\n\
    Facebook: location = the CITY PATH segment (/marketplace/<city>/search). Price = \
    maxPrice=/minPrice=.
- Set judgment_prompt to a string for research/comparison/filtering tasks (and for \
  any logged-out Facebook feed); null for simple alert watches.
- The AI rates each find 1-5 (1 no match, 3 acceptable, 4 good match, 5 great deal) and \
  only alerts at or above "min_rating" (default 3). Map the user's words to the number: \
  "only great deals" / "only the best" → min_rating: 5; "good matches only" / "only \
  strong ones" / "too many notifications" → 4; "show me everything" / "I'm missing some" \
  / "more results" → 2. It only applies when a judgment_prompt is set, so add one if they \
  ask to filter by quality.
- "keywords" (must contain at least one) and "antikeywords" (exclude if any present) are \
  cheap word filters. When the user says "ignore anything that mentions parts/repair/ \
  salvage/for parts" → antikeywords: ["parts","repair","salvage"]. "must say 4x4" → \
  keywords: ["4x4"]. Use plain words, not phrases.
- Always include "action". If you are unsure whether a watch exists, prefer \
  "create" with a clearly new name.

════════════════════════════════════════
TONE — be a friendly, conversational helper
════════════════════════════════════════
Talk like a helpful person, not a form. Warm, natural, and a little personable — use \
plain language and contractions, react to what the user said ("Nice, that Ram looks \
like a solid find"), and offer a relevant next step or suggestion when it helps. Keep \
it concise (usually 1-3 sentences) — conversational, not chatty filler. When you run a \
listing_query, briefly say what you pulled up and, if useful, point something out \
(e.g. "Most of these are automatics — want me to narrow to manuals?"). When you suggest \
a watch, say in plain words what it'll do and whether they need to press Start \
(continuous) or Connect Facebook (logged-in). If the user seems new, explain simply.
The "message" field is what the user reads — make it sound human."""


def create_app(manager: "ServiceManager") -> FastAPI:
    app = FastAPI(title="Web Watcher", docs_url=None, redoc_url=None)

    # ------------------------------------------------------------------
    # Health + status
    # ------------------------------------------------------------------

    @app.get("/api/health")
    def health():
        from web_watcher.__version__ import __version__
        return {"ok": True, "version": __version__}

    @app.get("/api/status")
    def get_status():
        return manager.get_statuses()

    # ------------------------------------------------------------------
    # Service control
    # ------------------------------------------------------------------

    @app.post("/api/services/{name}/start")
    def start_service(name: str, bg: BackgroundTasks):
        _require_known_service(name)
        bg.add_task(manager.start, name)
        return {"queued": "start", "service": name}

    @app.post("/api/services/{name}/stop")
    def stop_service(name: str, bg: BackgroundTasks):
        _require_known_service(name)
        bg.add_task(manager.stop, name)
        return {"queued": "stop", "service": name}

    @app.post("/api/services/{name}/restart")
    def restart_service(name: str, bg: BackgroundTasks):
        _require_known_service(name)
        bg.add_task(manager.restart, name)
        return {"queued": "restart", "service": name}

    # ------------------------------------------------------------------
    # Watch CRUD
    # ------------------------------------------------------------------

    @app.get("/api/watches")
    def list_watches():
        from web_watcher.config import load
        from web_watcher.storage import get_last_run, watch_stats, get_goal_state

        cfg      = load()
        job_map  = {j["watch_name"]: j for j in manager.get_job_info()}
        owner_names = _load_owner_names()      # chat_id -> the person's Telegram name
        result   = []
        for w in cfg.watches:
            last = get_last_run(w.name)
            job  = job_map.get(w.name, {})
            stats = watch_stats(w.id or w.name, w.name)
            goal_state = get_goal_state(w.id or w.name) if w.goal_kind else None
            result.append({
                "goal_kind":        w.goal_kind,
                "target_size":      w.target_size,
                "goal_state":       goal_state,   # {available, note} for a goal watch, else None
                "name":             w.name,
                "enabled":          w.enabled,
                "owner":            w.owner,        # who owns it (a Telegram chat_id, "" = unassigned)
                "owner_name":       owner_names.get(str(w.owner or ""), ""),   # for grouping in the UI
                "urls":             w.urls,
                "instruction":      w.instruction,
                "interval_minutes": w.interval_minutes,
                "cron_expression":  w.cron_expression,
                "perception":       w.perception,
                "notify":           w.notify.model_dump(),
                "click_path":       [s.model_dump() for s in w.click_path],
                "model_override":   w.model_override,
                "autonomous":       w.autonomous,
                "max_agent_steps":  w.max_agent_steps,
                "judgment_prompt":  w.judgment_prompt,
                # Surfaced so you can SEE whether a supervised run is being recorded and whether
                # it uses the saved login — both matter before starting a Facebook sweep, and
                # both were previously invisible in the list (you had to guess).
                "record_video":     w.record_video,
                "use_login_profile": w.use_login_profile,
                "mode":             w.mode,
                "continuous_scroll_passes":    w.continuous_scroll_passes,
                "continuous_idle_seconds":     w.continuous_idle_seconds,
                "continuous_search_variation": w.continuous_search_variation,
                "continuous_max_alerts":       w.continuous_max_alerts,
                "use_login_profile":           w.use_login_profile,
                "continuous_running": bool(job.get("continuous_running", False)),
                "last_run":         last,
                "next_run_utc":     job.get("next_run_utc"),
                "stats":            stats,
            })
        return result

    # NOTE: the scheduler reload runs as a BackgroundTask, not inline. reload() stops
    # continuous watches and join()s their threads (up to 30s if an agent watch is
    # mid-sweep) — doing that inside the request would hang the HTTP call and the UI
    # would show "server unreachable". Returning first keeps the dashboard responsive;
    # the reload lands a moment later.
    @app.post("/api/watches", status_code=201)
    def create_watch(body: dict, bg: BackgroundTasks):
        from web_watcher.config import Watch, load, save, lock as _cfg_lock
        if isinstance(body.get("urls"), list):
            body["urls"], _ = _normalize_marketplace_urls(
                body["urls"], body.get("instruction") or body.get("name") or "")
        body = _coerce_watch_numbers(_backfill_schedule(body))
        try:
            new_watch = Watch.model_validate(body)
        except ValidationError as exc:
            raise HTTPException(400, detail=_validation_detail(exc))

        # Read-modify-write under the config lock: without it two callers (dashboard, owner's
        # bot, buddy's bot — FastAPI runs sync endpoints on a threadpool) can both load, both
        # append, and the second save silently discards the first person's new watch.
        with _cfg_lock():
            cfg = load()
            if any(w.name == new_watch.name for w in cfg.watches):
                raise HTTPException(409, detail=f"Watch {new_watch.name!r} already exists")

            cfg.watches.append(new_watch)
            save(cfg)
        bg.add_task(manager.reload_scheduler)
        # NOTE: exploration of an unknown site happens when the watch is STARTED (see
        # scheduler._execute_continuous_watch), not here — creating a watch shouldn't kick
        # off a browser. `needs_exploring` just lets the UI mention it up front.
        from web_watcher.sitelearn import unknown_sites, site_status
        # Comprehend any NON-builtin site we don't yet understand, in the background, so the
        # agent reasons from what the site actually is — and a not-viable site (e.g. a weather
        # site) can be flagged. Built-in marketplaces (craigslist/ebay/…) are skipped.
        try:
            from web_watcher.storage import get_site_understanding, site_key
            seen: set[str] = set()
            for u in (new_watch.urls or []):
                k = site_key(u)
                if not k or k in seen:
                    continue
                seen.add(k)
                if site_status(u).get("kind") != "builtin" and not get_site_understanding(u):
                    manager.comprehend_start(u)
        except Exception:
            pass
        return {"ok": True, "name": new_watch.name, "needs_exploring": unknown_sites(new_watch.urls)}

    @app.put("/api/watches/{watch_name}")
    def update_watch(watch_name: str, body: dict, bg: BackgroundTasks):
        from web_watcher.config import Watch, load, save, lock as _cfg_lock
        if isinstance(body.get("urls"), list):
            body["urls"], _ = _normalize_marketplace_urls(
                body["urls"], body.get("instruction") or body.get("name") or watch_name or "")
        body = _coerce_watch_numbers(body)

        with _cfg_lock():          # see create_watch — closes the lost-update window
            cfg = load()
            watch_name = _resolve_watch_name(watch_name, cfg) or watch_name
            idx = next((i for i, w in enumerate(cfg.watches) if w.name == watch_name), None)
            if idx is None:
                raise HTTPException(404, detail=f"Watch {watch_name!r} not found")

            try:
                updated = _merge_watch_update(cfg.watches[idx], body, watch_name)
            except ValidationError as exc:
                raise HTTPException(400, detail=_validation_detail(exc))

            cfg.watches[idx] = updated
            save(cfg)
        bg.add_task(manager.reload_scheduler)
        return {"ok": True, "name": watch_name}

    @app.delete("/api/watches/{watch_name}")
    def delete_watch(watch_name: str, bg: BackgroundTasks):
        from web_watcher.config import load, save, lock as _cfg_lock
        with _cfg_lock():          # a delete racing an edit must not resurrect the watch
            cfg = load()
            before = len(cfg.watches)
            cfg.watches = [w for w in cfg.watches if w.name != watch_name]
            if len(cfg.watches) == before:
                raise HTTPException(404, detail=f"Watch {watch_name!r} not found")
            save(cfg)
        bg.add_task(manager.reload_scheduler)
        return {"ok": True}

    @app.post("/api/watches/{watch_name}/enabled")
    def set_watch_enabled(watch_name: str, body: dict, bg: BackgroundTasks):
        """Enable/disable a watch without rewriting its whole config (used by the
        assistant's lifecycle actions and the dashboard)."""
        from web_watcher.config import load, save, lock as _cfg_lock
        with _cfg_lock():          # enable/disable races the assistant's lifecycle actions
            cfg = load()
            w = next((w for w in cfg.watches if w.name == watch_name), None)
            if w is None:
                raise HTTPException(404, detail=f"Watch {watch_name!r} not found")
            w.enabled = bool(body.get("enabled", True))
            save(cfg)
        bg.add_task(manager.reload_scheduler)
        return {"ok": True, "name": watch_name, "enabled": w.enabled}

    @app.post("/api/watches/{watch_name}/run")
    def run_watch_now(watch_name: str, bg: BackgroundTasks):
        bg.add_task(manager.run_watch_now, watch_name)
        return {"queued": "run", "watch": watch_name}

    @app.post("/api/watches/{watch_name}/continuous/start")
    def start_continuous(watch_name: str, bg: BackgroundTasks):
        bg.add_task(manager.start_continuous, watch_name)
        return {"queued": "start", "watch": watch_name}

    @app.post("/api/watches/{watch_name}/continuous/stop")
    def stop_continuous(watch_name: str, bg: BackgroundTasks):
        bg.add_task(manager.stop_continuous, watch_name)
        return {"queued": "stop", "watch": watch_name}

    @app.post("/api/watches/{watch_name}/action")
    def watch_lifecycle_action(watch_name: str, body: dict, bg: BackgroundTasks):
        """One mode-aware lifecycle control for a single watch: enable | disable | start | stop |
        delete. The bot and dashboard get ONE tool that does the right thing for a continuous vs a
        scheduled watch. OWNERSHIP is enforced by the chat layer before this is called."""
        from web_watcher.config import load, save, lock as _cfg_lock
        action = str(body.get("action") or "").strip().lower()
        cfg = load()
        w = next((x for x in cfg.watches if x.name == watch_name), None)
        if w is None:
            raise HTTPException(404, detail=f"Watch {watch_name!r} not found")
        continuous = getattr(w, "mode", "") == "continuous"
        if action == "delete":
            cfg.watches = [x for x in cfg.watches if x.name != watch_name]
            save(cfg)
            bg.add_task(manager.reload_scheduler)
            return {"ok": True, "name": watch_name, "action": "delete"}
        if action in ("enable", "start"):
            if not w.enabled:
                w.enabled = True
                save(cfg)
                bg.add_task(manager.reload_scheduler)
            if action == "start" and continuous:
                bg.add_task(manager.start_continuous, watch_name)
            # If the whole Watcher is paused, a single start won't actually sweep — surface that so
            # the caller can tell the user to resume the master switch first.
            return {"ok": True, "name": watch_name, "action": action, "paused": manager.is_paused()}
        if action in ("reset", "restart", "remake", "rebuild"):
            # "Start it over fresh": wipe this watch's finds + dedup memory so it re-discovers and
            # RE-JUDGES everything with the current logic (which is how junk collected under old
            # settings gets cleared), then make sure it's enabled and sweeping. The watch config
            # (its URLs, keywords, etc.) is untouched — this clears results, it doesn't recreate.
            from web_watcher.storage import clear_watch_results
            removed = clear_watch_results(watch_id=getattr(w, "id", None), watch_name=w.name)
            if not w.enabled:
                w.enabled = True
                save(cfg)
            bg.add_task(manager.reload_scheduler)
            if continuous:
                bg.add_task(manager.start_continuous, watch_name)
            return {"ok": True, "name": watch_name, "action": "reset",
                    "removed": removed, "paused": manager.is_paused()}
        if action in ("disable", "stop"):
            # "Stop" has to actually stop it. Stopping a per-watch loop is a no-op whenever the
            # orchestrator is driving — it owns every ENABLED continuous watch and simply sweeps
            # it again on the next pass — so the bot could report "stopped all 2 of your watches"
            # while both kept running. `enabled` is the lever that works in BOTH execution modes,
            # and it's the state the Active/Inactive lists already show.
            if continuous:
                bg.add_task(manager.stop_continuous, watch_name)     # drop any per-watch loop now
            if w.enabled:
                w.enabled = False
                save(cfg)
                bg.add_task(manager.reload_scheduler)
            return {"ok": True, "name": watch_name, "action": action, "enabled": False}
        raise HTTPException(400, detail=f"Unknown watch action {action!r}")

    @app.post("/api/owners/action")
    def owner_lifecycle_action(body: dict, bg: BackgroundTasks):
        """Start or stop ONE PERSON'S watches as a group — the per-user equivalent of the master
        switch, for the server console where watches are grouped by user. owner "" = the
        unassigned group.

        This flips `enabled`, not the running loops, because that's the lever that works in BOTH
        execution modes: when the orchestrator is driving it services every ENABLED continuous
        watch, so stopping a per-watch loop would just be re-started on its next pass. It's also
        the state the console already shows, so the button matches what the user sees.

        STOP remembers exactly which watches it turned off, and START turns those back on — so a
        watch the person had already deactivated on purpose is not silently resurrected by a
        group start. With nothing remembered (first use), START enables the whole group, which is
        what clicking "Start all" on a visible group plainly means."""
        from web_watcher.config import load, save, lock as _cfg_lock
        action = str(body.get("action") or "").strip().lower()
        if action not in ("start", "stop"):
            raise HTTPException(400, detail=f"Unknown owner action {action!r}")
        owner = str(body.get("owner") or "").strip()

        cfg = load()
        mine = [w for w in cfg.watches if str(getattr(w, "owner", "") or "") == owner]
        if not mine:
            return {"ok": True, "owner": owner, "action": action, "changed": 0,
                    "detail": "no watches for this user"}

        suspended = _load_suspended()
        remembered = set(suspended.get(owner) or [])
        changed: list[str] = []

        if action == "stop":
            for w in mine:
                if w.enabled:
                    w.enabled = False
                    changed.append(w.name)
                if getattr(w, "mode", "") == "continuous":
                    try:
                        manager.stop_continuous(w.name)       # also drop any per-watch loop now
                    except Exception:
                        pass
            if changed:
                suspended[owner] = changed
                _save_suspended(suspended)
        else:
            targets = remembered if remembered else {w.name for w in mine}
            for w in mine:
                if w.name in targets and not w.enabled:
                    w.enabled = True
                    changed.append(w.name)
            suspended.pop(owner, None)
            _save_suspended(suspended)

        if changed:
            save(cfg)
            bg.add_task(manager.reload_scheduler)
        log.info("owner %s: %s — %d watch(es) changed", owner or "(unassigned)", action, len(changed))
        return {"ok": True, "owner": owner, "action": action, "changed": len(changed),
                "names": changed, "total": len(mine),
                "enabled_now": sum(1 for w in mine if w.enabled),
                "paused": manager.is_paused()}

    # ------------------------------------------------------------------
    # Chat review — the big model audits our own conversations
    # ------------------------------------------------------------------

    @app.post("/api/review/chats")
    def start_chat_review(body: dict | None = None):
        """Run the slow, thorough audit of stored conversations. Returns immediately; poll
        GET /api/review/chats for progress and the finished report."""
        body = body or {}
        since = body.get("since")
        return manager.review_start(
            since=float(since) if since is not None else None,
            model=str(body.get("model") or ""),
        )

    @app.get("/api/review/chats")
    def chat_review_status():
        """Live status of a running review, plus the last finished report (which survives a
        restart — it's saved to disk)."""
        from web_watcher import review as _review
        status = manager.review_status()
        if not status.get("report"):
            last = _review.latest_report()
            if last:
                status["report"] = last
        status["watermark"] = _review.watermark()
        return status

    @app.get("/api/review/settings")
    def get_review_settings():
        cfg = _load_cfg()
        rc = getattr(cfg, "review", None)
        from web_watcher import review as _review
        return {"enabled": bool(getattr(rc, "enabled", False)),
                "every_hours": float(getattr(rc, "every_hours", 24.0)),
                "notify": bool(getattr(rc, "notify", True)),
                "last_run_at": _review.watermark().get("last_run_at", 0.0)}

    @app.post("/api/review/settings")
    def set_review_settings(body: dict):
        """Turn the self-audit on/off and set how often it runs."""
        from web_watcher.config import load, save, lock as _cfg_lock
        cfg = load()
        if "enabled" in body:
            cfg.review.enabled = bool(body["enabled"])
        if "notify" in body:
            cfg.review.notify = bool(body["notify"])
        if "every_hours" in body:
            try:
                cfg.review.every_hours = max(1.0, float(body["every_hours"] or 24.0))
            except (TypeError, ValueError):
                pass
        save(cfg)
        return {"ok": True, "enabled": cfg.review.enabled,
                "every_hours": cfg.review.every_hours, "notify": cfg.review.notify}

    @app.get("/api/review/chats/text", response_class=PlainTextResponse)
    def chat_review_text():
        """The latest report as plain text — the fastest way to actually read it."""
        from web_watcher import review as _review
        report = (manager.review_status().get("report")) or _review.latest_report()
        return _review.render_report(report or {})

    @app.post("/api/browser/clear-hidden")
    def clear_hidden_postings(body: dict | None = None):
        """Forget any listings a site was told to hide. Craigslist keeps hidden postings in
        localStorage, which is reloaded into every sweep — so one stray click on "hide posting"
        would remove that listing from everything the watch ever sees again."""
        from web_watcher.browser import clear_site_local_storage
        site = str((body or {}).get("site") or "craigslist").strip()
        return clear_site_local_storage(site)

    @app.get("/api/cloud/escalations")
    def cloud_escalations(limit: int = 50):
        """Every call that needed Claude, with both answers and what it cost — the audit trail
        for the bill and the list of what the local model can't do yet."""
        from web_watcher import llm
        return {"summary": llm.escalation_summary(),
                "budget": llm.budget_state(_load_cfg()),
                "recent": llm.escalations(max(1, min(limit, 200)))}

    @app.get("/api/listings/archive")
    def listing_archive(key: str = "", url: str = ""):
        """Serve the frozen MHTML copy of a matched listing, so a page that has since been
        deleted still opens and renders. Chrome opens .mhtml directly. Looked up by listing_key
        or by the listing's url."""
        from fastapi.responses import FileResponse
        from web_watcher import archive, storage
        lk = (key or "").strip()
        if not lk and url.strip():
            row = storage.get_listing_by_url(url.strip())
            lk = (row or {}).get("listing_key", "") if row else ""
        p = archive.archive_file(lk) if lk else None
        if not p:
            raise HTTPException(404, detail="no frozen copy saved for this listing")
        return FileResponse(str(p), media_type="multipart/related",
                            filename=f"{lk[:60] or 'listing'}.mhtml")

    @app.get("/api/sweep/issues")
    def sweep_issues(limit: int = 100):
        """What has gone WRONG while watching — the browsing/judging counterpart to the cloud
        escalation log. Aggregated by kind and by watch so a recurring problem (a watch that keeps
        getting stuck, a site that keeps challenging us) is visible instead of scrolled past."""
        from web_watcher import issues
        return {"summary": issues.issue_summary(),
                "recent": issues.issues(max(1, min(limit, 200)))}

    # ------------------------------------------------------------------
    # Site drill — can we actually use this site?
    # ------------------------------------------------------------------

    @app.post("/api/drill")
    def start_drill(body: dict | None = None):
        """Run the competency drill for a site (default Facebook). Returns immediately; poll
        GET /api/drill for progress and the report."""
        body = body or {}
        return manager.drill_start(str(body.get("site") or "facebook.com"),
                                   headless=bool(body.get("headless", False)))

    @app.get("/api/drill")
    def drill_status():
        from web_watcher import drill as _drill
        status = manager.drill_status()
        if not status.get("report"):
            last = _drill.latest_report()
            if last:
                status["report"] = last
        return status

    @app.get("/api/drill/text", response_class=PlainTextResponse)
    def drill_text():
        from web_watcher import drill as _drill
        report = (manager.drill_status().get("report")) or _drill.latest_report()
        return _drill.render_report(report or {})

    @app.post("/api/connect/facebook")
    def connect_facebook(bg: BackgroundTasks):
        bg.add_task(manager.connect_facebook)
        return {"queued": "connect", "site": "facebook"}

    @app.get("/api/connect/facebook")
    def connect_facebook_status():
        return {"status": manager.fb_connect_status()}

    # ------------------------------------------------------------------
    # History + schedule
    # ------------------------------------------------------------------

    @app.get("/api/history")
    def get_history(watch: str | None = None, limit: int = 100):
        from web_watcher.storage import get_history as _gh
        return _gh(watch_name=watch, limit=limit)

    @app.get("/api/listings")
    def get_listings(
        watch: str | None = None, matched: bool | None = None, q: str | None = None,
        transmission: str | None = None, drivetrain: str | None = None,
        min_year: int | None = None, max_price: int | None = None,
        max_mileage: int | None = None, limit: int = 200,
    ):
        """Query the listing store (the Results data). `q` is a general free-text search
        over title + ad body — works for any kind of item, not just vehicles."""
        return _run_listing_query({
            "watch": watch, "matched_only": matched, "text": q, "transmission": transmission,
            "drivetrain": drivetrain, "min_year": min_year, "max_price": max_price,
            "max_mileage": max_mileage, "limit": limit,
        })

    @app.post("/api/watches/{watch_name}/antikeyword")
    def add_antikeyword(watch_name: str, body: dict, bg: BackgroundTasks):
        """Append ONE antikeyword to a watch (dedup, case-insensitive) — an APPEND, not a replace,
        so we never wipe the watch's existing filters. Feeds the 'not a match → skip ones like
        this' flow. Body: {word}."""
        from web_watcher.config import load, save, lock as _cfg_lock
        word = (body.get("word") or "").strip().lower()
        # Under the lock: this is an APPEND, so a concurrent edit reloading stale watches would
        # drop the word the user just taught us (or another of their filters).
        with _cfg_lock():
            cfg = load()
            watch_name = _resolve_watch_name(watch_name, cfg) or watch_name
            w = next((x for x in cfg.watches if x.name == watch_name), None)
            if w is None:
                raise HTTPException(404, detail=f"Watch {watch_name!r} not found")
            if not word:
                raise HTTPException(400, detail="no word given")
            existing = [a for a in (w.antikeywords or [])]
            if word not in [a.lower() for a in existing]:
                w.antikeywords = existing + [word]
                save(cfg)
                bg.add_task(manager.reload_scheduler)
        return {"ok": True, "name": watch_name, "antikeywords": w.antikeywords}

    @app.post("/api/listings/exclude")
    def exclude_listing_ep(body: dict):
        """Hand-remove a bad match. Marks the listing excluded+unmatched for every watch that
        surfaced it, so it drops from the matches now and a later re-judge can't bring it back.
        Body: {url}."""
        from web_watcher.storage import exclude_listing as _excl
        url = (body.get("url") or "").strip()
        if not url:
            raise HTTPException(400, detail="no url given")
        removed = _excl(url)
        return {"ok": True, "url": url, "removed": removed}

    @app.delete("/api/listings")
    def clear_results(watch: str | None = None):
        """Clear Results. With ?watch=<name> wipes just that watch's finds (and its dedup
        memory, so the next sweep re-discovers and RE-JUDGES with current logic); with no
        watch, wipes ALL Results. Watch configs are never touched. This is how stale finds
        collected under old logic get cleared without deleting the watch."""
        from web_watcher.storage import clear_watch_results, clear_all_results
        if watch:
            cfg = _load_cfg()
            w = next((w for w in cfg.watches if w.name == watch), None)
            if w is None:
                raise HTTPException(404, detail=f"Watch {watch!r} not found")
            removed = clear_watch_results(watch_id=getattr(w, "id", None), watch_name=w.name)
        else:
            removed = clear_all_results()
        return {"ok": True, "watch": watch, "removed": removed}

    # ── Deep Inspect (deal/scam evaluation of one listing) ────────────────────
    @app.post("/api/inspect")
    def start_inspect(body: dict):
        """Kick off a Deep Inspect of one listing (opens it, reads the full posting, and a big
        local model returns a deal + scam-risk verdict). Non-blocking — poll GET /api/inspect.
        Body: {url, watch?, criteria?}. criteria defaults to the named watch's instruction."""
        url = (body.get("url") or "").strip()
        if not url.startswith("http"):
            raise HTTPException(400, detail="A listing URL is required.")
        criteria = (body.get("criteria") or "").strip()
        if not criteria and body.get("watch"):
            cfg = _load_cfg()
            w = next((w for w in cfg.watches if w.name == body["watch"]), None)
            if w:
                criteria = w.instruction or ""
        return manager.inspect_start(url, criteria)

    @app.get("/api/inspect")
    def get_inspect(url: str):
        return manager.inspect_status(url)

    # ── Site comprehension (what kind of site is this, is it watchable) ───────
    @app.post("/api/comprehend")
    def start_comprehend(body: dict):
        """Comprehend a site: open it, scan its structure, and a big local model decides what
        KIND of site it is, what its search box is for, and whether it's viable to monitor.
        Non-blocking — poll GET /api/comprehend. Result is cached on the site profile."""
        url = (body.get("url") or "").strip()
        if not url.startswith("http"):
            raise HTTPException(400, detail="A site URL is required.")
        return manager.comprehend_start(url, refresh=bool(body.get("refresh")))

    @app.get("/api/comprehend")
    def get_comprehend(url: str):
        # Serve a cached understanding immediately if we have one; else the live job status.
        from web_watcher.storage import get_site_understanding
        cached = get_site_understanding(url)
        if cached:
            return {"status": "done", "url": url, "understanding": cached}
        return manager.comprehend_status(url)

    # ── Learned sites ────────────────────────────────────────────────────────
    @app.get("/api/sites")
    def list_sites():
        """Return the learned site profiles — the sites Web Watcher has explored and knows
        how to read (listing-URL shape, search param, sort options)."""
        from web_watcher.storage import list_site_profiles
        return list_site_profiles()

    @app.get("/api/sites/status")
    def site_status_endpoint(url: str):
        """Does Web Watcher already know how to read this site? → {domain, known, kind}
        (kind: builtin | learned | unknown). The assistant/UI uses this to decide whether a
        site needs exploring before a watch on it will work well."""
        from web_watcher.sitelearn import site_status
        return site_status(url)

    @app.post("/api/sites/learn")
    def learn_site_endpoint(body: dict):
        """Point Web Watcher at a NEW site so it explores the layout once and saves a
        profile. Synchronous (drives a real browser); takes ~20-40s. Body:
        {"url": ..., "use_login_profile": bool}. With use_login_profile it explores using
        the persistent signed-in profile (for login-gated sites); otherwise as a guest,
        and a login-only site is reported rather than having its sign-in form touched."""
        url = (body.get("url") or "").strip()
        if not url or not url.startswith("http"):
            raise HTTPException(400, detail="A full http(s) URL is required.")
        from web_watcher.sitelearn import learn_site
        cfg = _load_cfg()
        return learn_site(
            url,
            model=cfg.models.effective_council_model,
            headless=cfg.browser.headless,
            persistent=bool(body.get("use_login_profile")),
            profile_dir=cfg.browser.profile_dir,
        )

    @app.delete("/api/sites/{domain}")
    def delete_site(domain: str):
        from web_watcher.storage import delete_site_profile
        removed = delete_site_profile(domain)
        if not removed:
            raise HTTPException(404, detail=f"No learned profile for {domain!r}.")
        return {"deleted": domain}

    @app.get("/api/schedule")
    def get_schedule():
        return manager.get_job_info()

    @app.get("/api/oversight")
    def get_oversight():
        """Live narration feed from the oversight agent — the little 'mind' that
        watches the watches and talks to itself in the Watches tab."""
        try:
            return manager.oversight_snapshot()
        except Exception:
            return {"running": False, "updated_at": None, "entries": [], "watches": []}

    @app.get("/api/logs")
    def get_logs(after: int = 0, category: str = "", watch: str = "", text: str = "",
                 limit: int = 300):
        """Live activity feed (in-memory ring): categorized recent log records for the
        Activity tab. Poll with the returned last_seq as `after` to stream only new lines.
        Filters: category (search/ai/alert/skipped/error/login/system), watch, text."""
        try:
            from web_watcher import logbuffer
            return logbuffer.get_ring().snapshot(
                after=after, category=category or None, watch=watch or None,
                text=text or None, limit=max(1, min(500, limit)))
        except Exception:
            return {"entries": [], "last_seq": after}

    @app.post("/api/oversight/action")
    def oversight_action(body: dict, bg: BackgroundTasks):
        """Run a one-click fix The Watcher offered on a concern (e.g. broaden a watch's
        search terms). Returns a short result message the panel shows back."""
        atype = body.get("type")
        watch_name = body.get("watch")
        if atype == "broaden_terms":
            # Optional user-dictated terms (list or comma-separated string); else auto-refresh.
            raw = body.get("terms")
            if isinstance(raw, str):
                raw = [t for t in raw.split(",")]
            return _action_broaden_terms(watch_name, manager, bg, terms_override=raw)
        raise HTTPException(400, detail=f"Unknown oversight action: {atype!r}")

    @app.post("/api/oversight/chat")
    def oversight_chat(body: dict):
        """Talk to The Watcher — the same brain as the assistant, but in oversight mode:
        it leads with what it's been observing and can review, look things up, and act."""
        messages: list[dict] = body.get("messages", [])
        cfg   = _load_cfg()
        model = cfg.models.effective_council_model
        # owner scopes the whole turn to one Telegram person (their chat_id): they see and act
        # on ONLY their own watches, on their OWN chat thread. None = the desktop admin view.
        owner = str(body.get("owner") or "").strip() or None
        # Remember the sender's Telegram name so the admin console shows a person, not a number.
        if owner:
            _record_owner_name(owner, str(body.get("owner_name") or "").strip())

        # "show me my settings" is answered directly from real state — no model call, so it's
        # instant and can never be hallucinated. Scoped: everyone sees their own settings; the
        # owner also gets the server-wide block. See _render_settings.
        _latest = _latest_user_text(messages)
        if _is_settings_request(_latest):
            # html=True tells the Telegram bridge to send this with HTML parse mode — the block
            # is ours and fully escaped, so its <b>/<i> render as formatting, not literal tags.
            result = {"message": _render_settings(cfg, manager, owner),
                      "watch_suggestion": None, "html": True, "raw": None}
            result["raw"] = result["message"]
            _persist_chat_turn(messages, result, owner)
            result.pop("raw", None)
            return result

        # "what watches do I have / what's running" is answered directly from real state too — a
        # clean, well-spaced block instead of the model running the titles together in a paragraph
        # (and mis-reporting run-state). A lookup ("show me the matches") is NOT caught here.
        _glob = _is_global_status_request(_latest)
        if (_pure_status_request(_latest) or _glob) and not _is_lookup_request(_latest):
            # A global-scoped question ("is the whole watcher running?", "globally how many?") leads
            # with a counts-only line spanning ALL users, then the speaker's own watches. A buddy
            # gets a real global number without ever seeing another person's watch titles.
            msg = _render_watch_status(cfg, manager, owner)
            if _glob:
                msg = _render_global_running(cfg, manager) + "\n\n" + msg
            result = {"message": msg, "watch_suggestion": None, "html": True, "raw": None}
            result["raw"] = result["message"]
            _persist_chat_turn(messages, result, owner)
            result.pop("raw", None)
            return result

        # "how does this work / what can you do / help" — one fixed, friendly explainer, so a new
        # user (a buddy just handed the bot) gets the same clear answer every time, not the 14b's
        # improvisation. Not for specific how-tos ("how do I stop a watch") — those go to the model.
        if _is_help_request(_latest):
            result = {"message": _render_help(owner), "watch_suggestion": None, "html": True,
                      "raw": None}
            result["raw"] = result["message"]
            _persist_chat_turn(messages, result, owner)
            result.pop("raw", None)
            return result

        try:
            snap = manager.oversight_snapshot(limit=14)
        except Exception:
            snap = {"entries": []}
        narration = "\n".join(
            f"  - [{e.get('kind')}] {e.get('text')}" for e in snap.get("entries", [])
        ) or "  (nothing observed yet)"
        observed_ctx = (
            "WHAT YOU'VE RECENTLY OBSERVED (your own narration, newest first):\n" + narration
        )
        # What this conversation just put on screen, and any recorded verdicts a "why…"
        # question is about — both injected so follow-ups are answered from facts in hand
        # instead of "I don't have context" / a guess. Empty strings when not applicable.
        shown_ctx   = _recent_shown_block(messages, owner)
        verdict_ctx = _verdict_context(_latest, cfg, owner)
        system = (
            _WATCHER_SYSTEM + "\n\n" + _CHAT_SYSTEM_BASE + "\n\n"
            + _build_watches_context(cfg, manager, owner=owner)
            + (("\n\n" + shown_ctx) if shown_ctx else "")
            + (("\n\n" + verdict_ctx) if verdict_ctx else "")
            + "\n\n" + observed_ctx
            + "\n\n" + _cloud_spend_context(cfg)
        )
        result = _complete_assistant_turn(system, messages, cfg, model, owner=owner)

        # The master switch (pause/resume the WHOLE Watcher) is executed here, server-side, so it
        # works identically from the desktop dock and the phone. _complete_assistant_turn only ever
        # sets program_action after confirming the requester is the admin, so this is safe to run.
        prog = result.pop("program_action", None)
        if prog in ("pause", "resume"):
            try:
                if prog == "pause":
                    manager.pause_all()
                else:
                    manager.resume_all()
            except Exception as exc:
                log.warning("could not %s the Watcher from chat: %s", prog, exc)
                result["message"] = f"I couldn't {prog} the Watcher just now: {exc}"

        _persist_chat_turn(messages, result, owner)
        result.pop("raw", None)
        return result

    @app.get("/api/oversight/chat/history")
    def get_watcher_history(owner: str | None = None):
        # owner scopes to one Telegram person's thread; the desktop dock omits it (main thread).
        return _load_watcher_history(str(owner).strip() if owner else None)

    @app.delete("/api/oversight/chat/history")
    def clear_watcher_history(owner: str | None = None):
        _save_watcher_history([], str(owner).strip() if owner else None)
        return {"ok": True}

    @app.post("/api/bug/report")
    def submit_bug(body: dict):
        """Bundle a self-contained bug report (description + recent logs + version + system +
        a CREDENTIAL-FREE watch summary) into a zip on the Desktop, so the tester can send it
        to the developer. Fully offline; never includes config.yaml or notification secrets."""
        title = (body.get("title") or "").strip() or "Bug report"
        desc  = (body.get("description") or "").strip()
        try:
            path = _write_bug_report(title, desc)
        except Exception as exc:
            log.warning("bug report failed: %s", exc)
            raise HTTPException(500, detail=f"Could not create the report: {exc}")
        try:                      # best-effort: pop open the folder so it's easy to find/send
            import os as _os
            _os.startfile(str(path.parent))   # type: ignore[attr-defined]
        except Exception:
            pass
        return {"ok": True, "path": str(path), "name": path.name}

    # ------------------------------------------------------------------
    # Orchestrator — the single driver (opt-in; The Watcher runs your watches)
    # ------------------------------------------------------------------

    @app.get("/api/orchestrator")
    def get_orchestrator():
        try:
            return manager.orchestrator_status()
        except Exception:
            return {"running": False, "current": None, "cycles": 0, "topics": []}

    @app.post("/api/orchestrator/start")
    def start_orchestrator():
        try:
            started = manager.start_orchestrator()
            return {"ok": True, "running": True, "started": started}
        except Exception as exc:
            raise HTTPException(409, detail=str(exc))

    @app.post("/api/orchestrator/stop")
    def stop_orchestrator():
        try:
            manager.stop_orchestrator()
            return {"ok": True, "running": False}
        except Exception as exc:
            raise HTTPException(409, detail=str(exc))

    # --- The global master switch: is the whole Watcher watching, or paused? ---------
    @app.get("/api/watcher/status")
    def watcher_status():
        try:
            return manager.watcher_status()
        except Exception:
            return {"running": True, "paused": False, "driver_running": False,
                    "current": None, "continuous_running": []}

    @app.post("/api/watcher/pause")
    def watcher_pause():
        """Master switch OFF — pause ALL watching for everyone. Admin action (the desktop, or the
        bot's admin path; the chat layer enforces admin-only before ever calling this)."""
        try:
            manager.pause_all()
            return {"ok": True, "paused": True, "running": False}
        except Exception as exc:
            raise HTTPException(409, detail=str(exc))

    @app.post("/api/watcher/resume")
    def watcher_resume():
        """Master switch ON — resume watching."""
        try:
            started = manager.resume_all()
            return {"ok": True, "paused": False, "running": True, "driver_started": started}
        except Exception as exc:
            raise HTTPException(409, detail=str(exc))

    @app.get("/api/summary")
    def get_summary():
        """A compact status snapshot for the assistant's launch greeting: how many
        watches, which are running, how many matches found, and any recent errors."""
        from web_watcher.config import load
        from web_watcher.storage import count_matches, get_history
        cfg = load()
        try:
            job_map = {j["watch_name"]: j for j in manager.get_job_info()}
        except Exception:
            job_map = {}
        watches = []
        total_matches = 0
        for w in cfg.watches:
            m = 0
            try:
                m = count_matches(w.id or w.name)
            except Exception:
                pass
            total_matches += m
            watches.append({
                "name": w.name, "mode": w.mode, "enabled": w.enabled,
                "running": bool(job_map.get(w.name, {}).get("continuous_running")),
                "matches": m,
            })
        try:
            recent = get_history(limit=25)
        except Exception:
            recent = []
        errors = [r for r in recent if r.get("error")]
        return {
            "first_run": len(cfg.watches) == 0,
            # watch_count = what it's ACTIVELY watching (deactivated ones are kept but ignored, so
            # counting them overstated "watching N watches"). total_count keeps the full figure.
            "watch_count": sum(1 for w in cfg.watches if w.enabled),
            "total_count": len(cfg.watches),
            "disabled_count": sum(1 for w in cfg.watches if not w.enabled),
            "enabled_count": sum(1 for w in cfg.watches if w.enabled),
            "running_count": sum(1 for w in watches if w["running"]),
            "total_matches": total_matches,
            "recent_error_count": len(errors),
            "last_error": (errors[0].get("error") or "")[:140] if errors else None,
            "watches": watches,
        }

    # ------------------------------------------------------------------
    # Browser settings
    # ------------------------------------------------------------------

    @app.get("/api/browser")
    def get_browser_settings():
        cfg = _load_cfg()
        return {"headless": cfg.browser.headless, "stealth": cfg.browser.stealth,
                "show_agent_cursor": cfg.browser.show_agent_cursor}

    @app.post("/api/browser")
    def set_browser_settings(body: dict):
        from web_watcher.config import load, save, lock as _cfg_lock
        cfg = load()
        if "headless" in body:
            cfg.browser.headless = bool(body["headless"])
        if "stealth" in body:
            cfg.browser.stealth = bool(body["stealth"])
        if "show_agent_cursor" in body:
            cfg.browser.show_agent_cursor = bool(body["show_agent_cursor"])
        save(cfg)
        return {"headless": cfg.browser.headless, "stealth": cfg.browser.stealth,
                "show_agent_cursor": cfg.browser.show_agent_cursor}

    # ------------------------------------------------------------------
    # Facebook halt — the emergency brake (global, persistent, human-cleared)
    # ------------------------------------------------------------------

    @app.get("/api/facebook/halt")
    def get_fb_halt():
        """Is Facebook watching halted after a security check, and why? The UI shows a banner
        with a Resume button; nothing auto-clears it."""
        from web_watcher import fb_safety
        state = fb_safety.halt_state()
        return {"halted": bool(state), "state": state}

    @app.delete("/api/facebook/halt")
    def clear_fb_halt():
        """The human says the account is healthy again — let Facebook watches run. This is the
        ONLY way a halt is lifted (deliberately: no timer, no restart, no silent resume)."""
        from web_watcher import fb_safety
        cleared = fb_safety.clear_halt()
        if cleared:
            log.info("Facebook halt cleared from the dashboard")
        return {"ok": True, "cleared": cleared}

    # ------------------------------------------------------------------
    # Credentials (Settings → Notifications & Keys): Telegram, Anthropic key, email
    # ------------------------------------------------------------------

    def _mask(secret: str) -> str:
        """Show a secret as ••••last4 so the UI can confirm it's set without echoing it back."""
        s = (secret or "").strip()
        if not s:
            return ""
        return ("•" * max(4, len(s) - 4)) + s[-4:] if len(s) > 4 else "••••"

    @app.get("/api/credentials")
    def get_credentials():
        """Current notification/key config with SECRETS MASKED — never echoes a raw token/key.
        On save, a blank secret field means 'keep the existing one' (see set_credentials)."""
        from web_watcher import llm
        cfg = _load_cfg()
        tg = cfg.notifications.telegram
        em = cfg.notifications.email
        cloud = cfg.models.cloud
        judge = (cloud.roles or {}).get("judge")
        judge_on = bool(judge and (judge.provider or "").lower() == "anthropic")
        spend = llm.usage_snapshot()
        budget = llm.budget_state(cfg)
        chat_route = (cloud.roles or {}).get("chat")
        chat_on = bool(chat_route and (chat_route.provider or "").lower() == "anthropic")
        return {
            "spend": {"cost_usd": round(spend.get("cost_usd", 0.0), 4),
                      "calls": spend.get("calls", 0),
                      "month_spent": budget["spent"], "cap": budget["cap"],
                      "remaining": budget["remaining"],
                      "today": budget.get("today", 0.0), "day_cap": budget.get("day_cap", 0.0)},
            "telegram": {"chat_id": tg.chat_id, "token_set": bool(tg.bot_token),
                         "token_hint": _mask(tg.bot_token),
                         "two_way": bool(getattr(tg, "two_way", False)),
                         "allowed_chat_ids": list(getattr(tg, "allowed_chat_ids", []) or []),
                         "checkin_hours": getattr(tg, "checkin_hours", 12.0)},
            "email": {"smtp_server": em.smtp_server, "smtp_port": em.smtp_port,
                      "from_address": em.from_address, "to_address": em.to_address,
                      "password_set": bool(em.app_password)},
            "anthropic": {
                "key_set": bool(cloud.anthropic_api_key) or bool(os.environ.get("ANTHROPIC_API_KEY")),
                "key_hint": _mask(cloud.anthropic_api_key),
                "key_from_env": (not cloud.anthropic_api_key) and bool(os.environ.get("ANTHROPIC_API_KEY")),
                "judge_enabled": judge_on,
                "judge_model": (judge.model if judge else "") or "claude-haiku-4-5",
                "chat_enabled": chat_on,
                "chat_model": (chat_route.model if chat_route else "") or "claude-haiku-4-5",
                "monthly_budget_usd": budget["cap"],
                "daily_budget_usd": budget.get("day_cap", 0.0),
                "auto": bool(getattr(cloud, "auto", True)),
            },
        }

    @app.post("/api/credentials")
    def set_credentials(body: dict):
        """Save notification/key config. Secret fields (bot_token, app_password, api_key) are only
        updated when a non-blank value is supplied — a blank keeps the stored secret intact, so the
        masked display never overwrites the real value. Writes config.yaml via config.save()."""
        from web_watcher.config import load, save, ProviderRoute
        cfg = load()

        tg = body.get("telegram") or {}
        if "chat_id" in tg:
            cfg.notifications.telegram.chat_id = str(tg.get("chat_id") or "").strip()
        if (tg.get("bot_token") or "").strip():
            cfg.notifications.telegram.bot_token = tg["bot_token"].strip()
        if "two_way" in tg:
            cfg.notifications.telegram.two_way = bool(tg["two_way"])
        if "checkin_hours" in tg:
            try:
                v = str(tg.get("checkin_hours") or "").strip()
                cfg.notifications.telegram.checkin_hours = max(0.0, float(v)) if v else 12.0
            except (TypeError, ValueError):
                pass
        if "allowed_chat_ids" in tg:
            # Accept a list or a comma/space-separated string from the UI field.
            raw = tg["allowed_chat_ids"]
            ids = raw if isinstance(raw, list) else re.split(r"[,\s]+", str(raw or ""))
            cfg.notifications.telegram.allowed_chat_ids = [
                str(c).strip() for c in ids if str(c).strip()
            ]

        em = body.get("email") or {}
        for f in ("smtp_server", "from_address", "to_address"):
            if f in em:
                setattr(cfg.notifications.email, f, str(em.get(f) or "").strip())
        if "smtp_port" in em:
            try:
                cfg.notifications.email.smtp_port = int(em["smtp_port"])
            except (TypeError, ValueError):
                pass
        if (em.get("app_password") or "").strip():
            cfg.notifications.email.app_password = em["app_password"].strip()

        an = body.get("anthropic") or {}
        if (an.get("api_key") or "").strip():
            cfg.models.cloud.anthropic_api_key = an["api_key"].strip()
        roles = dict(cfg.models.cloud.roles or {})
        if "judge_enabled" in an:
            if an.get("judge_enabled"):
                roles["judge"] = ProviderRoute(provider="anthropic",
                                               model=str(an.get("judge_model") or "").strip())
            else:
                roles.pop("judge", None)
        if "chat_enabled" in an:
            if an.get("chat_enabled"):
                roles["chat"] = ProviderRoute(provider="anthropic",
                                              model=str(an.get("chat_model") or "").strip())
            else:
                roles.pop("chat", None)
        cfg.models.cloud.roles = roles
        if "daily_budget_usd" in an:
            try:
                cfg.models.cloud.daily_budget_usd = max(0.0, float(an["daily_budget_usd"] or 0))
            except (TypeError, ValueError):
                pass
        if "monthly_budget_usd" in an:
            try:
                cfg.models.cloud.monthly_budget_usd = max(0.0, float(an["monthly_budget_usd"] or 0))
            except (TypeError, ValueError):
                pass

        save(cfg)
        # Apply a two-way-chat change immediately — the user shouldn't have to restart the app
        # to start (or stop) texting The Watcher.
        try:
            manager.restart_telegram()
        except Exception as exc:
            log.debug("could not restart the Telegram bridge: %s", exc)
        return {"ok": True}

    @app.post("/api/telegram/test")
    def telegram_test(body: dict):
        """Send a real test message to the configured (or just-entered) chat, so the user can
        confirm notifications reach their phone. Uses the posted token/chat_id if given, else
        the saved ones (POST /api/credentials first to persist)."""
        cfg = _load_cfg()
        token = (str(body.get("bot_token") or "").strip()
                 or cfg.notifications.telegram.bot_token or "").strip()
        chat_id = (str(body.get("chat_id") or "").strip()
                   or cfg.notifications.telegram.chat_id or "").strip()
        if not token or not chat_id:
            return {"ok": False, "error": "Enter a bot token and chat ID (or use Detect) first."}
        try:
            r = httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id,
                      "text": "✅ Web Watcher test — your Telegram alerts are working."},
                timeout=15.0)
            data = r.json()
            if data.get("ok"):
                return {"ok": True}
            return {"ok": False, "error": data.get("description", "Telegram rejected the message.")}
        except Exception as exc:
            return {"ok": False, "error": f"Could not reach Telegram: {exc}"}

    @app.post("/api/telegram/detect-chat-id")
    def telegram_detect(body: dict):
        """Find the chat ID(s) that have messaged this bot (via getUpdates), so the user doesn't
        have to hunt for the number — they just message their bot once, then click Detect."""
        token = (str(body.get("bot_token") or "").strip()
                 or _load_cfg().notifications.telegram.bot_token or "").strip()
        if not token:
            return {"ok": False, "error": "Enter your bot token first."}
        try:
            r = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15.0)
            data = r.json()
            if not data.get("ok"):
                return {"ok": False, "error": data.get("description", "That bot token looks invalid.")}
            chats, seen = [], set()
            for upd in reversed(data.get("result") or []):
                msg = upd.get("message") or upd.get("channel_post") or {}
                chat = msg.get("chat") or {}
                cid = chat.get("id")
                if cid is None or str(cid) in seen:
                    continue
                seen.add(str(cid))
                name = (chat.get("title")
                        or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
                        or chat.get("username") or str(cid))
                chats.append({"id": str(cid), "name": name})
            if not chats:
                return {"ok": False, "error": "No messages yet. Open Telegram, send any message to "
                                              "your bot, then click Detect again."}
            return {"ok": True, "chats": chats}
        except Exception as exc:
            return {"ok": False, "error": f"Could not reach Telegram: {exc}"}

    # ------------------------------------------------------------------
    # Telegram access requests — a stranger who messages the bot is NOT let in, but the
    # admin is alerted (by the bridge, on their phone) and the request is parked here so it
    # can be approved with one click from the console. See telegram_bot._notify_access_request.
    # ------------------------------------------------------------------

    @app.post("/api/telegram/access-request")
    def telegram_access_request(body: dict):
        """Record a pending access request (called by the bridge when an unknown chat messages)."""
        cid = str(body.get("chat_id") or "").strip()
        if not cid:
            return {"ok": False}
        _record_access_request(cid, str(body.get("name") or "").strip())
        return {"ok": True}

    @app.get("/api/telegram/checkin-prefs")
    def telegram_checkin_prefs():
        """Per-person check-in cadence (hours), keyed by chat_id. The bridge reads this so each
        person's own setting wins over the config default."""
        return _load_checkin_prefs()

    @app.get("/api/telegram/access-requests")
    def telegram_list_access_requests():
        return _load_access_requests()

    @app.post("/api/telegram/access-requests/allow")
    def telegram_allow_access(body: dict):
        """Grant a pending request: add its chat_id to the allowlist, save, restart the bridge
        so it takes effect live, and clear it from the pending list."""
        from web_watcher.config import load, save, lock as _cfg_lock
        cid = str(body.get("chat_id") or "").strip()
        if not cid:
            return {"ok": False, "error": "no chat_id"}
        # The allow-list is a SECURITY boundary — a lost update here silently un-invites
        # someone the owner just approved. Append under the lock.
        with _cfg_lock():
            cfg = load()
            allowed = list(cfg.notifications.telegram.allowed_chat_ids or [])
            if cid not in allowed:
                allowed.append(cid)
                cfg.notifications.telegram.allowed_chat_ids = allowed
                save(cfg)
        # Remember their name for the console, then drop the pending request.
        name = str(body.get("name") or "").strip()
        if name:
            _record_owner_name(cid, name)
        _remove_access_request(cid)
        try:
            manager.restart_telegram()
        except Exception as exc:
            log.debug("could not restart the Telegram bridge: %s", exc)
        # Tell them they're in — otherwise approval is silent and they're left wondering whether
        # the bot works. Sent AFTER the bridge restart so the allow-list is already live if they
        # reply right away. Best-effort: a failed welcome must never fail the approval.
        try:
            from web_watcher import notify
            first = (name.split() or [""])[0]
            hi = f"You're in{', ' + first if first else ''}! 👋\n\n"
            notify.send_plain_telegram(
                hi + "I'm your Web Watcher. Tell me what to keep an eye on and I'll watch for it "
                "and message you when something matches — for example:\n\n"
                "  • “Watch craigslist for a manual pickup under $10k near Anacortes”\n"
                "  • “Let me know about aluminum boats under 20 feet”\n\n"
                "You can ask me “what am I watching?” anytime, or “show me the latest match”.",
                cfg.notifications, chat_id_override=cid)
        except Exception as exc:
            log.debug("could not send the welcome message to %s: %s", cid, exc)
        return {"ok": True}

    @app.post("/api/telegram/access-requests/dismiss")
    def telegram_dismiss_access(body: dict):
        cid = str(body.get("chat_id") or "").strip()
        if cid:
            _remove_access_request(cid)
        return {"ok": True}

    @app.get("/api/oversight/threads")
    def list_oversight_threads():
        """The admin console's per-person chat index: the desktop thread + each Telegram
        person's thread, with message counts and the last line."""
        return _list_conversation_threads(_load_cfg())

    @app.post("/api/oversight/threads/message")
    def admin_thread_message(body: dict):
        """Admin steps into a person's conversation to help them: send a message AS The Watcher
        to that Telegram chat. It's delivered to their phone and recorded in their thread (from
        their side it's just the bot talking). Only for Telegram threads — the desktop thread is
        the admin's own dock."""
        owner = str(body.get("owner") or "").strip()
        text = str(body.get("text") or "").strip()
        if not owner:
            return {"ok": False, "error": "Pick a Telegram conversation to message."}
        if not text:
            return {"ok": False, "error": "Nothing to send."}
        token = (_load_cfg().notifications.telegram.bot_token or "").strip()
        if not token:
            return {"ok": False, "error": "No Telegram bot token is configured (Settings → Notifications & Keys)."}
        try:
            r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                           json={"chat_id": owner, "text": text, "disable_web_page_preview": True},
                           timeout=15.0)
            data = r.json()
            if not data.get("ok"):
                return {"ok": False, "error": data.get("description", "Telegram rejected the message.")}
        except Exception as exc:
            return {"ok": False, "error": f"Could not reach Telegram: {exc}"}
        import time as _t
        hist = _load_watcher_history(owner)
        hist.append({"role": "assistant", "content": text, "ts": _t.time(), "admin": True})
        _save_watcher_history(hist[-200:], owner)
        return {"ok": True}

    # ------------------------------------------------------------------
    # System specs + hardware re-scan (Settings → System)
    # ------------------------------------------------------------------

    @app.get("/api/system/specs")
    def system_specs():
        """Hardware summary (CPU/RAM/GPU/VRAM), the tier this hardware maps to, the models
        currently in use, and any in-progress re-scan download."""
        from web_watcher.gpu_detect import probe_system
        spec = probe_system()
        cfg = _load_cfg()
        spec["current_models"] = {
            "text":    cfg.models.text_model,
            "vision":  cfg.models.vision_model,
            "council": cfg.models.effective_council_model,
        }
        rec = spec.get("recommended") or {}
        spec["matches_current"] = (
            cfg.models.text_model == rec.get("text_model")
            and (cfg.models.vision_model or "") == (rec.get("vision_model") or "")
        )
        spec["rescan"] = dict(_rescan_state)
        return spec

    @app.post("/api/system/rescan")
    def system_rescan():
        """Re-detect the GPU/tier (for a hardware swap). If the recommended models differ from
        what's configured, switch to them and download the new ones in the background."""
        import threading
        from web_watcher.config import load
        from web_watcher.gpu_detect import probe_system
        spec = probe_system()
        rec = spec.get("recommended") or {}
        cfg = load()
        changed = (cfg.models.text_model != rec.get("text_model")
                   or (cfg.models.vision_model or "") != (rec.get("vision_model") or ""))
        if not changed:
            return {"changed": False, "specs": spec,
                    "message": f"No change — your hardware maps to the {spec['recommended_tier']} tier."}
        # Download first, switch the config LAST (inside the thread, after every model is on disk),
        # so the app keeps using the current working model until the new one is fully ready.
        if _rescan_state.get("status") != "pulling":
            threading.Thread(target=_apply_new_models_bg, args=(rec,), daemon=True).start()
        where = spec.get("gpu_name") or "your hardware"
        return {"changed": True, "specs": spec,
                "message": (f"Detected {where} → {spec['recommended_tier']} tier. "
                            "Downloading the updated AI models in the background — your current "
                            "model keeps working until they're ready, then it switches automatically.")}

    @app.get("/api/system/models")
    def system_models():
        """The selectable model sets (with download sizes + plain-English trade-offs), plus every
        model actually installed on this machine and its real disk size. Powers the Settings model
        selector: pick a lighter set to reduce load, or delete models you no longer use."""
        from web_watcher.gpu_detect import probe_system, tier_catalog
        spec = probe_system()
        cfg  = _load_cfg()
        in_use = {m for m in (cfg.models.text_model, cfg.models.vision_model,
                              cfg.models.effective_council_model) if m}

        installed = _installed_models()          # {name: size_mb} from Ollama
        catalog = tier_catalog(spec.get("vram_mb"))
        for t in catalog:
            t["installed"] = all(m in installed for m in t["models"])
            # Only the models we don't already have need downloading.
            t["to_download_mb"] = sum(_model_size_mb(m) or 0
                                      for m in t["models"] if m not in installed)
            t["current"] = (t["text_model"] == cfg.models.text_model
                            and (t["vision_model"] or "") == (cfg.models.vision_model or ""))

        return {
            "specs": spec,
            "tiers": catalog,
            "installed": [{"name": n, "size_mb": s, "in_use": n in in_use}
                          for n, s in sorted(installed.items())],
            "installed_total_mb": sum(installed.values()),
            "current_models": {"text": cfg.models.text_model,
                               "vision": cfg.models.vision_model,
                               "council": cfg.models.effective_council_model},
            "rescan": dict(_rescan_state),
        }

    @app.post("/api/system/models/select")
    def system_models_select(body: dict):
        """Switch to a named model set. Downloads anything missing FIRST, then swaps the config
        (see _apply_new_models_bg) — the current model keeps working until the new one is ready.
        A set larger than the detected VRAM is allowed (the probe can be wrong, and Ollama will
        offload the overflow to CPU) but the response says so."""
        import threading
        from web_watcher.gpu_detect import probe_system, tier_catalog
        name = (body or {}).get("tier")
        spec = probe_system()
        match = next((t for t in tier_catalog(spec.get("vram_mb")) if t["tier_name"] == name), None)
        if not match:
            raise HTTPException(404, detail=f"Unknown model set {name!r}")
        if _rescan_state.get("status") == "pulling":
            raise HTTPException(409, detail="A model download is already running.")

        rec = {"text_model": match["text_model"], "vision_model": match["vision_model"],
               "council_model": match["council_model"]}
        threading.Thread(target=_apply_new_models_bg, args=(rec,), daemon=True).start()

        warn = None
        if not match["fits"]:
            warn = (f"{match['tier_name']} needs about {match['min_vram_mb'] // 1000}GB of video "
                    f"memory and this machine reports {(spec.get('vram_mb') or 0) // 1000}GB. "
                    "It will still run, but partly on the CPU — expect it to be slow.")
        return {"tier": match["tier_name"], "warning": warn,
                "message": (f"Switching to the {match['tier_name']} model set. Downloading what's "
                            "missing in the background — your current model keeps working until "
                            "it's ready, then it switches automatically.")}

    @app.post("/api/system/models/delete")
    def system_models_delete(body: dict):
        """Remove a downloaded model to free disk. The models the app is CURRENTLY using are
        protected — deleting one would break chat and every watch until it was re-downloaded, and
        there's no need: switch model sets first, which leaves the old model unused and deletable."""
        name = ((body or {}).get("name") or "").strip()
        if not name:
            raise HTTPException(400, detail="No model named.")
        cfg = _load_cfg()
        in_use = {m for m in (cfg.models.text_model, cfg.models.vision_model,
                              cfg.models.effective_council_model) if m}
        if name in in_use:
            raise HTTPException(409, detail=(
                f"{name} is the model Web Watcher is using right now. Switch to a different model "
                "set first — then this one becomes unused and you can delete it."))
        try:
            with httpx.Client(timeout=30.0) as client:
                r = client.request("DELETE", f"{OLLAMA_URL}/api/delete", json={"name": name})
                r.raise_for_status()
        except Exception as exc:
            raise HTTPException(502, detail=f"Ollama could not delete {name}: {exc}")
        log.info("deleted model %s", name)
        return {"deleted": name}

    # ------------------------------------------------------------------
    # Auto-update
    # ------------------------------------------------------------------

    @app.get("/api/update/status")
    def update_status():
        """Current version + whether a newer release is downloaded and ready to apply."""
        try:
            return manager.update_status()
        except Exception as exc:
            return {"current": "", "available": None, "staged": False,
                    "configured": False, "error": str(exc)}

    @app.post("/api/update/check")
    def update_check():
        """Force an immediate check (+ download/stage if a newer release exists)."""
        return manager.check_updates_now()

    @app.post("/api/update/apply")
    def update_apply():
        """Apply whichever update is waiting. A full-installer update (runtime bump) runs the
        verified .exe and closes the app; a code update flags a restart so launcher.py swaps the
        new code in and relaunches. 409 if nothing is ready."""
        installer_waiting = manager.update_status().get("installer_ready")
        if manager.run_installer():
            return {"ok": True, "restarting": True, "kind": "installer"}
        if installer_waiting:
            # It was ready but wouldn't launch — say so rather than "nothing to apply".
            raise HTTPException(502, detail=manager.update_status().get("error")
                                or "The update installer wouldn't start.")
        if manager.request_restart():
            return {"ok": True, "restarting": True, "kind": "code"}
        raise HTTPException(409, detail="No update is ready to apply.")

    @app.post("/api/reset")
    def factory_reset(body: dict):
        """DESTRUCTIVE: erase all personal data (watches, results, DB, saved logins, history)
        and restart fresh. Requires body {"confirm": "ERASE EVERYTHING"} as a server-side
        guard on top of the UI's multi-step confirmation, so no stray/accidental call can
        wipe data."""
        if (body or {}).get("confirm") != "ERASE EVERYTHING":
            raise HTTPException(400, detail="Reset not confirmed.")
        manager.request_reset()
        return {"ok": True, "resetting": True}

    # ------------------------------------------------------------------
    # Notification preview
    # ------------------------------------------------------------------

    @app.get("/api/notifications/preview")
    def notification_preview(watch: str | None = None, run_id: int | None = None):
        from datetime import datetime, timezone
        from web_watcher.storage import get_history, get_last_run, get_run_by_id
        from web_watcher.notify import _format_message, NotificationPayload
        from web_watcher.reasoning import ReasoningResult

        if run_id:
            record = get_run_by_id(run_id)
        elif watch:
            record = get_last_run(watch)
        else:
            record = (get_history(limit=1) or [None])[0]
        if not record:
            return {"found": None, "watch_name": watch, "html": None, "telegram": None,
                    "subject": None, "run_timestamp": None}

        result = ReasoningResult(
            found=bool(record["found"]),
            summary=record.get("summary") or "(no summary)",
            confidence=record.get("confidence") or "low",
            link=record.get("link"),
        )
        ts_raw = record.get("run_timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_raw)
        except Exception:
            ts = datetime.now(timezone.utc)

        payload = NotificationPayload(watch_name=record["watch_name"], result=result, timestamp=ts)
        cfg = _load_cfg()
        tg_ok  = bool(cfg.notifications.telegram.bot_token and cfg.notifications.telegram.chat_id)
        em_ok  = bool(cfg.notifications.email.from_address and cfg.notifications.email.app_password
                      and cfg.notifications.email.to_address)

        return {
            "watch_name":    record["watch_name"],
            "run_timestamp": ts_raw,
            "found":         bool(record["found"]),
            "confidence":    record.get("confidence"),
            "subject":       f"[Web Watcher] {record['watch_name']} — match found",
            "html":          _format_message(payload, html=True),
            "telegram":      _format_message(payload, html=False),
            "telegram_configured": tg_ok,
            "email_configured":    em_ok,
            "has_screenshot":      bool(record.get("screenshot_path")),
        }

    # ------------------------------------------------------------------
    # Static files — must be last
    # ------------------------------------------------------------------

    class _NoCacheStaticFiles(StaticFiles):
        """StaticFiles that forbids caching the HTML. Without a Cache-Control header the
        embedded WebView2 cached the dashboard page and kept rendering the OLD ui after a
        self-update — the live version badge on a stale page made updates look broken. The
        page is ~130 KB from localhost; refetching it every load costs nothing."""
        def file_response(self, *args, **kwargs):
            resp = super().file_response(*args, **kwargs)
            if getattr(resp, "media_type", "") == "text/html":
                resp.headers["Cache-Control"] = "no-store, max-age=0"
            return resp

    app.mount("/", _NoCacheStaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_known_service(name: str) -> None:
    if name not in _KNOWN_SERVICES:
        raise HTTPException(status_code=404, detail=f"Unknown service: {name!r}")


def _validation_detail(exc) -> list[dict]:
    """A JSON-safe rendering of a pydantic ValidationError. exc.errors() embeds the raw
    ValueError object under ctx for model-validator failures — FastAPI can't serialize
    that, so the intended 400 exploded into a 500 (the create-watch regression)."""
    return [
        {"loc": list(e.get("loc") or ()), "msg": str(e.get("msg", "")), "type": str(e.get("type", ""))}
        for e in exc.errors()
    ]


_WATCH_INT_FIELDS = ("min_rating", "interval_minutes", "continuous_max_alerts",
                     "max_agent_steps", "continuous_scroll_passes", "continuous_idle_seconds")


def _coerce_watch_numbers(body: dict) -> dict:
    """The chat model sometimes hands an integer field a value as TEXT — "3 stars", "3.0",
    "every 6 hours" — which Pydantic rejects, so the whole create/edit fails with a validation
    error the user can do nothing about (the live 'min_rating: Input should be a valid integer'
    that lost a MacGregor sailboat watch). Pull the first integer out of such a value; DROP a
    field we can't read so its model default applies rather than blocking the watch. min_rating
    is clamped to its 1-5 range so an over-eager '10' can't fail the range validator either."""
    if not isinstance(body, dict):
        return body
    out = dict(body)
    for f in _WATCH_INT_FIELDS:
        if f not in out:
            continue
        v = out[f]
        if isinstance(v, bool):            # bool is an int subclass — treat as unset
            out.pop(f)
            continue
        if isinstance(v, int):
            n = v
        elif isinstance(v, float):
            n = int(v)
        else:
            m = re.search(r"-?\d+", str(v))
            if not m:
                out.pop(f)                             # unreadable → fall back to the default
                continue
            n = int(m.group())
        out[f] = max(1, min(5, n)) if f == "min_rating" else n
    return out


def _backfill_schedule(body: dict) -> dict:
    """Give a schedule-less watch body a sane default (every 30 min) instead of failing
    validation. The chat extractor often proposes a watch with no interval/cron at all —
    the card should still create cleanly rather than erroring at the user."""
    if (body.get("mode") != "continuous"
            and body.get("interval_minutes") in (None, "", 0)
            and not body.get("cron_expression")):
        body = dict(body)
        body.pop("cron_expression", None)
        body["interval_minutes"] = 30
    return body


def _load_cfg():
    from web_watcher.config import load
    return load()


# Sites that serve ONE flat domain (no per-city subdomain). The local model tends to
# pattern-match from Craigslist ("seattle.craigslist.org") and invent bogus city
# subdomains like "seattle.offerup.com" — a host that doesn't resolve. We rewrite those
# deterministically on watch create/update so a model hallucination can't ship a dead URL.
# Craigslist / Kijiji are intentionally ABSENT — they DO use real city subdomains.
_FLAT_SITES = frozenset({
    "offerup.com", "ebay.com", "cargurus.com", "cars.com", "autotrader.com",
    "facebook.com", "govdeals.com", "publicsurplus.com", "mercari.com", "nextdoor.com",
    "gumtree.com",
})


def _merge_watch_update(existing, body: dict, watch_name: str):
    """Apply a partial update `body` onto an EXISTING watch and return a validated Watch.

    Merging (rather than validating `body` as a standalone Watch) is what makes the
    assistant's "update" suggestions work: they usually carry only the changed fields
    (e.g. urls + instruction) and omit mode/interval/id. Validated alone, such a body
    defaults mode to "schedule" and then fails "must specify interval_minutes or
    cron_expression". Here everything untouched is preserved, the stable id is kept (so
    listing history isn't orphaned), the name is authoritative, and assistant-only extras
    like "action"/"search_terms" are ignored (not Watch fields)."""
    from web_watcher.config import Watch
    merged = existing.model_dump()
    for k, v in (body or {}).items():
        if k in Watch.model_fields and k != "id":
            merged[k] = v
    merged["name"] = watch_name
    merged["id"]   = existing.id
    return Watch.model_validate(merged)


def _normalize_marketplace_urls(urls, hint_text: str = "") -> tuple[list, list]:
    """Rewrite obviously-wrong hosts (a city/geo subdomain on a flat site → the bare
    domain). Returns (normalized_urls, changes) where changes is a list of (before, after).

    hint_text (the watch's instruction) lets a URL be localized from what the user ASKED —
    "vehicles in Anacortes" fixes a card whose URL points at the wrong region / a bogus zip,
    so the card shows the right location immediately instead of only self-healing on the
    first sweep."""
    from urllib.parse import urlparse, urlunparse
    from web_watcher.cl_geo import refine_search_url, url_zip, zip_from_text
    from web_watcher.storage import site_key
    hint_zip = zip_from_text(hint_text or "")
    out, changes = [], []
    for u in urls or []:
        try:
            p = urlparse(u)
            host = (p.netloc or "").lower()
            sk = site_key(u)
            if sk in _FLAT_SITES and host not in (sk, "www." + sk):
                u2 = urlunparse(p._replace(netloc=sk))
            else:
                u2 = u
            # Per-site refine: junk query text → real params, wrong/invented locations
            # corrected (craigslist region, OfferUp canonical /search, eBay _stpos…). When
            # the URL carries no valid location of its own, seed it from the instruction.
            nu = refine_search_url(u2, fallback_zip=hint_zip) if hint_zip and url_zip(u2) is None \
                else refine_search_url(u2)
            if nu != u:
                changes.append((u, nu))
            out.append(nu)
        except Exception:
            out.append(u)
    # Watch-level location propagation: if ANY url in this watch is localized to a zip
    # (craigslist postal, eBay _stpos) — or the instruction named one — give that zip to the
    # urls that lack one. "vehicles in anacortes on craigslist and ebay" localizes BOTH.
    try:
        watch_zip = next((z for z in (url_zip(u) for u in out) if z), None) or hint_zip
        if watch_zip:
            for i, u in enumerate(out):
                nu = refine_search_url(u, fallback_zip=watch_zip)
                if nu != u:
                    changes.append((u, nu))
                    out[i] = nu
    except Exception:
        pass
    return out, changes


_WATCHER_SYSTEM = """\
You are THE WATCHER — the oversight agent of Web Watcher. You run in the background and
keep an eye on the user's watches all day, narrating what you see. The user is now talking
to you directly in the Watches tab.

Voice: first person, warm, concise, a little watchful — you're the one who's been keeping
vigil. Lead with what you've actually OBSERVED (see "WHAT YOU'VE RECENTLY OBSERVED" below
and the health lines) rather than generic answers. If a watch is finding nothing, erroring,
or stopped, say so plainly and offer the fix.

You have EVERY ability the main assistant has — create/edit watches (watch_suggestion),
manage them (watch_actions: start/stop/enable/disable/delete), and look up what's been
found (listing_query). Use them to actually help, not just describe. When the user asks you
to fix or change something, propose the concrete action. Reply in the SAME JSON format
described below.

REVIEWING A WATCH: when the user asks how a watch is doing / to review or fix it, read that
watch's HEALTH line below and answer from it honestly — how many it has matched vs seen, and
any error. If a health line carries a DIAGNOSIS, act on it: explain it plainly and propose a
concrete fix as an update (broaden the search terms, relax a keyword filter, or lower
min_rating) so the user can apply it in one click. Don't claim a watch is fine if its health
says it has matched nothing."""


from web_watcher import paths
_WATCHER_HISTORY_PATH = paths.watcher_history_path()


def _history_path(owner: str | None = None):
    """Where a conversation is stored. The desktop dock (owner=None) uses the main file; each
    Telegram person (owner = their chat_id) gets their OWN thread, so two people sharing the bot
    don't see each other's conversation. The owner is sanitized to safe filename characters."""
    if not owner:
        return _WATCHER_HISTORY_PATH
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(owner))[:40] or "user"
    return _WATCHER_HISTORY_PATH.with_name(f"watcher_history_{safe}.json")


_TG_TAG_RE = re.compile(r"</?(b|strong|i|em|u|s|code|pre|a|br)\b[^>]*>", re.I)


def _strip_html(text: str) -> str:
    """Telegram-legal markup → plain text, for anything we STORE rather than send. Unescapes the
    handful of entities the formatter introduces so a saved reply reads exactly as it was said."""
    if not text or ("<" not in text and "&" not in text):
        return text or ""
    out = _TG_TAG_RE.sub("", text)
    for ent, ch in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
        out = out.replace(ent, ch)
    return out


def _shown_summary(result: dict) -> dict:
    """A compact, display-only record of what a turn actually put on screen — the listings and any
    proposed watch — so the admin Chats viewer can reconstruct the context, not just the prose.
    Small on purpose (it lives in the saved history): a handful of listings, key fields only."""
    out: dict = {}
    listings = result.get("listings")
    if isinstance(listings, list) and listings:
        rows = []
        for r in listings[:8]:
            if not isinstance(r, dict):
                continue
            rows.append({
                "title":  str(r.get("title") or "")[:120],
                "price":  str(r.get("price_text") or r.get("price") or "")[:24],
                "url":    str(r.get("url") or "")[:400],
                "rating": r.get("rating"),
            })
        if rows:
            out["listings"] = rows
            if len(listings) > len(rows):
                out["more"] = len(listings) - len(rows)
    sugg = (result.get("watch_suggestions")
            or ([result["watch_suggestion"]] if result.get("watch_suggestion") else []))
    names = [str(s.get("name") or "").strip() for s in sugg if isinstance(s, dict)]
    names = [n for n in names if n]
    if names:
        out["proposed_watches"] = names[:5]
    return out


def _recent_shown_block(messages: list, owner: str | None) -> str:
    """The listings this conversation just put on screen, numbered as the user saw them — so
    "how much is the second one?" is answerable.

    The `shown` record was deliberately kept out of the model's context to avoid bloat — and
    the result was amnesia: the assistant showed a list and, one turn later, denied any
    knowledge of it ("I don't have information about any previous matches"). This narrow
    re-injection fixes that: ONLY the most recent shown list, ONLY when it belongs to THIS
    conversation (its prose must appear in the transcript the client sent — saved history can
    hold older threads), and compact by construction (`shown` is already capped at 8 rows).
    """
    if not any(isinstance(m, dict) and m.get("role") == "assistant" for m in (messages or [])):
        return ""                      # first turn of a fresh chat — nothing was shown yet
    try:
        history = _load_watcher_history(owner)
    except Exception:
        return ""
    sent = {str(m.get("content") or "").strip()
            for m in messages if isinstance(m, dict) and m.get("role") == "assistant"}
    for m in reversed(history):
        if not (isinstance(m, dict) and m.get("role") == "assistant"):
            continue
        shown = (m.get("shown") or {}).get("listings") or []
        if not shown:
            continue
        if str(m.get("content") or "").strip() not in sent:
            continue                   # a different (older) conversation's list — not this one
        lines = []
        for i, r in enumerate(shown, 1):
            bits = [str(r.get("title") or "?")[:90]]
            if r.get("price"):
                bits.append(str(r.get("price")))
            if r.get("rating") is not None:
                bits.append(f"rated {r.get('rating')}")
            line = f"  {i}. " + " — ".join(bits)
            if r.get("url"):
                line += f"\n     {str(r.get('url'))[:200]}"
            lines.append(line)
        return ("LISTINGS YOU JUST SHOWED THE USER (numbered exactly as they saw them — "
                "“the second one” means #2 below):\n" + "\n".join(lines) + "\n"
                "Answer follow-ups about these items from THIS list — price, location, which "
                "one is which, links. If a detail isn't here, say what IS known and offer to "
                "vet the listing rather than claiming you have no context.")
    return ""


_WHY_VERDICT_RE = re.compile(
    r"\bwhy\b.{0,80}\b(?:reject|skip|drop|refus|rate|flag|not (?:a )?match|"
    r"did ?n[o']?t (?:it |you )?(?:match|keep|like|show))"
    r"|\bwhy (?:isn'?t|wasn'?t|didn'?t|no)\b", re.I | re.S)

_WHY_STOPWORDS = frozenset({
    "why", "did", "didn", "you", "your", "the", "that", "this", "these", "those", "watch",
    "watches", "listing", "listings", "reject", "rejected", "match", "matched", "matches",
    "wasn", "isn", "about", "have", "what", "from", "with", "show", "shown", "keep", "kept",
    "drop", "dropped", "skip", "skipped", "rate", "rated", "flag", "flagged", "there", "then",
})


def _verdict_context(text: str, cfg, owner: str | None) -> str:
    """The judge's RECORDED verdicts for the listing(s) a "why did you reject…" question is
    about — real rows, real reasons, so the answer comes from the database instead of a guess.

    Observed live: asked "Why did you reject the Macgregor in Bothell?", the assistant invented
    a plausible story ("very specific search terms…") and blamed the wrong watch — while the
    store held the actual verdict for that exact listing. Guessing when the truth is one query
    away is the definition of regurgitation; this injects the truth.
    """
    if not _WHY_VERDICT_RE.search(text or ""):
        return ""
    toks = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9'’-]{3,}", text or "")
            if w.lower() not in _WHY_STOPWORDS][:6]
    if not toks:
        return ""
    try:
        from web_watcher.storage import query_listings
        watches = _watches_for_owner(cfg, owner) or list(cfg.watches)
        hits: dict[str, tuple] = {}
        for w in watches[:12]:
            wid = getattr(w, "id", None) or w.name
            for t in toks[:2]:
                try:
                    rows = query_listings(watch_id=wid, text=t, limit=15)
                except Exception:
                    continue
                for r in rows:
                    title = str(r.get("title") or "").lower()
                    score = sum(1 for tk in toks if tk in title)
                    key = r.get("listing_key") or r.get("url") or title
                    if score and (key not in hits or score > hits[key][0]):
                        hits[key] = (score, w.name, r)
        if not hits:
            return ""
        best = sorted(hits.values(), key=lambda h: -h[0])[:3]
        lines = []
        for _score, wname, r in best:
            verdict = "MATCHED" if r.get("matched") else "rejected"
            reason = str(r.get("judge_reason") or "no reason recorded").strip()
            rating = r.get("rating")
            lines.append(f"  • “{str(r.get('title') or '')[:90]}” — {verdict}"
                         + (f", rated {rating}" if rating is not None else "")
                         + f": {reason[:140]}  (watch: {wname})")
        return ("RECORDED VERDICTS from the database for the listing(s) the user is asking "
                "about — answer their WHY from these facts, never from a guess:\n"
                + "\n".join(lines))
    except Exception as exc:
        log.debug("verdict context failed: %s", exc)
        return ""


def _persist_chat_turn(messages: list, result: dict, owner: str | None) -> None:
    """Save the exchange on EVERY turn that had a user message — including degraded/error turns —
    so a transient model hiccup can't punch a permanent hole in the saved chat. (An older gate only
    saved fully-successful turns, which silently dropped BOTH sides on any error — the "chat stopped
    logging" report.) Uses "raw" (clean prose) when present, else the shown message."""
    last = messages[-1] if messages else None
    if not (isinstance(last, dict) and last.get("role") == "user"):
        return
    import time as _t
    now = _t.time()
    # Store PLAIN TEXT. Markup belongs to the delivery channel, not the record: a reply saved
    # with <b> tags is read back to the model as its own past work, and it starts writing tags
    # into its prose — which is how literal "<i>Change yours:</i>" reached a phone. Formatting is
    # applied at send time (notify/_send); history keeps what was actually said.
    reply_text = _strip_html(result.get("raw") or result.get("message") or "")
    n_sugg = len(result.get("watch_suggestions") or
                 ([result["watch_suggestion"]] if result.get("watch_suggestion") else []))
    log.info("Watcher chat turn: user=%r → reply %d char(s), %d suggestion(s)",
             str(last.get("content", ""))[:80], len(reply_text), n_sugg)
    try:
        history = _load_watcher_history(owner)
        # Stamp both turns so the UI can show "when" dividers on scroll-back. Keep the client's
        # own ts if it sent one (the user typed slightly before we replied).
        user_msg = dict(last)
        user_msg.setdefault("ts", now)
        history.append(user_msg)
        assistant_turn = {"role": "assistant", "content": reply_text, "ts": now}
        # Record WHAT WAS SHOWN alongside the prose, so the admin's Chats viewer can render the
        # full context a person saw — not just "here are 5 matches" with the matches gone. Kept in
        # a SEPARATE display-only field (never in `content`): the model is fed role+content only,
        # so this can't bloat or pollute the conversation it reads back. Compact by design —
        # title/price/url/rating per listing, capped — this is an oversight aid, not the store.
        shown = _shown_summary(result)
        if shown:
            assistant_turn["shown"] = shown
        history.append(assistant_turn)
        _save_watcher_history(history[-200:], owner)
    except Exception as exc:
        log.warning("Watcher chat: could not persist turn: %s", exc)


def _load_watcher_history(owner: str | None = None) -> list:
    try:
        p = _history_path(owner)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save_watcher_history(history: list, owner: str | None = None) -> None:
    try:
        p = _history_path(owner)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("Could not save watcher history: %s", exc)


# --- Group suspend memory ---------------------------------------------------------
# When the console stops ALL of one person's watches, we remember exactly which ones were on at
# the time, so starting the group back up restores that set instead of blanket-enabling — a watch
# they had deliberately deactivated must stay deactivated through a group stop/start cycle.
_SUSPENDED_PATH = _WATCHER_HISTORY_PATH.with_name("owner_suspended.json")


def _load_suspended() -> dict:
    try:
        if _SUSPENDED_PATH.exists():
            d = json.loads(_SUSPENDED_PATH.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _save_suspended(data: dict) -> None:
    try:
        _SUSPENDED_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SUSPENDED_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("could not save the group-suspend memory: %s", exc)


# --- Owner display names ----------------------------------------------------------
# A Telegram thread is keyed by chat_id (a bare number). So the admin console can show a
# human name instead of "5821334", we remember the sender's Telegram name the first time
# they talk. This is display sugar only — the security boundary is still the chat_id.
_OWNER_NAMES_PATH = _WATCHER_HISTORY_PATH.with_name("watcher_owners.json")


def _load_owner_names() -> dict:
    try:
        if _OWNER_NAMES_PATH.exists():
            d = json.loads(_OWNER_NAMES_PATH.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return {str(k): str(v) for k, v in d.items()}
    except Exception:
        pass
    return {}


def _record_owner_name(owner: str | None, name: str) -> None:
    owner, name = str(owner or "").strip(), str(name or "").strip()
    if not owner or not name:
        return
    try:
        d = _load_owner_names()
        if d.get(owner) == name:
            return
        d[owner] = name
        _OWNER_NAMES_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("could not record owner name: %s", exc)


# --- Per-person check-in preference (set from the bot: "check in every 6 hours") ---
# Each person controls their OWN quiet-period check-in cadence; the config value is the default
# for anyone who hasn't set one. Stored beside the other per-owner state.
_CHECKIN_PREFS_PATH = _WATCHER_HISTORY_PATH.with_name("checkin_prefs.json")


def _load_checkin_prefs() -> dict:
    try:
        if _CHECKIN_PREFS_PATH.exists():
            d = json.loads(_CHECKIN_PREFS_PATH.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return {str(k): float(v) for k, v in d.items()}
    except Exception:
        pass
    return {}


def _set_checkin_pref(owner: str, hours: float) -> None:
    owner = str(owner or "").strip()
    if not owner:
        return
    try:
        d = _load_checkin_prefs()
        d[owner] = max(0.0, float(hours))
        _CHECKIN_PREFS_PATH.write_text(json.dumps(d), encoding="utf-8")
    except Exception as exc:
        log.warning("could not save check-in preference: %s", exc)


# "check in every 6 hours" / "twice a day" / "stop checking in" → hours (0 = off), else None.
_CHECKIN_RE = re.compile(
    r"\b(check(?:ing)?[- ]?ins?|check on me|update me|ping me|notify me|status update)\b", re.I)
_EVERY_HOURS_RE = re.compile(r"\bevery\s+(\d+(?:\.\d+)?)\s*(hours?|hrs?|h)\b", re.I)
_EVERY_DAY_RE   = re.compile(r"\b(once|twice|(\d+)\s*times?)\s+(a|per)\s+day\b", re.I)
_STOP_CHECKIN_RE = re.compile(r"\b(stop|don'?t|no more|disable|turn off|quit)\b", re.I)


def _classify_checkin(text: str):
    """Parse a check-in cadence request into hours (0 = turn them off), or None if the message
    isn't about check-ins. Deterministic — a settings change shouldn't ride on the 14b."""
    t = text or ""
    if not _CHECKIN_RE.search(t):
        return None
    if _STOP_CHECKIN_RE.search(t):
        return 0.0
    m = _EVERY_HOURS_RE.search(t)
    if m:
        try:
            return max(0.0, float(m.group(1)))
        except ValueError:
            return None
    m = _EVERY_DAY_RE.search(t)
    if m:
        word = (m.group(1) or "").lower()
        n = 1.0 if word.startswith("once") else 2.0 if word.startswith("twice") else None
        if n is None:
            try:
                n = float(m.group(2))
            except (TypeError, ValueError):
                return None
        return 24.0 / n if n > 0 else None
    return None


# --- The settings block ("show me my settings") ------------------------------------
# Answered deterministically (no model call): it must be accurate, instant, and correctly
# SCOPED — everyone sees their own settings; only the owner also sees server-wide ones.
_SETTINGS_RE = re.compile(
    r"\b(settings?|preferences?|options|configuration|config|my setup)\b", re.I)


def _is_settings_request(text: str) -> bool:
    t = text or ""
    if not _SETTINGS_RE.search(t):
        return False
    # "change the settings for X" is a request to DO something; a bare ask is a request to SEE.
    return not re.search(r"\b(change|set|update|edit|turn|make)\b", t, re.I)


# "Show me the match" must SHOW the match. The 14b's extractor keeps classifying this as ordinary
# conversation, so the reply describes the watch's CRITERIA back at you — "it's an under 30-foot
# motor boat, priced within $15,000" — which is what was asked FOR, not what was found. Same class
# of miss as settings and start/stop, so it gets the same treatment: decided in code, not by a
# small model that has already proved it gets this wrong.
_LOOKUP_RE = re.compile(
    r"\b("
    r"show\s+(me|us|it|them)?|show\s+me\s+the|"
    r"list\s+(them|it|the|out|all)|let'?s\s+see|lemme\s+see|"
    r"what\s+(did|have)\s+(you|it|we)\s+(find|found|got)|"
    r"what'?s\s+(it|the\s+watch)?\s*found|"
    r"any(thing)?\s+(new|on|from|for|good|yet)|got\s+anything|"
    r"the\s+(match|matches|listing|listings|find|finds|hits?|results?)\b|"
    r"top\s+\d+|best\s+(ones?|matches|listings|finds)"
    r")", re.I)

# Words that mean "make/change a watch", never "show me what you found". A message with these is
# not a lookup no matter what else it says, so "show me how to set up a watch" can't hijack.
_NOT_LOOKUP_RE = re.compile(
    r"\b(set\s*up|create|make\s+a\s+(new\s+)?watch|add\s+a\s+watch|start\s+watching|"
    r"delete|remove|pause|stop|resume|change|edit|update)\b", re.I)


def _is_lookup_request(text: str) -> bool:
    """True when the person is asking to SEE what's been found. Deliberately conservative: a
    false positive shows listings nobody asked for, which is far less costly than the current
    false negative (describing the search criteria instead of the results)."""
    t = (text or "").strip()
    if not t or _NOT_LOOKUP_RE.search(t):
        return False
    return bool(_LOOKUP_RE.search(t))


# Fill-in-the-blank scaffolding a small model produces when asked to list things it was about to
# be handed: bracketed slots ("[Boat Title]", "[Link to Listing]") and masked numbers ("$XX,XXX").
_PLACEHOLDER_RE = re.compile(
    r"\[(?:[A-Z][\w' ]{2,28}|link[^\]]{0,20}|url[^\]]{0,20}|title[^\]]{0,20}|"
    r"price[^\]]{0,20}|description[^\]]{0,20})\]"
    r"|\$\s?X[X,\.]*X"
    r"|\bxx+[,\.]?xx+\b",
    re.I)


def _looks_like_a_blank_template(text: str) -> bool:
    """True when a reply is a listing template with the values left blank rather than an answer.

    Needs MORE THAN ONE marker: a single bracketed phrase is ordinary writing ("[see below]",
    "[I'll keep looking]"), but two is a pattern, and a pattern is a template. Three was too
    lenient — a two-row blank table came in just under it."""
    return len(_PLACEHOLDER_RE.findall(text or "")) >= 2


# A reply that walks through listings item by item: "**Match 1:** … Title: … Price: $12,000".
_ENUM_LINE_RE = re.compile(r"^\s*(?:[-*•‣]|\d+[.)])\s+\S", re.M)
_ITEM_FIELD_RE = re.compile(r"\b(?:title|price|location|description|link|url|match\s*\d)\s*[:：]", re.I)


def _prose_enumerates_listings(text: str) -> bool:
    """True when the reply is itself walking through a list of items.

    This is the guard against the worst failure this assistant has: asked for the matches, the
    small model writes out plausible listings it INVENTED — "20ft Motor Boat with Outboard
    Engine, $12,000, Everett" — while the real rows travel alongside it. Blank placeholders are
    obviously wrong to a reader; convincing fabrications are not, and someone could drive to
    Everett for a boat that never existed.

    So whenever we hold the real rows, the model does not get to narrate them. It needs a
    genuine list shape (several bulleted/numbered items, or repeated Title:/Price: fields) —
    ordinary prose that merely mentions one price is left alone."""
    t = text or ""
    if len(_ITEM_FIELD_RE.findall(t)) >= 3:
        return True
    bullets = [m for m in _ENUM_LINE_RE.findall(t)]
    if len(bullets) < 2:
        return False
    # Bulleted lines that carry a price or a listing field — a plain bulleted answer is fine.
    listish = sum(1 for line in t.splitlines()
                  if _ENUM_LINE_RE.match(line) and (re.search(r"\$\s?\d", line)
                                                    or _ITEM_FIELD_RE.search(line)))
    return listish >= 2


# A lead-in to a list that's about to be shown needs a sentence or two. Past this, the model has
# stopped introducing the listings and started re-describing the search or inventing entries.
_LEAD_IN_MAX_CHARS = 320


def _should_replace_prose(message: str) -> bool:
    """True when the model's words should give way to the real listing rows.

    Detecting each fabrication shape one at a time is a losing game — the first attempt caught
    blank placeholders, the next caught invented listings, and the one after slipped through by a
    single marker ("- [Match Details]" twice where three were needed). So the test is inverted:
    when we already hold the rows, the model's job is a LEAD-IN, and a lead-in is short. Anything
    long is re-describing the search or making entries up, and the rows say it better anyway.

    A short, genuine remark ("the Sea Ray at $14,500 looks like the best value") is well under the
    limit and survives — which is the one thing worth keeping from the model here."""
    text = (message or "").strip()
    if not text:
        return True
    if _looks_like_a_blank_template(text) or _prose_enumerates_listings(text):
        return True
    return len(text) > _LEAD_IN_MAX_CHARS


# Words in a watch's name that don't identify it — every watch has some of these.
_WATCH_NAME_NOISE = frozenset({
    "watch", "watches", "the", "and", "for", "with", "under", "over", "near", "within", "all",
    "my", "a", "an", "of", "in", "on", "to", "up", "new", "used", "only", "miles", "mile",
})


def _watch_named_in(text: str, cfg, owner) -> str:
    """The watch the person is talking about, matched on a distinctive word of its name.

    "show me the matches for boats" plainly means the boats watch — but with two watches and no
    focus set, the lookup ran unscoped and returned boats mixed with a Nissan Rogue from the
    other one. Returns "" unless EXACTLY ONE watch matches, because acting on an ambiguous
    reference is how the wrong watch gets shown."""
    words = {w for w in re.findall(r"[a-z0-9']+", (text or "").lower())
             if len(w) > 2 and w not in _WATCH_NAME_NOISE}
    if not words:
        return ""
    hits = []
    for w in _watches_for_owner(cfg, owner):
        name_words = {x for x in re.findall(r"[a-z0-9']+", w.name.lower())
                      if len(x) > 2 and x not in _WATCH_NAME_NOISE}
        # Match on the singular/plural stem so "boats" finds "Boat" and vice versa.
        if any(a == b or a.rstrip("s") == b.rstrip("s") for a in words for b in name_words):
            hits.append(w.name)
    return hits[0] if len(hits) == 1 else ""


# A question ABOUT the watches themselves — which exist, which are on, what's on the watchlist.
# This is never a request to see listings, whatever the model decides to attach. Distinct from a
# lookup ("show me the matches"), which asks to see what was FOUND.
_WATCH_STATUS_RE = re.compile(
    r"\b("
    r"what\s+watches|which\s+watches|any\s+watches|"
    r"watches?\s+(are\s+)?(running|active|on|off|going|set\s*up|do\s+i\s+have)|"
    r"watch\s*list|watchlist|"
    r"(running|active)\s+watches|"
    r"list\s+(my\s+|the\s+)?watches|my\s+watches\b|"
    r"how\s+many\s+watches|status\s+of\s+(my\s+)?watches"
    r")\b", re.I)


def _is_watch_status_request(text: str) -> bool:
    """True when the person is asking about the WATCHES (which exist / are running), not to see
    the listings a watch has found. A status answer must never drag a wall of matches with it."""
    return bool(_WATCH_STATUS_RE.search(text or ""))


# An imperative about the watches. "Turn off all my watches. Actually wait, keep the MacGregor
# one on, kill the rest" matched the status regex (via "my watches") and got a STATUS LIST as
# its answer — nothing acted on, the contradiction never even seen. A message that tells us to
# DO something must reach the full assistant path, which grounds and executes actions.
_WATCH_ACTION_WORDS_RE = re.compile(
    r"\b(turn (?:off|on)|shut (?:down|off)|switch (?:off|on)|stop|start|kill|pause|resume|"
    r"enable|disable|delete|remove|reset|restart|create|add|set ?up)\b", re.I)

# The message ALSO asks what was FOUND ("…and did any of them find anything in the last day?").
# The status fast-path answers only the status half and silently drops the rest — a compound
# question deserves the model, which has the same real state in its context.
_ASKS_ABOUT_FINDS_RE = re.compile(
    r"\b(?:f(?:i|ou)nds?|found|match(?:es|ed)?|anything (?:new|good)|new listings?|"
    r"turn(?:ed)? (?:anything )?up|come (?:up|in)|seen anything)\b", re.I)


def _pure_status_request(text: str) -> bool:
    """The status fast-path fires ONLY for a message that is a status question and nothing
    else — no action to carry out, no second question about the finds riding along."""
    t = text or ""
    return (_is_watch_status_request(t)
            and not _WATCH_ACTION_WORDS_RE.search(t)
            and not _ASKS_ABOUT_FINDS_RE.search(t))


# A general "how does this work / what can you do / help" — answered from a fixed, friendly
# explainer, not the 14b (which gives a different, sometimes wrong answer every time). Deliberately
# NOT matching specific how-to questions ("how do I stop a watch") — those go to the model.
_HELP_RE = re.compile(
    r"how (?:do|does|would|can) (?:you|it|this|the bot|web ?watcher|i)\b[^?]{0,24}\bwork"
    r"|how do (?:you|i) work"
    r"|how (?:do i|to) use\b"
    r"|what (?:can|do) you do\b|what can i (?:ask|say|do)\b"
    r"|\bwhat are you\b|\bwhat is this\b|\bwho are you\b"
    r"|getting started\b|how (?:do i|to) get start"
    r"|^\s*/?help\s*$",
    re.I)


def _is_help_request(text: str) -> bool:
    return bool(_HELP_RE.search((text or "").strip()))


def _render_help(owner: str | None) -> str:
    """A fixed, friendly 'how I work' explainer — the same clear answer every time someone asks,
    instead of whatever the 14b improvises. HTML so the Telegram bridge renders the formatting."""
    return (
        "👋 <b>I'm Web Watcher — I keep an eye on marketplaces for you.</b>\n\n"
        "Tell me what you're hunting for and I'll watch Craigslist (and other sites) around the "
        "clock, then message you the moment something matches — usually with a photo.\n\n"
        "<b>Set up a watch</b> — just say what you want, for example:\n"
        "  • “Watch craigslist for a manual pickup under $10k near Anacortes”\n"
        "  • “Let me know about aluminum fishing boats under 20 feet”\n"
        "I'll ask anything I need, then start watching.\n\n"
        "<b>On each match</b> you get a card with two buttons:\n"
        "  • 🔗 <b>Open</b> — jump straight to the listing\n"
        "  • 🔍 <b>Vet</b> — I read the whole posting and rate the deal + flag scam risk\n\n"
        "<b>Handy things to say</b>:\n"
        "  • “What am I watching?” — your watches and what they've found\n"
        "  • “Show me the latest match” — your newest find\n"
        "  • “That's not a match” — I drop the last one I showed you, and can learn to skip "
        "similar ones (just give me a word to avoid, like “utility”)\n"
        "  • “Stop / start / reset the boats watch”\n"
        "  • “Only alert me on great deals”, or “raise the price to $12k”\n\n"
        "Everything runs on the owner's own computer, and your watches are private to you. "
        "Just talk to me in plain English — that's how you drive me. What should I watch for?")


_SINGULAR_LATEST_RE = re.compile(r"\b(?:latest|last|most recent|newest)\b", re.I)
# Plural / many-signals that mean the user wants a LIST even alongside "latest" ("latest matches",
# "last few"). Their presence keeps the default page size instead of collapsing to a single item.
_MANY_RE = re.compile(r"\b(?:matches|listings|finds|ones|results|few|several|all|some)\b", re.I)


def _lookup_limit(text: str, default=10):
    """How many finds to show, taken from the USER'S words. 'top 20' / 'show me 5' → that many.
    'the latest match' / 'most recent listing' (singular) → exactly ONE — that phrasing asks for a
    single item, not a page. When the words carry no count signal at all, returns `default`
    (callers pass default=None to mean 'no explicit ask — fall back to the model's/standard page').
    Bounded so a stray number can't dump the whole DB."""
    t = text or ""
    # People write counts as WORDS — "the three most recent", "top five". The digit-only
    # parsing returned the default 10 for those, so "show me the three most recent matches"
    # came back headed "Here are 10 matches".
    _words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
              "eight": 8, "nine": 9, "ten": 10, "dozen": 12, "fifteen": 15, "twenty": 20,
              "a couple of": 2, "a couple": 2, "a few": 3, "a handful of": 5, "a handful": 5}
    for w, n in sorted(_words.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(w)}\b(?=.*\b(?:most recent|recent|latest|newest|"
                     rf"matches?|listings?|finds?|results?|ones?|items?)\b)", t, re.I):
            t = re.sub(rf"\b{re.escape(w)}\b", str(n), t, count=1, flags=re.I)
            break
    m = re.search(r"\b(?:top|first|best|show me|latest|last|newest|the)\s+(\d{1,6})\b", t, re.I)
    if m:
        try:
            return max(1, min(int(m.group(1)), 30))
        except ValueError:
            pass
    # The count can also come BEFORE the noun/recency word — "the 5 most recent", "5 matches",
    # "5 recent ones". The keyword-first regex above misses these, so "5 most recent" used to fall
    # through to the singular rule and return 1 (or the model's guessed 10). Catch a leading number.
    m2 = re.search(r"\b(\d{1,6})\s+(?:most\s+recent|recent|latest|newest|"
                   r"matches?|listings?|finds?|results?|ones?|items?)\b", t, re.I)
    if m2:
        try:
            return max(1, min(int(m2.group(1)), 30))
        except ValueError:
            pass
    # "latest / last / most recent / newest <singular>" with no plural in sight → just the one.
    if _SINGULAR_LATEST_RE.search(t) and not _MANY_RE.search(t):
        return 1
    return default


def _fmt_hours(h: float) -> str:
    if not h:
        return "off"
    if abs(h - 24.0) < 0.01:
        return "once a day"
    if abs(h - 12.0) < 0.01:
        return "twice a day"
    return f"every {h:g}h"


def _render_watch_status(cfg, manager, owner: str | None) -> str:
    """A clean, well-spaced answer to "what watches do I have / what's running".

    Built here rather than left to the model: it kept running them together in one paragraph and
    mis-stating run-state. Each watch is its own block with a blank line between, led by a status
    dot, so the titles stand apart on a phone. HTML — the Telegram bridge sends it with parse mode
    and the desktop renders it too."""
    import html as _h
    mine = _watches_for_owner(cfg, owner)
    if not mine:
        return ("You don't have any watches yet. Tell me what to look for — e.g. “watch "
                "craigslist for manual pickup trucks under $10k near Anacortes”.")

    paused = False
    try:
        paused = manager.is_paused()
    except Exception:
        pass
    try:
        job_map = {j["watch_name"]: j for j in manager.get_job_info()}
    except Exception:
        job_map = {}

    def _state(w):
        if paused:
            return ("⏸", "paused (master switch is off)")
        if not getattr(w, "enabled", True):
            return ("⚪", "off — not watching")
        if getattr(w, "mode", "") == "continuous" and not job_map.get(w.name, {}).get("continuous_running"):
            return ("🟢", "on — watching (between sweeps)")
        return ("🟢", "on — watching now")

    on = sum(1 for w in mine if getattr(w, "enabled", True)) if not paused else 0
    if paused:
        head = f"⏸ <b>Watching is paused.</b> You have {len(mine)} watch(es), none running:"
    elif on == 0:
        head = f"⚪ <b>Nothing is running.</b> You have {len(mine)} watch(es), all turned off:"
    else:
        head = f"🟢 <b>{on} of {len(mine)} watch(es) running:</b>"

    blocks = [head]
    from web_watcher.storage import watch_stats
    for w in mine:
        dot, label = _state(w)
        line = [f"{dot} <b>{_h.escape(w.name)}</b>", f"   {label}"]
        try:
            st = watch_stats(w.id or w.name, w.name)
            line.append(f"   {st['matches']} matched of {st['observations']} seen")
        except Exception:
            pass
        blocks.append("\n".join(line))

    # A blank line between each block is what makes the titles stand apart.
    return "\n\n".join(blocks)


# A status question scoped to the WHOLE Watcher, not just the speaker's own watches — "is the
# whole watcher running?", "globally how many watches?", "are any OTHER watches running?". The
# normal _WATCH_STATUS_RE misses these ("watcher" isn't "watches"; "globally" carries no verb),
# so they used to fall through to the 14b and come back messy.
_GLOBAL_STATUS_RE = re.compile(
    r"\b("
    r"whole\s+watcher|entire\s+watcher|the\s+watcher\s+(running|going|on|working|up)|"
    r"is\s+(the\s+)?watcher\s+(running|on|going|up|working)|"
    r"any(thing)?\s+(else\s+)?(watches?\s+)?running|other\s+watches?|"
    r"global\w*|overall|in\s+total\b|across\s+(all|everyone|users)|"
    r"how\s+many\s+watches?\s+(are\s+there|in\s+total|total|globally|overall|running)|"
    r"total\s+(number\s+of\s+)?watches?"
    r")\b", re.I)


def _is_global_status_request(text: str) -> bool:
    return bool(_GLOBAL_STATUS_RE.search(text or ""))


def _render_global_running(cfg, manager) -> str:
    """COUNTS only — how many watches exist and how many are running across ALL users. Never lists
    another person's watch titles or details (a buddy must not learn what others watch); the admin
    already sees the per-watch detail through the normal status block. This is mainly so a buddy
    asking "is anything else running?" gets a real number instead of only their own."""
    all_w = list(getattr(cfg, "watches", []) or [])
    total = len(all_w)
    try:
        paused = bool(manager.is_paused())
    except Exception:
        paused = False
    running = 0 if paused else sum(1 for w in all_w if getattr(w, "enabled", True))
    if total == 0:
        return "No watches exist yet across the whole Watcher."
    if paused:
        return (f"⏸ <b>The whole Watcher is paused</b> — nothing is sweeping right now. "
                f"{total} watch(es) exist across everyone.")
    return (f"🟢 <b>{running} of {total} watch(es) running</b> right now across everyone "
            f"(all users combined).")


def _render_settings(cfg, manager, owner: str | None) -> str:
    """The settings block. TWO LEVELS, deliberately separated:
      • YOUR SETTINGS — what this person controls for themselves (their check-in cadence, their
        watches, where their alerts go). Everyone gets this.
      • SERVER SETTINGS — app-wide state (master switch, AI provider + spend, who may use the bot,
        version). Only the OWNER sees this; a buddy never learns about other people or the key."""
    from web_watcher.__version__ import __version__
    admin = _is_admin_owner(owner, cfg)
    # The personal block lists watches this person actually OWNS — even for the admin, who can
    # SEE everything (that total belongs in the server block, not under "Your settings").
    mine = ([w for w in cfg.watches if str(getattr(w, "owner", "") or "") == str(owner)]
            if owner else list(cfg.watches))
    lines = ["⚙️ <b>Your settings</b>"]

    # Check-in cadence: this person's own preference, else the configured default.
    default_h = float(getattr(cfg.notifications.telegram, "checkin_hours", 12.0) or 0)
    hours = _load_checkin_prefs().get(str(owner), default_h) if owner else default_h
    lines.append(f"• Quiet check-ins: <b>{_fmt_hours(float(hours))}</b>")
    running = sum(1 for w in mine if getattr(w, "enabled", True))
    lines.append(f"• Your watches: <b>{len(mine)}</b> ({running} on)")
    for w in mine[:8]:
        state = "on" if getattr(w, "enabled", True) else "off"
        lines.append(f"   – {w.name} ({state})")
    if len(mine) > 8:
        lines.append(f"   – …and {len(mine) - 8} more")
    lines.append("• Alerts: sent to this chat")
    lines += ["", "<i>Change yours:</i> “check in every 6 hours”, “twice a day”, “stop checking "
              "in”, “stop my watches”."]

    if admin:
        lines += ["", "🛠 <b>Server settings</b> (owner only)"]
        try:
            st = manager.watcher_status()
            lines.append(f"• Watching: <b>{'ON' if st.get('running') else 'PAUSED'}</b>"
                         + (f" — checking {st['current']}" if st.get("current") else ""))
        except Exception:
            pass
        lines.append(f"• Watches on this server: <b>{len(cfg.watches)}</b>")
        tg = cfg.notifications.telegram
        people = 1 + len([c for c in (getattr(tg, "allowed_chat_ids", []) or []) if c])
        lines.append(f"• People allowed to use the bot: <b>{people}</b>")
        lines.append(f"• Two-way chat: <b>{'on' if getattr(tg, 'two_way', False) else 'off'}</b>"
                     f" · default check-ins: <b>{_fmt_hours(default_h)}</b>")
        # AI provider + spend
        try:
            from web_watcher import llm
            roles = (cfg.models.cloud.roles or {})
            chat_route = roles.get("chat")
            cloud_chat = bool(chat_route and (chat_route.provider or "").lower() == "anthropic")
            judge = roles.get("judge")
            cloud_judge = bool(judge and (judge.provider or "").lower() == "anthropic")
            lines.append(f"• AI: local <b>{cfg.models.text_model}</b> · Claude for chat: "
                         f"<b>{'on' if cloud_chat else 'off'}</b> · for rating: "
                         f"<b>{'on' if cloud_judge else 'off'}</b>")
            b = llm.budget_state(cfg)
            if b.get("cap"):
                lines.append(f"• Claude spend: <b>${b['spent']:.2f}</b> of ${b['cap']:.2f} "
                             f"(~${b['remaining']:.2f} left)")
            elif b.get("spent"):
                lines.append(f"• Claude spend this month: <b>${b['spent']:.2f}</b> (no cap set)")
        except Exception:
            pass
        lines.append(f"• Version: <b>{__version__}</b>")
        lines += ["", "<i>Owner controls:</i> “pause everything” / “resume watching”, and anything "
                  "above can be changed in the app under Settings."]
    return "\n".join(lines)


# --- Telegram access requests (parked for one-click approval in the console) -------
_ACCESS_REQ_PATH = _WATCHER_HISTORY_PATH.with_name("telegram_access_requests.json")


def _load_access_requests() -> list:
    try:
        if _ACCESS_REQ_PATH.exists():
            d = json.loads(_ACCESS_REQ_PATH.read_text(encoding="utf-8"))
            if isinstance(d, list):
                return d
    except Exception:
        pass
    return []


def _save_access_requests(reqs: list) -> None:
    try:
        _ACCESS_REQ_PATH.write_text(json.dumps(reqs, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("could not save access requests: %s", exc)


def _record_access_request(chat_id: str, name: str) -> None:
    chat_id = str(chat_id or "").strip()
    if not chat_id:
        return
    import time as _t
    reqs = _load_access_requests()
    for r in reqs:
        if str(r.get("chat_id")) == chat_id:      # already pending — refresh name/time only
            if name:
                r["name"] = name
            r["ts"] = _t.time()
            _save_access_requests(reqs)
            return
    reqs.append({"chat_id": chat_id, "name": name, "ts": _t.time()})
    _save_access_requests(reqs[-50:])             # cap so a spammer can't grow this unbounded


def _remove_access_request(chat_id: str) -> None:
    chat_id = str(chat_id or "").strip()
    reqs = [r for r in _load_access_requests() if str(r.get("chat_id")) != chat_id]
    _save_access_requests(reqs)


# --- Conversation-thread index (the admin console's per-person chat viewer) --------

def _thread_summary(owner: str | None, label: str, hist: list, cfg) -> dict:
    """One row for the Chats console: who, how many messages, the last line, and how many
    watches they own (owner=None is the desktop admin, who sees every watch)."""
    last = hist[-1] if hist else None
    snippet, last_ts = "", None
    if isinstance(last, dict):
        snippet = str(last.get("content") or "").strip().replace("\n", " ")
        if len(snippet) > 90:
            snippet = snippet[:90] + "…"
        last_ts = last.get("ts")
    return {
        "owner": owner,
        "label": label,
        "messages": len(hist),
        "last_ts": last_ts,
        "last_snippet": snippet,
        "watches": len(cfg.watches) if owner is None else len(_watches_for_owner(cfg, owner)),
    }


def _list_conversation_threads(cfg) -> list[dict]:
    """Every chat thread for the admin console: the desktop/main thread (owner=None, sees all)
    plus one per Telegram person (owner = their chat_id). Read-only summaries, newest first."""
    names = _load_owner_names()
    threads = [_thread_summary(None, "Desktop · you (all watches)", _load_watcher_history(None), cfg)]
    try:
        for p in sorted(_WATCHER_HISTORY_PATH.parent.glob("watcher_history_*.json")):
            token = p.stem[len("watcher_history_"):]
            if not token:
                continue
            hist = _load_watcher_history(token)
            # An emptied thread leaves its file behind. Listing a person with no messages and no
            # watches just leaves a mystery entry in the console — nothing to read, nothing to do.
            if not hist and not _watches_for_owner(cfg, token):
                continue
            label = names.get(token) or f"Telegram · {token}"
            threads.append(_thread_summary(token, label, hist, cfg))
    except Exception as exc:
        log.warning("could not list chat threads: %s", exc)
    # Desktop first, then the busiest / most-recent Telegram threads.
    tel = sorted(threads[1:], key=lambda t: (t.get("last_ts") or 0), reverse=True)
    return threads[:1] + tel


def _desktop_dir():
    """The user's Desktop (OneDrive-redirected or local), else the data dir — wherever the bug
    report is easiest for the tester to find and send."""
    for env in ("OneDrive", "USERPROFILE"):
        base = os.environ.get(env, "")
        if base:
            d = Path(base) / "Desktop"
            if d.is_dir():
                return d
    return paths.data_dir()


def _watch_summary_no_secrets(cfg) -> str:
    """A repro-useful watch list with NO notification credentials (no Telegram token / email
    password) — just name, mode, urls, instruction."""
    lines = []
    for w in getattr(cfg, "watches", []):
        try:
            lines.append(json.dumps({
                "name": w.name, "mode": getattr(w, "mode", None),
                "autonomous": getattr(w, "autonomous", None),
                "urls": getattr(w, "urls", None),
                "instruction": getattr(w, "instruction", None),
                "judgment_prompt": getattr(w, "judgment_prompt", None),
            }, ensure_ascii=False))
        except Exception:
            pass
    return "\n".join(lines) or "(no watches)"


def _write_bug_report(title: str, description: str):
    """Write a zip (report.txt + newest logs) to the Desktop and return its path. Deliberately
    excludes config.yaml and any notification secrets."""
    import platform, zipfile
    from datetime import datetime
    from web_watcher.__version__ import __version__

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = _desktop_dir() / f"WebWatcher-bug-{ts}.zip"

    try:
        cfg = _load_cfg()
        watches = _watch_summary_no_secrets(cfg)
    except Exception:
        watches = "(could not read watches)"

    report = (
        f"WEB WATCHER BUG REPORT\n"
        f"======================\n"
        f"When       : {datetime.now().isoformat(timespec='seconds')}\n"
        f"App version: {__version__}\n"
        f"OS         : {platform.platform()}\n"
        f"Python     : {platform.python_version()}\n\n"
        f"TITLE: {title}\n\n"
        f"WHAT HAPPENED:\n{description or '(no description provided)'}\n\n"
        f"WATCHES (no credentials included):\n{watches}\n"
    )

    logs = []
    try:
        logs = sorted(paths.log_dir().glob("web_watcher_*.log"),
                      key=lambda p: p.stat().st_mtime, reverse=True)[:2]
    except Exception:
        pass

    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("report.txt", report)
        for lf in logs:
            try:
                z.write(lf, arcname=f"logs/{lf.name}")
            except Exception:
                pass
    return dest


def _action_broaden_terms(watch_name: str, manager, bg, terms_override=None) -> dict:
    """Change a watch's search terms and rebuild its URLs (one per term), then reload.
    Two modes:
      • terms_override given  → set EXACTLY those terms (user dictated them).
      • otherwise             → regenerate a FRESH, different set via the learning engine,
        bypassing the cache (force) and steering away from the terms already in use — so
        asking to 'change/broaden' actually produces something new instead of the cached set.
    Backs The Watcher's 'matched none of them' concern button and chat term-change requests."""
    from urllib.parse import urlparse, parse_qsl, unquote_plus
    from web_watcher.config import load, save, lock as _cfg_lock
    from web_watcher.search_terms import expand_search_terms, build_search_urls, _search_param

    cfg = load()
    w = next((x for x in cfg.watches if x.name.lower() == str(watch_name or "").lower()), None)
    if w is None:
        raise HTTPException(404, detail=f"Watch {watch_name!r} not found")

    base = next((u for u in (w.urls or [])
                 if _search_param(dict(parse_qsl(urlparse(u).query, keep_blank_values=True)))), None)
    if not base:
        return {"ok": False, "message": f"'{w.name}' doesn't use a keyword-search URL, so "
                "there are no search terms to change. I can adjust its judgment filter instead "
                "if you'd like — just ask."}

    # The terms currently baked into the watch's URLs (so a refresh can avoid repeating them).
    current: list[str] = []
    for u in (w.urls or []):
        q = dict(parse_qsl(urlparse(u).query, keep_blank_values=True))
        p = _search_param(q)
        if p and q.get(p):
            current.append(unquote_plus(q[p]))

    override = [t.strip() for t in (terms_override or []) if str(t).strip()]
    if override:
        terms, verb = override, "set"
    else:
        intent = (w.instruction or w.name or "").strip()
        terms = expand_search_terms(intent, cfg.models.effective_council_model,
                                    force=True, avoid=current)
        verb = "refreshed"
        if not terms:
            return {"ok": False, "message": "I couldn't come up with better terms right now — the "
                    "local model didn't return any. Worth trying again in a moment."}

    w.urls = build_search_urls(base, terms)
    save(cfg)
    bg.add_task(manager.reload_scheduler)
    return {
        "ok": True,
        "terms": terms,
        "message": f"{verb.capitalize()} '{w.name}' to search {len(terms)} ways: "
                   f"{', '.join(terms)}. It'll use these (each explored a few different ways — "
                   f"newest, price, etc.) on the next sweep.",
    }


def _watch_search_terms(w) -> list[str]:
    """Decode the actual search terms baked into a watch's URLs (the query= values),
    de-duplicated in order. So when the user asks 'what are our search terms?' the answer
    is plain text in the prompt — not something the model has to parse out of raw URLs
    (which it gets wrong: it read 'what are our terms?' as 'terms of service')."""
    from urllib.parse import urlparse, parse_qsl, unquote_plus
    from web_watcher.search_terms import _search_param
    terms: list[str] = []
    for u in (w.urls or []):
        try:
            q = dict(parse_qsl(urlparse(u).query, keep_blank_values=True))
            p = _search_param(q)
            if p and q.get(p):
                t = unquote_plus(q[p]).strip()
                if t and t not in terms:
                    terms.append(t)
        except Exception:
            continue
    return terms


def _cloud_spend_context(cfg) -> str:
    """A one-line spend/budget summary in the chat prompt so the user can just ASK the bot
    'how much have I spent this month / how much is left' and it answers from real numbers.
    When no cloud is in use, says so plainly (nothing is being spent)."""
    try:
        from web_watcher import llm
        b = llm.budget_state(cfg)
    except Exception:
        return "CLOUD SPEND: unavailable."
    any_cloud = False
    try:
        any_cloud = any((r.provider or "").lower() == "anthropic"
                        for r in (cfg.models.cloud.roles or {}).values())
    except Exception:
        pass
    if not any_cloud and b["spent"] <= 0:
        return ("CLOUD SPEND: The Watcher is running fully on local models right now — $0 of "
                "cloud (Claude) tokens are being spent. Tell the user that if they ask about cost.")
    cap = (f"a ${b['cap']:.2f}/month cap (about ${b['remaining']:.2f} left this month)"
           if b["cap"] > 0 else "no monthly cap set")
    return (f"CLOUD SPEND (Claude): about ${b['spent']:.2f} spent so far this month ({b['month']}), "
            f"with {cap}. This is an estimate from token usage, not a live account balance. "
            "Answer cost questions from these numbers.")


def _watches_for_owner(cfg, owner: str | None):
    """The watches an assistant turn may see/act on. owner=None (the desktop dashboard) → ALL.
    The ADMIN on Telegram (owner == the configured main chat_id) also sees ALL — they own the app,
    so "how many watches are there?" from the owner's phone must answer for everything, including
    unassigned ones. Any OTHER Telegram person sees ONLY their own watches, so a buddy can't see
    or touch anyone else's."""
    if not owner or _is_admin_owner(owner, cfg):
        return list(cfg.watches)
    return [w for w in cfg.watches if str(getattr(w, "owner", "") or "") == str(owner)]


def _build_watches_context(cfg, manager, owner: str | None = None) -> str:
    """Render the user's watches IN FULL, each with a HEALTH line (state + last-run
    result/error + matches found), for the assistant/Watcher system prompt. Shared by
    the main assistant and the oversight Watcher so both review against the same facts.

    owner scopes the list (see _watches_for_owner): the desktop shows all; a Telegram user
    sees only their own."""
    from web_watcher.storage import get_last_run, watch_stats
    try:
        job_map = {j["watch_name"]: j for j in manager.get_job_info()}
    except Exception:
        job_map = {}

    def _health_line(w) -> str:
        # Defensive: a DB/scheduler hiccup here must not 500 the whole assistant.
        try:
            # Plain words, because the model reads "DISABLED, stopped" and still writes "running".
            # A watch that's turned off is NOT watching, full stop — say exactly that.
            if not w.enabled:
                state = "OFF — not watching (turned off)"
            elif w.mode == "continuous":
                sweeping = job_map.get(w.name, {}).get("continuous_running")
                state = "ON — watching now" if sweeping else "ON — watching (idle between sweeps)"
            else:
                state = "ON — watching (scheduled)"
            last = get_last_run(w.name)
            if not last:
                health = "never run"
            elif last.get("error"):
                health = f"last run ERROR: {str(last['error'])[:90]}"
            else:
                health = f"last run: {str(last.get('summary') or '')[:90]}"
            st = watch_stats(w.id or w.name, w.name)
            found = st["matches"]
            counts = f"{found} matched of {st['observations']} seen"
            # The key diagnostic so the Watcher can proactively help: a watch that has looked
            # at many listings but matched none has search terms too narrow / rating too high.
            if st["observations"] >= 25 and found == 0:
                counts += " — DIAGNOSIS: seen plenty but matched nothing; likely the search is too narrow, a keyword filter is too strict, or min_rating is too high. Offer to broaden it."
            return f"      health: {state} | {health} | {counts}"
        except Exception:
            return "      health: (unavailable)"

    def _watch_summary(w) -> str:
        sched = (
            "continuous" if w.mode == "continuous"
            else (f"every {w.interval_minutes}m" if w.interval_minutes
                  else f"cron {w.cron_expression}")
        )
        lines = [
            f"  • {w.name}",
        ]
        # The decoded search terms first — this is what the user means by "what are we
        # searching for / what are our terms", so make it the most prominent, plain line.
        terms = _watch_search_terms(w)
        if terms:
            lines.append(f"      search terms ({len(terms)}): {', '.join(terms)}")
        lines += [
            f"      urls: {', '.join(w.urls)}",
            f"      mode: {w.mode} ({sched}) | autonomous: {w.autonomous} | "
            f"perception: {w.perception} | use_login_profile: {w.use_login_profile}",
            f"      instruction: {w.instruction[:200]}",
            _health_line(w),
        ]
        if w.judgment_prompt:
            lines.append(f"      judgment_prompt: {w.judgment_prompt[:200]}")
        return "\n".join(lines)

    scoped = _watches_for_owner(cfg, owner)

    # A pre-computed status line the model can read back verbatim, instead of inferring run-state
    # from per-watch detail (which it got wrong — calling turned-off watches "running"). Facts,
    # counted here, stated plainly.
    on = sum(1 for w in scoped if w.enabled)
    paused = False
    try:
        paused = manager.is_paused()
    except Exception:
        pass
    if scoped:
        if paused:
            status = (f"STATUS: the master switch is PAUSED, so NOTHING is being watched right now "
                      f"(you have {len(scoped)} watch(es); {on} would be on if resumed).")
        elif on == 0:
            status = (f"STATUS: {len(scoped)} watch(es) exist but ALL are turned OFF — nothing is "
                      f"being watched right now.")
        else:
            status = (f"STATUS: {on} of {len(scoped)} watch(es) are ON and being watched right now"
                      + (f"; {len(scoped) - on} are turned off." if len(scoped) > on else "."))
        status += " Read this back when asked what's running; do NOT call a running watch 'off' or an off one 'running'.\n\n"
    else:
        status = ""

    body = status + (
        ("EXISTING WATCHES: none assigned to you yet — ask the owner to assign you some in the "
         "Web Watcher app." if owner else "EXISTING WATCHES: none configured yet.") if not scoped else
        "EXISTING WATCHES (edit one via action:\"update\"; manage via watch_actions; "
        "use the health line to review/advise; don't duplicate). When the user asks what "
        "you're searching for / what the search terms are / which cars/items you're "
        "looking for, ANSWER DIRECTLY from the 'search terms' line below — just read them "
        "back in plain words. Do NOT run a listing_query for that (a listing_query looks up "
        "what's been FOUND, not what you're searching for), and do NOT propose an update "
        "unless they ask to change something:\n"
        + "\n".join(_watch_summary(w) for w in scoped)
    )
    return body + "\n\n" + _learned_sites_context()


def _learned_sites_context() -> str:
    """A short line telling the assistant which sites Web Watcher has already explored
    (and can read reliably) vs. needing a learn pass. The 3 built-ins always work."""
    try:
        from web_watcher.storage import list_site_profiles
        learned = [p.get("display_name") or p.get("domain") for p in list_site_profiles()]
    except Exception:
        learned = []
    builtins = "Craigslist, eBay, Facebook Marketplace (built-in)"
    extra = ("; learned: " + ", ".join(learned)) if learned else ""
    return (
        "KNOWN SITES Web Watcher can already read: " + builtins + extra + ".\n"
        "When the user asks to watch a site that is NOT in that known list:\n"
        "  1. Tell them you haven't explored that site yet and will need to look at how it's "
        "laid out before you can watch it well.\n"
        "  2. ASK the simple question that matters: does that site require you to be LOGGED IN "
        "to see listings? (If yes, set use_login_profile:true on the watch; if no, leave it "
        "false.) Ask any other quick thing you genuinely need, but keep it to a question or two.\n"
        "  3. Then propose the watch. Web Watcher does NOT explore on creation — the FIRST time "
        "the watch is STARTED it does a quick exploration round of any new site before it begins "
        "watching (with an on-screen heads-up). So tell the user: it'll explore the site the first "
        "time they start the watch. You never type a login/password yourself — a login-gated site "
        "uses the saved signed-in profile."
    )


# Keys that mark a dict as a BARE watch object the local model emitted without the
# {"message", "watch_suggestion"} envelope (a frequent qwen mistake).
_WATCH_SHAPE_KEYS = {"urls", "instruction", "judgment_prompt", "mode", "autonomous",
                     "perception", "interval_minutes", "cron_expression", "max_agent_steps",
                     "goal_kind", "target_size"}


def _normalize_turn(data: dict) -> dict:
    """
    Repair the common shape mistakes the local model makes so the chat NEVER shows raw JSON:
      • a bare watch object at the top level (no 'message', no 'watch_suggestion') is adopted
        as a watch_suggestion — this is the "garbled gook" the user saw (raw JSON dumped into
        the bubble because 'message' was missing);
      • watch_suggestion_2 / _3 / … (the model's way of proposing several watches) are
        collected alongside watch_suggestion so multi-watch updates aren't silently dropped;
      • a missing 'message' is always synthesized.
    Returns a dict that always has a 'message' and, when applicable, 'watch_suggestion' +
    'watch_suggestions' (the full list).
    """
    if not isinstance(data, dict):
        return {"message": str(data), "watch_suggestion": None}

    # Collect every watch_suggestion* the model produced, in stable order. An existing
    # watch_suggestions LIST is canonical (the extractor emits one for multi-item requests) —
    # rebuilding from the singular keys alone would silently drop every watch after the first.
    if isinstance(data.get("watch_suggestions"), list):
        suggestions = [w for w in data["watch_suggestions"] if isinstance(w, dict)]
    else:
        suggestions = []
        if isinstance(data.get("watch_suggestion"), dict):
            suggestions.append(data["watch_suggestion"])
        for k in sorted(k for k in data if k.startswith("watch_suggestion_")):
            if isinstance(data[k], dict):
                suggestions.append(data[k])

    # The model sometimes returns a BARE watch object (no envelope) → adopt it as the suggestion.
    bare_watch = ("message" not in data and not suggestions and
                  (data.get("name") or data.get("action") == "update"
                   or bool(_WATCH_SHAPE_KEYS & set(data.keys()))))
    if bare_watch:
        suggestions = [data]
        out = {}
    else:
        out = dict(data)

    if suggestions:
        # New watches proposed without any schedule get the default up front, so the card
        # shows a real schedule and Create can't fail validation. Updates are left alone —
        # they merge onto the existing watch, which already has its schedule.
        suggestions = [s if s.get("action") == "update" else _backfill_schedule(s)
                       for s in suggestions]
        # Clean each suggestion's URLs NOW (bogus hosts, craigslist junk queries / wrong
        # region) so the CARD shows what will actually be watched — not a URL that gets
        # silently rewritten at create time.
        for s in suggestions:
            if isinstance(s.get("urls"), list):
                hint = s.get("instruction") or s.get("listing_query") or s.get("name") or ""
                s["urls"], _ = _normalize_marketplace_urls(s["urls"], hint)
        out["watch_suggestion"]  = suggestions[0]
        out["watch_suggestions"] = suggestions

    if not out.get("message"):
        if suggestions:
            names = [s.get("name") for s in suggestions if s.get("name")]
            verb  = "update" if any(s.get("action") == "update" for s in suggestions) else "set up"
            out["message"] = (f"Here's how I'd {verb} {', '.join(names) or 'that watch'} — "
                              "review and apply below.")
        else:
            out["message"] = "Done."
    return out


# ---------------------------------------------------------------------------
# Two-phase assistant turn: converse in plain English (phase 1), then structure any
# concrete watch action as JSON (phase 2). Forcing JSON on EVERY turn made the local
# model worse at understanding — it split attention between reading the user and
# emitting a rigid envelope, so it lost track of which watch was meant and sometimes
# described an edit in prose while forgetting the watch_suggestion object (no card).
# Splitting the turn lets phase 1 focus purely on comprehension and phase 2 purely on
# extraction — the card only appears when there's a real, concrete change to make.
# ---------------------------------------------------------------------------

_CONVERSE_OVERRIDE = """\
════════════════════════════════════════
HOW TO REPLY (READ THIS LAST — IT OVERRIDES ANY EARLIER FORMAT INSTRUCTION)
════════════════════════════════════════
Reply to the user in natural, plain English. Do NOT output JSON, code, or field names —
just talk, like a knowledgeable helper. A SEPARATE step (not you) turns the conversation
into the actual watch config, so you never need to write it yourself.

⚠ EXAMPLES ARE NOT REQUESTS. Everything named in the instructions above (cars, kayaks, trucks,
kitchen appliances, any brand or model) is an ILLUSTRATION of format and technique only. NEVER
mention, propose, or create a watch for an item that appears only in an example. The ONLY things
you may act on are the items the USER actually typed in this conversation. If the user has not
named a thing to watch, do not invent one.

Your job here is only to UNDERSTAND and RESPOND:
- Track which watch the user is talking about. If they say "it", "that one", "the watch",
  or "change something else on it", they mean the watch CURRENTLY IN FOCUS (named below)
  unless they clearly name a different one. Do not switch watches on your own.
- While a NEW watch is being set up, follow-up details belong to IT. "I'd prefer it in black",
  "cap it at $500", "the one I just described" refer to the watch under discussion — never
  bolt them onto an older existing watch because it shares a word.
- "No" + a clarification is a CORRECTION, not a cancellation. "no, for the new fridge watch" /
  "no, I mean the search terms" means you attached the request to the wrong thing — re-read
  their earlier messages and redo it for the RIGHT thing. Never reply "OK, never mind then" to
  a correction, and never make the user restate details they already gave.
- A QUESTION about a watch ("what are its search terms?", "what is it looking for?") gets an
  ANSWER from the config shown below — not a proposal to change anything.
- New vs. existing: if the user asks to watch something NEW or DIFFERENT from the watches
  they already have (e.g. "also watch for canoes", "set up a watch for…", "can you watch X"),
  treat it as a BRAND-NEW watch and say you'll set one up. Having other watches does NOT mean
  they want to edit an existing one — don't fold a new request into an existing watch.
- When the user clearly asks to set up or change a watch, COMMIT — say you're doing it, as a
  STATEMENT, not a question. Name back THE ITEM THE USER ACTUALLY ASKED FOR, e.g. "Sure —
  setting up that watch on Craigslist now." The build step handles the rest. NEVER ask the user
  to confirm an action they already asked for — "Do you want me to set it up?" is wrong when
  they just said "set up a … watch".
- A BARE "yes" / "no" / "ok" ANSWERS THE QUESTION YOU JUST ASKED. Re-read your own previous
  message and do THAT. If you just asked "want to see the matches?", a "yes" means SHOW THE
  MATCHES — it does NOT mean create or re-confirm a watch. Never answer a bare yes with "I'll set
  up that watch" unless the thing you just offered was setting up that watch.
- SHOW, DON'T OFFER. When the user asks to SEE something they already have ("show me the
  matches", "list the top 15", "what have you found"), just show it — don't reply with "would you
  like me to show them?". They already asked.
- BE HONEST ABOUT WHAT YOU CAN DO. Your tools are exactly: set up or change a watch, and
  start / stop / pause / enable / disable / delete watches. That's it. NEVER claim to do — or
  say you've already done — anything outside that set: you cannot "start the services", "clear
  everything", run commands, or change the computer. If the user asks for something you can't do,
  say so plainly and offer the closest thing you actually can (e.g. "I can't start the app's
  services, but I can start your watches"). And never report an action as already finished — a
  separate step applies it; say what you WILL do, not that it's done.
- Use the details the user ALREADY gave (what to watch, price, which sites). Do NOT ask them
  to repeat something they've already said. For an optional detail they did NOT give (a price
  cap, extra sites), just pick a sensible default and mention it in passing ("no price limit
  for now — tell me if you want one"); never hold up the watch to interrogate them for it.
- Only ask a clarifying question when you genuinely can't tell WHAT they want to watch. Ask at
  most ONE, and only if it's truly essential — otherwise proceed with sensible defaults.
- MARKETPLACE vs SPECIFIC PAGE: if the user is shopping for an ITEM on a marketplace you know
  (Craigslist, eBay, Facebook Marketplace, OfferUp), you can build the search yourself — go ahead.
  But if they want to watch a SPECIFIC website, a local page, a news topic, a schedule, or a
  status page (e.g. "the Anacortes clam digger", "the Seattle Times for ferry news", "when the
  campsite page opens"), you do NOT know the exact web address — ASK them for the link/URL of the
  page to watch. NEVER invent a URL or quietly turn it into a Craigslist search. It's better to
  ask "What's the web address of the page you want me to check?" than to guess wrong.
- Keep it short and conversational."""

_EXTRACT_SYSTEM = """\
You convert a conversation into ONE concrete watch action, but only if one is clearly
warranted right now. You are given the conversation, the assistant's latest reply to the
user, the user's existing watches (full current config), and which watch is in focus.

⚠ EXAMPLES ARE NOT REQUESTS. Any item named in these instructions is an illustration only.
Build a watch ONLY for what the USER actually asked for in the conversation. If the user never
named a thing to watch, output intent "none" — never invent an item.

STEP 1 — the most important decision: is this a CREATE or an UPDATE?

- "create" — the user wants a NEW, SEPARATE watch. Choose this when ANY of these hold:
    • they say things like "new watch", "another watch", "also watch (for)…", "create /
      add / set up a watch", "start watching…", "make one for…", "can you watch…";
    • they describe watching a thing that NONE of the existing watches already covers;
    • they are starting a fresh topic rather than pointing at an existing watch.
  Give it a NEW name (different from every existing watch) and a full config.
  TWO OR MORE DIFFERENT THINGS = TWO OR MORE WATCHES. When one message asks to watch
  DISTINCT items ("watch for a <thing A> and a <thing B>"), return a "watches" LIST —
  one complete watch object per item, each with its own name, instruction, and urls.
  Never merge different items into one watch and never quietly drop all but the first.
  (Multiple SITES for the SAME item is still ONE watch with several urls.)
  IMPORTANT: the user ALREADY HAVING other watches does NOT mean they want to edit one.
  Most "watch X for me" requests are BRAND-NEW watches. If you are torn between create and
  update, choose CREATE unless the user clearly points at one specific existing watch.
  BUILD THE CONFIG FROM WHAT THE USER ALREADY SAID anywhere in the conversation — the thing to
  watch (instruction/name), the price cap, and the site(s) → urls. Do NOT wait for the user to
  restate details they already gave. For anything they did NOT specify, use sensible defaults
  (no price cap; a reasonable marketplace for that item) rather than emitting "none".
  URL RULE — a CREATE MUST have at least one real http(s) URL, and never an empty urls list:
    • Item shopping on a marketplace you know (Craigslist/eBay/Facebook/OfferUp) → build the
      search URL(s) yourself from the item + location.
    • EVERY SITE THE USER NAMED gets urls. "watch craigslist and offerup for <item>" → the urls
      list has craigslist search URLs AND offerup search URLs in the SAME watch. Never quietly
      drop a site down to just the first one; if the user said "everywhere" / "all the usual
      places", cover Craigslist, OfferUp, and eBay.
    • Each URL is a REAL SEARCH for the item: the query is the item's name or a close synonym.
      Do NOT emit a bare adjective, price, or size as its own search (no "?query=black",
      "?query=under+800"), and do NOT invent query parameters a site doesn't have. The ONLY
      price/location params that exist, per site: craigslist max_price/min_price/postal/
      search_distance/purveyor; OfferUp price_max/price_min/radius (NO location param, NO
      city path — offerup.com/search only; it shows the user's own area automatically);
      eBay _udhi/_udlo/_stpos/_sadis; Facebook maxPrice/minPrice (+ the city PATH segment).
      Anywhere else, price caps and locations belong in the instruction text, not the URL.
      When the user named a town, express it as its 5-digit zip (craigslist postal= and
      eBay _stpos= — you know most towns' zips) and add the 50-mile radius param; never
      as words in the query text.
    • Watching a SPECIFIC website/page/topic/schedule/news (not a marketplace item search) where
      the user has NOT given a URL → you do NOT know the address; return intent "none" (the
      assistant is asking them for the link). Do NOT fabricate a URL and do NOT turn it into a
      Craigslist search. Example: "watch the Anacortes clam digger" with no link → "none".

- "update" — the user wants to CHANGE an EXISTING watch. Choose this ONLY when the user
  points at a specific existing watch: by typing (part of) its name, or by "it" / "that one" /
  "the watch" / "change something else on it" (which mean the watch CURRENTLY IN FOCUS).
  Start from that watch's current config (below) and change ONLY what the user asked; keep
  the same name.
  A QUESTION IS NOT AN UPDATE. "what are its search terms?", "what is that watch looking
  for?", "how's it doing?" are the user ASKING — answer-only turns, intent "none" (or "lookup"
  for found listings). Emit an update ONLY for an explicit request to CHANGE something.
  THE REQUESTED CHANGE MUST BE IN THE CONFIG YOU EMIT. "also look for <another thing>" on a
  watch means the new config CONTAINS searches/instruction text for <another thing> IN
  ADDITION to everything already there. Before answering, check: does the emitted config
  actually contain what the user just asked for? If it is identical to the current config,
  you have not made the change.

- "actions" — the user asked to start/stop/enable/disable/delete watch(es).
- "lookup"  — the user asked what's been found / to see listings.
- "none"    — the assistant asked the user a question, OR the user is still
              discussing/clarifying, OR there is no concrete, unambiguous action yet.

STEP 2 — only if UPDATE, pick WHICH watch: from CURRENTLY IN FOCUS, or a name the user typed.
Do NOT pick a watch just because a word in the request (a site like "offer up"/"craigslist"/
"ebay", a search term, or a price) also appears in that watch's name or config. Those words
describe WHAT to change, not WHICH watch — and they NEVER, on their own, turn a new-watch
request into an update. Example: focus is the sports-car watch and the user says "also look on
offer up" → ADD OfferUp to the sports-car watch (update). But "watch craigslist for a canoe"
when no canoe watch exists → CREATE, even though a "craigslist" word appears elsewhere.

When unsure between create/update/none, prefer CREATE for a clearly-new thing, else "none".
Never invent a change the user didn't ask for.

RESTOCK / BACK-IN-STOCK WATCHES (a goal watch, not a listings search):
When the user wants to know when a SPECIFIC product (usually a single product-page URL) comes
BACK IN STOCK / becomes available again — often in a particular size or variant — create a
watch with "goal_kind": "restock". Put the product URL in "urls" (exactly as given, keep any
?variant=), and put the size/variant in "target_size" (e.g. "34W x 30L", "Large", "US 10").
Do NOT turn it into a keyword search and do NOT add other sites. Example: user says "watch
https://shop.com/products/pants?variant=123 and tell me when 34W x 30L is back in stock" →
{"action":"create","name":"<short name>","urls":["https://shop.com/products/pants?variant=123"],
"instruction":"Alert when 34W x 30L is back in stock","goal_kind":"restock",
"target_size":"34W x 30L","interval_minutes":30}. (Interval watches only — restock checks run
on a schedule, not a continuous browser.)

Output STRICT JSON, no prose, no markdown:
{
  "intent": "update" | "create" | "actions" | "lookup" | "none",
  "watches": [ one full watch config PER DISTINCT ITEM the user asked to watch — usually
               exactly ONE; two different items → a list of TWO complete configs. Each
               INCLUDES "name" (for CREATE a NEW name not equal to any existing watch; for
               UPDATE the EXACT existing name). Optional per-watch fields when the user
               asked for them: "min_rating" (1-5 alert threshold — set it when they talk
               about how many / how good the alerts should be), "keywords" (must-include
               words), "antikeywords" (exclude words). On an UPDATE, INCLUDE the field you
               are changing even if it's the only change. COUNT the items before you
               answer: if the user named two things, a one-element list is WRONG. ] | null,
  "watch_actions": [ {"action": "start|stop|enable|disable|delete", "name": "..."} ] | null,
  "listing_query": { ... } | null   — ONLY for intent "lookup" (showing found listings).
                                      NEVER put a watch config here.
}
For intent "create" or "update" the config goes in "watches" — a create/update with
"watches": null is INVALID. For intent "none", set all three to null."""


# Phrases that mean the assistant is COMMITTING to set up / change a watch (a statement of
# action), as opposed to merely asking the user a question. Used to let a committed 'create'
# card through even when the reply also ends with an optional question.
_COMMIT_RE = re.compile(
    r"\b(sett?ing up|set up|i['’]?ll set|i['’]?ll creat|creat(?:e|ing)|i['’]?ll add|adding a watch|"
    r"i['’]?ll (?:watch|start|widen|update|expand)|start(?:ing)? (?:a )?watch|"
    r"watch(?:ing)? (?:for|the)|here['’]?s (?:your|the) watch|i['’]?ve set)\b",
    re.IGNORECASE)


# How many trailing chat messages the model + focus tracker actually see. The UI keeps the full
# transcript on screen forever, but only this recent window is sent to the model each turn — enough
# for short-term back-references ("it", "that one") without dragging in stale, unrelated history.
# ~7 exchanges (user+assistant pairs).
_CHAT_CONTEXT_MESSAGES = 14
# The 14-message cap exists because a local 14b model has a small context window and both
# loses focus and eventually overflows when fed more. A cloud model (Claude, 200k context)
# has neither problem, so when the chat role is routed there we remember far more of the
# conversation. Kept bounded (not "everything") so cost + latency stay predictable.
_CHAT_CONTEXT_MESSAGES_CLOUD = 60


def _chat_context_limit(cfg) -> int:
    """How many recent messages to feed the assistant — more when chat runs on Claude."""
    try:
        from web_watcher import llm
        if llm.resolve_route(cfg, "chat")[0] == "anthropic":
            return _CHAT_CONTEXT_MESSAGES_CLOUD
    except Exception:
        pass
    return _CHAT_CONTEXT_MESSAGES


def _reply_commits_to_action(message: str) -> bool:
    """True if the assistant's reply states it IS setting up / changing a watch (not just asking).
    Lets a committed create proceed even if the message also contains an optional question."""
    return bool(_COMMIT_RE.search(message or ""))


def _prose(content) -> str:
    """Reduce a stored/model message to plain prose. Older turns (and the occasional model
    slip) are raw JSON envelopes; replaying those as conversation context confused the model
    about which watch was meant. Extract the human-readable line and drop the JSON."""
    if not isinstance(content, str):
        return str(content)
    s = content.strip()
    if not s.startswith("{"):
        return content
    try:
        o = json.loads(s)
    except Exception:
        return content
    if isinstance(o, dict):
        if isinstance(o.get("message"), str) and o["message"].strip():
            return o["message"]
        if o.get("name") or o.get("urls") or o.get("instruction"):
            verb = "updated" if o.get("action") == "update" else "set up"
            return f"(I {verb} {o.get('name') or 'a watch'}.)"
    return content


_FOCUS_STOP = {"the", "a", "an", "watch", "watches", "for", "on", "in", "of", "and",
               "under", "up", "to", "my", "this", "that", "it", "vehicles", "vehicle"}


def _watch_focus_tokens(name: str) -> list[str]:
    """Distinctive tokens of a watch name for fuzzy focus matching. Drops the parenthetical
    SITE tag (e.g. '(OfferUp)') so a user saying 'also look on offer up' does NOT accidentally
    resolve to a watch merely NAMED after OfferUp — the site is a target to add, not a selector."""
    import re
    base = re.sub(r"\(.*?\)", "", name or "")
    toks = re.findall(r"[a-z0-9]+", base.lower())
    return [t for t in toks if t not in _FOCUS_STOP and len(t) > 2]


# A create being DISCUSSED — "create a watch for X…", "I'll set up a watch…" — that hasn't been
# applied yet. While one is in play, follow-ups belong to IT, not to whichever existing watch
# shares a word with something said earlier.
PENDING_CREATE = "__pending_create__"
_CREATEISH_RE = re.compile(
    r"(\b(create|make|add|set ?up|start)\b.{0,40}\bwatch\b)|(\bwatch\b.{0,20}\bfor\b)|"
    r"\bnew watch\b|\bi['’]?ll set up\b|\bsetting up a\b",
    re.IGNORECASE | re.DOTALL)


def _focused_watch_name(messages: list, cfg) -> str | None:
    """What the conversation is currently about: the EXISTING watch most recently referenced —
    so 'it' / 'that watch' / 'change something else on it' resolves across turns — or
    PENDING_CREATE when the most recent topic is a NEW watch still being set up.

    Scanned newest-first; within each message, an existing-watch token match wins over the
    create-ish check. That ordering makes an applied create self-heal: once the watch exists,
    the same words that made the conversation 'pending' now match the real watch by name.
    (Real failure this prevents: mid-fridge-creation, 'i would prefer it to be black' walked
    past the fridge talk and hit 'Manual Sports Cars' from six messages earlier.)"""
    watches = [(w.name, _watch_focus_tokens(w.name)) for w in getattr(cfg, "watches", [])]
    for m in reversed(messages or []):
        c = m.get("content") if isinstance(m, dict) else None
        if not isinstance(c, str):
            continue
        low_toks = set(re.findall(r"[a-z0-9]+", c.lower()))
        best, best_hits = None, 0
        for name, toks in watches:
            if not toks:
                continue
            hits = sum(1 for t in toks
                       if t in low_toks
                       or (t.endswith("s") and t[:-1] in low_toks)
                       or (t + "s") in low_toks)
            if hits >= min(2, len(toks)) and hits > best_hits:
                best, best_hits = name, hits
        if best:
            return best
        if _CREATEISH_RE.search(c):
            return PENDING_CREATE
    return None


# An UPDATE (edit an existing watch) is only real when the user's OWN latest message asks for a
# CHANGE. The small extract model otherwise proposes 'update' actions for watches the user never
# mentioned — the reported "2 edit cards I wasn't asking for" bug. These words signal a real change
# request; a greeting / statement / question about a watch has none.
_CHANGE_SIGNAL_RE = re.compile(
    r"\b(also|add|adding|includ\w*|chang\w*|edit\w*|updat\w*|modif\w*|adjust\w*|tweak\w*|"
    r"set\b|make it|rename|remov\w*|delete|drop|exclud\w*|switch\w*|instead|widen\w*|"
    r"expand\w*|broaden\w*|narrow\w*|raise|lower|increase|decrease|bump|cap\b|limit\b|"
    r"only\b|no longer|as well|max\b|min\b|price|budget|under|over|less than|more than|"
    r"radius|distance|sites?|keyword|antikeyword|"
    # run-mode changes: "make it always run", "run continuously", "keep it running", "24/7".
    r"always run|run always|run all the time|all the time|run continuous\w*|continuous\w*|"
    r"keep (it )?running|24/?7|nonstop|constantly|every \d+ ?min\w*|hourly|daily)\b", re.IGNORECASE)

# "both/all/every watch(es)" — an explicit request to change more than one at once (rare, but real).
_ALL_WATCHES_RE = re.compile(r"\b(both|all|every|each)\b[\w\s]{0,24}\bwatch", re.IGNORECASE)

# A start/stop/enable/disable/DELETE action card only appears when the user's own message uses
# that action's verb. Without this, the eager extractor can surface an unasked-for action card —
# a spurious "Delete <watch>?" is the same bug class as the spurious edit cards, and scarier.
_ACTION_VERB_RE = {
    "delete":  re.compile(r"\b(delete|remove|get rid of|trash|discard|drop)\b", re.I),
    "stop":    re.compile(r"\b(stop|pause|halt|turn off|shut (?:it |them )?off)\b", re.I),
    "start":   re.compile(r"\b(start|run|resume|begin|kick off|fire up|turn on)\b", re.I),
    "enable":  re.compile(r"\b(enable|re-?enable|activate|turn on|switch on)\b", re.I),
    "disable": re.compile(r"\b(disable|deactivate|turn off|switch off)\b", re.I),
    "reset":   re.compile(r"\b(reset|re-?set|remake|re-?make|recreate|re-?create|rebuild|"
                          r"re-?build|redo|re-?do|start (?:it )?(?:over|fresh|again)|"
                          r"clear (?:it|out)|wipe (?:it|and)|fresh start)\b", re.I),
}

# Reset intent, on its own — used for a deterministic short-circuit so "remake/reset the X watch"
# actually DOES it instead of the 14b chatting about it. NB "restart" is NOT here: a restart is
# just start; a reset also WIPES the finds and re-judges, so we only reset on the clearer verbs.
_RESET_RE = re.compile(r"\b(reset|re-?set|remake|re-?make|recreate|re-?create|rebuild|re-?build|"
                       r"redo|re-?do|start (?:it )?(?:over|fresh|afresh)|start over|"
                       r"wipe (?:it|and)|clear (?:it|out)|fresh start)\b", re.I)


def _latest_user_text(messages: list) -> str:
    """The most recent USER message's text (the turn that triggered this action)."""
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"]
    return ""


def _watch_referenced_in(text: str, name: str) -> bool:
    """True if `text` actually names this watch — enough distinctive tokens overlap (all of them
    for a one-word name, ≥2 otherwise). Reuses _watch_focus_tokens so the site suffix is ignored."""
    toks = _watch_focus_tokens(name)
    if not toks:
        return False
    low = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    hits = sum(1 for t in toks
               if t in low or (t.endswith("s") and t[:-1] in low) or (t + "s") in low)
    return hits >= min(2, len(toks))


# ── Start/stop intent (the master switch vs a person's own watches) ───────────────
# Two levels the user talks to: the WHOLE Watcher (admin-only master switch) and their OWN
# watches. We classify this deterministically rather than trust the 14b — start/stop of everything
# is too consequential to leave to a model that emits blank thoughts. See _classify_lifecycle.
_STOP_VERB_RE  = re.compile(r"\b(stop|pause|halt|shut\s*(it|them|down|off)|turn\s*(it|them)?\s*off|"
                            r"shut\s*down|standby|stand\s*down)\b", re.I)
_START_VERB_RE = re.compile(r"\b(start|resume|unpause|un-?pause|turn\s*(it|them)?\s*(on|back\s*on)|"
                            r"fire\s*up|kick\s*off|get\s*going|begin\s*watching|start\s*watching)\b", re.I)
# "the whole thing" — the global program, not a single watch.
_WHOLE_PROGRAM_RE = re.compile(
    r"\b(everything|all\s+watching|all\s+the\s+watches|the\s+(whole|entire)\s+(watcher|thing|program|"
    r"app)|the\s+watcher|it\s+all|all\s+of\s+it)\b", re.I)
# "all my watches" — every watch the speaker owns (scoped, not global).
_ALL_MINE_RE = re.compile(r"\b(all|both|every|each)\b[\w\s]{0,20}\b(my\s+|our\s+)?watch(es)?\b", re.I)


def _classify_lifecycle(text: str):
    """Classify a start/stop request into (verb, scope). verb is 'pause' or 'resume'; scope is
    'program' (the whole Watcher), 'all_mine' (all the speaker's watches), or 'bare' (a lone
    'stop'/'start' with no object). Returns (None, None) when the turn isn't a start/stop at all,
    or (None,'ambiguous') when it mixes both verbs. A NAMED watch ('stop the truck watch') is left
    to the normal action extractor — this only fires when no specific watch is named."""
    t = text or ""
    stop, start = bool(_STOP_VERB_RE.search(t)), bool(_START_VERB_RE.search(t))
    if not (stop or start):
        return (None, None)
    if stop and start:
        return (None, "ambiguous")             # "stop and restart" — let the model/user clarify
    verb = "pause" if stop else "resume"
    if _WHOLE_PROGRAM_RE.search(t):
        return (verb, "program")
    if _ALL_MINE_RE.search(t):
        return (verb, "all_mine")
    return (verb, "bare")


def _named_lifecycle_action(text: str, cfg, owner):
    """A plain "start/stop the <name> watch" → the concrete {action, name}, decided in code.

    The 14b is unreliable at turning this into a structured action: it answered "start up the
    macgregor watch" with an EDIT card (dropped as ungrounded), so nothing ran — and then
    _classify_lifecycle, which deliberately ignores named watches, treated the turn as a BARE
    start and asked the admin "the whole Watcher, or just yours?" instead of just starting it.
    So we detect it ourselves, mirroring the deterministic reset short-circuit.

    Fires only when the message is a COMMAND (not a question), carries a start/stop verb, is NOT
    program- or all-mine-scoped (those have their own handlers), and names EXACTLY ONE watch the
    speaker owns. Focus is intentionally not used — a bare "stop" with a focused watch stays with
    the existing bare handler. Returns {"action", "name"} or None."""
    t = (text or "").strip()
    if not t or t.endswith("?"):
        return None                                   # a question is not a command
    if _WHOLE_PROGRAM_RE.search(t) or _ALL_MINE_RE.search(t):
        return None                                   # whole-program / all-mine handled elsewhere
    stop, start = bool(_STOP_VERB_RE.search(t)), bool(_START_VERB_RE.search(t))
    if stop == start:                                 # neither, or both ("stop and restart") — skip
        return None
    # "stop and restart" / "restart it" reads a restart, not a plain stop — _START_VERB_RE misses
    # the "restart"/"again" cue, so guard it here rather than half-executing (a lone stop).
    if stop and re.search(r"\b(re-?start|start\s+(it\s+)?(again|over)|again)\b", t, re.I):
        return None
    named = _watch_named_in(t, cfg, owner)            # the watch called out in the user's words
    real = _resolve_watch_name(named or "", cfg)
    if real and _is_owned(real, cfg, owner):
        return {"action": "stop" if stop else "start", "name": real}
    return None


def _is_admin_owner(owner, cfg) -> bool:
    """The admin is the desktop dashboard (owner=None) or the main configured Telegram chat. Only
    the admin may flip the global master switch; a buddy can only touch their own watches."""
    if owner is None:
        return True
    try:
        return str(owner) == str(cfg.notifications.telegram.chat_id or "")
    except Exception:
        return False


# Words that mark a turn as worth escalating to cloud (create/search intent). Change-intent is
# covered separately by _CHANGE_SIGNAL_RE; everyday chat (greetings, status, yes/no) matches
# neither and stays local — fast and free.
_HARD_CHAT_RE = re.compile(
    r"\b(watch|monitor|alert|track|find|looking for|look for|search|set up|setup|create|"
    r"under|over|\$|price|budget|miles?|radius|distance|craigslist|facebook|offerup|ebay|"
    r"marketplace|always run|continuous\w*)\b", re.IGNORECASE)


_BARE_CONFIRM_RE = re.compile(
    r"^\s*(y|ye|yes|yep|yeah|yup|ok|okay|sure|sounds good|please|do it|go ahead|"
    r"n|no|nope|nah|never ?mind)\b[\s.!,]*$", re.I)


def _is_bare_confirmation(text: str) -> bool:
    """True for a message that is ONLY a yes/no with no content of its own. Such a turn answers
    the assistant's previous question — it can't be a request to watch something new."""
    return bool(_BARE_CONFIRM_RE.match(text or ""))


def _is_hard_chat_turn(messages: list, focus: str | None) -> bool:
    """Should this turn be allowed to use cloud? Everyday chat stays local; the hard turns —
    creating or changing a watch, or a long/complex ask — escalate when cloud chat is enabled.
    Deliberately cheap and text-only (no extra model call). A no-op unless cloud chat is on."""
    if focus == PENDING_CREATE:
        return True                                   # mid-setup of a new watch — needs care
    text = (_latest_user_text(messages) or "").strip()
    if len(text) > 80:
        return True                                   # a long / multi-part message
    return bool(_CHANGE_SIGNAL_RE.search(text) or _HARD_CHAT_RE.search(text))


def _ground_update_suggestions(suggestions: list, messages: list, focus: str | None) -> list:
    """Deterministic guard against SPURIOUS edit cards. The small extract model sometimes proposes
    'update' actions for existing watches the user never mentioned (the reported "2 cards to edit
    even though I wasn't talking about them"). An update survives ONLY when the user's own latest
    message (a) asks for a change AND (b) points at that watch — by naming it, by 'it'/'that one'
    (the focus watch), or by 'both/all watches'. Creates and everything else pass through untouched;
    this is a last-line net that a prompt alone can't guarantee on a 14b model."""
    latest = _latest_user_text(messages)
    asked_change = bool(_CHANGE_SIGNAL_RE.search(latest))
    all_watches = bool(_ALL_WATCHES_RE.search(latest))
    kept = []
    for s in suggestions:
        if not isinstance(s, dict) or (s.get("action") or "create") != "update":
            kept.append(s)
            continue
        name = s.get("name", "")
        grounded = all_watches or (bool(name) and name == focus) or _watch_referenced_in(latest, name)
        if asked_change and grounded:
            kept.append(s)
        else:
            log.info("chat: dropped ungrounded edit card for %r (asked_change=%s grounded=%s) — "
                     "the user didn't ask to edit it", name, asked_change, grounded)
    return kept


def _resolve_watch_name(name: str, cfg) -> str | None:
    """Map a possibly-imperfect watch name (as the model wrote it) to the EXACT stored name.
    The local model often drops the site suffix — 'Diesel Vehicles' for 'Diesel Vehicles
    (OfferUp)' — which made an update 404. Match exact → case-insensitive → distinctive-token
    overlap, so an assistant edit lands on the right watch instead of failing."""
    stored = [w.name for w in getattr(cfg, "watches", [])]
    if not name:
        return None
    if name in stored:
        return name
    low = name.strip().lower()
    for n in stored:
        if n.strip().lower() == low:
            return n
    want = set(_watch_focus_tokens(name))
    if want:
        best, best_ov = None, 0
        for n in stored:
            toks = set(_watch_focus_tokens(n))
            ov = len(want & toks)
            if toks and ov >= max(1, (len(toks) + 1) // 2) and ov > best_ov:
                best, best_ov = n, ov
        if best:
            return best
    return None


def _watches_config_context(cfg) -> str:
    """Full current config of each watch as JSON, so an update starts from real values."""
    lines = []
    for w in getattr(cfg, "watches", []):
        try:
            cfg_json = json.dumps(w.model_dump(exclude_none=True), ensure_ascii=False)
        except Exception:
            cfg_json = json.dumps({"name": getattr(w, "name", "?")})
        lines.append(f"- {cfg_json}")
    body = "\n".join(lines) or "  (no watches yet)"
    return ("EXISTING WATCHES (full current config — for an update, start from the matching "
            "one and change ONLY what the user asked):\n" + body)


def _converse_focus_line(focus: str | None, conv: list) -> str:
    """A short 'what we're talking about RIGHT NOW' line for the CONVERSE phase. Without it,
    only extraction knew the focus — the natural-reply model had to re-derive the topic from
    raw history every turn and regularly dropped it mid-setup (asked 'what item are you
    looking for?' three turns after the user said 'manual car under 8000')."""
    if focus == PENDING_CREATE:
        frag = ""
        for m in reversed(conv or []):
            if m.get("role") == "user" and _CREATEISH_RE.search(str(m.get("content", ""))):
                frag = str(m.get("content", ""))[:200]
                break
        return ("\n\nRIGHT NOW: the user is in the middle of setting up a NEW watch"
                + (f" (they asked: \"{frag}\")" if frag else "")
                + ". Every follow-up answer — a price, extra sites, a color, 'under 8000' — is "
                "a DETAIL OF THAT WATCH. Do NOT ask again what they're looking for; combine "
                "everything they've said so far and commit to setting it up.")
    if focus:
        return (f"\n\nRIGHT NOW: the conversation is about the existing watch \"{focus}\" — "
                "'it' / 'that one' refer to it.")
    return ""


def _chat_reply_natural(system: str, messages: list, model: str, force_local: bool = False):
    """Phase 1 — a natural-language reply (NO forced JSON). Returns (text, eval, prompt, dur).
    force_local keeps easy turns on the local model even when cloud chat is enabled."""
    # Routed through the provider seam. Everyday chat stays local (fast + free on this machine);
    # only the hard turns escalate to cloud when it's on (force_local=False). Falls back to local
    # automatically on any cloud error. See llm.py and _is_hard_chat_turn.
    from web_watcher import llm
    cfg = _load_cfg()
    msgs = [{"role": "system", "content": system + "\n\n" + _CONVERSE_OVERRIDE}, *messages]

    # AUTO (the default): run local, check the answer, and pay for Claude only if the local one
    # actually failed. force_local is honoured as a hard "never escalate this turn".
    if getattr(getattr(cfg.models, "cloud", None), "auto", True) and not force_local:
        got = llm.chat_smart(msgs, role="chat", local_model=model, cfg=cfg,
                             timeout=90.0, cache_system=True)
        if got["escalated"]:
            log.info("chat: escalated to %s — %s", got["used"], got["why"])
        return (_prose(got["text"]), 0, 0, 0)

    text = llm.chat(msgs, role="chat", local_model=model, cfg=cfg, timeout=90.0,
                    cache_system=True, force_local=force_local)
    # Token counts are an Ollama-only diagnostic; the cloud path simply reports none.
    return (_prose(text), 0, 0, 0)


def _extract_watch_action(messages: list, reply: str, cfg, model: str,
                          focus: str | None, force_local: bool = False) -> dict:
    """Phase 2 — decide if the conversation warrants a concrete watch action and, if so,
    return it as {watch_suggestion?, watch_actions?, listing_query?}. Returns {} for 'none'.
    A dedicated call so extraction can't be crowded out by conversation. Never raises."""
    if focus == PENDING_CREATE:
        focus_line = ("\n\nCURRENTLY IN FOCUS: a NEW watch the user is in the middle of setting "
                      "up (it does NOT exist yet — it is not in the list above). Follow-up "
                      "details ('make it black', 'the one I just described', 'add a price cap') "
                      "refer to THAT new watch: fold them into the CREATE. Do NOT attach them "
                      "to any existing watch, no matter which words they share.")
    elif focus:
        focus_line = (f"\n\nCURRENTLY IN FOCUS: \"{focus}\" — use this ONLY when the user refers "
                      "back to it with 'it' / 'that one' / 'the watch' / 'change something else on "
                      "it'. If the user is asking to watch a NEW or DIFFERENT thing, that is a "
                      "CREATE — do NOT force it onto this focus watch.")
    else:
        focus_line = "\n\nCURRENTLY IN FOCUS: (none yet)"
    system = (_EXTRACT_SYSTEM + "\n\n" + _watches_config_context(cfg) + focus_line
              + "\n\nThe assistant just told the user:\n\"" + (reply or "") + "\"")
    try:
        from web_watcher import llm
        msgs = [{"role": "system", "content": system}, *messages]
        # Extraction is the app's most consequential call — a wrong one is a watch that quietly
        # finds nothing for a week — and it's rare (only when a watch is made or changed). So on a
        # HARD turn it goes through the local-first cascade under its OWN 'extract' role: local
        # first, Claude only if the local model can't even produce valid JSON. Easy turns
        # (force_local) and auto-off stay strictly local, same as the chat reply.
        auto = getattr(getattr(getattr(cfg, "models", None), "cloud", None), "auto", True)
        if force_local or not auto:
            content = llm.chat(msgs, role="extract", local_model=model, cfg=cfg,
                               format_json=True, timeout=60.0, force_local=True)
        else:
            # Extraction is the app's most consequential call, and it had the WEAKEST check —
            # the default validator only asks "is this valid JSON?", so a local model that
            # returned an EMPTY {} on a turn where the assistant just PROMISED to set up a watch
            # sailed through and never escalated. The result: the assistant says "setting up your
            # Miata watch" and no watch is created. This validator ties the extraction to the
            # reply: if the reply committed to a create/change, the extraction MUST carry an
            # action; otherwise (a no-op turn) an empty result is correct and stays local.
            def _extract_usable(text: str) -> bool:
                try:
                    d = json.loads(llm._extract_json_text(text))
                except Exception:
                    return False
                if not isinstance(d, dict):
                    return False
                if _reply_commits_to_action(reply or ""):
                    return bool(d.get("watch_suggestion") or d.get("watch_suggestions")
                                or d.get("watch_actions"))
                return True
            content = llm.chat_smart(msgs, role="extract", local_model=model, cfg=cfg,
                                     format_json=True, timeout=60.0,
                                     validate=_extract_usable).get("text", "")
        data = _parse_chat_response(content)
    except Exception as exc:
        log.warning("action-extraction failed: %s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    intent = (data.get("intent") or "none").lower()
    # "watch me for X and Y" is TWO watches; the model returns them as a "watches" list.
    # A single "watch" object stays supported — most requests are one thing.
    raw = data.get("watches")
    raw_watches = (raw if isinstance(raw, list)
                   else [raw] if isinstance(raw, dict)      # bare object instead of a list
                   else [data.get("watch")])
    raw_watches = [w for w in raw_watches if isinstance(w, dict)]
    if intent in ("update", "create") and not raw_watches:
        # Wrong-slot repair: the model sometimes builds a perfectly good config but drops it
        # into listing_query (or another slot). If it walks like a watch, adopt it.
        for v in data.values():
            if isinstance(v, dict) and _WATCH_SHAPE_KEYS & set(v.keys()):
                raw_watches = [v]
                break
    if intent in ("update", "create") and raw_watches:
        out = []
        for w in raw_watches:
            w = dict(w)
            w.setdefault("action", intent)
            if intent == "update":
                # Snap the name to the real stored watch (model often drops the site suffix),
                # falling back to the focus watch — so applying the edit can't 404.
                # (PENDING_CREATE is a sentinel, not a watch name — never write it into one.)
                real = (_resolve_watch_name(w.get("name", ""), cfg)
                        or (focus if focus != PENDING_CREATE else None))
                if real:
                    w["name"] = real
                else:
                    # An "update" naming NO real watch is a mislabeled CREATE (the model
                    # says update mid-setup while the watch doesn't exist yet). Flipping it
                    # keeps the card honest and makes Apply POST instead of 404-ing a PUT.
                    w["action"] = "create"
                    out.append(w)
                    continue
                # An edit that doesn't touch the urls often omits them entirely; that means
                # "unchanged", not "no urls" — backfill from the stored watch so the no-URL
                # safety net (which exists to stop unmonitorable CREATEs) can't eat the edit.
                if not w.get("urls"):
                    stored = next((sw for sw in getattr(cfg, "watches", [])
                                   if getattr(sw, "name", None) == w.get("name")), None)
                    if stored is not None and getattr(stored, "urls", None):
                        w["urls"] = list(stored.urls)
            out.append(w)
        return {"watch_suggestion": out[0], "watch_suggestions": out}
    if intent == "actions" and isinstance(data.get("watch_actions"), list):
        return {"watch_actions": data["watch_actions"]}
    if intent == "lookup" and isinstance(data.get("listing_query"), dict):
        return {"listing_query": data["listing_query"]}
    return {}


def _is_owned(name: str, cfg, owner: str | None) -> bool:
    """True if this watch may be acted on in the current scope. owner=None (desktop) → any
    watch. owner set (a Telegram person) → only a watch whose owner is that same chat_id, so
    the buddy can never start/stop/edit/delete someone else's watch."""
    if not owner or _is_admin_owner(owner, cfg):
        return True                       # desktop, or the owner on their phone: full control
    w = next((x for x in cfg.watches if x.name == name), None)
    return bool(w and str(getattr(w, "owner", "") or "") == str(owner))


def _complete_assistant_turn(system: str, messages: list, cfg, model: str,
                             owner: str | None = None) -> dict:
    """Run one assistant turn as two phases: (1) a natural-language reply that focuses purely
    on understanding the user and tracking which watch is meant, then (2) a dedicated extraction
    that turns any concrete request into a validated watch_suggestion / watch_actions /
    listing_query. Returns the response dict (plus a private "raw" the caller persists then
    drops). Powers the oversight Watcher chat. Never raises.

    owner (a Telegram sender's chat_id) scopes EVERYTHING to that person: creates are stamped
    with their owner, and edits/actions/lookups on watches they don't own are dropped."""
    # Only consider the RECENT tail of the conversation. The chat UI never clears itself, so the
    # client sends the ENTIRE history every turn; feeding all of it to the model makes it look too
    # far back (stale focus — it grabs a watch mentioned 20 messages ago — and eventually overflows
    # context). Keep the last _CHAT_CONTEXT_MESSAGES so "it"/"that one" resolve to the current
    # thread, not ancient history.
    messages = (messages or [])[-_chat_context_limit(cfg):]
    # Clean the replayed context: older assistant turns may be raw JSON envelopes, which
    # otherwise confuse the model about which watch is in play.
    conv = [({**m, "content": _prose(m.get("content"))}
             if isinstance(m, dict) and m.get("role") == "assistant" else m)
            for m in messages]
    focus = _focused_watch_name(conv, cfg)
    # Smart escalation: everyday chat stays on the fast local model; only the hard turns (creating
    # or changing a watch, a long/complex ask) are allowed to reach cloud when it's enabled. This
    # keeps the phone snappy and the bill near zero while cloud still handles the comprehension-
    # heavy turns. A no-op when cloud chat is off — everything is local either way.
    hard = _is_hard_chat_turn(conv, focus)
    log.debug("chat: %s turn (%s)", "hard" if hard else "easy",
              "cloud-eligible" if hard else "local-only")
    try:
        reply, eval_count, prompt_count, duration_ns = _chat_reply_natural(
            system + _converse_focus_line(focus, conv), conv, model, force_local=not hard)
        action = _extract_watch_action(conv, reply, cfg, model, focus, force_local=not hard)
        data = _normalize_turn({"message": reply, **action})

        listings = None
        lq = data.get("listing_query")
        # When the person plainly asked to SEE what was found, make sure the lookup is one that
        # actually answers them. Two failures showed up live and both are fixed here:
        #   • no lookup at all → the reply described the watch's CRITERIA back ("it's an under
        #     30-foot motor boat…"), which is what was asked FOR, not what was found;
        #   • an UNSCOPED lookup → 200 rows of everything ever seen, Brooklyn sofas included,
        #     because the model omitted the watch, the matched-only flag and the limit.
        # So we fill a missing query AND complete a vague one, without overriding anything the
        # model actually decided.
        # A lookup runs when EITHER the user plainly asked to see finds OR the model emitted a
        # listing_query on its own. Both paths go through the SAME scoping+capping here, because
        # the raw model query is the dangerous one: "what's on my watchlist?" isn't a lookup, but
        # the model answered it with an unscoped, uncapped query that dumped 200 rows of
        # everything. Whatever produced the query, it leaves this block scoped to a watch and
        # capped — or dropped.
        latest = _latest_user_text(conv)
        # A question about the WATCHES ("what watches are running?", "what's on my watchlist?") is
        # answered in prose, never with listings — so drop any query the model attached, whatever
        # it named. This wins over the lookup/scoping logic below.
        if _is_watch_status_request(latest) and not _is_lookup_request(latest):
            if isinstance(lq, dict):
                log.info("chat: dropped a listing query on a watch-status question")
            lq = None
        is_lookup = _is_lookup_request(latest)
        if is_lookup or isinstance(lq, dict):
            target = focus if (focus and focus != PENDING_CREATE) else ""
            if not target:
                target = _watch_named_in(latest, cfg, owner)    # "…matches for boats"
            if not target and is_lookup:
                mine = _watches_for_owner(cfg, owner)
                if len(mine) == 1:
                    target = mine[0].name    # sole watch — safe to assume, but only for a real lookup
            if not isinstance(lq, dict):
                lq = {}
            if not (lq.get("watch") or "").strip() and target:
                lq["watch"] = target
            if lq.get("matched_only") is None:
                lq["matched_only"] = True           # "show me the matches" means the MATCHES
            # The user's OWN words decide how many: an explicit count ("top 10") or a singular
            # "the latest match" (→ 1) WINS over whatever limit the model guessed into its query.
            # Without this, "show me the latest match" returned 10 because the model's listing_query
            # already carried limit=10, so our singular-latest rule never got to apply.
            explicit = _lookup_limit(latest, default=None)
            if explicit is not None:
                lq["limit"] = explicit
            elif not lq.get("limit"):
                lq["limit"] = 10
            # The model hallucinated a lookup for a message that never asked to SEE listings, and
            # we can't pin it to a watch the user referenced — running it would dump the whole
            # store. Drop it; the model's own prose (which has the watch context) answers.
            if not is_lookup and not (lq.get("watch") or "").strip():
                lq = None
                log.info("chat: dropped an unscoped model lookup for a non-lookup message")
            elif isinstance(lq, dict) and lq.get("watch"):
                log.info("chat: listing lookup scoped to %r (matched_only=%s, limit=%s)",
                         lq["watch"], lq["matched_only"], lq["limit"])
        if isinstance(lq, dict):
            # Scope a Telegram person's "what have you found" to THEIR watches: a lookup must
            # name a watch they own, otherwise it could surface everyone's finds. Desktop is open.
            if owner and not _is_owned(_resolve_watch_name(lq.get("watch", "") or "", cfg) or "",
                                       cfg, owner):
                log.info("chat: dropped listing lookup for %s — no owned watch named", owner)
            else:
                listings = _run_listing_query(lq)

        _VALID_ACTIONS = {"delete", "enable", "disable", "start", "stop", "reset"}
        latest_user = _latest_user_text(messages)
        watch_actions = []
        for a in (data.get("watch_actions") or []):
            act = a.get("action") if isinstance(a, dict) else None
            if act not in _VALID_ACTIONS:
                continue
            # Grounding: the user's OWN message must use this action's verb — otherwise the eager
            # extractor can surface an action (worst case a DELETE) card the user never asked for.
            verb = _ACTION_VERB_RE.get(act)
            if verb and not verb.search(latest_user):
                log.info("chat: dropped ungrounded '%s' action for %r — user didn't ask for it",
                         act, a.get("name"))
                continue
            real = _resolve_watch_name(a.get("name", ""), cfg)   # tolerate model name drift
            if real and not _is_owned(real, cfg, owner):
                log.info("chat: dropped '%s' on %r — not owned by %s", act, real, owner)
                continue
            if real:
                watch_actions.append({"action": act, "name": real})

        # Deterministic RESET. The 14b kept CHATTING about "remake/reset the X watch" ("could you
        # clarify…") instead of doing it, so we detect the intent in code and issue the reset
        # ourselves — clear the watch's finds and re-run it fresh — grounded on a watch the user
        # named (or the one in focus) and owns. Replaces any start/enable it emitted for the same
        # watch, since a reset already re-enables and re-runs it.
        if _RESET_RE.search(latest_user) and not any(a["action"] == "reset" for a in watch_actions):
            target = focus if (focus and focus != PENDING_CREATE) else _watch_named_in(latest_user, cfg, owner)
            real = _resolve_watch_name(target or "", cfg)
            if real and _is_owned(real, cfg, owner):
                watch_actions = [a for a in watch_actions
                                 if not (a["name"] == real and a["action"] in ("start", "enable"))]
                watch_actions.append({"action": "reset", "name": real})
                log.info("chat: deterministic reset of %r", real)
        watch_actions = watch_actions or None

        message = data["message"]   # _normalize_turn guarantees a message (never raw JSON)
        suggestions = data.get("watch_suggestions") or (
            [data["watch_suggestion"]] if isinstance(data.get("watch_suggestion"), dict) else [])
        # Hard safety net: never ship a watch with no real URL. A watch with an empty (or bogus,
        # non-http) urls list can't monitor anything — the model occasionally does this for a
        # request it couldn't turn into a real page (e.g. "watch the Anacortes clam digger"). Drop
        # those; the prompt tells the assistant to ask the user for the link instead.
        def _has_real_url(s):
            return isinstance(s, dict) and any(
                isinstance(u, str) and u.strip().lower().startswith(("http://", "https://"))
                for u in (s.get("urls") or []))
        suggestions = [s for s in suggestions if _has_real_url(s)]
        # Deterministic guard: drop 'update' cards the user didn't actually ask for. The small
        # extract model sometimes proposes edits to existing watches that were never mentioned
        # (the "2 edit cards I wasn't talking about" bug); an edit survives only when the user's
        # own latest message asks for a change AND points at that watch. Creates are untouched.
        suggestions = _ground_update_suggestions(suggestions, messages, focus)
        # A BARE "yes"/"no"/"ok" answers the assistant's own last question — it can never name a
        # new thing to watch. Without this, "show me the matches" → "want to see them?" → "Yes"
        # came back as "I'll set up that watch", hijacking the conversation with a create card.
        # The one exception is a create already mid-setup (PENDING_CREATE), where yes DOES confirm.
        if focus != PENDING_CREATE and _is_bare_confirmation(latest_user):
            dropped = [s for s in suggestions if (s.get("action") or "create") == "create"]
            if dropped:
                log.info("chat: dropped %d create card(s) on a bare yes/no — answering the "
                         "question instead", len(dropped))
            suggestions = [s for s in suggestions if (s.get("action") or "create") != "create"]
        # Ownership scope (Telegram): a person may only EDIT their own watches, and any watch
        # they CREATE is stamped as theirs so it shows up under them next time. Desktop
        # (owner=None) is unaffected.
        if owner:
            scoped = []
            for s in suggestions:
                if not isinstance(s, dict):
                    continue
                if (s.get("action") or "create") == "update":
                    if _is_owned(s.get("name", ""), cfg, owner):
                        scoped.append(s)
                    else:
                        log.info("chat: dropped edit of %r — not owned by %s", s.get("name"), owner)
                else:
                    s["owner"] = str(owner)      # a buddy-created watch belongs to the buddy
                    scoped.append(s)
            suggestions = scoped
        # Confirm-before-creating: if the assistant is ASKING the user to clarify (and NOT also
        # committing to the watch), hold any 'create' suggestion until they answer — so a truly
        # half-formed request ("guitars?") gets clarified first instead of instantly spawning a
        # watch. But if the reply COMMITS ("setting up a Miata watch under $8k…"), let the card
        # through even if it ends with an optional question ("want price alerts too?") — the user
        # asked for the watch, so don't make them re-confirm. (Edits always go through.)
        if "?" in message and not _reply_commits_to_action(message):
            suggestions = [s for s in suggestions
                           if isinstance(s, dict) and (s.get("action") or "create") != "create"]
        # Expand each proposed marketplace watch's search into effective terms.
        all_terms = []
        for sug in suggestions:
            if isinstance(sug, dict) and sug.get("mode") == "continuous" and sug.get("urls"):
                all_terms += _expand_watch_search(sug, messages, model)
        if all_terms:
            seen = set(); uniq = [t for t in all_terms if not (t in seen or seen.add(t))]
            message += "\n\nI'll search a few ways to catch more of these: " + ", ".join(uniq) + "."

        # ── Per-person settings from chat: check-in cadence ("check in every 6 hours") ──
        checkin_hours = _classify_checkin(latest_user)
        if checkin_hours is not None and owner:
            _set_checkin_pref(owner, checkin_hours)
            message = ("Got it — I'll stop the quiet-period check-ins. You'll still get alerts "
                       "for new finds." if checkin_hours == 0 else
                       f"Got it — I'll check in about every {checkin_hours:g} hour"
                       f"{'' if checkin_hours == 1 else 's'} when there's nothing new.")
            suggestions, watch_actions = [], None

        # ── Deterministic NAMED start/stop ("start up the macgregor watch") ──
        # Same reason as the reset short-circuit above: the 14b is unreliable at emitting the
        # action, and when it whiffs the bare handler below asks the admin "whole Watcher or just
        # yours?" instead of just doing it. If the user's own words name a single owned watch with
        # a clear start/stop verb, carry it out. Only fills a gap — never overrides a real action.
        if not watch_actions:
            _life = _named_lifecycle_action(latest_user, cfg, owner)
            if _life:
                watch_actions = [_life]
                message = ("Starting it now." if _life["action"] == "start" else "Stopping it now.")
                log.info("chat: deterministic %s of %r", _life["action"], _life["name"])

        # ── Master switch vs "my watches" (deterministic — too consequential for the 14b) ──
        # The whole-Watcher pause/resume is admin-only; a person's bare "stop" is scoped to their
        # own watches, and an admin's ambiguous bare "stop" asks which they mean (the user's choice).
        program_action = None
        verb, scope = _classify_lifecycle(latest_user)
        already_named = bool(watch_actions) or bool(suggestions)
        if verb and scope and scope != "ambiguous":
            is_admin = _is_admin_owner(owner, cfg)
            act = "stop" if verb == "pause" else "start"
            if scope == "program":
                watch_actions, suggestions = None, []      # they meant the whole thing, not one watch
                if is_admin:
                    program_action = verb
                    message = ("Pausing all watching now — nothing will sweep until you resume."
                               if verb == "pause" else
                               "Resuming — The Watcher is watching again.")
                else:
                    message = (f"Only the owner can {verb} the whole Watcher. I can {act} your own "
                               f"watches though — just tell me to {act} my watches.")
            elif scope == "all_mine":
                suggestions = []
                mine = _watches_for_owner(cfg, owner)
                # "Kill them all — except X" / "…keep the MacGregor one on". The blanket action
                # used to steamroll the exception clause and stop the one watch the user asked to
                # spare. If the message carves out an exception and names a watch, spare it.
                spared = None
                if re.search(r"\b(?:keep|except|but (?:leave|keep)|apart from|other than|"
                             r"spare|leave)\b", latest_user, re.I):
                    spared = _resolve_watch_name(
                        _watch_named_in(latest_user, cfg, owner) or "", cfg)
                acted = [w for w in mine if w.name != spared]
                watch_actions = [{"action": act, "name": w.name} for w in acted] or None
                if spared and watch_actions:
                    message = (f"{act.title()}ing {len(watch_actions)} of your watches — "
                               f"keeping “{spared}” as it is.")
                else:
                    message = (f"{act.title()}ing all {len(watch_actions)} of your watches."
                               if watch_actions else "You don't have any watches yet.")
            elif scope == "bare" and not already_named:
                if is_admin:
                    message = (f"Do you mean {verb} the **whole Watcher** (everything, for everyone), "
                               f"or just your own watches?")     # ask when it's not obvious
                else:
                    mine = _watches_for_owner(cfg, owner)
                    watch_actions = [{"action": act, "name": w.name} for w in mine] or None
                    message = (f"{act.title()}ing your {len(watch_actions)} watch(es)."
                               if watch_actions else "You don't have any watches yet.")

        # WE HOLD THE REAL ROWS, SO THE MODEL DOES NOT GET TO NARRATE THEM. Asked for the
        # matches, the small model either writes a blank template ("**Title:** [Boat Title] …
        # $XX,XXX") or — far worse — invents convincing ones ("20ft Motor Boat with Outboard
        # Engine, $12,000, Everett") while the genuine listings travel alongside it. A reader can
        # see through blanks; they cannot see through a fabrication, and someone could drive to
        # Everett for a boat that never existed. Either shape is replaced with a plain lead-in.
        if listings and _should_replace_prose(message):
            what = (lq or {}).get("watch") or "your watches"
            message = f"Here {'is' if len(listings) == 1 else 'are'} " \
                      f"{len(listings)} match{'' if len(listings) == 1 else 'es'} for “{what}”:"
            log.info("chat: replaced a placeholder listing template with the real rows")

        return {
            "message":           message,
            "watch_suggestion":  suggestions[0] if suggestions else None,
            "watch_suggestions": suggestions or None,
            "listings":          listings,
            "watch_actions":     watch_actions,
            "program_action":    program_action,
            "tokens":            eval_count,
            "prompt_tokens":     prompt_count,
            "duration_ms":       duration_ns // 1_000_000,
            "raw":               message,   # clean prose persisted to history (not a JSON blob)
        }
    except httpx.ConnectError:
        return {"message": "Ollama is not reachable. Start the Ollama service and try again.",
                "watch_suggestion": None}
    except Exception as exc:
        log.warning("Assistant turn error: %s", exc)
        return {"message": f"Assistant error: {exc}", "watch_suggestion": None}


def _expand_watch_search(ws: dict, messages: list, model: str) -> list[str]:
    """
    If a suggested continuous watch points at a marketplace SEARCH URL, expand the
    shopper's intent into several effective search terms and rebuild ws['urls'] (one URL
    per term). Returns the terms used (for the message), or [] if not applicable / failed.
    Mutates ws in place. Never raises.
    """
    try:
        from urllib.parse import urlparse, parse_qsl
        from web_watcher.search_terms import expand_search_terms, build_search_urls, _search_param
        urls = ws.get("urls") or []
        if not urls:
            return []
        base = urls[0]
        query = dict(parse_qsl(urlparse(base).query, keep_blank_values=True))
        if not _search_param(query):
            return []                          # not a keyword-search URL (e.g. a feed/page)
        last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        intent = (ws.get("instruction") or last_user or "").strip()
        terms = expand_search_terms(intent, model)
        if not terms:
            return []
        ws["urls"] = build_search_urls(base, terms)
        return terms
    except Exception as exc:
        log.warning("Watch search-term expansion failed: %s", exc)
        return []


def _run_listing_query(params: dict, db_path=None) -> list[dict]:
    """
    Resolve a listing-query request (from the /api/listings endpoint or an assistant
    `listing_query`) and return matching rows from the global store. A watch name is
    resolved to its stable id; an unresolvable name returns [] rather than querying all.
    """
    from web_watcher.config import load
    from web_watcher.storage import query_listings

    def _int(v):
        try:
            return int(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    watch_id = None
    wname = params.get("watch")
    if wname:
        w = next((x for x in load().watches if x.name.lower() == str(wname).lower()), None)
        if not w:
            return []
        watch_id = w.id or w.name

    try:
        return query_listings(
            watch_id=watch_id,
            matched=params.get("matched_only"),          # True / False / None
            text=(params.get("text") or None),
            transmission=(params.get("transmission") or None),
            drivetrain=(params.get("drivetrain") or None),
            min_year=_int(params.get("min_year")),
            max_price=_int(params.get("max_price")),
            max_mileage=_int(params.get("max_mileage")),
            limit=min(_int(params.get("limit")) or 200, 500),
            db_path=db_path,
        )
    except Exception as exc:
        log.warning("Listing query failed: %s", exc)
        return []


def _parse_chat_response(raw: str) -> dict:
    """Extract the JSON object from the model's response, tolerating extra text."""
    import re
    text = raw.strip()
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find the outermost {...} block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    # Fallback: treat raw text as the message
    return {"message": text, "watch_suggestion": None}
