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

_API_TIMEOUT = 15.0
_STARTUP_TIMEOUT = 5.0            # launcher.py: never make an offline user wait longer than this
_DOWNLOAD_READ_TIMEOUT = 120.0    # a stalled socket, not a slow one, should abort the download


@dataclass
class UpdateInfo:
    version:      str            # normalized, e.g. "0.16.4-alpha"
    notes:        str            # changelog / release body (sha line stripped)
    download_url: str            # the code-zip asset URL
    sha256:       Optional[str]  # expected hash, if the release provides one


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

_SHA_RE = re.compile(r"sha256:\s*([0-9a-fA-F]{64})")


def parse_release(data: dict) -> Optional[UpdateInfo]:
    """Turn a GitHub releases/latest JSON object into an UpdateInfo, or None if it has no
    usable code-zip asset. The expected sha256 is read from a `sha256: <hex>` line in the
    release body (build_release.py puts it there); that line is stripped from the notes."""
    if not isinstance(data, dict):
        return None
    tag = (data.get("tag_name") or data.get("name") or "").strip()
    if not tag:
        return None
    version = tag.lstrip("vV")
    body = data.get("body") or ""
    sha_m = _SHA_RE.search(body)
    sha256 = sha_m.group(1).lower() if sha_m else None
    notes = _SHA_RE.sub("", body).strip()

    zip_url = None
    for asset in (data.get("assets") or []):
        name = (asset.get("name") or "").lower()
        if name.endswith(".zip"):
            zip_url = asset.get("browser_download_url")
            break
    if not zip_url:
        return None
    return UpdateInfo(version=version, notes=notes, download_url=zip_url, sha256=sha256)


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
