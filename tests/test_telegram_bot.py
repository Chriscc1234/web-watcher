"""Two-way Telegram bridge: the sender allowlist (a security boundary) and the pure helpers.

No network here — only the logic that decides WHO may drive the app and how a reply is shaped.
See web_watcher/telegram_bot.py."""

from __future__ import annotations

from web_watcher.telegram_bot import TelegramBridge, _chunk, _describe_suggestions


def _bridge(chat_id="12345") -> TelegramBridge:
    return TelegramBridge("111:TOKEN", chat_id, "http://127.0.0.1:7878")


# ── the security boundary ──────────────────────────────────────────────────────

def test_only_the_configured_chat_is_authorized():
    b = _bridge("12345")
    assert b._authorized("12345") is True
    assert b._authorized(12345) is True          # Telegram sends ints
    assert b._authorized("99999") is False       # a stranger who found the bot
    assert b._authorized(None) is False


def test_dispatch_ignores_unauthorized_and_empty(monkeypatch):
    b = _bridge("12345")
    handled = []
    monkeypatch.setattr(b, "_handle_message", lambda t: handled.append(t))
    b._dispatch({"message": {"text": "hi", "chat": {"id": "99999"}}})   # stranger
    assert handled == []
    b._dispatch({"message": {"text": "", "chat": {"id": "12345"}}})     # no text
    assert handled == []
    b._dispatch({"message": {"text": "hello", "chat": {"id": "12345"}}})
    assert handled == ["hello"]


def test_not_configured_without_token_or_chat():
    assert TelegramBridge("", "123", "u").configured is False
    assert TelegramBridge("tok", "", "u").configured is False
    assert TelegramBridge("tok", "123", "u").configured is True


def test_start_is_a_noop_when_unconfigured():
    assert TelegramBridge("", "", "u").start() is False


# ── reply shaping ──────────────────────────────────────────────────────────────

def test_chunk_respects_the_telegram_limit():
    assert _chunk("short", 4096) == ["short"]
    big = "\n".join(f"line {i}" for i in range(2000))
    parts = _chunk(big, 200)
    assert all(len(p) <= 200 for p in parts)
    assert "".join(p.replace("\n", "") for p in parts) == big.replace("\n", "")


def test_chunk_handles_a_single_unbroken_run():
    parts = _chunk("x" * 500, 100)
    assert all(len(p) <= 100 for p in parts) and "".join(parts) == "x" * 500


def test_describe_suggestions_names_the_drafted_watches():
    assert _describe_suggestions({}) == ""
    one = _describe_suggestions({"watch_suggestion": {"name": "Trucks"}})
    assert "Trucks" in one
    many = _describe_suggestions({"watch_suggestions": [{"name": "A"}, {"name": "B"}]})
    assert "A" in many and "B" in many and "2" in many
