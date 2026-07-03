"""Tests for the in-app Console (shell) backend: cd persistence + command execution."""
from __future__ import annotations
from web_watcher.services import ServiceManager


def test_console_echo_and_exit_code():
    m = ServiceManager()
    r = m.console_run("echo hello-console")
    assert "hello-console" in r["stdout"]
    assert r["code"] == 0


def test_console_cd_persists_and_bad_cd_errors(tmp_path):
    m = ServiceManager()
    m._console_cwd = str(tmp_path)
    (tmp_path / "sub").mkdir()
    r = m.console_run("cd sub")
    assert r["code"] == 0 and r["cwd"].endswith("sub")
    bad = m.console_run("cd does_not_exist")
    assert bad["code"] == 1 and "no such directory" in bad["stderr"]
    # cwd unchanged after a bad cd
    assert m.console_cwd().endswith("sub")


def test_console_empty_command_is_noop():
    m = ServiceManager()
    r = m.console_run("   ")
    assert r["code"] == 0 and r["stdout"] == "" and r["stderr"] == ""
