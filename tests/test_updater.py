"""
Tests for the self-updater: version comparison, GitHub release parsing, and a full
stage→apply round-trip (no network — a hand-built zip stands in for a downloaded release).
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from web_watcher import updater


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("remote,local,expected", [
    ("0.16.4-alpha", "0.16.3-alpha", True),    # patch bump
    ("0.17.0-alpha", "0.16.9-alpha", True),    # minor bump
    ("1.0.0",        "0.99.99",      True),     # major bump
    ("0.16.3-alpha", "0.16.3-alpha", False),    # same
    ("0.16.3-alpha", "0.16.4-alpha", False),    # older
    ("0.16.3",       "0.16.3-alpha", True),     # final > same-numbered pre-release
    ("0.16.3-alpha", "0.16.3",       False),    # pre-release < final
    ("v0.16.4-alpha","0.16.3-alpha", True),     # leading v tolerated
])
def test_is_newer(remote, local, expected):
    assert updater.is_newer(remote, local) is expected


def test_is_newer_handles_garbage():
    assert updater.is_newer("not-a-version", "0.1.0") is False
    assert updater.is_newer("0.2.0", "garbage") is True


# ---------------------------------------------------------------------------
# GitHub release parsing
# ---------------------------------------------------------------------------

def test_parse_release_extracts_zip_and_sha_and_strips_sha_from_notes():
    data = {
        "tag_name": "v0.16.4-alpha",
        "body": "## [0.16.4-alpha]\n- did a thing\n\nsha256: " + ("a" * 64) + "\n",
        "assets": [
            {"name": "RELEASE_NOTES.md", "browser_download_url": "http://x/notes"},
            {"name": "web-watcher-0.16.4-alpha.zip", "browser_download_url": "http://x/bundle.zip"},
        ],
    }
    info = updater.parse_release(data)
    assert info is not None
    assert info.version == "0.16.4-alpha"
    assert info.download_url == "http://x/bundle.zip"
    assert info.sha256 == "a" * 64
    assert "sha256:" not in info.notes          # stripped from the displayed notes
    assert "did a thing" in info.notes


def test_parse_release_none_without_zip_asset():
    assert updater.parse_release({"tag_name": "v1", "assets": []}) is None
    assert updater.parse_release({}) is None


# ---------------------------------------------------------------------------
# Stage + apply round-trip
# ---------------------------------------------------------------------------

def _make_bundle(tmp_path: Path, marker_text: str) -> Path:
    """A zip containing a top-level web_watcher/ with one changed file + one new file."""
    zip_path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("web_watcher/__version__.py", marker_text)
        z.writestr("web_watcher/newmod.py", "VALUE = 42\n")
    return zip_path


def test_stage_verifies_sha_and_apply_swaps_files(tmp_path):
    root = tmp_path
    live = root / "web_watcher"
    live.mkdir()
    (live / "__version__.py").write_text('__version__ = "0.0.1"\n', encoding="utf-8")

    bundle = _make_bundle(root, '__version__ = "9.9.9"\n')
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()

    # Wrong hash → never staged.
    assert updater.stage_zip(bundle, "9.9.9", root=root, expected_sha256="b" * 64) is None
    assert updater.pending_update(root) is None

    # Correct hash → staged, then applied.
    assert updater.stage_zip(bundle, "9.9.9", root=root, expected_sha256=digest) is not None
    assert updater.pending_update(root) == "9.9.9"

    applied = updater.apply_pending_update(root)
    assert applied == "9.9.9"
    assert (live / "__version__.py").read_text(encoding="utf-8") == '__version__ = "9.9.9"\n'  # overwritten
    assert (live / "newmod.py").read_text(encoding="utf-8") == "VALUE = 42\n"                  # added
    assert (root / "updates" / "backup" / "web_watcher" / "__version__.py").exists()           # backed up
    assert updater.pending_update(root) is None                                                # cleared


def test_apply_noop_when_nothing_staged(tmp_path):
    assert updater.apply_pending_update(tmp_path) is None


def test_manager_stages_and_reports_update(monkeypatch, tmp_path):
    from web_watcher.services import ServiceManager
    from web_watcher import updater as U
    info = U.UpdateInfo(version="9.9.9", notes="big news", download_url="x", sha256=None)
    staged = {"v": None}
    monkeypatch.setattr(U, "pending_update", lambda root=None: staged["v"])
    monkeypatch.setattr(U, "check_for_update", lambda cur: info)
    def fake_stage(i):
        staged["v"] = i.version
        return tmp_path
    monkeypatch.setattr(U, "download_and_stage", fake_stage)

    mgr = ServiceManager()
    st = mgr.check_updates_now()
    assert st["available"]["version"] == "9.9.9"
    assert st["staged"] is True
    assert mgr._update_available["notes"] == "big news"


def test_manager_request_restart_flags_and_closes(monkeypatch, tmp_path):
    from web_watcher.services import ServiceManager
    from web_watcher import updater as U
    monkeypatch.setattr(U, "pending_update", lambda root=None: "9.9.9")
    monkeypatch.setattr(U, "UPDATES_DIR", tmp_path)
    monkeypatch.setattr(U, "RESTART_FLAG", tmp_path / "RESTART_REQUESTED")

    closed = {"n": 0}
    class FakeWindow:
        def destroy(self): closed["n"] += 1
    mgr = ServiceManager()
    mgr._window = FakeWindow()
    assert mgr.request_restart() is True
    assert (tmp_path / "RESTART_REQUESTED").exists()   # launcher will see this
    assert closed["n"] == 1                             # window closed to trigger relaunch

    # Nothing staged → no restart.
    monkeypatch.setattr(U, "pending_update", lambda root=None: None)
    assert mgr.request_restart() is False


def test_launcher_reset_wipes_data_and_resets_config(tmp_path, monkeypatch):
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location(
        "ww_launcher", str(Path(__file__).resolve().parent.parent / "launcher.py"))
    launcher = importlib.util.module_from_spec(spec)
    sys.modules["ww_launcher"] = launcher
    spec.loader.exec_module(launcher)

    # User data now lives in a per-user data root (WW_DATA_DIR), separate from the app folder.
    from web_watcher import paths
    data_root = tmp_path / "dataroot"
    data_root.mkdir()
    (data_root / "history.db").write_text("stuff")
    (data_root / "browser_state.json").write_text("cookies")       # saved logins
    (data_root / "config.yaml").write_text("watches:\n- name: mine\n")
    monkeypatch.setenv("WW_DATA_DIR", str(data_root))

    app = tmp_path / "app"
    app.mkdir()
    (app / "config.example.yaml").write_text("watches: []\n")

    launcher._do_reset(app)

    # All user data gone except a fresh config + the re-migration guard marker.
    assert not (data_root / "history.db").exists()
    assert not (data_root / "browser_state.json").exists()
    assert (data_root / "config.yaml").read_text() == "watches: []\n"   # reset to template
    assert (data_root / paths._MIGRATION_MARKER).exists()              # blocks legacy re-import


def test_manager_request_reset_flags_and_closes(monkeypatch, tmp_path):
    from web_watcher.services import ServiceManager
    from web_watcher import updater as U
    monkeypatch.setattr(U, "UPDATES_DIR", tmp_path)
    closed = {"n": 0}
    class FakeWindow:
        def destroy(self): closed["n"] += 1
    mgr = ServiceManager()
    mgr._window = FakeWindow()
    assert mgr.request_reset() is True
    assert (tmp_path / "RESET_REQUESTED").exists()   # launcher will wipe on next start
    assert closed["n"] == 1
