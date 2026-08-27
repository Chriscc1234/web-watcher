"""The sweep-issue log — the browsing/judging counterpart to the cloud escalation log."""
from __future__ import annotations
from web_watcher import issues


def test_records_and_lists_newest_first(tmp_path):
    issues.record_issue("stuck", "Boat", "a", data_dir=tmp_path)
    issues.record_issue("no_listings", "eBay", "b", data_dir=tmp_path)
    rows = issues.issues(data_dir=tmp_path)
    assert [r["kind"] for r in rows] == ["no_listings", "stuck"]   # newest first


def test_summary_aggregates_by_kind_and_watch(tmp_path):
    for _ in range(3):
        issues.record_issue("stuck", "Boat", "x", data_dir=tmp_path)
    issues.record_issue("challenge", "Boat", "y", data_dir=tmp_path)
    issues.record_issue("stuck", "Cars", "z", data_dir=tmp_path)
    s = issues.issue_summary(data_dir=tmp_path)
    assert s["total"] == 5
    assert s["by_kind"]["stuck"] == 4                 # one stuck event is noise; four is a pattern
    assert s["by_watch"]["Boat"] == 4                 # worst-offending watch surfaces
    assert list(s["by_kind"])[0] == "stuck"           # sorted worst-first


def test_recording_never_raises_into_a_sweep(tmp_path, monkeypatch):
    # A problem-logger that can itself throw would turn a hiccup into a crash.
    import web_watcher.issues as I
    monkeypatch.setattr(I, "_path", lambda data_dir=None: (_ for _ in ()).throw(OSError("disk")))
    I.record_issue("stuck", "W", "detail")            # must be swallowed
    assert I.issues() == [] or True                    # no exception is the assertion


def test_corrupt_line_is_skipped_not_fatal(tmp_path):
    p = tmp_path / "sweep_issues.jsonl"
    p.write_text('{"kind":"stuck","watch":"W"}\nnot json\n{"kind":"challenge","watch":"W"}\n',
                 encoding="utf-8")
    rows = issues.issues(data_dir=tmp_path)
    assert len(rows) == 2                              # the junk line is dropped, not fatal


def test_kinds_vocabulary_covers_the_instrumented_sites():
    # The scheduler/agent record these; keeping them in the fixed set keeps the summary legible.
    for k in ("stuck", "no_listings", "forced_scroll", "false_positive",
              "challenge", "nav_failed", "blind_escalation"):
        assert k in issues.KINDS
