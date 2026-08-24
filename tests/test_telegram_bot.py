"""Two-way Telegram bridge: the sender allowlist (a security boundary) and the pure helpers.

No network here — only the logic that decides WHO may drive the app and how a reply is shaped.
See web_watcher/telegram_bot.py."""

from __future__ import annotations

import pytest

from web_watcher.telegram_bot import (
    TelegramBridge, _chunk, _describe_suggestions, _heartbeat_message, _is_affirmative,
    _is_negative, _parse_iso, _suggestions_of, _HEARTBEAT_EVERY_S,
)


def _bridge(chat_id="12345") -> TelegramBridge:
    return TelegramBridge("111:TOKEN", chat_id, "http://127.0.0.1:7878")


# ── the security boundary ──────────────────────────────────────────────────────

def test_only_the_configured_chat_is_authorized():
    b = _bridge("12345")
    assert b._authorized("12345") is True
    assert b._authorized(12345) is True          # Telegram sends ints
    assert b._authorized("99999") is False       # a stranger who found the bot
    assert b._authorized(None) is False


def test_extra_allowed_chats_can_talk_too():
    # "you AND your buddy": the alert chat plus any extra IDs, nobody else.
    b = TelegramBridge("tok", "111", "u", allowed_chat_ids=["222", " 333 "])
    assert b._authorized("111") is True          # the alert chat is always allowed
    assert b._authorized("222") is True
    assert b._authorized(333) is True            # whitespace trimmed, int-safe
    assert b._authorized("444") is False         # everyone else is still ignored


def test_blank_extra_ids_do_not_open_the_door():
    # An empty string must never end up in the allowlist — str(None)/"" could match junk.
    b = TelegramBridge("tok", "111", "u", allowed_chat_ids=["", "   ", None])
    assert b.allowed == {"111"}
    assert b._authorized("") is False


def test_dispatch_ignores_unauthorized_and_empty(monkeypatch):
    b = _bridge("12345")
    handled, knocks = [], []
    monkeypatch.setattr(b, "_handle_message", lambda t, sender="", sender_name="": handled.append(t))
    monkeypatch.setattr(b, "_notify_access_request", lambda cid, name="": knocks.append(cid))
    b._dispatch({"message": {"text": "hi", "chat": {"id": "99999"}}})   # stranger
    assert handled == [] and knocks == ["99999"]                        # not handled, admin alerted
    b._dispatch({"message": {"text": "", "chat": {"id": "12345"}}})     # no text
    assert handled == []
    b._dispatch({"message": {"text": "hello", "chat": {"id": "12345"}}})
    assert handled == ["hello"]


def test_notify_access_request_alerts_admin_once(monkeypatch):
    b = _bridge("12345")
    sent = []
    monkeypatch.setattr(b, "_send", lambda t, chat_id="": sent.append((chat_id, t)))
    monkeypatch.setattr("web_watcher.telegram_bot.httpx.post", lambda *a, **k: None)
    b._notify_access_request("99999", "Stranger")
    b._notify_access_request("99999", "Stranger")          # same knocker again — no second alert
    admin_alerts = [t for cid, t in sent if cid == "12345"]
    assert len(admin_alerts) == 1 and "99999" in admin_alerts[0]
    assert any(cid == "99999" for cid, _ in sent)          # the knocker got an acknowledgement


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


def test_describe_suggestions_names_them_and_asks_for_a_yes():
    assert _describe_suggestions({}) == ""
    one = _describe_suggestions({"watch_suggestion": {"name": "Trucks"}})
    assert "Trucks" in one and "yes" in one.lower()
    many = _describe_suggestions({"watch_suggestions": [{"name": "A"}, {"name": "B"}]})
    assert "A" in many and "B" in many
    edit = _describe_suggestions({"watch_suggestion": {"name": "Trucks", "action": "update"}})
    assert "Edit" in edit          # an edit must not read as a brand-new watch


def test_suggestions_of_handles_both_shapes():
    assert _suggestions_of({}) == []
    assert _suggestions_of({"watch_suggestion": {"name": "A"}}) == [{"name": "A"}]
    assert len(_suggestions_of({"watch_suggestions": [{"name": "A"}, {"name": "B"}]})) == 2


# ── the confirm-from-your-phone flow ───────────────────────────────────────────

def test_affirmative_and_negative_detection():
    for yes in ("yes", "y", "Yep", "ok", "do it", "go ahead", "sure!", "apply"):
        assert _is_affirmative(yes) is True, yes
    for no in ("no", "nope", "cancel", "never mind", "don't"):
        assert _is_negative(no) is True, no
    # A message that merely STARTS with a yes-word is a NEW request, not consent to apply.
    for not_yes in ("ok now find me a boat instead", "yes but change the price to 5000",
                    "sure, what about trucks?"):
        assert _is_affirmative(not_yes) is False, not_yes


def test_yes_applies_the_pending_change(monkeypatch):
    b = _bridge()
    sent, applied = [], []
    monkeypatch.setattr(b, "_send", lambda t, chat_id="": sent.append(t))
    monkeypatch.setattr(b, "_typing", lambda chat_id="": None)
    monkeypatch.setattr(b, "_apply_pending", lambda p: applied.append(p) or "✅ Done.")
    monkeypatch.setattr(b, "_ask_watcher", lambda t, o="", n="": pytest.fail("a yes must not re-ask the model"))
    b._pending = [{"name": "Trucks", "action": "update"}]
    b._handle_message("yes")
    assert applied == [[{"name": "Trucks", "action": "update"}]]
    assert b._pending is None            # consumed, so a later stray "yes" can't re-apply
    assert sent == ["✅ Done."]


def test_no_cancels_without_applying(monkeypatch):
    b = _bridge()
    sent = []
    monkeypatch.setattr(b, "_send", lambda t, chat_id="": sent.append(t))
    monkeypatch.setattr(b, "_typing", lambda chat_id="": None)
    monkeypatch.setattr(b, "_apply_pending", lambda p: pytest.fail("must not apply on 'no'"))
    b._pending = [{"name": "Trucks"}]
    b._handle_message("no")
    assert b._pending is None and "left everything" in sent[0].lower()


def test_a_new_request_after_a_proposal_is_not_consent(monkeypatch):
    b = _bridge()
    sent = []
    monkeypatch.setattr(b, "_send", lambda t, chat_id="": sent.append(t))
    monkeypatch.setattr(b, "_typing", lambda chat_id="": None)
    monkeypatch.setattr(b, "_apply_pending", lambda p: pytest.fail("must not apply"))
    monkeypatch.setattr(b, "_ask_watcher", lambda t, o="", n="": {"message": "Sure, boats instead."})
    b._pending = [{"name": "Trucks"}]
    b._handle_message("actually find me a boat")
    assert sent == ["Sure, boats instead."]


# ── proactive check-ins (heartbeats) ─────────────────────────────────────────────

def test_heartbeat_fires_when_quiet_and_offers_help(monkeypatch):
    b = _bridge("111")
    sent = []
    monkeypatch.setattr(b, "_send", lambda t, chat_id="": sent.append((chat_id, t)))
    monkeypatch.setattr(b, "_fetch_watches",
                        lambda: [{"name": "Manual Cars", "owner": "111", "enabled": True,
                                  "stats": {"last_match_at": None}}])
    monkeypatch.setattr(b, "_owner_last_chat_ts", lambda owner: 0.0)
    b._start_ts = 0.0                                  # old enough that we're overdue
    b._run_heartbeats(now=_HEARTBEAT_EVERY_S + 10)
    assert sent and sent[0][0] == "111"
    assert "broaden" in sent[0][1].lower() or "vet" in sent[0][1].lower()


def test_heartbeat_stays_quiet_when_recently_in_touch(monkeypatch):
    b = _bridge("111")
    sent = []
    monkeypatch.setattr(b, "_send", lambda t, chat_id="": sent.append(t))
    monkeypatch.setattr(b, "_fetch_watches",
                        lambda: [{"name": "Manual Cars", "owner": "111", "enabled": True,
                                  "stats": {}}])
    now = _HEARTBEAT_EVERY_S + 100
    monkeypatch.setattr(b, "_owner_last_chat_ts", lambda owner: now - 60)   # chatted a minute ago
    b._run_heartbeats(now=now)
    assert sent == []                                  # recently in touch → no check-in


def test_heartbeat_skips_owner_with_no_enabled_watches(monkeypatch):
    b = _bridge("111")
    sent = []
    monkeypatch.setattr(b, "_send", lambda t, chat_id="": sent.append(t))
    monkeypatch.setattr(b, "_fetch_watches",
                        lambda: [{"name": "Off", "owner": "111", "enabled": False, "stats": {}}])
    monkeypatch.setattr(b, "_owner_last_chat_ts", lambda owner: 0.0)
    b._start_ts = 0.0
    b._run_heartbeats(now=_HEARTBEAT_EVERY_S + 10)
    assert sent == []                                  # nothing actually watching


def test_reversible_action_applies_immediately(monkeypatch):
    # "stop my truck watch" → the server grounds+scopes it; the bridge carries it out at once.
    b = _bridge()
    posted, sent = [], []
    monkeypatch.setattr(b, "_send", lambda t, chat_id="": sent.append(t))
    monkeypatch.setattr(b, "_typing", lambda chat_id="": None)
    monkeypatch.setattr(b, "_ask_watcher",
                        lambda t, o="", n="": {"message": "On it.",
                                               "watch_actions": [{"action": "stop", "name": "Trucks"}]})

    class _R:
        status_code = 200
        def json(self): return {"ok": True}
    monkeypatch.setattr("web_watcher.telegram_bot.httpx.post",
                        lambda url, **k: posted.append(url) or _R())
    b._handle_message("stop my truck watch")
    assert any("/api/watches/Trucks/action" in u for u in posted)   # hit the action endpoint
    assert b._pending is None and b._pending_deletes is None         # nothing left hanging
    assert "stopped" in sent[0].lower()


def test_delete_waits_for_a_yes(monkeypatch):
    b = _bridge()
    posted, sent = [], []
    monkeypatch.setattr(b, "_send", lambda t, chat_id="": sent.append(t))
    monkeypatch.setattr(b, "_typing", lambda chat_id="": None)
    monkeypatch.setattr(b, "_ask_watcher",
                        lambda t, o="", n="": {"message": "",
                                               "watch_actions": [{"action": "delete", "name": "Trucks"}]})

    class _R:
        status_code = 200
        def json(self): return {"ok": True}
    monkeypatch.setattr("web_watcher.telegram_bot.httpx.post",
                        lambda url, **k: posted.append(url) or _R())
    b._handle_message("delete my truck watch")
    assert posted == []                                    # NOT applied yet — waiting for a yes
    assert b._pending_deletes == [{"action": "delete", "name": "Trucks"}]
    assert "yes" in sent[0].lower()
    b._handle_message("yes")                               # confirm
    assert any("/api/watches/Trucks/action" in u for u in posted)
    assert b._pending_deletes is None


def test_a_turn_with_suggestions_arms_the_confirmation(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "_send", lambda t, chat_id="": None)
    monkeypatch.setattr(b, "_typing", lambda chat_id="": None)
    monkeypatch.setattr(b, "_ask_watcher",
                        lambda t, o="", n="": {"message": "Here's what I'd set up.",
                                   "watch_suggestion": {"name": "Trucks"}})
    b._handle_message("watch for trucks")
    assert b._pending == [{"name": "Trucks"}]
