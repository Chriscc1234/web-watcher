"""
Tests for gpu_detect.py — all offline, no subprocess or network calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from web_watcher.gpu_detect import (
    TierInfo,
    _TIERS,
    _select_tier,
    detect_tier,
    validate_inference,
    _vram_from_nvidia_smi,
    _vram_from_wmi,
)


# ---------------------------------------------------------------------------
# _select_tier
# ---------------------------------------------------------------------------

def test_select_tier_none_returns_cpu():
    tier = _select_tier(None)
    assert tier.tier_name == "CPU"
    assert tier.vision_model is None


def test_select_tier_16gb():
    tier = _select_tier(16_000)
    assert tier.tier_name == "16GB"
    assert tier.text_model == "qwen2.5:14b"
    assert tier.vision_model == "qwen2.5vl:7b"


def test_select_tier_24gb():
    tier = _select_tier(24_000)
    assert tier.tier_name == "24GB"
    assert tier.text_model == "qwen2.5:32b"


def test_select_tier_48gb():
    tier = _select_tier(48_000)
    assert tier.tier_name == "48GB+"
    assert tier.text_model == "qwen2.5:72b"


def test_select_tier_exactly_8gb():
    tier = _select_tier(8_000)
    assert tier.tier_name == "8-12GB"
    assert tier.text_model == "qwen2.5:7b"


def test_select_tier_exactly_6gb():
    tier = _select_tier(6_000)
    assert tier.tier_name == "6GB"
    assert tier.text_model == "qwen2.5:7b"
    assert tier.vision_model == "moondream"


def test_select_tier_below_6gb_gives_cpu():
    tier = _select_tier(5_999)
    assert tier.tier_name == "CPU"
    assert tier.vision_model is None


def test_select_tier_large_vram():
    tier = _select_tier(80_000)  # e.g. A100 80 GB
    assert tier.tier_name == "48GB+"


# ---------------------------------------------------------------------------
# TierInfo.fallback()
# ---------------------------------------------------------------------------

def _tier(name: str) -> TierInfo:
    return next(t for t in _TIERS if t.tier_name == name)


def test_tier_fallback_16gb_to_8gb():
    assert _tier("16GB").fallback().tier_name == "8-12GB"


def test_tier_fallback_6gb_to_cpu():
    assert _tier("6GB").fallback().tier_name == "CPU"


def test_tier_fallback_cpu_is_stable():
    cpu = _TIERS[-1]
    assert cpu.fallback() is cpu   # no tier below CPU


# ---------------------------------------------------------------------------
# TierInfo.council_model — defaults to text_model
# ---------------------------------------------------------------------------

def test_council_model_defaults_to_text_model():
    # Every tier leaves council reasoning on a model that is actually pulled.
    for t in _TIERS:
        assert t.council_model
    assert _tier("16GB").council_model == "qwen2.5:14b"


# ---------------------------------------------------------------------------
# TierInfo.as_dict()
# ---------------------------------------------------------------------------

def test_as_dict_has_required_keys():
    tier = _tier("16GB")
    d = tier.as_dict()
    assert set(d) == {"tier_name", "text_model", "vision_model", "council_model", "min_vram_mb"}
    assert d["tier_name"] == "16GB"


# ---------------------------------------------------------------------------
# _vram_from_nvidia_smi — mocked subprocess
# ---------------------------------------------------------------------------

def _mock_smi(stdout: str, returncode: int = 0):
    result       = MagicMock()
    result.returncode = returncode
    result.stdout     = stdout
    return result


@patch("web_watcher.gpu_detect.subprocess.run")
def test_nvidia_smi_parses_single_gpu(mock_run):
    mock_run.return_value = _mock_smi("16376\n")
    assert _vram_from_nvidia_smi() == 16376


@patch("web_watcher.gpu_detect.subprocess.run")
def test_nvidia_smi_picks_largest_of_multi_gpu(mock_run):
    mock_run.return_value = _mock_smi("8192\n16376\n")
    assert _vram_from_nvidia_smi() == 16376


@patch("web_watcher.gpu_detect.subprocess.run")
def test_nvidia_smi_non_zero_return_gives_none(mock_run):
    mock_run.return_value = _mock_smi("", returncode=1)
    assert _vram_from_nvidia_smi() is None


@patch("web_watcher.gpu_detect.subprocess.run", side_effect=FileNotFoundError)
def test_nvidia_smi_not_found_gives_none(_mock):
    assert _vram_from_nvidia_smi() is None


@patch("web_watcher.gpu_detect.subprocess.run")
def test_nvidia_smi_empty_output_gives_none(mock_run):
    mock_run.return_value = _mock_smi("   \n  \n")
    assert _vram_from_nvidia_smi() is None


# ---------------------------------------------------------------------------
# _vram_from_wmi — mocked subprocess
# ---------------------------------------------------------------------------

def _mock_ps(stdout: str, returncode: int = 0):
    result = MagicMock()
    result.returncode = returncode
    result.stdout     = stdout
    return result


@patch("web_watcher.gpu_detect.subprocess.run")
def test_wmi_parses_bytes_to_mb(mock_run):
    # 8 GB = 8589934592 bytes → 8192 MB
    mock_run.return_value = _mock_ps("8589934592\n")
    assert _vram_from_wmi() == 8192


@patch("web_watcher.gpu_detect.subprocess.run")
def test_wmi_picks_largest_adapter(mock_run):
    # Integrated (512 MB) + discrete (8 GB)
    mock_run.return_value = _mock_ps("536870912\n8589934592\n")
    assert _vram_from_wmi() == 8192


@patch("web_watcher.gpu_detect.subprocess.run")
def test_wmi_nonzero_return_gives_none(mock_run):
    mock_run.return_value = _mock_ps("", returncode=1)
    assert _vram_from_wmi() is None


@patch("web_watcher.gpu_detect.subprocess.run", side_effect=FileNotFoundError)
def test_wmi_not_found_gives_none(_mock):
    assert _vram_from_wmi() is None


# ---------------------------------------------------------------------------
# detect_tier — integration (mocked VRAM)
# ---------------------------------------------------------------------------

@patch("web_watcher.gpu_detect._detect_vram_mb", return_value=6_144)
def test_detect_tier_6gb_floor(mock_vram):
    tier = detect_tier()
    assert tier.tier_name == "6GB"
    assert tier.text_model == "qwen2.5:7b"
    assert tier.vision_model == "moondream"


@patch("web_watcher.gpu_detect._detect_vram_mb", return_value=None)
def test_detect_tier_no_gpu_gives_cpu(mock_vram):
    tier = detect_tier()
    assert tier.tier_name == "CPU"
    assert tier.vision_model is None


# ---------------------------------------------------------------------------
# validate_inference — mocked httpx
# ---------------------------------------------------------------------------

def _make_response(status: int = 200, elapsed: float = 1.0):
    resp = MagicMock()
    resp.is_success    = (status < 400)
    resp.status_code   = status
    resp.json.return_value = {
        "message": {"content": "OK", "role": "assistant"},
    }
    return resp


@patch("time.monotonic", side_effect=[0.0, 1.5])
@patch("web_watcher.gpu_detect.httpx.Client")
def test_validate_inference_fast_response_is_ok(mock_client_cls, _mock_time):
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = lambda s: mock_ctx
    mock_ctx.__exit__  = MagicMock(return_value=False)
    mock_ctx.post.return_value = _make_response(200)
    mock_client_cls.return_value = mock_ctx

    assert validate_inference("qwen2.5:3b", max_seconds=30.0) is True


@patch("time.monotonic", side_effect=[0.0, 50.0])
@patch("web_watcher.gpu_detect.httpx.Client")
def test_validate_inference_slow_response_is_false(mock_client_cls, _mock_time):
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = lambda s: mock_ctx
    mock_ctx.__exit__  = MagicMock(return_value=False)
    mock_ctx.post.return_value = _make_response(200)
    mock_client_cls.return_value = mock_ctx

    assert validate_inference("qwen2.5:3b", max_seconds=30.0) is False


@patch("web_watcher.gpu_detect.httpx.Client")
def test_validate_inference_connection_error_is_false(mock_client_cls):
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = lambda s: mock_ctx
    mock_ctx.__exit__  = MagicMock(return_value=False)
    mock_ctx.post.side_effect = Exception("connection refused")
    mock_client_cls.return_value = mock_ctx

    assert validate_inference("qwen2.5:3b") is False


@patch("time.monotonic", side_effect=[0.0, 2.0])
@patch("web_watcher.gpu_detect.httpx.Client")
def test_validate_inference_non_200_is_false(mock_client_cls, _mock_time):
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = lambda s: mock_ctx
    mock_ctx.__exit__  = MagicMock(return_value=False)
    mock_ctx.post.return_value = _make_response(503)
    mock_client_cls.return_value = mock_ctx

    assert validate_inference("qwen2.5:3b") is False
