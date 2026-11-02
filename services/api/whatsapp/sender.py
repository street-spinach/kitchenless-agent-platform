"""The WhatsApp sender: the `api` host's handler for `whatsapp_out` (architecture item 39).

`dispatch` looks up the member's phone and its `channel_mode`:

*   `sim`: insert the message into `sim_outbound`. The Graph API is never called.
*   `real`: call `send_real`. The Graph API client, its retries and the 24-hour window
    belong to #73; until then `send_real` raises, so the step fails and follows the
    consumer's retry rules (it fails closed).

A member with no phone has nothing to send to, and the message is dropped with a log line.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from services.api.connect import Connect, pool_connect
from services.common.consumer import Inbound, StepResult

__all__ = ["RealSendUnavailable", "Sender", "not_built"]

log = logging.getLogger(__name__)

# One phone per member; the earliest wins if there are more.
_PHONE_SQL = """
SELECT phone_e164, channel_mode FROM channel_identities
WHERE user_id = %s ORDER BY created_at, phone_e164 LIMIT 1
"""

# A replayed step (the row went back to `pending` after the insert) inserts nothing again.
_SIM_SQL = """
INSERT INTO sim_outbound (phone_e164, run_id, body)
SELECT %(phone)s::text, %(run_id)s::uuid, %(body)s::jsonb
WHERE NOT EXISTS (SELECT 1 FROM sim_outbound
                  WHERE phone_e164 = %(phone)s::text
                    AND body->>'message_id' = %(message_id)s::text)
"""

SendReal = Callable[[str, dict[str, Any]], Awaitable[None]]


class RealSendUnavailable(RuntimeError):
    """The real WhatsApp sender is not built yet (#73)."""


async def not_built(phone: str, body: dict[str, Any]) -> None:
    raise RealSendUnavailable("the real WhatsApp sender is not built yet (#73)")


class Sender:
    """`consume_forever("api", Sender())` delivers every `whatsapp_out` message."""

    def __init__(self, connect: Connect = pool_connect, send_real: SendReal = not_built) -> None:
        self._connect = connect
        self._send_real = send_real

    async def __call__(self, msg: Inbound) -> StepResult:
        row = msg.row
        if row.type != "whatsapp_out":
            raise ValueError(f"the api host only sends whatsapp_out, got {row.type!r}")
        body = msg.body.model_dump(mode="json")
        async with self._connect() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(_PHONE_SQL, (body["user_id"],))
                phone = await cur.fetchone()
            if phone is None:
                log.warning("whatsapp_out %s: the member has no phone", row.message_id)
                return StepResult()
            if phone["channel_mode"] == "sim":
                payload = {"message_id": str(row.message_id), **body}
                async with conn.transaction():
                    await conn.execute(
                        _SIM_SQL,
                        {
                            "phone": phone["phone_e164"],
                            "run_id": row.run_id,
                            "body": Jsonb(payload),
                            "message_id": str(row.message_id),
                        },
                    )
                return StepResult()
        await self._send_real(phone["phone_e164"], body)
        return StepResult()
