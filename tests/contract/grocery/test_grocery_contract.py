"""Grocery adapter contract (architecture item 69; item 45 amended 2026-11-02, #47).

Mock mode. Each interface test runs against every grocery adapter; only the mock store
exists until build step 7 adds Square retail and Kroger. Tests marked "mock only" exercise
`mock_events`.
"""

from typing import Any

import pytest

from services.mcp.grocery.adapters.mock_store import MockStoreAdapter
from services.mcp.meta import AdapterFailure
from tests.contract.shop import STOCK, STORE, Shop, card

pytestmark = pytest.mark.db

ADAPTERS = [MockStoreAdapter]
MILK = "MOCK-MILK-WHOLE"
EGGS = "MOCK-EGGS-12"
LINES = [
    {"sku": MILK, "qty": 2, "unit_minor": 429},
    {"sku": EGGS, "qty": 1, "unit_minor": 349},
]


def _store(env: str = "live") -> MockStoreAdapter:
    return MockStoreAdapter(card(STORE), env)


def _place(shop: Shop, adapter: Any, key: str, lines: list[dict[str, Any]] = LINES) -> Any:
    return shop.run(
        lambda: adapter.place_order(
            idempotency_key=key, order_id="order-1", lines=lines, currency="USD"
        )
    )


@pytest.mark.parametrize("adapter_cls", ADAPTERS)
def test_search_shape(shop_db: Shop, adapter_cls: type[Any]) -> None:
    adapter = adapter_cls(card(STORE), "live")

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
    assert item["allergens"] == ["milk"] and item["available_qty"] == STOCK

    # An empty query lists every item that passes the filters; excluded allergens go.
    every = shop_db.run(lambda: adapter.search_items("", {"allergen_codes": ["milk"]}))
    assert sorted(i["sku"] for i in every["items"]) == ["MOCK-BANANAS-2LB", EGGS]
    # A zone the card does not cover returns nothing.
    assert shop_db.run(lambda: adapter.search_items("", {"zone_id": "us-xx-00000"})) == {
        "items": []
    }


@pytest.mark.parametrize("adapter_cls", ADAPTERS)
def test_place_order_idempotent(shop_db: Shop, adapter_cls: type[Any]) -> None:
    adapter = adapter_cls(card(STORE), "live")

    first = _place(shop_db, adapter, "key-1")
    again = _place(shop_db, adapter, "key-1")

    assert again == first
    assert first == {
        "external_order_id": "key-1",
        "state": "placed",
        "total_minor": 2 * 429 + 349,
        "currency": "USD",
    }
    rows = shop_db.query("SELECT idempotency_key, env, agent_id FROM mock_orders")
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
