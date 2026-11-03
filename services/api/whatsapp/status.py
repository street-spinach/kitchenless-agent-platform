"""WhatsApp status callbacks (experience §5 "Status callbacks", EX-I16).

The webhook passes each `entry[].changes[].value.statuses[]` list to `record`. A status
updates `whatsapp_sends.status` and `status_at` by `graph_message_id`; a Graph message id
with no `whatsapp_sends` row is ignored. A `failed` status with error 131047 (the person was
outside the window after all) holds a holdable message: its `held_messages` row is inserted
then, ignoring a duplicate `message_id`. Statuses are validated in code (§13): any other
status value, or a malformed entry, is skipped.
"""

import logging
from collections.abc import Iterable
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from services.api.whatsapp import templates
from services.api.whatsapp.sender import OUTSIDE_WINDOW_CODE
from services.common.protocol.schemas import WhatsAppOut

__all__ = ["FAILED", "STATUSES", "record"]

log = logging.getLogger(__name__)

FAILED = "failed"
# The Graph status values recorded (§5); anything else is skipped.
STATUSES = frozenset({"sent", "delivered", "read", FAILED})

_UPDATE_SQL = """
UPDATE whatsapp_sends s SET status = %s, status_at = now()
FROM agent_messages m
WHERE s.graph_message_id = %s AND m.message_id = s.message_id
RETURNING s.message_id, s.user_id, m.body
"""

_HOLD_SQL = """
INSERT INTO held_messages (message_id, user_id, body, ref_kind, ref_id)
VALUES (%s, %s, %s, %s, %s) ON CONFLICT (message_id) DO NOTHING
"""


def _codes(status: dict[str, Any]) -> set[int]:
    codes: set[int] = set()
    for error in status.get("errors") or []:
        code = error.get("code") if isinstance(error, dict) else None
        if isinstance(code, int):
            codes.add(code)
    return codes


async def record(conn: AsyncConnection, statuses: Iterable[object]) -> None:
    """Record each Graph status callback (§5). Unknown ids and bad entries do nothing."""
    for status in statuses:
        if not isinstance(status, dict):
            continue
        graph_id, value = status.get("id"), status.get("status")
        if not isinstance(graph_id, str) or not graph_id or value not in STATUSES:
            log.info("whatsapp status: skipped %r", value)
            continue
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(_UPDATE_SQL, (value, graph_id))
                send = await cur.fetchone()
            if send is None:
                continue  # not a message this platform sent (or not recorded): ignored
            if value == FAILED and OUTSIDE_WINDOW_CODE in _codes(status):
                await _hold(conn, send)


async def _hold(conn: AsyncConnection, send: dict[str, Any]) -> None:
    """A late 131047 on a holdable message: hold it as the sender would have (§5 step 5)."""
    try:
        body = WhatsAppOut.model_validate(send["body"]).model_dump(mode="json")
    except ValidationError:
        log.warning("whatsapp status: %s has no whatsapp_out body", send["message_id"])
        return
    ref = body.get("ref") or {}
    if not templates.holdable(body["template"], ref.get("kind")):
        return
    await conn.execute(
        _HOLD_SQL, (send["message_id"], send["user_id"], Jsonb(body), ref["kind"], ref["id"])
    )
