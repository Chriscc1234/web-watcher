"""Round-trip and validation tests for the config schema."""

import copy
import tempfile
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from web_watcher.config import (
    AppConfig,
    Watch,
    ClickStep,
    load,
    save,
    round_trip,
)


MINIMAL_WATCH = {
    "name": "Test watch",
    "urls": ["https://example.com"],
    "interval_minutes": 5,
    "instruction": "Alert me if anything changes.",
}


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def test_minimal_watch_valid():
    cfg = AppConfig.model_validate({"watches": [MINIMAL_WATCH]})
    assert cfg.watches[0].name == "Test watch"
    assert cfg.watches[0].perception == "auto"


def test_watch_requires_schedule():
    bad = {**MINIMAL_WATCH}
    del bad["interval_minutes"]
    with pytest.raises(ValidationError, match="interval_minutes.*cron_expression"):
        AppConfig.model_validate({"watches": [bad]})


def test_watch_rejects_both_schedule_fields():
    bad = {**MINIMAL_WATCH, "cron_expression": "*/5 * * * *"}
    with pytest.raises(ValidationError, match="not specify both"):
        AppConfig.model_validate({"watches": [bad]})


def test_watch_cron_expression_valid():
    w = {**MINIMAL_WATCH, "cron_expression": "0 8 * * *"}
    del w["interval_minutes"]
    cfg = AppConfig.model_validate({"watches": [w]})
    assert cfg.watches[0].cron_expression == "0 8 * * *"


def test_watch_invalid_perception():
    bad = {**MINIMAL_WATCH, "perception": "magic"}
    with pytest.raises(ValidationError, match="perception"):
        AppConfig.model_validate({"watches": [bad]})


def test_click_step_scroll_requires_amount():
    with pytest.raises(ValidationError, match="amount"):
        ClickStep.model_validate({"action": "scroll"})


def test_click_step_click_requires_target():
    with pytest.raises(ValidationError, match="target"):
        ClickStep.model_validate({"action": "click"})


def test_click_step_unknown_action():
    with pytest.raises(ValidationError, match="Unknown click-path action"):
        ClickStep.model_validate({"action": "hover", "target": "#foo"})


def test_defaults_populated():
    cfg = AppConfig.model_validate({})
    assert cfg.models.text_model == "qwen2.5:7b"
    # council_model is unset by default and falls back to text_model
    assert cfg.models.council_model == ""
    assert cfg.models.effective_council_model == "qwen2.5:7b"
    assert cfg.notifications.email.smtp_port == 587


# ---------------------------------------------------------------------------
# Round-trip (load -> save -> reload)
# ---------------------------------------------------------------------------

def _write_yaml(path: Path, data: dict) -> None:
    with path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def test_round_trip_preserves_watch_name(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    _write_yaml(cfg_path, {"watches": [MINIMAL_WATCH]})

    reloaded = round_trip(cfg_path)
    assert reloaded.watches[0].name == "Test watch"


def test_round_trip_preserves_click_path(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    watch = {
        **MINIMAL_WATCH,
        "click_path": [
            {"action": "click", "target": "text=More"},
            {"action": "scroll", "amount": 500},
        ],
    }
    _write_yaml(cfg_path, {"watches": [watch]})

    reloaded = round_trip(cfg_path)
    assert len(reloaded.watches[0].click_path) == 2
    assert reloaded.watches[0].click_path[1].amount == 500


def test_round_trip_is_stable(tmp_path):
    """A second round-trip must produce identical output to the first."""
    cfg_path = tmp_path / "config.yaml"
    _write_yaml(cfg_path, {"watches": [MINIMAL_WATCH]})

    first = round_trip(cfg_path)
    second = round_trip(cfg_path)
    assert first.model_dump() == second.model_dump()


def test_load_missing_file_returns_defaults(tmp_path):
    cfg = load(tmp_path / "nonexistent.yaml")
    assert cfg.watches == []
    assert cfg.models.text_model == "qwen2.5:7b"


def test_load_assigns_and_persists_stable_ids(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    with cfg_path.open("w") as f:
        yaml.dump({"watches": [{**MINIMAL_WATCH, "name": "W1"}]}, f)

    first = load(cfg_path)
    wid = first.watches[0].id
    assert wid                                   # an id was assigned
    # Persisted to disk, and stable across reloads (not regenerated each load).
    assert "id:" in cfg_path.read_text()
    assert load(cfg_path).watches[0].id == wid


def test_save_then_load(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    original = load()  # load from repo default
    save(original, cfg_path)
    reloaded = load(cfg_path)
    assert original.model_dump() == reloaded.model_dump()
