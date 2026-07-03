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

sha256: 19b4e33bb6631eb6b1dbab7cf23659502196a9ac77af94b08e3828165a009ec9
