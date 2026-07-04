"""Tests for the central data-dir resolver + one-time legacy migration (paths.py)."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def fresh_paths(tmp_path, monkeypatch):
    """A freshly-imported paths module whose data root is an isolated tmp dir, with the
    process-level cache reset so data_dir() re-resolves."""
    root = tmp_path / "dataroot"
    monkeypatch.setenv("WW_DATA_DIR", str(root))
    import web_watcher.paths as paths
    importlib.reload(paths)
    paths._ROOT = None  # ensure no cached resolution leaks in
    return paths, root


def test_override_wins_and_dir_created(fresh_paths):
    paths, root = fresh_paths
    assert paths.data_dir() == root
    assert root.is_dir()


def test_accessors_all_under_root(fresh_paths):
    paths, root = fresh_paths
    assert paths.config_path() == root / "config.yaml"
    assert paths.db_path() == root / "history.db"
    assert paths.screenshots_dir() == root / "screenshots"
    assert paths.log_dir() == root / "logs"
    assert paths.webview_dir() == root / "webview"
    assert paths.browser_state_path() == root / "browser_state.json"
    assert paths.profile_dir() == root / "profiles" / "default"
    assert paths.watcher_history_path() == root / "watcher_history.json"


def test_result_is_cached(fresh_paths):
    paths, root = fresh_paths
    first = paths.data_dir()
    assert paths.data_dir() is first  # same object, resolution ran once


def test_fresh_install_drops_marker_without_legacy(fresh_paths, monkeypatch, tmp_path):
    paths, root = fresh_paths
    # Point legacy locations at empty/nonexistent spots so there's nothing to migrate.
    monkeypatch.setattr(paths, "_LEGACY_DATA_DIR", tmp_path / "nope_data")
    monkeypatch.setattr(paths, "_LEGACY_CONFIG", tmp_path / "nope_config.yaml")
    paths.data_dir()
    assert (root / paths._MIGRATION_MARKER).exists()


def test_migrates_legacy_and_keeps_originals(fresh_paths, monkeypatch, tmp_path):
    paths, root = fresh_paths
    # Build a fake legacy layout: <legacy>/config.yaml and <legacy>/data/{history.db, logs/x}
    legacy = tmp_path / "legacy_app"
    legacy_data = legacy / "data"
    (legacy_data / "logs").mkdir(parents=True)
    (legacy / "config.yaml").write_text("watches: []\n", encoding="utf-8")
    (legacy_data / "history.db").write_text("DBDATA", encoding="utf-8")
    (legacy_data / "logs" / "session.log").write_text("log line\n", encoding="utf-8")
    monkeypatch.setattr(paths, "_LEGACY_DATA_DIR", legacy_data)
    monkeypatch.setattr(paths, "_LEGACY_CONFIG", legacy / "config.yaml")

    paths.data_dir()

    # Copied into the new root...
    assert (root / "config.yaml").read_text(encoding="utf-8") == "watches: []\n"
    assert (root / "history.db").read_text(encoding="utf-8") == "DBDATA"
    assert (root / "logs" / "session.log").read_text(encoding="utf-8") == "log line\n"
    # ...and the originals are untouched (kept as a backup).
    assert (legacy / "config.yaml").exists()
    assert (legacy_data / "history.db").exists()
    # Marker present so it won't run again.
    assert (root / paths._MIGRATION_MARKER).exists()


def test_migration_does_not_clobber_existing_dest(fresh_paths, monkeypatch, tmp_path):
    paths, root = fresh_paths
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text("KEEP ME\n", encoding="utf-8")
    legacy = tmp_path / "legacy_app"
    legacy.mkdir()
    (legacy / "config.yaml").write_text("OLD\n", encoding="utf-8")
    monkeypatch.setattr(paths, "_LEGACY_DATA_DIR", tmp_path / "nope")
    monkeypatch.setattr(paths, "_LEGACY_CONFIG", legacy / "config.yaml")

    paths.data_dir()
    assert (root / "config.yaml").read_text(encoding="utf-8") == "KEEP ME\n"


def test_marker_blocks_remigration(fresh_paths, monkeypatch, tmp_path):
    paths, root = fresh_paths
    root.mkdir(parents=True, exist_ok=True)
    (root / paths._MIGRATION_MARKER).write_text("already\n", encoding="utf-8")
    legacy = tmp_path / "legacy_app"
    legacy.mkdir()
    (legacy / "config.yaml").write_text("OLD\n", encoding="utf-8")
    monkeypatch.setattr(paths, "_LEGACY_CONFIG", legacy / "config.yaml")
    monkeypatch.setattr(paths, "_LEGACY_DATA_DIR", tmp_path / "nope")

    paths.data_dir()
    assert not (root / "config.yaml").exists()  # migration skipped entirely
