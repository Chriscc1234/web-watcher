"""
Scheduler tests.

Pipeline tests mock all external calls (browser, Ollama, notifications)
so they run offline with no dependencies.

Live test (pytest -m live) runs the full pipeline end-to-end against the
real NWS weather page + Ollama.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from web_watcher.browser import PageResult
from web_watcher.config import Watch
from web_watcher.reasoning import ReasoningResult
from web_watcher.scheduler import WatchScheduler, _execute_watch
from web_watcher.storage import get_history, get_last_run, init_db


# ---------------------------------------------------------------------------
# Config fixture
# ---------------------------------------------------------------------------

WATCH_DEF = {
    "name": "test-watch",
    "enabled": True,
    "urls": ["https://example.com"],
    "interval_minutes": 5,
    "instruction": "Alert on snow.",
    "perception": "text",
    "notify": {"telegram": False, "email": False},
    "model_override": None,
    "click_path": [],
    # Simple (non-agent) mode: these pipeline tests mock run_watch + Reasoner and
    # must stay offline. Without this they'd default to autonomous=True and run the
    # real agent loop against a live Ollama (see module docstring).
    "autonomous": False,
}


@pytest.fixture
def config_file(tmp_path) -> Path:
    cfg = {
        "notifications": {"telegram": {"bot_token": "", "chat_id": ""}, "email": {}},
        "models": {"text_model": "mistral:latest", "vision_model": "moondream"},
        "watches": [WATCH_DEF],
    }
    p = tmp_path / "config.yaml"
    with p.open("w") as f:
        yaml.dump(cfg, f)
    return p


@pytest.fixture
def db(tmp_path) -> Path:
    path = tmp_path / "test.db"
    init_db(path)
    return path


# ---------------------------------------------------------------------------
# WatchScheduler lifecycle
# ---------------------------------------------------------------------------

def test_scheduler_starts_and_stops(config_file, db):
    s = WatchScheduler(config_path=config_file, db_path=db)
    s.start()
    assert s._apscheduler.running
    s.stop()
    assert not s._apscheduler.running


def test_scheduler_creates_job_per_enabled_watch(config_file, db):
    s = WatchScheduler(config_path=config_file, db_path=db)
    s.start()
    jobs = s._apscheduler.get_jobs()
    assert any(j.id == "test-watch" for j in jobs)
    s.stop()


def test_disabled_watch_not_scheduled(tmp_path, db):
    cfg = {
        "models": {"text_model": "m", "vision_model": "v"},
        "watches": [{**WATCH_DEF, "enabled": False}],
    }
    p = tmp_path / "config.yaml"
    with p.open("w") as f:
        yaml.dump(cfg, f)

    s = WatchScheduler(config_path=p, db_path=db)
    s.start()
    jobs = s._apscheduler.get_jobs()
    assert not any(j.id == "test-watch" for j in jobs)
    s.stop()


def test_get_job_info_returns_next_run(config_file, db):
    s = WatchScheduler(config_path=config_file, db_path=db)
    s.start()
    info = s.get_job_info()
    assert len(info) >= 1
    assert info[0]["watch_name"] == "test-watch"
    assert info[0]["next_run_utc"] is not None
    s.stop()


def test_reload_reschedules_jobs(config_file, db):
    s = WatchScheduler(config_path=config_file, db_path=db)
    s.start()
    s.reload()
    jobs = s._apscheduler.get_jobs()
    assert any(j.id == "test-watch" for j in jobs)
    s.stop()


# ---------------------------------------------------------------------------
# _execute_watch pipeline (all external calls mocked)
# ---------------------------------------------------------------------------

def _make_page_result(text="Some weather content " * 20):
    return PageResult(url="https://example.com", text=text)


def _good_reasoning():
    return ReasoningResult(
        found=True, summary="Winter storm warning", confidence="high", link=None
    )


def _no_match_reasoning():
    return ReasoningResult(
        found=False, summary="No warnings", confidence="high", link=None
    )


@patch("web_watcher.scheduler.BrowserSession")
@patch("web_watcher.scheduler.Reasoner")
@patch("web_watcher.scheduler.send_notifications")
def test_execute_watch_saves_found_record(mock_notify, mock_reasoner_cls, mock_browser_cls,
                                          config_file, db):
    mock_browser_cls.return_value.__enter__.return_value.run_watch.return_value = [_make_page_result()]
    mock_reasoner_cls.return_value.analyse_text.return_value = _good_reasoning()

    _execute_watch("test-watch", config_file, db)

    rows = get_history(watch_name="test-watch", db_path=db)
    assert len(rows) == 1
    assert rows[0]["found"] == 1
    assert rows[0]["summary"] == "Winter storm warning"
    mock_notify.assert_called_once()


@patch("web_watcher.scheduler.BrowserSession")
@patch("web_watcher.scheduler.Reasoner")
@patch("web_watcher.scheduler.send_notifications")
def test_execute_watch_no_notify_when_not_found(mock_notify, mock_reasoner_cls, mock_browser_cls,
                                                 config_file, db):
    mock_browser_cls.return_value.__enter__.return_value.run_watch.return_value = [_make_page_result()]
    mock_reasoner_cls.return_value.analyse_text.return_value = _no_match_reasoning()

    _execute_watch("test-watch", config_file, db)

    mock_notify.assert_not_called()
    rows = get_history(watch_name="test-watch", db_path=db)
    assert rows[0]["found"] == 0


@patch("web_watcher.scheduler.BrowserSession")
@patch("web_watcher.scheduler.Reasoner")
def test_execute_watch_browser_error_saved_to_history(mock_reasoner_cls, mock_browser_cls,
                                                       config_file, db):
    error_result = PageResult(url="https://example.com", text="", error="Navigation timeout")
    mock_browser_cls.return_value.__enter__.return_value.run_watch.return_value = [error_result]

    _execute_watch("test-watch", config_file, db)

    rows = get_history(watch_name="test-watch", db_path=db)
    assert rows[0]["error"] is not None
    assert "browser" in rows[0]["error"]
    mock_reasoner_cls.assert_not_called()


@patch("web_watcher.scheduler.BrowserSession")
@patch("web_watcher.scheduler.Reasoner")
def test_execute_watch_ollama_error_saved_to_history(mock_reasoner_cls, mock_browser_cls,
                                                      config_file, db):
    from web_watcher.reasoning import OllamaUnavailableError
    mock_browser_cls.return_value.__enter__.return_value.run_watch.return_value = [_make_page_result()]
    mock_reasoner_cls.return_value.analyse_text.side_effect = OllamaUnavailableError("not running")

    _execute_watch("test-watch", config_file, db)

    rows = get_history(watch_name="test-watch", db_path=db)
    assert "ollama" in rows[0]["error"]


def test_execute_watch_unknown_name_does_not_crash(config_file, db):
    _execute_watch("no-such-watch", config_file, db)
    assert get_history(db_path=db) == []


# ---------------------------------------------------------------------------
# run_now triggers a job
# ---------------------------------------------------------------------------

@patch("web_watcher.scheduler._execute_watch")
def test_run_now_adds_job(mock_execute, config_file, db):
    s = WatchScheduler(config_path=config_file, db_path=db)
    s.start()
    s.run_now("test-watch")
    time.sleep(0.5)   # let the thread pool pick it up
    s.stop()
    mock_execute.assert_called()


# ---------------------------------------------------------------------------
# Live test — full end-to-end pipeline
# ---------------------------------------------------------------------------

@pytest.mark.live
def test_live_full_pipeline(tmp_path):
    """Full pipeline: real browser + real Ollama + SQLite. No actual notification sent."""
    import yaml

    cfg = {
        "notifications": {"telegram": {"bot_token": "", "chat_id": ""}, "email": {}},
        "models": {"text_model": "mistral:latest", "vision_model": "moondream"},
        "watches": [{
            "name": "nws-live",
            "enabled": True,
            "urls": [
                "https://forecast.weather.gov/MapClick.php"
                "?CityName=Seattle&state=WA&site=SEW"
                "&textField1=47.6062&textField2=-122.3321"
            ],
            "interval_minutes": 60,
            "instruction": "Is there any severe weather warning, winter storm, or frost advisory?",
            "perception": "auto",
            "notify": {"telegram": False, "email": False},
            "model_override": None,
            "click_path": [],
        }],
    }
    config_path = tmp_path / "config.yaml"
    with config_path.open("w") as f:
        yaml.dump(cfg, f)

    db_path = tmp_path / "history.db"
    init_db(db_path)

    _execute_watch("nws-live", config_path, db_path)

    rows = get_history(watch_name="nws-live", db_path=db_path)
    assert len(rows) == 1, "Expected exactly one history record"
    row = rows[0]

    print(f"\nfound={row['found']} confidence={row['confidence']}")
    print(f"summary={row['summary']}")
    print(f"perception={row['perception_mode_used']} error={row['error']}")

    assert row["error"] is None, f"Pipeline error: {row['error']}"
    assert row["perception_mode_used"] in ("text", "vision")
    assert row["confidence"] in ("high", "medium", "low")
    assert row["summary"]


# ---------------------------------------------------------------------------
# Continuous sweep shared pipeline (_process_sweep_listings) — used by BOTH the
# scraper sweep and the agent-driven sweep, so prime/dedup must behave identically.
# ---------------------------------------------------------------------------

from web_watcher.config import AppConfig
from web_watcher.monitor import Listing
from web_watcher.scheduler import (
    _process_sweep_listings, _exploration_plan, _jittered_idle,
)
from web_watcher.storage import count_seen_listings, has_seen_listing, query_listings


def _cont_watch(**over) -> Watch:
    base = {
        "name": "cont-watch",
        "enabled": True,
        "urls": ["https://www.facebook.com/marketplace/",
                 "https://www.facebook.com/marketplace/category/vehicles"],
        "instruction": "4x4 trucks",
        "mode": "continuous",
        "autonomous": True,
        "notify": {"telegram": False, "email": False},
    }
    base.update(over)
    return Watch.model_validate(base)


def _listing(n: int) -> Listing:
    # Distinct multi-token titles so each has its own content fingerprint (toy titles
    # like "Truck 1"/"Truck 2" collapse to one fingerprint once the single digit is
    # filtered as noise — correct for near-dupes, but not what these tests exercise).
    return Listing(key=f"fb:{n}", url=f"https://fb/item/{n}",
                   title=f"20{10+n} Ford F{n}50 pickup truck", price=f"${1000*n}")


def test_baseline_records_matches_to_results_without_alerting(db):
    # A brand-new watch's first sweep finds a big backlog. It must NOT notify, but it SHOULD
    # judge and record matches so Results isn't empty (the "found lots, shows nothing" bug).
    import web_watcher.scheduler as sch
    cfg = AppConfig.model_validate({})
    watch = _cont_watch(judgment_prompt="only diesels")
    batch = [_listing(n) for n in range(1, 6)]   # 5 listings
    with patch.object(sch, "_alert_new_listings") as mock_alert, \
         patch.object(sch, "_filter_listings_by_judgment", return_value=[batch[0], batch[2]]):
        _process_sweep_listings(watch, cfg, db, 0, batch, "t0", mode_label="continuous")
    mock_alert.assert_not_called()                       # no notifications on the backlog
    assert count_seen_listings("cont-watch", db) == 5    # whole batch baselined
    matched = {r["listing_key"] for r in query_listings(watch_id=watch.id or watch.name,
                                                         matched=True, db_path=db)}
    assert matched == {"fb:1", "fb:3"}                   # judged matches DID reach Results


def test_process_sweep_primes_then_dedups(db):
    cfg = AppConfig.model_validate({})
    watch = _cont_watch()
    with patch("web_watcher.scheduler._alert_new_listings") as mock_alert:
        mock_alert.return_value = 0
        # First sweep ever → priming: records a baseline, alerts nothing.
        _process_sweep_listings(watch, cfg, db, 0, [_listing(1), _listing(2), _listing(3)],
                                "2026-06-22T00:00:00", mode_label="continuous-agent")
        mock_alert.assert_not_called()
        assert count_seen_listings("cont-watch", db) == 3

        # Second sweep: 3 seen + 1 new → only the new one reaches the alerter.
        mock_alert.return_value = 1
        _process_sweep_listings(watch, cfg, db, 1,
                                [_listing(1), _listing(2), _listing(3), _listing(4)],
                                "2026-06-22T00:01:00", mode_label="continuous-agent")
        mock_alert.assert_called_once()
        alerted_arg = mock_alert.call_args[0][2]   # (watch, cfg, matched, ...)
        assert [l.key for l in alerted_arg] == ["fb:4"]


def test_process_sweep_empty_listings_records_no_match(db):
    cfg = AppConfig.model_validate({})
    with patch("web_watcher.scheduler._alert_new_listings") as mock_alert:
        _process_sweep_listings(_cont_watch(), cfg, db, 0, [], "2026-06-22T00:00:00")
        mock_alert.assert_not_called()
    rows = get_history(watch_name="cont-watch", db_path=db)
    assert rows[0]["found"] == 0


def test_exploration_plan_uses_watch_urls_and_known_style():
    watch = _cont_watch()
    for i in range(6):
        plan = _exploration_plan(i, watch)
        assert plan["start_url"] in watch.urls
        assert plan["style"] in {"scroll", "category", "search", "sort", "filter"}
    # Rotates the start URL across sweeps (2 urls → alternates).
    assert _exploration_plan(0, watch)["start_url"] != _exploration_plan(1, watch)["start_url"]


def test_jittered_idle_within_bounds():
    for _ in range(50):
        v = _jittered_idle(40)
        assert 1.0 <= v <= 40 + 0.5 * 40
    assert _jittered_idle(0) >= 1.0   # never sleeps zero


# ---------------------------------------------------------------------------
# run_agent on_step harvest hook fires (mocked agent internals)
# ---------------------------------------------------------------------------

def test_run_agent_on_step_fires():
    import web_watcher.agent as agent_mod
    from web_watcher.agent import run_agent, AgentAction

    done = AgentAction(thought="done", action="done", summary="finished")
    calls = []

    with patch.object(agent_mod, "_emit_focus_events", lambda p: None), \
         patch.object(agent_mod, "_detect_captcha", lambda p: False), \
         patch.object(agent_mod, "has_blocking_overlay", lambda p: False), \
         patch.object(agent_mod, "_snapshot_elements", lambda p: [{"index": 0, "tag": "a",
                      "label": "x", "href": "", "type": "", "inViewport": True}]), \
         patch.object(agent_mod, "_query_llm", lambda *a, **k: done), \
         patch.object(agent_mod, "_extract_text", lambda p: ""), \
         patch.object(agent_mod, "_human_pause", lambda *a, **k: None):
        page = MagicMock()
        page.url = "https://example.com"
        page.title.return_value = "t"
        run_agent(page, "browse", model="m", max_steps=3,
                  on_step=lambda pg: calls.append(1))

    # At minimum the initial pre-loop harvest fires.
    assert len(calls) >= 1


def test_run_agent_blocks_repeated_search():
    """The same search term is only executed once; repeats are rejected (anti-thrash)."""
    import web_watcher.agent as agent_mod
    from web_watcher.agent import run_agent, AgentAction

    exec_mock = MagicMock()
    with patch.object(agent_mod, "_emit_focus_events", lambda p: None), \
         patch.object(agent_mod, "_detect_captcha", lambda p: False), \
         patch.object(agent_mod, "has_blocking_overlay", lambda p: False), \
         patch.object(agent_mod, "_snapshot_elements", lambda p: [{"index": 0, "tag": "input",
                      "label": "search", "href": "", "type": "text", "inViewport": True}]), \
         patch.object(agent_mod, "_query_llm",
                      lambda *a, **k: AgentAction(thought="s", action="type", element_index=0, text="4x4 truck")), \
         patch.object(agent_mod, "_execute", exec_mock), \
         patch.object(agent_mod, "_extract_text", lambda p: ""), \
         patch.object(agent_mod, "_action_outcome", lambda *a, **k: "ok"), \
         patch.object(agent_mod, "_human_pause", lambda *a, **k: None):
        page = MagicMock()
        page.url = "https://seattle.craigslist.org/search/cta?query=4x4"
        page.title.return_value = "t"
        run_agent(page, "browse", model="m", max_steps=6)

    assert exec_mock.call_count == 1   # first search runs; the 5 repeats are rejected


def test_run_agent_should_stop_halts_before_acting():
    """should_stop returning True must end the browse before any LLM/action runs."""
    import web_watcher.agent as agent_mod
    from web_watcher.agent import run_agent

    llm = MagicMock()
    with patch.object(agent_mod, "_emit_focus_events", lambda p: None), \
         patch.object(agent_mod, "_detect_captcha", lambda p: False), \
         patch.object(agent_mod, "has_blocking_overlay", lambda p: False), \
         patch.object(agent_mod, "_snapshot_elements", lambda p: [{"index": 0}]), \
         patch.object(agent_mod, "_query_llm", llm), \
         patch.object(agent_mod, "_extract_text", lambda p: ""), \
         patch.object(agent_mod, "_human_pause", lambda *a, **k: None):
        page = MagicMock()
        page.url = "https://www.facebook.com/login/"
        page.title.return_value = "Log in"
        run_agent(page, "browse", model="m", max_steps=5,
                  should_stop=lambda pg: "/login" in pg.url)

    llm.assert_not_called()   # guardrail tripped before the model was ever consulted


def test_capture_listing_bodies_sets_details(monkeypatch):
    import web_watcher.scheduler as sch
    tab = MagicMock()
    page = MagicMock()
    page.context.new_page.return_value = tab
    monkeypatch.setattr(sch, "extract_listing_body", lambda t: "transmission: manual, 4x4")
    items = [_listing(1), _listing(2)]
    sch._capture_listing_bodies(page, items)
    assert all(l.details == "transmission: manual, 4x4" for l in items)
    assert page.context.new_page.call_count == 2
    assert tab.close.call_count == 2          # every tab is closed


def test_capture_listing_bodies_respects_stop(monkeypatch):
    import threading
    import web_watcher.scheduler as sch
    ev = threading.Event(); ev.set()
    page = MagicMock()
    sch._capture_listing_bodies(page, [_listing(1)], stop_event=ev)
    page.context.new_page.assert_not_called()   # stopped before opening any tab


def test_process_sweep_deep_reads_new_listings(db, monkeypatch):
    """Agent-sweep path (fetch_bodies=True) deep-reads new listings before judging."""
    import web_watcher.scheduler as sch
    cfg = AppConfig.model_validate({})
    watch = _cont_watch()
    # prime first so the next call has a non-priming 'new' set
    with patch.object(sch, "_alert_new_listings", return_value=0):
        sch._process_sweep_listings(watch, cfg, db, 0, [_listing(1)], "t0")
    captured = {}
    monkeypatch.setattr(sch, "_capture_listing_bodies",
                        lambda page, items, stop_event=None: captured.update({"n": len(items)}))
    with patch.object(sch, "_alert_new_listings", return_value=1), \
         patch.object(sch, "_filter_listings_by_judgment", side_effect=lambda nl, w, c, **k: nl):
        sch._process_sweep_listings(watch, cfg, db, 1, [_listing(1), _listing(2)], "t1",
                                    mode_label="continuous-agent",
                                    page=MagicMock(), fetch_bodies=True)
    assert captured.get("n") == 1   # only the one NEW listing (fb:2) gets deep-read


def test_process_sweep_suppresses_reposts(db):
    """A new listing-id whose content matches one already seen is a repost — not alerted."""
    import web_watcher.scheduler as sch
    cfg = AppConfig.model_validate({})
    watch = _cont_watch()
    with patch.object(sch, "_alert_new_listings", return_value=0):
        sch._process_sweep_listings(watch, cfg, db, 0, [_listing(1)], "t0")   # prime fb:1

    repost = Listing(key="fb:99", url="https://fb/item/99",
                     title=_listing(1).title, price=_listing(1).price)        # same item, new id
    with patch.object(sch, "_alert_new_listings", return_value=1) as mock_alert, \
         patch.object(sch, "_filter_listings_by_judgment", side_effect=lambda nl, w, c, **k: nl):
        sch._process_sweep_listings(watch, cfg, db, 1, [_listing(1), repost, _listing(2)], "t1",
                                    mode_label="continuous-agent")
    alerted = mock_alert.call_args[0][2]
    assert [l.key for l in alerted] == ["fb:2"]      # repost fb:99 suppressed, fb:2 fresh


# ---------------------------------------------------------------------------
# Cross-watch matching: a fresh find from one watch is offered to the others, so a
# Corvette the truck watch stumbles on still reaches the sports-car watch.
# ---------------------------------------------------------------------------

def _two_watch_cfg(truck_over=None, cars_over=None, **cfg_over) -> tuple:
    truck = _cont_watch(name="trucks", id="idA", instruction="4x4 trucks",
                        urls=["https://x.org/search/cta?query=4x4+truck"],
                        autonomous=False, **(truck_over or {}))
    cars  = _cont_watch(name="cars", id="idB", instruction="manual sports cars",
                        urls=["https://x.org/search/cta?query=corvette"],
                        judgment_prompt="manual sports cars only", autonomous=False,
                        **(cars_over or {}))
    cfg = AppConfig.model_validate(
        {"watches": [truck.model_dump(), cars.model_dump()], **cfg_over})
    return truck, cars, cfg


def _corvette(key="cl:vette") -> Listing:
    return Listing(key=key, url=f"https://x.org/item/{key}",
                   title="2015 Chevrolet Corvette Stingray manual", price="$45000")


def test_cross_watch_offers_fresh_finds_to_other_watch(db):
    import web_watcher.scheduler as sch
    truck, cars, cfg = _two_watch_cfg()
    # Prime BOTH watches so neither is in priming mode.
    with patch.object(sch, "_alert_new_listings", return_value=0):
        sch._process_sweep_listings(cars, cfg, db, 0, [_listing(7)], "t0")
        sch._process_sweep_listings(truck, cfg, db, 0, [_listing(1)], "t0")

    vette = _corvette()
    # Truck sweep finds the corvette as fresh (truck has no judge → alerts all, mocked).
    # Cross-watch then offers it to the cars watch, whose judge (mocked to accept) records
    # and alerts it under the cars watch.
    with patch.object(sch, "_alert_new_listings", return_value=1) as mock_alert, \
         patch.object(sch, "_filter_listings_by_judgment", side_effect=lambda nl, w, c, **k: nl):
        sch._process_sweep_listings(truck, cfg, db, 1, [_listing(1), vette], "t1")

    # The cars watch now owns an observation for the corvette and has it marked seen.
    assert has_seen_listing("cars", "cl:vette", db)
    car_matches = query_listings(watch_id="idB", matched=True, db_path=db)
    assert any(r["listing_key"] == "cl:vette" for r in car_matches)
    # ...and the cross-watch alert fired under the CARS watch, not just trucks.
    alerted_names = [c.args[0].name for c in mock_alert.call_args_list]
    assert "cars" in alerted_names


def test_cross_watch_respects_disable_flag(db):
    import web_watcher.scheduler as sch
    truck, cars, cfg = _two_watch_cfg(cross_watch_matching=False)
    with patch.object(sch, "_alert_new_listings", return_value=0):
        sch._process_sweep_listings(cars, cfg, db, 0, [_listing(7)], "t0")
        sch._process_sweep_listings(truck, cfg, db, 0, [_listing(1)], "t0")

    vette = _corvette("cl:v2")
    with patch.object(sch, "_alert_new_listings", return_value=1), \
         patch.object(sch, "_filter_listings_by_judgment", side_effect=lambda nl, w, c, **k: nl):
        sch._process_sweep_listings(truck, cfg, db, 1, [_listing(1), vette], "t1")

    assert not has_seen_listing("cars", "cl:v2", db)   # flag off → nothing leaks across


def test_cross_watch_skips_unprimed_other(db):
    import web_watcher.scheduler as sch
    truck, cars, cfg = _two_watch_cfg()
    # Prime ONLY trucks; the cars watch has never established its baseline.
    with patch.object(sch, "_alert_new_listings", return_value=0):
        sch._process_sweep_listings(truck, cfg, db, 0, [_listing(1)], "t0")

    vette = _corvette("cl:v3")
    judged: list = []
    with patch.object(sch, "_alert_new_listings", return_value=1), \
         patch.object(sch, "_filter_listings_by_judgment",
                      side_effect=lambda nl, w, c, **k: judged.append(w.name) or nl):
        sch._process_sweep_listings(truck, cfg, db, 1, [_listing(1), vette], "t1")

    assert "cars" not in judged                        # unprimed → never judged
    assert not has_seen_listing("cars", "cl:v3", db)


def test_judgment_filter_fail_closed_vs_open(monkeypatch):
    """On a judge error: a watch's own sweep fails OPEN (keep all), cross-watch fails CLOSED
    (keep none) — so an LLM hiccup can't leak un-judged listings into another watch."""
    import web_watcher.scheduler as sch
    import httpx
    cfg = AppConfig.model_validate({})
    watch = _cont_watch(judgment_prompt="trucks only")
    listings = [_listing(1), _listing(2)]

    class Boom:                       # any Ollama call explodes
        def __init__(self, *a, **k): pass
        def __enter__(self): raise RuntimeError("ollama down")
        def __exit__(self, *a): return False
    monkeypatch.setattr(httpx, "Client", Boom)

    assert sch._filter_listings_by_judgment(listings, watch, cfg) == listings          # fail-open
    assert sch._filter_listings_by_judgment(listings, watch, cfg, fail_closed=True) == []  # fail-closed


def test_cross_watch_skips_when_judge_errors(db, monkeypatch):
    """A judge error during cross-watch must NOT inject the source's listings into the other
    watch (this is the 'sports car in the trucks results' bug)."""
    import web_watcher.scheduler as sch
    import httpx
    truck, cars, cfg = _two_watch_cfg()
    with patch.object(sch, "_alert_new_listings", return_value=0):
        sch._process_sweep_listings(cars, cfg, db, 0, [_listing(7)], "t0")
        sch._process_sweep_listings(truck, cfg, db, 0, [_listing(1)], "t0")

    class Boom:
        def __init__(self, *a, **k): pass
        def __enter__(self): raise RuntimeError("ollama down")
        def __exit__(self, *a): return False
    monkeypatch.setattr(httpx, "Client", Boom)

    vette = _corvette("cl:err")
    with patch.object(sch, "_alert_new_listings", return_value=1):
        sch._process_sweep_listings(truck, cfg, db, 1, [_listing(1), vette], "t1")

    # cars (the OTHER watch) must have NO match for the corvette — cross-watch failed closed.
    assert not any(r["listing_key"] == "cl:err"
                   for r in query_listings(watch_id="idB", matched=True, db_path=db))


def test_registrable_domain():
    from web_watcher.scheduler import _registrable_domain
    assert _registrable_domain("https://www.facebook.com/marketplace/") == "facebook.com"
    assert _registrable_domain("https://www.threads.com/") == "threads.com"
    assert _registrable_domain("https://www.facebook.com/login/?next=x") == "facebook.com"
    assert _registrable_domain("not a url") == "not a url" or _registrable_domain("not a url") == ""


def test_update_blind_streak_escalates_after_two_empty():
    from web_watcher.scheduler import _update_blind_streak, _SCRAPER_BLIND_THRESHOLD
    s = 0
    s, esc = _update_blind_streak(0, s); assert s == 1 and not esc          # first empty
    s, esc = _update_blind_streak(0, s); assert s == 2 and esc              # second empty → escalate
    # A productive sweep resets the streak.
    s, esc = _update_blind_streak(7, s); assert s == 0 and not esc
    # A sweep that couldn't run (-1) is NOT evidence of blindness — streak unchanged.
    s = 1
    s, esc = _update_blind_streak(-1, s); assert s == 1 and not esc
    assert _SCRAPER_BLIND_THRESHOLD == 2


# ── #91/#92: keyword prefilter + 1-5 rating judge ──────────────────────────

from web_watcher.scheduler import _keyword_prefilter, _filter_listings_by_judgment


def _kw_listing(n, title, details=""):
    l = Listing(key=f"k:{n}", url=f"https://x/{n}", title=title, price="$100")
    l.details = details
    return l


def test_antikeyword_drops_listing():
    w = _cont_watch(antikeywords=["parts", "salvage"])
    kept, dropped = _keyword_prefilter(
        [_kw_listing(1, "Ford F150 truck"),
         _kw_listing(2, "F150 for parts only"),
         _kw_listing(3, "clean truck", details="salvage title")], w)
    assert [l.key for l in kept] == ["k:1"]
    assert {l.key for l in dropped} == {"k:2", "k:3"}
    assert "parts" in dropped[0].judge_reason


def test_required_keyword_must_be_present():
    w = _cont_watch(keywords=["4x4", "4wd"])
    kept, dropped = _keyword_prefilter(
        [_kw_listing(1, "F150 4x4 truck"),
         _kw_listing(2, "F150 2wd truck"),
         _kw_listing(3, "truck", details="has 4WD drivetrain")], w)
    assert {l.key for l in kept} == {"k:1", "k:3"}    # title OR details
    assert [l.key for l in dropped] == ["k:2"]


def test_no_keywords_configured_is_passthrough():
    w = _cont_watch()
    listings = [_kw_listing(1, "anything")]
    kept, dropped = _keyword_prefilter(listings, w)
    assert kept == listings and dropped == []


def _rating_reply(ratings):
    """Mock Ollama returning a ratings array."""
    import json as _json
    payload = {"message": {"content": _json.dumps({"ratings": ratings})}}

    class _R:
        def raise_for_status(self): pass
        def json(self): return payload
    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **k): return _R()
    return _C


def test_rating_judge_keeps_at_or_above_threshold(monkeypatch):
    import web_watcher.scheduler as sch
    cfg = AppConfig.model_validate({})
    w = _cont_watch(judgment_prompt="diesel only", min_rating=4)
    listings = [_kw_listing(i, f"item {i}") for i in range(4)]
    monkeypatch.setattr("httpx.Client", _rating_reply([
        {"i": 0, "r": 5, "why": "great deal"},
        {"i": 1, "r": 4, "why": "good"},
        {"i": 2, "r": 3, "why": "meh"},
        {"i": 3, "r": 1, "why": "wrong item"},
    ]))
    kept = _filter_listings_by_judgment(listings, w, cfg)
    assert {l.key for l in kept} == {"k:0", "k:1"}     # >= 4
    assert listings[0].rating == 5 and listings[0].judge_reason == "great deal"
    assert listings[3].rating == 1                      # ratings attached even when dropped


def test_rating_threshold_lowered_lets_more_through(monkeypatch):
    import web_watcher.scheduler as sch
    cfg = AppConfig.model_validate({})
    w = _cont_watch(judgment_prompt="x", min_rating=2)
    listings = [_kw_listing(i, f"item {i}") for i in range(3)]
    monkeypatch.setattr("httpx.Client", _rating_reply([
        {"i": 0, "r": 2, "why": ""}, {"i": 1, "r": 3, "why": ""}, {"i": 2, "r": 1, "why": ""},
    ]))
    kept = _filter_listings_by_judgment(listings, w, cfg)
    assert {l.key for l in kept} == {"k:0", "k:1"}     # 2 and 3 pass, 1 drops


def _rating_reply_seq(responses):
    """Mock Ollama returning a DIFFERENT ratings array per successive call (first pass,
    then the retry of unrated items). Runs out → empty ratings."""
    import json as _json
    calls = {"n": 0}

    class _R:
        def __init__(self, ratings): self._p = {"message": {"content": _json.dumps({"ratings": ratings})}}
        def raise_for_status(self): pass
        def json(self): return self._p
    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **k):
            i = calls["n"]; calls["n"] += 1
            return _R(responses[i] if i < len(responses) else [])
    return _C


def test_unrated_item_is_re_judged_then_kept(monkeypatch):
    # An item the model skips on the first pass is re-judged; a passing retry rating keeps it.
    cfg = AppConfig.model_validate({})
    w = _cont_watch(judgment_prompt="x", min_rating=3)
    listings = [_kw_listing(i, f"item {i}") for i in range(3)]
    # Pass 1 rates only local#0 (=k:0). Retry gets [k:1,k:2] as local #0,#1 → both good.
    monkeypatch.setattr("httpx.Client", _rating_reply_seq([
        [{"i": 0, "r": 5, "why": "top"}],
        [{"i": 0, "r": 4, "why": "ok"}, {"i": 1, "r": 4, "why": "ok"}],
    ]))
    kept = _filter_listings_by_judgment(listings, w, cfg)
    assert {l.key for l in kept} == {"k:0", "k:1", "k:2"}


def test_still_unrated_after_retry_is_treated_as_non_match(monkeypatch):
    # The dresser fix: an item unrated even after the retry is DROPPED, not given a free pass.
    cfg = AppConfig.model_validate({})
    w = _cont_watch(judgment_prompt="x", min_rating=3)
    listings = [_kw_listing(i, f"item {i}") for i in range(3)]
    # Pass 1 rates only k:0; the retry also returns nothing for the rest.
    monkeypatch.setattr("httpx.Client", _rating_reply_seq([
        [{"i": 0, "r": 5, "why": "top"}],
        [],
    ]))
    kept = _filter_listings_by_judgment(listings, w, cfg)
    assert {l.key for l in kept} == {"k:0"}
    assert listings[1].rating < 3 and listings[2].rating < 3   # recorded as non-match, not vanished


def test_min_rating_clamped_to_1_5():
    assert _cont_watch(min_rating=9).min_rating == 5
    assert _cont_watch(min_rating=0).min_rating == 1


# ── #78: Facebook safety harness wiring ────────────────────────────────────

def test_fb_checkpoint_on_landing_stops_and_backs_off(db, monkeypatch):
    """Landing on a Facebook checkpoint must alert + set a cooldown + NOT process listings."""
    import web_watcher.scheduler as sch
    cfg = AppConfig.model_validate({})
    watch = _cont_watch(name="FB Trucks",
                        urls=["https://www.facebook.com/marketplace/seattle/search?query=truck"],
                        use_login_profile=True)

    class _Page:
        url = "https://www.facebook.com/marketplace/seattle/search?query=truck"
        def goto(self, *a, **k): pass
        def inner_text(self, *a, **k): return "We've temporarily restricted your account"

    called = {"checkpoint": False, "process": False}
    monkeypatch.setattr(sch, "maybe_warm_homepage", lambda *a, **k: None)
    monkeypatch.setattr(sch, "dismiss_popups", lambda *a, **k: 0)
    monkeypatch.setattr(sch, "is_login_wall", lambda *a, **k: False)
    monkeypatch.setattr(sch, "_handle_fb_checkpoint",
                        lambda *a, **k: called.__setitem__("checkpoint", True))
    monkeypatch.setattr(sch, "_process_sweep_listings",
                        lambda *a, **k: called.__setitem__("process", True))

    sch._run_agent_continuous_sweep(watch, cfg, db, 0, _Page())
    assert called["checkpoint"] is True
    assert called["process"] is False   # a flagged session's listings are NOT processed


def test_fb_cooldown_skips_the_sweep(db, monkeypatch):
    import web_watcher.scheduler as sch
    import time as _t
    cfg = AppConfig.model_validate({})
    watch = _cont_watch(name="FB Boats",
                        urls=["https://www.facebook.com/marketplace/seattle/search?query=boat"])
    sch._FB_COOLDOWN[watch.name] = _t.time() + 3600   # on cooldown

    ran = {"agent": False}
    monkeypatch.setattr(sch, "maybe_warm_homepage",
                        lambda *a, **k: ran.__setitem__("agent", True))
    try:
        sch._run_agent_continuous_sweep(watch, cfg, db, 0, object())
    finally:
        sch._FB_COOLDOWN.pop(watch.name, None)
    assert ran["agent"] is False   # skipped before touching the browser


def test_non_facebook_watch_ignores_cooldown_map(db, monkeypatch):
    """A craigslist watch is never affected by the FB cooldown machinery."""
    import web_watcher.scheduler as sch
    from web_watcher import fb_safety
    assert not fb_safety.is_facebook("https://seattle.craigslist.org/search/cta")


# ── #94/#86: interval jitter + visible-cursor default ──────────────────────

def test_interval_jobs_get_jitter(config_file, db):
    """A scheduled watch fires on a jittered interval (anti-pattern), not a perfect clock."""
    s = WatchScheduler(config_path=config_file, db_path=db)
    s.start()
    job = next(j for j in s._apscheduler.get_jobs() if j.id == "test-watch")
    # WATCH_DEF is interval_minutes=5 → jitter = min(300, 5*60*0.2) = 60s
    assert getattr(job.trigger, "jitter", None) == 60
    s.stop()
