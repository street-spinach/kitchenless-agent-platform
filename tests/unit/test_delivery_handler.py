"""The delivery handler: courier quotes and bookings (architecture item 30, agent-protocol
P24; issue #57).

The handler runs with stub choices, an in-memory store and a fake `/mcp/delivery` server.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.agents.common.model import TEMPLATES, ModelResult
from services.agents.delivery import stubs as delivery_stubs
from services.agents.delivery.handler import DeliveryHandler, OwnQuote, allowed_windows
from services.common import ids
from services.common.consumer import StepFailed, StepResult
from services.common.directory import AgentCard
from services.common.mcp_client import ToolRefused
from services.common.protocol.schemas import Window
from tests.unit import model_fakes as mf
from tests.unit import seller_fakes as sf

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
TTL = 120
COURIER = "delivery:mock-us"
STORE = sf.GROCERY
HOUSEHOLD = sf.HOUSEHOLD
START = datetime(2026, 11, 2, 14, 0, tzinfo=UTC)
REQUESTED = Window.model_validate({"start": START, "end": START + timedelta(hours=2)}).model_dump(
    mode="json"
)
ADDRESS = uuid.uuid4()
CARD = AgentCard(
    agent_id=COURIER,
    agent_class="delivery",
    owner_ref="org:courier",
    region="us",
    zone_ids=(sf.ZONE,),
    currency="USD",
    host_process="agents",
    vendor="mock",
)


def _slot(hours: int) -> dict[str, str]:
    start = START + timedelta(hours=hours)
    return Window.model_validate({"start": start, "end": start + timedelta(hours=1)}).model_dump(
        mode="json"
    )


class FakeCourier:
    """`call_tool` for `/mcp/delivery`: a fixed fee and the slots in `windows`."""

    def __init__(self, windows: list[dict[str, str]] | None = None) -> None:
        self.windows = [_slot(0), _slot(1)] if windows is None else windows
        self.calls: list[dict[str, Any]] = []
        self.refuse: str | None = None

    def of(self, tool: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["tool"] == tool]

    async def __call__(self, server: str, tool: str, args: dict, *, ctx: Any, token: str) -> dict:
        assert server == "delivery"
        self.calls.append({"tool": tool, "args": args, "ctx": ctx, "token": token})
        if tool == "quote_delivery":
            return {"price_minor": 599, "currency": "USD", "windows": list(self.windows)}
        if tool == "book_delivery":
            if self.refuse:
                raise ToolRefused(self.refuse)
            return {
                "order_id": args["order_id"],
                "state": "courier_booked",
                "external_delivery_id": "ext-1",
                "window": args["window"],
            }
        raise AssertionError(f"unexpected tool {tool}")


class FakeStore:
    def __init__(self) -> None:
        self.quotes: dict[uuid.UUID, OwnQuote] = {}

    async def quote(self, quote_id: uuid.UUID) -> OwnQuote | None:
        return self.quotes.get(quote_id)

    async def household(self, order_id: uuid.UUID) -> str | None:
        return HOUSEHOLD

    def apply(self, result: StepResult) -> None:
        for qi in result.quote_inserts:
            self.quotes[qi.quote_id] = OwnQuote(
                quote_id=qi.quote_id,
                order_id=qi.order_id,
                seller_agent_id=qi.seller_agent_id,
                buyer_agent_id=qi.buyer_agent_id,
                window=qi.items.get("window"),
            )


class World:
    def __init__(
        self, *, stubs: dict[str, Any] | None = None, overrides: dict[str, Any] | None = None
    ) -> None:
        self.run_id = uuid.uuid4()
        self.order_id = uuid.uuid4()
        self.traces = mf.FakeTraces()
        self.courier = FakeCourier()
        self.store = FakeStore()
        self.handler = DeliveryHandler(
            model=mf.client(
                traces=self.traces,
                stubs=stubs if stubs is not None else dict(delivery_stubs.STUBS),
                run_state=mf.FakeRunState(overrides or {}),
            ),
            traces=self.traces,
            store=self.store,
            call_tool=self.courier,
            issue_token=lambda sub, act, scopes: f"token:{sub}:{act}",
            clock=lambda: NOW,
            quote_ttl_seconds=TTL,
        )

    def step(self, type: str, body: dict[str, Any], **kw: Any) -> StepResult:
        kw.setdefault("conversation_id", uuid.uuid4())
        kw.setdefault("from_agent", STORE)
        msg = sf.inbound(self.run_id, type, body, to_agent=COURIER, **kw)
        result = asyncio.run(self.handler(msg, CARD))
        self.store.apply(result)
        return result

    def quote_request(self, **kw: Any) -> StepResult:
        body = {
            "order_id": str(self.order_id),
            "zone_id": sf.ZONE,
            "window": REQUESTED,
            "pickup_agent_id": STORE,
        }
        return self.step("delivery_quote_request", body, **kw)

    def delivery_request(self, quote_id: uuid.UUID, window: dict[str, str], **kw: Any):
        body = {
            "order_id": str(self.order_id),
            "quote_id": str(quote_id),
            "window": window,
            "zone_id": sf.ZONE,
            "address_ref": str(ADDRESS),
        }
        return self.step("delivery_request", body, **kw)


# Window from the allowed set ------------------------------------------------------------


def test_window_defaults_to_the_earliest_allowed_slot() -> None:
    w = World()
    w.courier.windows = [_slot(1), _slot(0)]
    mid = uuid.uuid4()
    conv = uuid.uuid4()

    result = w.quote_request(message_id=mid, conversation_id=conv)

    call = w.courier.of("quote_delivery")[0]
    assert call["args"] == {"order_id": str(w.order_id), "zone_id": sf.ZONE, "window": REQUESTED}
    assert call["ctx"].agent_id == COURIER and call["token"] == "token:org:courier:delivery:mock-us"
    [msg] = result.messages
    assert (msg.type, msg.to_agent, msg.conversation_id) == ("delivery_quote", STORE, conv)
    assert msg.body["window"] == _slot(0)
    assert msg.body["price_minor"] == 599 and msg.body["currency"] == "USD"
    assert msg.body["quote_id"] == str(ids.quote_id(mid))
    assert msg.body["rationale"] == delivery_stubs.RATIONALES["choose_window"]
    [row] = result.quote_inserts
    assert (row.seller_agent_id, row.buyer_agent_id) == (COURIER, STORE)
    assert row.quote_id == ids.quote_id(mid) and row.items == {"window": _slot(0)}
    assert row.total_minor == 599 and row.conversation_id == conv


def test_model_pick_inside_the_allowed_set_is_used() -> None:
    def later(inputs: Any, default: Any, *, out_of_bounds: bool = False) -> ModelResult:
        return mf.stub_answer(_slot(1), "The later slot suits this route.")

    w = World(stubs={"choose_window": later})
    msg = w.quote_request().messages[0]
    assert msg.body["window"] == _slot(1)


def test_out_of_bounds_window_falls_back_to_the_rule_default() -> None:
    w = World(overrides={"STUB_OUT_OF_BOUNDS": "choose_window"})
    msg = w.quote_request().messages[0]
    assert msg.body["window"] == _slot(0)
    assert msg.body["rationale"] == TEMPLATES["choose_window"]
    [row] = w.traces.of("decision")
    assert (row["node"], row["source"], row["reason"]) == (
        "choose_window",
        "rule_default",
        "out_of_bounds",
    )
    assert row["model_pick"]["end"] == (START + timedelta(hours=4)).isoformat()


def test_choose_window_guidance_reaches_choose() -> None:
    w = World()
    recorder = mf.RecordingChooser(w.handler.model)
    w.handler.model = recorder
    w.courier.windows = [_slot(1), _slot(0)]

    msg = w.quote_request().messages[0]

    assert recorder.calls == [("choose_window", "Prefer the earliest window.")]
    # The allowed set and the rule default (the earliest window) are unchanged.
    [row] = w.traces.of("decision")
    assert row["allowed"] == {"one_of": [_slot(0), _slot(1)]}
    assert msg.body["window"] == _slot(0)


def test_adapter_windows_outside_the_request_are_not_allowed() -> None:
    requested = Window.model_validate(REQUESTED)
    late = _slot(2)  # 16:00-17:00, after the requested 14:00-16:00
    assert allowed_windows([late, _slot(1), {"start": "x"}], requested) == [_slot(1)]


def test_no_allowed_window_sends_no_quote() -> None:
    w = World()
    w.courier.windows = [_slot(2)]
    result = w.quote_request()
    assert result.messages == [] and result.quote_inserts == []
    [row] = w.traces.of("decision")
    assert (row["node"], row["choice"]) == ("choose_window", "no_window")


# expires_at from COURIER_QUOTE_TTL_SECONDS -----------------------------------------------


def test_expires_at_comes_from_courier_quote_ttl() -> None:
    w = World()
    result = w.quote_request()
    expires = NOW + timedelta(seconds=TTL)
    assert result.quote_inserts[0].expires_at == expires
    assert result.messages[0].body["expires_at"] == expires.isoformat()


def test_courier_quote_ttl_defaults_to_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.agents.delivery import handler as module

    class S:
        courier_quote_ttl_seconds = 45

    monkeypatch.setattr(module, "get_settings", lambda: S())
    handler = DeliveryHandler(model=None, traces=None, store=None)  # type: ignore[arg-type]
    assert handler.quote_ttl == timedelta(seconds=45)


# Booking key ------------------------------------------------------------------------------


def _quoted(w: World) -> tuple[uuid.UUID, dict[str, str]]:
    msg = w.quote_request().messages[0]
    return uuid.UUID(msg.body["quote_id"]), msg.body["window"]


def test_booking_key_is_order_book_delivery_courier() -> None:
    w = World()
    quote_id, window = _quoted(w)
    mid = uuid.uuid4()
    conv = uuid.uuid4()

    first = w.delivery_request(quote_id, window, message_id=mid, conversation_id=conv)
    again = w.delivery_request(quote_id, window, message_id=mid, conversation_id=conv)

    calls = w.courier.of("book_delivery")
    assert len(calls) == 2
    for call in calls:
        # The server keys the booking from the caller: idempotency_key(order, action, courier).
        key = ids.idempotency_key(
            uuid.UUID(call["args"]["order_id"]), "book_delivery", call["ctx"].agent_id
        )
        assert key == ids.idempotency_key(w.order_id, "book_delivery", COURIER)
        assert call["args"]["window"] == window
    assert calls[0]["args"] == calls[1]["args"]
    assert calls[0]["args"]["address_ref"] == str(ADDRESS)
    # A replayed step sends the same message ids.
    assert [m.message_id for m in first.messages] == [m.message_id for m in again.messages]
    seller, household = first.messages
    assert (seller.type, seller.to_agent, seller.conversation_id) == (
        "delivery_booked",
        STORE,
        conv,
    )
    assert (household.type, household.to_agent) == ("delivery_booked", HOUSEHOLD)
    assert seller.body["state"] == "courier_booked"
    assert seller.body["delivery_agent_id"] == COURIER and seller.body["window"] == window


def test_booking_a_window_the_courier_did_not_quote_fails_closed() -> None:
    w = World()
    quote_id, _ = _quoted(w)
    with pytest.raises(StepFailed) as e:
        w.delivery_request(quote_id, _slot(1))
    assert e.value.reason == "window_mismatch"
    assert w.courier.of("book_delivery") == []


def test_booking_another_couriers_quote_fails_closed() -> None:
    w = World()
    quote_id, window = _quoted(w)
    with pytest.raises(StepFailed) as e:
        w.delivery_request(quote_id, window, from_agent="grocery:other-us")
    assert e.value.reason == "unknown_quote"
    assert w.courier.of("book_delivery") == []


def test_refused_booking_sends_no_delivery_booked() -> None:
    w = World()
    quote_id, window = _quoted(w)
    w.courier.refuse = "out_of_zone"
    with pytest.raises(StepFailed) as e:
        w.delivery_request(quote_id, window)
    assert (e.value.where, e.value.reason) == ("book_delivery", "out_of_zone")
