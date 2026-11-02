"""Restaurant adapter contract (architecture item 69; item 45 amended 2026-11-02, #47).

Mock mode. Each interface test runs against every restaurant adapter; only the mock
restaurant exists until build step 7 adds Square restaurant.
"""

from typing import Any

import pytest

from services.mcp.meta import AdapterFailure
from services.mcp.restaurant.adapters.mock_restaurant import MockRestaurantAdapter
from tests.contract.shop import RESTAURANT, STOCK, Shop, card

pytestmark = pytest.mark.db

ADAPTERS = [MockRestaurantAdapter]
BOWL = "MOCK-DIN-CHICKEN-RICE"
LINES = [{"sku": BOWL, "qty": 2, "unit_minor": 1299}]


def _place(shop: Shop, adapter: Any, key: str) -> Any:
    return shop.run(
        lambda: adapter.place_order(
            idempotency_key=key, order_id="order-1", lines=LINES, currency="USD"
        )
    )


@pytest.mark.parametrize("adapter_cls", ADAPTERS)
def test_place_order_idempotent(shop_db: Shop, adapter_cls: type[Any]) -> None:
    adapter = adapter_cls(card(RESTAURANT), "live")

    first = _place(shop_db, adapter, "key-1")
    again = _place(shop_db, adapter, "key-1")

    assert again == first
    assert first == {
        "external_order_id": "key-1",
        "state": "placed",
        "total_minor": 2 * 1299,
        "currency": "USD",
    }
    assert len(shop_db.query("SELECT * FROM mock_orders")) == 1
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
