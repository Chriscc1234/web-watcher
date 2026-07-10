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


def _release_json(version="9.9.9", notes="big news"):
    return {"tag_name": f"v{version}", "body": notes,
            "assets": [{"name": f"web-watcher-{version}.zip",
                        "browser_download_url": "http://x/bundle.zip"}]}


def test_manager_stages_and_reports_update(monkeypatch, tmp_path):
    from web_watcher.services import ServiceManager
    from web_watcher import updater as U
    staged = {"v": None}
    monkeypatch.setattr(U, "pending_update", lambda root=None: staged["v"])
    monkeypatch.setattr(U, "_fetch_latest_release", lambda o, r, timeout=None: _release_json())
    def fake_stage(i):
        staged["v"] = i.version
        return tmp_path
    monkeypatch.setattr(U, "download_and_stage", fake_stage)

    mgr = ServiceManager()
    st = mgr.check_updates_now()
    assert st["available"]["version"] == "9.9.9"
    assert st["staged"] is True
    assert st["error"] is None
    assert st["checked_at"] > 0
    assert mgr._update_available["notes"] == "big news"


def test_manager_reports_unreachable_rather_than_up_to_date(monkeypatch):
    """An offline check must not look like a clean bill of health."""
    from web_watcher.services import ServiceManager
    from web_watcher import updater as U

    def boom(o, r, timeout=None):
        raise U.UpdateUnreachable("getaddrinfo failed")
    monkeypatch.setattr(U, "pending_update", lambda root=None: None)
    monkeypatch.setattr(U, "_fetch_latest_release", boom)

    st = ServiceManager().check_updates_now()
    assert st["staged"] is False
    assert st["available"] is None
    assert "Couldn't reach GitHub" in st["error"]


def test_manager_up_to_date_has_no_error(monkeypatch):
    from web_watcher.services import ServiceManager
    from web_watcher import updater as U
    from web_watcher.__version__ import __version__
    monkeypatch.setattr(U, "pending_update", lambda root=None: None)
    monkeypatch.setattr(U, "_fetch_latest_release",
                        lambda o, r, timeout=None: _release_json(version=__version__))
    st = ServiceManager().check_updates_now()
    assert st["error"] is None and st["available"] is None and st["staged"] is False


# ---------------------------------------------------------------------------
# check_and_stage — the startup one-shot the launcher calls
# ---------------------------------------------------------------------------

def test_check_and_stage_returns_version_and_stages(monkeypatch, tmp_path):
    from web_watcher import updater as U
    info = U.UpdateInfo(version="9.9.9", notes="", download_url="x", sha256=None)
    monkeypatch.setattr(U, "pending_update", lambda root=None: None)
    monkeypatch.setattr(U, "check_for_update", lambda cur, timeout=None: info)
    monkeypatch.setattr(U, "download_and_stage", lambda i, root, timeout=None: tmp_path)
    assert U.check_and_stage("0.1.0", root=tmp_path) == "9.9.9"


def test_check_and_stage_skips_network_when_already_staged(monkeypatch, tmp_path):
    """A previous session already downloaded it; the caller's apply step will install it."""
    from web_watcher import updater as U
    monkeypatch.setattr(U, "pending_update", lambda root=None: "9.9.9")
    def never(*a, **k):
        raise AssertionError("must not hit the network when an update is already staged")
    monkeypatch.setattr(U, "check_for_update", never)
    assert U.check_and_stage("0.1.0", root=tmp_path) is None


@pytest.mark.parametrize("blowup", [
    lambda cur, timeout=None: (_ for _ in ()).throw(RuntimeError("kaboom")),
    lambda cur, timeout=None: None,          # up to date
])
def test_check_and_stage_never_raises(monkeypatch, tmp_path, blowup):
    """Nothing about updating may stand between the user and their app."""
    from web_watcher import updater as U
    monkeypatch.setattr(U, "pending_update", lambda root=None: None)
    monkeypatch.setattr(U, "check_for_update", blowup)
    assert U.check_and_stage("0.1.0", root=tmp_path) is None


def test_check_and_stage_none_when_download_fails(monkeypatch, tmp_path):
    from web_watcher import updater as U
    info = U.UpdateInfo(version="9.9.9", notes="", download_url="x", sha256=None)
    monkeypatch.setattr(U, "pending_update", lambda root=None: None)
    monkeypatch.setattr(U, "check_for_update", lambda cur, timeout=None: info)
    monkeypatch.setattr(U, "download_and_stage", lambda i, root, timeout=None: None)
    assert U.check_and_stage("0.1.0", root=tmp_path) is None


def test_check_for_update_swallows_unreachable(monkeypatch):
    from web_watcher import updater as U
    def boom(o, r, timeout=None):
        raise U.UpdateUnreachable("no dns")
    monkeypatch.setattr(U, "_fetch_latest_release", boom)
    assert U.check_for_update("0.1.0") is None


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


def _load_launcher():
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location(
        "ww_launcher", str(Path(__file__).resolve().parent.parent / "launcher.py"))
    launcher = importlib.util.module_from_spec(spec)
    sys.modules["ww_launcher"] = launcher
    spec.loader.exec_module(launcher)
    return launcher


def test_launcher_checks_before_applying(tmp_path, monkeypatch):
    """The whole point of the startup check: a launch installs what that same launch found.
    If apply ran first, the freshly-staged update would sit unused until the NEXT start."""
    launcher = _load_launcher()
    order = []
    monkeypatch.setattr(launcher, "_stage_startup_update", lambda: order.append("check"))
    monkeypatch.setattr(launcher, "_apply_pending", lambda: order.append("apply"))
    monkeypatch.setattr(launcher, "_needs_setup", lambda: False)
    monkeypatch.setattr(launcher, "_run_app", lambda: order.append("run") or 0)
    monkeypatch.setattr(launcher, "RESET_FLAG", tmp_path / "RESET_REQUESTED")
    monkeypatch.setattr(launcher, "RESTART_FLAG", tmp_path / "RESTART_REQUESTED")

    assert launcher.main() == 0
    assert order == ["check", "apply", "run"]


def test_launcher_does_not_recheck_on_restart_loop(tmp_path, monkeypatch):
    """The update is already staged when we loop to apply it — re-checking would add a network
    round-trip to every 'Update & restart'."""
    launcher = _load_launcher()
    order = []
    restart = tmp_path / "RESTART_REQUESTED"
    restart.write_text("1")
    monkeypatch.setattr(launcher, "_stage_startup_update", lambda: order.append("check"))
    monkeypatch.setattr(launcher, "_apply_pending", lambda: order.append("apply"))
    monkeypatch.setattr(launcher, "_needs_setup", lambda: False)
    monkeypatch.setattr(launcher, "_run_app", lambda: order.append("run") or 0)
    monkeypatch.setattr(launcher, "RESET_FLAG", tmp_path / "RESET_REQUESTED")
    monkeypatch.setattr(launcher, "RESTART_FLAG", restart)

    assert launcher.main() == 0
    # First pass consumes the flag and loops; the second pass applies + runs without re-checking.
    assert order == ["check", "apply", "run", "apply", "run"]
    assert order.count("check") == 1


def test_launcher_startup_check_is_skippable(monkeypatch, capsys):
    launcher = _load_launcher()
    monkeypatch.setenv("WW_NO_UPDATE_CHECK", "1")
    launcher._stage_startup_update()          # must not touch the network or print
    assert capsys.readouterr().out == ""


def test_launcher_startup_check_survives_a_broken_updater(monkeypatch, capsys):
    """Even an exploding updater import must not stop the app from starting."""
    launcher = _load_launcher()
    monkeypatch.delenv("WW_NO_UPDATE_CHECK", raising=False)
    from web_watcher import updater as U
    monkeypatch.setattr(U, "check_and_stage",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    launcher._stage_startup_update()          # no exception escapes
    assert "update check skipped" in capsys.readouterr().out


def test_launcher_reset_wipes_data_and_resets_config(tmp_path, monkeypatch):
    launcher = _load_launcher()

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
