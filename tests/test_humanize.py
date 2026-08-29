"""The shared motor model — the tells a person watching the screen called out, pinned.

The user watched a live sweep and said: "superhuman quick especially typing and hitting the
right buttons and not overshooting at all... the same movements and patterns over and over."
Each test here guards one of those observations. All timing is captured by monkeypatching
time.sleep (no real waiting), and the mouse/keyboard are fakes that record what they were told.
"""

from __future__ import annotations

import math
import random

import pytest

from web_watcher import humanize


class _FakeMouse:
    def __init__(self):
        self.path: list[tuple[float, float]] = []

    def move(self, x, y):
        self.path.append((float(x), float(y)))


class _FakeKeyboard:
    def __init__(self):
        self.events: list[str] = []          # "type:x" / "press:Backspace"

    def type(self, ch):
        self.events.append(f"type:{ch}")

    def press(self, key):
        self.events.append(f"press:{key}")

    def typed_text(self) -> str:
        """Replay the event stream into the string a real input box would hold."""
        out = []
        for e in self.events:
            if e.startswith("type:"):
                out.append(e[5:])
            elif e == "press:Backspace" and out:
                out.pop()
        return "".join(out)


class _FakePage:
    def __init__(self):
        self.mouse = _FakeMouse()
        self.keyboard = _FakeKeyboard()


@pytest.fixture(autouse=True)
def _restore_global_rng():
    """These tests seed the GLOBAL random module for reproducibility — without restoring it,
    every later test in the run inherits a deterministic RNG state, and a statistical test
    elsewhere (test_human_read's budget expectations) failed only in the full suite."""
    state = random.getstate()
    yield
    random.setstate(state)


@pytest.fixture
def fast_clock(monkeypatch):
    """time.sleep records instead of sleeping — tests read the delays, and run instantly."""
    slept: list[float] = []
    monkeypatch.setattr(humanize.time, "sleep", lambda s: slept.append(float(s)))
    return slept


# ── typing ───────────────────────────────────────────────────────────────────────

def test_typing_is_not_a_metronome(fast_clock):
    """THE observed tell: .type(delay=randint(...)) samples ONE delay and repeats it for
    every keystroke. Per-key delays must actually vary."""
    page = _FakePage()
    random.seed(7)
    humanize.type_text(page, "fiat x19 anacortes")
    per_key = fast_clock
    assert len(per_key) >= len("fiat x19 anacortes")
    assert len(set(round(d, 4) for d in per_key)) > 3     # a metronome would collapse to 1

    mean = sum(per_key) / len(per_key)
    assert 0.04 < mean < 0.35                             # human-ish, not instant / stalled


def test_typed_text_is_always_exact_even_with_a_typo(fast_clock, monkeypatch):
    """Force the typo path every time: whatever gets fat-fingered must be backspaced so the
    final text is exactly what was asked."""
    monkeypatch.setattr(humanize.random, "random", lambda: 0.0)   # typo + every think-pause
    for seed in range(12):
        page = _FakePage()
        random.seed(seed)
        humanize.type_text(page, "macgregor sailboat")
        assert page.keyboard.typed_text() == "macgregor sailboat"
        assert "press:Backspace" in page.keyboard.events          # the typo really happened


def test_short_text_never_typos(fast_clock, monkeypatch):
    monkeypatch.setattr(humanize.random, "random", lambda: 0.0)
    page = _FakePage()
    humanize.type_text(page, "fiat")
    assert "press:Backspace" not in page.keyboard.events
    assert page.keyboard.typed_text() == "fiat"


# ── mouse ────────────────────────────────────────────────────────────────────────

def test_two_identical_moves_take_different_paths(fast_clock):
    """"The same movements and patterns over and over" — two approaches to the same target
    must not replay the same path."""
    a, b = _FakePage(), _FakePage()
    humanize.move(a, 600, 400)
    humanize.move(b, 600, 400)
    assert a.mouse.path != b.mouse.path
    assert a.mouse.path[-1] == (600.0, 400.0)             # both still END exactly on target
    assert b.mouse.path[-1] == (600.0, 400.0)


def test_long_moves_have_more_steps_than_short_ones(fast_clock):
    far, near = _FakePage(), _FakePage()
    far._ww_mouse = (0.0, 0.0)
    near._ww_mouse = (580.0, 390.0)
    humanize.move(far, 900, 600)
    humanize.move(near, 600, 400)
    assert len(far.mouse.path) > len(near.mouse.path)


def test_long_moves_overshoot_the_target(fast_clock):
    """"not overshooting at all" — a cross-screen approach should sail past and settle back.
    Statistical: across seeds, most long moves must contain a point beyond the target along
    the travel direction."""
    overshot = 0
    for seed in range(20):
        random.seed(seed)
        page = _FakePage()
        page._ww_mouse = (100.0, 100.0)
        humanize.move(page, 900, 500)
        dx, dy = 900 - 100, 500 - 100
        dl = math.hypot(dx, dy)
        ux, uy = dx / dl, dy / dl
        # projection of each path point onto the travel direction, relative to the target
        if any((x - 900) * ux + (y - 500) * uy > 1.0 for x, y in page.mouse.path):
            overshot += 1
    assert overshot >= 14                                  # most, not necessarily all
    assert page.mouse.path[-1] == (900.0, 500.0)


def test_cursor_memory_is_per_page_not_global(fast_clock):
    """The agent's old memory was a module-global: the cursor on tab B "resumed" from where
    tab A left it. Memory now rides on the Page object."""
    a, b = _FakePage(), _FakePage()
    humanize.move(a, 300, 300)
    humanize.move(b, 700, 200)
    assert a._ww_mouse == (300, 300)
    assert b._ww_mouse == (700, 200)


def test_fresh_page_approach_direction_varies(fast_clock):
    """The nav stack's old approach ALWAYS came from the upper-left (tx-60..220, ty-40..160).
    With no cursor memory the first path point should scatter around the target, not sit in
    one quadrant."""
    quadrants = set()
    for seed in range(30):
        random.seed(seed)
        page = _FakePage()
        humanize.move(page, 500, 400)
        x0, y0 = page.mouse.path[0]
        quadrants.add((x0 < 500, y0 < 400))
    assert len(quadrants) >= 3                             # up-left-only would be exactly 1


# ── the wiring (weak twin really retired) ────────────────────────────────────────

def test_navigate_click_delegates_to_the_shared_model(monkeypatch):
    from web_watcher import navigate
    called = {}
    monkeypatch.setattr(humanize, "click", lambda page, loc, timeout=5000:
                        (called.setdefault("args", (page, loc, timeout)), True)[1])
    assert navigate._human_click("PAGE", "LOC", 1234) is True
    assert called["args"] == ("PAGE", "LOC", 1234)


def test_agent_type_delegates_to_the_shared_model(monkeypatch):
    from web_watcher import agent
    seen = {}
    monkeypatch.setattr(humanize, "type_text", lambda page, text: seen.setdefault("t", text))
    agent._human_type(_FakePage(), "hello there")
    assert seen["t"] == "hello there"
