"""
Web Watcher launcher.

This is the process start.bat (and later the installer's shortcut) runs — NOT
`python -m web_watcher.main` directly. It exists so updates can be applied safely:

  1. Check GitHub for a newer release and stage it (bounded by a short timeout).
  2. Apply any staged update (swap the new code in) BEFORE the app is imported.
  3. Launch the app as a child process and wait.
  4. If the app asked to "Update & restart" (it drops updates/RESTART_REQUESTED on the
     way out), loop: apply the freshly-staged update and relaunch. Otherwise exit.

Step 1 must come before step 2. The running app also checks for updates, but it can only
STAGE them — the swap can't happen under a live process. So if the launcher never checked, you
would download version N while running N-1 and only install N on the *next* start, leaving every
user permanently one release behind unless they noticed the in-app banner. Checking here closes
that gap: what a launch finds, that same launch installs.

Because the swap happens here — before web_watcher is imported in the child — the app's
own running files are never mid-flight when they're replaced.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESTART_FLAG = ROOT / "updates" / "RESTART_REQUESTED"
RESET_FLAG   = ROOT / "updates" / "RESET_REQUESTED"


def _child_python() -> str:
    """Prefer pythonw.exe so the app runs windowless (no console flash) when started from a
    shortcut. Falls back to whatever launched us."""
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        cand = exe.with_name("pythonw.exe")
        if cand.exists():
            return str(cand)
    return str(exe)


def _console_python() -> str:
    """python.exe (with a console), so setup progress is visible."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        cand = exe.with_name("python.exe")
        if cand.exists():
            return str(cand)
    return str(exe)


def _needs_setup() -> bool:
    """True if first-run setup never completed (marker absent) — e.g. a stalled/interrupted
    install. Absent marker → we finish setup before launching, so the app self-heals."""
    try:
        from web_watcher import paths
        return not (paths.data_dir() / ".setup_complete").exists()
    except Exception:
        return False


def _run_setup() -> None:
    """Finish first-run setup (Ollama + models + browser + config) in a visible console, with
    the pull-retry built into install.py. Runs only when the completion marker is missing."""
    prov = ROOT / "provision.py"
    if not prov.exists():
        return
    print("  Finishing Web Watcher setup (first run or a previous run was interrupted)…")
    try:
        subprocess.run([_console_python(), str(prov)], cwd=str(ROOT))
    except Exception as exc:
        print(f"  (setup step failed: {exc})")


def _run_app() -> int:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    # Force UTF-8 for the child's stdio/filesystem. The bundled runtime otherwise defaults to
    # the Windows locale (cp1252), which raises UnicodeEncodeError whenever the app logs the
    # →/↳/✓ glyphs used throughout agent/scheduler output — crashing the console log handler.
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return subprocess.run([_child_python(), "-m", "web_watcher.main"],
                          cwd=str(ROOT), creationflags=flags, env=env).returncode


def _stage_startup_update() -> None:
    """Check for a newer release and stage it, so the _apply_pending() that follows installs it
    in this same launch. Bounded and silent: Web Watcher is an offline-first app, so a missing
    network, a dead DNS, or a GitHub outage must cost at most a few seconds and never a failure.
    Set WW_NO_UPDATE_CHECK=1 to skip (used by the test suite and by offline demos)."""
    if os.environ.get("WW_NO_UPDATE_CHECK"):
        return
    try:
        from web_watcher.updater import check_and_stage, _STARTUP_TIMEOUT
        from web_watcher.__version__ import __version__
        staged = check_and_stage(__version__, timeout=_STARTUP_TIMEOUT)
        if staged:
            print(f"  Downloaded update {staged} — installing…")
    except Exception as exc:
        print(f"  (update check skipped: {exc})")


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
    """Wipe ALL user data back to a fresh install: delete the user-data root (DB, saved
    logins/cookies, history, logs, screenshots, dashboard profile, config.yaml) and reset
    config.yaml to the blank template. Done HERE — before the app opens the DB — so nothing
    is file-locked. A `.migrated` marker is re-dropped so the reset does NOT re-import the
    legacy in-repo backup on the next launch."""
    try:
        from web_watcher import paths
        data_root = paths._default_root()
        shutil.rmtree(data_root, ignore_errors=True)
        data_root.mkdir(parents=True, exist_ok=True)
        # Block re-migration of the legacy backup that a reset is meant to discard.
        (data_root / paths._MIGRATION_MARKER).write_text("reset\n", encoding="utf-8")
        example = root / "config.example.yaml"
        target  = data_root / "config.yaml"
        if example.exists():
            shutil.copy2(example, target)
        print("  Reset to a fresh install (all personal data cleared).")
    except Exception as exc:
        print(f"  (reset skipped: {exc})")


def main() -> int:
    first = True
    while True:
        # Only on a cold start: a restart-to-apply loop already has its update staged, and
        # re-checking would just add a network round-trip to every relaunch.
        if first:
            _stage_startup_update()
            first = False
        _apply_pending()
        if _needs_setup():          # interrupted/first-run install → finish it, then launch
            _run_setup()
        code = _run_app()
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
        return code


if __name__ == "__main__":
    raise SystemExit(main())
