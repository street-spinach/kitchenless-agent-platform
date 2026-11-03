"""Inbound member messages become runs (architecture item 33, §8; issue #40).

`ingest` is used by the simulated WhatsApp route, `POST /runs` and, later, the real webhook
(#73). It only writes rows, in one transaction, and never waits on an agent:

1.  Insert `inbound_messages`. On conflict (a duplicate delivery), return. Otherwise set
    the phone's `channel_identities.last_inbound_at` to the row's `received_at`, before any
    routing, for every content kind (experience §5, EX-I10).
2.  Look up the phone in `channel_identities`, then the member's household and consumer
    agent. An unknown phone gets no run: once the inbound row is committed, it gets the
    `unknown_number` reply, "This number isn't registered yet." (identity-authz §5, I26),
    sent straight through the Graph API, since a message without a run has no outbox row.
    The sim route refuses unknown phones, so only the real webhook gets here.
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

Held items (experience §5, EX-I20): on every known-person path, once the path's run exists
and before its reply, `release_held` re-checks the person's held messages, drops stale ones
and sends up to 3 fresh ones on that run.

Identity (identity-authz §11, §12, I26): when `runs.start` refuses a household run because
the household agent's grant is missing, revoked or expired, no household run starts.
Instead a `chat` run with no household (exempt from the grant check, experience §3) carries
the renew text `consent_renewal` with a Renew button (`me:renew`) and
`ref = {grant, <grant_id>}`, and ends `completed` at once. It goes to the person who wrote
if they are an admin, otherwise to the household's approver, inside or outside WhatsApp's
24-hour window: the sender sends it, sends its Meta template or holds it (experience §5,
EX-I19).
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from services.api.whatsapp import templates
from services.api.whatsapp.sender import GraphSender
from services.common import ids, mcp_client, outbox, runs
from services.common.config import get_settings
from services.common.mcp_client import ToolRefused, call_tool_as
from services.common.run_context import RunContext
from services.common.trace import TraceStore
from services.identity import grants
from services.identity.errors import GrantError
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
    "CONSENT_RENEWAL",
    "HELD_CANCELLED_LINE",
    "HELD_NODE",
    "HELD_RELEASE_CAP",
    "UNKNOWN_NUMBER",
    "WHATSAPP",
    "Content",
    "ingest",
    "parse_shortfall_button",
    "release_held",
    "send_unknown_number",
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
# Identity-authz §5, §12 and I26.
UNKNOWN_NUMBER = "unknown_number"
CONSENT_RENEWAL = "consent_renewal"
# A run that only carries a code-built reply (experience §3): no household, so it is exempt
# from the grant check, and `runs.retry_target` skips it.
CODE_SOURCE = "code"
# Held items (experience §5 "Release", EX-I20).
HELD_RELEASE_CAP = 3
HELD_CANCELLED_LINE = "held_cancelled_line"
HELD_NODE = "held_release"
RELEASED = "released"
DROPPED = "dropped"
STALE = "stale"
# `TraceStore.write(..., conn=...)` never touches the pool.
_TRACES = TraceStore(None)  # type: ignore[arg-type]

SendUnknownNumber = Callable[[str], Awaitable[None]]


async def send_unknown_number(phone: str) -> None:
    """The `unknown_number` reply, sent straight through the Graph API (no run, so no outbox
    row). The person has just written, so they are inside the window. Best effort: a failed
    send is logged and not retried, and nothing else happens (fails closed)."""
    body = {"template": UNKNOWN_NUMBER, "params": {}, "buttons": []}
    try:
        settings = get_settings()
        sender = GraphSender(
            settings.whatsapp_token or "",
            settings.whatsapp_phone_number_id or "",
            delays=(0.0, 0.0),
        )
        await sender(phone, body)
    except Exception as exc:  # best effort: the inbound row is already committed
        log.warning("unknown_number reply not sent: %s", type(exc).__name__)


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


class _Unregistered:
    """`_ingest`'s result for a new message from a phone that is not registered."""


_UNREGISTERED = _Unregistered()


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
    reply_unknown: SendUnknownNumber = send_unknown_number,
) -> uuid.UUID | None:
    """Handle one inbound message in one transaction; return the run it started, if any
    (for an approval button, the approval's run).

    `kind`, `overrides` and `run_id` are passed by `POST /runs`; a WhatsApp message starts a
    `chat` run and the consumer agent classifies it. `call_tool` reaches `/mcp/household`
    for an approval button. `reply_unknown` sends the `unknown_number` reply to a phone
    that is not registered, after the inbound row is committed (a duplicate gets none).
    """
    result = await _ingest(
        conn, channel, external_id, phone, content, kind, overrides, run_id, call_tool
    )
    if isinstance(result, _Unregistered):
        await reply_unknown(phone)
        return None
    return result


async def _ingest(
    conn: AsyncConnection,
    channel: str,
    external_id: str,
    phone: str,
    content: Content,
    kind: str,
    overrides: dict[str, Any] | None,
    run_id: uuid.UUID | None,
    call_tool: CallTool,
) -> uuid.UUID | _Unregistered | None:
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
            return _UNREGISTERED

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
            started, ok = await _start(
                conn, member, inbound, household_id=member.household_id, kind=CHAT, trigger=trigger
            )
            await unscoped(conn, "ingest").execute(_SET_RUN_SQL, (started, channel, external_id))
            if not ok:
                return started
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


async def _start(
    conn: AsyncConnection,
    member: _Member,
    inbound: uuid.UUID,
    *,
    household_id: str | None,
    kind: str,
    trigger: dict[str, Any],
    run_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, bool]:
    """`(run, True)` for the run `runs.start` started. If it refused because the household
    agent's grant is missing, revoked or expired, `(run, False)` for the run that carried
    the renew reply instead (identity-authz §11, I26). `runs.start`'s own savepoint undid
    the refused start. Either way the person's held items are released on the run before
    its reply (EX-I20)."""
    try:
        started = await runs.start(
            conn, household_id=household_id, kind=kind, trigger=trigger, run_id=run_id
        )
    except GrantError as refused:
        log.info("household run refused: %s", refused.reason)
        return await _renew(conn, member, inbound, refused.reason, run_id), False
    await release_held(conn, member.user_id, started, inbound)
    return started, True


# Who is asked to renew the household agent's grant: the person who wrote if they are an
# admin, otherwise the household's approver (an admin).
_RENEW_TO_SQL = """
SELECT CASE WHEN m.role = 'admin' THEN m.user_id ELSE r.approver_user_id END
FROM household_members m
LEFT JOIN approval_rules r ON r.household_id = m.household_id
WHERE m.household_id = %s AND m.user_id = %s
"""

# The household agent's grant the refusal is about: its latest row. `grant_missing` has none.
_LATEST_GRANT_SQL = """
SELECT grant_id FROM agent_grants WHERE agent_id = %s ORDER BY created_at DESC LIMIT 1
"""


async def _renew(
    conn: AsyncConnection,
    member: _Member,
    inbound: uuid.UUID,
    reason: str,
    run_id: uuid.UUID | None,
) -> uuid.UUID:
    """The renew text `consent_renewal` (identity-authz §12, experience §6) with the Renew
    button `me:renew` and `ref = {grant, <grant_id>}`, in a `chat` run with no household
    that ends `completed` with the grant `reason`. Written inside or outside the
    recipient's window: the sender sends it, sends its Meta template or holds it
    (experience §5, EX-I19). `grant_missing` has no grant, so no `ref`: outside the window
    with no template it is skipped, not held."""
    started = await runs.start(
        conn,
        household_id=None,
        kind=CHAT,
        trigger={"source": CODE_SOURCE, "user_id": member.user_id, "inbound_id": inbound},
        run_id=run_id,
    )
    await release_held(conn, member.user_id, started, inbound)
    q = unscoped(conn, "ingest")
    cur = await q.execute(_RENEW_TO_SQL, (member.household_id, member.user_id))
    row = await cur.fetchone()
    to_user = row[0] if row is not None else None
    if to_user is not None:
        cur = await q.execute(_LATEST_GRANT_SQL, (f"household:{member.household_id}",))
        grant = await cur.fetchone()
        body: dict[str, Any] = {
            "user_id": str(to_user),
            "template": CONSENT_RENEWAL,
            "params": {},
            "buttons": [grants.RENEW_BUTTON],
        }
        if grant is not None:
            body["ref"] = {"kind": "grant", "id": str(grant[0])}
        await outbox.send(
            conn,
            message_id=ids.message_id(inbound, 0),
            run_id=started,
            conversation_id=None,
            causation_id=None,
            from_agent=API_AGENT,
            to_agent=WHATSAPP,
            type="whatsapp_out",
            body=body,
        )
    else:
        log.info("renew text not sent: no admin to send it to")
    await runs.finish(conn, started, API_AGENT, "completed", reason)
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

    started, ok = await _start(
        conn, member, inbound, household_id=household_id, kind=kind, trigger=trigger, run_id=run_id
    )
    if ok:
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
    await release_held(conn, member.user_id, run_id, inbound)
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
    started, ok = await _start(
        conn, member, inbound, household_id=member.household_id, kind=CHAT, trigger=trigger
    )
    if not ok:
        return started
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
    await release_held(conn, member.user_id, run_id, inbound)
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


# --- held items (experience §5 "Release", EX-I20) ---------------------------------------

# The person's pending held rows, oldest first. Locked, so a concurrent message from the
# same person waits and then finds them decided.
_HELD_SQL = """
SELECT held_id, message_id, body, ref_kind, ref_id FROM held_messages
WHERE user_id = %s AND status = 'pending'
ORDER BY created_at, held_id
FOR UPDATE
"""

_DECIDE_HELD_SQL = """
UPDATE held_messages SET status = %s, decided_at = now()
WHERE held_id = %s AND status = 'pending'
"""

# An approval's status, and whether its pending `approval_expiry` timer is due more than
# 5 minutes from now (§5 "Holdable kinds").
_HELD_APPROVAL_SQL = """
SELECT a.status,
       EXISTS (SELECT 1 FROM timers t
               WHERE t.kind = 'approval_expiry' AND t.status = 'pending'
                 AND t.ref->>'approval_id' = a.approval_id::text
                 AND t.due_at > now() + interval '5 minutes') AS due_later
FROM approvals a WHERE a.approval_id = %s
"""

# An order less than 72 hours old.
_HELD_ORDER_SQL = """
SELECT 1 FROM orders WHERE order_id = %s AND created_at > now() - interval '72 hours'
"""

# A grant with no newer grant row for the same agent (not replaced since).
_HELD_GRANT_SQL = """
SELECT 1 FROM agent_grants g
WHERE g.grant_id = %s
  AND NOT EXISTS (SELECT 1 FROM agent_grants n
                  WHERE n.agent_id = g.agent_id AND n.created_at > g.created_at)
"""

# The person's own decision: such an approval was not cancelled for want of an answer.
_DECIDED_BY_PERSON = frozenset({"approved", "rejected"})


async def release_held(
    conn: AsyncConnection, user_id: uuid.UUID, run_id: uuid.UUID, inbound_id: uuid.UUID
) -> None:
    """Release the person's held items on the run `run_id` (experience §5, EX-I20).

    Inside the caller's transaction: each `pending` row, oldest first, is checked again
    (§5 "Holdable kinds"). A stale one is marked `dropped`; up to `HELD_RELEASE_CAP` fresh
    ones are sent through the outbox as `system:api` with `ids.held_release_id(held_id)`
    and marked `released`; the rest stay `pending`. If an approval was dropped, one
    `held_cancelled_line` (`ids.held_cancelled_id(inbound_id)`) is sent too, with the
    oldest dropped approval's amount; it does not count toward the cap. Each decided row
    writes one `decision` trace row (`node: held_release`). A replay sends nothing again:
    the rows are decided and the message ids are fixed."""
    q = unscoped(conn, "held_release")
    async with q.cursor(row_factory=dict_row) as cur:
        await cur.execute(_HELD_SQL, (user_id,))
        rows = await cur.fetchall()
    released = 0
    cancelled_amount: str | None = None
    for row in rows:
        fresh, cancelled = await _held_check(conn, user_id, row)
        if not fresh:
            await q.execute(_DECIDE_HELD_SQL, (DROPPED, row["held_id"]))
            await _held_decision(conn, user_id, run_id, row, DROPPED, None)
            if cancelled and cancelled_amount is None:
                amount = (row["body"].get("params") or {}).get("amount")
                cancelled_amount = amount if isinstance(amount, str) and amount else None
            continue
        if released >= HELD_RELEASE_CAP:
            continue
        message_id = ids.held_release_id(row["held_id"])
        await outbox.send(
            conn,
            message_id=message_id,
            run_id=run_id,
            conversation_id=None,
            causation_id=None,
            from_agent=API_AGENT,
            to_agent=WHATSAPP,
            type="whatsapp_out",
            body=row["body"],
        )
        await q.execute(_DECIDE_HELD_SQL, (RELEASED, row["held_id"]))
        await _held_decision(conn, user_id, run_id, row, RELEASED, message_id)
        released += 1
    if cancelled_amount is not None:
        await outbox.send(
            conn,
            message_id=ids.held_cancelled_id(inbound_id),
            run_id=run_id,
            conversation_id=None,
            causation_id=None,
            from_agent=API_AGENT,
            to_agent=WHATSAPP,
            type="whatsapp_out",
            body={
                "user_id": str(user_id),
                "template": HELD_CANCELLED_LINE,
                "params": {"amount": cancelled_amount},
                "buttons": [],
            },
        )


async def _held_check(
    conn: AsyncConnection, user_id: uuid.UUID, row: dict[str, Any]
) -> tuple[bool, bool]:
    """`(fresh, cancelled)` for one held row. Anything that cannot be checked is stale
    (fails closed). `cancelled` is true for a stale approval that the person did not
    decide."""
    body, kind = row["body"], row["ref_kind"]
    if (
        not isinstance(body, dict)
        or body.get("user_id") != str(user_id)
        or not templates.holdable(str(body.get("template", "")), kind)
    ):
        return False, False
    try:
        ref_id = uuid.UUID(str(row["ref_id"]))
    except ValueError:
        return False, kind == "approval"
    q = unscoped(conn, "held_release")
    if kind == "approval":
        cur = await q.execute(_HELD_APPROVAL_SQL, (ref_id,))
        approval = await cur.fetchone()
        if approval is None:
            return False, True
        status, due_later = approval
        fresh = status == "pending" and bool(due_later)
        return fresh, not fresh and status not in _DECIDED_BY_PERSON
    sql = _HELD_ORDER_SQL if kind == "order" else _HELD_GRANT_SQL
    cur = await q.execute(sql, (ref_id,))
    return await cur.fetchone() is not None, False


async def _held_decision(
    conn: AsyncConnection,
    user_id: uuid.UUID,
    run_id: uuid.UUID,
    row: dict[str, Any],
    choice: str,
    message_id: uuid.UUID | None,
) -> None:
    """One `decision` trace row per decided held row (EX-I20)."""
    await _TRACES.write(
        run_id,
        API_AGENT,
        "decision",
        {
            "node": HELD_NODE,
            "choice": choice,
            "reason": STALE if choice == DROPPED else None,
            "explanation": "",
            "ref": str(row["message_id"]),
        },
        on_behalf_of=f"user:{user_id}",
        message_id=message_id,
        conn=conn,
    )
