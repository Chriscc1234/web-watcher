# Changelog

All notable changes to Web Watcher will be documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

---

## [0.20.1-alpha] — 2026-07-08 (Clean-machine launch fixes)

Found live on a fresh Windows 10 VM with no developer tools — the two things that stopped
0.20.0 from launching for a first-time user.

### Fixed — app now starts on a truly clean Windows
- **Bundled the Visual C++ runtime DLLs.** python-build-standalone ships the C runtime
  (`vcruntime140*`) but not the C++ runtime (`msvcp140.dll` et al), so Playwright's `greenlet`
  extension failed at import with *"DLL load failed while importing _greenlet"* on any machine
  without the VC++ Redistributable. `build_runtime.py` now copies the six C++ runtime DLLs next to
  `python.exe`, so native extensions load with nothing pre-installed.
- **Config written as UTF-8.** The installer's default watch name contains an em-dash; it was
  written with Windows' default cp1252 encoding, so `config.load()` (UTF-8) crashed at startup
  (`'utf-8' codec can't decode byte 0x97`) — the app 500'd on every `/api/watches` call and the
  scheduler failed to start. `install.py` now writes `config.yaml` as UTF-8.
- **Self-healing config read.** `config.load()` now falls back to cp1252 and rewrites the file as
  UTF-8 if it encounters a mis-encoded config, so any machine already carrying a bad config repairs
  itself on next launch instead of failing.

### Added — visible startup-crash dialog
- **No more silent sand-timer.** If the app hits a fatal error before the window opens, it now shows
  a native Windows message box naming the error and the session-log path (and pointing at the
  Report-a-bug flow), instead of vanishing with no feedback.

---

## [0.20.0-alpha] — 2026-07-04 (Self-healing install + bug reporter)

### Added — self-healing setup (from the live VM clean-install test)
- **Model downloads auto-retry on stall.** `install.py` now detects a stalled `ollama pull` (no
  progress for 150s — a flaky-network hang, seen live) and restarts it; Ollama resumes from its
  cached layers, so retries are cheap. Up to 6 attempts. Previously a single network blip hung the
  whole install forever with no recovery.
- **Interrupted installs self-heal on next launch.** Provisioning drops a `.setup_complete` marker
  when it finishes; if that marker is missing (a crashed/stalled install), the launcher re-runs
  setup — in a visible console, with the retry above — *before* opening the app. So a broken install
  fixes itself by just reopening the app (or re-running the installer); no command line ever needed.

### Added — in-app bug reporter
- **"🐛 Report a bug" button** (top nav) → a small form (title + what happened). On submit it bundles
  a `WebWatcher-bug-<timestamp>.zip` onto the **Desktop** (recent logs + app version + OS + a
  **credential-free** watch summary) and opens the folder, so a tester can send it to the developer.
  Fully offline; the report never includes `config.yaml` or notification secrets (`POST /api/bug/report`,
  `_write_bug_report`; test asserts no Telegram/email secrets leak).

---

## [0.19.0-alpha] — 2026-07-03 (Per-user data root — data separated from code)

### Changed
- **All user data now lives in a single per-user data root** (`%LOCALAPPDATA%\WebWatcher` on
  Windows, `~/.web-watcher` elsewhere), completely separate from the installed `web_watcher/` code.
  This is the groundwork for a real installer: an install/update can swap the code folder without
  ever touching a watch, result, saved login, or log. New central module `web_watcher/paths.py`
  resolves the root once (overridable with `$WW_DATA_DIR` for tests/portable installs) and exposes
  `config_path()`/`db_path()`/`screenshots_dir()`/`log_dir()`/`webview_dir()`/`browser_state_path()`/
  `profile_dir()`/`watcher_history_path()`. Every consumer (storage, config, browser, main, services,
  dashboard, install, uninstall, launcher reset) now points there.
- **One-time automatic migration (with backup).** On first launch after this update, the legacy
  in-repo `data/` + `config.yaml` are **copied** into the new root — the originals are left in place
  as a backup — and a `.migrated` marker prevents it from ever repeating. Migration is best-effort and
  never blocks startup; a `reset`/uninstall re-drops the marker so it won't re-import the discarded
  backup.

### Changed — chat that converses first, structures second
- **The Watcher chat is now a two-phase turn.** Forcing the model to emit a JSON envelope on
  EVERY reply split its attention between understanding the user and formatting — so it misread
  requests, lost track of which watch was meant across turns, and sometimes described an edit in
  prose while forgetting the `watch_suggestion` object (no card appeared). Now:
  - **Phase 1 — converse:** the model replies in plain English (no forced JSON), given a
    "currently in focus" line so *"it" / "that watch" / "change something else on it"* resolves to
    the watch discussed earlier in the conversation.
  - **Phase 2 — build:** a dedicated extraction call decides whether there's a concrete watch
    action now and, if so, emits its full config (an update starts from the existing watch's real
    config); otherwise `none` and no card. A card appears only when there's real structure.
- **History hygiene:** the assistant's turn is now persisted as clean prose, not the raw JSON
  blob — replaying JSON blobs as context was itself confusing the model about which watch was
  meant. Replayed context is also sanitized (`_prose`) as a safety net.
- **Robust watch card:** the card's buttons reference the suggestion via a registry id instead of
  embedding its JSON in an inline `onclick` — an apostrophe/quote/newline in an instruction used to
  break the attribute and the card would silently not render.

### Fixed — bundled-runtime runtime bugs (found via the real install)
- **Ollama no longer pops a blank console.** `services._start_ollama` spawned `ollama serve` with
  no window flags; under the windowless `pythonw` launch Windows gave it its own console (a blank
  "ollama.exe" terminal). Added `CREATE_NO_WINDOW`.
- **App-wide UTF-8.** The launcher now runs the app with `PYTHONUTF8=1`/`PYTHONIOENCODING=utf-8`.
  The bundled runtime otherwise defaults to cp1252, so any log line with the `→/↳/✓` glyphs
  (all over agent/scheduler output) raised `UnicodeEncodeError` and crashed the console log
  handler — which, stacked on an Ollama-down error, is what buried the real chat-failure cause.

### Hardened — Ollama install no longer depends on winget
- First-run provisioning now installs Ollama via winget **or**, if winget is missing or fails,
  by **downloading the official OllamaSetup.exe and running it silently** (`/VERYSILENT`). Fresh
  machines without the App Installer — and Windows Sandbox, which ships without winget — can now
  provision Ollama unattended. (`install.py`: `_install_ollama_via_winget` / `_install_ollama_via_download`.)

### Added — bundled Windows installer (Phase 2)
- **Self-contained installer** so an end user needs nothing pre-installed. `build_runtime.py` fetches
  a relocatable CPython (python-build-standalone 3.13) and pip-installs every dependency into it
  (torch/opencv/scipy/whisper/easyocr baked in — no PyInstaller hook fights); `installer/installer.iss`
  (Inno Setup) packages that runtime + the app code into `WebWatcher-Setup-<version>.exe`, driven by
  `build_installer.py`. Result is a ~278MB installer.
- **Per-user install** to `%LOCALAPPDATA%\Programs\WebWatcher` (`PrivilegesRequired=lowest`): no admin
  prompt, and the in-app auto-updater (which stages code swaps into the app folder) can write there —
  a Program Files install would need admin for every update. User **data** stays in
  `%LOCALAPPDATA%\WebWatcher` (see the relocation above), so uninstall/reinstall never touches watches,
  results, or saved logins. Verified end-to-end locally: silent install → correct tree → app imports &
  runs under the bundled runtime → silent uninstall leaves user data intact.
- **First-run provisioning** (`provision.py`) runs under the bundled runtime and reuses `install.py`
  with new flags `--skip-deps` (deps already bundled), `--skip-shortcuts` (Inno makes them), and
  `--keep-config` (never overwrite an existing config on reinstall/upgrade). It ensures Ollama (winget),
  detects the GPU tier, pulls the local models, and installs Playwright Chromium.
- **Fix:** `install.py`/`provision.py` now force UTF-8 on stdout/stderr — under the bundled runtime with
  output piped to Inno the default cp1252 crashed on the ✓/→ status glyphs.
- **App icon** — a magnifying-glass mark in the app's accent blue (`installer/make_icon.py` →
  `web_watcher/dashboard/static/icon.ico`/`.png`). Used on the Desktop + Start Menu shortcuts, the
  Apps-list entry (`UninstallDisplayIcon`), and the running window/taskbar (via `webview.start(icon=)`
  + a stable `AppUserModelID` so Windows shows our icon, not the generic python one). Dev shortcuts
  (`install.py`) use it too.
- **Uninstall shows in Windows "Apps & features"** (per-user Add/Remove entry) and, at uninstall time,
  offers a one-time choice — *"Also delete your Web Watcher data?"* — that wipes `%LOCALAPPDATA%\WebWatcher`
  only if the user opts in. Default (and any silent uninstall) keeps data for a future reinstall.

### Notes
- Tests: new `tests/test_paths.py` (resolution, accessors, caching, migration, no-clobber, marker
  guard) and a session-wide `tests/conftest.py` that pins `WW_DATA_DIR` to a throwaway dir so the
  suite never touches real user data. Full suite green.

---

## [0.18.0-alpha] — 2026-07-03 (In-app Console tab + uninstaller)

### Added
- **Console tab** — since the app now runs windowless, there's an in-app shell. Run commands in the
  app folder (with `cd` persisting between commands, `shell=True` so pipes work, 120s timeout), and a
  **▶ Live log** toggle that tails the current session log. Guarded by an app-only header
  (`X-WW-Console`) on `POST /api/console/run` so a random web page can't drive the shell (a custom
  header forces a CORS preflight this server doesn't allow). `GET /api/console/log` tails
  `data/logs/`. (`services.console_run`/`tail_log`, endpoints, Console tab UI; tests added.)
- **Uninstaller** (`uninstall.py` + `uninstall.bat`) — removes the Desktop + Start Menu shortcuts and
  any legacy auto-start task, and optionally erases personal data (`--purge-data`). Leaves the shared
  heavy tools (Python, Ollama, models) and prints how to remove them. (The Inno Setup installer in
  Phase 2 will generate a proper Add/Remove-Programs uninstaller too.)

---

## [0.17.2-alpha] — 2026-07-02 (Consolidated start methods: Desktop + Start Menu only)

### Changed
- **The only ways to start Web Watcher are now a Desktop shortcut and a Start Menu entry** — both
  launch it **windowless** (`pythonw launcher.py`, no console flash). Replaced the installer's optional
  "start at Windows login" Task Scheduler registration (`register_startup`) with `create_shortcuts`,
  which creates both shortcuts (resolving the OneDrive-redirected Desktop correctly) and removes any
  legacy auto-start task from older installs. `launcher.py` now runs the app via `pythonw` +
  `CREATE_NO_WINDOW`. Flag renamed `--skip-startup` → `--skip-shortcuts` (old name kept as an alias).

---

## [0.17.1-alpha] — 2026-07-02 (Reset to a fresh install)

### Added
- **"Reset to a fresh install" (Settings → Danger zone).** Permanently erases all personal data —
  watches, results, DB, saved logins/cookies, chat history, settings — and restarts clean; the app and
  AI models stay installed. Gated behind three deliberate steps (warning → confirm → type "ERASE
  EVERYTHING") plus a server-side confirm guard on `POST /api/reset`. The wipe runs in `launcher.py`
  **before** the app opens the DB (no file locks), mirroring the update-restart mechanism via a
  `RESET_REQUESTED` flag. (`launcher._do_reset`, `services.request_reset`, `/api/reset`, Settings UI;
  tests added.)

---

## [0.17.0-alpha] — 2026-07-01 (Auto-update: git foundation + in-app GitHub-Releases updater)

### Added
- **Version control foundation.** Repo now under git with a hardened `.gitignore` (excludes
  `config.yaml` credentials, all of `data/` — saved cookies, DB, chat history — plus `updates/`,
  `dist/`, `build/`, caches). Added `config.example.yaml` (blank template; the app runs on defaults
  when `config.yaml` is absent).
- **In-app auto-updater (notify + one-click apply).** `web_watcher/updater.py` checks GitHub Releases
  on launch and every 6h; when a newer version exists it downloads the code bundle, **verifies its
  sha256**, and stages it under `updates/pending/`. The dashboard shows a banner (*"Version X is ready
  to install — What's new · Update & restart"*); the changelog opens in the chat dock. Updates ship
  **code only** — never the ~15-20 GB of models. Endpoints: `GET /api/update/status`,
  `POST /api/update/check`, `POST /api/update/apply`. (`services.ServiceManager` background checker +
  `check_updates_now`/`update_status`/`request_restart`.)
- **Safe apply-on-restart.** New `launcher.py` (now what `start.bat` runs) applies any staged update by
  swapping the new code in **before** the app imports it, then launches — and relaunches when the app
  drops `updates/RESTART_REQUESTED` (the one-click apply). The live `web_watcher/` is backed up to
  `updates/backup/` first for manual rollback.
- **One-command releases.** `build_release.py` zips the app (code + static, no `__pycache__`), computes
  the sha256, extracts this version's CHANGELOG section into release notes (with a `sha256:` line the
  updater reads), and prints the `gh release create` command.

### Notes
- Updates are **disabled until configured**: set `WW_UPDATE_OWNER` (your GitHub user/org) — until then
  the checker no-ops and the banner never shows. Bundled-Python installer (so the buddy needs nothing
  pre-installed) is the next phase.
- 15 updater tests (version compare, release parsing, stage→apply round-trip, manager flow).

---

## [0.16.3-alpha] — 2026-07-01 (Watch-suggestion card no longer squishes in the chat)

### Fixed
- **The watch card in the chat dock squished/distorted when a suggestion had many search URLs.** The
  `.kv` rows are `display:flex`, and the value span had no `min-width:0`/wrapping — so a URL row with 8
  long Craigslist URLs on one line couldn't wrap, overflowed, squished the labels, and (with the new
  content auto-width) over-widened the dock. Two fixes: the card values now wrap
  (`overflow-wrap:anywhere`, label `flex-shrink:0`), and the card shows a compact **"Searches (N):
  term · term · … on <site>"** summary (decoded from the query params) instead of a wall of raw URLs.
  Card text is now HTML-escaped too. (`index.html`: `.watch-card .kv` CSS + `appendWatchCard`.)

---

## [0.16.2-alpha] — 2026-07-01 (Updating a watch from chat no longer errors)

### Fixed
- **Updating a watch via the assistant failed** with "Watch must specify either 'interval_minutes'
  or 'cron_expression'." The assistant's `update` suggestions carry only the changed fields (urls,
  instruction, terms) and omit `mode`, so `PUT /api/watches/{name}` validated the partial body as a
  brand-new watch → `mode` defaulted to `schedule` → the schedule validator rejected it (a continuous
  watch has no interval). `update_watch` now **merges** the incoming fields onto the existing watch
  (via `_merge_watch_update`) instead of validating the body standalone: mode/interval/id and anything
  the caller didn't touch are preserved, and assistant-only extras (`action`, `search_terms`) are
  ignored. Verified against the real failing suggestion; test added.

---

## [0.16.1-alpha] — 2026-07-01 (Baseline/flood now populates Results — "found lots, shows nothing" fixed)

### Fixed
- **A newly-started (or freshly-fixed) watch found hundreds of listings but showed nothing in
  Results.** Found in the log: the Craigslist watch extracted 200/165 listings, hit the flood guard,
  and `re-baselining … no alerts` — but re-baselining (and first-run priming) recorded the listings as
  *seen* WITHOUT judging them, so none got a match verdict → the (matches-only) Results view stayed
  empty. Worse, because the watch rotates search terms, each term's fresh backlog re-tripped the flood
  guard, so it kept baselining and never judged anything. Now priming and flood both run the judge on a
  capped slice (`_BASELINE_JUDGE_CAP = 60`, one card-level LLM call) and **record the matches to
  Results** — they still suppress notifications (no push-spam for pre-existing stock), but the finds
  actually show up. (`scheduler._baseline_batch`; test added.) This was surfaced by the 0.16.0
  Craigslist URL fix finally returning listings; the two together mean Craigslist watches now populate
  Results again.

---

## [0.16.0-alpha] — 2026-07-01 (Craigslist changed its URL format — extraction restored)

### Fixed
- **Craigslist had been returning 0 listings for every sweep** (found while reading the app log: the
  "Manual Sports Cars" watch logged 319 consecutive `Extracted 0 unique listing(s)` with no CAPTCHA or
  error). Root cause: **Craigslist changed its listing-URL format.** Old: `<city>.craigslist.org/…/<digits>.html`.
  New (2026): `www.craigslist.org/view/d/<slug>/<alphanumeric-id>` — no `.html`, a base62-style id. The
  built-in `_PAT_CL` only matched the old `.html` form, so `_listing_key` returned None for every card
  even though 253 results were on the page. Added `_PAT_CL_NEW` (`/view/[dp]/<slug>/<id>`), matched
  first with the legacy pattern as fallback, and taught the card-climb guard (`listingHrefs`) to
  recognize `/view/d/` too. **Live-verified: 30 listings now extract from a Craigslist search, correct
  distinct titles, no mislabeling.** (`monitor.py`; test added.)
- Note: the self-healing scraper→agent escalation (0.15.1) wouldn't have rescued this — the agent reads
  the same DOM, so the fix had to be the URL pattern. Old stored `.html` listings still resolve.

---

## [0.15.9-alpha] — 2026-06-29 (Results default to real matches + instant action confirmation)

### Changed
- **Results now show what the Watcher actually FOUND, not everything it crawled past.** The "Matches
  only" filter is **on by default**, so the Results view shows just the listings the judge accepted
  (e.g. the diesel watch shows real diesels, not the sports cars / random junk its searches happened
  to surface — those were already marked *not matched*, just still displayed). Untick the box to see
  everything. The all-watches view now respects the filter too: `query_listings(matched=True)` with no
  specific watch keeps only listings some watch judged a match (`EXISTS` over observations). (`index.html`
  default-checked; `storage.query_listings`; test added.)

### Fixed
- **Clicking a one-click action button now confirms instantly in the chat.** `watcherAction` used to
  open the dock but stay silent until the (sometimes slow) action finished. It now immediately echoes
  what you clicked as a message plus a live "The Watcher is on it…" bubble, then replaces it with the
  result — so you get feedback the moment you click. (`index.html`.)

---

## [0.15.8-alpha] — 2026-06-29 (Two larger text sizes + watch the agent's mouse)

### Added
- **Two more (larger) text-size steps.** The A-size row now has six steps: added `xxlarge` (1.6×) and
  `huge` (1.85×) on top of small/normal/large/xlarge. The native window auto-sizes to these too (when
  not maximized), clamped to the screen. (`index.html`: `_ZOOM`, the `fs-*` buttons, `setFontSize`.)
- **See where the agent's mouse is.** New opt-in "Show agent cursor" toggle (Settings) draws a red
  cursor in the agent's browser that follows its synthetic mouse (tracks the real `mousemove` events
  Playwright dispatches) and pulses on click. Only visible with a **visible** browser (Headless off).
  Off by default — it adds a DOM node a site could see, so it slightly reduces stealth. (`config.py`
  `BrowserConfig.show_agent_cursor`; `browser.py` `_CURSOR_JS` + `BrowserSession(show_cursor=…)`;
  `scheduler._open_continuous_browser` passes it; `GET/POST /api/browser`; Settings toggle.)

---

## [0.15.7-alpha] — 2026-06-29 (Native window resizes to fit text size when not maximized)

### Added
- **The window now sizes itself to the selected text size — when it isn't maximized.** The 0.14.1
  accessibility fix removed native window resizing because it fought a *maximized* window; this brings
  it back for the *non-maximized* case only. New `_WindowApi` (main.py) is exposed to the UI as
  `window.pywebview.api.resize_for_zoom(zoom)`: it resizes the window to `BASE × zoom` (clamped to the
  screen work area and the 900×600 min), and **skips entirely when the window is maximized** (tracked
  via pywebview `maximized`/`restored` events). `setFontSize` calls it on every change, and a
  `pywebviewready` listener re-applies the current zoom once the bridge is up (so it also sizes right
  on launch). In a plain browser / headless preview it's a guarded no-op. (`main.py`, `index.html`.)

---

## [0.15.6-alpha] — 2026-06-29 (Chat dock opens compact instead of always-tall)

### Fixed
- **The chat window no longer stands ~320px tall in the corner when it's nearly empty.** Its
  `min-height` floor was `min(320px, …)`, which forced a tall skinny box even with just the greeting.
  Lowered the floor to `min(150px, …)` so the dock sizes to its content and grows upward as the
  conversation fills in (the content auto-grow + top-grip resize from earlier are unchanged).
  (`index.html`: `.watcher-dock` min-height.)

---

## [0.15.5-alpha] — 2026-06-29 (Confirm-before-creating, explore-on-START, dock grows wider, bouncy loader)

### Changed
- **The Watcher confirms before dropping a watch on you.** For a vague or half-formed request it now
  restates what it thinks you mean and asks (which site? budget? new/used?) instead of instantly
  spawning a watch. Prompt guidance PLUS a deterministic backstop: if the assistant's reply contains a
  question, any brand-new ("create") watch is **held** that turn until you answer (a confident,
  non-question reply still ships the watch immediately). (`_complete_assistant_turn`; test added.)
- **Site exploration now happens on START, not on creation.** Creating a watch no longer launches a
  browser. The first time a watch is **started** (individually or via the Watcher), it does one
  exploration round of any not-yet-learned site first — with a visible heads-up in The Watcher's feed
  ("Heads up — I haven't explored offerup.com yet, I'll do a quick exploration round before I start
  watching"). Uses the watch's `use_login_profile` for login-gated sites; explored once per process so
  restarts don't re-run it. (`scheduler._explore_new_sites_on_start`; `ServiceManager.narrate` wired
  into the scheduler via `_narrator`.)

### Added
- **The chat window now grows wider too, not just taller.** A left-edge drag grip resizes width (drag
  left = wider; double-click = auto), and the dock auto-widens to fit content that doesn't wrap (wide
  cards/rows), capped at the viewport. (`index.html`: `.dock-resize-grip-x`, `_initDockResizeX`,
  `_growDockWidth` hooked into `appendMsg`.)
- **The "The Watcher is looking…" loader now bounces.** Each letter animates with a staggered wave
  (`_fillBouncingText` + `.bounce-letter`) instead of sitting as static text.

---

## [0.15.4-alpha] — 2026-06-29 (Auto-explore new sites — no button, ask-about-login first)

### Added
- **Web Watcher now explores a new site by itself, no "explore" button.** When a watch is created for
  a site it hasn't learned (not built-in, no profile), `create_watch` kicks off a **background
  exploration** of that site (`_background_learn_site`) and narrates progress into The Watcher's feed
  ("Exploring offerup.com to learn how it's laid out… → Learned it"). It uses the watch's own
  `use_login_profile`, so a login-gated site is explored signed-in via the saved profile — Web Watcher
  never types credentials itself.
- **The assistant knows whether it's seen a site, and asks the one question that matters.** The chat
  context now tells the model exactly which sites are known (built-ins + learned); for anything else it
  is instructed to tell the user it hasn't explored the site yet and **ask whether it needs a login**
  (→ sets `use_login_profile`) before proposing the watch. Verified live: asked to watch Mercari, the
  assistant replied "Does Mercari require you to be logged in to see listings?" instead of silently
  inventing a URL.
- **Site-knowledge API.** `GET /api/sites/status?url=` → `{domain, known, kind}` (builtin/learned/
  unknown); `POST /api/sites/learn` now takes `use_login_profile`. New deterministic helpers
  `site_status` / `unknown_sites` / `first_url_for_domain` in `sitelearn.py`; `ServiceManager.narrate`
  lets background work post to the oversight feed.

---

## [0.15.3-alpha] — 2026-06-29 (Bogus city-subdomain URLs — "seattle.offerup.com")

### Fixed
- **The assistant invented dead hosts like `seattle.offerup.com` and wouldn't self-correct.** The local
  model pattern-matches from Craigslist (which really does use `seattle.craigslist.org`) and stamps a
  city subdomain onto sites that serve one flat domain — a host that doesn't resolve. In the chat log
  it did this for OfferUp, kept it after "modify to offerup.com", then misread "not seattle.offerup.com"
  entirely. Two-part fix:
  - **Deterministic backstop:** `create_watch` / `update_watch` now run `_normalize_marketplace_urls`,
    which rewrites a city/geo subdomain to the bare domain for known flat sites (OfferUp, eBay, CarGurus,
    Cars.com, AutoTrader, Facebook, GovDeals, Mercari, Nextdoor, Gumtree). Craigslist/Kijiji are excluded
    — they DO use real city subdomains. A model hallucination can no longer ship a dead URL. Test added.
  - **Prompt knowledge:** the assistant system prompt now carries the correct search-URL format for each
    common site and an explicit "city subdomains are a Craigslist-only trait — never write
    seattle.offerup.com; apply user URL corrections exactly and re-emit the fixed urls" rule.
- **Cleaned the live config:** the saved "Diesel Vehicles (OfferUp)" watch had its four
  `seattle.offerup.com` URLs rewritten to `offerup.com` in `config.yaml`.

---

## [0.15.2-alpha] — 2026-06-29 (Extractor mislabel fix — the "74 dupes / links to a different car" bug)

### Fixed
- **Listings were being mislabeled with a neighbour's title (bogus "N dupes", link went to a
  different vehicle).** The card-text climb in `extract_listings` (`_EXTRACT_JS`) could overshoot the
  individual result card and ascend into the shared results `<ol>`, whose `innerText` is dominated by
  the FIRST result — so dozens of different trucks/SUVs got stamped with the first listing's title
  ("2002 Mazda Miata Roadster"). Identical title+price → same content fingerprint → they collapsed
  into one row with a false "74 dupes" badge, and its link pointed at a *different* car. The climb now
  **stops before entering any container that holds more than one listing** (`listingHrefs(parent) > 1`).
  Real-browser DOM test added (`tests/test_extraction_dom.py`).
- **A dup group now links to its freshest still-live repost, not the deleted original.** `find_duplicate`
  keeps the earliest-seen listing as canonical, but that original post is usually already deleted by
  the time a seller has reposted — so its link 404'd / redirected. `query_listings` now returns the
  URL of the most-recently-seen member of the fingerprint group (`best_url`). (`storage.py`; test in
  `tests/test_storage_listings.py`.)
- **Fingerprint weak-signal guard.** A terse generic title with no year ("chevy silverado" @ $5,600)
  used to fingerprint and could merge DIFFERENT trucks. `listing_fingerprint` now emits nothing unless
  the title is distinctive (≥4 tokens, or a year + ≥2 tokens) — per this module's "better to show a
  possible dup than hide a real listing" philosophy.
- **One-time data cleanup of the live DB.** Re-titled + re-fingerprinted **337** already-corrupted
  Craigslist rows from their URL slug (make- then year-mismatch detection); title/URL mismatches went
  3194→**0**. The bogus 74-member Miata group is gone; the largest remaining dup groups are now genuine
  reposts (a dealer relisting one 2025 F-450, a fridge with a distinct model number).

---

## [0.15.1-alpha] — 2026-06-29 (Chat-send fix + self-healing scraper→agent escalation)

### Fixed
- **The Watcher chat box did nothing on Send.** `watcherSend()` still called `autoGrowInput()` /
  set `input.dataset.manual` — leftovers from the 0.14.7 textarea version that 0.14.8 reverted and
  deleted. The undefined call threw a `ReferenceError` right after clearing the input and before the
  fetch, so the message vanished and nothing was sent. Removed the two dead lines. (Verified: the
  served `index.html` no longer references `autoGrowInput`; `/api/oversight/chat` returns 200. A page
  reload picks up the fix.) Results were never actually broken — the store has 3k+ listings and
  `/api/listings` returns them; this was the same single JS error making the page feel dead.

### Added
- **Self-healing engine selection — a watch escalates itself from scraper to AI agent when the
  scraper is blind.** If a non-autonomous continuous watch harvests **zero** listings for
  `_SCRAPER_BLIND_THRESHOLD` (2) sweeps in a row, the page almost certainly renders its listings with
  JavaScript (an SPA the fast scraper can't read), so the loop **auto-switches that watch to the agent
  path** for the rest of the session and records a run note. The user's `autonomous` flag is left
  untouched — this is a runtime, reversible decision, not a config rewrite. This is the real answer to
  "does it need to be an autonomous agent?": cheap scraper by default, agent automatically *only* where
  it's actually needed. (`scheduler.py`: `_run_continuous_sweep` now returns its harvest count,
  `_update_blind_streak`, dispatch in `_execute_continuous_watch`; test in `tests/test_scheduler.py`.)

---

## [0.15.0-alpha] — 2026-06-29 (Learn-a-new-site profiles + fingerprint hardening)

### Added
- **Point Web Watcher at a NEW site and it learns the layout.** New learned-site system: a
  `site_profiles` store (`storage.py`) keyed by registrable domain (`site_key()`), holding each
  site's listing-URL shape (a regex whose one capture = the stable listing id), search param, and
  sort options. `sitelearn.learn_site(url)` drives a real browser to a search-results page, harvests
  priced listing cards + the search/sort controls, and **deterministically infers the listing-URL
  pattern** (`monitor.infer_listing_pattern`) — no LLM needed for the regex (optional offline LLM
  only polishes the display name + a layout note). This turns "3 hardcoded sites" into "any site
  you've taught it once". Endpoints: `GET /api/sites`, `POST /api/sites/learn {url}`,
  `DELETE /api/sites/{domain}`.
- **Listing extraction is now profile-aware.** `_listing_key` / `extract_listings` accept learned
  profiles, so dedup keys are stable on sites beyond the built-in eBay/Facebook/Craigslist. The
  continuous sweep (scraper + agent-driven) loads profiles each sweep and passes them through.
- **Fingerprint hardening (`browser.py`).** The user-agent now tracks the **actual bundled Chromium
  version** (read from `browser.version`) instead of a hardcoded string that could go stale and
  mismatch the engine — a classic bot tell. `hardwareConcurrency`/`deviceMemory` are randomized to a
  *coherent* pair per session (was a constant 8/8), the headless viewport rotates across real desktop
  sizes (was a fixed 1920×1080), and the Sec-CH-UA client hints follow the real major version. New
  `maybe_warm_homepage()` sometimes lands on the site root before a deep search URL (the agent sweep
  uses it) so we aren't always teleporting straight to a results page.
- **The assistant knows which sites are "known"** (built-ins + learned) and steers users to *learn* a
  brand-new site before watching it. Watch-URL rule added: every URL must be a search-results/category
  page, never a single listing (fixes a dogfood case where it invented a listing URL).

### Fixed
- **A failed site-learn can no longer poison a site.** If inference fails (e.g. a JS/SPA site with no
  server-rendered listings) or only category links are found, `learn_site` now persists **nothing**
  and reports honestly; `_listing_key` also ignores any empty-regex profile. (A persisted empty-regex
  profile would have made `_listing_key` return None for every URL on that host, killing detection.)
  The learner only saves a profile when it saw enough **priced** listing cards and the pattern
  confirms against a sample.

### Verified
- 214 + new tests green. Live: `learn_site` on a real eBay results page inferred `/itm/(\d{5,})` +
  `_nkw` search + `_sop` sort and confirmed; Cars.com (SPA) and a Kijiji category page both failed
  *safe* (nothing persisted). Dogfood: the local assistant (qwen2.5:14b) created schema-valid watches
  from plain-English prompts for all six test sites (Craigslist, eBay, CarGurus, OfferUp, Kijiji,
  GovDeals).

### Notes
- **Facebook Marketplace / real account: still hold.** Recommendation unchanged — don't point a real
  personal FB account at it yet. If/when FB is tackled: throwaway account, persistent login profile,
  visible (non-headless), low frequency, after this hardening. FB remains the last target, not the
  proving ground.
- Best test targets going forward: server-rendered classifieds (eBay ✓, Craigslist, GovDeals) learn
  cleanly; heavy SPAs (CarGurus, Cars.com, AutoTrader) need an autonomous agent watch rather than the
  profile learner, since their listings render client-side.

---

## [0.14.8-alpha] — 2026-06-29 (Resizable chat WINDOW, cross-watch fix, listing dates + saved info)

### Added
- **The chat WINDOW now grows with its content and is resizable.** (The 0.14.7 change resized the
  little input box, which wasn't the ask.) The dock height is now `auto` — a big response makes the
  whole window taller — floored at a comfortable min and capped at the viewport (then the
  conversation scrolls). A drag grip along the top edge resizes the window manually (drag up = taller,
  since it's bottom-anchored); double-click the grip to return to auto-fit. The input is back to a
  simple single line. (`index.html`: `.watcher-dock` CSS, `.dock-resize-grip`, `_initDockResize`.)
- **Results now show when a listing was posted and when it was saved.** Each result carries a
  `Posted <date> · Saved <date>` line — "Posted" is the seller's date read from the ad page (captured
  on deep-read, populates going forward), "Saved" is when Web Watcher first scraped it. New
  `posted_at` column on the `listings` table (additive migration) + `extract_listing_posted_at`.
- **Saved post info stays accessible even if the original listing is deleted.** Every result has a
  "Saved info (kept even if the post is removed)" expander showing the cached ad body. The global
  `listings` store already persists title/price/body/image outside any watch and never deletes them
  when the source post goes away — this just surfaces it. (`index.html`: `renderResults`, `.saved-info`.)

### Fixed
- **Sports cars no longer leak into the trucks results (cross-watch matching).** When the cross-watch
  judge errored (e.g. Ollama busy/timeout), the judgment filter fell back to "keep ALL" — which dumped
  un-judged listings from one watch into another. The cross-watch path now fails **closed**: on a judge
  error it adds nothing to the other watch (a watch's OWN sweep still fails open, so it never silently
  drops a real match for the watch you created). (`scheduler.py`: `_filter_listings_by_judgment`
  `fail_closed`, `_cross_watch_match`; tests in `tests/test_scheduler.py`.)

### Notes
- **Why a sweep scrolls a page then switches search terms:** for Craigslist each search-term URL is a
  complete page of results (everything loads at once; scrolling just fetches the thumbnails), so once
  it's read that page it rotates to the next term + sort order to widen coverage and catch new posts
  quickly across all your terms. Over a full cycle every term is visited across every sort. Dwelling
  longer on one term would actually delay spotting new posts on the others — so this is by design.

### Added
- **The Watcher chat box grows — automatically and manually.** The single-line input is now a
  textarea that **auto-grows** as you type (up to ~320px, then it scrolls) so longer requests
  aren't cramped on one line, and has a **manual drag handle** (bottom-right corner) to set your
  own height. Once you drag it, auto-grow yields to your chosen size (tracked via a ResizeObserver);
  sending a message snaps it back to one line. Enter sends, **Shift+Enter** inserts a new line. An
  empty box rests at exactly one line (the long placeholder no longer inflates it). The Send button
  stays pinned to the bottom as the box grows. (`index.html`: `<textarea>`, `autoGrowInput`,
  `_initInputResize`, `.dock-inputrow` CSS.) Verified headless: rests at 38px, grows with content,
  caps at 320px with scroll, `resize: vertical` handle present, manual size respected, resets on
  send, no console errors.

### Fixed
- **Opening the chat dock (e.g. after clicking "broaden search terms") no longer shows garbled
  raw JSON.** The broaden action itself was fine — the garble came from the dock lazy-loading the
  *saved* conversation when it opens. Older assistant turns were persisted as raw JSON, and some
  are BARE watch objects with no `message` field (the pre-`_normalize_turn` shape); the history
  loader only pulled out `.message` and otherwise dumped the raw JSON string into a bubble. New
  `historyText()` mirrors the server's `_normalize_turn` on the client: use the message if present,
  synthesize a friendly line for a bare watch object (e.g. `(I updated "Manual Sports Cars" for
  you.)`), and only fall back to the text when it isn't JSON. So no saved turn can ever render as
  raw JSON again, regardless of how it was stored. (`index.html`: `historyText`,
  `loadWatcherHistory`.) Verified headless: the dock loaded the real saved history with 0 raw-JSON
  bubbles, bare-watch turns rendered as clean synthesized lines, no console errors.

---

## [0.14.5-alpha] — 2026-06-28 (Active / Inactive watch sections)

### Added
- **The Watches tab is now split into Active and Inactive sections.** Active = watches that are
  on (will run / be driven by The Watcher); Inactive = watches kept in the list but turned off.
  Each row has a one-click **Deactivate** / **▶ Activate** button that moves the watch between the
  two stacked sections — no more opening Edit and hunting for a checkbox. Inactive rows are dimmed,
  show an "inactive" status, and hide the Start/Run-Now control (an inactive watch doesn't run).
  Deactivating a running continuous watch also stops its loop (the scheduler reload won't restore a
  disabled watch). Backed by the existing `POST /api/watches/{name}/enabled` endpoint — `enabled`
  IS the active/inactive flag. (`index.html`: `renderWatchSection`, `watchRowHtml`,
  `setWatchActive`.)

### Fixed
- **Row buttons broke for watch names containing a double quote** (e.g. `Refrigerators … 36" wide`).
  The inline `onclick="setWatchActive('…36" wide…')"` had its attribute closed early by the stray
  `"`, so Deactivate (and Delete/Start) silently did nothing on that watch. The name is now escaped
  for BOTH layers it passes through — JS single-quoted string (`\`, `'`) then HTML attribute (`&`,
  `"`) — so any name works. (Latent since the original flat table; surfaced by the new sections.)
  Verified headless: clicking the rendered Deactivate button on the `36"` refrigerator watch
  transfers it Active→Inactive and back, counts update, config persists, no console errors.

---

## [0.14.4-alpha] — 2026-06-28 (Live-run round 3 — cross-watch matching + The Watcher gets its context back)

Two things from the live run: a good find surfaced by the "wrong" watch was being lost, and
The Watcher kept misunderstanding what it was actually searching for.

### Added
- **Cross-watch matching.** Every listing is stored once globally, but a match verdict is recorded
  per watch — so a Corvette the 4x4-truck watch loaded while scrolling was real inventory the user
  wants that just got surfaced by the wrong watch, and was lost until the sports-car watch happened
  to find it itself. Now, after a sweep alerts on its own fresh finds, those listings are run against
  the user's OTHER continuous watches' criteria; a match is recorded and alerted under that watch,
  provenance noted (`cross-watch: surfaced by '<source>'`). Bounded + safe: only other enabled,
  continuous, **primed** watches with a `judgment_prompt`; only listings that watch hasn't seen
  (capped per sweep); every candidate marked seen afterwards so nothing is re-judged each sweep. New
  config flag `cross_watch_matching` (default **on**; set false to keep watches fully independent).
  (`scheduler.py`: `_cross_watch_match`; `config.py`; tests in `tests/test_scheduler.py`.)

### Fixed
- **The Watcher now knows what it's searching for.** In the live chat it read "what are our search
  terms?" as a question about *terms of service*, and "what cars are we looking for?" made it run a
  database lookup instead of just answering. Root cause: its context fed it raw URLs
  (`…?query=Miata`) and it had to mentally decode each one. The watches context now includes a plain
  `search terms (N): …` line per watch (decoded + de-duplicated from the URLs), and the prompt tells
  it to answer term/"what are we looking for" questions directly from that line — no `listing_query`,
  no spurious update. Verified live: both questions now answer correctly. (`dashboard/server.py`:
  `_watch_search_terms`, `_build_watches_context`; tests in `tests/test_dashboard_turn.py`.)

---

## [0.14.3-alpha] — 2026-06-25 (Live-run round 2 — chat reliability, scroll depth, honest badge)

More fixes from live use.

### Fixed
- **No more "garbled gook" in the chat after editing a watch.** The local model sometimes returns
  a watch object at the TOP LEVEL (no `{"message", "watch_suggestion"}` envelope); the old code then
  dumped the raw JSON string into the chat bubble. New `_normalize_turn` repairs the shape before
  rendering: a bare watch object is adopted as a suggestion with a synthesized message, and a missing
  message is always filled in — the chat never shows raw JSON again. (`dashboard/server.py`; tests in
  `tests/test_dashboard_turn.py`.)
- **Multi-watch updates aren't silently dropped.** When asked to change BOTH watches the model emits
  `watch_suggestion_2` (/_3/…), which the UI ignored — only one watch updated. The turn now collects
  every `watch_suggestion*` into `watch_suggestions`, expands each one's search terms, and the dock
  renders a card per watch. (`dashboard/server.py`, `index.html`.)
- **Sweeps now scroll to the bottom of the results page.** `human_scroll` did a fixed 4 passes
  regardless of page length, leaving long results pages half-read. It's now adaptive: it scrolls in
  human-paced bursts until the page stops growing (bottom reached), with a minimum effort and a safety
  cap (≤14 passes) so it never scrolls forever. (`monitor.py`.)
- **The launcher badge stopped crying wolf.** It counted ALL narration (including routine
  "checking X next" decisions and periodic reviews), so a number appeared even when nothing new was
  worth seeing. It now counts only **finds and concerns**; and opening the dock shows a short
  "Since you last looked: …" recap of those events, so a badge always corresponds to something you
  can actually see. (`index.html`: `_NOTEWORTHY_KINDS`, `_showUnseenNarration`.)

---

## [0.14.2-alpha] — 2026-06-24 (Search variety & term control)

The watches felt like they searched the same things every sweep. Root causes + fixes:

### Fixed
- **Craigslist searches now actually vary their sort/filter each sweep.** `vary_search` rotated
  the sort order for eBay and Facebook but Craigslist (the main site in use) fell into the generic
  branch that only appended a cache-buster — so every sweep was the same term in the same default
  order. Craigslist now rotates **newest → relevant → price↑ → price↓** and bundles duplicate posts
  on alternating sweeps, so each search term surfaces a different slice of inventory. (`monitor.py`.)
- **"Change / broaden the terms" no longer returns the same cached set.** `expand_search_terms` is
  cache-first (keyed by the watch's instruction), so re-running it on the same watch always returned
  the identical terms — asking The Watcher to change them did nothing. The explicit-change path now
  passes `force=True` (bypass + **replace** the cache) and `avoid=<current terms>` so the model
  returns a genuinely different, broader set. (`search_terms.py`, `storage.save_term_expansion`
  gained `replace=`.)

### Added
- **Sort control on the Results view.** A "Sort" dropdown lets you order found listings by
  Newest found, **Matches first**, Price (low→high / high→low), **Price per mile (best value)**,
  Year (newest / oldest), or Mileage (lowest). Sorting is instant (client-side over the
  already-loaded rows, no refetch); listings missing that value always sort last so blanks don't
  crowd the top, and "Matches first" falls back to newest within each group. (`index.html`:
  `res-sort`, `_sortResults`, `rerenderResults`.)
- **Editable "Search terms" field in the watch editor.** A friendly comma-separated field (e.g.
  `4x4 truck, lifted truck, diesel 4x4, Tacoma 4wd`) sits under the URL. It prefills from the
  watch's existing search URLs and, on save, rebuilds one URL per term — no need to hand-edit raw
  URLs. Each term is still explored several ways per sweep by the sort/filter rotation above.
  (`index.html`: `f-terms`, `wwBuildSearchUrls`/`wwExtractTerms`.)
- **The Watcher can set exact, user-dictated terms.** The `broaden_terms` action now accepts an
  optional `terms` list/string; when present it sets exactly those (skipping the model), so a chat
  request like "search for X, Y, Z" can be applied directly. Otherwise it auto-refreshes a fresh set.
  (`_action_broaden_terms`, `POST /api/oversight/action`.)

---

## [0.14.1-alpha] — 2026-06-24 (Live-run polish — control, thumbnails, accessibility)

First round of fixes from the user's live orchestrator run.

### Added
- **Start / Stop control for The Watcher — on the dock AND in the Watches panel.** Previously the
  only way to start the orchestrator was the Settings toggle, which kicked off runs the instant you
  flipped it. There's now a **"▶ Start watching" / "⏸ Stop watching"** button in two reachable spots:
  the omnipresent dock header, and The Watcher panel above the watch list. All three controls (both
  buttons + the Settings toggle) are one synced state via `_applyOrchState`; orchestrator run-state
  now refreshes on every tab (the 8s poll), not just Settings. (`toggleWatcherRun`, `dock-run-btn`,
  `watches-run-btn`.)
- **Result thumbnails.** Listing cards in the Results tab now show a photo after the price. The
  card extractor captures the rendered image URL (`img.currentSrc`/`src`/`data-src`, https-only,
  ignores data:/svg placeholders); it flows through `Listing.image` → a new `listings.image` column
  (additive migration — `ALTER TABLE` on existing DBs) → `upsert_listing` → the Results render.
  Cards with no usable image just omit it (graceful `onerror` hide). (`monitor.py`, `storage.py`,
  `scheduler.py`, `index.html`.)

### Changed
- **Default text size is now the largest (xlarge).** The app ships to users who find small text
  hard to read, so a fresh install starts at the biggest size. A saved preference (any size set via
  the A buttons) always wins on return visits, so existing machines are unaffected. (`loadAccessibilitySettings`.)

### Fixed
- **UI preferences now persist across restarts (text size, etc.).** The native window
  (pywebview) ran in its default `private_mode=True`, which discards `localStorage`/cookies on
  exit — so the chosen text size reset to the default every launch. Now started with
  `private_mode=False` and a fixed `storage_path` (`data/webview/`), so all browser-side prefs
  survive a restart. (`main.py`.)
- **Accessibility text-size no longer breaks a maximized window.** Two root causes:
  (1) `setFontSize` resized the *native* OS window on every change, which fought a maximized window —
  removed that coupling (scaling is now purely visual; the content area scrolls). Dropped the unused
  `resize_window` pywebview bridge from `main.py`.
  (2) CSS `zoom` scales `px` but leaves `vh`/`vw` unscaled, so fixed/overlay elements (splash, the
  Watcher dock, modals) overflowed the viewport when zoomed — worst when maximized (large viewport).
  Every viewport unit on those elements is now counter-scaled by `/var(--zoom)`, matching the body.
  Verified headless at 1920×1080 + xlarge: dock and modal stay within the viewport, no page overflow.
- **Less alarming log when you edit a watch while The Watcher drives.** Saving a watch reloads the
  scheduler, which logged "0 continuous watch(es) running" — true for the scheduler's own threads but
  scary, since the orchestrator was still driving them. `reload_scheduler` now adds a clarifying line:
  "The Watcher (orchestrator) is driving N continuous watch(es) and will apply the change on its next
  cycle." (No behavior change — the orchestrator already picked up edits on its next cycle.)

---

## [0.14.0-alpha] — 2026-06-24 (The Orchestrator — The Watcher drives)

### Added
- **The Watcher can now RUN your watches, not just narrate them** — the north-star consolidation,
  Phase 1. New `web_watcher/orchestrator.py`: a single driver (one daemon thread + one shared
  browser) that cycles through your active topics like one person shopping, instead of a
  thread-and-browser per watch.
  - **Attention policy = staleness + productivity** (user's choice): it services whichever watch
    has gone longest unchecked, nudged by how often each actually finds things, with a little jitter
    so it's human-like — and never revisits a site before its own idle floor. No LLM call per cycle,
    so it doesn't compete with judging/chat for the one local GPU. (`_pick_next`.)
  - **It reuses the exact existing pipeline** — `_run_continuous_sweep` / `_run_agent_continuous_sweep`
    → dedup → deep-read → judge → alert → the listing store. Only *who decides what to look at and
    when* changed.
  - **The Watcher is its voice:** every decision is narrated through `OversightAgent.note()`
    ("Checking 'Trucks' next — longest since I looked"), so you see its reasoning in the dock and can
    chat with it while it works.
  - **Opt-in and coexists** (user's choice): a Settings toggle, **"Let The Watcher run my watches."**
    While ON it's the single driver (per-watch continuous loops stand down; `start_continuous` becomes
    a no-op so a site is never swept twice); turn it OFF and per-watch Start/Stop works exactly as
    before. Schedule-mode watches are never touched. Oversight treats continuous watches as "running"
    while the orchestrator owns them (no false "stopped").
  - `ServiceManager` owns it (`start_orchestrator`/`stop_orchestrator`/`orchestrator_running`/
    `orchestrator_status`, stopped before the scheduler on shutdown). New `GET /api/orchestrator` +
    `POST /api/orchestrator/start|stop`. Tests: `tests/test_orchestrator.py` (4) + an oversight
    `note()` test. Verified headless (toggle Off→On→"currently checking"→Off, no errors).

---

## [0.13.0-alpha] — 2026-06-24 (The Watcher, everywhere)

### Added
- **The Watcher is now omnipresent (Step 2 of the unify).** A floating launcher orb sits in the
  corner of *every* view; clicking it opens the single Watcher chat dock — one conversation, one
  memory, reachable from Watches, Results, Activity, and Settings alike.
  - The launcher **pulses while watching** and shows an **unread badge** when new narration arrives
    while the dock is closed (so The Watcher can get your attention from any view). Polling now runs
    on every tab, not just Watches.
  - The dock's conversation **loads lazily** the first time you open it (history, or a first-run
    greeting from `/api/summary`).
  - The Watches home is now feed-focused: the ambient narration + a slim **"💬 Ask The Watcher"**
    button that opens the same dock (no more embedded chat box crowding the watch list).

### Removed
- **Deleted the dormant Assistant code** now that The Watcher is the one AI: the old `#tab-assistant`
  view and its chat CSS, the `sendChat`/`loadChatHistory`/`showLaunchGreeting`/`clearChat` JS and the
  `chatHistory` state, and the server's `POST /api/chat` + `GET|DELETE /api/chat/history` endpoints
  plus `_load_chat_history`/`_save_chat_history`/`_CHAT_HISTORY_PATH`. The shared
  `_build_watches_context`/`_complete_assistant_turn` helpers stay — they power The Watcher's chat.

### Fixed
- `timeAgo` clamps to "just now" for sub-5s/negative deltas (guards minor client/server clock skew on
  a freshly-emitted narration line).

---

## [0.12.1-alpha] — 2026-06-24

### Changed
- **Returning to the Watches tab now shows The Watcher resting** (feed + "💬 Talk to The Watcher"
  trigger) instead of the chat left hanging open from before — `showTab('watches')` collapses the
  chat on arrival. The conversation is preserved behind the trigger (re-open to resume).

---

## [0.12.0-alpha] — 2026-06-23 (one AI: "The Watcher")

### Changed
- **Unified the Assistant and the oversight agent into ONE named AI — "The Watcher" (Step 1 of
  the UI-design direction the user chose: "one AI, contextual modes").** They were always the
  same brain (shared turn engine); now they're one identity:
  - **Removed the separate "Assistant" tab.** Nav is now Watches · Results · Activity · Settings —
    tabs are data views; the AI is not one of them.
  - **The Watcher lives in the Watches home** as the single AI: ambient "watching" feed + a chat
    with full assistant powers (create/edit/manage watches, look up listings). It **greets you on
    arrival** (first-run intro, or a status update built from `/api/summary`) and **loads its own
    conversation history** (`/api/oversight/chat/history`) on boot via `loadWatcherHistory()`/
    `showWatcherGreeting()`.
  - Tone kept deliberately warm/helpful so the "guardian" name reads as on-your-side, not cold.
  - NOTE: the old Assistant tab DOM + its `/api/chat` chat functions remain dormant (unreachable,
    not loaded) — they'll be deleted in Step 2 along with making The Watcher omnipresent (a dock/
    launcher on the other views).

---

## [0.11.1-alpha] — 2026-06-23 (live-test fixes)

### Fixed
- **The Watcher now reacts to a watch starting/stopping within a second or two** instead of
  waiting out its ~75s idle. Added `OversightAgent.nudge()` (wakes the loop early via a
  `_wake` event; the idle is now interruptible); `ServiceManager.start_continuous`/
  `stop_continuous` nudge it. First look shortened to ~3s. (Live test: the feed used to say
  "no watches running" for a minute or two after you pressed Start.)
- **Less GPU contention / fewer long pauses:** the Watcher's periodic spoken review now uses
  the LIGHT `text_model`, not the heavy council model — narration phrasing doesn't need the
  14B, and keeping it off the council model leaves the one local GPU free for the sweep judge
  and the assistant/Watcher chat (the actual source of the stalls).

### Changed
- **Watches is now the home tab** (first in the nav, active on load); the Assistant moved to
  second. Watches + The Watcher load on boot.
- **The Watcher chat collapses.** It tucks the conversation away after ~30s of inactivity (or
  via the ▾ button), leaving just the feed and a "💬 Talk to The Watcher" trigger — so it
  stops crowding the watch list after you've used it. Re-opening preserves the conversation.

---

## [0.11.0-alpha] — 2026-06-23

### Added
- **You can talk to The Watcher now — and its concerns are one-click fixes.** The oversight
  agent stopped being read-only:
  - **Chat with The Watcher** in the Watches tab. It's the *same brain* as the main assistant
    (create/edit watches, start/stop/enable/disable/delete, look up found listings) but in
    oversight mode — it leads with what it's actually been observing (its own recent narration +
    each watch's health) instead of generic answers. New `POST /api/oversight/chat` with its own
    persona (`_WATCHER_SYSTEM`) and separate history; conversation renders in a panel beside the feed.
  - **One-click fixes.** When The Watcher flags a watch that's matched nothing, the concern now
    carries an action — a "Broaden its search terms" button that runs the search-term expansion
    engine, rebuilds the watch's URLs, and reloads it. New `POST /api/oversight/action`
    (`broaden_terms`); oversight `concern` entries carry an optional `action`.
  - The assistant turn machinery (`_build_watches_context`, `_complete_assistant_turn`) and the
    chat-render functions (`appendMsg`/`appendWatchCard`/`appendListingResults`/`appendWatchActionsCard`,
    now container-parameterized) are shared between the main assistant and The Watcher — no
    duplicated logic, full capability parity.
  - This is step toward the orchestrator: the oversight brain now *acts*, not just narrates.
    Tests: `tests/test_oversight.py` (9). Verified headless (chat reply + broaden action end-to-end).

### Changed
- Refactored `/api/chat` to use the shared `_build_watches_context` + `_complete_assistant_turn`
  helpers (behavior identical; the main assistant path is unchanged).

---

## [0.10.0-alpha] — 2026-06-23

### Added
- **The Watcher — a visible oversight agent (first slice of the oversight-agent vision).**
  A background "mind" now watches the watches and narrates what it sees, in the first person,
  live in the Watches tab. It ticks on its own cadence (~75s), diffs each look against the last,
  and speaks up about *events* rather than streaming status:
  - **find** — "'4x4 Trucks' just turned up 2 new matches. (8 total now.)"
  - **concern** — flags a watch that's looked at many listings but matched none (the search-terms-too-literal
    case), once, and nudges you to broaden it; also surfaces a watch's last-run errors.
  - **decision / status** — notes when a watch starts or stops.
  - **review** — a periodic spoken check-in; templated by default, with a best-effort local-LLM
    "voice" pass (short timeout, falls back to the template so the panel never stalls on the 14B).
  - New `web_watcher/oversight.py` (`OversightAgent`: thread + delta detection + capped narration
    buffer + thread-safe `snapshot()`), owned by `ServiceManager` (starts with the scheduler, stops
    before it). New `GET /api/oversight`. New Watches-tab panel ("THE WATCHER") with a pulsing orb
    (live/asleep) and a color-coded feed that auto-refreshes while the tab is open.
  - `storage.count_observations(watch_id)` — the data-quality signal paired with `count_matches`.
  - This is the orchestrator north-star made visible + conversational first: the same brain that
    will later *drive* the watches currently *comments* on them. Tests: `tests/test_oversight.py` (8).

---

## [0.9.0-alpha] — 2026-06-23

### Fixed
- **Editing a watch orphaned all its data.** The update endpoint rebuilt the watch from a
  body that didn't carry its stable `id`, so a fresh id got assigned on next load —
  orphaning every stored listing/observation (the Craigslist watch's results vanished from
  its filter while still showing under "All"). Update now preserves the existing id;
  existing orphaned data was re-pointed.
- **Results watch dropdown didn't update** when you added a watch — it populated only once.
  It now refreshes every load (preserving your selection).

### Added
- **Smart search-term expansion (the big one).** A watch that searches a literal phrase
  often misses the target — "sports car" on Craigslist returns SUVs ("Sport Utility"),
  finding 234 listings and 0 real matches. The local model won't reliably expand terms
  inline (verified), so this is now a **focused, single-purpose LLM call** (`search_terms.py`)
  that turns intent → several effective search terms (synonyms, types, common phrasings,
  misspellings) and builds one search URL per term. Results are cached in a **learning
  store** (`term_expansions` table) that grows over time (unions new terms) and is reused
  instantly. When the assistant proposes a continuous marketplace watch, the backend
  auto-expands its search and tells you the terms it'll use. Falls back safely (page/feed
  watches and non-search URLs are untouched).
- **Results are now general, not vehicle-specific.** Replaced the transmission/drivetrain
  dropdowns with a free-text **search** over title + ad body (`q` param → `query_listings(text=)`),
  so the Results view (and the assistant's `listing_query`) works for ANY kind of item, not
  just cars. Each result now shows **which watch(es) found it** (`watches` column), so you can
  tell results apart in the "All" view.
- **Anti-bot pacing on deep-read.** The ad-reading step no longer machine-guns tabs
  open/closed — it pauses 1.5–4s between listings and 0.8–2s "reading" each before closing,
  so it looks like a person, not a scraper (important for Facebook's bot detection).

### Added
- **Phase 4 (first pass) — chat-first launch.** The app now opens straight to the
  **Assistant** (moved to the first nav slot), which makes the first move: a friendly
  status update built from a new `GET /api/summary` — "Welcome back. You have N watches
  (M running). They've found X matches so far. ⚠ K recent errors…" — or an intro on first
  run. A **splash screen** (logo + spinner) covers startup and fades once services
  respond. The old **Services** tab is reframed as **Settings** (moved to the end). All
  verified by rendering headless. (Folding History/Notifications/Results together is left
  as later polish — they're distinct enough to stay separate for now.)

### Fixed
- **Craigslist "Xhr ago" timestamps were corrupting titles and breaking dedup.** CL puts a
  relative time ("<1hr ago", "2h ago", "35 mins ago") in each card; the noise-stripping
  only handled single-letter `h`, so the timestamp leaked into the title AND the
  fingerprint — making one listing (e.g. a 2008 F450) look like 8 different ones as the
  "ago" ticked up. Now a robust relative-time pattern strips it from both titles (cut at
  the timestamp) and fingerprints, so re-sightings collapse to one listing. (Existing
  duplicates remain; future sweeps dedup correctly.)
- **Assistant now reliably applies filters.** Asking the assistant for "manuals" returned
  automatics because the model wasn't translating words into filters. The prompt now
  insists: "manual" → transmission, "4x4" → drivetrain, "under $8k" → max_price, etc.
- Chat token/time stats reordered to tokens · time · speed (Claude-style).

### Changed
- Assistant tone is now explicitly conversational (warm, natural, reacts to the user)
  instead of terse — the "message" field is meant to read human.
- **Tabs consolidated.** Results stays the headline ("what was found"). History +
  Notifications merged into one **Activity** tab: the run log, where clicking a run shows
  that run's alert preview inline (Notifications was always just the preview of a run).
  Nav is now Assistant · Watches · Results · Activity · Settings. Verified headless.

### Fixed
- Links (listing titles in the Results tab and chat) were unreadable — there was no
  anchor style, so they fell back to the browser's default dark blue on the dark theme.
  Added a readable link color (`--blue`) with hover underline and a visited shade.

---

## [0.8.0-alpha] — 2026-06-23

### Changed
- **Phase 3 — simpler watch form (smart defaults + Advanced drawer).** The Add/Edit Watch
  modal now leads with just the essentials — Name, What to watch (URL), What to look for,
  How often, Notifications, Enabled — and tucks the implementation details (the AI-click
  toggle + max steps, perception, continuous tuning, cron, model override) into the
  Advanced drawer. Clearer labels throughout (e.g. "Let the AI click & explore — slower,
  for sites that need interaction. Off = fast scraper"). All field IDs preserved so
  save/edit/mode-toggle logic is unchanged; verified by rendering the modal headless.

### Added
- **Results tab — scroll through everything that's been found.** A new dashboard tab
  renders the listing store (via `GET /api/listings`) as a filterable list: pick a watch,
  transmission, drivetrain, max price, or "matches only", and see each listing with
  price/year/mileage/attrs, a **match** badge, a **×N dupes** badge, the judge's reason,
  and a link to the ad. This surfaces all the data-layer work in the UI (not just chat);
  Phase 4 will fold it into the larger redesign.

### Changed (hardening)
- **SQLite concurrency.** `_connect` now opens in WAL mode with a busy-timeout, so the
  dashboard/assistant can READ while a continuous sweep WRITES (the data layer does many
  writes per sweep) instead of risking "database is locked".
- The assistant's per-watch health gathering is now defensive — a DB/scheduler hiccup
  degrades to "(unavailable)" rather than failing the whole chat response.

---

## [0.7.0-alpha] — 2026-06-23

### Added
- **Assistant-as-manager: review + lifecycle control.** The chat assistant can now manage
  watches, not just create/edit them. Each watch in its context carries a **health line**
  (enabled/disabled, running/stopped, last-run result or ERROR, matches found), so it can
  answer "how are my watches doing?" and suggest fixes for ones that error / find nothing.
  It can also propose **lifecycle actions** — `watch_actions: [{action, name}]` with
  delete / enable / disable / start / stop, including **bulk** ("delete all but the truck
  watch") — which the UI shows as a **confirmation card** (nothing runs until you click
  Confirm; delete is styled destructive). The backend drops actions that name a
  non-existent watch (so a hallucinated name can't fire). New `POST /api/watches/{name}/enabled`
  endpoint; `storage.count_matches`. This closes the lifecycle gap from when "delete all
  but Craigslist" silently did nothing. Verified live: bulk-delete and review both work.
- **Ask the assistant what's been found (listing queries).** The chat assistant can now
  query the listing store: ask "what manual 4x4 trucks under $8k have shown up?" or "show
  the matches from my truck watch" and it emits a structured `listing_query` (watch,
  matched_only, transmission, drivetrain, min_year, max_price, max_mileage), which the
  backend runs via `query_listings` and renders as a result list in chat — each row with
  price/year/mileage/transmission/drivetrain, a **match** badge, and a **×N dupes** badge
  (from dup_count). New `GET /api/listings` endpoint exposes the same query for the future
  Results view. Verified live: the model emits the right filters and correctly uses a
  watch-suggestion (not a query) for "create a watch …".
- **Repost / duplicate detection — group + note, never hide.** Each listing gets a
  normalized content `fingerprint` (year + price + cleaned title, with volatile bits like
  "1h ago" and the mileage badge stripped). A listing with a NEW id but the same
  fingerprint AND same source as one the watch already surfaced is a re-post of the same
  item. Per the guiding rule — *rather see a dup than miss real content* — it is NOT
  hidden: it's recorded, **linked to the original** (observation reason "duplicate of …"),
  and **inherits the original's match verdict** so a real match is never dropped; we only
  skip the redundant deep-read + re-alert. `query_listings` now returns a **`dup_count`**
  so the dups can be *noted on* the listing. Matching is deliberately conservative —
  **same-source only** (we never merge across sites), so two different listings are kept
  separate rather than wrongly collapsed. `listings.fingerprint` column (+ index +
  migration), `storage.find_duplicate`. Cross-site/fuzzier matching is a later enhancement.

### Fixed
- **Listing titles no longer start with junk.** Craigslist's image-carousel dots
  ("• • • • …", U+2022) were captured as the start of the card title; `extract_listings`
  now strips leading bullet/separator glyphs. Cleaner titles also make the new content
  fingerprint more reliable.

---

## [0.6.0-alpha] — 2026-06-23

### Added

**Phase 2 data layer — listing-centric store (results persist OUTSIDE the watch)**
- New global `listings` table (SQLite): one row per real-world listing, keyed by stable
  `source:native_id`, deduped ACROSS watches. Stores url/title/price, the raw deep-read
  `details`, and parsed attribute columns (price_value, year, mileage, transmission,
  drivetrain) for filtering/sorting. `upsert_listing` preserves richer prior data on a
  cheap re-sighting (won't wipe details/attributes, bumps last_seen).
- New `observations` table linking watch ⇄ listing per (watch, listing): matched verdict
  + judge reason + first/last seen, keyed by a STABLE watch id so renames/deletes don't
  orphan history. A listing seen by several watches = one listing row + N observations.
- **Watches now have a stable `id`** (config.py): assigned and persisted on first load
  (one-time migration); the data layer keys observations by it, not the mutable name.
- `monitor.parse_listing_attributes` pulls price/year/mileage/transmission/drivetrain out
  of a listing's title + ad body (Craigslist's explicit `transmission:`/`drive:`/
  `odometer:` lines, with keyword fallbacks).
- `storage.query_listings` filters the store by attribute (transmission, drivetrain,
  min_year, max_price, max_mileage) and by watch/matched — the query surface for the
  coming assistant-as-manager and Results view.
- The continuous sweep now persists every new listing (with attributes + match verdict)
  on priming, flood, and normal paths. Verified live on Craigslist: real listings →
  deep-read → parsed attributes → queryable store ("manual transmission only" filter and
  per-watch matched counts both correct).

**Humanized scraper**
- **Humanized scraper.** The fast (non-agent) sweep now acts like a person when stealth
  is on: instead of jumping straight to a query URL, `monitor.humanized_search` lands on
  the search page, moves the mouse to the search box, TYPES the query with real,
  human-paced key events, and presses Enter (verifying/correcting the value, since
  Craigslist swallows the first keystroke). So you get keyboard + mouse + human scrolling
  + real-Chrome stealth — without the LLM agent. Falls back to a direct navigation when
  there's no search term, no search box, or stealth is off. Verified live on Craigslist.

### Changed
- **Deep-read decoupled from the agent; scraper is now the default for clean sites.**
  The fast (non-agent) continuous sweep now also deep-reads each new ad's attributes
  (when a judgment_prompt is set) — so Craigslist/eBay get instant scraping PLUS
  attribute filtering (manual transmission, 4x4, mileage) without the slow, thrash-prone
  agent. Both sweep paths gate deep-read on `bool(watch.judgment_prompt)` and share the
  same `_process_sweep_listings`. The AI agent (`autonomous: true`) is now reserved for
  login-/interaction-gated sites (e.g. Facebook logged in) that lack a usable URL; the
  assistant's guidance was updated to recommend `autonomous: false` for clean-URL sites.
  (The Craigslist truck watch was switched to `autonomous: false`.)

### Fixed
- **Agent can finish a continuous sweep instead of thrashing.** The premature-done
  guard (built for schedule-mode: search→navigate→gather→done) rejected EVERY 'done'
  in a scroll-only continuous sweep, because scrolling only changes the URL hash so it
  always looked "still on the start page" — the agent burned all 20 steps alternating
  scroll/rejected-done. `run_agent` gained `exploration_mode`: in the continuous sweep
  it may finish after a couple of scrolls (its only job is to load listings; reading is
  done afterward by the deep-read).
- **Agent stopped repeating the same search.** It would re-type the identical query
  ("4x4 truck") over and over, thrashing without progress. `run_agent` now tracks
  submitted search terms and rejects a repeat with feedback to scroll or finish.
- **Simpler, clearer agent job.** Now that the system deep-reads ad pages itself, the
  agent no longer needs to open listings. Its exploration instruction is now: scroll
  to load as many listings as possible; run a search only once per *different* term;
  finish after good coverage. (Removed the "open and read each listing" guidance the
  model kept fumbling.)
- Deep-read fetch failures are logged at WARNING with the exception (were swallowed at
  debug), so a systematic "0/N deep-read" is diagnosable from the session log.

---

## [0.5.0-alpha] — 2026-06-22

### Added

**Deep-read: the watch opens ads and reads them (so it can filter on what's IN the ad)**
- After an agent sweep harvests listing cards, the system now deep-reads each NEW
  listing: it opens the ad page in a background tab (`page.context.new_page()` — the
  agent's own page keeps its place; the agent doesn't manage tabs), pulls the body +
  structured attributes via `monitor.extract_listing_body`, and stores them on
  `Listing.details`. Sequential, capped at `_MAX_BODY_FETCH` (12) new listings per
  sweep, and honours the stop signal.
- The judgment filter now feeds those AD DETAILS to the judge, so a `judgment_prompt`
  can match on transmission (manual/auto), drivetrain (4x4/4wd), mileage, condition,
  etc. — things that live in the ad body, not the card title. Verified live on
  Craigslist: a listing yielded `drive: 4wd · odometer: 154,172 · transmission: automatic`.
- `monitor.Listing` gained a `details` field; `monitor.extract_listing_body()` pulls
  Craigslist `#postingbody`/`.attrgroup`, eBay item specifics, and generic
  description/article containers, with a trimmed-body fallback.
- The fast scraper sweep stays cheap (no deep-read); only the agent sweep deep-reads.

### Fixed
- **"Server unreachable" after editing/deleting a watch.** create/update/delete watch
  endpoints ran `reload_scheduler()` inline; reload stops continuous watches and
  join()s their threads (up to 30s if an agent watch is mid-sweep), hanging the HTTP
  request. The reload now runs as a BackgroundTask so the request returns immediately.
- **Continuous agent watch now stops mid-browse.** The sweep's `should_stop` guard also
  trips when the loop's stop_event is set, so Stop / reload / delete halt the agent
  within a step instead of waiting out the 30s join timeout.
- **Human-like typing restored.** `_human_type` was pasting at 12 ms/key (instant — a
  bot tell). It now types key-by-key at ~100 ms with jitter and occasional brief
  pauses (a short search term lands in ~1s), bounded so it never stalls a run.
- Assistant guidance was stale: it still told the model to make continuous watches
  `autonomous: false` (the old scraper-only assumption), so a plain-English request
  would have created a dumb-scraper watch instead of the new agent-driven one. The
  assistant now defaults continuous marketplace watches to `autonomous: true`, gives
  them a search-results URL + judgment filter, and bumps max_agent_steps to ~20.
  Verified live: "watch Craigslist Seattle for 4x4 trucks all day" → a valid
  continuous, autonomous watch.

### Changed
- Agent-driven continuous sweep instruction now tells the agent to EXPLORE rather than
  skim — open promising listings, read the full ad, then go back to the results — since
  a title alone often isn't enough to judge a match. The aim is to surface candidates for
  a person to review, so it leans toward opening anything plausible. (A Craigslist Seattle
  trucks watch was added as a logged-out-friendly test bed while Facebook walls guests.)

---

## [0.4.1-alpha] — 2026-06-22

### Fixed

**Agent-driven continuous sweep safety (from a live FB run)**
- **Agent never logs in / never wanders off-site.** A logged-out category URL
  (`/marketplace/category/vehicles`) redirected to Facebook's login page; the agent
  then typed fake credentials into the form and clicked through to threads.com.
  `run_agent` gained a `should_stop` guardrail (checked at the top of every step);
  the continuous sweep passes one that halts the instant the page leaves the start
  site (`_registrable_domain`) or hits a login wall. The exploration instruction now
  hard-forbids logging in, entering any email/password/phone, and following off-site
  links, and tells the agent to scroll first rather than finish immediately.
- **Login-wall URLs are skipped, not poked.** After the initial load, a logged-out
  watch that lands on a login wall now records "skipped (login wall)" and ends the
  sweep instead of letting the agent interact with the login form.
- **Vision 400s fixed.** At large windows (e.g. 2560×1440) a screenshot exceeded
  qwen2.5vl's default 4096-token context → every vision call returned HTTP 400, so the
  agent browsed blind and looped on empty `done`. Both vision payloads now send
  `options.num_ctx = 8192`.

---

## [0.4.0-alpha] — 2026-06-22

### Added

**Agent-driven continuous watch — "browse like a person, all day" (Phase 1 of the unification)**
- A continuous watch with `autonomous: true` now lets the **agent drive** the page — scrolling,
  searching, and opening categories like a person — while the existing extractor **harvests**
  listings from every page it visits, then runs the same dedup → judge → alert pipeline. The
  pure scraper remains the `autonomous: false` fast path.
- `run_agent()` gained an `on_step` harvest hook (`agent.py`): a callback invoked on the initial
  page and after every action, used to accumulate `extract_listings` while the agent browses.
  Default `None` — schedule-mode agent runs are unchanged. Callback errors are swallowed.
- `_run_agent_continuous_sweep` + `_exploration_plan` (`scheduler.py`): each sweep picks a
  randomized human-like browsing style (scroll-heavy / category-hop / search-first), rotates the
  start URL across the watch's URLs, and jitters idle time (`_jittered_idle`) so behavior isn't
  clockwork (anti-pattern).
- `_process_sweep_listings` (`scheduler.py`): the dedup → prime → flood-guard → judge → alert →
  record pipeline is now shared by both the scraper sweep and the agent sweep, so they alert
  identically; only the gathering differs. Agent-sweep runs are tagged `continuous-agent` in
  history.

### Notes
- Next phases (not in this release): assistant-as-manager (reads logs/run history, reviews watches,
  suggests prompt/action fixes) and UI simplification (smart defaults + Advanced drawer).

---

## [0.3.0-alpha] — 2026-06-22

### Added

**Agent blocker handling**
- Overlay/popup gate in the agent loop, adjacent to the CAPTCHA gate: every step the
  agent cheaply probes for a blocking login/cookie/consent modal (`monitor.has_blocking_overlay`)
  and dismisses it (`monitor.dismiss_popups(settle_ms=0)`) before acting. Previously
  overlay dismissal lived only in the continuous sweep, so the autonomous agent got
  stuck behind Facebook's "Log in or sign up" modal.
- `monitor.dismiss_popups()` gained a `settle_ms` param (0 for the per-step agent gate,
  800 for the just-navigated continuous sweep).

**Assistant can edit + understands all modes**
- Dashboard assistant can now EDIT existing watches, not just create — suggestions carry
  `action: "create" | "update"`; the chat endpoint feeds it full existing-watch configs;
  the UI passes the WHOLE suggestion through (PUT on update).
- Assistant system prompt now covers schedule-vs-continuous mode, the Facebook Marketplace
  logged-out-feed vs logged-in-search distinction (and that the login popup is an overlay,
  not a CAPTCHA), and the full field set (mode, continuous_*, use_login_profile, judgment_prompt).

**Higher GPU tiers (ships to varied hardware)**
- `gpu_detect` tiers extended upward: 48GB+→qwen2.5:72b, 24GB→qwen2.5:32b, 16GB→qwen2.5:14b
  (16GB is no longer the ceiling). Each tier carries a `council_model` (defaults to its text_model).
- Judgment filter now logs each REJECTED listing and the judge's stated reason, so a
  surprising "kept 0/N" can be checked (genuinely off-target feed vs. an over-strict judge).

### Changed

**Continuous watch — one persistent browser, multi-URL category coverage**
- The continuous loop now owns ONE browser session reused across every sweep (reopens only
  if the user closes it or it crashes), instead of opening and closing a window each sweep —
  no more flickering windows for an always-on watch.
- Each sweep rotates through ALL of a watch's URLs (`urls[sweep_index % len(urls)]`), so
  adding several category-feed URLs gives category coverage. Was hardcoded to `urls[0]`.

**Agent recovery rewritten (was the 3-expert council)**
- Replaced the 3-persona + synthesiser council (4 sequential same-model calls, none of which
  saw the numbered element list yet had to emit an `element_index`) with ONE context-rich
  "get unstuck" pass that sees the actual indexed elements and reasons overlay→captcha→
  navigation→fallback before committing to a valid action. Faster and accurate.

### Fixed

- **Vision model tag**: tiers used `qwen2.5-vl:7b` (with a dash) — not a valid Ollama tag;
  corrected to `qwen2.5vl:7b`. Fresh installs would have failed the vision pull.
- **council_model on fresh installs**: the installer never wrote `council_model`, so new
  configs fell back to the stale `mixtral:latest` default and the council/judge/assistant
  broke. `ModelsConfig.council_model` now defaults to `""` with an `effective_council_model`
  property (falls back to text_model); the installer pulls and writes it; all consumers use
  the property. config.py defaults moved to the qwen2.5 family.
- Stale tests: config default-model assertions (expected a two-iterations-old `mistral:latest`)
  and a scheduler fixture that silently ran the live agent (defaulted `autonomous=True`) instead
  of the mocked simple path.

---

## [0.2.0-alpha] — 2026-06-21

### Added

**Autonomous browser agent**
- `run_agent()` loop: LLM-driven page interaction with up to N configurable steps (`max_agent_steps`)
- DOM snapshot with viewport awareness — elements sorted as ON SCREEN → nav → BELOW FOLD, cap raised to 60
- `inViewport` flag per element from `getBoundingClientRect()`; ON SCREEN / BELOW FOLD grouping shown to agent
- Checkbox/radio `[CHECKED]` / `[unchecked]` state and `[FOCUSED]` indicator in element list
- `autonomous` flag per watch; `max_agent_steps` field; `judgment_prompt` for post-browse verdict
- Agent scratchpad (`remember` action) persists facts across steps; surfaced to judgment step
- Judgment step: custom `judgment_prompt` applied to scratchpad + final page text via council model
- Action outcome tracking: every action records `"navigated → <url>"`, `"title changed"`, or `"page unchanged"` — shown in history so the LLM has situational awareness

**Multi-agent offline council**
- Council of three local Ollama experts (Navigation, Anti-Bot, Fallback) convenes when agent is stuck
- Synthesiser turns council discussion into a single concrete next action
- All council experts enforce offline-only constraint — no paid CAPTCHA services suggested
- Stuck-on-type special case: force-presses Enter after COUNCIL_TRIGGER identical type attempts

**Vision / OCR**
- `_describe_page()`: takes JPEG screenshot, calls llava (or any vision-capable Ollama model), returns description
- Vision fires once per URL path change (strips query params via `_url_path()`) — filter clicks don't re-trigger
- Vision description passed to LLM as `"Page layout and content (from visual scan)"` section
- OCR fallback: if DOM text snippet is below `ocr_threshold` chars and no page_context yet, vision is used for OCR
- `ocr_threshold` config field (default 200 chars) controls when OCR fallback fires
- `vision_model` config field (default `llava:latest`); `council_model` field (default `mixtral:latest`)

**Agent reliability fixes**
- Auto-focus detection: type action skips click when `document.activeElement` already matches target element
- Safe Ctrl+A: only selects field contents when `document.activeElement.tagName` is INPUT or TEXTAREA
- Non-input type guard: skips focus-click if target element is a link or button (prevents unintended navigation)
- Force-Enter re-focus: before pressing Enter on stuck-type, JS re-focuses nearest visible text input
- Stuck-type sig ignores element_index: autocomplete dropdowns change indices between steps; sig now `"type|<text>"` so COUNCIL_TRIGGER correctly fires after 3 identical search attempts
- False CAPTCHA fix: reCAPTCHA iframe check now only fires when page body is under 500 chars (prevents false positives on pages that embed tracking iframes)
- Minimal system prompt: reduced from 45 lines to ~15; removed web-usage rules that fought the model's training knowledge — only the tool contract is described

**Dashboard**
- Per-session log files in `data/logs/web_watcher_YYYYMMDD_HHMMSS.log`; oldest sessions auto-pruned (keep 30)
- Version badge next to title: plain for stable, orange pill with `vX.Y.Z-alpha` for pre-release builds
- Stealth mode toggle is now a consistent slider (matches browser-visible toggle); no longer shows a visible checkbox alongside the slider track
- Fixed `--accent` CSS variable undefined (toggle track `on` state now renders correctly in blue)
- `--orange` CSS variable added for pre-release badge

**Configuration**
- `ModelsConfig.vision_model` default changed to `llava:latest`
- `ModelsConfig.council_model` field added (default `mixtral:latest`)
- `ModelsConfig.ocr_threshold` field added (default `200`)
- `Watch.autonomous` flag, `max_agent_steps`, `judgment_prompt` fields

### Fixed
- `PageResult.screenshot` → `PageResult.screenshot_bytes` in scheduler empty-page guard (was crashing after every agent run with `AttributeError`)
- Type action log now shows both `el=<index>` and `text=<text>` so element targeting is visible in logs
- Agent action detail log was showing only text OR element for type actions — now shows both

---

## [0.1.0] — 2026-06-20

Initial release. All core features built and tested (128 tests, all passing).

### Added

**Configuration**
- Pydantic v2 schema for the full config (`config.yaml`) — notifications, models, watches, click-paths
- Validation: `interval_minutes` XOR `cron_expression`, perception mode enum, min 1 URL per watch
- `load()` / `save()` / `round_trip()` helpers; YAML round-trip preserves comments

**Browser automation**
- Playwright (sync API) browser session with headless Chromium
- Click-path support: `click`, `select`, `scroll`, `wait_for_selector`, `wait_ms` actions
- Flexible locator (`text=`, `role=` prefixes, CSS/XPath fallback)
- Per-URL and per-step error isolation — one bad URL doesn't abort the whole watch

**Perception layer**
- `auto` mode: extract text → usability heuristic → vision fallback if needed
- Text usability check: length gate, 9 JS-required patterns, loading-placeholder detection
- `text` / `vision` modes available for manual override per watch

**Reasoning (local LLM)**
- Ollama API integration (`POST /api/chat`) with `format:"json"` for syntax reliability
- Two-attempt JSON repair/retry loop; schema validation separate from parsing
- Centre-truncation of long pages (`MAX_TEXT_CHARS = 12 000`)
- `OllamaUnavailableError` for clean error propagation
- Vision path via multi-modal Ollama models (moondream, llava, qwen2.5-vl)

**Notifications**
- Telegram: HTML-formatted message via Bot API; optional screenshot attachment (`sendPhoto`)
- Email: STARTTLS SMTP, multipart alternative (HTML + plain text), PNG screenshot attachment
- `send_notifications()` fires both channels independently; partial success is not an error

**Storage**
- SQLite `run_history` table with index on `(watch_name, run_timestamp)`
- `save_run()`, `get_history()`, `get_last_run()` — missing DB returns empty/None gracefully
- Screenshots saved to `data/screenshots/{watch}_{timestamp}.png` on vision-path matches

**Scheduler**
- APScheduler `BackgroundScheduler` with `ThreadPoolExecutor(max_workers=4)`
- Per-watch `interval_minutes` or arbitrary `cron_expression`
- `coalesce=True`, `max_instances=1`, `misfire_grace_time=120`
- `run_now()` for immediate manual trigger; `reload()` after config changes

**Dashboard (FastAPI + pywebview)**
- REST API: health, status, service control, watch CRUD, history, schedule
- Service management: Ollama, Dashboard Server, Scheduler — start / stop / restart each independently
- Ollama adopt-vs-start: inherits a running instance rather than spawning a duplicate; only terminates what it started
- Watch CRUD propagates immediately to the scheduler via `reload()`
- Tabbed SPA (Services | Watches | History | Assistant) — dark theme, no external CDN dependencies
- Add/Edit watch modal with advanced toggle (cron expression, model override)
- Run history table with watch filter, found/confidence/summary columns
- AI Assistant tab backed by local Ollama; suggests and can create watches directly from conversation
- Native app window via pywebview (1150×780, min 900×600); closes all services on window close
- Auto-starts all services on app open
- `Shutdown All` button for graceful teardown from the UI
- Browser visibility toggle and action speed control in Services tab
- Version badge in header fetched from `/api/health`

**GPU detection & installer**
- `gpu_detect.py`: VRAM probe via `nvidia-smi`, WMI/PowerShell fallback
- Tier selection: 16 GB+ → qwen2.5:14b + qwen2.5-vl:7b; 8–12 GB → qwen2.5:7b + llava:7b; 6 GB → qwen2.5:7b-q4_K_M + moondream; CPU → qwen2.5:3b + vision disabled
- `TierInfo.fallback()` for one-step demotion; validation via timed test inference call
- `install.py`: step-by-step installer — checks Python, installs deps, checks/installs Ollama, detects GPU, pulls models (background process + live progress), validates inference, installs Playwright Chromium, writes `config.yaml`, prompts for credentials, optional Windows startup registration
- `install.bat`: Windows launcher; auto-installs Python 3.12 via winget if absent
- `install.sh`: Linux/macOS shell wrapper

**Developer experience**
- 128 offline tests across all modules (pytest); live tests gated behind `-m live`
- `start.bat`: double-click launcher
- Desktop shortcut created by installer

### Architecture notes
- All config writes call `manager.reload_scheduler()` — no restart needed for watch changes
- `_execute_watch` is module-level (not a method) to satisfy APScheduler serialization
- FastAPI `BackgroundTasks` used for service-control endpoints so the response is returned before the action runs (critical for "restart server")
- Port 7878 for dashboard; Ollama on default 11434

[Unreleased]: https://github.com/your-org/web-watcher/compare/v0.2.0-alpha...HEAD
[0.2.0-alpha]: https://github.com/your-org/web-watcher/compare/v0.1.0...v0.2.0-alpha
[0.1.0]: https://github.com/your-org/web-watcher/releases/tag/v0.1.0
