"""
SQLite storage tests — all offline, use temp databases.
"""

from __future__ import annotations

import pytest
from pathlib import Path

from web_watcher.storage import RunRecord, get_history, get_last_run, init_db, save_run


@pytest.fixture
def db(tmp_path) -> Path:
    path = tmp_path / "test.db"
    init_db(path)
    return path


def _rec(**kwargs) -> RunRecord:
    defaults = dict(
        watch_name="weather",
        run_timestamp="2026-06-20T12:00:00+00:00",
        found=True,
        summary="Winter storm warning",
        link="https://nws.gov",
        confidence="high",
        perception_mode_used="text",
        error=None,
        screenshot_path=None,
    )
    return RunRecord(**{**defaults, **kwargs})


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

def test_init_creates_table(db):
    import sqlite3
    conn = sqlite3.connect(db)
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    assert any("run_history" in t for t in tables)


def test_init_is_idempotent(db):
    init_db(db)  # second call must not fail
    init_db(db)  # third call must not fail


# ---------------------------------------------------------------------------
# save_run + get_history
# ---------------------------------------------------------------------------

def test_save_and_retrieve(db):
    row_id = save_run(_rec(), db)
    assert isinstance(row_id, int) and row_id > 0

    rows = get_history(db_path=db)
    assert len(rows) == 1
    assert rows[0]["watch_name"] == "weather"
    assert rows[0]["found"] == 1
    assert rows[0]["summary"] == "Winter storm warning"


def test_get_history_newest_first(db):
    save_run(_rec(run_timestamp="2026-06-20T10:00:00+00:00", summary="first"), db)
    save_run(_rec(run_timestamp="2026-06-20T11:00:00+00:00", summary="second"), db)
    save_run(_rec(run_timestamp="2026-06-20T12:00:00+00:00", summary="third"), db)

    rows = get_history(db_path=db)
    assert rows[0]["summary"] == "third"
    assert rows[-1]["summary"] == "first"


def test_get_history_filtered_by_watch(db):
    save_run(_rec(watch_name="weather"), db)
    save_run(_rec(watch_name="news"), db)
    save_run(_rec(watch_name="weather"), db)

    rows = get_history(watch_name="weather", db_path=db)
    assert len(rows) == 2
    assert all(r["watch_name"] == "weather" for r in rows)


def test_get_history_limit(db):
    for i in range(10):
        save_run(_rec(run_timestamp=f"2026-06-20T{i:02d}:00:00+00:00"), db)

    rows = get_history(limit=3, db_path=db)
    assert len(rows) == 3


def test_get_history_missing_db_returns_empty(tmp_path):
    rows = get_history(db_path=tmp_path / "nonexistent.db")
    assert rows == []


# ---------------------------------------------------------------------------
# get_last_run
# ---------------------------------------------------------------------------

def test_get_last_run_returns_most_recent(db):
    save_run(_rec(summary="older"), db)
    save_run(_rec(summary="newer"), db)

    last = get_last_run("weather", db_path=db)
    assert last is not None
    assert last["summary"] == "newer"


def test_get_last_run_returns_none_when_empty(db):
    assert get_last_run("nonexistent", db_path=db) is None


def test_get_last_run_missing_db_returns_none(tmp_path):
    assert get_last_run("x", db_path=tmp_path / "no.db") is None


# ---------------------------------------------------------------------------
# Error records
# ---------------------------------------------------------------------------

def test_save_error_record(db):
    save_run(_rec(found=False, error="browser: timeout", summary=None, link=None), db)
    rows = get_history(db_path=db)
    assert rows[0]["error"] == "browser: timeout"
    assert rows[0]["found"] == 0


# ---------------------------------------------------------------------------
# Screenshot path stored and retrieved
# ---------------------------------------------------------------------------

def test_screenshot_path_round_trips(db):
    path = "/data/screenshots/weather_20260620.png"
    save_run(_rec(screenshot_path=path), db)
    rows = get_history(db_path=db)
    assert rows[0]["screenshot_path"] == path
