"""
Transferring a watch to another user — chat parsing, person resolution, and the rules.

A transfer is exact business: a watch, a recipient, a permission. Ambiguity in any of them
must become a question, never a guess — a watch quietly handed to the wrong person is worse
than asking.
"""

from __future__ import annotations

import types

import pytest

from web_watcher.dashboard import server as S


# --------------------------------------------------------------------------
# The phrase detector
# --------------------------------------------------------------------------

def test_transfer_phrasings_are_detected():
    for t in ("Give my truck watch to Steve",
              "transfer the MacGregor watch to my buddy",
              "hand over the boats watch to Chris",
              "Can you reassign the cars watch to 8708818228",
              "pass the truck watch to him"):
        m = S._TRANSFER_RE.search(t)
        assert m, t
        assert m.group("who").strip(), t


def test_non_transfers_are_not_detected():
    for t in ("give me the latest matches", "what watches do I have",
              "I want to give up on the truck", "start the boats watch",
              "give my buddy's number a call"):
        assert not S._TRANSFER_RE.search(t), t


def test_recipient_capture():
    m = S._TRANSFER_RE.search("transfer the MacGregor watch to my buddy")
    assert m.group("who").strip().lower() == "my buddy"


# --------------------------------------------------------------------------
# Person resolution
# --------------------------------------------------------------------------

def _cfg(main="111", allowed=("222",)):
    tg = types.SimpleNamespace(chat_id=main, allowed_chat_ids=list(allowed))
    return types.SimpleNamespace(notifications=types.SimpleNamespace(telegram=tg))


def _names(monkeypatch, d):
    monkeypatch.setattr(S, "_load_owner_names", lambda: d)


def test_resolve_by_name(monkeypatch):
    _names(monkeypatch, {"222": "Steve Miller"})
    cid, label = S._resolve_person("steve", _cfg(), actor=None)
    assert cid == "222" and "Steve" in label


def test_resolve_by_chat_id(monkeypatch):
    _names(monkeypatch, {})
    cid, _ = S._resolve_person("222", _cfg(), actor=None)
    assert cid == "222"


def test_unknown_chat_id_refused(monkeypatch):
    _names(monkeypatch, {})
    cid, why = S._resolve_person("99999", _cfg(), actor=None)
    assert cid is None and "allowed" in why


def test_my_buddy_resolves_when_exactly_one_other_person(monkeypatch):
    _names(monkeypatch, {"222": "Steve"})
    cid, label = S._resolve_person("my buddy", _cfg(), actor=None)
    assert cid == "222"


def test_my_buddy_ambiguous_with_two_others(monkeypatch):
    _names(monkeypatch, {"222": "Steve", "333": "Pat"})
    cid, why = S._resolve_person("my buddy", _cfg(allowed=("222", "333")), actor=None)
    assert cid is None


def test_unknown_name_asks(monkeypatch):
    _names(monkeypatch, {"222": "Steve"})
    cid, why = S._resolve_person("Zorblax", _cfg(), actor=None)
    assert cid is None and "Zorblax" in why


def test_me_resolves_to_the_actor(monkeypatch):
    _names(monkeypatch, {})
    assert S._resolve_person("me", _cfg(), actor="222")[0] == "222"
    assert S._resolve_person("me", _cfg(), actor=None)[0] == ""   # desktop admin


# --------------------------------------------------------------------------
# The transfer itself (config layer stubbed)
# --------------------------------------------------------------------------

class _FakeWatch(types.SimpleNamespace):
    pass


def _wire_config(monkeypatch, watches, main="111", allowed=("222",)):
    cfg = _cfg(main, allowed)
    cfg.watches = watches
    import web_watcher.config as C
    monkeypatch.setattr(C, "load", lambda path=None: cfg)
    monkeypatch.setattr(C, "save", lambda c, path=None: None)
    import contextlib
    monkeypatch.setattr(C, "lock", lambda: contextlib.nullcontext())
    monkeypatch.setattr(S, "_load_owner_names", lambda: {"222": "Steve"})
    return cfg


def test_admin_can_transfer_any_watch(monkeypatch):
    w = _FakeWatch(name="Boats", owner="")
    _wire_config(monkeypatch, [w])
    ok, err = S._perform_transfer("Boats", "222", actor=None)
    assert ok, err
    assert w.owner == "222"


def test_owner_can_transfer_their_own(monkeypatch):
    w = _FakeWatch(name="Boats", owner="222")
    _wire_config(monkeypatch, [w])
    ok, _ = S._perform_transfer("Boats", "", actor="222")   # give it back to the desktop
    assert ok and w.owner == ""


def test_stranger_cannot_transfer_someone_elses(monkeypatch):
    w = _FakeWatch(name="Boats", owner="111")
    _wire_config(monkeypatch, [w], allowed=("222", "333"))
    ok, why = S._perform_transfer("Boats", "333", actor="222")
    assert not ok and "current owner" in why
    assert w.owner == "111", "the watch moved despite the refusal"


def test_transfer_to_unknown_person_refused(monkeypatch):
    w = _FakeWatch(name="Boats", owner="")
    _wire_config(monkeypatch, [w])
    ok, why = S._perform_transfer("Boats", "99999", actor=None)
    assert not ok and "known person" in why


def test_missing_watch_reports_not_found(monkeypatch):
    _wire_config(monkeypatch, [])
    ok, why = S._perform_transfer("Ghost", "222", actor=None)
    assert not ok and "not found" in why


def test_transfer_to_current_owner_is_a_noop(monkeypatch):
    w = _FakeWatch(name="Boats", owner="222")
    _wire_config(monkeypatch, [w])
    ok, _ = S._perform_transfer("Boats", "222", actor=None)
    assert ok and w.owner == "222"


# --------------------------------------------------------------------------
# One-off admin message to a known person (/api/telegram/send)
# --------------------------------------------------------------------------

def test_send_endpoint_exists_and_gates_unknown_people():
    """The endpoint is a convenience for people already in the circle, not a relay."""
    import inspect
    src = inspect.getsource(S.create_app) if hasattr(S, "create_app") else \
        open("web_watcher/dashboard/server.py", encoding="utf-8").read()
    assert "/api/telegram/send" in src
    assert "isn't a known person" in src
