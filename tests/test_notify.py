"""
Notification sender tests — all offline (no real Telegram or SMTP).

Live tests (pytest -m live) require real credentials in config.yaml.
"""

from __future__ import annotations

import smtplib
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call
import pytest

from web_watcher.config import NotificationsConfig, TelegramConfig, EmailConfig
from web_watcher.notify import (
    NotificationPayload,
    _format_message,
    send_email,
    send_notifications,
    send_telegram,
)
from web_watcher.reasoning import ReasoningResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _result(found=True, summary="Ice storm warning", confidence="high", link=None):
    return ReasoningResult(found=found, summary=summary, confidence=confidence, link=link)


def _payload(result=None, screenshot=None):
    return NotificationPayload(
        watch_name="Test watch",
        result=result or _result(),
        timestamp=datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc),
        screenshot_bytes=screenshot,
    )


def _cfg(telegram=True, email=True):
    return NotificationsConfig(
        telegram=TelegramConfig(bot_token="tok123", chat_id="chat456") if telegram else TelegramConfig(),
        email=EmailConfig(
            smtp_server="smtp.example.com",
            smtp_port=587,
            from_address="from@example.com",
            app_password="pass",
            to_address="to@example.com",
        ) if email else EmailConfig(),
    )


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def test_format_plain_contains_watch_name():
    msg = _format_message(_payload(), html=False)
    assert "Test watch" in msg
    assert "Ice storm warning" in msg
    assert "HIGH" in msg


def test_format_html_contains_link():
    result = _result(link="https://nws.gov/alerts")
    msg = _format_message(_payload(result=result), html=True)
    assert "https://nws.gov/alerts" in msg
    assert "<a href=" in msg


def test_format_plain_no_link_when_none():
    msg = _format_message(_payload(), html=False)
    assert "Link:" not in msg


def test_format_html_no_link_tag_when_none():
    msg = _format_message(_payload(), html=True)
    assert "<a href=" not in msg


def test_format_timestamp_present():
    msg = _format_message(_payload(), html=False)
    assert "2026-06-20" in msg


# ---------------------------------------------------------------------------
# Telegram — missing config
# ---------------------------------------------------------------------------

def test_telegram_skipped_when_no_token():
    cfg = NotificationsConfig(telegram=TelegramConfig(bot_token="", chat_id=""))
    result = send_telegram(_payload(), cfg)
    assert result is False


def test_telegram_skipped_when_no_chat_id():
    cfg = NotificationsConfig(telegram=TelegramConfig(bot_token="tok", chat_id=""))
    result = send_telegram(_payload(), cfg)
    assert result is False


# ---------------------------------------------------------------------------
# Telegram — HTTP success (mocked)
# ---------------------------------------------------------------------------

def test_telegram_sends_message_on_success(respx_mock=None):
    cfg = _cfg(email=False)
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()

    with patch("web_watcher.notify.httpx.Client") as mock_client_cls:
        instance = mock_client_cls.return_value.__enter__.return_value
        instance.post.return_value = mock_response

        ok = send_telegram(_payload(), cfg)

    assert ok is True
    assert instance.post.call_count == 1   # text only, no screenshot
    call_kwargs = instance.post.call_args
    assert "sendMessage" in call_kwargs[0][0]


def test_telegram_sends_screenshot_when_present():
    cfg = _cfg(email=False)
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch("web_watcher.notify.httpx.Client") as mock_client_cls:
        instance = mock_client_cls.return_value.__enter__.return_value
        instance.post.return_value = mock_response

        ok = send_telegram(_payload(screenshot=b"\x89PNG"), cfg)

    assert ok is True
    assert instance.post.call_count == 2   # sendMessage + sendPhoto
    photo_url = instance.post.call_args_list[1][0][0]
    assert "sendPhoto" in photo_url


def test_telegram_returns_false_on_http_error():
    cfg = _cfg(email=False)
    import httpx as _httpx

    with patch("web_watcher.notify.httpx.Client") as mock_client_cls:
        instance = mock_client_cls.return_value.__enter__.return_value
        err_response = MagicMock()
        err_response.status_code = 401
        err_response.text = "Unauthorized"
        instance.post.side_effect = _httpx.HTTPStatusError(
            "401", request=MagicMock(), response=err_response
        )

        ok = send_telegram(_payload(), cfg)

    assert ok is False


# ---------------------------------------------------------------------------
# Email — missing config
# ---------------------------------------------------------------------------

def test_email_skipped_when_no_address():
    cfg = NotificationsConfig(email=EmailConfig())
    result = send_email(_payload(), cfg)
    assert result is False


# ---------------------------------------------------------------------------
# Email — SMTP success (mocked)
# ---------------------------------------------------------------------------

def test_email_sends_on_success():
    cfg = _cfg(telegram=False)

    with patch("web_watcher.notify.smtplib.SMTP") as mock_smtp:
        server = mock_smtp.return_value.__enter__.return_value
        ok = send_email(_payload(), cfg)

    assert ok is True
    server.login.assert_called_once_with("from@example.com", "pass")
    server.sendmail.assert_called_once()


def test_email_attaches_screenshot():
    cfg = _cfg(telegram=False)

    with patch("web_watcher.notify.smtplib.SMTP") as mock_smtp:
        server = mock_smtp.return_value.__enter__.return_value
        ok = send_email(_payload(screenshot=b"\x89PNG"), cfg)

    assert ok is True
    # sendmail called with content that includes the attachment
    raw_email = server.sendmail.call_args[0][2]
    assert "screenshot.png" in raw_email


def test_email_returns_false_on_auth_error():
    cfg = _cfg(telegram=False)

    with patch("web_watcher.notify.smtplib.SMTP") as mock_smtp:
        server = mock_smtp.return_value.__enter__.return_value
        server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Bad credentials")

        ok = send_email(_payload(), cfg)

    assert ok is False


# ---------------------------------------------------------------------------
# send_notifications — channel independence
# ---------------------------------------------------------------------------

def test_send_notifications_both_channels():
    cfg = _cfg()
    with patch("web_watcher.notify.send_telegram", return_value=True) as mt, \
         patch("web_watcher.notify.send_email",    return_value=True) as me:
        results = send_notifications(_payload(), cfg, use_telegram=True, use_email=True)

    assert results == {"telegram": True, "email": True}
    mt.assert_called_once()
    me.assert_called_once()


def test_send_notifications_telegram_failure_does_not_block_email():
    cfg = _cfg()
    with patch("web_watcher.notify.send_telegram", return_value=False), \
         patch("web_watcher.notify.send_email",    return_value=True) as me:
        results = send_notifications(_payload(), cfg)

    assert results["telegram"] is False
    assert results["email"] is True
    me.assert_called_once()


def test_send_notifications_telegram_only():
    cfg = _cfg()
    with patch("web_watcher.notify.send_telegram", return_value=True) as mt, \
         patch("web_watcher.notify.send_email") as me:
        results = send_notifications(_payload(), cfg, use_telegram=True, use_email=False)

    assert "email" not in results
    mt.assert_called_once()
    me.assert_not_called()
