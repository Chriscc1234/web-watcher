"""Chat Review — the audit of the app's own conversations.

The mechanical pass must be certain (a regex finding is a fact, so a false positive here is
worse than a missed one), the chunker must never hand the model more than it can read, and the
watermark must make a second review skip what the first already covered. The model pass itself is
stubbed — what's tested is that we only keep findings that cite REAL turns. See web_watcher/review.py."""

from __future__ import annotations

import json

import pytest

from web_watcher import review as R


# ── collecting turns ─────────────────────────────────────────────────────────────

@pytest.fixture()
def threads(monkeypatch, tmp_path):
    """Two stored conversations in a temp data dir."""
    from web_watcher import paths
    main = tmp_path / "watcher_history.json"
    main.write_text(json.dumps([
        {"role": "user", "content": "desktop question", "ts": 100},
        {"role": "assistant", "content": "desktop answer", "ts": 101},
    ]), encoding="utf-8")
    (tmp_path / "watcher_history_555.json").write_text(json.dumps([
        {"role": "user", "content": "buddy question", "ts": 200},
        {"role": "assistant", "content": "buddy answer", "ts": 201},
    ]), encoding="utf-8")
    (tmp_path / "watcher_owners.json").write_text(json.dumps({"555": "Buddy"}), encoding="utf-8")
    monkeypatch.setattr(paths, "watcher_history_path", lambda: main)
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    return tmp_path


def test_collect_turns_merges_threads_in_time_order(threads):
    turns = R.collect_turns(0.0)
    assert [t["content"] for t in turns] == [
        "desktop question", "desktop answer", "buddy question", "buddy answer"]
    assert turns[0]["thread"] == "Desktop (you)"
    assert turns[2]["thread"] == "Buddy"            # the remembered name, not the raw chat id
    assert [t["id"] for t in turns] == [0, 1, 2, 3]  # stable ids for citation


def test_collect_turns_respects_the_watermark(threads):
    assert [t["content"] for t in R.collect_turns(150.0)] == ["buddy question", "buddy answer"]
    assert R.collect_turns(9_999.0) == []


def test_watermark_round_trips(threads):
    assert R.watermark()["last_ts"] == 0.0
    R.set_watermark(201.0)
    assert R.watermark()["last_ts"] == 201.0


def test_unreadable_thread_is_skipped_not_fatal(threads):
    (threads / "watcher_history_bad.json").write_text("{ not json", encoding="utf-8")
    assert len(R.collect_turns(0.0)) == 4          # the good threads still come through


# ── the mechanical pass ──────────────────────────────────────────────────────────

def _t(i, role, content, ts=1000):
    return {"id": i, "thread": "T", "owner": "1", "role": role, "content": content, "ts": ts}


def _kinds(turns):
    return {f["kind"] for f in R.scan_mechanical(turns)}


def test_raw_html_in_a_reply_is_flagged_high():
    found = R.scan_mechanical([_t(0, "assistant", "<i>Change yours:</i> say 'every 6 hours'")])
    assert found and found[0]["kind"] == "raw_html" and found[0]["severity"] == "high"
    assert found[0]["turns"] == [0]


def test_prose_that_merely_mentions_a_tag_is_not_html():
    # "<30 feet" and a bare comparison must not read as markup.
    assert "raw_html" not in _kinds([_t(0, "assistant", "boats <30 feet, price > 5000")])


def test_empty_and_duplicate_replies_are_flagged():
    assert "empty_reply" in _kinds([_t(0, "assistant", " ")])
    assert "repeated_reply" in _kinds([_t(0, "assistant", "same"), _t(1, "assistant", "same")])


def test_error_text_shown_to_the_user_is_flagged():
    assert "error_text" in _kinds([_t(0, "assistant", "Sorry, the server timed out.")])


def test_unanswered_question_is_flagged():
    turns = [_t(0, "user", "did you see my watch request?"), _t(1, "user", "hello?")]
    found = [f for f in R.scan_mechanical(turns) if f["kind"] == "unanswered"]
    assert found and found[0]["turns"] == [0, 1]


def test_a_normal_exchange_produces_nothing():
    assert R.scan_mechanical([
        _t(0, "user", "how many watches do I have?"),
        _t(1, "assistant", "You have two: the manual-transmission cars watch and the boats watch."),
    ]) == []


# ── chunking ─────────────────────────────────────────────────────────────────────

def test_chunks_respect_the_turn_limit():
    turns = [_t(i, "user", "x") for i in range(30)]
    chunks = R._chunks(turns)
    assert all(len(c) <= R._CHUNK_TURNS for c in chunks)
    assert sum(len(c) for c in chunks) == 30            # nothing dropped


def test_chunks_respect_the_character_limit():
    turns = [_t(i, "user", "y" * 5_000) for i in range(4)]
    chunks = R._chunks(turns)
    assert all(sum(len(t["content"]) for t in c) <= R._CHUNK_CHARS + 5_000 for c in chunks)
    assert sum(len(c) for c in chunks) == 4


def test_one_oversized_turn_is_truncated_when_rendered():
    text = R._render([_t(0, "user", "z" * 9_000)])
    assert "truncated" in text and len(text) < 3_000


# ── the model pass ───────────────────────────────────────────────────────────────

def test_findings_citing_turns_outside_the_chunk_are_dropped(monkeypatch):
    from web_watcher import llm
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps({"findings": [
        {"severity": "high", "kind": "Confused Watch", "what": "mixed up two watches",
         "turns": [0, 99], "check_live": "open the watch list"},
        {"severity": "low", "kind": "junk", "what": "", "turns": [0]},      # no substance → dropped
    ]}))
    out = R._judge_chunk([_t(0, "user", "hi")], "m", None)
    assert len(out) == 1
    assert out[0]["turns"] == [0]                     # 99 never existed — not carried into the report
    assert out[0]["kind"] == "confused_watch"         # normalized


def test_review_states_the_context_window_it_needs(monkeypatch):
    """Ollama truncates silently past its small default window, so a long read MUST ask for room."""
    seen = {}

    def fake_chat(messages, **kw):
        seen.update(kw)
        return json.dumps({"findings": []})

    from web_watcher import llm
    monkeypatch.setattr(llm, "chat", fake_chat)
    R._judge_chunk([_t(0, "user", "hi")], "qwen2.5:72b", None)
    assert seen["num_ctx"] >= 8_192
    assert seen["force_local"] is True                # the audit is a local-quality-tier job
    assert seen["timeout"] >= 600                     # "zero limits on time"


def test_full_review_merges_both_passes_and_advances_the_watermark(threads, monkeypatch):
    from web_watcher import llm
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps({"findings": [
        {"severity": "medium", "kind": "vague", "what": "the answer dodged the question",
         "turns": [1], "check_live": "re-ask it in the app"}]}))
    monkeypatch.setattr(R, "resolve_review_model", lambda cfg=None: "stub-model")

    report = R.review_chats(cfg=None)
    assert report["turns_reviewed"] == 4
    assert report["model"] == "stub-model"
    assert any(f["source"] == "model" for f in report["findings"])
    assert report["findings"][0]["evidence"]                   # real text attached, not a paraphrase
    assert R.watermark()["last_ts"] == 201.0                   # next run starts after these
    assert R.latest_report()["turns_reviewed"] == 4            # saved to disk

    # A second run has nothing new to say.
    assert R.review_chats(cfg=None)["turns_reviewed"] == 0


def test_a_failing_chunk_does_not_lose_the_rest_of_the_report(threads, monkeypatch):
    from web_watcher import llm

    def boom(*a, **k):
        raise RuntimeError("ollama fell over")

    monkeypatch.setattr(llm, "chat", boom)
    monkeypatch.setattr(R, "resolve_review_model", lambda cfg=None: "stub-model")
    report = R.review_chats(cfg=None)
    assert "could not be read" in report["note"]              # the failure is stated, not hidden
    assert report["turns_reviewed"] == 4                      # mechanical findings still stand


def test_render_report_is_plain_text():
    report = R._report(0.0, "m", 0.0, [{"severity": "high", "kind": "raw_html", "what": "tags",
                                        "turns": [0], "check_live": "look here", "source": "mechanical"}],
                       [_t(0, "assistant", "<b>x</b>")], ["T"])
    text = R.render_report(report)
    assert "raw_html" in text and "look here" in text
    assert "<b>" in text                    # quoting the evidence verbatim is the point
    assert not text.startswith("<")         # but the report itself carries no markup of its own


def test_render_report_handles_no_review_yet():
    assert "No review" in R.render_report({})


# ── running on a schedule ────────────────────────────────────────────────────────
# The audit is slow by design, so when it runs matters: off unless asked for, on the user's
# interval, and never on top of a sweep (the big model would hold the GPU for a long time).

def test_review_is_off_by_default():
    from web_watcher.config import AppConfig
    rc = AppConfig().review
    assert rc.enabled is False and rc.every_hours == 24.0 and rc.notify is True


def test_review_settings_round_trip(monkeypatch):
    from unittest.mock import MagicMock
    from fastapi.testclient import TestClient
    from web_watcher.config import AppConfig
    from web_watcher.dashboard.server import create_app
    from web_watcher.dashboard import server as S
    import web_watcher.config as C

    cfg = AppConfig(watches=[])
    monkeypatch.setattr(C, "load", lambda: cfg)
    monkeypatch.setattr(C, "save", lambda c: None)
    monkeypatch.setattr(S, "_load_cfg", lambda: cfg)
    client = TestClient(create_app(MagicMock()))

    assert client.get("/api/review/settings").json()["enabled"] is False
    r = client.post("/api/review/settings", json={"enabled": True, "every_hours": 6}).json()
    assert r["enabled"] is True and r["every_hours"] == 6.0
    # An interval below an hour would mean auditing more or less continuously.
    assert client.post("/api/review/settings",
                       json={"every_hours": 0.1}).json()["every_hours"] == 1.0


def test_a_scheduled_run_waits_for_a_quiet_moment(monkeypatch, tmp_path):
    """The 72b holds the GPU for a long time — it must never start on top of a sweep."""
    from web_watcher.services import ServiceManager
    m = ServiceManager()
    started = []
    monkeypatch.setattr(m, "review_start", lambda *a, **k: started.append(1))
    monkeypatch.setattr(m, "orchestrator_running", lambda: True)      # watching is busy
    monkeypatch.setattr(m, "review_status", lambda: {"status": "idle"})

    from web_watcher.config import AppConfig, ReviewConfig
    import web_watcher.config as C
    monkeypatch.setattr(C, "load", lambda: AppConfig(review=ReviewConfig(enabled=True, every_hours=1)))
    monkeypatch.setattr(R, "watermark", lambda data_dir=None: {"last_run_at": 0.0})

    # Exactly one pass of the loop body, then stop (wait() False = "keep going").
    calls = {"n": 0}

    def fake_wait(timeout=None):
        calls["n"] += 1
        return calls["n"] > 1
    monkeypatch.setattr(m._review_stop, "wait", fake_wait)
    m._review_scheduler()
    assert started == []          # deferred while watching is busy, not run
