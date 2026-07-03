"""
Web Watcher uninstaller.

Removes the Desktop + Start Menu shortcuts and any legacy auto-start task, and optionally
erases your personal data. It deliberately does NOT remove the shared heavy tools (Python,
Ollama, the downloaded models) — those may be used by other things; instructions to remove
them are printed at the end.

    python uninstall.py            # interactive
    python uninstall.py --purge-data --yes    # non-interactive, also wipe data

(The app folder itself can't delete itself while this script runs from inside it — delete
the folder by hand afterwards, or let the future installer's uninstaller do it.)
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _remove_shortcuts_and_task() -> None:
    if platform.system() != "Windows":
        return
    ps = (
        "foreach ($d in @([Environment]::GetFolderPath('Desktop'), "
        "[Environment]::GetFolderPath('Programs'))) {"
        "  $p = Join-Path $d 'Web Watcher.lnk';"
        "  if (Test-Path $p) { Remove-Item $p -Force; Write-Output ('  removed ' + $p) }"
        "}"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
    except Exception as exc:
        print(f"  (could not remove shortcuts automatically: {exc})")
    # Remove any legacy auto-start-at-login task from older installs.
    subprocess.run(["schtasks", "/delete", "/tn", "WebWatcher", "/f"], capture_output=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Web Watcher uninstaller")
    ap.add_argument("--purge-data", action="store_true",
                    help="Also erase personal data (data/ + config.yaml)")
    ap.add_argument("--yes", action="store_true", help="Non-interactive")
    args = ap.parse_args()

    print("Web Watcher uninstaller")
    print("-" * 30)

    _remove_shortcuts_and_task()
    print("  Removed the Desktop + Start Menu shortcuts (and any auto-start task).")

    purge = args.purge_data
    if not purge and not args.yes:
        ans = input("\nAlso ERASE all personal data — watches, results, saved logins, "
                    "history? [y/N]: ").strip().lower()
        purge = ans in ("y", "yes")
    if purge:
        shutil.rmtree(ROOT / "data", ignore_errors=True)
        (ROOT / "config.yaml").unlink(missing_ok=True)
        print("  Erased personal data (data/ + config.yaml).")
    else:
        print("  Kept your data (data/ + config.yaml) — delete them by hand if you want it gone.")

    print("\nStill installed (shared tools — remove manually only if nothing else needs them):")
    print(f"  - The app folder:   {ROOT}   (delete it after this script exits)")
    print("  - Ollama + models:  `ollama list` then `ollama rm <model>`;")
    print("                      uninstall Ollama itself from Settings > Apps.")
    print("  - Python + packages (leave unless you installed them only for Web Watcher).")
    print("\nDone.")


if __name__ == "__main__":
    main()
