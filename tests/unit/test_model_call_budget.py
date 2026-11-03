"""Spark calls per graph stay within the §13 table (agents §13, AG14).

Each test counts `ModelClient.complete()` calls with the stub model, one graph at a time,
with the default seed: 3 stores for the weekly order and 2 restaurants for a dinner. The
shortfall and re-quote rows are not counted here.
"""

from collections import Counter
from collections.abc import Iterator
from typing import Any

import pytest

from services.agents.common.model import ModelClient
from services.common.consumer import RunFinish
from tests.unit import test_consumer_graph as cgt
from tests.unit.test_household_graph import MEMBERS, STARTER_AGENT, World
from tests.unit.test_household_instant import RESTAURANTS, SoloWorld

# Agents §13: the maximum Spark calls in each graph for each flow.
CONSUMER_BUDGET = {"chat": 2, "off_topic": 1, "learn_dislike": 2, "order": 1}
HOUSEHOLD_BUDGET = {"instant": 2, "dinner": 2, "weekly": 2}
# Agents §13, Instant row: `build_list` and the `choose_offer` explanation.
HOUSEHOLD_NODES = {("household", "build_list"): 1, ("household", "choose_offer"): 1}
# The default seed (agents §13).
STORES = ["grocery:mock-us", "grocery:square-us", "grocery:local-us"]


class Calls:
    """`complete()` calls, by agent class and node."""

    def __init__(self) -> None:
        self.nodes: Counter[tuple[str, str]] = Counter()

    def of(self, agent_class: str) -> int:
        return sum(n for (cls, _), n in self.nodes.items() if cls == agent_class)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[Calls]:
    counted = Calls()
    inner = ModelClient.complete

    async def complete(self: ModelClient, ctx: Any, *args: Any, **kwargs: Any) -> Any:
        node = args[2] if len(args) > 2 else kwargs["node"]
        counted.nodes[(ctx.agent_id.split(":", 1)[0], node)] += 1
        return await inner(self, ctx, *args, **kwargs)

    monkeypatch.setattr(ModelClient, "complete", complete)
    yield counted


# Consumer graph ------------------------------------------------------------------------


def _consumer(text: str) -> cgt.World:
    w = cgt.World(text)
    w.step(w.user_message())
    return w


def test_chat_budget(calls: Calls) -> None:
    _consumer("hi there, how are you?")
    assert calls.nodes == {("consumer", "understand_message"): 1, ("consumer", "reply_to_user"): 1}
    assert calls.of("consumer") <= CONSUMER_BUDGET["chat"]


def test_off_topic_budget(calls: Calls) -> None:
    _consumer("Book me a flight to Colombo")
    assert calls.nodes == {("consumer", "understand_message"): 1}
    assert calls.of("consumer") <= CONSUMER_BUDGET["off_topic"]


def test_learn_dislike_budget(calls: Calls) -> None:
    w = _consumer("I don't like mushrooms")
    assert w.household.learned == {"mushrooms"}
    assert calls.nodes == {("consumer", "understand_message"): 1, ("consumer", "learn_dislike"): 1}
    assert calls.of("consumer") <= CONSUMER_BUDGET["learn_dislike"]


@pytest.mark.parametrize(
    "text",
    ["Get me a chicken wrap now", "Order dinner for tonight please", "Can you do the weekly shop?"],
    ids=["instant", "dinner", "weekly"],
)
def test_order_flows_consumer_budget(calls: Calls, text: str) -> None:
    # The member's message, then the household's constraints request to the same consumer.
    w = _consumer(text)
    w.step(w.constraints_request())
    assert calls.of("consumer") <= CONSUMER_BUDGET["order"]


# Household graph -----------------------------------------------------------------------


class DinnerWorld(World):
    """A household dinner (every member asked) from the seed's 2 restaurants."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(sellers=RESTAURANTS, **kwargs)

    async def find_sellers(self, cls: str, region: str, zone: str, currency: str) -> list[str]:
        assert cls == "restaurant"
        return list(self.sellers)

    async def label_seller(self, run_id: Any, agent_id: str) -> str:
        return agent_id

    def start(self) -> Any:
        return self.step(self.inbound("start_dinner", {"kind": "dinner"}, from_agent=STARTER_AGENT))

    def to_quoting(self) -> Any:
        self.start()
        for agent in sorted(MEMBERS):
            self.constraints(agent)


class WeeklyWorld(World):
    """The weekly order from the seed's 3 stores."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(sellers=STORES, **kwargs)

    async def label_seller(self, run_id: Any, agent_id: str) -> str:
        return agent_id


def _deliver(w: World, seller: str) -> None:
    w.order_update("order_confirm", "placed", seller)
    w.order_update("delivery_booked", "courier_booked", "delivery:mock-us")
    done = w.order_update("order_status", "delivered", "system:api")
    assert done.finish == RunFinish("completed")


def _order(w: World, totals: dict[str, int]) -> str:
    """Every seller offers, the round closes, the cheapest is accepted and delivered."""
    w.to_quoting()
    for seller, total in totals.items():
        w.offer(seller, total)
    [accept] = w.round_closed().messages
    assert accept.type == "accept"
    _deliver(w, accept.to_agent)
    return accept.to_agent


def test_instant_budget(calls: Calls) -> None:
    w = SoloWorld()
    assert _order(w, {RESTAURANTS[0]: 2400, RESTAURANTS[1]: 2100}) == RESTAURANTS[1]
    assert calls.nodes == HOUSEHOLD_NODES
    assert calls.of("household") <= HOUSEHOLD_BUDGET["instant"]


def test_dinner_budget(calls: Calls) -> None:
    w = DinnerWorld()
    assert _order(w, {RESTAURANTS[0]: 2400, RESTAURANTS[1]: 2100}) == RESTAURANTS[1]
    assert calls.nodes == HOUSEHOLD_NODES
    assert calls.of("household") <= HOUSEHOLD_BUDGET["dinner"]


def test_weekly_budget(calls: Calls) -> None:
    w = WeeklyWorld()
    assert _order(w, {STORES[0]: 2400, STORES[1]: 2100, STORES[2]: 2600}) == STORES[1]
    assert calls.nodes == HOUSEHOLD_NODES
    assert calls.of("household") <= HOUSEHOLD_BUDGET["weekly"]


def test_approval_adds_no_call(calls: Calls) -> None:
    # Over budget on a solo order, which never counters: the approver is asked (+0 calls).
    w = SoloWorld(remaining_minor=2000)
    w.to_quoting()
    w.offer(RESTAURANTS[0], 3000)
    w.offer(RESTAURANTS[1], 2500)
    assert w.round_closed().messages == []
    assert len(w.household.of("request_approval")) == 1
    [accept] = w.approve().messages
    assert (accept.type, accept.to_agent) == ("accept", RESTAURANTS[1])
    _deliver(w, RESTAURANTS[1])
    assert calls.of("household") <= HOUSEHOLD_BUDGET["instant"]
