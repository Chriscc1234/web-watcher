"""Frozen-page archive — keep a matched listing's page after the original goes stale."""
from __future__ import annotations
from pathlib import Path
import web_watcher.archive as A


def _tmp_snapshot(tmp_path, name="k1", body="MHTML BODY"):
    p = tmp_path / f"snap_{name}.mhtml"
    p.write_text(body, encoding="utf-8")
    return p


def test_keep_promotes_a_match_and_discard_drops_a_non_match(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_archive_dir", lambda: tmp_path / "arch")
    tmp = _tmp_snapshot(tmp_path)
    dst = A.keep(tmp, "craigslist:123")
    assert dst is not None and dst.exists()
    assert not tmp.exists()                       # moved, not copied
    assert A.archive_file("craigslist:123") == dst

    t2 = _tmp_snapshot(tmp_path, "k2")
    A.discard(t2)
    assert not t2.exists()                        # a non-match leaves nothing behind


def test_a_listing_key_with_slashes_is_filesystem_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_archive_dir", lambda: tmp_path / "arch")
    tmp = _tmp_snapshot(tmp_path)
    dst = A.keep(tmp, "offerup:a/b:c d")
    assert dst is not None and dst.exists()       # no path traversal, no crash


def test_prune_caps_by_count(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_archive_dir", lambda: tmp_path / "arch")
    monkeypatch.setattr(A, "_MAX_FILES", 3)
    import time
    for i in range(6):
        t = _tmp_snapshot(tmp_path, f"k{i}")
        A.keep(t, f"site:{i}")
        time.sleep(0.01)                          # distinct mtimes so oldest-first is well-defined
    remaining = sorted((tmp_path / "arch").glob("*.mhtml"))
    assert len(remaining) == 3                     # oldest three pruned


def test_capture_and_keep_never_raise_on_junk(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_archive_dir", lambda: tmp_path / "arch")
    assert A.capture_temp(object(), "k") is None   # not a real page → None, no raise
    assert A.keep(None, "k") is None
    A.discard(None)                                # no-op, no raise
    assert A.archive_file("never-archived") is None
