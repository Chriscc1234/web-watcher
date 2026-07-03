"""
Web Watcher launcher.

This is the process start.bat (and later the installer's shortcut) runs — NOT
`python -m web_watcher.main` directly. It exists so updates can be applied safely:

  1. Apply any staged update (swap the new code in) BEFORE the app is imported.
  2. Launch the app as a child process and wait.
  3. If the app asked to "Update & restart" (it drops updates/RESTART_REQUESTED on the
     way out), loop: apply the freshly-staged update and relaunch. Otherwise exit.

Because the swap happens here — before web_watcher is imported in the child — the app's
own running files are never mid-flight when they're replaced.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESTART_FLAG = ROOT / "updates" / "RESTART_REQUESTED"
RESET_FLAG   = ROOT / "updates" / "RESET_REQUESTED"


def _apply_pending() -> None:
    # Import the updater lazily (it's stdlib-only for apply) and swap in any staged update.
    try:
        from web_watcher.updater import apply_pending_update
        applied = apply_pending_update(ROOT)
        if applied:
            print(f"  Applied update {applied}.")
    except Exception as exc:
        print(f"  (update apply skipped: {exc})")


def _do_reset(root: Path = ROOT) -> None:
    """Wipe ALL user data back to a fresh install: delete data/ (DB, saved logins/cookies,
    history, logs, screenshots, dashboard profile) and reset config.yaml to the blank
    template. Done HERE — before the app opens the DB — so nothing is file-locked."""
    try:
        shutil.rmtree(root / "data", ignore_errors=True)
        example = root / "config.example.yaml"
        target  = root / "config.yaml"
        target.unlink(missing_ok=True)
        if example.exists():
            shutil.copy2(example, target)
        print("  Reset to a fresh install (all personal data cleared).")
    except Exception as exc:
        print(f"  (reset skipped: {exc})")


def main() -> int:
    while True:
        _apply_pending()
        proc = subprocess.run([sys.executable, "-m", "web_watcher.main"], cwd=str(ROOT))
        # A fresh-install reset takes priority, then an update-and-restart. Both close the
        # window and drop a flag on the way out; we act on it here, then relaunch.
        if RESET_FLAG.exists():
            RESET_FLAG.unlink(missing_ok=True)
            _do_reset()
            print("  Restarting fresh…")
            continue
        if RESTART_FLAG.exists():
            RESTART_FLAG.unlink(missing_ok=True)
            print("  Restarting to apply update…")
            continue
        return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
