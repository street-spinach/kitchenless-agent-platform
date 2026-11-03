"""The WhatsApp sender: the `api` host's handler for `whatsapp_out` (architecture item 39).

`dispatch` looks up the member's phone, its `channel_mode` and its window state
(`last_inbound_at`), then follows experience §5:

1.  Render and validate the body (both paths). A body that breaks the Graph limits ends
    with outcome `refused`, reason `whatsapp_unrenderable`.
2.  Inside the window (`last_inbound_at` within 23 h 45 min): send; outcome `sent`.
    *   `sim`: insert into `sim_outbound`, with the Graph payload under `graph`. The Graph
        API is never called.
    *   `real`: call `send_real`. `main.py` passes `GraphSender`, which calls the Graph API
        (issue #73): 3 attempts in total over about a minute, then `StepFailed`, so the row
        becomes `failed` and the run is unaffected (§9). A Graph 131047 (outside the window
        after all) raises `GraphRefused` and continues at step 3. A `Sender` built without
        `send_real` raises `RealSendUnavailable` (fails closed).
3.  Outside, with an approved Meta template for the key (`WA_TPL_<KEY>`) whose parameters
    code can build (`templates.params`): send the template, on the sim path into
    `sim_outbound` as above; outcome `template` (EX-I13). A Graph 132000-132999 (template
    refused or paused) ends with outcome `refused`, reason `template_refused`, not retried.
4.  Outside, otherwise: a holdable message is held (`held_messages`, outcome `held`);
    anything else is `skipped`.

Every outcome writes one `whatsapp_sends` row and one `decision` trace row
(`node: whatsapp_window`). A replayed step whose `whatsapp_sends` row exists does nothing.

A member with no phone has nothing to send to, and the message is dropped with a log line.
"""

import asyncio
import datetime
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from services.api.connect import Connect, pool_connect
from services.api.whatsapp import templates
from services.common.config import Settings
from services.common.consumer import Inbound, StepFailed, StepResult
from services.common.trace import TraceStore
from services.safety.scope import unscoped

__all__ = [
    "ATTEMPTS",
    "GRAPH_URL",
    "HELD",
    "NO_TEMPLATE",
    "OUTSIDE_WINDOW",
    "OUTSIDE_WINDOW_CODE",
    "MAX_BODY_CHARS",
    "MAX_BUTTONS",
    "MAX_TITLE_CHARS",
    "REFUSED",
    "RETRY_DELAYS_SECONDS",
    "SEND_FAILED",
    "SENT",
    "SKIPPED",
    "TEMPLATE",
    "TEMPLATE_REFUSED",
    "TEXTS",
    "UNRENDERABLE",
    "WHERE",
    "WINDOW",
    "WINDOW_NODE",
    "GraphRefused",
    "GraphSender",
    "RealSendUnavailable",
    "Sender",
    "graph_payload",
    "not_built",
    "template_payload",
]

log = logging.getLogger(__name__)

# One phone per member; the earliest wins if there are more. `inside` is the window check
# (experience §5): NULL `last_inbound_at` is outside (fails closed).
_PHONE_SQL = """
SELECT phone_e164, channel_mode, last_inbound_at,
       coalesce(last_inbound_at > now() - %(window)s::interval, false) AS inside
FROM channel_identities
WHERE user_id = %(user_id)s ORDER BY created_at, phone_e164 LIMIT 1
"""

# A step replayed after its outcome was written does nothing again.
_DONE_SQL = "SELECT 1 FROM whatsapp_sends WHERE message_id = %s"

_SENDS_SQL = """
INSERT INTO whatsapp_sends (message_id, user_id, outcome, reason, graph_message_id)
VALUES (%s, %s, %s, %s, %s) ON CONFLICT (message_id) DO NOTHING
"""

_HOLD_SQL = """
INSERT INTO held_messages (message_id, user_id, body, ref_kind, ref_id)
VALUES (%s, %s, %s, %s, %s) ON CONFLICT (message_id) DO NOTHING
"""

# A replayed step (the row went back to `pending` after the insert) inserts nothing again.
_SIM_SQL = """
INSERT INTO sim_outbound (phone_e164, run_id, body)
SELECT %(phone)s::text, %(run_id)s::uuid, %(body)s::jsonb
WHERE NOT EXISTS (SELECT 1 FROM sim_outbound
                  WHERE phone_e164 = %(phone)s::text
                    AND body->>'message_id' = %(message_id)s::text)
"""

# `TraceStore.write(..., conn=...)` never touches the pool.
_TRACES = TraceStore(None)  # type: ignore[arg-type]
WHATSAPP = "channel:whatsapp"

# Returns the Graph message id (`messages[0].id`), or `None` if the response has none.
SendReal = Callable[[str, dict[str, Any]], Awaitable[str | None]]


class RealSendUnavailable(RuntimeError):
    """This `Sender` was built without a real sender."""


class GraphRefused(Exception):
    """The Graph API refused a send with an error code that a retry cannot change."""

    def __init__(self, code: int) -> None:
        super().__init__(f"Graph error {code}")
        self.code = code


async def not_built(phone: str, body: dict[str, Any]) -> str | None:
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
# WhatsApp limits (experience §5 step 2): at most 3 reply buttons per message, each title
# at most 20 characters, and a body of at most 1,024 characters.
MAX_BUTTONS = 3
MAX_TITLE_CHARS = 20
MAX_BODY_CHARS = 1024
# The `decision` trace row for a send's outcome (experience §5 and §15).
WINDOW_NODE = "whatsapp_window"
# Outcomes and reasons (experience §5).
SENT = "sent"
TEMPLATE = "template"
HELD = "held"
SKIPPED = "skipped"
REFUSED = "refused"
OUTSIDE_WINDOW = "outside_window"
NO_TEMPLATE = "no_template"
TEMPLATE_REFUSED = "template_refused"
# Inside means `last_inbound_at` later than now minus 23 h 45 min; the margin fails closed.
WINDOW = datetime.timedelta(hours=23, minutes=45)
# Graph: the person is outside the window (§5 step 7). Not retried.
OUTSIDE_WINDOW_CODE = 131047
# Template refused or paused (§5 step 7): outcome `refused`. Not retried.
_TEMPLATE_REFUSED_CODES = range(132000, 133000)

# The message catalog (experience §6). `{x}` is a `params` value; lists are passed already
# joined, as one string param. A key that is not here is not sent (fails closed).
TEXTS: dict[str, str] = {
    "retry_prompt": "I couldn't finish that. Reply *retry* and I'll try again.",
    "media_not_supported": "I can't read photos or voice notes yet. Please type it.",
    # Agent-protocol §10 "Replies".
    "already_decided": "That's already been decided.",
    # Approvals (architecture §8, CUJs 3 and 7). Neutral wording (agents AG-A14): an
    # approval is not always about the budget. `reply_by` is the expiry time (EX-I2).
    "approval_request": (
        "Your agent wants to spend {amount} and needs your approval. Reply by {reply_by}."
    ),
    "approval_expired": "This approval has expired, so the order was cancelled.",
    "approval_not_approver": "Only {approver} can approve this.",
    "approval_cancelled": "Order cancelled: it wasn't approved.",
    # Agents §11.
    "chat_reply": "{text}",
    "order_summary": "{text}",
    "no_offers": "No store could fill this order right now. Reply *retry* to try again.",
    "off_topic_reply": "I can only help with food: meals, groceries and dinner orders.",
    "dislike_saved": (
        "Saved: you don't like {value}. I'll avoid it when I can. "
        "Allergies are set by your household admin."
    ),
    "dislike_removed": "Done: {value} is off your dislikes.",
    "dislike_not_removed": (
        "I can only forget dislikes you told me. Your household admin can change the others."
    ),
    "dislike_not_saved": "Allergies and diets are saved by {admin}. Ask them to tell me.",
    "take_turns_offer": (
        "Tonight's dinner had something one person doesn't like ({values}). "
        "Want me to take turns on whose dislikes come first?"
    ),
    "take_turns_offer_off": (
        "Tonight's dinner had something one person doesn't like ({values}). Take turns is on."
    ),
    "take_turns_saved": "Take turns is now {state}.",
    "take_turns_not_approver": "Only {approver} can change take turns.",
    # Agent-protocol §10: model-written shortfall text, buttons built by code.
    "shortfall_prompt": "{text}",
    "shortfall_result": "{text}",
    # Pantry §6: a weekly order with everything at home (PT-OD5). Its experience §6
    # wording lands with pantry P5's sender, not here (EX-I1).
    "nothing_needed": "Nothing to order this week. Already at home: {items}.",
    # Onboarding by chat (experience §9).
    "setup_ask": (
        "Let's set up your household. Tell me who lives with you (and the phone number of "
        "each adult who'll use WhatsApp), any allergies or diets, and your weekly food budget."
    ),
    "setup_restate": "I'll save: {lines}. {unmatched}Nothing else is saved.",
    "setup_remove_confirm": "Remove {value} for {name}? Orders could then include it.",
    "setup_saved": "Saved.",
    "setup_complete": "You're set up. Tell me when you'd like your first order.",
    "setup_discarded": "OK, nothing was saved.",
    "setup_not_admin": "Only {admin} can change this.",
    "setup_expired": "That expired. Send it again and I'll restate it.",
    "setup_not_understood": (
        "I didn't catch that. Try one person at a time, for example: Kavi is allergic to peanuts."
    ),
    "setup_phone_taken": "{phone} is already in another household, so I didn't add it.",
    "join_link": "Send this to {name}. It works only from {phone}: {link}",
    "joined": "Welcome, {name}. You're in {household}. Tell me anything you don't like to eat.",
    # The /me pages (experience §8).
    "me_link": "Here's your page. The link works for 10 minutes: {link}",
    # Held items (experience §5).
    "held_cancelled_line": (
        "While you were away, the order for {amount} was cancelled because it wasn't "
        "approved in time."
    ),
    # Reminders (experience §10).
    "reminder_delivery_today": (
        "Groceries from {store} arrive today, {window}. Nothing needed from you."
    ),
    "reminder_pantry_summary": "At home by our records: {at_home}. Not sure about: {unsure}.",
    "reminders_off": "Reminders are off. Write *reminders on* to turn them back on.",
    "reminders_on": "Reminders are on.",
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
    template has no text here, a param it names is missing, or it breaks a WhatsApp limit
    (more than 3 buttons, a title over 20 characters, a body over 1,024 characters)."""
    text = _render(body.get("template", ""), body.get("params"))
    buttons = body.get("buttons") or []
    if text is None or len(text) > MAX_BODY_CHARS or len(buttons) > MAX_BUTTONS:
        return None
    if any(len(b["title"]) > MAX_TITLE_CHARS for b in buttons):
        return None
    base = _base(phone)
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


def _base(phone: str) -> dict[str, Any]:
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone.removeprefix("+"),
    }


def template_payload(phone: str, template: dict[str, Any]) -> dict[str, Any]:
    """The Graph API `/messages` request for a Meta template (`templates.request`)."""
    return {**_base(phone), "type": "template", "template": template}


class GraphSender:
    """`send_real` for `channel_mode='real'` phones: one Graph API text or button message,
    or the Meta template under the body's `meta_template` key when the `Sender` adds one.

    Returns the Graph message id (`messages[0].id`). A Graph error body with code 131047
    or 132000-132999 raises `GraphRefused(code)` at once: a retry cannot change it. Any
    other failed attempt (a timeout, a connection error, another non-2xx response) is
    retried after `RETRY_DELAYS_SECONDS`; after `ATTEMPTS` it raises `StepFailed`. A body
    that cannot be rendered, or missing credentials, raise `StepFailed` without calling the
    Graph API.
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

    async def __call__(self, phone: str, body: dict[str, Any]) -> str | None:
        template = body.get("meta_template")
        payload = (
            template_payload(phone, template)
            if isinstance(template, dict)
            else graph_payload(phone, body)
        )
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
                        return _graph_message_id(response)
                    code = _graph_error_code(response)
                    if code == OUTSIDE_WINDOW_CODE or code in _TEMPLATE_REFUSED_CODES:
                        raise GraphRefused(code)
                    error = f"HTTP {response.status_code}"
                except httpx.HTTPError as exc:
                    error = type(exc).__name__
                log.warning("whatsapp send attempt %s/%s failed: %s", attempt, ATTEMPTS, error)
                if attempt < ATTEMPTS:
                    await self._sleep(self._delays[min(attempt, len(self._delays)) - 1])
        raise StepFailed(WHERE, SEND_FAILED)


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _graph_message_id(response: httpx.Response) -> str | None:
    data = _json(response)
    try:
        message_id = data["messages"][0]["id"]
    except (KeyError, IndexError, TypeError):
        return None
    return message_id if isinstance(message_id, str) and message_id else None


def _graph_error_code(response: httpx.Response) -> int | None:
    data = _json(response)
    try:
        code = data["error"]["code"]
    except (KeyError, TypeError):
        return None
    return code if isinstance(code, int) else None


# --- the handler ------------------------------------------------------------------------


class Sender:
    """`consume_forever("api", Sender(...))` delivers every `whatsapp_out` message.

    `settings` holds the approved Meta template names; without it no template is sent
    (fails closed)."""

    def __init__(
        self,
        connect: Connect = pool_connect,
        send_real: SendReal = not_built,
        settings: Settings | None = None,
    ) -> None:
        self._connect = connect
        self._send_real = send_real
        self._settings = settings

    async def __call__(self, msg: Inbound) -> StepResult:
        row = msg.row
        if row.type != "whatsapp_out":
            raise ValueError(f"the api host only sends whatsapp_out, got {row.type!r}")
        body = msg.body.model_dump(mode="json")
        user_id = body["user_id"]
        async with self._connect() as conn:
            async with unscoped(conn, "whatsapp_sender").cursor(row_factory=dict_row) as cur:
                await cur.execute(_PHONE_SQL, {"window": WINDOW, "user_id": user_id})
                phone = await cur.fetchone()
                if phone is None:
                    log.warning("whatsapp_out %s: the member has no phone", row.message_id)
                    return StepResult()
                await cur.execute(_DONE_SQL, (row.message_id,))
                if await cur.fetchone() is not None:
                    return StepResult()  # a replayed step: its outcome is written
            graph = graph_payload(phone["phone_e164"], body)
            if graph is None:
                log.warning("whatsapp_out %s: %s", row.message_id, UNRENDERABLE)
                await _record(conn, msg, user_id, REFUSED, UNRENDERABLE)
                return StepResult()
            if not phone["inside"]:
                await self._outside(conn, msg, body, phone)
                return StepResult()
            if phone["channel_mode"] == "sim":
                await _sim(conn, msg, phone["phone_e164"], {**body, "graph": graph}, SENT)
                return StepResult()
            try:
                graph_message_id = await self._send_real(phone["phone_e164"], body)
            except GraphRefused as refused:
                if refused.code != OUTSIDE_WINDOW_CODE:
                    await _refused(conn, msg, user_id, refused)
                    return StepResult()
                # Outside after all (§5 step 7): not retried; continue at step 4.
                await self._outside(conn, msg, body, phone)
                return StepResult()
            await _record(conn, msg, user_id, SENT, None, graph_message_id)
        return StepResult()

    async def _outside(
        self, conn: AsyncConnection, msg: Inbound, body: dict[str, Any], phone: dict[str, Any]
    ) -> None:
        """§5 step 4 outside the window: the Meta template, if the key has an approved name
        and code can build its parameters; otherwise steps 5-6."""
        key, user_id = body["template"], body["user_id"]
        name = templates.meta_name(key, self._settings) if self._settings else None
        values = await templates.params(conn, key, body) if name else None
        if name is None or values is None:
            await _hold_or_skip(conn, msg, body)
            return
        template = templates.request(name, values, body.get("buttons") or [])
        if phone["channel_mode"] == "sim":
            graph = template_payload(phone["phone_e164"], template)
            await _sim(conn, msg, phone["phone_e164"], {**body, "graph": graph}, TEMPLATE)
            return
        try:
            graph_message_id = await self._send_real(
                phone["phone_e164"], {**body, "meta_template": template}
            )
        except GraphRefused as refused:
            if refused.code == OUTSIDE_WINDOW_CODE:
                # A template may be sent outside the window, so this is not expected; it is
                # not retried and does not loop back to step 4.
                raise StepFailed(WHERE, SEND_FAILED) from refused
            await _refused(conn, msg, user_id, refused)
            return
        await _record(conn, msg, user_id, TEMPLATE, None, graph_message_id)


async def _sim(
    conn: AsyncConnection, msg: Inbound, phone: str, body: dict[str, Any], outcome: str
) -> None:
    """The sim path: one `sim_outbound` row (the Graph request under `graph`) and the
    outcome, in one transaction."""
    message_id = str(msg.row.message_id)
    async with conn.transaction():
        await unscoped(conn, "whatsapp_sender").execute(
            _SIM_SQL,
            {
                "phone": phone,
                "run_id": msg.row.run_id,
                "body": Jsonb({"message_id": message_id, **body}),
                "message_id": message_id,
            },
        )
        await _record(conn, msg, body["user_id"], outcome, None)


async def _refused(
    conn: AsyncConnection, msg: Inbound, user_id: str, refused: GraphRefused
) -> None:
    """§5 step 7: 132000-132999 (template refused or paused) ends `refused`, reason
    `template_refused`, not retried. Any other code raises `StepFailed` as before."""
    if refused.code not in _TEMPLATE_REFUSED_CODES:
        raise StepFailed(WHERE, SEND_FAILED) from refused
    log.warning("whatsapp_out %s: %s (%s)", msg.row.message_id, TEMPLATE_REFUSED, refused.code)
    await _record(conn, msg, user_id, REFUSED, TEMPLATE_REFUSED)


async def _hold_or_skip(conn: AsyncConnection, msg: Inbound, body: dict[str, Any]) -> None:
    """§5 steps 5-6 outside the window with no template: a holdable message is held,
    anything else is skipped."""
    key = body["template"]
    ref = body.get("ref") or {}
    if templates.holdable(key, ref.get("kind")):
        async with conn.transaction():
            await unscoped(conn, "whatsapp_sender").execute(
                _HOLD_SQL,
                (msg.row.message_id, body["user_id"], Jsonb(body), ref["kind"], ref["id"]),
            )
            await _record(conn, msg, body["user_id"], HELD, None)
        return
    reason = NO_TEMPLATE if key in templates.REMINDER_KEYS else OUTSIDE_WINDOW
    await _record(conn, msg, body["user_id"], SKIPPED, reason)


async def _record(
    conn: AsyncConnection,
    msg: Inbound,
    user_id: str,
    outcome: str,
    reason: str | None,
    graph_message_id: str | None = None,
) -> None:
    """One `whatsapp_sends` row and one `decision` trace row for a send's outcome (§5)."""
    async with conn.transaction():
        await unscoped(conn, "whatsapp_sender").execute(
            _SENDS_SQL, (msg.row.message_id, user_id, outcome, reason, graph_message_id)
        )
        await _TRACES.write(
            msg.row.run_id,
            WHATSAPP,
            "decision",
            {
                "node": WINDOW_NODE,
                "choice": outcome,
                "reason": reason,
                "explanation": "",
                "ref": str(msg.row.message_id),
            },
            on_behalf_of=f"user:{user_id}",
            conversation_id=msg.row.conversation_id,
            message_id=msg.row.message_id,
            conn=conn,
        )
