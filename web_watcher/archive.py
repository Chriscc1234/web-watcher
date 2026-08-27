"""Frozen-page archive — a self-contained snapshot of a listing, so it survives the original.

We already keep a TEXT copy of every listing (title, price, the ad body, attributes) — enough to
show a deleted post's details. What we did NOT keep was the PAGE: the photos, the layout, the
seller's exact wording as it appeared. When a listing is pulled — sold, expired, deleted — that is
gone. This module freezes it as MHTML: a single file that inlines the HTML, CSS and images, so it
opens and renders offline weeks later even after the source is dead.

Two principles, both learned the hard way this session:

  • NO EXTRA PAGE VISITS. The snapshot is taken during the deep-read visit the sweep already makes
    to read the ad body. Re-opening a page just to archive it is extra traffic and, on a site like
    Facebook, an extra chance to look like a bot. Capture is free when we're already there.

  • KEEP ONLY WHAT MATTERED. Every deep-read page is snapshotted to a TEMP file; after judging, the
    snapshot is KEPT only for listings that matched (the ones a person might act on) and discarded
    for the rest. Bounded further by a hard cap on count and total size, pruning oldest first — an
    archive that grows without limit is a disk that fills without warning.

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  capture_temp   snapshot the CURRENT page to a temp .mhtml (during deep-read); returns the path
  keep           promote a temp snapshot to the permanent archive for a matched listing + prune
  discard        delete a temp snapshot (a non-match); best-effort
  archive_file   the permanent path for a listing_key, or None if not archived
"""

from __future__ import annotations

import logging
import re
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

_DIRNAME = "archives"
# Hard bounds. Whichever trips first prunes oldest-first. A listing MHTML is typically 0.3–3 MB;
# 400 files / 800 MB keeps a useful recent window without letting the disk run away.
_MAX_FILES = 400
_MAX_BYTES = 800 * 1024 * 1024
_lock = threading.Lock()


def _archive_dir() -> Path:
    from web_watcher import paths
    return paths.data_dir() / _DIRNAME


def _safe_key(listing_key: str) -> str:
    """A filesystem-safe name for a listing key (keys can contain slashes, colons, spaces)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(listing_key or "x"))[:120] or "x"


def capture_temp(page, listing_key: str = "") -> Path | None:
    """Snapshot the page CURRENTLY loaded in `page` to a temp .mhtml. Returns the path, or None if
    the browser can't snapshot (non-Chromium, or the page went away). Best-effort — a failed
    archive must never disturb the sweep that was reading the ad."""
    try:
        cdp = page.context.new_cdp_session(page)
        data = (cdp.send("Page.captureSnapshot", {"format": "mhtml"}) or {}).get("data", "")
        if not data:
            return None
        tmp = Path(tempfile.gettempdir()) / f"ww_arch_{_safe_key(listing_key)}_{int(time.time()*1000)}.mhtml"
        tmp.write_text(data, encoding="utf-8")
        return tmp
    except Exception as exc:
        log.debug("archive capture failed for %s: %s", listing_key[:40], exc)
        return None


def keep(tmp_path: Path | None, listing_key: str) -> Path | None:
    """Promote a temp snapshot to the permanent archive for a MATCHED listing, then prune the
    archive back within its bounds. Returns the permanent path, or None. Never raises."""
    if not tmp_path:
        return None
    try:
        tmp_path = Path(tmp_path)
        if not tmp_path.exists():
            return None
        with _lock:
            d = _archive_dir()
            d.mkdir(parents=True, exist_ok=True)
            dst = d / f"{_safe_key(listing_key)}.mhtml"
            tmp_path.replace(dst)
            _prune(d)
        log.info("Archived a frozen copy of %s (%d KB)", listing_key[:50], dst.stat().st_size // 1024)
        return dst
    except Exception as exc:
        log.debug("could not keep archive for %s: %s", listing_key[:40], exc)
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass
        return None


def discard(tmp_path: Path | None) -> None:
    """Delete a temp snapshot for a listing that did NOT match. Best-effort."""
    if not tmp_path:
        return
    try:
        Path(tmp_path).unlink()
    except OSError:
        pass


def archive_file(listing_key: str) -> Path | None:
    """The permanent archive path for a listing, or None if it was never archived."""
    try:
        p = _archive_dir() / f"{_safe_key(listing_key)}.mhtml"
        return p if p.exists() else None
    except Exception:
        return None


def _prune(d: Path) -> None:
    """Keep the archive within _MAX_FILES and _MAX_BYTES, oldest-first. Called under _lock."""
    try:
        files = sorted(d.glob("*.mhtml"), key=lambda f: f.stat().st_mtime)
        # By count.
        while len(files) > _MAX_FILES:
            files.pop(0).unlink(missing_ok=True)  # type: ignore[call-arg]
        # By total size.
        total = sum(f.stat().st_size for f in files if f.exists())
        i = 0
        while total > _MAX_BYTES and i < len(files):
            f = files[i]
            try:
                total -= f.stat().st_size
                f.unlink(missing_ok=True)  # type: ignore[call-arg]
            except OSError:
                pass
            i += 1
    except Exception as exc:
        log.debug("archive prune failed: %s", exc)
