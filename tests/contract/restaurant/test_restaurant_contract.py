"""Restaurant adapter contract (architecture item 69; item 45 amended 2026-11-02, #47;
adapters §11).

Each interface test runs against every restaurant adapter: the mock restaurant, and Square
restaurant on the replay transport (`tests/contract/square.py`) with the network blocked.
Tests marked "mock only" exercise `mock_events` or `mock_stock`. Under `REAL=1` (owner
only) the Square cases call the live sandbox.
"""

import uuid
from typing import Any

import pytest

from services.mcp.meta import AdapterFailure
from services.mcp.restaurant.adapters.mock_restaurant import MockRestaurantAdapter
from services.mcp.restaurant.adapters.square import SquareRestaurantAdapter
from tests.contract import square
from tests.contract.shop import RESTAURANT, SQUARE_KITCHEN, STOCK, Shop, card

pytestmark = pytest.mark.db

ADAPTERS = [MockRestaurantAdapter, SquareRestaurantAdapter]
BOWL = "MOCK-DIN-CHICKEN-RICE"
LINES = [{"sku": BOWL, "qty": 2, "unit_minor": 1299}]


@pytest.fixture(autouse=True)
def _offline(request: pytest.FixtureRequest) -> None:
    if not square.REAL:
        request.getfixturevalue("no_network")


def _agent(adapter_cls: type[Any]) -> str:
    return SQUARE_KITCHEN if adapter_cls is SquareRestaurantAdapter else RESTAURANT


def _place(
    shop: Shop,
    adapter: Any,
    key: str,
    lines: list[dict[str, Any]] = LINES,
    order_id: str = "order-1",
) -> Any:
    return shop.run(
        lambda: adapter.place_order(
            idempotency_key=key, order_id=order_id, lines=lines, currency="USD"
        )
    )


def test_operations_declared() -> None:
    assert frozenset({"place_order", "amend_order"}) == MockRestaurantAdapter.OPERATIONS
    assert frozenset({"place_order"}) == SquareRestaurantAdapter.OPERATIONS
    assert not hasattr(SquareRestaurantAdapter, "amend_order")


@pytest.mark.parametrize("adapter_cls", ADAPTERS)
def test_search_and_quote_read_our_menu(shop_db: Shop, adapter_cls: type[Any]) -> None:
    adapter = adapter_cls(card(_agent(adapter_cls)), "live")

    found = shop_db.run(lambda: adapter.search_items("chicken", {}))
    vegan = shop_db.run(lambda: adapter.search_items("", {"diet_codes": ["vegan"]}))
    quote = shop_db.run(lambda: adapter.quote([{"sku": BOWL, "qty": 1}]))

    assert [i["sku"] for i in found["items"]] == [BOWL]
    assert [i["sku"] for i in vegan["items"]] == ["MOCK-DIN-VEG-PASTA"]
    assert quote == {
        "currency": "USD",
        "lines": [
            {
                "sku": BOWL,
                "name": "Grilled chicken and rice bowl",
                "qty": 1,
                "unit_minor": 1299,
                "available": True,
            }
        ],
    }


@pytest.mark.parametrize("adapter_cls", ADAPTERS)
def test_place_order_idempotent(
    shop_db: Shop, monkeypatch: pytest.MonkeyPatch, adapter_cls: type[Any]
) -> None:
    adapter = adapter_cls(card(_agent(adapter_cls)), "live")
    if adapter_cls is SquareRestaurantAdapter:
        replay = None if square.REAL else square.Replay("create_order", "create_order_replay")
        square.use_square(monkeypatch, replay)
        key = str(uuid.uuid4()) if square.REAL else square.RECORDED_KEY
        lines, order_id = square.RECORDED_LINES, square.RECORDED_ORDER_ID
    else:
        key, lines, order_id = "key-1", LINES, "order-1"

    first = _place(shop_db, adapter, key, lines, order_id)
    again = _place(shop_db, adapter, key, lines, order_id)

    assert again == first
    total = sum(line["qty"] * line["unit_minor"] for line in lines)
    assert set(first) == {"external_order_id", "state", "total_minor", "currency"}
    assert {k: first[k] for k in ("state", "total_minor", "currency")} == {
        "state": "placed",
        "total_minor": total,
        "currency": "USD",
    }
    orders = shop_db.query("SELECT * FROM mock_orders")
    if adapter_cls is SquareRestaurantAdapter:
        if not square.REAL:
            assert first["external_order_id"] == square.RECORDED_SQUARE_ORDER_ID
        assert orders == []
    else:
        assert first["external_order_id"] == "key-1"
        assert len(orders) == 1
        assert shop_db.stock("live", RESTAURANT, BOWL) == STOCK - 2


def test_capacity_event_respected(shop_db: Shop) -> None:
    """Mock only: `kitchen_overloaded` refuses quote and place_order; search still works."""
    adapter = MockRestaurantAdapter(card(RESTAURANT), "live")
    shop_db.event("live", "kitchen_overloaded", RESTAURANT)

    for call in (
        lambda: adapter.quote([{"sku": BOWL, "qty": 1}]),
        lambda: adapter.place_order(
            idempotency_key="key-1", order_id="order-1", lines=LINES, currency="USD"
        ),
    ):
        with pytest.raises(AdapterFailure) as e:
            shop_db.run(call)
        assert (e.value.reason, e.value.retryable) == ("kitchen_overloaded", False)
    assert shop_db.query("SELECT * FROM mock_orders") == []
    assert shop_db.stock("live", RESTAURANT, BOWL) == STOCK

    found = shop_db.run(lambda: adapter.search_items("chicken", {}))
    assert [i["sku"] for i in found["items"]] == [BOWL]

    # The eval copy of the card is not overloaded.
    eval_kitchen = MockRestaurantAdapter(card(RESTAURANT), "eval")
    quote = shop_db.run(lambda: eval_kitchen.quote([{"sku": BOWL, "qty": 1}]))
    assert quote["lines"][0]["available"] is True
