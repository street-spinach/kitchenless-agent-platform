"""Mock restaurant backend (architecture item 45, amended 2026-11-02, #47).

The same search, stock, pricing and orders as the mock store (`services/mocks/stores.py`),
over `menu_items`, plus `kitchen_overloaded`: while an active event of that kind (no
params) targets the card in the call's `env`, `quote` and `place_order` raise
`AdapterFailure("kitchen_overloaded", retryable=False)`. `search_items` still works.
"""

from typing import Any

from psycopg import AsyncConnection

from services.mcp.meta import AdapterFailure
from services.mocks.stores import MockShop

__all__ = ["KITCHEN_OVERLOADED", "MockRestaurant"]

KITCHEN_OVERLOADED = "kitchen_overloaded"
_OVERLOADED_TOOLS = frozenset({"quote", "place_order"})


class MockRestaurant(MockShop):
    """The mock backend of one restaurant card in one `env`."""

    table = "menu_items"

    async def _events(self, conn: AsyncConnection, tool: str) -> list[tuple[str, Any]]:
        events = await super()._events(conn, tool)
        if tool in _OVERLOADED_TOOLS and any(kind == KITCHEN_OVERLOADED for kind, _ in events):
            raise AdapterFailure(KITCHEN_OVERLOADED, retryable=False)
        return events
