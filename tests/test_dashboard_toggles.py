"""Every Settings toggle must flip on a single click.

The switches are a hidden <input type=checkbox> inside a <label>, with a styled .toggle-track
div. The track ALSO carried onclick="...chk.click()", so one user click toggled the box twice —
once via the handler, once via the label's native forwarding — and it landed back where it
started. Live in the browser: change fired 2x and before === after on ALL FIVE toggles, so the
switch looked dead. The label already forwards the click; the onclick was redundant.

This is a static guard: no toggle track may carry that onclick again."""

from __future__ import annotations

import re
from pathlib import Path

_HTML = Path(__file__).resolve().parent.parent / "web_watcher" / "dashboard" / "static" / "index.html"


def _tracks() -> list[str]:
    html = _HTML.read_text(encoding="utf-8")
    return re.findall(r'<div class="toggle-track"[^>]*>', html)


def test_toggles_exist():
    assert len(_tracks()) >= 5, "expected the Settings toggles to be present"


def test_no_toggle_track_reclicks_its_checkbox():
    offenders = [t for t in _tracks() if "onclick" in t]
    assert not offenders, (
        "A toggle track has an onclick that clicks its own checkbox. The wrapping <label> "
        "already forwards the click, so this toggles twice and the switch does nothing:\n  "
        + "\n  ".join(offenders))


def test_each_toggle_track_has_an_id_for_the_state_helpers():
    # _apply*Toggle() paint the track/label from the id; a missing id silently stops the
    # switch from ever showing 'On'.
    assert all('id="' in t for t in _tracks())
