"""The WhatsApp sender: the `api` host's handler for `whatsapp_out` (architecture item 39).

`dispatch` looks up the member's phone and its `channel_mode`:

*   `sim`: insert the message into `sim_outbound`. The Graph API is never called.
*   `real`: call `send_real`. `main.py` passes `GraphSender`, which calls the Graph API
    (issue #73): 3 attempts in total over about a minute, then `StepFailed`, so the row
    becomes `failed` and the run is unaffected (§9). A `Sender` built without `send_real`
    raises `RealSendUnavailable` (fails closed).

The Graph API decides the 24-hour customer service window: a send outside it is refused
there and counts as a failed attempt. Templates for that case are Q5 (experience design).

A member with no phone has nothing to send to, and the message is dropped with a log line.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from services.api.connect import Connect, pool_connect
from services.common.config import Settings
from services.common.consumer import Inbound, StepFailed, StepResult

__all__ = [
    "ATTEMPTS",
    "GRAPH_URL",
    "MAX_BUTTONS",
    "RETRY_DELAYS_SECONDS",
    "SEND_FAILED",
    "TEXTS",
    "UNRENDERABLE",
    "WHERE",
    "GraphSender",
    "RealSendUnavailable",
    "Sender",
    "graph_payload",
    "not_built",
]

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
    """This `Sender` was built without a real sender."""


async def not_built(phone: str, body: dict[str, Any]) -> None:
    raise RealSendUnavailable("this Sender has no real WhatsApp sender")


# --- the Graph API ----------------------------------------------------------------------

GRAPH_URL = "https://graph.facebook.com/v21.0"
WHERE = "whatsapp.sender"
SEND_FAILED = "whatsapp_send_failed"
UNRENDERABLE = "whatsapp_unrenderable"
# 3 attempts in total over about a minute (architecture §9). With an 8 s request timeout
# the worst case is 3 x 8 + 40 = 64 s, inside the consumer's 75 s step cap.
ATTEMPTS = 3
RETRY_DELAYS_SECONDS: tuple[float, ...] = (20.0, 20.0)
REQUEST_TIMEOUT_SECONDS = 8.0
# WhatsApp reply buttons: at most 3 per message.
MAX_BUTTONS = 3

# The texts the designs fix (architecture §9 and item 33). Other wording belongs to the
# agents and experience designs, which add their templates here; until then such a
# message is not sent (fails closed).
TEXTS: dict[str, str] = {
    "retry_prompt": "I couldn't finish this. Reply *retry* to try again.",
    "media_not_supported": "Photos and voice notes aren't supported yet.",
    # Agent-protocol §10 "Replies".
    "already_decided": "Already decided.",
    # Approvals (architecture §8, CUJs 3 and 7, item 33; #63). `{name}` is a `params` value.
    "approval_request": "Your agent wants to spend {amount}, over this week's budget. Approve?",
    "approval_expired": "This approval has already expired.",
    "approval_not_approver": "Only {approver} can approve this.",
    "approval_cancelled": "Order cancelled: no approval.",
}


def _render(template: str, params: Any) -> str | None:
    text = TEXTS.get(template)
    if text is None:
        return None
    try:
        return text.format_map(dict(params or {}))
    except (KeyError, IndexError, ValueError, TypeError):
        return None  # a missing param: not sent (fails closed)


def graph_payload(phone: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """The Graph API `/messages` request for a `whatsapp_out` body, or `None` if its
    template has no text here, a param it names is missing, or it has more buttons than
    WhatsApp allows."""
    text = _render(body.get("template", ""), body.get("params"))
    buttons = body.get("buttons") or []
    if text is None or len(buttons) > MAX_BUTTONS:
        return None
    base: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone.removeprefix("+"),
    }
    if not buttons:
        return {**base, "type": "text", "text": {"body": text}}
    replies = [
        {"type": "reply", "reply": {"id": b["payload"], "title": b["title"]}} for b in buttons
    ]
    return {
        **base,
        "type": "interactive",
        "interactive": {"type": "button", "body": {"text": text}, "action": {"buttons": replies}},
    }


class GraphSender:
    """`send_real` for `channel_mode='real'` phones: one Graph API text or button message.

    An attempt that times out, fails to connect or gets a non-2xx response is retried after
    `RETRY_DELAYS_SECONDS`; after `ATTEMPTS` it raises `StepFailed`. A body that cannot be
    rendered, or missing credentials, raise `StepFailed` without calling the Graph API.
    """

    def __init__(
        self,
        token: str,
        phone_number_id: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        delays: Sequence[float] = RETRY_DELAYS_SECONDS,
    ) -> None:
        self._token = token
        self._phone_number_id = phone_number_id
        self._transport = transport
        self._sleep = sleep
        self._delays = tuple(delays)

    @classmethod
    def from_settings(cls, settings: Settings) -> "GraphSender":
        return cls(settings.whatsapp_token or "", settings.whatsapp_phone_number_id or "")

    async def __call__(self, phone: str, body: dict[str, Any]) -> None:
        payload = graph_payload(phone, body)
        if payload is None:
            raise StepFailed(WHERE, UNRENDERABLE)
        if not self._token or not self._phone_number_id:
            raise StepFailed(WHERE, SEND_FAILED)
        url = f"{GRAPH_URL}/{self._phone_number_id}/messages"
        headers = {"Authorization": f"Bearer {self._token}"}
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SECONDS, transport=self._transport
        ) as client:
            for attempt in range(1, ATTEMPTS + 1):
                try:
                    response = await client.post(url, json=payload, headers=headers)
                    if response.is_success:
                        return
                    error = f"HTTP {response.status_code}"
                except httpx.HTTPError as exc:
                    error = type(exc).__name__
                log.warning("whatsapp send attempt %s/%s failed: %s", attempt, ATTEMPTS, error)
                if attempt < ATTEMPTS:
                    await self._sleep(self._delays[min(attempt, len(self._delays)) - 1])
        raise StepFailed(WHERE, SEND_FAILED)


# --- the handler ------------------------------------------------------------------------


class Sender:
    """`consume_forever("api", Sender(...))` delivers every `whatsapp_out` message."""

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
