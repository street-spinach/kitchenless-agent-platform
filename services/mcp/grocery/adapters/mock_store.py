"""`MockStoreAdapter`, the mock grocery adapter (architecture item 44).

It has the seller adapter interface (`services/mcp/seller.py` `SellerAdapter`) over the
mock store backend in `services/mocks/stores.py`, for one card and the call's `env`.
Real adapters (Square retail, Kroger) arrive in build step 7.
"""

from typing import ClassVar

from services.common.directory import AgentCard
from services.mocks.stores import MockShop

__all__ = ["MockStoreAdapter"]


class MockStoreAdapter(MockShop):
    OPERATIONS: ClassVar[frozenset[str]] = frozenset({"place_order", "amend_order"})

    def __init__(self, card: AgentCard, env: str) -> None:
        super().__init__(env=env, agent_id=card.agent_id, zone_ids=card.zone_ids)
