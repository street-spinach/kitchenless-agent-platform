"""Inbound member messages become runs (architecture item 33, §8; issue #40).

`ingest` is used by the simulated WhatsApp route, `POST /runs` and, later, the real webhook
(#73). It only writes rows, in one transaction, and never waits on an agent:

1.  Insert `inbound_messages`. On conflict (a duplicate delivery), return.
2.  Look up the phone in `channel_identities`, then the member's household and consumer
    agent. An unknown phone gets no run. Its "not registered" reply belongs to
    identity-authz I26 (#124).
3.  An approval button (`ap:<approval_id>:approved|rejected`, #63) calls `/mcp/household
    decide_approval` with `api`'s service token; see `_approval_tap`. Shortfall buttons
    are #70; until then any other button starts nothing.
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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from services.common import ids, mcp_client, outbox, runs
from services.common.mcp_client import ToolRefused
from services.common.run_context import RunContext
from services.identity.tokens import MockIssuer

__all__ = [
    "API_AGENT",
    "APPROVAL_EXPIRED",
    "MEDIA_NOT_SUPPORTED",
    "NOT_APPROVER",
    "WHATSAPP",
    "Content",
    "ingest",
]

log = logging.getLogger(__name__)

CallTool = Callable[..., Awaitable[dict[str, Any]]]

WHATSAPP = "channel:whatsapp"
# The sender of replies `api` writes itself (architecture §3).
API_AGENT = "system:api"
HOUSEHOLD_SERVER = "household"
MEDIA_NOT_SUPPORTED = "media_not_supported"
# Approval button replies (CUJs 3 and 7); texts in `services/api/whatsapp/sender.py`.
APPROVAL_EXPIRED = "approval_expired"
NOT_APPROVER = "approval_not_approver"
WRONG_APPROVER = "wrong_approver"
CHAT = "chat"
RETRY_TEXT = "retry"


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
    call_tool: CallTool = mcp_client.call_tool,
) -> uuid.UUID | None:
    """Handle one inbound message in one transaction; return the run it started, if any
    (for an approval button, the approval's run).

    `kind`, `overrides` and `run_id` are passed by `POST /runs`; a WhatsApp message starts a
    `chat` run and the consumer agent classifies it. `call_tool` reaches `/mcp/household`
    for an approval button.
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
            tap = ids.parse_approval_button(content.button)
            if tap is None:
                log.info("inbound button ignored: not an approval button (shortfalls are #70)")
                return None
            approval_run = await _approval_tap(conn, member, inbound, *tap, call_tool=call_tool)
            if approval_run is not None:
                await conn.execute(_SET_RUN_SQL, (approval_run, channel, external_id))
            return approval_run

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


_APPROVAL_SQL = """
SELECT a.run_id, a.household_id, a.order_id, a.status, u.display_name AS approver
FROM approvals a
LEFT JOIN approval_rules r ON r.household_id = a.household_id
LEFT JOIN users u ON u.user_id = r.approver_user_id
WHERE a.approval_id = %s
"""


# The approval's expiry timer is cancelled once it is decided (architecture §4 timers).
_CANCEL_EXPIRY_SQL = """
UPDATE timers SET status = 'cancelled'
WHERE run_id = %s AND kind = 'approval_expiry' AND status = 'pending' AND ref @> %s
"""


async def _approval(conn: AsyncConnection, approval_id: uuid.UUID) -> dict[str, Any] | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_APPROVAL_SQL, (approval_id,))
        return await cur.fetchone()


def _service_token() -> str:
    # `api`'s own service token (identity-authz §8). Every caller uses the mock issuer
    # until identity-authz replaces the token source (Q4).
    return MockIssuer().issue(API_AGENT, API_AGENT, [])


async def _approval_tap(
    conn: AsyncConnection,
    member: _Member,
    inbound: uuid.UUID,
    approval_id: uuid.UUID,
    decision: str,
    *,
    call_tool: CallTool,
) -> uuid.UUID | None:
    """Step 3, the approval button: `decide_approval` with `api`'s service token.

    *   Decided (now, or by an earlier tap of the approver): send `approval_decision` to
        the household agent, cancel the `approval_expiry` timer and give the run a new
        `run_timeout` (§4 timers).
    *   Expired: reply "This approval has already expired." (CUJ 3).
    *   Not the approver: reply "Only <approver> can approve this." The guard has written
        the refused `tool_result` row (CUJ 7).
    Anything else (an unknown approval, another refusal) changes nothing.
    """
    approval = await _approval(conn, approval_id)
    if approval is None:
        log.info("approval button for an unknown approval ignored")
        return None
    run_id: uuid.UUID = approval["run_id"]
    args = {
        "approval_id": str(approval_id),
        "decision": decision,
        "user_id": str(member.user_id),
    }
    ctx = RunContext(run_id=run_id, agent_id=API_AGENT, conversation_id=None)
    try:
        result = await call_tool(
            HOUSEHOLD_SERVER, "decide_approval", args, ctx=ctx, token=_service_token()
        )
        status = result.get("status")
    except ToolRefused as refused:
        if refused.reason == WRONG_APPROVER:
            name = approval["approver"] or "the approver"
            await _reply(conn, run_id, member, inbound, NOT_APPROVER, {"approver": name})
            return run_id
        # For example `run_ended` once an expired approval has cancelled the run. A refused
        # tap never sends a decision (fails closed); it can only learn of the expiry.
        log.info("approval tap refused: %s", refused.reason)
        if (await _approval(conn, approval_id) or {}).get("status") == "expired":
            await _reply(conn, run_id, member, inbound, APPROVAL_EXPIRED, {})
        return run_id

    if status in ids.APPROVAL_DECISIONS:
        await outbox.send(
            conn,
            message_id=ids.approval_decision_id(approval_id),
            run_id=run_id,
            conversation_id=None,
            causation_id=None,
            from_agent=API_AGENT,
            to_agent=f"household:{approval['household_id']}",
            type="approval_decision",
            body={
                "approval_id": str(approval_id),
                "order_id": str(approval["order_id"]),
                "status": status,
            },
        )
        await conn.execute(_CANCEL_EXPIRY_SQL, (run_id, Jsonb({"approval_id": str(approval_id)})))
        await runs.resume_timeout(conn, run_id)
    elif status == "expired":
        await _reply(conn, run_id, member, inbound, APPROVAL_EXPIRED, {})
    return run_id


async def _reply(
    conn: AsyncConnection,
    run_id: uuid.UUID,
    member: _Member,
    inbound: uuid.UUID,
    template: str,
    params: dict[str, str],
) -> None:
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
            "template": template,
            "params": params,
            "buttons": [],
        },
    )


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
