"""Two-way Telegram bridge: the sender allowlist (a security boundary) and the pure helpers.

No network here — only the logic that decides WHO may drive the app and how a reply is shaped.
See web_watcher/telegram_bot.py."""

from __future__ import annotations

import time

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
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sent.append((chat_id, t)))
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
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sent.append(t))
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
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sent.append(t))
    monkeypatch.setattr(b, "_typing", lambda chat_id="": None)
    monkeypatch.setattr(b, "_apply_pending", lambda p: pytest.fail("must not apply on 'no'"))
    b._pending = [{"name": "Trucks"}]
    b._handle_message("no")
    assert b._pending is None and "left everything" in sent[0].lower()


def test_a_new_request_after_a_proposal_is_not_consent(monkeypatch):
    b = _bridge()
    sent = []
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sent.append(t))
    monkeypatch.setattr(b, "_typing", lambda chat_id="": None)
    monkeypatch.setattr(b, "_apply_pending", lambda p: pytest.fail("must not apply"))
    monkeypatch.setattr(b, "_ask_watcher", lambda t, o="", n="": {"message": "Sure, boats instead."})
    b._pending = [{"name": "Trucks"}]
    b._handle_message("actually find me a boat")
    assert sent == ["Sure, boats instead."]


# ── tap-to-vet button ────────────────────────────────────────────────────────────

def test_vet_token_roundtrip_and_fits_callback_data(tmp_path, monkeypatch):
    from web_watcher import notify
    monkeypatch.setattr(notify, "_vet_store_path", lambda: tmp_path / "vet_links.json")
    url = "https://offerup.com/item/detail/41b6e682-17bd-3b0a-a1cc-9b0da7882a27"
    tok = notify.remember_vet_link(url)
    assert notify.vet_url_for(tok) == url
    assert len(f"vet:{tok}") <= 64          # Telegram's callback_data cap
    assert notify.vet_url_for("nosuchtoken") == ""


def test_vet_button_runs_inspect_and_replies(monkeypatch, tmp_path):
    from web_watcher import notify
    monkeypatch.setattr(notify, "_vet_store_path", lambda: tmp_path / "vet_links.json")
    monkeypatch.setattr(notify, "image_bytes_for_listing", lambda img, link="": None)  # no photo → text
    url = "https://offerup.com/item/detail/x"
    tok = notify.remember_vet_link(url)

    b = _bridge("111")
    sent = []
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sent.append(t))
    monkeypatch.setattr(b, "_typing", lambda chat_id="": None)
    monkeypatch.setattr(b, "_answer_callback", lambda cb_id, text="": None)
    monkeypatch.setattr(b, "_vet_listing", lambda u: f"VERDICT for {u}")
    b._handle_callback({"id": "1", "data": f"vet:{tok}",
                        "message": {"chat": {"id": "111"}}})
    assert sent and f"VERDICT for {url}" in sent[0]


def test_not_a_match_in_chat_removes_the_last_shown_listing(monkeypatch):
    """No per-card button any more — you just tell the bot, and it removes the last one it showed."""
    b = _bridge("111")
    b._last_listing["111"] = {"url": "https://x/bad", "title": "Bad Jeep"}
    posted, sent = [], []
    monkeypatch.setattr(b, "_typing", lambda chat_id="": None)
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sent.append(t))

    class _R:
        status_code = 200
        def json(self): return {"removed": 1}

    monkeypatch.setattr("web_watcher.telegram_bot.httpx.post",
                        lambda url, **k: posted.append(k.get("json")) or _R())
    b._handle_message("that's not a match", "111", "Chris")
    assert posted and posted[0]["url"] == "https://x/bad"
    assert any("Removed" in s for s in sent)
    assert "111" not in b._last_listing            # cleared after removal


def test_not_a_match_without_a_shown_listing_asks_which_one(monkeypatch):
    b = _bridge("111")
    sent = []
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sent.append(t))
    monkeypatch.setattr("web_watcher.telegram_bot.httpx.post",
                        lambda *a, **k: pytest.fail("must not exclude anything without a tracked match"))
    b._handle_message("not a match", "111", "Chris")
    assert sent and "which one" in sent[0].lower()


def test_not_a_match_legacy_callback_still_excludes(monkeypatch, tmp_path):
    """The old alerts' 🚫 button is gone, but its callback still works for any lingering messages."""
    from web_watcher import notify
    monkeypatch.setattr(notify, "_vet_store_path", lambda: tmp_path / "vet_links.json")
    url = "https://x/view/d/bad-boat/abc"
    tok = notify.remember_vet_link(url)
    b = _bridge("111")
    posted, sent = {}, []
    monkeypatch.setattr(b, "_answer_callback", lambda cb_id, text="": None)
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sent.append(t))

    class _R:
        status_code = 200
        def json(self): return {"ok": True, "removed": 2}

    monkeypatch.setattr("web_watcher.telegram_bot.httpx.post",
                        lambda url, **k: posted.update(k.get("json") or {}) or _R())
    b._handle_callback({"id": "1", "data": f"unmatch:{tok}", "message": {"chat": {"id": "111"}}})
    assert posted.get("url") == url                       # the right listing was excluded
    assert "Removed from your matches" in sent[0]


def test_vet_verdict_is_sent_as_a_photo_card_when_an_image_exists(monkeypatch):
    """The verdict can land after other chatting, so the listing's picture makes it unmistakable
    which listing it's about."""
    from web_watcher import notify
    monkeypatch.setattr(notify, "image_bytes_for_listing", lambda img, link="": b"JPEGDATA")
    b = _bridge("111")
    calls = []

    class _Resp:
        status_code = 200
        text = "ok"

    monkeypatch.setattr("web_watcher.telegram_bot.httpx.post",
                        lambda url, **k: calls.append((url, k)) or _Resp())
    monkeypatch.setattr(b, "_send", lambda *a, **k: pytest.fail("a photo verdict must not fall back"))
    b._send_verdict("111", "1998 Tacoma", "★★★★☆ great deal, low scam risk",
                    "https://x/view/d/tacoma/abc")
    photo = [c for c in calls if "sendPhoto" in c[0]]
    assert len(photo) == 1
    assert photo[0][1]["files"]["photo"][1] == b"JPEGDATA"
    assert "1998 Tacoma" in photo[0][1]["data"]["caption"]
    assert "great deal" in photo[0][1]["data"]["caption"]


def test_vet_button_ignores_unauthorized_chat(monkeypatch):
    b = _bridge("111")
    sent = []
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sent.append(t))
    monkeypatch.setattr(b, "_vet_listing", lambda u: pytest.fail("must not vet for a stranger"))
    b._handle_callback({"id": "1", "data": "vet:abc", "message": {"chat": {"id": "99999"}}})
    assert sent == []


def test_format_verdict_renders_stars_risk_and_flags():
    from web_watcher.telegram_bot import _format_verdict
    out = _format_verdict({"deal_quality": 2, "scam_risk": "high",
                           "deal_reason": "Above comps.", "red_flags": ["no photos"]})
    assert "★★☆☆☆" in out and "high scam risk" in out and "no photos" in out


# ── the "top N" list: a photo card + a Vet button per listing ────────────────────

def test_top_n_card_with_an_image_uploads_the_photo_with_a_vet_button(monkeypatch, tmp_path):
    """A picture AND a per-listing vet control need one message per listing — Telegram won't
    attach buttons to a multi-photo album. The card UPLOADS the image bytes (Telegram's own fetch
    of craigslist URLs 400s) and carries a vet: button."""
    import json
    from web_watcher import notify
    monkeypatch.setattr(notify, "_vet_store_path", lambda: tmp_path / "vet_links.json")
    monkeypatch.setattr(notify, "fetch_image_bytes", lambda u: b"JPEGDATA")     # we fetch, not TG
    b = _bridge()
    calls = []

    class _Resp:
        status_code = 200
        text = "ok"

    monkeypatch.setattr("web_watcher.telegram_bot.httpx.post",
                        lambda url, **k: calls.append((url, k)) or _Resp())
    monkeypatch.setattr(b, "_send", lambda *a, **k: pytest.fail("a photo card must not fall back"))
    row = {"url": "https://skagit.craigslist.org/boo/1.html", "title": "Sea Ray 190",
           "price_text": "$9,000", "source": "craigslist",
           "image": "https://images.craigslist.org/x.jpg", "rating": 4}
    b._send_listing_card(row, "12345")

    photo = [c for c in calls if "sendPhoto" in c[0]]
    assert len(photo) == 1
    kwargs = photo[0][1]
    assert kwargs["files"]["photo"][1] == b"JPEGDATA"                # bytes uploaded, not a URL
    data = kwargs["data"]
    assert "Sea Ray 190" in data["caption"] and "$9,000" in data["caption"]
    kb = json.loads(data["reply_markup"])["inline_keyboard"][0]      # markup is a JSON string here
    vet = next(btn for btn in kb if btn.get("callback_data", "").startswith("vet:"))
    assert notify.vet_url_for(vet["callback_data"][4:]) == row["url"]     # token → this listing


def test_top_n_card_falls_back_to_text_when_the_image_cant_be_fetched(monkeypatch, tmp_path):
    """A picture we can't download must never cost the alert — fall back to the text card, which
    still carries the Vet button."""
    from web_watcher import notify
    monkeypatch.setattr(notify, "_vet_store_path", lambda: tmp_path / "vet_links.json")
    monkeypatch.setattr(notify, "image_bytes_for_listing", lambda img, link="": None)  # no picture
    b = _bridge()
    sent = []
    monkeypatch.setattr(b, "_send",
                        lambda t, chat_id="", html=False, buttons=None: sent.append((t, buttons)))
    monkeypatch.setattr("web_watcher.telegram_bot.httpx.post",
                        lambda *a, **k: pytest.fail("no photo call when the image can't be fetched"))
    row = {"url": "https://x/boo/9.html", "title": "Skiff", "source": "craigslist",
           "image": "https://images.craigslist.org/dead.jpg"}
    b._send_listing_card(row, "12345")
    assert len(sent) == 1
    text, buttons = sent[0]
    assert "Skiff" in text
    assert any(btn.get("callback_data", "").startswith("vet:") for btn in buttons[0])


def test_top_n_card_without_an_image_falls_back_to_text_but_keeps_vet(monkeypatch, tmp_path):
    """No picture must never cost the vet control — a text card still carries the Vet button."""
    from web_watcher import notify
    monkeypatch.setattr(notify, "_vet_store_path", lambda: tmp_path / "vet_links.json")
    b = _bridge()
    sent = []
    monkeypatch.setattr(b, "_send",
                        lambda t, chat_id="", html=False, buttons=None: sent.append((t, buttons)))
    monkeypatch.setattr("web_watcher.telegram_bot.httpx.post",
                        lambda *a, **k: pytest.fail("no photo call without an image"))
    row = {"url": "https://x/boo/2.html", "title": "Bayliner 175",
           "price_text": "$7,500", "source": "craigslist"}
    b._send_listing_card(row, "12345")
    assert len(sent) == 1
    text, buttons = sent[0]
    assert "Bayliner 175" in text
    assert any(btn.get("callback_data", "").startswith("vet:") for btn in buttons[0])


def test_top_request_sends_a_header_then_one_card_per_listing(monkeypatch, tmp_path):
    from web_watcher import notify
    monkeypatch.setattr(notify, "_vet_store_path", lambda: tmp_path / "vet_links.json")
    monkeypatch.setattr(notify, "watch_for_brief", lambda tok: "Boats Watch")
    b = _bridge()
    rows = [{"url": f"https://x/{i}", "title": f"Boat {i}"} for i in range(3)]

    class _R:
        def raise_for_status(self): pass
        def json(self): return rows

    monkeypatch.setattr("web_watcher.telegram_bot.httpx.get", lambda *a, **k: _R())
    monkeypatch.setattr("web_watcher.telegram_bot.time.sleep", lambda s: None)
    headers, cards = [], []
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: headers.append(t))
    monkeypatch.setattr(b, "_send_listing_card", lambda row, chat: cards.append(row))
    b._handle_top_request("sometoken:10", "12345")
    assert len(headers) == 1 and "Top 3" in headers[0]
    assert [r["title"] for r in cards] == ["Boat 0", "Boat 1", "Boat 2"]


# ── the "Show top N" buttons debounce rapid taps ─────────────────────────────────

def test_top_buttons_debounce_rapid_taps(monkeypatch):
    """Tapping top-10 then top-20 back to back must not fire two overlapping card bursts."""
    b = _bridge("111")
    monkeypatch.setattr(b, "_answer_callback", lambda cb_id, text="": None)
    ran = []
    monkeypatch.setattr(b, "_handle_top_request", lambda payload, chat: ran.append(payload))
    cb = lambda n: {"id": str(n), "data": f"top:tok:{n}", "message": {"chat": {"id": "111"}}}
    b._handle_callback(cb(10))
    b._handle_callback(cb(20))          # immediately after → swallowed by the cooldown
    assert ran == ["tok:10"]


def test_top_buttons_allow_a_later_tap(monkeypatch):
    import web_watcher.telegram_bot as TB
    b = _bridge("111")
    monkeypatch.setattr(b, "_answer_callback", lambda cb_id, text="": None)
    monkeypatch.setattr(TB, "_TOP_COOLDOWN_S", 0.0)     # no cooldown → a later tap is honoured
    ran = []
    monkeypatch.setattr(b, "_handle_top_request", lambda payload, chat: ran.append(payload))
    cb = lambda n: {"id": str(n), "data": f"top:tok:{n}", "message": {"chat": {"id": "111"}}}
    b._handle_callback(cb(10))
    b._handle_callback(cb(20))
    assert ran == ["tok:10", "tok:20"]


# ── the "hang on, this is a hard one" slow-turn nudge ────────────────────────────

def test_a_slow_turn_nudges_in_order_at_its_times(monkeypatch):
    import web_watcher.telegram_bot as TB
    b = _bridge()
    sent = []
    monkeypatch.setattr(b, "_typing", lambda chat_id="": None)
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sent.append(t))
    monkeypatch.setattr(TB, "_TYPING_REFRESH_S", 0.01)
    with b._typing_until_sent("111", slow_nudges=[(0.02, "hang on"), (0.06, "still on it")]):
        time.sleep(0.16)                       # long enough to cross both thresholds
    assert sent == ["hang on", "still on it"]   # in order, each once


def test_a_fast_turn_does_not_nudge(monkeypatch):
    b = _bridge()
    sent = []
    monkeypatch.setattr(b, "_typing", lambda chat_id="": None)
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sent.append(t))
    with b._typing_until_sent("111", slow_nudges=[(5.0, "hang on")]):
        pass                                   # replies immediately
    assert "hang on" not in sent


def test_the_second_nudge_holds_until_its_own_time(monkeypatch):
    """The first nudge fires; the later one must NOT fire yet if the reply lands between them."""
    import web_watcher.telegram_bot as TB
    b = _bridge()
    sent = []
    monkeypatch.setattr(b, "_typing", lambda chat_id="": None)
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sent.append(t))
    monkeypatch.setattr(TB, "_TYPING_REFRESH_S", 0.01)
    with b._typing_until_sent("111", slow_nudges=[(0.02, "hang on"), (5.0, "still on it")]):
        time.sleep(0.08)                       # past #1, nowhere near #2
    assert sent == ["hang on"]


def test_there_is_real_variety_in_the_thinking_sayings():
    """Several distinct phrases per stage so the reassurance never gets repetitive."""
    import web_watcher.telegram_bot as TB
    assert len(set(TB._THINKING_NUDGES)) >= 4
    assert len(set(TB._STILL_WORKING_NUDGES)) >= 3
    assert all(isinstance(m, str) and m.strip() for m in TB._THINKING_NUDGES + TB._STILL_WORKING_NUDGES)


def test_no_nudge_without_the_opt_in(monkeypatch):
    import web_watcher.telegram_bot as TB
    b = _bridge()
    sent = []
    monkeypatch.setattr(b, "_typing", lambda chat_id="": None)
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sent.append(t))
    monkeypatch.setattr(TB, "_TYPING_REFRESH_S", 0.01)
    with b._typing_until_sent("111"):          # no slow_nudges → never sends anything
        time.sleep(0.05)
    assert sent == []


# ── a chat lookup shows rich cards for a small result set ────────────────────────

def test_a_small_lookup_is_shown_as_rich_cards(monkeypatch):
    """'Show me the latest match' should come as a full card (photo + buttons), not a flat line."""
    b = _bridge("111")
    monkeypatch.setattr(b, "_ask_watcher", lambda *a, **k: {
        "message": "Here's your latest match:",
        "listings": [{"url": "https://x/1", "title": "Jeep", "price_text": "$5,600", "rating": 4}]})
    monkeypatch.setattr(b, "_typing", lambda chat_id="": None)
    cards, sends = [], []
    monkeypatch.setattr(b, "_send_listing_card", lambda row, chat: cards.append(row))
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sends.append(t))
    b._handle_message("show me the latest match", "111", "Chris")
    assert len(cards) == 1 and cards[0]["title"] == "Jeep"


def test_a_big_lookup_stays_a_compact_list(monkeypatch):
    b = _bridge("111")
    rows = [{"url": f"https://x/{i}", "title": f"Car {i}", "rating": 3} for i in range(8)]
    monkeypatch.setattr(b, "_ask_watcher", lambda *a, **k: {"message": "matches:", "listings": rows})
    monkeypatch.setattr(b, "_typing", lambda chat_id="": None)
    cards, sends = [], []
    monkeypatch.setattr(b, "_send_listing_card", lambda row, chat: cards.append(row))
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sends.append(t))
    b._handle_message("show me the matches", "111", "Chris")
    assert cards == []                         # a big list is not a card storm
    assert sends and "Car 0" in sends[0]        # rendered as the compact text list


# ── approving a new user from the chat ───────────────────────────────────────────

def test_admin_can_allow_a_user_from_the_chat(monkeypatch):
    b = _bridge("111")                              # 111 is the admin (main configured chat)
    posted, sent = [], []
    monkeypatch.setattr(b, "_answer_callback", lambda cb_id, text="": None)
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sent.append(t))

    class _Get:
        def json(self): return [{"chat_id": "999", "name": "Dave"}]

    class _Post:
        status_code = 200
        def json(self): return {"ok": True}

    monkeypatch.setattr("web_watcher.telegram_bot.httpx.get", lambda url, **k: _Get())
    monkeypatch.setattr("web_watcher.telegram_bot.httpx.post",
                        lambda url, **k: posted.append((url, k.get("json"))) or _Post())
    b._handle_callback({"id": "1", "data": "allow:999", "message": {"chat": {"id": "111"}}})
    allow = [p for p in posted if "allow" in p[0]]
    assert allow and allow[0][1]["chat_id"] == "999" and allow[0][1]["name"] == "Dave"
    assert any("is in" in s for s in sent)


def test_deny_dismisses_the_request(monkeypatch):
    b = _bridge("111")
    posted = []
    monkeypatch.setattr(b, "_answer_callback", lambda cb_id, text="": None)
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: None)

    class _Post:
        status_code = 200
        def json(self): return {}

    monkeypatch.setattr("web_watcher.telegram_bot.httpx.post",
                        lambda url, **k: posted.append(url) or _Post())
    b._handle_callback({"id": "1", "data": "deny:999", "message": {"chat": {"id": "111"}}})
    assert any("dismiss" in u for u in posted)


def test_a_non_admin_buddy_cannot_approve_people(monkeypatch):
    b = TelegramBridge("tok", "111", "u", allowed_chat_ids=["222"])   # 222 is a buddy, not admin
    answered = []
    monkeypatch.setattr(b, "_answer_callback", lambda cb_id, text="": answered.append(text))
    monkeypatch.setattr("web_watcher.telegram_bot.httpx.post",
                        lambda *a, **k: pytest.fail("a non-admin must never let someone in"))
    b._handle_callback({"id": "1", "data": "allow:999", "message": {"chat": {"id": "222"}}})
    assert answered and "owner" in answered[0].lower()


# ── applying a proposed watch: "already exists" is not an error ──────────────────

def test_already_exists_reads_as_friendly_not_a_failure(monkeypatch):
    """A create for a watch that's already there should reassure, not throw a red warning — the
    thing the user asked for exists."""
    b = _bridge()

    class _R:
        status_code = 409
        def json(self): return {"detail": "Watch 'Boats' already exists"}

    monkeypatch.setattr("web_watcher.telegram_bot.httpx.post", lambda *a, **k: _R())
    out = b._apply_pending([{"action": "create", "name": "Boats", "urls": ["https://x"]}])
    assert "already set up" in out
    assert "Couldn't" not in out and "⚠️" not in out


def test_a_real_apply_failure_still_warns(monkeypatch):
    b = _bridge()

    class _R:
        status_code = 400
        def json(self): return {"detail": "bad field"}

    monkeypatch.setattr("web_watcher.telegram_bot.httpx.post", lambda *a, **k: _R())
    out = b._apply_pending([{"action": "create", "name": "Boats", "urls": ["https://x"]}])
    assert "Couldn't apply" in out and "bad field" in out


def _get_returning(rows):
    class _R:
        def json(self): return rows
    return lambda *a, **k: _R()


def test_watch_diff_flags_a_changed_instruction(monkeypatch):
    b = _bridge()
    monkeypatch.setattr("web_watcher.telegram_bot.httpx.get",
                        _get_returning([{"name": "Boats", "instruction": "old", "urls": ["https://x"]}]))
    diff = b._watch_diff("Boats", {"instruction": "new", "urls": ["https://x"]})
    assert diff and "Looking for" in diff[0] and "old" in diff[0] and "new" in diff[0]


def test_watch_diff_shows_concrete_price_and_radius_labels(monkeypatch):
    """The diff must say WHAT changed, in plain money/miles — not a vague 'filters changed'."""
    b = _bridge()
    existing = [{"name": "Boats", "instruction": "boats",
                 "urls": ["https://skagit.craigslist.org/search/boo?max_price=15000&search_distance=150&postal=98221"]}]
    monkeypatch.setattr("web_watcher.telegram_bot.httpx.get", _get_returning(existing))
    diff = b._watch_diff("Boats", {"instruction": "boats",
        "urls": ["https://skagit.craigslist.org/search/boo?max_price=20000&search_distance=300&postal=98221"]})
    joined = " | ".join(diff)
    assert "Max price: $15,000 → $20,000" in joined
    assert "Search radius: 150 mi → 300 mi" in joined


def test_watch_diff_is_empty_when_effectively_the_same(monkeypatch):
    b = _bridge()
    monkeypatch.setattr("web_watcher.telegram_bot.httpx.get",
                        _get_returning([{"name": "Boats", "instruction": "same", "urls": ["https://x"]}]))
    assert b._watch_diff("Boats", {"instruction": "same", "urls": ["https://x"]}) == []


def test_a_differing_collision_offers_update_replace_leave(monkeypatch):
    b = _bridge()

    class _Post:
        status_code = 409
        def json(self): return {"detail": "Watch 'Boats' already exists"}

    monkeypatch.setattr("web_watcher.telegram_bot.httpx.post", lambda *a, **k: _Post())
    monkeypatch.setattr(b, "_watch_diff", lambda name, body: ["Looking for: “old” → “new”"])
    out = b._apply_pending([{"action": "create", "name": "Boats",
                             "instruction": "new", "urls": ["https://x"]}])
    assert "You already have a watch called" in out and "what would change" in out.lower()
    assert "Looking for" in out
    assert "update" in out and "replace" in out and "leave" in out
    assert b._pending_conflict and b._pending_conflict["name"] == "Boats"


def test_conflict_update_puts_the_new_settings(monkeypatch):
    b = _bridge()
    b._pending_conflict = {"name": "Boats", "body": {"instruction": "new", "urls": ["https://x"]}}
    calls, sent = {}, []
    monkeypatch.setattr("web_watcher.telegram_bot.httpx.put",
                        lambda url, **k: calls.update(put=url) or type("R", (), {"status_code": 200})())
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sent.append(t))
    b._resolve_conflict("update", "111")
    assert "put" in calls and "Updated" in sent[0] and b._pending_conflict is None


def test_conflict_replace_deletes_then_recreates(monkeypatch):
    b = _bridge()
    b._pending_conflict = {"name": "Boats", "body": {"urls": ["https://x"]}}
    seq, sent = [], []
    monkeypatch.setattr("web_watcher.telegram_bot.httpx.delete",
                        lambda url, **k: seq.append("del") or type("R", (), {"status_code": 200})())
    monkeypatch.setattr("web_watcher.telegram_bot.httpx.post",
                        lambda url, **k: seq.append("post") or type("R", (), {"status_code": 201})())
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sent.append(t))
    b._resolve_conflict("replace", "111")
    assert seq == ["del", "post"] and "Replaced" in sent[0]


def test_conflict_leave_changes_nothing(monkeypatch):
    b = _bridge()
    b._pending_conflict = {"name": "Boats", "body": {}}
    sent = []
    monkeypatch.setattr("web_watcher.telegram_bot.httpx.put",
                        lambda *a, **k: pytest.fail("leave must not modify the watch"))
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sent.append(t))
    b._resolve_conflict("leave", "111")
    assert "kept" in sent[0].lower() and b._pending_conflict is None


def test_an_unclear_conflict_answer_re_asks_and_holds(monkeypatch):
    b = _bridge()
    b._pending_conflict = {"name": "Boats", "body": {}}
    sent = []
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sent.append(t))
    b._resolve_conflict("what do you mean", "111")
    assert b._pending_conflict is not None                 # still held for a real answer
    assert "update" in sent[0] and "replace" in sent[0] and "leave" in sent[0]


# ── proactive check-ins (heartbeats) ─────────────────────────────────────────────

def test_checkin_hours_configures_the_interval():
    assert TelegramBridge("t", "1", "u", checkin_hours=12).checkin_s == 12 * 3600
    assert TelegramBridge("t", "1", "u", checkin_hours=0).checkin_s == 0     # disabled


def test_heartbeat_disabled_when_checkin_hours_zero(monkeypatch):
    b = TelegramBridge("t", "111", "u", checkin_hours=0)
    monkeypatch.setattr(b, "_fetch_watches", lambda: pytest.fail("must not even look"))
    b._run_heartbeats(now=10_000_000)          # returns immediately, no send


def test_heartbeat_fires_when_quiet_and_offers_help(monkeypatch):
    b = _bridge("111")
    sent = []
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sent.append((chat_id, t)))
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
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sent.append(t))
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
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sent.append(t))
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
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sent.append(t))
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
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sent.append(t))
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
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: None)
    monkeypatch.setattr(b, "_typing", lambda chat_id="": None)
    monkeypatch.setattr(b, "_ask_watcher",
                        lambda t, o="", n="": {"message": "Here's what I'd set up.",
                                   "watch_suggestion": {"name": "Trucks"}})
    b._handle_message("watch for trucks")
    assert b._pending == [{"name": "Trucks"}]


# ── HTML parse mode (the "settings printed literal <i> tags" bug) ────────────────

def test_html_blocks_are_sent_with_parse_mode(monkeypatch):
    """Our own formatted blocks (settings, listing lists) must go out with parse_mode=HTML,
    or Telegram prints the tags literally — which is exactly what happened to the settings
    block's <i>Change yours:</i>."""
    b = _bridge()
    posts = []

    class _R:
        status_code = 200
        def json(self): return {"ok": True}
    monkeypatch.setattr("web_watcher.telegram_bot.httpx.post",
                        lambda url, **k: posts.append(k.get("json") or {}) or _R())
    b._send("<b>Your settings</b>", "111", html=True)
    assert posts[-1].get("parse_mode") == "HTML"
    assert posts[-1]["text"] == "<b>Your settings</b>"      # ours is already correct — untouched

    # Model prose is CONVERTED (its Markdown rendered) and escaped, so it goes out as HTML too
    # but still cannot break the parser — the escaping happens before any tag is introduced.
    b._send("plain model prose with < and & in it", "111")
    assert posts[-1].get("parse_mode") == "HTML"
    assert posts[-1]["text"] == "plain model prose with &lt; and &amp; in it"


def test_settings_reply_is_sent_as_html(monkeypatch):
    b = _bridge()
    sent = []
    monkeypatch.setattr(b, "_send", lambda t, chat_id="", html=False, buttons=None: sent.append((t, html)))
    monkeypatch.setattr(b, "_typing", lambda chat_id="": None)
    monkeypatch.setattr(b, "_ask_watcher",
                        lambda t, o="", n="": {"message": "⚙️ <b>Your settings</b>", "html": True})
    b._handle_message("settings")
    assert sent and sent[0][1] is True             # html=True → tags render as formatting


def test_no_literal_markdown_asterisks_in_prompts():
    from web_watcher.telegram_bot import _describe_suggestions
    out = _describe_suggestions({"watch_suggestion": {"name": "Trucks"}})
    assert "*" not in out                          # asterisks print literally without markdown mode


# ── chat stays responsive (a slow vet must not freeze the bot) ───────────────────

def test_updates_are_handled_off_the_poll_loop(monkeypatch):
    """A chat turn or a vet can take minutes on a local model. Handling them inline froze the
    bot — a message sent during a vet sat unanswered until it finished."""
    b = _bridge()
    started = []
    monkeypatch.setattr(b, "_get_updates", lambda: [{"message": {"text": "hi", "chat": {"id": "12345"}}}])
    monkeypatch.setattr(b, "_latest_offset", lambda: None)
    monkeypatch.setattr(b, "_maybe_run_heartbeats", lambda: None)

    class _T:
        def __init__(self, target=None, args=(), name=None, daemon=None):
            started.append(name)
        def start(self): b._stop.set()          # one pass then exit the loop
    monkeypatch.setattr("web_watcher.telegram_bot.threading.Thread", _T)
    b._loop()
    assert "telegram-update" in started          # dispatched on its own thread, loop kept reading


def test_typing_is_refreshed_until_the_reply_is_ready(monkeypatch):
    """Telegram expires 'typing' after ~5s, so one call left the chat looking idle mid-think."""
    b = _bridge()
    beats = []
    monkeypatch.setattr(b, "_typing", lambda chat_id="": beats.append(chat_id))
    monkeypatch.setattr("web_watcher.telegram_bot._TYPING_REFRESH_S", 0.02)
    with b._typing_until_sent("111"):
        time.sleep(0.12)
    assert len(beats) >= 2 and beats[0] == "111"  # kept alive, not a single shot
    n = len(beats)
    time.sleep(0.08)
    assert len(beats) == n                        # and stopped once the reply was ready


# ── vetting a listing that has since gone away ───────────────────────────────────
# A dead link is exactly when the saved copy matters most. Answering "couldn't read it" while
# holding the title, price and source is the least useful thing we could do.

def test_vetting_a_dead_listing_still_reports_what_was_saved():
    from web_watcher.telegram_bot import _format_verdict
    out = _format_verdict({
        "fetched": False,
        "error": "Couldn't open the listing page — it looks removed.",
        "known": {"title": "1998 Toyota Tacoma 4x4", "price_text": "$8,500",
                  "source": "craigslist.org", "posted_at": "2026-08-20T10:00:00"},
    })
    assert "removed" in out
    assert "1998 Toyota Tacoma 4x4" in out       # the stored title
    assert "$8,500" in out                       # the stored price
    assert "Craigslist" in out                   # the stored source, named properly


def test_vetting_a_dead_listing_with_nothing_saved_says_so():
    from web_watcher.telegram_bot import _format_verdict
    out = _format_verdict({"fetched": False, "error": "Couldn't open it.", "known": {}})
    assert "don't have a saved copy" in out


def test_a_normal_verdict_restates_the_saved_facts():
    from web_watcher.telegram_bot import _format_verdict
    out = _format_verdict({
        "fetched": True, "deal_quality": 4, "scam_risk": "low", "summary": "Looks solid.",
        "known": {"title": "1998 Toyota Tacoma", "price_text": "$8,500", "source": "facebook.com"},
    })
    assert "★★★★☆" in out and "Looks solid." in out
    assert "1998 Toyota Tacoma" in out and "Facebook Marketplace" in out


# ── the model writes Markdown; Telegram doesn't read it ──────────────────────────
# From the real log: "1. **Anacortes Manual Transmission Cars Watch** - ..." reached the phone
# with the asterisks printed. Sending as Markdown instead is not the fix — one underscore in a
# listing URL breaks the parse and Telegram drops the WHOLE message. Convert, escape, send HTML.

def _md(text):
    from web_watcher.telegram_bot import _markdown_to_telegram_html
    return _markdown_to_telegram_html(text)


def test_markdown_bold_and_italic_become_html():
    assert _md("You have **two watches**") == "You have <b>two watches</b>"
    assert _md("the *boats* one") == "the <i>boats</i> one"


def test_angle_brackets_in_a_listing_title_are_escaped_not_sent_raw():
    """An unescaped < in a title makes Telegram reject the send — the reply is simply lost."""
    assert _md("1998 Toyota <Tacoma> & trailer") == "1998 Toyota &lt;Tacoma&gt; &amp; trailer"


def test_multiplication_and_snake_case_are_left_alone():
    # A watch about "5*3 sheets" or a URL slug must not turn into italics.
    assert _md("price is 5*3 and a_b_c stays") == "price is 5*3 and a_b_c stays"


def test_markdown_links_become_tappable():
    assert _md("See [the listing](https://x.com/a_b?c=1)") == \
        'See <a href="https://x.com/a_b?c=1">the listing</a>'


def test_conversion_is_safe_on_empty_and_plain_text():
    assert _md("") == ""
    assert _md("just a normal sentence") == "just a normal sentence"


def test_a_reply_is_converted_before_sending(monkeypatch):
    """The bridge must not send raw model prose — that's how the asterisks got through."""
    from web_watcher.telegram_bot import TelegramBridge
    sent = {}
    monkeypatch.setattr("web_watcher.telegram_bot.httpx.post",
                        lambda url, **kw: sent.update(kw.get("json") or {}) or _Ok())
    b = TelegramBridge("tok", "111", "http://127.0.0.1:7878")
    b._send("You have **two watches**", "111")
    assert sent["text"] == "You have <b>two watches</b>"
    assert sent["parse_mode"] == "HTML"


class _Ok:
    status_code = 200

    def json(self):
        return {"ok": True}

    def raise_for_status(self):
        pass
