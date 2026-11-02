"""The context of an A2A demo negotiation (agent-protocol §11 "Building the row", P20).

`start_context` runs in the A2A endpoint's transaction for a `quote_request` with no
`contextId`. It starts the `a2a_demo` run for the seeded `a2a-demo` household and inserts
its `draft` order, both idempotently, and returns the fields the endpoint fills itself
(client values for them are ignored, §11 step 4).
"""

import uuid
from datetime import UTC, datetime, timedelta

from psycopg import AsyncConnection

from services.common import ids, runs
from services.common.config import get_settings
from services.common.directory import AgentCard
from services.common.protocol.a2a import A2AContext

__all__ = ["HOUSEHOLD_ID", "RUN_KIND", "WINDOW_END", "WINDOW_START", "start_context"]

HOUSEHOLD_ID = "a2a-demo"
RUN_KIND = "a2a_demo"
# The window asked for, relative to the request: the household graph's default (item 29).
WINDOW_START = timedelta(hours=2)
WINDOW_END = timedelta(hours=4)
_ORDER_KIND = {"grocery": "grocery", "restaurant": "dinner"}

# The seeded household's first address, as the household MCP server's `create_order` does.
_INSERT_ORDER_SQL = """
INSERT INTO orders (order_id, run_id, household_id, kind, currency, address_ref)
SELECT %s, %s, h.household_id, %s, h.currency, a.address_ref
FROM households h JOIN household_addresses a ON a.household_id = h.household_id
WHERE h.household_id = %s
ORDER BY a.created_at, a.address_ref
LIMIT 1
ON CONFLICT (order_id) DO NOTHING
"""


async def start_context(
    conn: AsyncConnection,
    message_id: uuid.UUID,
    target_card: AgentCard,
    *,
    now: datetime | None = None,
) -> A2AContext:
    """Start the run and draft order for an inbound A2A `quote_request` (P20).

    Runs in the caller's transaction; starting the same `message_id` twice inserts nothing
    new. Raises `ValueError` for a card that is not grocery or restaurant, and `LookupError`
    when the `a2a-demo` household or its address is not seeded.
    """
    kind = _ORDER_KIND.get(target_card.agent_class)
    if kind is None:
        raise ValueError(f"{target_card.agent_id}: A2A negotiation is for grocery or restaurant")
    cur = await conn.execute(
        "SELECT zone_id, currency FROM households WHERE household_id = %s", (HOUSEHOLD_ID,)
    )
    household = await cur.fetchone()
    if household is None:
        raise LookupError(f"household {HOUSEHOLD_ID} is not seeded")
    zone_id, currency = household

    run_id = ids.a2a_run_id(message_id)
    order_id = ids.a2a_order_id(message_id)
    await runs.start(
        conn,
        household_id=HOUSEHOLD_ID,
        kind=RUN_KIND,
        trigger={"source": "a2a", "inbound_id": message_id},
        run_id=run_id,
    )
    cur = await conn.execute(_INSERT_ORDER_SQL, (order_id, run_id, kind, HOUSEHOLD_ID))
    if cur.rowcount == 0:
        cur = await conn.execute("SELECT 1 FROM orders WHERE order_id = %s", (order_id,))
        if await cur.fetchone() is None:
            raise LookupError(f"household {HOUSEHOLD_ID} has no address")

    now = now or datetime.now(UTC)
    return A2AContext(
        run_id=run_id,
        order_id=order_id,
        conversation_id=ids.conversation_id(message_id),
        zone_id=zone_id,
        currency=currency.strip(),
        window={
            "start": (now + WINDOW_START).isoformat(),
            "end": (now + WINDOW_END).isoformat(),
        },
        reply_by=(now + timedelta(seconds=get_settings().quote_round_seconds)).isoformat(),
    )
