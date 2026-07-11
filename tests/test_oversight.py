"""
Tests for the oversight agent's narration logic — the delta detection that turns
per-tick watch state into first-person events. Pure/offline: we drive the diff
helpers directly with synthetic state, no config/db/threads/Ollama.
"""

from __future__ import annotations

from web_watcher.oversight import OversightAgent, _DRY_OBS_THRESHOLD


class _FakeManager:
    def get_job_info(self):
        return []


def _agent() -> OversightAgent:
    return OversightAgent(_FakeManager())


def _w(name, *, matches=0, observations=0, running=True, error=None):
    return {"name": name, "enabled": True, "mode": "continuous",
            "running": running, "matches": matches, "observations": observations, "error": error}


def _kinds(a):
    return [e["kind"] for e in a.snapshot()["entries"]]


def _texts(a):
    return [e["text"] for e in a.snapshot()["entries"]]


def test_greeting_on_first_look():
    a = _agent()
    a._emit_greeting([_w("Trucks", matches=2, running=True)])
    entries = a.snapshot()["entries"]
    assert entries and entries[0]["kind"] == "status"
    assert "watch" in entries[0]["text"].lower()


def test_greeting_with_no_watches_invites_setup():
    a = _agent()
    a._emit_greeting([])
    assert "nothing to watch" in _texts(a)[0].lower()


def test_new_matches_are_narrated_with_delta():
    a = _agent()
    a._prev = {"Trucks": _w("Trucks", matches=2)}
    a._running_prev = {"Trucks"}
    a._narrate_deltas([_w("Trucks", matches=5)])
    assert "find" in _kinds(a)
    # the 3-new delta should appear somewhere in the line
    assert any("3" in t for t in _texts(a))


def test_dry_watch_flagged_exactly_once():
    a = _agent()
    dry = _w("Sports Cars", matches=0, observations=_DRY_OBS_THRESHOLD + 5)
    a._prev = {"Sports Cars": dry}; a._running_prev = {"Sports Cars"}
    a._narrate_deltas([dry])
    a._narrate_deltas([dry])   # second look must NOT re-nag
    assert _kinds(a).count("concern") == 1


def test_dry_watch_concern_carries_broaden_action():
    a = _agent()
    dry = _w("Sports Cars", matches=0, observations=_DRY_OBS_THRESHOLD + 5)
    a._prev = {"Sports Cars": dry}; a._running_prev = {"Sports Cars"}
    a._narrate_deltas([dry])
    concern = next(e for e in a.snapshot()["entries"] if e["kind"] == "concern")
    assert concern["action"] == {"type": "broaden_terms", "watch": "Sports Cars",
                                 "label": "Broaden its search terms"}


def test_match_clears_dry_flag_so_it_can_warn_again():
    a = _agent()
    dry = _w("Sports Cars", matches=0, observations=_DRY_OBS_THRESHOLD + 5)
    a._prev = {"Sports Cars": dry}; a._running_prev = {"Sports Cars"}
    a._narrate_deltas([dry])
    # it finally matches something → flag clears
    a._narrate_deltas([_w("Sports Cars", matches=1, observations=_DRY_OBS_THRESHOLD + 6)])
    assert "Sports Cars" not in a._dry_flagged


def test_error_narrated_once_then_cleared():
    a = _agent()
    a._prev = {"W": _w("W")}; a._running_prev = {"W"}
    a._narrate_deltas([_w("W", error="boom")])
    a._narrate_deltas([_w("W", error="boom")])   # same error, no repeat
    assert _kinds(a).count("concern") == 1
    # a clean run clears the memory so a future error narrates again
    a._narrate_deltas([_w("W")])
    a._narrate_deltas([_w("W", error="boom")])
    assert _kinds(a).count("concern") == 2


def test_started_and_stopped_transitions():
    a = _agent()
    a._prev = {"W": _w("W", running=False)}; a._running_prev = set()
    a._narrate_deltas([_w("W", running=True)])
    assert "decision" in _kinds(a)
    # now it stops
    a._prev = {"W": _w("W", running=True)}; a._running_prev = {"W"}
    a._narrate_deltas([_w("W", running=False)])
    assert any("stopped" in t.lower() for t in _texts(a))


def test_note_injects_external_narration():
    # The orchestrator narrates its decisions through The Watcher via note().
    a = _agent()
    a.note("decision", "Checking 'Trucks' next — longest since I looked.", watch="Trucks")
    e = a.snapshot()["entries"][0]
    assert e["kind"] == "decision" and "Trucks" in e["text"] and e["watch"] == "Trucks"


def test_snapshot_is_newest_first_and_bounded():
    a = _agent()
    for i in range(80):
        a._emit("status", f"line {i}")
    snap = a.snapshot(limit=10)
    assert len(snap["entries"]) == 10
    assert snap["entries"][0]["text"] == "line 79"   # newest first


def test_removed_watch_is_narrated():
    a = _agent()
    a._prev = {"Trucks": _w("Trucks")}
    a._running_prev = {"Trucks"}
    a._narrate_deltas([])   # watch deleted, nothing left
    texts = _texts(a)
    assert any("removed" in t and "Trucks" in t for t in texts)
    # last-one case invites the user to set up the next watch
    assert any("last one" in t for t in texts)


def test_added_watch_is_narrated():
    a = _agent()
    a._prev = {"Trucks": _w("Trucks")}
    a._running_prev = {"Trucks"}
    a._narrate_deltas([_w("Trucks"), _w("Boats", running=False)])
    assert any("New watch 'Boats'" in t for t in _texts(a))


def test_removed_watch_forgets_flags_for_clean_recreate():
    a = _agent()
    dry = _w("Trucks", matches=0, observations=_DRY_OBS_THRESHOLD + 5)
    a._prev = {"Trucks": dry}; a._running_prev = {"Trucks"}
    a._narrate_deltas([dry])                      # flags it dry
    assert "Trucks" in a._dry_flagged
    a._prev = {"Trucks": dry}
    a._narrate_deltas([])                         # deleted
    assert "Trucks" not in a._dry_flagged
