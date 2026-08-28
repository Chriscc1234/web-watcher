"""Local copies of match photos — the card survives the listing.

The MHTML archive freezes a matched listing's PAGE, but the alert card in the admin's Chats
viewer still pointed at the SELLER'S image URL — which dies when the listing sells or expires,
so the record of "what was he sent" rotted card by card. The person's phone keeps the photo
it received (Telegram stores the upload); the app now does the same: the exact bytes that were
sent are saved here, and the mirrored thread entry points at the local copy.

Big originals are downscaled to a thumbnail (the user's rule: keep the whole thing, a
thumbnail if it's too big) — the card renders at ~230px, so a 3MB camera original buys
nothing. Bounded store, oldest pruned first, same philosophy as archive.py.

── KEY LOCATIONS ─────────────────────────────────────────────────────────────
  save_bytes    store an image for a listing key → the serving name, or None
  file_for      the on-disk path for a serving name (None if absent/unsafe)
  _shrink       downscale anything over the size/dimension caps to JPEG
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import io
import logging
import re
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_DIRNAME = "thumbs"
# Store the original when it's already small; shrink past these. 350KB / 1200px covers every
# marketplace thumbnail we've seen while keeping a phone-photo original out of the store.
_KEEP_BYTES = 350 * 1024
_MAX_DIM = 1200
_JPEG_QUALITY = 82
# Bounds for the whole store — a debugging/oversight aid must not quietly fill a disk.
_MAX_FILES = 1000
_MAX_BYTES = 200 * 1024 * 1024
_lock = threading.Lock()


def _dir() -> Path:
    from web_watcher import paths
    return paths.data_dir() / _DIRNAME


def _safe(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(key or "x"))[:120] or "x"


def _shrink(data: bytes) -> bytes:
    """Downscale/re-encode when the original is over the caps. On any failure (not an image,
    Pillow surprise) the original bytes come back — a copy that's too big beats no copy."""
    if len(data) <= _KEEP_BYTES:
        return data
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
        img.thumbnail((_MAX_DIM, _MAX_DIM))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=_JPEG_QUALITY, optimize=True)
        shrunk = out.getvalue()
        return shrunk if 0 < len(shrunk) < len(data) else data
    except Exception as exc:
        log.debug("thumb shrink failed (%s) — keeping the original bytes", exc)
        return data


def save_bytes(listing_key: str, data: bytes | None) -> str | None:
    """Store an image for a listing. Returns the SERVING NAME (for /api/thumb/<name>), or
    None. Idempotent per key — a re-alert overwrites with the same content. Never raises."""
    if not data:
        return None
    try:
        name = f"{_safe(listing_key)}.jpg"
        with _lock:
            d = _dir()
            d.mkdir(parents=True, exist_ok=True)
            (d / name).write_bytes(_shrink(data))
            _prune(d)
        return name
    except Exception as exc:
        log.debug("could not save thumb for %s: %s", listing_key, exc)
        return None


def file_for(name: str) -> Path | None:
    """The on-disk file for a serving name — None when absent or the name is unsafe (no path
    tricks reach past the store)."""
    clean = _safe(str(name or ""))
    if not clean or clean != str(name):
        return None
    p = _dir() / clean
    return p if p.exists() and p.is_file() else None


def _prune(d: Path) -> None:
    try:
        files = sorted(d.glob("*.jpg"), key=lambda f: f.stat().st_mtime)
        while len(files) > _MAX_FILES:
            files.pop(0).unlink(missing_ok=True)
        total = sum(f.stat().st_size for f in files if f.exists())
        i = 0
        while total > _MAX_BYTES and i < len(files):
            try:
                total -= files[i].stat().st_size
                files[i].unlink(missing_ok=True)
            except OSError:
                pass
            i += 1
    except Exception as exc:
        log.debug("thumb prune failed: %s", exc)
