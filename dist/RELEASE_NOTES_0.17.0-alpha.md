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

sha256: ec87e51cab6766ebdfe355aba905b4d2792f7678b99e7f16be0a2b109c63806d
