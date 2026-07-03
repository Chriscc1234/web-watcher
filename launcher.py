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

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESTART_FLAG = ROOT / "updates" / "RESTART_REQUESTED"


def _apply_pending() -> None:
    # Import the updater lazily (it's stdlib-only for apply) and swap in any staged update.
    try:
        from web_watcher.updater import apply_pending_update
        applied = apply_pending_update(ROOT)
        if applied:
            print(f"  Applied update {applied}.")
    except Exception as exc:
        print(f"  (update apply skipped: {exc})")


def main() -> int:
    while True:
        _apply_pending()
        proc = subprocess.run([sys.executable, "-m", "web_watcher.main"], cwd=str(ROOT))
        # Relaunch only if the app explicitly requested it (an update-and-restart click).
        if RESTART_FLAG.exists():
            try:
                RESTART_FLAG.unlink()
            except Exception:
                pass
            print("  Restarting to apply update…")
            continue
        return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
