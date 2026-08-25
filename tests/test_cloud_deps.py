"""The cloud-SDK self-heal: when a user has set a cloud key but the `anthropic` package was never
installed, the app installs it in the background at startup — otherwise every escalation dies on
ImportError and silently falls back to local, so loaded credits can never be spent. Guarded on a
configured key so a purely-local install never runs pip. See services._ensure_cloud_deps."""

from __future__ import annotations

import pytest

from web_watcher import services as S
from web_watcher.services import ServiceManager


def _mgr():
    # The method uses no instance state, so skip the heavy __init__.
    return ServiceManager.__new__(ServiceManager)


def _cfg_with_key(key: str):
    from web_watcher.config import AppConfig
    cfg = AppConfig()
    cfg.models.cloud.anthropic_api_key = key
    return cfg


class _Done:
    returncode = 0
    stderr = ""


def test_installs_when_key_set_and_sdk_missing(monkeypatch):
    monkeypatch.setattr("web_watcher.config.load", lambda: _cfg_with_key("sk-ant-xxx"))
    monkeypatch.setattr(S, "_anthropic_installed", lambda: False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(S.subprocess, "run", lambda cmd, *a, **k: calls.append(cmd) or _Done())

    _mgr()._ensure_cloud_deps()

    assert len(calls) == 1
    cmd = calls[0]
    assert "pip" in cmd and "install" in cmd and any("anthropic" in str(x) for x in cmd)


def test_skips_when_already_installed(monkeypatch):
    monkeypatch.setattr("web_watcher.config.load", lambda: _cfg_with_key("sk-ant-xxx"))
    monkeypatch.setattr(S, "_anthropic_installed", lambda: True)
    monkeypatch.setattr(S.subprocess, "run",
                        lambda *a, **k: pytest.fail("must not pip-install when the SDK is present"))
    _mgr()._ensure_cloud_deps()


def test_skips_when_no_cloud_key(monkeypatch):
    monkeypatch.setattr("web_watcher.config.load", lambda: _cfg_with_key(""))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(S, "_anthropic_installed", lambda: False)
    monkeypatch.setattr(S.subprocess, "run",
                        lambda *a, **k: pytest.fail("no key → a local-only install must never pip"))
    _mgr()._ensure_cloud_deps()


def test_a_failed_install_is_not_fatal(monkeypatch):
    monkeypatch.setattr("web_watcher.config.load", lambda: _cfg_with_key("sk-ant-xxx"))
    monkeypatch.setattr(S, "_anthropic_installed", lambda: False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def _boom(*a, **k):
        raise RuntimeError("pip exploded")

    monkeypatch.setattr(S.subprocess, "run", _boom)
    _mgr()._ensure_cloud_deps()          # must swallow the error, not raise
