"""`MockRestaurantAdapter`, the mock restaurant adapter (architecture item 44).

It has the seller adapter interface (`services/mcp/seller.py` `SellerAdapter`) over the
mock restaurant backend in `services/mocks/restaurants.py`, for one card and the call's
`env`. The real adapter (Square restaurant) arrives in build step 7.
"""

from services.common.directory import AgentCard
from services.mocks.restaurants import MockRestaurant

__all__ = ["MockRestaurantAdapter"]


class MockRestaurantAdapter(MockRestaurant):
    def __init__(self, card: AgentCard, env: str) -> None:
        super().__init__(env=env, agent_id=card.agent_id, zone_ids=card.zone_ids)
