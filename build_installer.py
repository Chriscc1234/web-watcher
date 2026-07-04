r"""
Build the Web Watcher Setup.exe.

Two-stage build:
  1. `python build_runtime.py`   → build/runtime/{python,app}  (self-contained runtime + code)
  2. `python build_installer.py` → installer/Output/WebWatcher-Setup-<version>.exe

This wrapper stamps the app version (from web_watcher/__version__.py) into the Inno Setup
compile and locates ISCC.exe. Run build_runtime.py first (or pass --with-runtime to chain it).

Requires Inno Setup 6 (ISCC.exe). Install once:  winget install JRSoftware.InnoSetup
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT      = Path(__file__).resolve().parent
ISS       = ROOT / "installer" / "installer.iss"
RUNTIME   = ROOT / "build" / "runtime" / "python" / "python.exe"

import os as _os
_ISCC_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    Path(_os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
]


def _version() -> str:
    ns: dict = {}
    exec((ROOT / "web_watcher" / "__version__.py").read_text(encoding="utf-8"), ns)
    return ns["__version__"]


def _find_iscc() -> Path:
    import shutil
    for c in _ISCC_CANDIDATES:
        if c.exists():
            return c
    which = shutil.which("iscc") or shutil.which("ISCC")
    if which:
        return Path(which)
    raise SystemExit(
        "ISCC.exe (Inno Setup 6) not found. Install it:\n"
        "    winget install JRSoftware.InnoSetup\n"
        "then re-run this script."
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the Web Watcher installer")
    ap.add_argument("--with-runtime", action="store_true",
                    help="Run build_runtime.py first (fresh runtime) before compiling")
    args = ap.parse_args()

    if args.with_runtime:
        print("[build_installer] building runtime first…")
        subprocess.run([sys.executable, str(ROOT / "build_runtime.py")], check=True)

    if not RUNTIME.exists():
        raise SystemExit(f"runtime not built ({RUNTIME} missing) — run build_runtime.py first "
                         "or pass --with-runtime")

    version = _version()
    iscc = _find_iscc()
    print(f"[build_installer] version={version}  iscc={iscc}")
    cmd = [str(iscc), f"/DAppVersion={version}", str(ISS)]
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise SystemExit(f"ISCC failed (exit {r.returncode})")
    out = ROOT / "installer" / "Output" / f"WebWatcher-Setup-{version}.exe"
    print(f"[build_installer] DONE -> {out}"
          + ("" if out.exists() else "  (expected output not found - check ISCC log)"))


if __name__ == "__main__":
    main()
