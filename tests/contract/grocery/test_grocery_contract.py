"""Grocery adapter contract (architecture item 69; item 45 amended 2026-11-02, #47;
adapters §11).

Each interface test runs against every grocery adapter: the mock store, and Square retail
on the replay transport (`tests/contract/square.py`) with the network blocked. Tests marked
"mock only" exercise `mock_events` or `mock_stock`. Under `REAL=1` (owner only) the Square
cases call the live sandbox.
"""

import uuid
from typing import Any

import pytest

from services.mcp.grocery.adapters.mock_store import MockStoreAdapter
from services.mcp.grocery.adapters.square import SquareRetailAdapter
from services.mcp.meta import AdapterFailure
from tests.contract import square
from tests.contract.shop import OAT_MILK, SOURDOUGH, SQUARE_STORE, STOCK, STORE, Shop, card

pytestmark = pytest.mark.db

ADAPTERS = [MockStoreAdapter, SquareRetailAdapter]
MILK = "MOCK-MILK-WHOLE"
EGGS = "MOCK-EGGS-12"
LINES = [
    {"sku": MILK, "qty": 2, "unit_minor": 429},
    {"sku": EGGS, "qty": 1, "unit_minor": 349},
]


@pytest.fixture(autouse=True)
def _offline(request: pytest.FixtureRequest) -> None:
    if not square.REAL:
        request.getfixturevalue("no_network")


def _agent(adapter_cls: type[Any]) -> str:
    return SQUARE_STORE if adapter_cls is SquareRetailAdapter else STORE


def _store(env: str = "live") -> MockStoreAdapter:
    return MockStoreAdapter(card(STORE), env)


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
    assert frozenset({"place_order", "amend_order"}) == MockStoreAdapter.OPERATIONS
    assert frozenset({"place_order"}) == SquareRetailAdapter.OPERATIONS
    assert not hasattr(SquareRetailAdapter, "amend_order")


@pytest.mark.parametrize("adapter_cls", ADAPTERS)
def test_search_shape(shop_db: Shop, adapter_cls: type[Any]) -> None:
    adapter = adapter_cls(card(_agent(adapter_cls)), "live")

    result = shop_db.run(lambda: adapter.search_items("milk", {"diet_codes": ["vegetarian"]}))

    assert set(result) == {"items"}
    assert [i["sku"] for i in result["items"]] == [MILK]
    [item] = result["items"]
    assert set(item) == {
        "sku",
        "name",
        "brand",
        "allergens",
        "diets",
        "unit_minor",
        "currency",
        "available_qty",
    }
    assert isinstance(item["unit_minor"], int) and item["currency"] == "USD"
    assert item["allergens"] == ["milk"]
    # The mock counts `mock_stock`; Square's stock is the table's `in_stock`, with no count.
    assert item["available_qty"] == (STOCK if adapter_cls is MockStoreAdapter else None)

    # An empty query lists every item that passes the filters; excluded allergens go.
    every = shop_db.run(lambda: adapter.search_items("", {"allergen_codes": ["milk"]}))
    expected = ["MOCK-BANANAS-2LB", EGGS]
    if adapter_cls is SquareRetailAdapter:
        expected += [OAT_MILK, SOURDOUGH]
    assert sorted(i["sku"] for i in every["items"]) == sorted(expected)
    # A zone the card does not cover returns nothing.
    assert shop_db.run(lambda: adapter.search_items("", {"zone_id": "us-xx-00000"})) == {
        "items": []
    }


@pytest.mark.parametrize("adapter_cls", ADAPTERS)
def test_quote_prices_from_our_table(shop_db: Shop, adapter_cls: type[Any]) -> None:
    adapter = adapter_cls(card(_agent(adapter_cls)), "live")

    quote = shop_db.run(
        lambda: adapter.quote(
            [{"sku": MILK, "qty": 2}, {"sku": EGGS, "qty": 1}, {"sku": "NOPE", "qty": 1}]
        )
    )

    assert quote == {
        "currency": "USD",
        "lines": [
            {
                "sku": MILK,
                "name": "Whole milk 1 gal",
                "qty": 2,
                "unit_minor": 429,
                "available": True,
            },
            {
                "sku": EGGS,
                "name": "Large eggs 12 ct",
                "qty": 1,
                "unit_minor": 349,
                "available": True,
            },
            {"sku": "NOPE", "name": None, "qty": 1, "unit_minor": None, "available": False},
        ],
    }


def test_square_stock_is_in_stock_flag(shop_db: Shop) -> None:
    """Square only: an item with `in_stock = false` is not found and is unavailable."""
    shop_db.query(
        "UPDATE catalog_items SET in_stock = false WHERE agent_id = %s AND sku = %s RETURNING sku",
        SQUARE_STORE,
        MILK,
    )
    adapter = SquareRetailAdapter(card(SQUARE_STORE), "live")

    found = shop_db.run(lambda: adapter.search_items("", {}))
    quote = shop_db.run(lambda: adapter.quote([{"sku": MILK, "qty": 1}]))

    assert MILK not in {i["sku"] for i in found["items"]}
    assert quote["lines"][0]["available"] is False


@pytest.mark.parametrize("adapter_cls", ADAPTERS)
def test_place_order_idempotent(
    shop_db: Shop, monkeypatch: pytest.MonkeyPatch, adapter_cls: type[Any]
) -> None:
    adapter = adapter_cls(card(_agent(adapter_cls)), "live")
    if adapter_cls is SquareRetailAdapter:
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
    rows = shop_db.query("SELECT idempotency_key, env, agent_id FROM mock_orders")
    if adapter_cls is SquareRetailAdapter:
        if not square.REAL:
            assert first["external_order_id"] == square.RECORDED_SQUARE_ORDER_ID
        # A Square order is never a mock order.
        assert rows == []
    else:
        assert first["external_order_id"] == "key-1"
        assert rows == [{"idempotency_key": "key-1", "env": "live", "agent_id": STORE}]


def test_place_order_decrements_stock_once(shop_db: Shop) -> None:
    """Mock only."""
    adapter = _store()

    _place(shop_db, adapter, "key-1")
    assert shop_db.stock("live", STORE, MILK) == STOCK - 2
    assert shop_db.stock("live", STORE, EGGS) == STOCK - 1

    # A replay decrements nothing.
    _place(shop_db, adapter, "key-1")
    assert shop_db.stock("live", STORE, MILK) == STOCK - 2
    # The eval stock of the same card is untouched.
    assert shop_db.stock("eval", STORE, MILK) == STOCK

    # A line that can't be filled refuses the whole order and changes nothing.
    too_many = [{"sku": EGGS, "qty": 1, "unit_minor": 349}, {"sku": MILK, "qty": STOCK}]
    too_many[1]["unit_minor"] = 429
    with pytest.raises(AdapterFailure) as e:
        _place(shop_db, adapter, "key-2", too_many)
    assert (e.value.reason, e.value.retryable) == ("out_of_stock", False)
    assert shop_db.stock("live", STORE, EGGS) == STOCK - 1
    assert shop_db.stock("live", STORE, MILK) == STOCK - 2
    assert [r["idempotency_key"] for r in shop_db.query("SELECT * FROM mock_orders")] == ["key-1"]


def test_out_of_stock_event_respected(shop_db: Shop) -> None:
    """Mock only."""
    adapter = _store()
    # Events for another env or another card do not apply.
    shop_db.event("eval", "out_of_stock", STORE, {"sku": MILK})
    shop_db.event("live", "out_of_stock", "grocery:other-us", {"sku": MILK})
    quote = shop_db.run(lambda: adapter.quote([{"sku": MILK, "qty": 1}]))
    assert quote["lines"][0]["available"] is True

    shop_db.event("live", "out_of_stock", STORE, {"sku": MILK})

    quote = shop_db.run(lambda: adapter.quote([{"sku": MILK, "qty": 1}, {"sku": EGGS, "qty": 1}]))
    assert quote["currency"] == "USD"
    assert [(line["sku"], line["available"]) for line in quote["lines"]] == [
        (MILK, False),
        (EGGS, True),
    ]
    found = shop_db.run(lambda: adapter.search_items("", {}))
    assert MILK not in {i["sku"] for i in found["items"]}

    with pytest.raises(AdapterFailure) as e:
        _place(shop_db, adapter, "key-1")
    assert (e.value.reason, e.value.retryable) == ("out_of_stock", False)
    assert shop_db.query("SELECT * FROM mock_orders") == []
    assert shop_db.stock("live", STORE, EGGS) == STOCK

    # A quantity above the stock is unavailable too, and an unknown SKU is never available.
    quote = shop_db.run(
        lambda: adapter.quote([{"sku": EGGS, "qty": STOCK + 1}, {"sku": "NOPE", "qty": 1}])
    )
    assert [line["available"] for line in quote["lines"]] == [False, False]
    assert quote["lines"][1]["unit_minor"] is None


def test_adapter_down_event_is_retryable_5xx(shop_db: Shop) -> None:
    """Mock only."""
    adapter = _store()
    shop_db.event("live", "adapter_down", STORE)

    calls = [
        lambda: adapter.search_items("milk", {}),
        lambda: adapter.quote([{"sku": MILK, "qty": 1}]),
        lambda: adapter.place_order(
            idempotency_key="key-1", order_id="order-1", lines=LINES, currency="USD"
        ),
    ]
    for call in calls:
        with pytest.raises(AdapterFailure) as e:
            shop_db.run(call)
        assert (e.value.reason, e.value.retryable) == ("adapter_5xx", True)
    assert shop_db.query("SELECT * FROM mock_orders") == []
    assert shop_db.stock("live", STORE, MILK) == STOCK

    # The eval copy of the card is not down.
    eval_store = _store("eval")
    assert shop_db.run(lambda: eval_store.quote([{"sku": MILK, "qty": 1}]))["lines"][0]["available"]
