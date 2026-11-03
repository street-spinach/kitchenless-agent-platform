"""`negotiate`: the household's code-only counter (agents §7, AG-A2, AG-A3; issue #202).

The counter is sent only when all six rules hold: a household order, no counter yet, the
best offer not final, budget left, the best offer over it, and a runner-up that covers as
many lines. It is always at the remaining budget, and no model is called.
"""

import asyncio
import uuid
from datetime import timedelta
from typing import Any

from services.agents.household import graph as hg
from services.common import ids
from tests.unit.test_household_graph import HOUSEHOLD, NOW, World

BEST = "grocery:square-us"
RUNNER_UP = "grocery:mock-us"


def _offer(w: World, seller: str, total: int, *, covered: int, lines: int = 2) -> Any:
    """An offer with `lines` lines, of which the first `covered` have an `offered_sku`."""
    body = {
        "order_id": w.order_id,
        "quote_id": str(uuid.uuid5(uuid.NAMESPACE_URL, seller + w.order_id)),
        "round": 0,
        "lines": [
            {
                "line": n,
                "requested_item": f"item {n}",
                "offered_sku": f"{seller}-{n}" if n <= covered else None,
                "offered_name": f"Item {n}" if n <= covered else None,
                "qty": 1,
                "unit_minor": total // max(covered, 1),
            }
            for n in range(1, lines + 1)
        ],
        "total_minor": total,
        "currency": "USD",
        "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
        "final": False,
    }
    return w.step(w.inbound("offer", body, from_agent=seller))


def _negotiate_choices(w: World) -> list[str]:
    return [d["choice"] for d in w.traces.of("decision") if d["node"] == "negotiate"]


def test_counter_at_remaining_to_best_seller() -> None:
    w = World(remaining_minor=2000)
    w.to_quoting()
    w.offer(RUNNER_UP, 3000)
    w.offer(BEST, 2500)
    [counter] = w.round_closed().messages
    assert (counter.type, counter.to_agent) == ("counter_offer", BEST)
    assert counter.body["total_minor"] == 2000
    assert counter.body["quote_id"] == str(uuid.uuid5(uuid.NAMESPACE_URL, BEST + w.order_id))
    saved = w.saved()
    assert saved["counter"] == {"seller": BEST, "from_minor": 2500, "to_minor": 2000}
    assert (saved["counters"], saved["awaiting_counter"]) == (1, BEST)
    [decision] = [d for d in w.traces.of("decision") if d["node"] == "negotiate"]
    assert decision["choice"] == "counter"
    assert decision["explanation"] == "best=2500 remaining=2000 counter=2000"
    # No model call decides the counter.
    assert [name for name, _ in w.stubs.seen].count("negotiate") == 0


def test_single_valid_offer_no_counter() -> None:
    w = World(remaining_minor=2000)
    w.to_quoting()
    w.offer(BEST, 2500)
    assert w.round_closed().messages == []
    assert _negotiate_choices(w) == ["choose"]
    [asked] = w.household.of("request_approval")
    assert asked["args"]["amount"]["amount_minor"] == 2500


def test_runner_up_with_fewer_lines_no_counter() -> None:
    w = World(remaining_minor=2000)
    w.to_quoting()
    _offer(w, BEST, 2500, covered=2)
    _offer(w, RUNNER_UP, 1000, covered=1)
    assert w.round_closed().messages == []
    assert _negotiate_choices(w) == ["choose"]
    [asked] = w.household.of("request_approval")
    assert asked["args"]["amount"]["amount_minor"] == 2500  # full coverage beats cheaper


def test_final_offer_no_counter() -> None:
    w = World(remaining_minor=2000)
    w.to_quoting()
    w.offer(RUNNER_UP, 3000)
    w.offer(BEST, 2500, round=1)  # `final: true`
    assert w.round_closed().messages == []
    assert _negotiate_choices(w) == ["choose"]
    assert w.saved().get("counter") is None


def test_solo_order_no_counter() -> None:
    w = World(remaining_minor=2000)
    w.to_quoting()
    w.offer(RUNNER_UP, 3000)
    w.offer(BEST, 2500)
    # `start_order` stores `scope` from `StartOrder` in build step 9; set it directly here.
    config = {"configurable": {"thread_id": ids.thread_id(HOUSEHOLD, w.run_id)}}
    asyncio.run(w.agent.graph.aupdate_state(config, {"scope": "solo"}, as_node="send"))
    assert w.round_closed().messages == []
    assert _negotiate_choices(w) == ["choose"]
    assert len(w.household.of("request_approval")) == 1


def test_no_budget_left_no_counter() -> None:
    w = World(remaining_minor=0)
    w.to_quoting()
    w.offer(RUNNER_UP, 3000)
    w.offer(BEST, 2500)
    assert w.round_closed().messages == []
    assert _negotiate_choices(w) == ["choose"]


def test_after_one_counter_chooses() -> None:
    w = World(remaining_minor=2000)
    w.to_quoting()
    w.offer(RUNNER_UP, 3000)
    w.offer(BEST, 2500)
    w.round_closed()
    # The seller counters at its floor, still over budget: no second counter.
    reply = w.offer(BEST, 2250, round=0)
    assert reply.messages == []
    assert _negotiate_choices(w) == ["counter", "choose"]
    [asked] = w.household.of("request_approval")
    assert asked["args"]["amount"]["amount_minor"] == 2250


def test_counter_agreed_authorizes_at_counter_total() -> None:
    w = World(remaining_minor=2000)
    w.to_quoting()
    w.offer(RUNNER_UP, 3000)
    w.offer(BEST, 2500)
    w.round_closed()
    [accept] = w.offer(BEST, 2000, round=1).messages
    assert (accept.type, accept.to_agent, accept.body["total_minor"]) == ("accept", BEST, 2000)
    assert w.household.of("request_approval") == []


def test_counter_rejected_takes_equal_coverage_runner_up() -> None:
    w = World(remaining_minor=2000)
    w.to_quoting()
    w.offer(RUNNER_UP, 3000)
    w.offer(BEST, 2500)
    [counter] = w.round_closed().messages
    assert counter.to_agent == BEST
    result = w.reject(BEST, counter.body["quote_id"], "price")
    assert result.messages == []
    assert w.saved()["excluded_sellers"] == [BEST]
    # The runner-up covers as many lines, so the reject cost price, never items.
    [asked] = w.household.of("request_approval")
    assert asked["args"]["amount"] == {"amount_minor": 3000, "currency": "USD"}
    assert w.saved()["chosen_quote_id"] == str(
        uuid.uuid5(uuid.NAMESPACE_URL, RUNNER_UP + w.order_id)
    )
    assert w.saved()["phase"] == hg.AWAITING_APPROVAL
    assert w.household.of("authorize_spend") == []
