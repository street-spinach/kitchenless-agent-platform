"""Inbound member messages become runs (architecture item 33, §8; issue #40).

`ingest` is used by the simulated WhatsApp route, `POST /runs` and, later, the real webhook
(#73). It only writes rows, in one transaction, and never waits on an agent:

1.  Insert `inbound_messages`. On conflict (a duplicate delivery), return. Otherwise set
    the phone's `channel_identities.last_inbound_at` to the row's `received_at`, before any
    routing, for every content kind (experience §5, EX-I10).
2.  Look up the phone in `channel_identities`, then the member's household and consumer
    agent. An unknown phone gets no run. Its "not registered" reply belongs to
    identity-authz I26 (#124).
3.  Buttons.
    *   A payload starting `sf:` (agent-protocol §10 and P29, #70) is parsed as
        `sf:<shortfall_id>:<line>:<sku | drop>` and calls the household tool
        `choose_shortfall_option` with the `api` service token (obtained by
        `call_tool_as`), in a `RunContext` for the shortfall's run. On `already_decided`
        the member gets `already_decided`; nothing else is sent, because the tool sends
        `shortfall_closed` when the last line is picked. A malformed payload, an unknown
        shortfall or a refused call starts nothing. If the tool is unavailable the error
        propagates, so the transaction rolls back and the inbound row is not kept: a
        redelivered tap is tried again.
    *   An approval button (`ap:<approval_id>:approved|rejected`, #63) calls
        `/mcp/household decide_approval` with `api`'s service token; see `_approval_tap`.
    *   A take-turns button (`tt:<order_id>:on|off`, agents §10), checked before the
        approval button, starts a `chat` run (committed first, so the guard sees it) and
        calls `/mcp/household set_take_turns` with `api`'s service token; see
        `_take_turns_tap`.
    *   Any other button starts nothing.
4.  Text equal to "retry", any case: if `runs.retry_target` finds a run, start a new run
    with `retry_of` and send the original request again as `user_message`. Otherwise it is
    normal text.
5.  Media: reply `media_not_supported` in a `chat` run that is
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
from services.common.mcp_client import ToolRefused, call_tool_as
from services.common.run_context import RunContext
from services.safety.scope import unscoped

__all__ = [
    "ALREADY_DECIDED",
    "API_AGENT",
    "APPROVAL_EXPIRED",
    "MEDIA_NOT_SUPPORTED",
    "NOT_APPROVER",
    "SET_TAKE_TURNS",
    "SHORTFALL_PREFIX",
    "TAKE_TURNS_SAVED",
    "WHATSAPP",
    "Content",
    "ingest",
    "parse_shortfall_button",
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
# Shortfall buttons (agent-protocol §10): `sf:<shortfall_id>:<line>:<sku | drop>`.
SHORTFALL_PREFIX = "sf:"
HOUSEHOLD_SERVER = "household"
CHOOSE_SHORTFALL_OPTION = "choose_shortfall_option"
# The `whatsapp_out` template for "That's already been decided." (agent-protocol §10).
ALREADY_DECIDED = "already_decided"
# The take-turns tap (agents §10): `tt:<order_id>:on|off`.
SET_TAKE_TURNS = "set_take_turns"
TAKE_TURNS_SAVED = "take_turns_saved"
TAKE_TURNS_NOT_APPROVER = "take_turns_not_approver"


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
RETURNING external_id, received_at
"""

# The window state (experience §5): every new inbound message from a known phone.
_SET_LAST_INBOUND_SQL = """
UPDATE channel_identities SET last_inbound_at = %s WHERE phone_e164 = %s
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
    async with unscoped(conn, "ingest").cursor(row_factory=dict_row) as cur:
        await cur.execute(_MEMBER_SQL, (phone,))
        rows = await cur.fetchall()
    if len(rows) != 1:
        return None
    return _Member(**rows[0])


async def _run_row(conn: AsyncConnection, run_id: uuid.UUID) -> dict[str, Any]:
    async with unscoped(conn, "ingest").cursor(row_factory=dict_row) as cur:
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
    call_tool: CallTool = mcp_client.call_tool_as,
) -> uuid.UUID | None:
    """Handle one inbound message in one transaction; return the run it started, if any
    (for an approval button, the approval's run).

    `kind`, `overrides` and `run_id` are passed by `POST /runs`; a WhatsApp message starts a
    `chat` run and the consumer agent classifies it. `call_tool` reaches `/mcp/household`
    for an approval button.
    """
    async with conn.transaction():
        cur = await unscoped(conn, "ingest").execute(_INSERT_INBOUND_SQL, (channel, external_id))
        inserted = await cur.fetchone()
        if inserted is None:
            log.info("duplicate inbound %s message ignored", channel)
            return None
        # Before any routing, for every content kind (experience EX-I10). An unknown phone
        # matches no row.
        await unscoped(conn, "ingest").execute(_SET_LAST_INBOUND_SQL, (inserted[1], phone))

        member = await _member(conn, phone)
        if member is None:
            log.info("inbound %s message from an unregistered phone", channel)
            return None

        inbound = ids.inbound_id(channel, external_id)
        trigger: dict[str, Any] = {
            "source": channel,
            "user_id": member.user_id,
            "inbound_id": inbound,
        }
        if content.kind == "button":
            if content.button.startswith(SHORTFALL_PREFIX):
                run = await _shortfall_button(conn, member, inbound, content.button)
                if run is not None:
                    await unscoped(conn, "ingest").execute(
                        _SET_RUN_SQL, (run, channel, external_id)
                    )
                return None
            # The take-turns tap is checked before the approval button (agents §10).
            take_turns = ids.parse_take_turns_button(content.button)
            if take_turns is None:
                tap = ids.parse_approval_button(content.button)
                if tap is None:
                    log.info("inbound button ignored: not a shortfall, take-turns or approval")
                    return None
                approval_run = await _approval_tap(conn, member, inbound, *tap, call_tool=call_tool)
                if approval_run is not None:
                    await unscoped(conn, "ingest").execute(
                        _SET_RUN_SQL, (approval_run, channel, external_id)
                    )
                return approval_run
            # A new `chat` run, since the order's run has ended. It is committed below,
            # before the tool call, so that the guard's step 0 sees it running.
            started = await runs.start(
                conn, household_id=member.household_id, kind=CHAT, trigger=trigger
            )
            await unscoped(conn, "ingest").execute(_SET_RUN_SQL, (started, channel, external_id))
        else:
            if overrides:
                trigger["overrides"] = overrides

            if content.kind == "media":
                started = await _media_reply(conn, member, inbound, trigger)
            else:
                started = await _start_text(
                    conn, member, inbound, trigger, content.text, kind, run_id
                )

            await unscoped(conn, "ingest").execute(_SET_RUN_SQL, (started, channel, external_id))
            return started

    # Only a take-turns tap gets here.
    await conn.commit()
    order_id, on = take_turns
    await _take_turns_tap(conn, started, member, inbound, order_id, on, call_tool=call_tool)
    return started


# The household approver's display name (experience EX-I2).
_APPROVER_NAME_SQL = """
SELECT u.display_name FROM approval_rules r
JOIN users u ON u.user_id = r.approver_user_id
WHERE r.household_id = %s
"""


async def _take_turns_tap(
    conn: AsyncConnection,
    run_id: uuid.UUID,
    member: _Member,
    inbound: uuid.UUID,
    order_id: uuid.UUID,
    on: bool,
    *,
    call_tool: CallTool,
) -> None:
    """Steps 2 to 5 of the take-turns tap (agents §10): `set_take_turns` with `api`'s
    service token in the new run; `take_turns_saved` only if the setting changed; a
    `wrong_approver` refusal replies `take_turns_not_approver` with the approver's name
    (experience EX-I2); any other refusal is logged and sends nothing; the run ends
    `completed`.

    If the tool cannot be reached the error propagates, nothing is sent and the run is
    left to its `run_timeout`. The tap can simply be sent again.
    """
    args = {"order_id": str(order_id), "on": on, "user_id": str(member.user_id)}
    ctx = RunContext(run_id=run_id, agent_id=API_AGENT, conversation_id=None)
    reason: str | None = None
    try:
        result = await call_tool(HOUSEHOLD_SERVER, SET_TAKE_TURNS, args, ctx=ctx)
    except ToolRefused as refused:
        log.info("take-turns tap refused: %s", refused.reason)
        result, reason = {}, refused.reason
    async with conn.transaction():
        if reason == WRONG_APPROVER:
            cur = await unscoped(conn, "ingest").execute(_APPROVER_NAME_SQL, (member.household_id,))
            (name,) = await cur.fetchone() or (None,)
            params = {"approver": name or "the approver"}
            await _reply(conn, run_id, member, inbound, TAKE_TURNS_NOT_APPROVER, params)
        elif result.get("changed") is True:
            state = {"state": "on" if result.get("take_turns") else "off"}
            await _reply(conn, run_id, member, inbound, TAKE_TURNS_SAVED, state)
        await runs.finish(conn, run_id, API_AGENT, "completed", SET_TAKE_TURNS)


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
    async with unscoped(conn, "ingest").cursor(row_factory=dict_row) as cur:
        await cur.execute(_APPROVAL_SQL, (approval_id,))
        return await cur.fetchone()


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
    *   Expired: reply `approval_expired` (CUJ 3).
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
        result = await call_tool(HOUSEHOLD_SERVER, "decide_approval", args, ctx=ctx)
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
        await unscoped(conn, "ingest").execute(
            _CANCEL_EXPIRY_SQL, (run_id, Jsonb({"approval_id": str(approval_id)}))
        )
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
    cur = await unscoped(conn, "ingest").execute(
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
        result = await call_tool_as(
            HOUSEHOLD_SERVER,
            CHOOSE_SHORTFALL_OPTION,
            {
                "shortfall_id": str(parsed.shortfall_id),
                "line": parsed.line,
                "pick": parsed.pick,
                "user_id": str(member.user_id),
            },
            ctx=ctx,
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
