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
    _format_telegram,
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
# Telegram message format (the "no alert ever arrived" bug: the email HTML doc
# was sent to Telegram, which rejected it — "Unsupported start tag html")
# ---------------------------------------------------------------------------

_TG_FORBIDDEN = ("<html", "<body", "<table", "<tr", "<td", "<h2", "<span", "style=", "<br")


def test_telegram_format_has_no_unsupported_tags():
    msg = _format_telegram(_payload(result=_result(link="https://x.com/i")))
    low = msg.lower()
    for tag in _TG_FORBIDDEN:
        assert tag not in low, tag
    assert "<b>" in msg and 'href="https://x.com/i"' in msg   # supported tags only


def test_telegram_format_escapes_raw_html_in_summary():
    # A scraped summary containing raw tags must be escaped, not passed through.
    r = _result(summary="Deal <b>x</b> & <script>evil</script> at <html>")
    msg = _format_telegram(_payload(result=r))
    assert "<script>" not in msg and "&lt;script&gt;" in msg
    assert msg.startswith(("🟢", "🟡", "🔴", "🔔"))          # never starts with a raw tag


def test_telegram_format_omits_link_when_none():
    msg = _format_telegram(_payload(result=_result(link=None)))
    assert "Open listing" not in msg


def test_telegram_omits_confidence_for_rated_listings():
    # A marketplace find has a ★ rating (the real signal); the hardcoded "HIGH confidence" is noise.
    rated = _result(summary="★★★☆☆ New match: 2008 Miata — $6,500", confidence="high")
    msg = _format_telegram(_payload(result=rated))
    assert "confidence" not in msg.lower()
    assert "★★★☆☆" in msg


def test_telegram_keeps_confidence_for_yesno_watches():
    # A yes/no watch (weather) has no rating — confidence is meaningful there, so keep it.
    plain = _result(summary="Frost warning tonight", confidence="high")
    msg = _format_telegram(_payload(result=plain))
    assert "HIGH" in msg and "confidence" in msg.lower()


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


def test_owner_override_routes_alert_to_the_owner():
    # A watch owned by a friend (a chat_id) alerts THEM, not the main chat.
    cfg = _cfg(email=False)                    # main chat_id = "chat456"
    mock_response = MagicMock(); mock_response.raise_for_status = MagicMock()
    with patch("web_watcher.notify.httpx.Client") as mock_client_cls:
        instance = mock_client_cls.return_value.__enter__.return_value
        instance.post.return_value = mock_response
        ok = send_telegram(_payload(), cfg, chat_id_override="999buddy")
    assert ok is True
    assert instance.post.call_args.kwargs["json"]["chat_id"] == "999buddy"


def test_blank_owner_override_falls_back_to_main_chat():
    cfg = _cfg(email=False)
    mock_response = MagicMock(); mock_response.raise_for_status = MagicMock()
    with patch("web_watcher.notify.httpx.Client") as mock_client_cls:
        instance = mock_client_cls.return_value.__enter__.return_value
        instance.post.return_value = mock_response
        send_telegram(_payload(), cfg, chat_id_override="")
    assert instance.post.call_args.kwargs["json"]["chat_id"] == "chat456"


def test_send_notifications_passes_owner_through():
    cfg = _cfg()
    with patch("web_watcher.notify.send_telegram", return_value=True) as mt, \
         patch("web_watcher.notify.send_email", return_value=True):
        send_notifications(_payload(), cfg, owner_chat_id="999buddy")
    assert mt.call_args.kwargs["chat_id_override"] == "999buddy"


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


# ── source, price and photo: use what we already stored ──────────────────────────
# We record a listing's source, price and thumbnail the moment a watch finds it. Not showing
# them on the alert made the reader guess which site a find came from, and made the vetter
# announce "no price given" about listings whose price we'd known all along.

def test_source_label_names_sites_the_way_people_do():
    from web_watcher.notify import source_label
    assert source_label("facebook.com") == "Facebook Marketplace"
    assert source_label("https://seattle.craigslist.org/x") == "Craigslist"
    assert source_label("offerup.com") == "OfferUp"
    assert source_label("") == ""
    assert source_label("someshop.example.com") == "someshop.example.com"   # unknown → tidy host


def test_alert_shows_the_source_and_price(monkeypatch):
    from web_watcher import notify
    monkeypatch.setattr(notify, "_known_facts",
                        lambda url: {"source": "facebook.com", "price_text": "$8,500"})
    payload = _payload(_result(summary="★★★★☆ 1998 Toyota Tacoma", link="https://x/1"))
    text = notify._format_telegram(payload)
    assert "Facebook Marketplace" in text
    assert "$8,500" in text


def test_alert_does_not_repeat_a_price_already_in_the_summary(monkeypatch):
    from web_watcher import notify
    monkeypatch.setattr(notify, "_known_facts",
                        lambda url: {"source": "craigslist.org", "price_text": "$8,500"})
    payload = _payload(_result(summary="★★★★☆ Tacoma — $8,500 in Anacortes", link="https://x/1"))
    text = notify._format_telegram(payload)
    assert text.count("$8,500") == 1


def test_a_missing_stored_record_never_breaks_the_alert(monkeypatch):
    from web_watcher import notify
    monkeypatch.setattr(notify, "_known_facts", lambda url: {})
    text = notify._format_telegram(_payload(_result(summary="a find", link="https://x/1")))
    assert "Open listing" in text


def test_alert_uploads_the_listing_photo_bytes_not_a_url(monkeypatch):
    """Telegram's own fetch of craigslist image URLs was 400ing, so every boat alert lost its
    picture and fell back to text. The alert now DOWNLOADS the image and uploads the bytes via a
    multipart sendPhoto — it never hands Telegram the URL to fetch."""
    from web_watcher import notify
    monkeypatch.setattr(notify, "_known_facts",
                        lambda url: {"source": "craigslist.org", "image": "https://img/x.jpg"})
    monkeypatch.setattr(notify, "image_bytes_for_listing", lambda img, link="": b"JPEGDATA")
    calls = []

    class _Resp:
        def raise_for_status(self): pass

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, **kw):
            calls.append((url, kw)); return _Resp()

    monkeypatch.setattr(notify.httpx, "Client", _Client)
    ok = send_telegram(_payload(_result(summary="★★★★☆ Boat", link="https://x/1")),
                       _cfg(email=False))
    assert ok is True
    photo = [c for c in calls if "sendPhoto" in c[0]]
    assert len(photo) == 1                                   # exactly one photo, no screenshot
    assert photo[0][1]["files"]["photo"][1] == b"JPEGDATA"   # bytes uploaded, not a URL
    assert "photo" not in (photo[0][1].get("json") or {})    # not the old json-with-URL shape
    assert not any("sendMessage" in c[0] for c in calls)     # the photo REPLACED the text send


def test_alert_falls_back_to_text_when_the_photo_cant_be_downloaded(monkeypatch):
    """A picture we can't fetch must not cost the alert — send the text message instead."""
    from web_watcher import notify
    monkeypatch.setattr(notify, "_known_facts",
                        lambda url: {"source": "craigslist.org", "image": "https://img/dead.jpg"})
    monkeypatch.setattr(notify, "image_bytes_for_listing", lambda img, link="": None)
    calls = []

    class _Resp:
        def raise_for_status(self): pass

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, **kw):
            calls.append((url, kw)); return _Resp()

    monkeypatch.setattr(notify.httpx, "Client", _Client)
    ok = send_telegram(_payload(_result(summary="★★★★☆ Boat", link="https://x/1")),
                       _cfg(email=False))
    assert ok is True
    assert any("sendMessage" in c[0] for c in calls)         # text still went out
    assert not any("sendPhoto" in c[0] for c in calls)       # no photo attempted


def test_image_recovers_from_og_image_when_the_scraper_stored_none(monkeypatch):
    """Agent sweeps miss craigslist's lazy-loaded card images, so a match can be stored with no
    thumbnail. The alert then recovers the picture from the listing page's og:image."""
    from web_watcher import notify
    monkeypatch.setattr(notify, "_known_facts", lambda url: {"source": "craigslist.org"})  # no image
    seen = {}

    def _fake_og(listing_url):
        seen["og_called_with"] = listing_url
        return "https://images.craigslist.org/recovered_600x450.jpg"

    monkeypatch.setattr(notify, "og_image_for", _fake_og)
    monkeypatch.setattr(notify, "fetch_image_bytes",
                        lambda u: b"RECOVERED" if "recovered" in u else None)
    calls = []

    class _Resp:
        def raise_for_status(self): pass

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, **kw):
            calls.append((url, kw)); return _Resp()

    monkeypatch.setattr(notify.httpx, "Client", _Client)
    ok = send_telegram(_payload(_result(summary="★★★★☆ Boat", link="https://cl/view/d/boat/abc")),
                       _cfg(email=False))
    assert ok is True
    assert seen["og_called_with"] == "https://cl/view/d/boat/abc"   # recovered from THIS listing
    photo = [c for c in calls if "sendPhoto" in c[0]]
    assert len(photo) == 1 and photo[0][1]["files"]["photo"][1] == b"RECOVERED"


def _og_client(text):
    class _Resp:
        def __init__(self): self.text = text
        def raise_for_status(self): pass

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, **kw): return _Resp()

    return _Client


def test_og_image_parses_either_attribute_order(monkeypatch):
    from web_watcher import notify
    monkeypatch.setattr(notify.httpx, "Client", _og_client(
        '<head><meta property="og:image" content="https://images.craigslist.org/a_600x450.jpg"></head>'))
    assert notify.og_image_for("https://cl/view/d/x/1").endswith("a_600x450.jpg")
    monkeypatch.setattr(notify.httpx, "Client", _og_client(
        '<head><meta content="https://images.craigslist.org/b_600x450.jpg" property="og:image"></head>'))
    assert notify.og_image_for("https://cl/view/d/x/1").endswith("b_600x450.jpg")


def test_og_image_never_fetches_facebook(monkeypatch):
    """FB pages are login-walled and we only ever touch FB as a human — never a background fetch."""
    from web_watcher import notify

    def _boom(**kw):
        raise AssertionError("must not fetch a Facebook page for og:image")

    monkeypatch.setattr(notify.httpx, "Client", _boom)
    assert notify.og_image_for("https://www.facebook.com/marketplace/item/123") == ""


def test_og_image_returns_blank_when_absent(monkeypatch):
    from web_watcher import notify
    monkeypatch.setattr(notify.httpx, "Client", _og_client("<head><title>no og here</title></head>"))
    assert notify.og_image_for("https://cl/view/d/x/1") == ""


# ── the baseline burst ───────────────────────────────────────────────────────────
# A new or changed watch finds hundreds of listings that were already there. Alerting on all of
# them is a wall of noise, so we bank them silently — but silence looks exactly like a broken
# watch ("it never notified me about any boats"). One message with the numbers, and buttons.

def _tg_cfg():
    from web_watcher.config import NotificationsConfig, TelegramConfig
    return NotificationsConfig(telegram=TelegramConfig(bot_token="tok", chat_id="111"))


def _capture(monkeypatch):
    sent = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"ok": True}

    class _Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None, **kw):
            sent.update(json or {})
            return _Resp()

    from web_watcher import notify
    monkeypatch.setattr(notify.httpx, "Client", _Client)
    return sent


def test_baseline_briefing_states_the_numbers_and_offers_the_backlog(monkeypatch, tmp_path):
    from web_watcher import notify, paths
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    sent = _capture(monkeypatch)
    assert notify.send_baseline_briefing("Boats Watch", 189, 10, _tg_cfg()) is True
    assert "189 listings" in sent["text"]
    assert "10 of them" in sent["text"]
    buttons = sent["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["⭐ Show top 10", "Show top 20"]
    assert all(len(b["callback_data"].encode()) <= 64 for b in buttons)   # Telegram's hard cap


def test_baseline_briefing_with_no_matches_offers_no_buttons(monkeypatch, tmp_path):
    from web_watcher import notify, paths
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    sent = _capture(monkeypatch)
    notify.send_baseline_briefing("Boats Watch", 189, 0, _tg_cfg())
    assert "None of them matched" in sent["text"]
    assert "reply_markup" not in sent


def test_the_briefing_token_resolves_back_to_the_watch(monkeypatch, tmp_path):
    from web_watcher import notify, paths
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    tok = notify.remember_brief("Anacortes Under 30' Motor Boats Watch")
    assert notify.watch_for_brief(tok) == "Anacortes Under 30' Motor Boats Watch"
    assert notify.watch_for_brief("nope") == ""


def test_a_briefing_token_cannot_collide_with_a_vet_token(monkeypatch, tmp_path):
    """Both live in one small file; a shared key would resolve a watch name as a listing URL."""
    from web_watcher import notify, paths
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    notify.remember_vet_link("https://x/1", "A listing")
    tok = notify.remember_brief("https://x/1")           # same string, different kind
    assert notify.watch_for_brief(tok) == "https://x/1"
    assert notify.vet_entry_for(notify.vet_token("https://x/1")).get("url") == "https://x/1"


# ── the example ask is in THIS watch's words ─────────────────────────────────────
# A briefing for a truck watch that suggests "show me the boats with outboards" reads like it
# wasn't about your watch at all.

def test_example_ask_uses_the_watch_instruction():
    from web_watcher.notify import example_ask
    got = example_ask("Anacortes Under 30' Motor Boats Watch",
                      "Look for under 30-foot motor boats with outboard motors within 150 miles of Anacortes.")
    assert got == "show me the under 30-foot motor boats with outboard motors"


def test_example_ask_drops_the_location_clause():
    from web_watcher.notify import example_ask
    got = example_ask("Cars", "Find manual transmission cars under $8000 within 100 miles of Anacortes")
    assert "miles" not in got and "anacortes" not in got
    assert "manual transmission cars" in got


def test_example_ask_falls_back_to_the_watch_name():
    from web_watcher.notify import example_ask
    assert example_ask("Thrive Workwear Pants Restock", "") == "show me the thrive workwear pants restock"


def test_example_ask_strips_a_trailing_watch_from_the_name():
    from web_watcher.notify import example_ask
    assert not example_ask("Motor Boats Watch", "").endswith("watch")


def test_example_ask_has_a_generic_last_resort():
    from web_watcher.notify import example_ask
    assert example_ask("W", "") == "show me what you found"


def test_the_briefing_example_is_watch_specific(monkeypatch, tmp_path):
    from web_watcher import notify, paths
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    sent = _capture(monkeypatch)
    notify.send_baseline_briefing("Boats Watch", 189, 10, _tg_cfg(),
                                  instruction="Look for under 30-foot motor boats with outboard motors")
    assert "under 30-foot motor boats with outboard motors" in sent["text"]
