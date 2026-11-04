"""The story line of `order_summary` (showcase §5): built in code from graph state."""

from typing import Any

from services.agents.household import graph as hg
from tests.unit.test_household_graph import World
from tests.unit.test_household_negotiate import BEST, RUNNER_UP


def _delivered(w: World) -> Any:
    w.order_update("order_confirm", "placed", BEST)
    done = w.order_update("order_status", "delivered", "system:api")
    [summary] = done.messages
    return summary


def _text(w: World) -> str:
    return str(_delivered(w).body["params"]["text"])


def test_story_line_without_a_counter() -> None:
    w = World(remaining_minor=12000)  # $30.00 already spent of $150.00
    w.accepted()
    assert _text(w).endswith(
        " I asked 2 stores. Square Grocery (simulated) won at $21.00. "
        "1 of 1 lines passed the allergy and diet checks. Spent $51.00 of $150.00 this week."
    )


def test_story_line_with_an_accepted_counter() -> None:
    w = World(remaining_minor=2000)
    w.to_quoting()
    w.offer(RUNNER_UP, 3000)
    w.offer(BEST, 2500)
    w.round_closed()
    w.offer(BEST, 2000, round=1)
    text = _text(w)
    assert " won at $20.00 after one counter-offer (was $25.00). " in text
    assert text.endswith(" Spent $150.00 of $150.00 this week.")


def test_no_counter_text_when_the_counter_changed_nothing() -> None:
    state = {
        "kind": "grocery",
        "conversations": {"a": "1"},
        "weekly_budget": {"amount_minor": 15000, "currency": "USD"},
        "remaining_minor": 2000,
        "counter": {"seller": "a", "from_minor": 2500, "to_minor": 2000},
        "chosen_quote_id": "q",
        "quotes": {"a": {"quote_id": "q", "total_minor": 2500}},
    }
    line = hg._story_line(state, "Fresh Market", 1, 2500, "USD")
    assert line is not None
    assert "counter" not in line
    assert line.startswith("I asked 1 store. Fresh Market won at $25.00. ")


def test_story_line_counts_the_shortfall_amended_order() -> None:
    w = World()
    w.accepted()
    state = {**w.saved(), "remaining_minor": 15000}
    line = hg._story_line(state, "Fresh Market", 3, 1500, "USD")
    assert line is not None
    assert " won at $15.00. 3 of 3 lines passed " in line
    assert line.endswith("Spent $15.00 of $150.00 this week.")


def test_one_line_and_no_new_data() -> None:
    w = World()
    w.accepted()
    text = _text(w)
    assert "\n" not in text
    assert "11111111" not in text  # no member id


def test_no_budget_in_state_means_no_story_line() -> None:
    # A run checkpointed before the line existed has no `weekly_budget`.
    state = {"remaining_minor": 100, "conversations": {"a": "1"}}
    assert hg._story_line(state, "Fresh Market", 1, 100, "USD") is None
    assert hg._summary("Delivered.", state, None) == "Delivered."


def test_story_line_left_out_when_it_would_break_the_body_limit() -> None:
    fits = "x" * (hg.SUMMARY_MAX_CHARS - 6)
    assert hg._summary(fits, {}, "story") == f"{fits} story"
    too_long = "x" * (hg.SUMMARY_MAX_CHARS - 5)
    assert hg._summary(too_long, {}, "story") == too_long


def test_summary_still_goes_to_the_starter_with_no_buttons_of_its_own() -> None:
    # See how is added by the sender, inside the window only (showcase §5).
    w = World()
    w.accepted()
    assert _delivered(w).body["buttons"] == []
