"""Per-site challenge cooldown — backing off from a site that pushed back.

The behaviour this replaces: when a challenge could not be cleared, the agent convened a
recovery council and kept poking the same challenged page, then returned a minute later.
Retrying a failed human-check over and over is the most bot-like thing the app could do.
"""
from __future__ import annotations
import time
from web_watcher import sitecool


def test_host_is_keyed_to_the_site_not_the_subdomain(tmp_path):
    # A challenge from skagit.craigslist.org means CRAIGSLIST is unhappy — rest them together.
    assert sitecool.host_of("https://skagit.craigslist.org/search/boo") == "craigslist.org"
    assert sitecool.host_of("https://www.craigslist.org/x") == "craigslist.org"
    assert sitecool.host_of("https://offerup.com/search") == "offerup.com"
    assert sitecool.host_of("not a url") == ""


def test_challenge_rests_the_site(tmp_path):
    assert sitecool.cooling_for("https://boattrader.com/x", tmp_path) == 0
    sitecool.record_challenge("https://boattrader.com/x", "CAPTCHA", solved=False, data_dir=tmp_path)
    assert sitecool.is_cooling("https://boattrader.com/other", tmp_path) is True
    assert sitecool.cooling_for("https://boattrader.com/x", tmp_path) > 60


def test_backoff_doubles_on_consecutive_challenges(tmp_path):
    a = sitecool.record_challenge("https://x.com/1", solved=False, data_dir=tmp_path)
    b = sitecool.record_challenge("https://x.com/2", solved=False, data_dir=tmp_path)
    assert b["streak"] == 2
    assert (b["until"] - b["at"]) > (a["until"] - a["at"])   # rests longer each time


def test_a_solved_challenge_still_rests_the_site_but_lighter(tmp_path):
    # Being challenged AT ALL means we were close enough to the line to trip it. Charging on at
    # full speed is what turns one challenge into a block.
    solved = sitecool.record_challenge("https://a.com/1", solved=True, data_dir=tmp_path)
    unsolved = sitecool.record_challenge("https://b.com/1", solved=False, data_dir=tmp_path)
    assert (solved["until"] - solved["at"]) > 0
    assert (solved["until"] - solved["at"]) < (unsolved["until"] - unsolved["at"])


def test_cooldown_is_capped(tmp_path):
    for _ in range(20):
        rec = sitecool.record_challenge("https://y.com/1", solved=False, data_dir=tmp_path)
    assert (rec["until"] - rec["at"]) <= 8 * 60 * 60 + 1


def test_human_can_clear_a_site(tmp_path):
    sitecool.record_challenge("https://z.com/1", solved=False, data_dir=tmp_path)
    assert sitecool.clear_site("https://z.com/1", tmp_path) is True
    assert sitecool.is_cooling("https://z.com/1", tmp_path) is False
    assert sitecool.clear_site("https://z.com/1", tmp_path) is False   # nothing left to clear


def test_active_lists_only_live_cooldowns(tmp_path):
    sitecool.record_challenge("https://live.com/1", solved=False, data_dir=tmp_path)
    assert [r["host"] for r in sitecool.active(tmp_path)] == ["live.com"]


def test_corrupt_state_never_wedges_a_watch(tmp_path):
    (tmp_path / "site_cooldowns.json").write_text("{{{not json", encoding="utf-8")
    assert sitecool.cooling_for("https://any.com/1", tmp_path) == 0   # fails OPEN, not closed


# ── native dialogs must never be clickable or callable ────────────────────────────
# A native print dialog is drawn by the browser/OS, not the page, so automation cannot see or
# dismiss it — the browser hangs until a human clicks. Observed live: the agent clicked
# craigslist's 'print' on a listing page and the sweep froze for six minutes.

def test_print_and_other_native_dialog_controls_are_blocked():
    from web_watcher.fb_safety import is_mutating_action
    for label in ("print", "Print", "print this posting", "download", "save as", "export"):
        assert is_mutating_action(label) is True, label


def test_normal_browsing_controls_are_still_allowed():
    # A guard that stops the agent browsing is worse than no guard.
    from web_watcher.fb_safety import is_mutating_action
    for label in ("newest", "price", "sort by", "show more", "next page",
                  "MacGregor 26X - $16,500", "boat type", "condition"):
        assert is_mutating_action(label) is False, label


def test_browser_neuters_native_dialogs_in_page():
    # Belt and braces: even an UNLABELLED control calling window.print() must not hang us.
    from web_watcher.browser import _NO_NATIVE_DIALOGS_JS
    js = _NO_NATIVE_DIALOGS_JS
    assert "window" in js and "print" in js
    assert "beforeprint" in js          # print triggered from a handler
    assert "confirm" in js and "prompt" in js
