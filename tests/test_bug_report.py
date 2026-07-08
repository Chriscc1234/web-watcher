"""The bug report must never leak notification credentials (Telegram token, email password)."""

from __future__ import annotations

import zipfile

from web_watcher.dashboard.server import _watch_summary_no_secrets, _write_bug_report


def test_watch_summary_omits_notify_secrets():
    from web_watcher.config import Watch
    w = Watch.model_validate({
        "name": "Trucks", "mode": "continuous", "instruction": "find trucks",
        "urls": ["https://seattle.craigslist.org/search/cta?query=truck"],
        "notify": {"telegram": True, "telegram_token": "SECRET-TELEGRAM-123",
                   "email": True, "email_password": "SECRET-EMAIL-PW"},
    })
    import types
    cfg = types.SimpleNamespace(watches=[w])
    out = _watch_summary_no_secrets(cfg)
    assert "Trucks" in out and "truck" in out          # useful repro context is present
    assert "SECRET-TELEGRAM-123" not in out            # …but no secrets
    assert "SECRET-EMAIL-PW" not in out
    assert "telegram_token" not in out and "email_password" not in out


def test_bug_report_zip_has_report_and_no_secrets(tmp_path, monkeypatch):
    # Desktop → tmp; data root → tmp (so we read no real config).
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / "Desktop").mkdir()
    monkeypatch.setenv("WW_DATA_DIR", str(tmp_path / "data"))

    import web_watcher.paths as paths
    import importlib
    importlib.reload(paths)
    paths._ROOT = None

    path = _write_bug_report("My title", "It broke")
    assert path.exists() and path.suffix == ".zip"
    with zipfile.ZipFile(path) as z:
        report = z.read("report.txt").decode()
    assert "My title" in report and "It broke" in report
    assert "App version" in report
