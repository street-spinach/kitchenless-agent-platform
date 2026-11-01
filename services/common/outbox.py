"""The outbox: the one function that sends every agent message (architecture item 19, §4;
agent-protocol P11, amendment A10).

`send` validates the body, renders the summary, inserts the `agent_messages` row once per
`message_id`, and in the same (caller's) transaction writes the `message` trace and calls
`pg_notify('a2a_<to_host>', '')`, which Postgres delivers at commit. Rows for a target card
with `host_process='external'` are inserted already `processed` and are not notified.
"""

import uuid
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from services.common import directory, ids
from services.common.protocol import OutboundInvalid, ProtocolError, check_static, render_summary
from services.common.trace import TraceStore

__all__ = ["OutboundInvalid", "send"]

EXTERNAL = "external"

_INSERT_SQL = """
INSERT INTO agent_messages
  (message_id, run_id, conversation_id, causation_id, from_agent, to_agent, to_host, type,
   body, status, processed_at)
VALUES
  (%(message_id)s, %(run_id)s, %(conversation_id)s, %(causation_id)s, %(from_agent)s,
   %(to_agent)s, %(to_host)s, %(type)s, %(body)s,
   CASE WHEN %(external)s THEN 'processed' ELSE 'pending' END,
   CASE WHEN %(external)s THEN now() END)
ON CONFLICT (message_id) DO NOTHING
RETURNING message_id
"""

# `TraceStore.write(..., conn=...)` writes on the given connection and never touches the
# pool, so the outbox needs no pool of its own.
_TRACES = TraceStore(None)  # type: ignore[arg-type]


async def _on_behalf_of(conn: AsyncConnection, agent_id: str) -> str | None:
    """The trace's `on_behalf_of`: the sender card's `owner_ref`, `None` for `system`
    (architecture §3 identifiers table)."""
    if ids.parse_agent_id(agent_id)[0] == "system":
        return None
    owner = (await directory.card(conn, agent_id)).owner_ref
    return None if owner == "system" else owner


async def send(
    conn: AsyncConnection,
    *,
    message_id: uuid.UUID,
    run_id: uuid.UUID,
    conversation_id: uuid.UUID | None,
    causation_id: uuid.UUID | None,
    from_agent: str,
    to_agent: str,
    type: str,
    body: dict[str, Any],
    trace_extra: dict[str, Any] | None = None,
) -> bool:
    """Send one message inside the caller's transaction. Return whether a row was inserted.

    Raises `OutboundInvalid` before any insert when the body fails C1-C3 (P4), so the
    sender's own step fails and follows the normal retry rules. The caller computes
    `message_id` with `ids.message_id(...)`; a replayed step reuses it and inserts nothing.
    """
    try:
        parsed = check_static(type, from_agent, to_agent, body)  # step 1
    except ProtocolError as exc:
        raise OutboundInvalid(type, [{"code": exc.code, "detail": exc.detail}]) from None
    summary = render_summary(type, parsed)  # step 2

    to_host = (await directory.card(conn, to_agent)).host_process
    external = to_host == EXTERNAL
    async with conn.transaction():
        cur = await conn.execute(  # step 3
            _INSERT_SQL,
            {
                "message_id": message_id,
                "run_id": run_id,
                "conversation_id": conversation_id,
                "causation_id": causation_id,
                "from_agent": from_agent,
                "to_agent": to_agent,
                "to_host": to_host,
                "type": type,
                "body": Jsonb(body),
                "external": external,
            },
        )
        if await cur.fetchone() is None:
            return False  # step 5

        payload = {
            **(trace_extra or {}),
            "from": from_agent,
            "to": to_agent,
            "type": type,
            "body": body,
            "summary": summary,
            "causation_id": str(causation_id) if causation_id is not None else None,
        }
        await _TRACES.write(  # step 4
            run_id,
            from_agent,
            "message",
            payload,
            on_behalf_of=await _on_behalf_of(conn, from_agent),
            conversation_id=conversation_id,
            message_id=message_id,
            conn=conn,
        )
        if not external:
            await conn.execute("SELECT pg_notify(%s, '')", (f"a2a_{to_host}",))
    return True
