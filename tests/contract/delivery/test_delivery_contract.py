"""Delivery adapter contract (architecture item 69; item 46 amended 2026-11-02, #48).

Mock mode. Each interface test runs against every courier adapter; only the mock courier
exists until build step 7 adds DoorDash Drive. Tests marked "mock only" exercise the mock
courier's slots, batches, `courier_delayed` events and status posts. Status posts are
stubbed: the `/internal/delivery-status` route belongs to #58.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from services.common import ids
from services.common.directory import AgentCard
from services.common.protocol.schemas import Window
from services.mcp.delivery.adapters.mock_courier import MockCourierAdapter
from services.mcp.meta import AdapterFailure
from services.mocks import courier
from tests.contract.shop import ZONE, Shop

pytestmark = pytest.mark.db

ADAPTERS = [MockCourierAdapter]
COURIER = "delivery:mock-us"
OTHER_COURIER = "delivery:other-us"
OTHER_ZONE = "us-oh-45203"
RULE = {"fee_minor": 599, "slot_minutes": 60, "slot_capacity": 2}
STEP = 20
DAY = datetime(2030, 1, 7, tzinfo=UTC)


def _card(agent_id: str = COURIER) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        agent_class="delivery",
        owner_ref="org:mock-courier-us",
        region="us",
        zone_ids=(ZONE, OTHER_ZONE),
        currency="USD",
        host_process="agents",
        vendor="mock",
    )


@pytest.fixture
def courier_db(pg: str, monkeypatch: pytest.MonkeyPatch) -> Shop:
    """Two mock courier cards with the seeded `delivery` rule, in a fresh schema."""
    with psycopg.connect(pg) as conn:
        for agent_id in (COURIER, OTHER_COURIER):
            conn.execute(
                "INSERT INTO agent_cards (agent_id, class, owner_ref, region, zone_ids,"
                " currency, host_process, vendor) VALUES (%s, 'delivery', 'org:mock-courier-us',"
                " 'us', %s, 'USD', 'agents', 'mock')",
                (agent_id, [ZONE, OTHER_ZONE]),
            )
            conn.execute(
                "INSERT INTO business_rules (agent_id, kind, value) VALUES (%s, 'delivery', %s)",
                (agent_id, Jsonb(RULE)),
            )
    settings = SimpleNamespace(mock_courier_step_seconds=STEP, api_url="http://api.invalid")
    monkeypatch.setattr(courier, "get_settings", lambda: settings)
    return Shop(pg, monkeypatch)


def _at(hour: float) -> datetime:
    return DAY + timedelta(hours=hour)


def _win(start: float, end: float) -> Window:
    return Window(start=_at(start), end=_at(end))


def _wire(start: float, end: float) -> dict[str, str]:
    return {"start": _at(start).isoformat(), "end": _at(end).isoformat()}


def _key() -> str:
    return ids.vendor_key(ids.idempotency_key(uuid4(), "book_delivery", COURIER))


def _book(shop: Shop, adapter: Any, key: str, window: Window, zone: str = ZONE) -> dict[str, Any]:
    return shop.run(
        lambda: adapter.book_delivery(
            idempotency_key=key,
            order_id="order-1",
            zone_id=zone,
            window=window,
            address=object(),
        )
    )


def _row(shop: Shop, key: str) -> dict[str, Any]:
    [row] = shop.query(
        "SELECT external_delivery_id, env, agent_id, idempotency_key, zone_id, batch_id,"
        " state, next_state_at FROM mock_deliveries WHERE external_delivery_id = %s",
        key,
    )
    return row


class Poster:
    """A stub for `api` `/internal/delivery-status` that answers with `codes` in turn."""

    def __init__(self, *codes: int | Exception) -> None:
        self.codes = list(codes)
        self.bodies: list[dict[str, Any]] = []

    async def __call__(self, body: dict[str, Any]) -> int:
        self.bodies.append(body)
        code = self.codes.pop(0) if self.codes else 200
        if isinstance(code, Exception):
            raise code
        return code


def _tick(shop: Shop, post: Poster, now: datetime) -> int:
    return shop.run(lambda: courier.tick(post, now))


@pytest.mark.parametrize("adapter_cls", ADAPTERS)
def test_book_delivery_idempotent(courier_db: Shop, adapter_cls: type[Any]) -> None:
    adapter = adapter_cls(_card(), "live")
    key = _key()

    first = _book(courier_db, adapter, key, _win(10, 11))
    again = _book(courier_db, adapter, key, _win(10, 11))

    assert again == first
    assert first == {"external_delivery_id": key, "state": "booked", "window": _wire(10, 11)}
    rows = courier_db.query("SELECT idempotency_key, env, agent_id FROM mock_deliveries")
    assert rows == [{"idempotency_key": key, "env": "live", "agent_id": COURIER}]
    assert _row(courier_db, key)["batch_id"] == ids.mock_batch_id(key)

    # The same key from another card or env is not this card's delivery.
    for other in (adapter_cls(_card(OTHER_COURIER), "live"), adapter_cls(_card(), "eval")):
        with pytest.raises(AdapterFailure) as e:
            courier_db.run(lambda a=other: a.get_delivery_status(key))
        assert (e.value.reason, e.value.retryable) == ("adapter_4xx", False)


@pytest.mark.parametrize("adapter_cls", ADAPTERS)
def test_status_progression(courier_db: Shop, adapter_cls: type[Any]) -> None:
    adapter = adapter_cls(_card(), "live")
    key = _key()
    before = datetime.now(UTC)
    _book(courier_db, adapter, key, _win(10, 11))
    after = datetime.now(UTC)

    status = courier_db.run(lambda: adapter.get_delivery_status(key))
    assert status == {"external_delivery_id": key, "state": "booked", "window": _wire(10, 11)}
    due = _row(courier_db, key)["next_state_at"]
    assert before + timedelta(seconds=STEP) <= due <= after + timedelta(seconds=STEP)

    post = Poster()
    # Not due yet: nothing is posted.
    assert _tick(courier_db, post, due - timedelta(seconds=1)) == 0
    assert post.bodies == []

    assert _tick(courier_db, post, due) == 1
    assert courier_db.run(lambda: adapter.get_delivery_status(key))["state"] == "picked_up"
    assert _row(courier_db, key)["next_state_at"] == due + timedelta(seconds=STEP)

    assert _tick(courier_db, post, due + timedelta(seconds=STEP)) == 1
    assert courier_db.run(lambda: adapter.get_delivery_status(key))["state"] == "delivered"
    assert _row(courier_db, key)["next_state_at"] is None

    # Delivered is final.
    assert _tick(courier_db, post, due + timedelta(days=1)) == 0
    assert post.bodies == [
        {"external_delivery_id": key, "status": "picked_up", "event_id": f"{key}:picked_up"},
        {"external_delivery_id": key, "status": "delivered", "event_id": f"{key}:delivered"},
    ]


def test_quote_windows_respect_slot_capacity(courier_db: Shop) -> None:
    """Mock only."""
    adapter = MockCourierAdapter(_card(), "live")

    quote = courier_db.run(lambda: adapter.quote_delivery(ZONE, _win(10, 13.5)))
    # Whole 60-minute slots from the start; the last half hour is not a slot.
    assert quote == {
        "price_minor": 599,
        "currency": "USD",
        "windows": [_wire(10, 11), _wire(11, 12), _wire(12, 13)],
    }

    # 11-12 overlaps two booked rows, so it is full; 12-13 overlaps one.
    _book(courier_db, adapter, _key(), _win(11, 12))
    _book(courier_db, adapter, _key(), _win(11.5, 12.5))
    # Rows of another env, card or zone, or no longer booked, do not count.
    _book(courier_db, MockCourierAdapter(_card(), "eval"), _key(), _win(10, 11))
    _book(courier_db, MockCourierAdapter(_card(OTHER_COURIER), "live"), _key(), _win(10, 11))
    _book(courier_db, adapter, _key(), _win(10, 11), zone=OTHER_ZONE)
    moved = _key()
    _book(courier_db, adapter, moved, _win(10, 11))
    _book(courier_db, adapter, _key(), _win(10, 11))
    courier_db.query(
        "UPDATE mock_deliveries SET state = 'picked_up' WHERE external_delivery_id = %s"
        " RETURNING 1",
        moved,
    )

    quote = courier_db.run(lambda: adapter.quote_delivery(ZONE, _win(10, 13.5)))
    assert quote["windows"] == [_wire(10, 11), _wire(12, 13)]

    # A window shorter than one slot, or with no free slot, gives an empty list.
    assert courier_db.run(lambda: adapter.quote_delivery(ZONE, _win(10, 10.5)))["windows"] == []
    assert courier_db.run(lambda: adapter.quote_delivery(ZONE, _win(11, 12)))["windows"] == []


def test_booking_joins_overlapping_booked_batch(courier_db: Shop) -> None:
    """Mock only."""
    adapter = MockCourierAdapter(_card(), "live")
    first, second, later, elsewhere, joiner = (_key() for _ in range(5))

    _book(courier_db, adapter, first, _win(10, 11))
    _book(courier_db, adapter, second, _win(10.5, 11.5))
    _book(courier_db, adapter, later, _win(12, 13))
    _book(courier_db, adapter, elsewhere, _win(10, 11), zone=OTHER_ZONE)

    batch = ids.mock_batch_id(first)
    assert _row(courier_db, first)["batch_id"] == batch
    # It joins the earliest overlapping booked row's batch and keeps its own window.
    assert _row(courier_db, second)["batch_id"] == batch
    assert courier_db.run(lambda: adapter.get_delivery_status(second))["window"] == _wire(
        10.5, 11.5
    )
    # No overlap, or another zone: a batch of its own.
    assert _row(courier_db, later)["batch_id"] == ids.mock_batch_id(later)
    assert _row(courier_db, elsewhere)["batch_id"] == ids.mock_batch_id(elsewhere)

    # Only `booked` rows are joined.
    courier_db.query(
        "UPDATE mock_deliveries SET state = 'picked_up' WHERE external_delivery_id = ANY(%s)"
        " RETURNING 1",
        [first, second],
    )
    _book(courier_db, adapter, joiner, _win(10, 11))
    assert _row(courier_db, joiner)["batch_id"] == ids.mock_batch_id(joiner)
    assert UUID(str(_row(courier_db, joiner)["batch_id"])) != batch


def test_courier_delayed_largest_wins(courier_db: Shop) -> None:
    """Mock only."""
    adapter = MockCourierAdapter(_card(), "live")
    courier_db.event("live", "courier_delayed", COURIER, {"delay_seconds": 30})
    courier_db.event("live", "courier_delayed", COURIER, {"delay_seconds": 90})
    # Another env, another card or an inactive event does not apply.
    courier_db.event("eval", "courier_delayed", COURIER, {"delay_seconds": 500})
    courier_db.event("live", "courier_delayed", OTHER_COURIER, {"delay_seconds": 1000})
    courier_db.event("live", "courier_delayed", COURIER, {"delay_seconds": 2000})
    courier_db.query(
        "UPDATE mock_events SET active = false WHERE params->>'delay_seconds' = '2000' RETURNING 1"
    )
    step = timedelta(seconds=STEP + 90)

    key = _key()
    before = datetime.now(UTC)
    _book(courier_db, adapter, key, _win(10, 11))
    after = datetime.now(UTC)
    due = _row(courier_db, key)["next_state_at"]
    assert before + step <= due <= after + step

    # Each later step is delayed too.
    assert _tick(courier_db, Poster(), due) == 1
    assert _row(courier_db, key)["next_state_at"] == due + step

    # With no active event, a step lasts MOCK_COURIER_STEP_SECONDS.
    courier_db.query("UPDATE mock_events SET active = false RETURNING 1")
    assert _tick(courier_db, Poster(), due + step) == 1
    assert _row(courier_db, key)["state"] == "delivered"
    other = _key()
    _book(courier_db, MockCourierAdapter(_card(), "eval"), other, _win(10, 11))
    assert _row(courier_db, other)["next_state_at"] - datetime.now(UTC) <= timedelta(seconds=STEP)


def test_state_advances_only_after_status_post(courier_db: Shop) -> None:
    """Mock only."""
    adapter = MockCourierAdapter(_card(), "live")
    key = _key()
    _book(courier_db, adapter, key, _win(10, 11))
    due = _row(courier_db, key)["next_state_at"]
    body = {"external_delivery_id": key, "status": "picked_up", "event_id": f"{key}:picked_up"}

    # A non-2xx answer, or a failed post, leaves the row as it was.
    post = Poster(500, 404, ConnectionError("api down"), 204)
    for _ in range(3):
        assert _tick(courier_db, post, due) == 0
        row = _row(courier_db, key)
        assert (row["state"], row["next_state_at"]) == ("booked", due)

    # The next tick retries the same post; a 2xx moves the row.
    assert _tick(courier_db, post, due) == 1
    assert post.bodies == [body] * 4
    assert _row(courier_db, key)["state"] == "picked_up"
