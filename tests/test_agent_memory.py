"""The agent's memory of its OWN actions — "have I already pushed this button, and what did it do?"

Element indices are positional: the snapshot re-sorts by viewport + y and reassigns 0..N on EVERY
step, so they shift with every scroll. The action history used to render an old action's index
against the CURRENT element list, which produced a FABRICATED memory — "step 3: click [12]
'condition'" for a click that actually hit 'boat type'. Being lied to about what it had already
tried, the agent re-clicked the same dead craigslist sort controls over and over (933 scrolls and
141 stuck-events in one real session).

Two guarantees are tested here:
  1. The label is captured AT ACTION TIME and never re-resolved (AgentAction.element_label).
  2. _tried_ledger gives a whole-run, per-CONTROL view that outlives the 10-step history window,
     and calls out controls that did nothing.

See agent.py."""

from __future__ import annotations

from web_watcher.agent import AgentAction, _tried_ledger


def _click(label, outcome="page unchanged", idx=12):
    return AgentAction(thought="", action="click", element_index=idx,
                       element_label=label, outcome=outcome)


# ── the ledger ────────────────────────────────────────────────────────────────────

def test_dead_control_is_called_out_as_pointless():
    led = _tried_ledger([_click("boat type"), _click("condition")])
    assert "boat type" in led and "condition" in led
    assert "NOTHING CHANGED" in led
    assert "do not click it again" in led.lower() or "do NOT try these again" in led


def test_repeat_count_is_shown():
    # The real failure: the same control clicked several times across a run.
    led = _tried_ledger([_click("boat type"), _click("boat type"), _click("boat type")])
    assert "3" in led and "boat type" in led


def test_a_control_that_worked_keeps_its_real_outcome():
    led = _tried_ledger([AgentAction(thought="", action="type", element_index=0,
                                     element_label="search craigslist", text="MacGregor",
                                     outcome="navigated -> results page")])
    assert "navigated -> results page" in led
    assert "NOTHING CHANGED" not in led


def test_a_real_effect_wins_over_page_unchanged_for_the_same_control():
    # Same control, two outcomes: the informative one is what the agent needs to remember.
    led = _tried_ledger([_click("sort", outcome="page unchanged"),
                         _click("sort", outcome="navigated -> sorted by price")])
    assert "navigated -> sorted by price" in led
    assert "NOTHING CHANGED" not in led


def test_scrolls_and_unlabelled_actions_are_not_in_the_ledger():
    # Scroll isn't a control you can "already have pushed", and an action with no captured label
    # can't be described truthfully — better absent than guessed.
    led = _tried_ledger([
        AgentAction(thought="", action="scroll", direction="down"),
        AgentAction(thought="", action="click", element_index=5, element_label=None,
                    outcome="page unchanged"),
    ])
    assert led == ""


def test_empty_history_yields_no_block():
    assert _tried_ledger([]) == ""


def test_ledger_is_capped_so_it_cannot_swamp_the_prompt():
    led = _tried_ledger([_click(f"control {i}") for i in range(40)])
    assert len([ln for ln in led.splitlines() if ln.strip().startswith("- ")]) <= 14


# ── the label is captured, not re-resolved ────────────────────────────────────────

def test_action_carries_the_label_it_was_chosen_against():
    # The regression: an action must remember the label from the page it acted on, so that a later
    # step (after scrolling shuffled every index) cannot rewrite what it thinks it clicked.
    a = _click("boat type", idx=12)
    b = _click("condition", idx=12)          # SAME index, different control, one scroll later
    assert a.element_label == "boat type"
    assert b.element_label == "condition"
    led = _tried_ledger([a, b])
    assert "boat type" in led and "condition" in led


def test_default_label_is_none_for_a_plain_action():
    assert AgentAction(thought="", action="scroll").element_label is None
