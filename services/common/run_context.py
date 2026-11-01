"""The caller-side run context (architecture item 23a).

`RunContext` is the `ctx` that `call_tool` (item 23), `ModelClient.complete` and `choose`
(item 22) take. The agent host builds it from the inbound message's `run_id`, `to_agent` and
`conversation_id`. It holds ids only: tokens, modes and overrides are loaded where they are
used. The server-side `CallContext` (item 41) is a different type.
"""

from dataclasses import dataclass
from uuid import UUID

__all__ = ["RunContext"]


@dataclass(frozen=True)
class RunContext:
    run_id: UUID
    agent_id: str
    conversation_id: UUID | None
