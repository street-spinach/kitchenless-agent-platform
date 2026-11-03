"""Take-turns in the household graph (agents §10, AG7; AG-CUJ 8).

`finish` of a household dinner with an unavoidable dislike sends the approver one
take-turns message with one button that sets the opposite of the current setting, and calls
`advance_turn` when take-turns was on. No order waits for the answer.
"""

import uuid
from typing import Any

import pytest

from services.agents.household import graph as hg
from services.agents.household import stubs
from services.common import ids
from services.common.consumer import RunFinish, StepResult
from tests.unit.test_household_graph import (
    APPROVER,
    HOUSEHOLD,
    MEMBERS,
    STARTER,
    World,
)

A = "consumer:us-1-a"  # the starter, STARTER
B = "consumer:us-1-b"
RESTAURANT = "restaurant:mock-us"
LIST = [
    {"item": "Mushroom risotto", "qty": 1, "unit": "each"},
    {"item": "Green salad", "qty": 1, "unit": "each"},
]


class DinnerWorld(World):
    """A household dinner from one restaurant."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(sellers=[RESTAURANT], **kwargs)

    async def find_sellers(self, cls: str, region: str, zone: str, currency: str) -> list[str]:
        assert cls == "restaurant"
        return list(self.sellers)

    async def label_seller(self, run_id: uuid.UUID, agent_id: str) -> str:
        return agent_id

    def start(self) -> StepResult:
        return self.step(self.inbound("start_dinner", {"kind": "dinner"}, from_agent=A))

    def answer(self, agent: str, dislikes: list[str]) -> StepResult:
        body = {"order_id": self.order_id, "allergens": [], "diets": [], "dislikes": dislikes}
        return self.step(self.inbound("constraints", body, from_agent=agent))

    def delivered_msg(self) -> Any:
        body = {"order_id": self.order_id, "state": "delivered"}
        return self.inbound("order_status", body, from_agent="system:api")

    def deliver(self, a_dislikes: list[str], b_dislikes: list[str]) -> StepResult:
        """Run the dinner to `delivered` and return `finish`'s step."""
        self.start()
        self.answer(A, a_dislikes)
        self.answer(B, b_dislikes)
        self.offer(RESTAURANT, 2100)
        [accept] = self.round_closed().messages
        assert accept.type == "accept"
        self.order_update("order_confirm", "placed", RESTAURANT)
        self.order_update("delivery_booked", "courier_booked", "delivery:mock-us")
        return self.step(self.delivered_msg())


@pytest.fixture(autouse=True)
def _setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stubs, "LISTS", {"grocery": LIST, "dinner": LIST})


def _offers(result: StepResult) -> list[Any]:
    return [
        m
        for m in result.messages
        if m.type == "whatsapp_out"
        and m.body["template"] in (hg.TAKE_TURNS_OFFER_TEMPLATE, hg.TAKE_TURNS_OFFER_OFF_TEMPLATE)
    ]


def _finish_choices(w: World) -> list[str]:
    return [d["choice"] for d in w.traces.of("decision") if d["node"] == "finish"]


def test_offer_sent_to_approver_once() -> None:
    w = DinnerWorld()
    # Nobody's turn removes the mushrooms: take-turns is off and only Sam dislikes them.
    done = w.deliver(["mushrooms"], [])
    assert done.finish == RunFinish("completed")
    summary, offer = done.messages
    assert summary.body["template"] == hg.SUMMARY_TEMPLATE
    assert summary.body["user_id"] == STARTER
    assert (offer.type, offer.to_agent) == ("whatsapp_out", hg.WHATSAPP)
    assert offer.body["user_id"] == APPROVER
    assert offer.body["template"] == hg.TAKE_TURNS_OFFER_TEMPLATE
    assert offer.body["params"] == {"values": "mushrooms"}
    assert offer.body["buttons"] == [
        {"payload": ids.take_turns_button(uuid.UUID(w.order_id), True), "title": "Take turns"}
    ]
    assert offer.body["buttons"][0]["payload"] == f"tt:{w.order_id}:on"
    assert _finish_choices(w) == ["take_turns_offered"]
    # A redelivered `delivered` returns the filed messages and sends no second offer.
    replay = w.step(w.delivered_msg())
    assert [m for m in _offers(replay) if m.message_id != offer.message_id] == []
    assert _finish_choices(w) == ["take_turns_offered"]
    # Take-turns was off: the turn is not moved.
    assert w.household.of("advance_turn") == []


def test_offer_names_no_member() -> None:
    w = DinnerWorld()
    [offer] = _offers(w.deliver(["mushrooms"], []))
    text = repr(offer.body["params"])
    for name in [A, B, *MEMBERS.values()]:
        assert name not in text
    for row in w.traces.of("decision"):
        if row["node"] == "finish":
            assert "mushrooms" not in repr(row) and STARTER not in repr(row)


def test_off_variant_when_on() -> None:
    w = DinnerWorld()
    # Sorted by user id the members are STARTER, APPROVER; index 1 picks the approver, whose
    # tomatoes are not on the list, so Sam's mushrooms stay.
    w.household.rules = {"take_turns": True, "turn_index": 1, "approver_user_id": APPROVER}
    [offer] = _offers(w.deliver(["mushrooms"], ["tomatoes"]))
    assert offer.body["template"] == hg.TAKE_TURNS_OFFER_OFF_TEMPLATE
    assert offer.body["params"] == {"values": "mushrooms"}
    assert offer.body["buttons"] == [
        {"payload": f"tt:{w.order_id}:off", "title": "Stop taking turns"}
    ]


def test_no_message_without_unavoidable_dislike() -> None:
    w = DinnerWorld()
    done = w.deliver([], [])
    assert done.finish == RunFinish("completed")
    assert _offers(done) == []
    [summary] = done.messages
    assert summary.body["template"] == hg.SUMMARY_TEMPLATE
    assert "take_turns_offered" not in _finish_choices(w)


def test_no_message_without_approver() -> None:
    w = DinnerWorld()
    w.household.rules = {"take_turns": False, "turn_index": 0, "approver_user_id": None}
    assert _offers(w.deliver(["mushrooms"], [])) == []


def test_advance_turn_called_once_when_on() -> None:
    w = DinnerWorld()
    w.household.rules = {"take_turns": True, "turn_index": 0, "approver_user_id": APPROVER}
    done = w.deliver([], [])
    assert done.finish == RunFinish("completed")
    [call] = w.household.of("advance_turn")
    assert call["args"] == {"order_id": w.order_id}
    # Agent code passes no token; the scopes are `tokens.TOOL_SCOPES` (identity-authz I6).
    assert "token" not in call and call["ctx"].agent_id == HOUSEHOLD
    assert "turn_advanced" in _finish_choices(w)
    # A redelivered `delivered` does not run `finish` again.
    w.step(w.delivered_msg())
    assert len(w.household.of("advance_turn")) == 1


def test_weekly_order_never_offers_or_advances() -> None:
    w = World()
    w.household.rules = {"take_turns": True, "turn_index": 0, "approver_user_id": APPROVER}
    w.start()
    w.constraints(A)
    body = {"order_id": w.order_id, "allergens": [], "diets": [], "dislikes": ["mushrooms"]}
    w.step(w.inbound("constraints", body, from_agent=B))
    w.offer("grocery:mock-us", 2400)
    w.offer("grocery:square-us", 2100)
    w.round_closed()
    w.order_update("order_confirm", "placed", "grocery:square-us")
    w.order_update("delivery_booked", "courier_booked", "delivery:mock-us")
    done = w.order_update("order_status", "delivered", "system:api")
    assert w.saved()["unavoidable_dislikes"] == ["mushrooms"]
    assert _offers(done) == []
    assert w.household.of("advance_turn") == []
