"""Re-scan must download the new models BEFORE switching the config to them.

A hardware re-scan can bump the model tier (e.g. a 6GB GPU that was mis-detected as CPU-only).
The dangerous ordering is: switch config → download in background, because for the several
minutes the multi-GB pull takes, the app points at a model that isn't on disk and every chat
turn / sweep fails. So `_apply_new_models_bg` pulls first and only switches once every model
is present — and leaves the working model in place if the download fails.
"""

from __future__ import annotations

import pytest

from web_watcher import config as C
from web_watcher.dashboard import server as S

REC = {"text_model": "qwen2.5:7b", "vision_model": "moondream", "council_model": "qwen2.5:7b"}


@pytest.fixture
def cpu_tier_config():
    """Seed the config on the CPU tier — the state a mis-detected machine lands in."""
    cfg = C.load()
    cfg.models.text_model = "qwen2.5:3b"
    cfg.models.vision_model = ""
    cfg.models.council_model = "qwen2.5:3b"
    C.save(cfg)
    return cfg


def _client_that(on_stream):
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def raise_for_status(self): pass
        def iter_lines(self): return iter([])

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def stream(self, method, url, json=None, **k):
            on_stream(json or {})
            return _Resp()
    return _Client


def test_failed_pull_leaves_working_model_in_place(cpu_tier_config, monkeypatch):
    def boom(_payload):
        raise RuntimeError("network died mid-download")
    monkeypatch.setattr(S.httpx, "Client", _client_that(boom))

    S._apply_new_models_bg(REC)

    assert C.load().models.text_model == "qwen2.5:3b"   # never switched
    assert S._rescan_state["status"] == "error"


def test_config_switches_only_after_every_model_is_downloaded(cpu_tier_config, monkeypatch):
    pulled: list[str] = []

    def record(payload):
        pulled.append(payload["name"])
        # The config must STILL be the old model while downloads are in flight.
        assert C.load().models.text_model == "qwen2.5:3b", "config switched before pull finished"
    monkeypatch.setattr(S.httpx, "Client", _client_that(record))

    S._apply_new_models_bg(REC)

    assert pulled == ["qwen2.5:7b", "moondream"]        # both pulled, deduped, in order
    cfg = C.load()
    assert cfg.models.text_model == "qwen2.5:7b"        # switched, but only at the end
    assert cfg.models.vision_model == "moondream"
    assert S._rescan_state["status"] == "done"
