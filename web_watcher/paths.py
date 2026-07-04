r"""
Central filesystem locations for all Web Watcher user data.

Every piece of persistent user state — config.yaml, the SQLite history DB, screenshots,
session logs, the browser storage-state + Chrome login profile, the webview cache, and the
Watcher chat/history JSON — lives under a SINGLE per-user data root resolved once here. This
keeps user data completely separate from the installed code so an install/update can swap the
`web_watcher/` package without ever touching a watch, result, or saved login.

Resolution order for the data root (first that applies wins):
  1. $WW_DATA_DIR                     explicit override (tests, portable/second installs)
  2. %LOCALAPPDATA%\WebWatcher        Windows default
  3. ~/.web-watcher                   non-Windows fallback

Legacy layout (pre-relocation alpha builds) kept data in the repo: `<app>/data/...` plus
`<app>/config.yaml`. On first use we migrate that once into the new root, COPYING (not moving)
so the originals remain as a backup, and drop a `.migrated` marker so it never repeats.

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  data_dir            ~L60   Resolve + create the data root (runs migration once)
  config_path         ~L95   <root>/config.yaml
  db_path / screenshots_dir / log_dir / webview_dir   ~L100
  browser_state_path / profile_dir                    ~L120
  watcher_history_path                                ~L130
  _migrate_legacy     ~L140  One-time copy of in-repo data/ + config.yaml (keeps backup)
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

# The installed app folder (holds web_watcher/) — also where legacy data used to live.
APP_ROOT = Path(__file__).resolve().parent.parent

# Legacy in-repo locations (pre-relocation). Migrated once into the new root.
_LEGACY_DATA_DIR   = APP_ROOT / "data"
_LEGACY_CONFIG     = APP_ROOT / "config.yaml"

_MIGRATION_MARKER  = ".migrated"     # dropped in the new root once migration has run
_APP_DIR_NAME      = "WebWatcher"

# Resolve the root once per process. None until first data_dir() call.
_ROOT: Path | None = None


def _default_root() -> Path:
    """Where user data lives by default: %LOCALAPPDATA%\\WebWatcher on Windows, else
    ~/.web-watcher. Overridable with $WW_DATA_DIR (used by tests and portable installs)."""
    override = os.environ.get("WW_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return Path(local) / _APP_DIR_NAME
    return Path.home() / ".web-watcher"


def data_dir() -> Path:
    """The resolved, existing user-data root. Creates it (and runs the one-time legacy
    migration) on first call, then caches the result for the rest of the process."""
    global _ROOT
    if _ROOT is not None:
        return _ROOT
    root = _default_root()
    root.mkdir(parents=True, exist_ok=True)
    try:
        _migrate_legacy(root)
    except Exception as exc:                       # never let migration block startup
        log.warning("legacy data migration skipped: %s", exc)
    _ROOT = root
    return root


# ---------------------------------------------------------------------------
# Individual well-known paths (all under the data root)
# ---------------------------------------------------------------------------

def config_path() -> Path:
    return data_dir() / "config.yaml"


def db_path() -> Path:
    return data_dir() / "history.db"


def screenshots_dir() -> Path:
    return data_dir() / "screenshots"


def log_dir() -> Path:
    return data_dir() / "logs"


def webview_dir() -> Path:
    return data_dir() / "webview"


def browser_state_path() -> Path:
    return data_dir() / "browser_state.json"


def profile_dir() -> Path:
    return data_dir() / "profiles" / "default"


def watcher_history_path() -> Path:
    return data_dir() / "watcher_history.json"


# ---------------------------------------------------------------------------
# One-time legacy migration (copy, keep originals as backup)
# ---------------------------------------------------------------------------

def _migrate_legacy(root: Path) -> None:
    """Copy the pre-relocation in-repo data into `root` exactly once.

    Old layout put everything in the repo:  <app>/config.yaml and <app>/data/<...>.
    We COPY those into the new root (leaving the originals untouched as a backup) and then
    drop a `.migrated` marker so this never runs again. Skips anything that already exists in
    the destination, so a partially-populated new root is never clobbered."""
    marker = root / _MIGRATION_MARKER
    if marker.exists():
        return
    # Nothing to migrate (fresh install) — still drop the marker so we don't re-scan forever.
    if not _LEGACY_DATA_DIR.exists() and not _LEGACY_CONFIG.exists():
        marker.write_text("no legacy data\n", encoding="utf-8")
        return

    copied: list[str] = []

    # config.yaml  →  <root>/config.yaml
    dst_cfg = root / "config.yaml"
    if _LEGACY_CONFIG.exists() and not dst_cfg.exists():
        shutil.copy2(_LEGACY_CONFIG, dst_cfg)
        copied.append("config.yaml")

    # data/*  →  <root>/*   (history.db, screenshots/, logs/, webview/, profiles/,
    # browser_state.json, watcher_history.json, chat_history.json, …)
    if _LEGACY_DATA_DIR.is_dir():
        for src in _LEGACY_DATA_DIR.iterdir():
            dst = root / src.name
            if dst.exists():
                continue
            try:
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
                copied.append(src.name)
            except Exception as exc:
                log.warning("could not migrate %s: %s", src.name, exc)

    marker.write_text(
        "migrated from " + str(APP_ROOT) + "\n" + ", ".join(copied) + "\n",
        encoding="utf-8",
    )
    if copied:
        log.info("migrated %d legacy item(s) into %s (originals kept as backup)",
                 len(copied), root)
