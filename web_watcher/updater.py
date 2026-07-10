"""
Self-update against GitHub Releases.

Web Watcher ships as a folder of Python code (the models/Ollama/Chromium are set up
once by the installer and are NOT touched by updates). This module handles the small,
frequent part: checking GitHub Releases for a newer version, downloading + verifying the
code bundle, staging it, and applying it on next launch.

Split of responsibilities:
  • launcher.py, at startup, calls check_and_stage() (bounded by a 5s timeout so an offline
    user is never made to wait) and then apply_pending_update() to swap the staged code in
    BEFORE the app imports it — so a launch installs whatever was found in that same launch;
  • the RUNNING app ALSO checks + downloads + STAGES into updates/pending/ every few hours
    (never touching the live files while it's running), which lets the UI offer an immediate
    "Update & restart" instead of waiting for the next cold start.

Configure the source once (your GitHub repo): set env WW_UPDATE_OWNER / WW_UPDATE_REPO,
or edit the constants below. Empty owner → updates are silently disabled (safe default).

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  parse_version / is_newer   ~L60   Version compare (handles "-alpha" pre-releases)
  parse_release              ~L105  GitHub releases/latest JSON → UpdateInfo
  UpdateUnreachable          ~L135  Raised when GitHub can't be reached (≠ "up to date")
  check_for_update           ~L155  Fetch latest release, return UpdateInfo if newer
  check_and_stage            ~L180  Startup one-shot: check + download + stage, never raises
  download_and_stage         ~L205  Download zip, verify sha256, extract to updates/pending
  pending_update             ~L260  Is a validated update staged?
  apply_pending_update       ~L275  Swap staged code into place (called by launcher.py)
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Source config (your GitHub repo; env vars override for testing) ─────────
GITHUB_OWNER = os.environ.get("WW_UPDATE_OWNER", "Chriscc1234").strip()
GITHUB_REPO  = os.environ.get("WW_UPDATE_REPO", "web-watcher").strip()

ROOT        = Path(__file__).resolve().parent.parent      # the app folder (holds web_watcher/)
UPDATES_DIR = ROOT / "updates"
PENDING_DIR = UPDATES_DIR / "pending"
BACKUP_DIR  = UPDATES_DIR / "backup"
APPLY_MARKER = "APPLY_VERSION"                            # file in PENDING_DIR naming the staged version
RESTART_FLAG = UPDATES_DIR / "RESTART_REQUESTED"          # launcher relaunches when this exists

# Root-level scripts that ship inside the code bundle alongside web_watcher/. launcher.py is the
# thing that APPLIES updates, so if it weren't updatable a bug in it could only be fixed by
# reinstalling. Overwriting them mid-run is safe: Python has already read and compiled them, and
# the replacement only takes effect on the next launch.
ROOT_FILES = ("launcher.py", "provision.py", "install.py", "uninstall.py")

# Deliberately NOT in ROOT_FILES: the installer owns this file. See local_runtime().
RUNTIME_MARKER = "RUNTIME"

_API_TIMEOUT = 15.0
_STARTUP_TIMEOUT = 5.0            # launcher.py: never make an offline user wait longer than this
_DOWNLOAD_READ_TIMEOUT = 120.0    # a stalled socket, not a slow one, should abort the download


@dataclass
class UpdateInfo:
    version:      str            # normalized, e.g. "0.16.4-alpha"
    notes:        str            # changelog / release body (metadata lines stripped)
    download_url: str            # the code-zip asset URL
    sha256:       Optional[str]  # expected hash, if the release provides one
    runtime:        int = 1              # the runtime build this release needs (see local_runtime)
    installer_url:  Optional[str] = None # the full-installer asset, when the release ships one
    installer_sha256: Optional[str] = None
    installer_size: int = 0              # bytes, from the GitHub asset metadata


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------

_VER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-.]?(.+))?$")


def parse_version(s: str):
    """'v0.16.4-alpha' → (0, 16, 4, prerank). prerank: 0 for a pre-release (-alpha/-beta/…),
    1 for a final release, so a pre-release sorts BEFORE the same-numbered final. None if
    unparseable."""
    s = (s or "").strip().lstrip("vV")
    m = _VER_RE.match(s)
    if not m:
        return None
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    prerank = 0 if m.group(4) else 1
    return (major, minor, patch, prerank)


def is_newer(remote: str, local: str) -> bool:
    """True if `remote` is a strictly newer version than `local`."""
    r, l = parse_version(remote), parse_version(local)
    if r is None:
        return False
    if l is None:
        return True
    return r > l


# ---------------------------------------------------------------------------
# GitHub release parsing
# ---------------------------------------------------------------------------

# `(?<![\w-])` keeps the code-zip hash from also matching inside `installer_sha256:`.
_SHA_RE           = re.compile(r"(?<![\w-])sha256:\s*([0-9a-fA-F]{64})")
_INSTALLER_SHA_RE = re.compile(r"installer_sha256:\s*([0-9a-fA-F]{64})")
_RUNTIME_RE       = re.compile(r"^runtime:\s*(\d+)\s*$", re.M)


def parse_release(data: dict) -> Optional[UpdateInfo]:
    """Turn a GitHub releases/latest JSON object into an UpdateInfo, or None if it has no
    usable code-zip asset. Three machine-readable lines are read out of the release body
    (build_release.py puts them there) and stripped from the displayed notes:

        sha256: <hex>            the code bundle's hash
        installer_sha256: <hex>  the full installer's hash — REQUIRED before we ever run it
        runtime: <int>           which runtime build this release needs (see local_runtime)
    """
    if not isinstance(data, dict):
        return None
    tag = (data.get("tag_name") or data.get("name") or "").strip()
    if not tag:
        return None
    version = tag.lstrip("vV")
    body = data.get("body") or ""
    sha_m = _SHA_RE.search(body)
    sha256 = sha_m.group(1).lower() if sha_m else None
    isha_m = _INSTALLER_SHA_RE.search(body)
    installer_sha = isha_m.group(1).lower() if isha_m else None
    rt_m = _RUNTIME_RE.search(body)
    runtime = int(rt_m.group(1)) if rt_m else 1

    notes = body
    for pat in (_INSTALLER_SHA_RE, _SHA_RE, _RUNTIME_RE):   # installer_sha first: it contains sha256:
        notes = pat.sub("", notes)
    notes = notes.strip()

    zip_url = installer_url = None
    installer_size = 0
    for asset in (data.get("assets") or []):
        name = (asset.get("name") or "").lower()
        if name.endswith(".zip") and not zip_url:
            zip_url = asset.get("browser_download_url")
        elif name.endswith(".exe") and not installer_url:
            installer_url = asset.get("browser_download_url")
            installer_size = int(asset.get("size") or 0)
    if not zip_url:
        return None
    return UpdateInfo(version=version, notes=notes, download_url=zip_url, sha256=sha256,
                      runtime=runtime, installer_url=installer_url,
                      installer_sha256=installer_sha, installer_size=installer_size)


# ---------------------------------------------------------------------------
# Runtime build: when a code-only update is not enough
# ---------------------------------------------------------------------------

def local_runtime(root: Path = ROOT) -> int:
    """Which build of the bundled Python runtime + pip dependencies this install has.

    Written by the installer and deliberately NOT shipped in the code bundle: if a code-only
    update could rewrite this file, it would claim to have upgraded a runtime it never touched.
    Missing → 1, which is every install made before the marker existed."""
    try:
        return int((root / RUNTIME_MARKER).read_text(encoding="utf-8").strip())
    except Exception:
        return 1


def needs_installer(info: UpdateInfo, root: Path = ROOT) -> bool:
    """True when this release needs the full installer rather than a code swap — i.e. it bumped
    the Python runtime, the pip dependencies, the bundled DLLs, or the Playwright browsers.

    Applying such a release as a code-only update would drop new code onto missing dependencies
    and the app would die on import, with no working code left to repair itself."""
    return info.runtime > local_runtime(root)


# ---------------------------------------------------------------------------
# Check
# ---------------------------------------------------------------------------

class UpdateUnreachable(Exception):
    """GitHub could not be reached (offline, DNS, timeout, 5xx). Distinct from 'up to date',
    so the UI can say "couldn't check" instead of falsely claiming you're current."""


def _fetch_latest_release(owner: str, repo: str,
                         timeout: float = _API_TIMEOUT) -> Optional[dict]:
    """The latest release JSON, or None when the repo has no releases. Raises UpdateUnreachable
    when the network call itself fails."""
    import httpx
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            r = client.get(url, headers={"Accept": "application/vnd.github+json"})
            if r.status_code == 404:
                return None   # no releases yet (or the repo went private)
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        log.debug("update check network error: %s", exc)
        raise UpdateUnreachable(str(exc)) from exc


def check_for_update(current_version: str, owner: str = None, repo: str = None,
                     timeout: float = _API_TIMEOUT) -> Optional[UpdateInfo]:
    """Return an UpdateInfo when a newer release than `current_version` exists, else None.
    Silent (returns None) when updates aren't configured or on any network error."""
    owner = (owner if owner is not None else GITHUB_OWNER)
    repo  = (repo if repo is not None else GITHUB_REPO)
    if not owner:
        return None
    try:
        data = _fetch_latest_release(owner, repo, timeout=timeout)
    except UpdateUnreachable:
        return None
    if not data:
        return None
    info = parse_release(data)
    if info and is_newer(info.version, current_version):
        return info
    return None


def check_and_stage(current_version: str, timeout: float = _API_TIMEOUT,
                    root: Path = ROOT) -> Optional[str]:
    """Startup path: check GitHub and stage a newer release so the caller's apply step installs
    it in this SAME launch. Returns the staged version, or None when up to date, already staged,
    offline, or anything at all went wrong — an update must never stand between the user and
    their app, so every failure here is swallowed."""
    try:
        if pending_update(root):
            return None          # a previous session already staged it; apply will pick it up
        info = check_for_update(current_version, timeout=timeout)
        if info is None:
            return None
        if needs_installer(info, root):
            # Code-only staging would drop new code onto missing dependencies. The running app
            # handles this release: it downloads the installer and asks before running it.
            log.info("update %s needs the full installer — not staging code", info.version)
            return None
        if download_and_stage(info, root, timeout=timeout) is None:
            return None
        return info.version
    except Exception as exc:
        log.debug("startup update check skipped: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Download + stage
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_and_stage(info: UpdateInfo, root: Path = ROOT,
                       timeout: Optional[float] = None) -> Optional[Path]:
    """Download the release zip, verify its sha256 (when provided), and extract it into
    updates/pending/. The zip must contain a top-level `web_watcher/` directory. Returns
    the pending dir on success, None on failure (a bad hash never gets staged).

    `timeout` bounds the *connect* and *read* stalls, not the whole transfer — a slow link on a
    big asset must still finish. None means no limit (the background checker, which has all day)."""
    import httpx
    limits = httpx.Timeout(timeout, read=_DOWNLOAD_READ_TIMEOUT) if timeout else None
    updates = root / "updates"
    pending = updates / "pending"
    dl_dir  = updates / "download"
    dl_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dl_dir / f"web-watcher-{info.version}.zip"
    try:
        with httpx.Client(timeout=limits, follow_redirects=True) as client:
            with client.stream("GET", info.download_url) as resp:
                resp.raise_for_status()
                with zip_path.open("wb") as f:
                    for chunk in resp.iter_bytes(1 << 16):
                        f.write(chunk)
    except Exception as exc:
        log.warning("update download failed: %s", exc)
        return None

    staged = stage_zip(zip_path, info.version, root, expected_sha256=info.sha256)
    zip_path.unlink(missing_ok=True)
    return staged


def stage_zip(zip_path: Path, version: str, root: Path = ROOT,
              expected_sha256: Optional[str] = None) -> Optional[Path]:
    """Verify (optional sha256) and extract a downloaded bundle into updates/pending/. The
    zip must contain a top-level web_watcher/ dir. Returns the pending dir, or None on a bad
    hash / bad bundle (which are never staged). Split out from download_and_stage so it's
    unit-testable without network."""
    pending = root / "updates" / "pending"
    if expected_sha256:
        actual = _sha256_file(zip_path)
        if actual.lower() != expected_sha256.lower():
            log.error("update sha256 mismatch (expected %s, got %s) — discarding",
                      expected_sha256, actual)
            return None

    if pending.exists():
        shutil.rmtree(pending, ignore_errors=True)
    pending.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(pending)
    except Exception as exc:
        log.error("update extract failed: %s", exc)
        shutil.rmtree(pending, ignore_errors=True)
        return None

    if not (pending / "web_watcher").is_dir():
        log.error("update bundle missing top-level web_watcher/ — discarding")
        shutil.rmtree(pending, ignore_errors=True)
        return None

    (pending / APPLY_MARKER).write_text(version, encoding="utf-8")
    log.info("update %s staged (pending apply on next launch)", version)
    return pending


# ---------------------------------------------------------------------------
# Full-installer updates (runtime bumps: new pip deps, new Python, new DLLs)
# ---------------------------------------------------------------------------

def download_installer(info: UpdateInfo, root: Path = ROOT,
                       on_progress=None) -> Optional[Path]:
    """Download the release's installer .exe and verify its sha256. Returns the path, or None.

    The hash is MANDATORY here, unlike the code bundle where it is merely expected. This is the
    only place Web Watcher executes a binary, so a release without `installer_sha256:` in its
    body, or a download that doesn't match it, is refused outright and the file deleted.

    `on_progress(downloaded_bytes, total_bytes)` is called as the transfer runs."""
    import httpx
    if not info.installer_url:
        log.error("release %s needs the installer but ships no .exe asset", info.version)
        return None
    if not info.installer_sha256:
        log.error("release %s ships an installer with no installer_sha256 — refusing to run it",
                  info.version)
        return None

    dl_dir = root / "updates" / "download"
    dl_dir.mkdir(parents=True, exist_ok=True)
    exe_path = dl_dir / f"WebWatcher-Setup-{info.version}.exe"
    try:
        with httpx.Client(timeout=httpx.Timeout(30.0, read=_DOWNLOAD_READ_TIMEOUT),
                          follow_redirects=True) as client:
            with client.stream("GET", info.installer_url) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length") or info.installer_size or 0)
                done = 0
                with exe_path.open("wb") as f:
                    for chunk in resp.iter_bytes(1 << 18):
                        f.write(chunk)
                        done += len(chunk)
                        if on_progress:
                            on_progress(done, total)
    except Exception as exc:
        log.warning("installer download failed: %s", exc)
        exe_path.unlink(missing_ok=True)
        return None

    actual = _sha256_file(exe_path)
    if actual.lower() != info.installer_sha256.lower():
        log.error("installer sha256 mismatch (expected %s, got %s) — discarding",
                  info.installer_sha256, actual)
        exe_path.unlink(missing_ok=True)
        return None
    log.info("installer %s downloaded and verified", info.version)
    return exe_path


_INSTALLER_SETTLE = 2.5   # seconds to confirm the installer didn't die on the launch pad


def launch_installer(exe_path: Path, settle: float = _INSTALLER_SETTLE) -> bool:
    """Start the verified installer detached. Return True only once it looks alive; the caller
    must then EXIT so the files unlock.

    Web Watcher cannot install over itself — `python\\python.exe` is the running interpreter and
    Windows holds it locked. So we hand off: spawn the installer as an independent process, quit,
    and let it replace the folder. It relaunches the app when it's done (installer.iss runs the
    launcher on a silent install). /SILENT rather than /VERYSILENT so the user sees a progress
    window and knows the machine isn't hung.

    We wait a moment and check the process before reporting success. The installer is UNSIGNED, so
    Defender or an enterprise policy can kill it on sight — and if we had already closed the app,
    the user would be left with no Web Watcher and no explanation. A nonzero exit this early means
    it never got going. (Exit 0 is normal and expected: Inno's stub extracts the real setup to
    %TEMP%, launches it, and returns immediately.)"""
    import subprocess
    import time
    if not exe_path.is_file():
        return False
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.Popen([str(exe_path), "/SILENT", "/NOCANCEL"],
                                creationflags=flags, close_fds=True)
    except Exception as exc:
        log.error("could not start installer: %s", exc)
        return False

    time.sleep(settle)
    rc = proc.poll()
    if rc is not None and rc != 0:
        log.error("installer exited immediately with code %s — it was probably blocked "
                  "(antivirus / policy). Staying on the current version.", rc)
        return False
    return True


# ---------------------------------------------------------------------------
# Apply (called by launcher.py BEFORE the app imports web_watcher)
# ---------------------------------------------------------------------------

def pending_update(root: Path = ROOT) -> Optional[str]:
    """The version staged and ready to apply, or None."""
    marker = root / "updates" / "pending" / APPLY_MARKER
    if marker.exists() and (root / "updates" / "pending" / "web_watcher").is_dir():
        try:
            return marker.read_text(encoding="utf-8").strip() or None
        except Exception:
            return None
    return None


def apply_pending_update(root: Path = ROOT) -> Optional[str]:
    """Swap a staged update into place by copying the staged files OVER the live ones
    (overwrite + add; safe on Windows even for already-imported modules — the on-disk files
    aren't locked). Returns the applied version, or None if nothing was pending. The whole
    live web_watcher/ is backed up to updates/backup/ first so a broken release can be
    rolled back by hand. Idempotent: clears the staging dir when done."""
    version = pending_update(root)
    if not version:
        return None
    pending    = root / "updates" / "pending"
    staged_pkg = pending / "web_watcher"
    live_pkg   = root / "web_watcher"
    backup     = root / "updates" / "backup"
    try:
        # Back up the current package + root scripts (best-effort — never block on backup).
        try:
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            backup.mkdir(parents=True, exist_ok=True)
            shutil.copytree(live_pkg, backup / "web_watcher")
            for name in ROOT_FILES:
                if (root / name).exists():
                    shutil.copy2(root / name, backup / name)
        except Exception as exc:
            log.debug("update backup skipped: %s", exc)

        # Copy every staged file over the live tree (overwrite existing, create new).
        for src in staged_pkg.rglob("*"):
            rel = src.relative_to(staged_pkg)
            dst = live_pkg / rel
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

        # Root scripts, when the bundle carries them. Older bundles don't — that's not an error.
        for name in ROOT_FILES:
            src = pending / name
            if src.is_file():
                shutil.copy2(src, root / name)

        shutil.rmtree(root / "updates" / "pending", ignore_errors=True)
        log.info("applied update %s", version)
        return version
    except Exception as exc:
        log.error("apply update failed: %s", exc)
        return None
