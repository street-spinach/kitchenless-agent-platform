"""The instant (solo) order (agents §8, AG8; AG-CUJ 3).

A `dinner` run with `scope: "solo"`: only the requester is asked, every member's allergens
filter the quote request, the window is now+30 min to now+90 min, and it never counters.
"""

import uuid
from datetime import datetime
from typing import Any

import pytest

from services.agents.household import graph as hg
from services.common.consumer import RunFinish, StepResult
from services.identity import tokens
from tests.unit.test_household_graph import MEMBERS, NOW, SECRET, STARTER, STARTER_AGENT, World

OTHER = "consumer:us-1-b"
RESTAURANTS = ["restaurant:mock-us", "restaurant:square-us"]


class SoloWorld(World):
    """Sam (the starter) orders for themself; the household has two restaurants."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(sellers=RESTAURANTS, **kwargs)
        # Kai, another member, is allergic to sesame.
        self.household.allergens = ["peanuts", "sesame"]

    async def find_sellers(self, cls: str, region: str, zone: str, currency: str) -> list[str]:
        assert (cls, region, zone, currency) == ("restaurant", "us", "us-oh-45202", "USD")
        return list(self.sellers)

    async def label_seller(self, run_id: uuid.UUID, agent_id: str) -> str:
        return agent_id

    def start(self) -> StepResult:
        body = {"kind": "dinner", "scope": "solo"}
        return self.step(self.inbound("start_dinner", body, from_agent=STARTER_AGENT))

    def to_quoting(self) -> StepResult:
        self.start()
        return self.constraints(STARTER_AGENT, ["peanuts"])


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tokens, "_secret", lambda: SECRET)


def _quote_requests(result: StepResult) -> list[Any]:
    return [m for m in result.messages if m.type == "quote_request"]


def test_solo_asks_only_requester() -> None:
    w = SoloWorld()
    result = w.start()
    [ask] = [m for m in result.messages if m.type == "constraints_request"]
    assert ask.to_agent == STARTER_AGENT
    assert ask.body["user_id"] == STARTER
    saved = w.saved()
    assert (saved["scope"], saved["kind"]) == ("solo", "dinner")
    assert saved["members"] == {STARTER_AGENT: STARTER}
    # Another member's `constraints` is not waited for, and is ignored.
    assert w.constraints(OTHER, ["milk"]).messages == []
    assert OTHER in MEMBERS and OTHER not in w.saved()["constraints"]


def test_solo_filters_household_allergens() -> None:
    w = SoloWorld()
    result = w.to_quoting()
    [call] = w.household.of("get_household_allergens")
    assert call["args"] == {}
    claims = tokens.validate(call["token"])
    assert (claims.sub, claims.scopes) == (f"user:{STARTER}", ("constraints:read",))
    requests = _quote_requests(result)
    assert sorted(r.to_agent for r in requests) == RESTAURANTS
    for r in requests:
        assert r.body["constraints"]["allergens"] == ["peanuts", "sesame"]
    assert w.saved()["household_allergens"] == ["peanuts", "sesame"]


def test_household_order_does_not_read_household_allergens() -> None:
    w = World()
    w.to_quoting()
    assert w.household.of("get_household_allergens") == []


def test_solo_window() -> None:
    w = SoloWorld()
    [request, *_] = _quote_requests(w.to_quoting())
    window = request.body["window"]
    start = datetime.fromisoformat(window["start"])
    end = datetime.fromisoformat(window["end"])
    assert (start - NOW, end - NOW) == hg.SOLO_WINDOW
    assert (start - NOW).total_seconds() == 30 * 60
    assert (end - NOW).total_seconds() == 90 * 60


def test_solo_never_counters() -> None:
    # Over budget with an equal-coverage runner-up: a household order would counter.
    w = SoloWorld(remaining_minor=2000)
    w.to_quoting()
    w.offer(RESTAURANTS[0], 3000)
    w.offer(RESTAURANTS[1], 2500)
    assert w.round_closed().messages == []
    choices = [d["choice"] for d in w.traces.of("decision") if d["node"] == "negotiate"]
    assert choices == ["choose"]
    assert w.saved().get("counter") is None
    # The same weekly budget applies: the over-budget pick goes to the approver.
    [asked] = w.household.of("request_approval")
    assert asked["args"]["amount"]["amount_minor"] == 2500


def test_solo_no_take_turns_message() -> None:
    w = SoloWorld()
    w.household.rules = {**w.household.rules, "take_turns": True}
    w.to_quoting()
    w.offer(RESTAURANTS[1], 2100)
    [accept] = w.round_closed().messages
    assert (accept.type, accept.to_agent) == ("accept", RESTAURANTS[1])
    w.order_update("order_confirm", "placed", RESTAURANTS[1])
    w.order_update("delivery_booked", "courier_booked", "delivery:mock-us")
    done = w.order_update("order_status", "delivered", "system:api")
    assert done.finish == RunFinish("completed")
    [summary] = done.messages
    assert summary.body["template"] == hg.SUMMARY_TEMPLATE
    assert summary.body["buttons"] == []
    assert w.household.of("advance_turn") == []
