"""Local copies of match photos — the card survives the listing.

The MHTML archive freezes the page; these freeze the CARD image. The seller's URL dies when
the listing sells; the bytes the person was actually sent do not.
"""

from __future__ import annotations

import io

import pytest

from web_watcher import thumbs


def _jpeg(px: int, noisy: bool = False) -> bytes:
    from PIL import Image
    if noisy:
        # Flat colour compresses absurdly well (3000px -> 142KB), so a "big" test image must
        # carry real entropy to actually exceed the byte cap the way a photo does.
        import random
        img = Image.frombytes("RGB", (px, px),
                              bytes(random.getrandbits(8) for _ in range(px * px * 3)))
    else:
        img = Image.new("RGB", (px, px), (200, 60, 60))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=95)
    return out.getvalue()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(thumbs, "_dir", lambda: tmp_path / "thumbs")
    return tmp_path / "thumbs"


def test_small_image_is_kept_verbatim(store):
    data = _jpeg(64)
    name = thumbs.save_bytes("cl:12345", data)
    assert name == "cl_12345.jpg"
    assert (store / name).read_bytes() == data


def test_big_image_is_shrunk_to_a_thumbnail(store):
    """The user's rule: keep the whole thing, a thumbnail if it's too big. A camera original
    buys nothing at card size."""
    big = _jpeg(1600, noisy=True)
    assert len(big) > thumbs._KEEP_BYTES
    name = thumbs.save_bytes("cl:big", big)
    saved = (store / name).read_bytes()
    assert len(saved) < len(big)
    from PIL import Image
    w, h = Image.open(io.BytesIO(saved)).size
    assert max(w, h) <= thumbs._MAX_DIM


def test_garbage_bytes_are_still_kept(store):
    """Not an image? Keep it anyway — a copy that's odd beats no copy — and never raise."""
    name = thumbs.save_bytes("k", b"not an image at all" * 40000)
    assert name and (store / name).exists()


def test_empty_bytes_save_nothing(store):
    assert thumbs.save_bytes("k", b"") is None
    assert thumbs.save_bytes("k", None) is None


def test_file_for_blocks_path_tricks(store):
    thumbs.save_bytes("ok", _jpeg(32))
    assert thumbs.file_for("ok.jpg") is not None
    assert thumbs.file_for("../secrets.txt") is None
    assert thumbs.file_for("..\..\config.yaml") is None
    assert thumbs.file_for("") is None


def test_store_is_pruned(store, monkeypatch):
    monkeypatch.setattr(thumbs, "_MAX_FILES", 3)
    for i in range(6):
        thumbs.save_bytes(f"k{i}", _jpeg(32))
    assert len(list(store.glob("*.jpg"))) <= 3


def test_alert_send_saves_and_mirrors_the_local_copy():
    src = open("web_watcher/notify.py", encoding="utf-8").read()
    assert "thumbs.save_bytes(key, img)" in src
    assert '/api/thumb/{_local_thumb}' in src


def test_thumb_endpoint_exists():
    src = open("web_watcher/dashboard/server.py", encoding="utf-8").read()
    assert '@app.get("/api/thumb/{name}")' in src
