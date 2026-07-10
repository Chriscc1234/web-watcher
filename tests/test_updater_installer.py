"""Full-installer updates: when a release bumps the bundled runtime (new Python, new pip deps,
new DLLs), a code-only swap would drop fresh code onto missing dependencies and the app would die
on import — with no working code left to repair itself.

Two properties are safety-critical and both are tested here:
  1. A runtime-bumped release is NEVER staged as a code update.
  2. An installer .exe is NEVER executed unless its sha256 was declared in the release body and
     the downloaded bytes match it. This is the only binary Web Watcher ever runs.
"""

from __future__ import annotations

import hashlib
import types

import pytest

from web_watcher import updater as U


def _release(version="9.9.9", runtime=1, zip_sha=None, exe_sha=None, with_exe=True):
    body = f"## {version}\n- notes\n\nsha256: {zip_sha or 'a' * 64}\nruntime: {runtime}\n"
    if exe_sha:
        body += f"installer_sha256: {exe_sha}\n"
    assets = [{"name": f"web-watcher-{version}.zip",
               "browser_download_url": "http://x/bundle.zip"}]
    if with_exe:
        assets.append({"name": f"WebWatcher-Setup-{version}.exe",
                       "browser_download_url": "http://x/setup.exe", "size": 291_000_000})
    return {"tag_name": f"v{version}", "body": body, "assets": assets}


# ---------------------------------------------------------------------------
# Release parsing
# ---------------------------------------------------------------------------

def test_parse_release_reads_runtime_installer_url_and_hashes():
    info = U.parse_release(_release(runtime=4, zip_sha="b" * 64, exe_sha="c" * 64))
    assert info.runtime == 4
    assert info.download_url == "http://x/bundle.zip"
    assert info.installer_url == "http://x/setup.exe"
    assert info.sha256 == "b" * 64
    assert info.installer_sha256 == "c" * 64
    assert info.installer_size == 291_000_000


def test_installer_sha_line_does_not_hijack_the_code_zip_sha():
    """`installer_sha256:` literally contains `sha256:` — the two must not collide."""
    info = U.parse_release(_release(zip_sha="b" * 64, exe_sha="c" * 64))
    assert info.sha256 == "b" * 64          # not "c"*64
    assert info.installer_sha256 == "c" * 64


def test_metadata_lines_are_stripped_from_the_user_facing_notes():
    info = U.parse_release(_release(runtime=3, exe_sha="c" * 64))
    for junk in ("sha256:", "installer_sha256:", "runtime:"):
        assert junk not in info.notes
    assert "- notes" in info.notes


def test_release_without_runtime_line_defaults_to_1():
    data = {"tag_name": "v9.9.9", "body": "no metadata",
            "assets": [{"name": "x.zip", "browser_download_url": "http://x/b.zip"}]}
    assert U.parse_release(data).runtime == 1


# ---------------------------------------------------------------------------
# local_runtime + needs_installer
# ---------------------------------------------------------------------------

def test_local_runtime_defaults_to_1_when_marker_is_absent_or_junk(tmp_path):
    assert U.local_runtime(tmp_path) == 1                       # pre-marker install
    (tmp_path / "RUNTIME").write_text("not a number")
    assert U.local_runtime(tmp_path) == 1
    (tmp_path / "RUNTIME").write_text("7\n")
    assert U.local_runtime(tmp_path) == 7


def test_needs_installer_only_when_runtime_bumped(tmp_path):
    (tmp_path / "RUNTIME").write_text("2\n")
    assert U.needs_installer(U.parse_release(_release(runtime=3)), tmp_path) is True
    assert U.needs_installer(U.parse_release(_release(runtime=2)), tmp_path) is False
    assert U.needs_installer(U.parse_release(_release(runtime=1)), tmp_path) is False


def test_runtime_marker_is_not_shipped_in_the_code_bundle():
    """If a code-only update could rewrite RUNTIME, it would claim to have upgraded a runtime it
    never touched, and the installer would never run again."""
    assert U.RUNTIME_MARKER not in U.ROOT_FILES


def test_check_and_stage_refuses_to_stage_a_runtime_bumped_release(monkeypatch, tmp_path):
    """THE critical guard: staging this as code would break the app on next import."""
    (tmp_path / "RUNTIME").write_text("1\n")
    info = U.parse_release(_release(runtime=2, exe_sha="c" * 64))
    monkeypatch.setattr(U, "pending_update", lambda root=None: None)
    monkeypatch.setattr(U, "check_for_update", lambda cur, timeout=None: info)

    def never(*a, **k):
        raise AssertionError("must not stage code for a release that needs the installer")
    monkeypatch.setattr(U, "download_and_stage", never)

    assert U.check_and_stage("0.1.0", root=tmp_path) is None


# ---------------------------------------------------------------------------
# download_installer — the hash gate
# ---------------------------------------------------------------------------

def _fake_httpx(monkeypatch, payload: bytes):
    class _Resp:
        headers = {"content-length": str(len(payload))}
        def raise_for_status(self): pass
        def iter_bytes(self, n): yield payload
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def stream(self, method, url): return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(httpx, "Timeout", lambda *a, **k: None)


def test_download_installer_verifies_hash_and_keeps_the_file(monkeypatch, tmp_path):
    payload = b"pretend installer bytes"
    digest = hashlib.sha256(payload).hexdigest()
    _fake_httpx(monkeypatch, payload)
    info = U.parse_release(_release(exe_sha=digest))

    seen = []
    path = U.download_installer(info, root=tmp_path, on_progress=lambda d, t: seen.append((d, t)))
    assert path is not None and path.read_bytes() == payload
    assert seen and seen[-1] == (len(payload), len(payload))     # progress reported


def test_download_installer_discards_a_tampered_binary(monkeypatch, tmp_path):
    _fake_httpx(monkeypatch, b"malicious payload")
    info = U.parse_release(_release(exe_sha="d" * 64))           # hash of something else
    assert U.download_installer(info, root=tmp_path) is None
    assert list((tmp_path / "updates" / "download").glob("*.exe")) == []   # deleted, not left around


def test_download_installer_refuses_a_release_with_no_declared_hash(monkeypatch, tmp_path):
    """No `installer_sha256:` in the body → we have nothing to verify against → never run it."""
    def never(*a, **k):
        raise AssertionError("must not download an installer we cannot verify")
    import httpx
    monkeypatch.setattr(httpx, "Client", never)
    info = U.parse_release(_release(exe_sha=None))
    assert info.installer_sha256 is None
    assert U.download_installer(info, root=tmp_path) is None


def test_download_installer_none_when_release_ships_no_exe(tmp_path):
    info = U.parse_release(_release(exe_sha="c" * 64, with_exe=False))
    assert info.installer_url is None
    assert U.download_installer(info, root=tmp_path) is None


# ---------------------------------------------------------------------------
# launch_installer + the manager
# ---------------------------------------------------------------------------

def _fake_popen(monkeypatch, returncode):
    """returncode: what poll() reports after the settle wait. None = still running."""
    calls = {}
    import subprocess
    def fake_popen(cmd, **kw):
        calls["cmd"] = cmd
        return types.SimpleNamespace(pid=123, poll=lambda: returncode)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return calls


def test_launch_installer_runs_detached_and_silent(monkeypatch, tmp_path):
    exe = tmp_path / "setup.exe"
    exe.write_bytes(b"x")
    calls = _fake_popen(monkeypatch, 0)     # Inno's stub exits 0 after spawning the real setup

    assert U.launch_installer(exe, settle=0) is True
    assert calls["cmd"][0] == str(exe)
    assert "/SILENT" in calls["cmd"]        # progress visible; installer.iss relaunches the app


def test_launch_installer_still_running_is_success(monkeypatch, tmp_path):
    exe = tmp_path / "setup.exe"
    exe.write_bytes(b"x")
    _fake_popen(monkeypatch, None)
    assert U.launch_installer(exe, settle=0) is True


def test_launch_installer_detects_a_blocked_binary(monkeypatch, tmp_path):
    """The installer is unsigned — antivirus or policy can kill it instantly. If we reported
    success, the caller would close the app and the user would be left with nothing."""
    exe = tmp_path / "setup.exe"
    exe.write_bytes(b"x")
    _fake_popen(monkeypatch, 1)             # died immediately, nonzero
    assert U.launch_installer(exe, settle=0) is False


def test_launch_installer_false_when_file_is_missing(tmp_path):
    assert U.launch_installer(tmp_path / "nope.exe") is False


def test_manager_keeps_the_window_open_when_the_installer_is_blocked(monkeypatch, tmp_path):
    from web_watcher.services import ServiceManager
    exe = tmp_path / "setup.exe"
    exe.write_bytes(b"x")
    mgr = ServiceManager()
    mgr._installer_path = exe
    closed = {"n": 0}
    mgr._window = types.SimpleNamespace(destroy=lambda: closed.__setitem__("n", closed["n"] + 1))
    monkeypatch.setattr(U, "launch_installer", lambda p: False)

    assert mgr.run_installer() is False
    assert closed["n"] == 0                              # app stays open
    assert "blocked" in mgr.update_status()["error"]     # and says why


def test_manager_will_not_run_an_installer_it_never_downloaded():
    from web_watcher.services import ServiceManager
    assert ServiceManager().run_installer() is False


def test_manager_downloads_installer_and_reports_progress(monkeypatch, tmp_path):
    from web_watcher.services import ServiceManager
    from web_watcher.__version__ import __version__

    exe = tmp_path / "setup.exe"
    exe.write_bytes(b"x")
    monkeypatch.setattr(U, "pending_update", lambda root=None: None)
    monkeypatch.setattr(U, "_fetch_latest_release",
                        lambda o, r, timeout=None: _release(exe_sha="c" * 64))
    monkeypatch.setattr(U, "is_newer", lambda remote, local: True)
    monkeypatch.setattr(U, "needs_installer", lambda info, root=None: True)
    monkeypatch.setattr(U, "download_installer", lambda info, on_progress=None: exe)

    def never(*a, **k):
        raise AssertionError("a runtime-bumped release must not be staged as code")
    monkeypatch.setattr(U, "download_and_stage", never)

    mgr = ServiceManager()
    st = mgr.check_updates_now()
    assert st["available"]["kind"] == "installer"
    assert st["available"]["size_mb"] == 291
    mgr._update_thread = None
    for t in list(__import__("threading").enumerate()):
        if t.name == "ww-installer-dl":
            t.join(timeout=5)
    assert mgr.update_status()["installer_ready"] is True

    # Runs only once asked, and closes the window so the installer can replace the folder.
    closed = {"n": 0}
    mgr._window = types.SimpleNamespace(destroy=lambda: closed.__setitem__("n", closed["n"] + 1))
    monkeypatch.setattr(U, "launch_installer", lambda p: True)
    assert mgr.run_installer() is True
    assert closed["n"] == 1
