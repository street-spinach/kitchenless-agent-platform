"""Inbound member messages become runs (architecture item 33, §8; issue #40).

`ingest` is used by the simulated WhatsApp route, `POST /runs` and, later, the real webhook
(#73). It only writes rows, in one transaction, and never waits on an agent:

1.  Insert `inbound_messages`. On conflict (a duplicate delivery), return.
2.  Look up the phone in `channel_identities`, then the member's household and consumer
    agent. An unknown phone gets no run. Its "not registered" reply belongs to
    identity-authz I26 (#124).
3.  Buttons. Approval buttons are #63; until then they start nothing. A payload starting
    `sf:` (agent-protocol §10 and P29, #70) is parsed as
    `sf:<shortfall_id>:<line>:<sku | drop>` and calls the household tool
    `choose_shortfall_option` with the `api` service token, in a `RunContext` for the
    shortfall's run. On `already_decided` the member gets "Already decided."; nothing else
    is sent, because the tool sends `shortfall_closed` when the last line is picked. A
    malformed payload, an unknown shortfall or a refused call starts nothing. If the tool
    is unavailable the error propagates, so the transaction rolls back and the inbound row
    is not kept: a redelivered tap is tried again.
4.  Text equal to "retry", any case: if `runs.retry_target` finds a run, start a new run
    with `retry_of` and send the original request again as `user_message`. Otherwise it is
    normal text.
5.  Media: reply "Photos and voice notes aren't supported yet." in a `chat` run that is
    finished `completed` at once, since every agent message needs a run.
6.  Anything else: `runs.start(...)` and send `user_message` to the member's consumer agent.

The member's text is kept in `runs.trigger.text`, where the consumer agent reads it.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from services.common import ids, outbox, runs
from services.common.mcp_client import ToolRefused, call_tool
from services.common.run_context import RunContext
from services.identity.tokens import MockIssuer

__all__ = [
    "ALREADY_DECIDED",
    "API_AGENT",
    "MEDIA_NOT_SUPPORTED",
    "SHORTFALL_PREFIX",
    "WHATSAPP",
    "Content",
    "ingest",
    "parse_shortfall_button",
]

log = logging.getLogger(__name__)

WHATSAPP = "channel:whatsapp"
# The sender of replies `api` writes itself (architecture §3).
API_AGENT = "system:api"
MEDIA_NOT_SUPPORTED = "media_not_supported"
CHAT = "chat"
RETRY_TEXT = "retry"
# Shortfall buttons (agent-protocol §10): `sf:<shortfall_id>:<line>:<sku | drop>`.
SHORTFALL_PREFIX = "sf:"
HOUSEHOLD_SERVER = "household"
CHOOSE_SHORTFALL_OPTION = "choose_shortfall_option"
# The `whatsapp_out` template for "Already decided." (agent-protocol §10).
ALREADY_DECIDED = "already_decided"


@dataclass(frozen=True)
class ShortfallPick:
    shortfall_id: uuid.UUID
    line: int
    pick: str


def parse_shortfall_button(payload: str) -> ShortfallPick | None:
    """`sf:<shortfall_id>:<line>:<sku | drop>`, or `None` if malformed (fails closed)."""
    parts = payload.split(":", 3)
    if len(parts) != 4 or parts[0] + ":" != SHORTFALL_PREFIX:
        return None
    _, raw_id, raw_line, pick = parts
    try:
        shortfall_id = uuid.UUID(raw_id)
    except ValueError:
        return None
    if not raw_line.isdecimal() or int(raw_line) < 1 or not pick:
        return None
    return ShortfallPick(shortfall_id, int(raw_line), pick)


def _service_token() -> str:
    # The `api` service token. Every caller uses the mock issuer until identity-authz
    # moves `ingest` to `call_tool_as` (identity-authz Q4 and its `ingest.py` row).
    return MockIssuer().issue(API_AGENT, API_AGENT, ())


@dataclass(frozen=True)
class Content:
    """One inbound message: `text`, a `button` press (its payload) or `media`."""

    kind: Literal["text", "button", "media"]
    text: str = ""
    button: str = ""


@dataclass(frozen=True)
class _Member:
    user_id: uuid.UUID
    household_id: str
    consumer: str


_INSERT_INBOUND_SQL = """
INSERT INTO inbound_messages (channel, external_id) VALUES (%s, %s)
ON CONFLICT (channel, external_id) DO NOTHING
RETURNING external_id
"""

_SET_RUN_SQL = """
UPDATE inbound_messages SET run_id = %s WHERE channel = %s AND external_id = %s
"""

# Every enabled consumer card of the phone's user, per household the user belongs to.
_MEMBER_SQL = """
SELECT ci.user_id, hm.household_id, c.agent_id AS consumer
FROM channel_identities ci
JOIN household_members hm ON hm.user_id = ci.user_id
JOIN agent_cards c
  ON c.class = 'consumer' AND c.enabled AND c.owner_ref = 'user:' || ci.user_id::text
WHERE ci.phone_e164 = %s
LIMIT 2
"""


async def _member(conn: AsyncConnection, phone: str) -> _Member | None:
    """The phone's member, or `None` if unknown or ambiguous (fails closed)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_MEMBER_SQL, (phone,))
        rows = await cur.fetchall()
    if len(rows) != 1:
        return None
    return _Member(**rows[0])


async def _run_row(conn: AsyncConnection, run_id: uuid.UUID) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT kind, household_id, trigger FROM runs WHERE run_id = %s", (run_id,)
        )
        row = await cur.fetchone()
    if row is None:
        raise LookupError(f"run {run_id} does not exist")
    return row


def _uuid_or(value: object, default: uuid.UUID) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return default


async def _send_user_message(
    conn: AsyncConnection,
    run_id: uuid.UUID,
    member: _Member,
    inbound: uuid.UUID,
    original: uuid.UUID,
) -> None:
    await outbox.send(
        conn,
        message_id=ids.message_id(inbound, 0),
        run_id=run_id,
        conversation_id=None,
        causation_id=None,
        from_agent=WHATSAPP,
        to_agent=member.consumer,
        type="user_message",
        body={"inbound_id": str(original)},
    )


async def ingest(
    conn: AsyncConnection,
    channel: str,
    external_id: str,
    phone: str,
    content: Content,
    *,
    kind: str = CHAT,
    overrides: dict[str, Any] | None = None,
    run_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Handle one inbound message in one transaction; return the run it started, if any.

    `kind`, `overrides` and `run_id` are passed by `POST /runs`; a WhatsApp message starts a
    `chat` run and the consumer agent classifies it.
    """
    async with conn.transaction():
        cur = await conn.execute(_INSERT_INBOUND_SQL, (channel, external_id))
        if await cur.fetchone() is None:
            log.info("duplicate inbound %s message ignored", channel)
            return None

        member = await _member(conn, phone)
        if member is None:
            log.info("inbound %s message from an unregistered phone", channel)
            return None

        inbound = ids.inbound_id(channel, external_id)
        if content.kind == "button":
            if not content.button.startswith(SHORTFALL_PREFIX):
                log.info("inbound button ignored: approval buttons are not built")
                return None
            run = await _shortfall_button(conn, member, inbound, content.button)
            if run is not None:
                await conn.execute(_SET_RUN_SQL, (run, channel, external_id))
            return None

        trigger: dict[str, Any] = {
            "source": channel,
            "user_id": member.user_id,
            "inbound_id": inbound,
        }
        if overrides:
            trigger["overrides"] = overrides

        if content.kind == "media":
            started = await _media_reply(conn, member, inbound, trigger)
        else:
            started = await _start_text(conn, member, inbound, trigger, content.text, kind, run_id)

        await conn.execute(_SET_RUN_SQL, (started, channel, external_id))
        return started


async def _start_text(
    conn: AsyncConnection,
    member: _Member,
    inbound: uuid.UUID,
    trigger: dict[str, Any],
    text: str,
    kind: str,
    run_id: uuid.UUID | None,
) -> uuid.UUID:
    household_id = member.household_id
    original = inbound
    trigger["text"] = text
    if kind == CHAT and text.strip().lower() == RETRY_TEXT:
        target = await runs.retry_target(conn, member.user_id)
        if target is not None:
            old = await _run_row(conn, target)
            old_trigger = old["trigger"] or {}
            kind, household_id = old["kind"], old["household_id"]
            trigger["retry_of"] = target
            trigger["text"] = old_trigger.get("text", "")
            original = _uuid_or(old_trigger.get("inbound_id"), inbound)

    started = await runs.start(
        conn, household_id=household_id, kind=kind, trigger=trigger, run_id=run_id
    )
    await _send_user_message(conn, started, member, inbound, original)
    return started


async def _media_reply(
    conn: AsyncConnection, member: _Member, inbound: uuid.UUID, trigger: dict[str, Any]
) -> uuid.UUID:
    started = await runs.start(conn, household_id=member.household_id, kind=CHAT, trigger=trigger)
    await outbox.send(
        conn,
        message_id=ids.message_id(inbound, 0),
        run_id=started,
        conversation_id=None,
        causation_id=None,
        from_agent=API_AGENT,
        to_agent=WHATSAPP,
        type="whatsapp_out",
        body={
            "user_id": str(member.user_id),
            "template": MEDIA_NOT_SUPPORTED,
            "params": {},
            "buttons": [],
        },
    )
    await runs.finish(conn, started, API_AGENT, "completed", MEDIA_NOT_SUPPORTED)
    return started


async def _shortfall_button(
    conn: AsyncConnection, member: _Member, inbound: uuid.UUID, payload: str
) -> uuid.UUID | None:
    """Send one shortfall pick to `choose_shortfall_option`; return the shortfall's run.

    Returns `None` for a malformed payload, an unknown shortfall or a refused call. Raises
    `AdapterUnavailable` if the tool cannot be reached.
    """
    parsed = parse_shortfall_button(payload)
    if parsed is None:
        log.info("inbound shortfall button ignored: malformed payload")
        return None
    cur = await conn.execute(
        "SELECT run_id FROM shortfalls WHERE shortfall_id = %s", (parsed.shortfall_id,)
    )
    row = await cur.fetchone()
    if row is None:
        log.info("inbound shortfall button ignored: unknown shortfall")
        return None
    run_id: uuid.UUID = row[0]
    ctx = RunContext(
        run_id=run_id,
        agent_id=API_AGENT,
        conversation_id=ids.shortfall_conversation_id(parsed.shortfall_id),
    )
    try:
        result = await call_tool(
            HOUSEHOLD_SERVER,
            CHOOSE_SHORTFALL_OPTION,
            {
                "shortfall_id": str(parsed.shortfall_id),
                "line": parsed.line,
                "pick": parsed.pick,
                "user_id": str(member.user_id),
            },
            ctx=ctx,
            token=_service_token(),
        )
    except ToolRefused as exc:
        log.info("shortfall pick refused", extra={"reason": exc.reason})
        return run_id
    if result.get("outcome") == "already_decided":
        await outbox.send(
            conn,
            message_id=ids.message_id(inbound, 0),
            run_id=run_id,
            conversation_id=None,
            causation_id=None,
            from_agent=API_AGENT,
            to_agent=WHATSAPP,
            type="whatsapp_out",
            body={
                "user_id": str(member.user_id),
                "template": ALREADY_DECIDED,
                "params": {},
                "buttons": [],
            },
        )
    return run_id
