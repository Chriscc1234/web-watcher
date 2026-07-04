# Building the Web Watcher installer

Produces a single `WebWatcher-Setup-<version>.exe` that installs on a machine with **nothing
pre-installed**. It bundles its own Python + every dependency; only Ollama and the local models
are fetched on first run.

## Prerequisites (build machine only)
- Python 3.13 (to run the build scripts) with `httpx` available.
- Inno Setup 6 — `winget install JRSoftware.InnoSetup`.
- Internet (build downloads the standalone Python + pip wheels).

## Build steps
```sh
# 1. Build the self-contained runtime (downloads CPython + pip-installs all deps into it).
#    Heavy (~1.5 GB runtime, torch/easyocr/whisper). Re-run with --skip-download to reuse.
python build_runtime.py

# 2. Compile the installer (stamps version from web_watcher/__version__.py).
python build_installer.py
#    → installer/Output/WebWatcher-Setup-<version>.exe   (~278 MB)

# Or chain both:
python build_installer.py --with-runtime
```

## What the installer does
- Installs **per-user** to `%LOCALAPPDATA%\Programs\WebWatcher` (no admin; keeps the in-app
  auto-updater able to write staged updates).
- Lays down `python\` (the runtime) + `app\` (the code).
- Creates Desktop + Start Menu shortcuts → `pythonw.exe launcher.py` (windowless).
- Runs `provision.py` on first install: ensures Ollama (winget), detects GPU tier, pulls the
  models, installs Playwright Chromium. Never overwrites an existing config (`--keep-config`).
- Registers an Add/Remove-Programs uninstaller.

## User data is separate
All watches/results/logins/logs live in `%LOCALAPPDATA%\WebWatcher` (see `web_watcher/paths.py`),
**not** in the install folder — so uninstalling or reinstalling never touches user data.

## Clean-room test
`WebWatcher-Sandbox-test.wsb` boots a pristine Windows Sandbox with the installer mapped in
read-only, for a true from-scratch install test. See the comments inside that file.
