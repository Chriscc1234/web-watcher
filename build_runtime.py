r"""
Build a relocatable, self-contained Python runtime for Web Watcher.

The shipped installer bundles Python itself so the end user needs nothing pre-installed.
Rather than freeze with PyInstaller (which fights torch/opencv/scipy hooks and whisper/easyocr
runtime weight downloads), we ship a full CPython from python-build-standalone with all of
Web Watcher's dependencies pip-installed into it. The result is a plain folder that runs as
`<runtime>\python.exe -m web_watcher.main` — no system Python, no venv activation.

Output layout (build/runtime/):
    build/runtime/python/          the standalone CPython (python.exe, Lib/, site-packages…)
    build/runtime/app/             a copy of the app code (web_watcher/, launcher.py, …)

Run:  python build_runtime.py            # full build (download + pip install everything)
      python build_runtime.py --skip-download   # reuse an already-extracted runtime

This is heavy (torch/easyocr pull ~1-2GB of wheels). Internet is required at BUILD time only;
the produced runtime — like the app itself — runs fully offline against local Ollama.

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  RUNTIME_URL / _resolve_asset   ~L40   Pinned python-build-standalone asset
  download_runtime               ~L70   Fetch + extract the standalone CPython
  install_deps                   ~L100  pip install requirements + the app into the runtime
  copy_app                       ~L130  Stage app code next to the runtime
  verify                         ~L150  Import-smoke the app under the fresh runtime
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT      = Path(__file__).resolve().parent
BUILD     = ROOT / "build"
RUNTIME   = BUILD / "runtime"
PY_DIR    = RUNTIME / "python"            # extracted standalone CPython lives here
APP_DIR   = RUNTIME / "app"              # staged copy of the app code
DL_DIR    = BUILD / "download"

# Pinned python-build-standalone asset (3.13, Windows x86_64, install_only). Matches the
# app's 3.13 line. Bump deliberately; a mismatched minor breaks compiled wheels.
RUNTIME_TAG = "20260623"
RUNTIME_ASSET = (
    f"cpython-3.13.14+{RUNTIME_TAG}-x86_64-pc-windows-msvc-install_only.tar.gz"
)
RUNTIME_URL = (
    "https://github.com/astral-sh/python-build-standalone/releases/download/"
    f"{RUNTIME_TAG}/" + RUNTIME_ASSET.replace("+", "%2B")
)

# App code copied into the shipped runtime (everything the running app imports/needs).
APP_INCLUDE = [
    "web_watcher",
    "launcher.py",
    "provision.py",
    "requirements.txt",
    "config.example.yaml",
    "CHANGELOG.md",
    "install.py",
    "uninstall.py",
]


def _log(msg: str) -> None:
    print(f"[build_runtime] {msg}", flush=True)


def _runtime_python() -> Path:
    """python.exe inside the extracted standalone runtime."""
    return PY_DIR / "python.exe"


def download_runtime() -> None:
    import httpx

    DL_DIR.mkdir(parents=True, exist_ok=True)
    tar_path = DL_DIR / RUNTIME_ASSET
    if not tar_path.exists():
        _log(f"downloading {RUNTIME_ASSET} …")
        with httpx.Client(timeout=None, follow_redirects=True) as c:
            with c.stream("GET", RUNTIME_URL) as r:
                r.raise_for_status()
                with tar_path.open("wb") as f:
                    for chunk in r.iter_bytes(1 << 16):
                        f.write(chunk)
        _log(f"downloaded {tar_path.stat().st_size/1e6:.1f}MB")
    else:
        _log("runtime tarball already downloaded")

    # The archive extracts to a top-level `python/` dir. Reset PY_DIR to it.
    if PY_DIR.exists():
        shutil.rmtree(PY_DIR, ignore_errors=True)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    _log("extracting runtime …")
    with tarfile.open(tar_path, "r:gz") as t:
        t.extractall(RUNTIME, filter="data")
    if not _runtime_python().exists():
        raise SystemExit(f"extraction did not produce {_runtime_python()}")
    _log(f"runtime ready at {PY_DIR}")


def install_deps() -> None:
    py = str(_runtime_python())
    _log("upgrading pip …")
    subprocess.run([py, "-m", "pip", "install", "--upgrade", "pip", "wheel"], check=True)
    _log("installing requirements (this is the heavy step: torch/easyocr/whisper) …")
    subprocess.run([py, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")],
                   check=True)
    # PyInstaller/pytest not needed at runtime — requirements.txt lists pytest for dev only;
    # it's small, harmless to keep. Nothing else to strip for a first working build.
    _log("dependencies installed")


def copy_app() -> None:
    if APP_DIR.exists():
        shutil.rmtree(APP_DIR, ignore_errors=True)
    APP_DIR.mkdir(parents=True, exist_ok=True)
    for name in APP_INCLUDE:
        src = ROOT / name
        if not src.exists():
            _log(f"  (skip missing {name})")
            continue
        dst = APP_DIR / name
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(src, dst)
    _log(f"app code staged at {APP_DIR}")


def verify() -> None:
    py = str(_runtime_python())
    _log("verifying app imports under the fresh runtime …")
    # Run from the staged app dir so `web_watcher` resolves without install.
    code = (
        "import web_watcher, web_watcher.paths as p, web_watcher.config, "
        "web_watcher.storage, web_watcher.main, webview, fastapi, uvicorn, "
        "playwright, torch, easyocr, whisper; "
        "print('OK', web_watcher.__version__)"
    )
    r = subprocess.run([py, "-c", code], cwd=str(APP_DIR))
    if r.returncode != 0:
        raise SystemExit("verification FAILED — app does not import under the runtime")
    _log("verification passed")


def _tree_size(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e6


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the relocatable Web Watcher runtime")
    ap.add_argument("--skip-download", action="store_true",
                    help="Reuse an already-extracted runtime (skip fetch+extract)")
    ap.add_argument("--skip-deps", action="store_true",
                    help="Skip pip install (assume deps already present)")
    args = ap.parse_args()

    if not args.skip_download:
        download_runtime()
    elif not _runtime_python().exists():
        raise SystemExit("--skip-download given but no runtime extracted yet")

    if not args.skip_deps:
        install_deps()
    copy_app()
    verify()

    _log(f"DONE. runtime={_tree_size(PY_DIR):.0f}MB  app={_tree_size(APP_DIR):.1f}MB  "
         f"total={_tree_size(RUNTIME):.0f}MB")
    _log(f"Launch test:  {_runtime_python()} -m web_watcher.main   (cwd={APP_DIR})")


if __name__ == "__main__":
    main()
