"""`authorize` routes a `needs_approval` refusal to `request_approval` (agents §7, AG10,
AG-A7; AG-CUJ 6).

A refusal after an approval that covers the total ends the run `error, approval_loop`: the
approver is never asked twice for the same amount.
"""

from services.agents.household import graph as hg
from services.common.consumer import RunFinish
from tests.unit.test_household_graph import World

SELLER = "grocery:square-us"


def _refused_within_budget() -> tuple[World, object]:
    """An offer within the remaining budget that the guard refuses with `needs_approval`
    (for example the child rule, AG11)."""
    w = World()
    w.household.refuse["authorize_spend"] = "needs_approval"
    w.to_quoting()
    w.offer(SELLER, 2000)
    return w, w.round_closed()


def _authorize_choices(w: World) -> list[str]:
    return [d["choice"] for d in w.traces.of("decision") if d["node"] == "authorize"]


def test_needs_approval_refusal_routes_to_request_approval() -> None:
    w, result = _refused_within_budget()
    [spend] = w.household.of("authorize_spend")
    assert spend["args"]["total"] == {"amount_minor": 2000, "currency": "USD"}
    [asked] = w.household.of("request_approval")
    assert asked["args"]["amount"] == {"amount_minor": 2000, "currency": "USD"}
    assert result.messages == []
    assert result.finish is None
    saved = w.saved()
    assert saved["phase"] == hg.AWAITING_APPROVAL
    assert saved["approval_minor"] == 2000
    assert _authorize_choices(w) == ["needs_approval_routed"]


def test_order_still_negotiating_after_refusal() -> None:
    w, _ = _refused_within_budget()
    # The refusal changed nothing: the order's last transition is `quoting → negotiating`.
    moves = [(c["args"]["expected"], c["args"]["new"]) for c in w.household.of("set_order_state")]
    assert moves[-1] == ("quoting", "negotiating")


def test_second_refusal_after_covering_approval_is_approval_loop() -> None:
    w, _ = _refused_within_budget()
    result = w.approve()
    assert len(w.household.of("authorize_spend")) == 2
    # Never asks a second time for the same amount.
    assert len(w.household.of("request_approval")) == 1
    assert result.messages == []
    assert result.finish == RunFinish("error", "approval_loop")
    assert w.saved()["phase"] == hg.DONE
    assert _authorize_choices(w) == ["needs_approval_routed", "approval_loop"]


def test_other_refusals_still_end_the_run() -> None:
    w = World()
    w.household.refuse["authorize_spend"] = "allergen_conflict"
    w.to_quoting()
    w.offer(SELLER, 2000)
    result = w.round_closed()
    assert result.finish == RunFinish("error", "allergen_conflict")
    assert w.household.of("request_approval") == []
