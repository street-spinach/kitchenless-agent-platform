"""Grocery and restaurant handlers: bounded choices and statelessness (architecture item 30,
§4 "Model proposes, code decides"; issue #56).

The handler runs with stub choices, an in-memory store and a fake seller MCP server.
"""

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.agents.common.seller import (
    COURIER_ROUND_SECONDS,
    CourierQuote,
    OrderInfo,
    SellerHandler,
    StoredQuote,
)
from services.common import orders
from services.common.consumer import StepResult, TimerCreate
from services.identity.errors import GrantRevoked
from tests.unit import model_fakes as mf
from tests.unit import seller_fakes as sf

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
COURIERS = ["delivery:fast-us", "delivery:mock-us"]
MILK = "MOCK-MILK-WHOLE"


class FakeStore:
    """`SellerStore` over dicts. `apply` keeps a step's quote changes, as a commit would."""

    def __init__(self) -> None:
        self.quotes: dict[uuid.UUID, StoredQuote] = {}
        self.courier_rows: list[CourierQuote] = []
        self.order_state = "authorized"

    async def quote(self, quote_id: uuid.UUID) -> StoredQuote | None:
        return self.quotes.get(quote_id)

    async def accepted_quote(self, order_id: uuid.UUID, seller: str) -> StoredQuote | None:
        return next(
            (
                q
                for q in self.quotes.values()
                if q.order_id == order_id and q.seller_agent_id == seller and q.status == "accepted"
            ),
            None,
        )

    async def courier_quotes(self, order_id: uuid.UUID, store: str) -> list[CourierQuote]:
        return list(self.courier_rows)

    async def order(self, order_id: uuid.UUID) -> OrderInfo:
        return OrderInfo(state=self.order_state, address_ref=uuid.uuid4(), zone_id=sf.ZONE)

    async def rules(self, agent_id: str) -> dict[str, Any]:
        assert agent_id == sf.GROCERY
        return dict(sf.RULES)

    async def couriers(self, region: str, zone_id: str, currency: str) -> list[str]:
        assert (region, zone_id, currency) == ("us", sf.ZONE, "USD")
        return list(COURIERS)

    def apply(self, result: StepResult) -> None:
        for qi in result.quote_inserts:
            self.quotes[qi.quote_id] = StoredQuote(
                quote_id=qi.quote_id,
                run_id=qi.run_id,
                order_id=qi.order_id,
                conversation_id=qi.conversation_id,
                seller_agent_id=qi.seller_agent_id,
                buyer_agent_id=qi.buyer_agent_id,
                items=qi.items,
                total_minor=qi.total_minor,
                currency=qi.currency,
                round=0,
                expires_at=qi.expires_at,
                status="open",
                constraints=dict(qi.constraints),
            )
        for u in result.quote_updates:
            q = self.quotes[u.quote_id]
            assert q.status in u.expected_status and q.round == u.expected_round
            self.quotes[u.quote_id] = replace(q, **dict(u.set))


class World:
    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.run_id = uuid.uuid4()
        self.order_id = uuid.uuid4()
        self.traces = mf.FakeTraces()
        self.stubs = sf.RecordingStubs(answers)
        self.seller = sf.FakeSeller()
        self.store = FakeStore()
        self.handler = SellerHandler(
            "grocery",
            model=sf.chooser(self.stubs, self.traces),
            traces=self.traces,
            store=self.store,
            call_tool=self.seller,
            clock=lambda: NOW,
            offer_ttl_seconds=600,
        )
        self.card = sf.CARD

    def step(self, type: str, body: dict[str, Any], **kw: Any) -> StepResult:
        kw.setdefault("conversation_id", uuid.uuid4())
        result = asyncio.run(self.handler(sf.inbound(self.run_id, type, body, **kw), self.card))
        self.store.apply(result)
        return result

    def quote(self, *, qty: int = 10, round: int = 0, status: str = "open") -> StoredQuote:
        """A stored quote for `qty` whole milk at list price (floor 408 per unit)."""
        price = sf.CATALOG[MILK][2]
        line = {
            "line": 0,
            "requested_item": "milk",
            "offered_sku": MILK,
            "offered_name": sf.CATALOG[MILK][0],
            "qty": qty,
            "unit_minor": price,
            "substituted_for": None,
        }
        q = StoredQuote(
            quote_id=uuid.uuid4(),
            run_id=self.run_id,
            order_id=self.order_id,
            conversation_id=uuid.uuid4(),
            seller_agent_id=sf.GROCERY,
            buyer_agent_id=sf.HOUSEHOLD,
            items={
                "lines": [line],
                "window": sf.request_body(self.order_id, [])["window"],
                "zone_id": sf.ZONE,
            },
            total_minor=qty * price,
            currency="USD",
            round=round,
            expires_at=NOW + timedelta(minutes=10),
            status=status,
            constraints={"allergens": [], "diets": []},
        )
        self.store.quotes[q.quote_id] = q
        return q

    def counter(self, q: StoredQuote, total: int) -> StepResult:
        body = {
            "order_id": str(q.order_id),
            "quote_id": str(q.quote_id),
            "round": q.round,
            "lines": q.lines,
            "total_minor": total,
            "currency": "USD",
        }
        return self.step("counter_offer", body, conversation_id=q.conversation_id)


def _floor(q: StoredQuote) -> int:
    return sum(line["qty"] * sf.FLOORS[line["offered_sku"]] for line in q.lines)


def _decisions(w: World, node: str) -> list[dict[str, Any]]:
    return [r for r in w.traces.of("decision") if r["node"] == node]


def test_counter_respects_margin_floor() -> None:
    w = World()
    q = w.quote()
    floor = _floor(q)
    buyer = q.total_minor * 90 // 100
    assert buyer < floor
    result = w.counter(q, buyer)
    [offer] = result.messages
    assert offer.type == "offer"
    assert offer.body["total_minor"] == floor  # the rule default: counter at the floor total
    assert offer.body["final"] is True and offer.body["round"] == 1
    assert w.store.quotes[q.quote_id].status == "countered"
    assert w.store.quotes[q.quote_id].total_minor >= floor
    [decision] = _decisions(w, "respond_to_counter")
    assert "agree" not in decision["allowed"]["actions"]
    assert decision["allowed"]["actions"]["counter"] == {"range": [floor, q.total_minor]}


def test_handler_keeps_no_state_between_households() -> None:
    w = World()
    before = dict(vars(w.handler))
    first = sf.request_body(
        w.order_id, [("whole milk", 1)], constraints={"allergens": ["tree_nuts"], "diets": []}
    )
    w.step("quote_request", first)
    other_run = uuid.uuid4()
    w.run_id = other_run
    second = sf.request_body(uuid.uuid4(), [("eggs", 1)])
    w.step("quote_request", second, from_agent="household:us-2")
    assert vars(w.handler) == before
    (_, inputs_1), (_, inputs_2) = w.stubs.seen
    text = repr(inputs_2)
    for leaked in ("tree_nuts", "whole milk", str(w.order_id), sf.HOUSEHOLD + "'"):
        assert leaked not in text
    assert inputs_2["message"]["body"]["items"] == [{"item": "eggs", "qty": 1, "unit": "each"}]
    assert "tree_nuts" in repr(inputs_1)


@pytest.mark.parametrize(
    "answer",
    [{"action": "agree"}, {"action": "counter", "arg": 100}, {"action": "counter", "arg": 0}],
)
def test_counter_never_below_floor_whatever_the_model_says(answer: dict[str, Any]) -> None:
    w = World(answers={"respond_to_counter": answer})
    q = w.quote()
    floor = _floor(q)
    result = w.counter(q, floor - 1)
    [offer] = result.messages
    assert offer.body["total_minor"] >= floor
    assert w.store.quotes[q.quote_id].total_minor >= floor
    [decision] = _decisions(w, "respond_to_counter")
    assert decision["source"] == "rule_default" and decision["reason"] == "out_of_bounds"


def test_substitutes_offered_only_from_allowed_set() -> None:
    w = World(answers={"rank_substitutes": {"0": ["MOCK-MILK-ALMOND"]}})
    w.seller.stock[MILK] = 0
    body = sf.request_body(
        w.order_id, [("whole milk", 2)], constraints={"allergens": ["tree_nuts"], "diets": []}
    )
    result = w.step("quote_request", body)
    [offer] = result.messages
    [line] = offer.body["lines"]
    assert line["offered_sku"] == "MOCK-MILK-2PCT"
    assert line["substituted_for"] == "whole milk"
    assert offer.body["total_minor"] == 2 * sf.CATALOG["MOCK-MILK-2PCT"][2]
    [decision] = _decisions(w, "rank_substitutes")
    assert decision["allowed"] == {"per_item": {"0": {"subset": ["MOCK-MILK-2PCT"], "max_len": 3}}}
    assert decision["source"] == "rule_default" and decision["reason"] == "out_of_bounds"
    # Every search the handler made carried the request's allergen filter.
    for call in w.seller.of("search_items"):
        assert call["args"]["filters"]["allergen_codes"] == ["tree_nuts"]


def test_prompt_inputs_hold_only_message_own_rules_and_allowed_set() -> None:
    w = World()
    body = sf.request_body(w.order_id, [("eggs", 1)])
    w.step("quote_request", body)
    q = w.quote()
    w.counter(q, q.total_minor - 10)
    assert [name for name, _ in w.stubs.seen] == ["rank_substitutes", "respond_to_counter"]
    for name, inputs in w.stubs.seen:
        assert set(inputs) == {"message", "rules", "allowed"}, name
        assert inputs["rules"] == sf.RULES
        assert set(inputs["message"]) == {"type", "body"}
        decision = _decisions(w, name)[0]
        assert inputs["allowed"] == decision["allowed"]
    assert w.stubs.seen[0][1]["message"]["body"]["items"] == body["items"]


def test_counter_allowed_only_while_round_is_zero() -> None:
    w = World(answers={"respond_to_counter": {"action": "counter", "arg": 4200}})
    q = w.quote(round=1, status="countered")
    buyer = _floor(q) + 10
    result = w.counter(q, buyer)
    [decision] = _decisions(w, "respond_to_counter")
    assert set(decision["allowed"]["actions"]) == {"reject", "agree"}
    assert decision["reason"] == "out_of_bounds"
    [offer] = result.messages
    assert offer.body["total_minor"] == buyer  # the default: agree
    assert offer.body["round"] == 2 and offer.body["final"] is True
    assert w.store.quotes[q.quote_id].status == "countered"


def test_accept_not_allowed_below_floor_total() -> None:
    w = World(answers={"respond_to_counter": {"action": "agree"}})
    q = w.quote(round=1, status="countered")
    result = w.counter(q, _floor(q) - 1)
    [decision] = _decisions(w, "respond_to_counter")
    assert set(decision["allowed"]["actions"]) == {"reject"}
    [reject] = result.messages
    assert reject.type == "reject" and reject.body["reason"] == "price"
    stored = w.store.quotes[q.quote_id]
    assert (stored.status, stored.closed_by) == ("rejected", "seller")


def test_one_choose_call_per_step() -> None:
    w = World()
    w.seller.stock[MILK] = 0
    body = sf.request_body(w.order_id, [("whole milk", 1), ("eggs", 2), ("bread", 1)])
    w.step("quote_request", body)
    assert len(w.stubs.seen) == 1
    q = w.quote()
    w.counter(q, q.total_minor - 5)
    assert len(w.stubs.seen) == 2
    points = [r for r in w.traces.of("decision") if r["node"] in sf.seller_stubs.STUBS]
    assert [r["node"] for r in points] == ["rank_substitutes", "respond_to_counter"]


# Choice-point guidance (agents §12 prompts 7 and 8, AG12, AG-A11) ------------------------

RANK_GROCERY = (
    "For each missing item, rank the substitute candidates by closeness in kind and size, "
    "then by price. The shopper's constraints are in the inputs."
)
COUNTER_GUIDANCE = (
    "A buyer has countered your offer. Prefer agreeing or countering within your business "
    "rules over rejecting, when the rules allow it."
)


def _quote_and_counter(w: World) -> None:
    w.seller.stock[MILK] = 0
    w.step("quote_request", sf.request_body(w.order_id, [("milk", 1)]))
    q = w.quote()
    w.counter(q, q.total_minor * 90 // 100)


def test_grocery_guidance_reaches_choose() -> None:
    w = World()
    recorder = mf.RecordingChooser(w.handler.model)
    w.handler.model = recorder
    _quote_and_counter(w)

    assert recorder.calls == [
        ("rank_substitutes", RANK_GROCERY),
        ("respond_to_counter", COUNTER_GUIDANCE),
    ]
    # Guidance changes neither the allowed sets nor the rule defaults the handler computes.
    baseline = World()
    _quote_and_counter(baseline)
    for node in ("rank_substitutes", "respond_to_counter"):
        [got] = _decisions(w, node)
        [want] = _decisions(baseline, node)
        assert (got["allowed"], got["choice"], got["source"]) == (
            want["allowed"],
            want["choice"],
            want["source"],
        )
    [counter] = _decisions(w, "respond_to_counter")
    assert counter["choice"] == {"action": "counter", "arg": _floor(w.quote())}


def test_restaurant_guidance_reaches_choose_with_dish() -> None:
    w = World()
    recorder = mf.RecordingChooser(w.handler.model)

    async def call_tool(server: str, tool: str, args: dict, *, ctx: Any) -> dict:
        assert server == "restaurant"
        return await w.seller("grocery", tool, args, ctx=ctx)

    w.handler = SellerHandler(
        "restaurant",
        model=recorder,
        traces=w.traces,
        store=w.store,
        call_tool=call_tool,
        clock=lambda: NOW,
        offer_ttl_seconds=600,
    )
    w.card = replace(sf.CARD, agent_class="restaurant")
    _quote_and_counter(w)

    assert recorder.calls == [
        ("rank_substitutes", RANK_GROCERY.replace("missing item", "missing dish")),
        ("respond_to_counter", COUNTER_GUIDANCE),
    ]


def test_seller_courier_round_uses_timer_not_wait() -> None:
    w = World()
    q = w.quote()
    accept = {
        "order_id": str(q.order_id),
        "quote_id": str(q.quote_id),
        "items_hash": orders.items_hash(
            [{"sku": MILK, "qty": 10, "unit_minor": sf.CATALOG[MILK][2]}]
        ),
        "total_minor": q.total_minor,
    }
    result = w.step("accept", accept, conversation_id=q.conversation_id)
    assert [m.type for m in result.messages] == ["delivery_quote_request"] * 2
    assert [m.to_agent for m in result.messages] == COURIERS
    assert len({m.conversation_id for m in result.messages}) == 2
    assert result.timers == [
        TimerCreate(
            kind="quote_round",
            due_at=timedelta(seconds=COURIER_ROUND_SECONDS),
            ref={"order_id": str(q.order_id), "round": "delivery", "to": sf.GROCERY},
        )
    ]
    assert w.seller.of("place_order") == []  # the step ends; nothing waits for couriers
    assert w.store.quotes[q.quote_id].status == "accepted"

    # The round closes on the timer: the cheapest live courier in the window is placed.
    window = q.items["window"]
    for courier, price in (("delivery:fast-us", 799), ("delivery:mock-us", 599)):
        w.store.courier_rows.append(
            CourierQuote(
                quote_id=uuid.uuid4(),
                conversation_id=uuid.uuid4(),
                seller_agent_id=courier,
                total_minor=price,
                currency="USD",
                expires_at=NOW + timedelta(minutes=2),
                status="open",
                window=window,
            )
        )
    closed = w.step(
        "quote_round_closed",
        {"order_id": str(q.order_id), "round": "delivery"},
        from_agent="system:sweeper",
        conversation_id=None,
    )
    [place] = w.seller.of("place_order")
    assert place["args"]["delivery_agent_id"] == "delivery:mock-us"
    assert place["args"]["items_hash"] == accept["items_hash"]
    assert [(m.type, m.to_agent) for m in closed.messages] == [
        ("order_confirm", sf.HOUSEHOLD),
        ("delivery_request", "delivery:mock-us"),
    ]


def test_place_order_grant_revoked_rejects_and_places_nothing() -> None:
    """Identity-authz §11: the household is told, and its next guarded call ends the run."""
    w = World()
    w.seller.place_error = GrantRevoked()
    q = w.quote()
    accept = {
        "order_id": str(q.order_id),
        "quote_id": str(q.quote_id),
        "items_hash": orders.items_hash(
            [{"sku": MILK, "qty": 10, "unit_minor": sf.CATALOG[MILK][2]}]
        ),
        "total_minor": q.total_minor,
    }
    w.step("accept", accept, conversation_id=q.conversation_id)
    w.store.courier_rows.append(
        CourierQuote(
            quote_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            seller_agent_id="delivery:mock-us",
            total_minor=599,
            currency="USD",
            expires_at=NOW + timedelta(minutes=2),
            status="open",
            window=q.items["window"],
        )
    )
    closed = w.step(
        "quote_round_closed",
        {"order_id": str(q.order_id), "round": "delivery"},
        from_agent="system:sweeper",
        conversation_id=None,
    )
    assert [(m.type, m.to_agent) for m in closed.messages] == [("reject", sf.HOUSEHOLD)]
