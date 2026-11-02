"""The grocery class handler (architecture item 30, agent-protocol P23).

The logic is shared with restaurants in `services/agents/common/seller.py`.
"""

from services.agents.common.seller import SellerHandler, build_default
from services.common.consumer import Inbound, StepResult
from services.common.directory import AgentCard

__all__ = ["AREA", "handle"]

AREA = "grocery"
_default: SellerHandler | None = None


async def handle(msg: Inbound, card: AgentCard) -> StepResult:
    """The grocery class handler registered in `services.agents.host.HANDLERS`."""
    global _default
    if _default is None:
        _default = build_default(AREA)
    return await _default(msg, card)
