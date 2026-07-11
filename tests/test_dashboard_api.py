"""Regression tests for the watch create/update endpoints.

The 0.22.x "error 500 on create" bug: chat suggestions often propose a watch with no
interval/cron at all → Watch's must_have_schedule model_validator raises → the handler
did `HTTPException(400, detail=exc.errors())`, but pydantic v2 embeds the raw ValueError
object under ctx in errors(), FastAPI can't JSON-serialize it, and the intended 400
exploded into a 500. Fixed by (a) `_validation_detail` (JSON-safe error rendering) and
(b) `_backfill_schedule` (schedule-less creates default to every 30 min instead of
erroring at the user).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from web_watcher.dashboard.server import (
    _backfill_schedule,
    _normalize_turn,
    _validation_detail,
    create_app,
)


@pytest.fixture()
def client():
    manager = MagicMock()
    app = create_app(manager)
    return TestClient(app)


def _cleanup(client, name):
    client.delete(f"/api/watches/{name}")


def test_create_without_schedule_defaults_to_30_min(client):
    """A suggestion-shaped body (no interval, no cron) must create, not 500."""
    r = client.post("/api/watches", json={
        "name": "no schedule watch",
        "instruction": "look for trucks",
        "urls": ["https://craigslist.org"],
    })
    try:
        assert r.status_code == 201, r.text
        from web_watcher.config import load
        w = next(w for w in load().watches if w.name == "no schedule watch")
        assert w.interval_minutes == 30
    finally:
        _cleanup(client, "no schedule watch")


def test_invalid_watch_returns_400_not_500(client):
    """Model-validator failures must come back as a clean 400 with JSON detail."""
    r = client.post("/api/watches", json={
        "name": "bad idle",
        "instruction": "x",
        "urls": ["https://example.com"],
        "mode": "continuous",
        "continuous_idle_seconds": 0,   # trips must_have_schedule's ValueError
    })
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]        # must be JSON-serializable
    assert any("continuous_idle_seconds" in d["msg"] for d in detail)


def test_missing_urls_returns_400(client):
    r = client.post("/api/watches", json={"name": "no urls", "instruction": "x", "urls": []})
    assert r.status_code == 400, r.text
    r.json()  # serializable


def test_validation_detail_is_json_safe():
    import json

    from pydantic import ValidationError

    from web_watcher.config import Watch
    with pytest.raises(ValidationError) as ei:
        Watch.model_validate({"name": "x", "instruction": "y", "urls": ["https://e.com"]})
    detail = _validation_detail(ei.value)
    json.dumps(detail)  # must not raise
    assert all({"loc", "msg", "type"} <= set(d) for d in detail)


def test_backfill_schedule_defaults_create_only():
    assert _backfill_schedule({"name": "a"})["interval_minutes"] == 30
    # continuous watches need no schedule — untouched
    assert "interval_minutes" not in _backfill_schedule({"name": "a", "mode": "continuous"})
    # explicit schedules are preserved
    assert _backfill_schedule({"interval_minutes": 5})["interval_minutes"] == 5
    assert "interval_minutes" not in _backfill_schedule({"cron_expression": "0 * * * *"})


def test_normalize_turn_backfills_creates_not_updates():
    out = _normalize_turn({
        "message": "ok",
        "watch_suggestions": [
            {"name": "new one", "instruction": "x", "urls": ["https://e.com"]},
            {"name": "old one", "action": "update", "instruction": "y"},
        ],
    })
    create, update = out["watch_suggestions"]
    assert create["interval_minutes"] == 30
    assert "interval_minutes" not in update


def test_normalize_urls_propagates_zip_across_sites():
    """'vehicles in anacortes on craigslist and ebay' — the craigslist postal must
    localize the eBay search too (eBay gets _stpos from the sibling URL's zip)."""
    from web_watcher.dashboard.server import _normalize_marketplace_urls
    out, changes = _normalize_marketplace_urls([
        "https://seattle.craigslist.org/search/sss?query=vehicles+in+anacortes+under+10k",
        "https://www.ebay.com/sch/i.html?_nkw=vehicles",
    ])
    cl, eb = out
    assert "skagit.craigslist.org" in cl and "postal=" in cl
    assert "_stpos=" in eb and "_sadis=50" in eb
    assert changes  # both rewrites reported


def test_normalize_urls_fixes_offerup_fabricated_path():
    from web_watcher.dashboard.server import _normalize_marketplace_urls
    out, _ = _normalize_marketplace_urls(
        ["https://www.offerup.com/WA-Anacortes/search?q=vehicles&priceMax=10000"])
    assert out[0].startswith("https://offerup.com/search?")
    assert "price_max=10000" in out[0]
    assert "WA-Anacortes" not in out[0]
