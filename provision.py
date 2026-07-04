"""
First-run provisioner (invoked by the installer, runs under the bundled runtime).

The Inno Setup installer lays down a self-contained Python runtime with every pip dependency
already baked in, plus the app code. What it CAN'T bundle is the heavy, machine-specific stuff:
Ollama + the local models, and the Playwright Chromium browser. This script does exactly that
on first launch, reusing install.py's existing logic — it simply runs the installer flow with
the parts the bundle already handled turned off:

    • --skip-deps       pip dependencies are already in the bundled runtime
    • --skip-shortcuts  the Inno installer created the Desktop + Start Menu shortcuts
    • --yes             non-interactive (accept defaults, no credential prompts)

So provisioning still: ensures Ollama (installs via winget if missing), detects the GPU tier,
pulls the right models, installs Playwright Chromium, and writes the initial config into the
per-user data root. Safe to re-run — model pulls and browser installs are idempotent.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

# Output is piped to Inno Setup under the bundled runtime → default cp1252 can't encode the
# status glyphs. Force UTF-8 so provisioning progress prints instead of crashing.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent


def main() -> None:
    print("=" * 52)
    print("  Web Watcher — first-run setup")
    print("=" * 52)
    print("  Bundled: Python + all app dependencies.")
    print("  Now setting up local AI (Ollama + models) and the browser.")
    print("  This is a one-time, multi-GB download; the app runs offline afterward.\n")

    # Drive install.py's main() with the bundle-aware flags.
    sys.argv = [str(ROOT / "install.py"),
                "--skip-deps", "--skip-shortcuts", "--keep-config", "--yes"]
    runpy.run_path(str(ROOT / "install.py"), run_name="__main__")


if __name__ == "__main__":
    main()
