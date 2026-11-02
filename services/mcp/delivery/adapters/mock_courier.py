"""`MockCourierAdapter`, the mock courier adapter (architecture item 44).

It has the courier adapter interface (`services/mcp/delivery/server.py` `CourierAdapter`)
over the mock courier backend in `services/mocks/courier.py`, for one card and the call's
`env`. The real adapter (DoorDash Drive) arrives in build step 7.
"""

from services.common.directory import AgentCard
from services.mocks.courier import MockCourier

__all__ = ["MockCourierAdapter"]


class MockCourierAdapter(MockCourier):
    def __init__(self, card: AgentCard, env: str) -> None:
        super().__init__(env=env, agent_id=card.agent_id, currency=card.currency)
