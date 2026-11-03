"""The `/mcp/household` MCP server (architecture §5 and item 43; agent-protocol P25).

`services/mcp/app.py` mounts `server`. Household tools use Postgres directly and have no
adapter (item 44). Guarded tools go through `services.safety.guard.run_guarded`; the
others use `@traced`. Every tool body runs inside `meta.tool_errors()`.

*   `get_constraints(user_id)` *guarded*: that person's allergy, diet and dislike rows.
*   `create_order(order_id, kind)` *guarded*: a `draft` order for the run's household.
*   `set_order_state(order_id, expected, new)`: only to `quoting`, `negotiating` or
    `cancelled`.
*   `get_pantry()`: `items` (the sum of pantry events per item and unit), `enabled` (the
    run's household has `env = 'eval'`), `as_of` (the database's `now()`, ISO-8601 UTC) and
    `estimates` (pantry design §4; one per vocabulary item with an event when `enabled`,
    else `[]`).
*   `get_budget()`.
*   `authorize_spend(order_id, quote_id, items, total)` *guarded*: key scoped by
    `quote_id`; sets `authorized`, `orders.seller_agent_id` (from the quote), the total,
    the items hash and `order_items`.
*   `request_approval(order_id, amount)` *guarded*: a `pending` approval for the
    household's approver; sets `awaiting_approval`. In the same transaction it creates the
    `approval_expiry` timer, pauses `run_timeout` and sends the approver the
    `approval_request` WhatsApp prompt with Approve and Reject buttons (§8, #63).
*   `decide_approval(approval_id, decision, user_id)` *guarded*, `api` service token.
*   `record_delivery(order_id)` *guarded*: one pantry event per SKU of a delivered order,
    with `pantry_item` from `catalog_items` (the seller's SKU), `kind 'add'` and `run_id`.

The agents tools (agents design §9, §10; AG-A5):

*   `get_household_allergens()` *guarded*: the union of every member's allergen codes.
*   `record_dislike(user_id, value, action)` *guarded*: add or remove a learned dislike;
    seeded rows are never deleted.
*   `get_household_rules()` *guarded*: take-turns, the turn index and the approver.
*   `advance_turn(order_id)` *guarded*: once per delivered dinner, while take-turns is on.
*   `set_take_turns(order_id, on, user_id)` *guarded*, `api` service token, approver only.

The shortfall tools (agent-protocol §10 and P25, #68):

*   `open_shortfall(shortfall_id, order_id, seller, lines, due_at)` and
    `get_shortfall(shortfall_id)` (`@traced`).
*   `choose_shortfall_option(shortfall_id, line, pick, user_id)` *guarded*, `api` service
    token: the first valid pick per line wins; the last pick closes the row and sends
    `shortfall_closed {reason: all_picked}` from `system:api` in the same transaction.
*   `authorize_amendment(order_id, shortfall_id, picks)` *guarded*: the
    `limits.authorize_amendment` result `{picks, refused}`.

The household is always the run's household (`runs.household_id`), never an argument.
"""

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError

from services.common import db, dislikes, ids, orders, outbox, pantry, runs
from services.common.config import get_settings
from services.common.money import Money
from services.common.money import format as money_format
from services.common.protocol.schemas import Currency, OrderLine, ShortfallLine
from services.common.trace import TraceStore
from services.mcp.knowledge.adapters.neo4j_knowledge import AllergenCheck
from services.mcp.knowledge.server import get_knowledge
from services.mcp.meta import CallContext, tool_error, tool_errors
from services.mcp.traced import Tool, traced
from services.safety import limits
from services.safety.guard import GuardRefused, run_guarded

__all__ = [
    "SETTABLE_STATES",
    "MoneyArg",
    "advance_turn",
    "authorize_amendment",
    "authorize_spend",
    "choose_shortfall_option",
    "create_order",
    "decide_approval",
    "format_reply_by",
    "get_budget",
    "get_constraints",
    "get_household_allergens",
    "get_household_rules",
    "get_pantry",
    "get_shortfall",
    "open_shortfall",
    "record_delivery",
    "record_dislike",
    "request_approval",
    "server",
    "set_order_state",
    "set_take_turns",
]

AREA = "household"
API_AGENT = "system:api"
WHATSAPP = "channel:whatsapp"
# The approver's prompt (architecture §8); its text is in `services/api/whatsapp/sender.py`.
APPROVAL_PROMPT_TEMPLATE = "approval_request"
APPROVAL_TTL_OVERRIDE = "APPROVAL_TTL_SECONDS"

# The only states `set_order_state` may set (item 43); the others have their own tools.
SETTABLE_STATES = frozenset({"quoting", "negotiating", "cancelled"})
# `authorized` is accepted so a replayed call returns its stored result.
_AUTHORIZE_FROM = ("negotiating", "awaiting_approval")
_DECISIONS = frozenset({"approved", "rejected"})

AUTHZ_DENIED = "authz_denied"
INVALID_STATE = "invalid_state"
STATE_NOT_ALLOWED = "state_not_allowed"
QUOTE_MISMATCH = "quote_mismatch"
INVALID_ITEMS = "invalid_items"
INVALID_KIND = "invalid_kind"
INVALID_DECISION = "invalid_decision"
NO_APPROVER = "no_approver"
# `open_shortfall`: the id is not the order's shortfall, or the lines or `due_at` are invalid.
INVALID_SHORTFALL = "invalid_shortfall"
# `record_dislike`: the value fails `dislikes.check_value` (agents §9).
INVALID_VALUE = "invalid_value"
# `advance_turn`: the order is not the household's delivered dinner (agents §10).
WRONG_ORDER_STATE = "wrong_order_state"
_DISLIKE_ACTIONS = frozenset({"add", "remove"})

server = MCPServer(name=AREA)

# `TraceStore.write(..., conn=...)` never touches the pool.
_TRACES = TraceStore(None)  # type: ignore[arg-type]


class MoneyArg(BaseModel):
    """A money argument on the wire: integer minor units and an ISO 4217 code."""

    amount_minor: int
    currency: Currency

    def money(self) -> Money:
        return Money(self.amount_minor, self.currency)


class _LazyKnowledge:
    """Connects to the graph only when an allergen check needs it.

    `limits.check_allergens` turns any exception here, including a missing Neo4j
    configuration, into `knowledge_unavailable` (fails closed).
    """

    async def check_allergens(
        self, skus: Sequence[str], allergen_codes: Sequence[str]
    ) -> AllergenCheck:
        return await get_knowledge().check_allergens(skus, allergen_codes)


# --- shared lookups -------------------------------------------------------------------


async def _run_household(ctx: CallContext) -> str | None:
    async with db.get_pool().connection() as conn:
        cur = await conn.execute("SELECT household_id FROM runs WHERE run_id = %s", (ctx.run_id,))
        row = await cur.fetchone()
    return None if row is None else row[0]


def _household_relation(relation: str, household: str | None) -> tuple[str, str] | None:
    # With no household there is nothing to check; the limits then refuse `authz_denied`.
    return None if household is None else (relation, f"household:{household}")


def _require_household(household: str | None) -> str:
    if household is None:
        raise GuardRefused(AUTHZ_DENIED)
    return household


async def _order(conn: AsyncConnection, order_id: uuid.UUID, household: str) -> dict[str, Any]:
    """The order, refused like `addresses.resolve` when it is missing or another household's."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT household_id, kind, state, currency FROM orders WHERE order_id = %s",
            (order_id,),
        )
        row = await cur.fetchone()
    if row is None or row["household_id"] != household:
        raise GuardRefused(AUTHZ_DENIED)
    return row


async def _member_allergens(conn: AsyncConnection, household: str) -> dict[str, list[str]]:
    cur = await conn.execute(
        "SELECT m.user_id, c.value FROM household_members m"
        " JOIN member_constraints c ON c.user_id = m.user_id AND c.kind = 'allergy'"
        " WHERE m.household_id = %s ORDER BY m.user_id, c.value",
        (household,),
    )
    allergens: dict[str, list[str]] = {}
    for user_id, code in await cur.fetchall():
        allergens.setdefault(str(user_id), []).append(code)
    return allergens


def _money(value: MoneyArg | Mapping[str, Any]) -> Money:
    return (value if isinstance(value, MoneyArg) else MoneyArg.model_validate(value)).money()


def _qty(value: Decimal) -> int | str:
    return int(value) if value == value.to_integral_value() else format(value.normalize(), "f")


# --- guarded tools --------------------------------------------------------------------


async def get_constraints(ctx: CallContext, *, user_id: uuid.UUID) -> dict[str, Any]:
    """The person's own constraint rows. Relation `agent acts_for user:<user_id>`."""

    async def call() -> dict[str, Any]:
        async with db.get_pool().connection() as conn:
            cur = await conn.execute(
                "SELECT kind, value FROM member_constraints WHERE user_id = %s"
                " ORDER BY kind, value",
                (user_id,),
            )
            rows = await cur.fetchall()
        by_kind: dict[str, list[str]] = {"allergy": [], "diet": [], "dislike": []}
        for kind, value in rows:
            by_kind[kind].append(value)
        return {
            "user_id": str(user_id),
            "allergies": by_kind["allergy"],
            "diets": by_kind["diet"],
            "dislikes": by_kind["dislike"],
        }

    return await run_guarded(
        ctx,
        tool=f"{AREA}.get_constraints",
        relation=("acts_for", f"user:{user_id}"),
        limits=None,
        key=None,
        request={"user_id": str(user_id)},
        call=call,
        apply=None,
    )


async def create_order(ctx: CallContext, *, order_id: uuid.UUID, kind: str) -> dict[str, Any]:
    """A `draft` order for the run's household, in its currency and at its address."""
    household = await _run_household(ctx)

    async def check(conn: AsyncConnection) -> None:
        h = _require_household(household)
        if kind not in ("grocery", "dinner"):
            raise GuardRefused(INVALID_KIND)
        cur = await conn.execute("SELECT household_id FROM orders WHERE order_id = %s", (order_id,))
        row = await cur.fetchone()
        if row is not None and row[0] != h:
            raise GuardRefused(AUTHZ_DENIED)

    async def call() -> dict[str, Any]:
        return {"order_id": str(order_id), "kind": kind, "state": "draft"}

    async def apply(conn: AsyncConnection, result: Any) -> None:
        cur = await conn.execute(
            "INSERT INTO orders (order_id, run_id, household_id, kind, currency, address_ref)"
            " SELECT %s, %s, h.household_id, %s, h.currency, a.address_ref"
            " FROM households h JOIN household_addresses a ON a.household_id = h.household_id"
            " WHERE h.household_id = %s ORDER BY a.created_at, a.address_ref LIMIT 1"
            " ON CONFLICT (order_id) DO NOTHING",
            (order_id, ctx.run_id, kind, household),
        )
        if cur.rowcount == 0:
            cur = await conn.execute("SELECT 1 FROM orders WHERE order_id = %s", (order_id,))
            if await cur.fetchone() is None:
                raise LookupError(f"household {household} has no address")

    return await run_guarded(
        ctx,
        tool=f"{AREA}.create_order",
        relation=_household_relation("acts_for", household),
        limits=check,
        key=ids.idempotency_key(order_id, "create_order", ctx.agent_id),
        request={"order_id": str(order_id), "kind": kind},
        call=call,
        apply=apply,
    )


async def authorize_spend(
    ctx: CallContext,
    *,
    order_id: uuid.UUID,
    quote_id: uuid.UUID,
    items: Sequence[OrderLine | Mapping[str, Any]],
    total: MoneyArg | Mapping[str, Any],
) -> dict[str, Any]:
    """Authorize the chosen quote. Relation `agent can_spend budget:<household>`.

    The key is scoped by `quote_id` (A16), so a re-quoted offer gets a new key. The quote
    must belong to the order and have this total; its seller becomes
    `orders.seller_agent_id`. The limits run in the §5 order (`limits.authorize_spend`).
    """
    household = await _run_household(ctx)
    lines = [OrderLine.model_validate(i) for i in items]
    amount = _money(total)
    hash_ = orders.items_hash(lines)
    seller: list[str] = []

    async def check(conn: AsyncConnection) -> None:
        h = _require_household(household)
        order = await _order(conn, order_id, h)
        if order["state"] not in (*_AUTHORIZE_FROM, "authorized"):
            raise GuardRefused(INVALID_STATE)
        if not lines or len({line.line for line in lines}) != len(lines):
            raise GuardRefused(INVALID_ITEMS)
        cur = await conn.execute(
            "SELECT seller_agent_id, total_minor, currency FROM quotes"
            " WHERE quote_id = %s AND order_id = %s",
            (quote_id, order_id),
        )
        quote = await cur.fetchone()
        if quote is None or (int(quote[1]), str(quote[2]).strip()) != (
            amount.amount_minor,
            amount.currency,
        ):
            raise GuardRefused(QUOTE_MISMATCH)
        await limits.authorize_spend(
            conn,
            household_id=h,
            order_id=order_id,
            items=lines,
            total=amount,
            member_allergens=await _member_allergens(conn, h),
            knowledge=_LazyKnowledge(),
        )
        seller.append(quote[0])

    async def call() -> dict[str, Any]:
        return {
            "order_id": str(order_id),
            "quote_id": str(quote_id),
            "state": "authorized",
            "seller_agent_id": seller[0],
            "total_minor": amount.amount_minor,
            "currency": amount.currency,
            "items_hash": hash_,
        }

    async def apply(conn: AsyncConnection, result: Any) -> None:
        moved = await orders.transition(
            conn,
            order_id,
            _AUTHORIZE_FROM,
            "authorized",
            seller_agent_id=seller[0],
            total_minor=amount.amount_minor,
            items_hash=hash_,
        )
        if not moved:
            raise RuntimeError(f"order {order_id} is no longer awaiting authorization")
        await conn.execute("DELETE FROM order_items WHERE order_id = %s", (order_id,))
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO order_items"
                " (order_id, line, sku, name, qty, unit_minor, substituted_for)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [
                    (order_id, i.line, i.sku, i.name, i.qty, i.unit_minor, i.substituted_for)
                    for i in lines
                ],
            )

    return await run_guarded(
        ctx,
        tool=f"{AREA}.authorize_spend",
        relation=None if household is None else ("can_spend", f"budget:{household}"),
        limits=check,
        key=ids.idempotency_key(order_id, "authorize", ctx.agent_id, scope=quote_id),
        request={
            "order_id": str(order_id),
            "quote_id": str(quote_id),
            "items": [i.model_dump(mode="json") for i in lines],
            "total": {"amount_minor": amount.amount_minor, "currency": amount.currency},
        },
        call=call,
        apply=apply,
    )


def _approval_ttl_seconds(overrides: Any) -> int:
    """The run override `APPROVAL_TTL_SECONDS` if it is a positive integer, else the
    setting (architecture item 36)."""
    value = overrides.get(APPROVAL_TTL_OVERRIDE) if isinstance(overrides, Mapping) else None
    try:
        seconds = int(value) if value is not None and not isinstance(value, bool) else 0
    except (TypeError, ValueError):
        seconds = 0
    return seconds if seconds > 0 else get_settings().approval_ttl_seconds


_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def format_reply_by(due_at: datetime, now: datetime, tz: ZoneInfo) -> str:
    """The `approval_request` reply-by time in the household's time zone (experience
    EX-I2): "6:30 pm", or "Sat 6:30 pm" when `due_at` is not on today's date there."""
    local = due_at.astimezone(tz)
    hour = local.hour % 12 or 12
    text = f"{hour}:{local.minute:02d} {'am' if local.hour < 12 else 'pm'}"
    if local.date() != now.astimezone(tz).date():
        text = f"{_WEEKDAYS[local.weekday()]} {text}"
    return text


async def _start_approval_wait(
    conn: AsyncConnection,
    ctx: CallContext,
    approval_id: uuid.UUID,
    order_id: uuid.UUID,
    approver: str,
    money: Money,
) -> None:
    """The rest of §8 "request_approval", in the tool's transaction: the `approval_expiry`
    timer, `run_timeout` paused, and the approver's WhatsApp prompt with two buttons."""
    cur = await conn.execute(
        "SELECT r.trigger->'overrides', h.timezone FROM runs r"
        " JOIN households h ON h.household_id = r.household_id WHERE r.run_id = %s",
        (ctx.run_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError(f"run {ctx.run_id} has no household")
    overrides, timezone = row
    cur = await conn.execute(
        "INSERT INTO timers (kind, run_id, due_at, ref)"
        " VALUES ('approval_expiry', %s, now() + make_interval(secs => %s::float8), %s)"
        " RETURNING due_at, now()",
        (
            ctx.run_id,
            _approval_ttl_seconds(overrides),
            Jsonb({"approval_id": str(approval_id), "order_id": str(order_id)}),
        ),
    )
    timer = await cur.fetchone()
    if timer is None:
        raise RuntimeError("the approval_expiry timer was not created")
    due_at, now = timer
    reply_by = format_reply_by(due_at, now, ZoneInfo(timezone))
    await runs.pause_timeout(conn, ctx.run_id)
    await outbox.send(
        conn,
        message_id=ids.approval_prompt_id(approval_id),
        run_id=ctx.run_id,
        conversation_id=None,
        causation_id=None,
        from_agent=ctx.agent_id,
        to_agent=WHATSAPP,
        type="whatsapp_out",
        body={
            "user_id": approver,
            "template": APPROVAL_PROMPT_TEMPLATE,
            "params": {"amount": money_format(money, "en"), "reply_by": reply_by},
            "buttons": [
                {"payload": ids.approval_button(approval_id, "approved"), "title": "Approve"},
                {"payload": ids.approval_button(approval_id, "rejected"), "title": "Reject"},
            ],
            "ref": {"kind": "approval", "id": str(approval_id)},
        },
    )


async def request_approval(
    ctx: CallContext, *, order_id: uuid.UUID, amount: MoneyArg | Mapping[str, Any]
) -> dict[str, Any]:
    """A `pending` approval for the household's approver; the order goes `awaiting_approval`."""
    household = await _run_household(ctx)
    money = _money(amount)
    approval_id = ids.approval_id(order_id)
    approver: list[str] = []

    async def check(conn: AsyncConnection) -> None:
        h = _require_household(household)
        order = await _order(conn, order_id, h)
        if order["state"] not in ("negotiating", "awaiting_approval"):
            raise GuardRefused(INVALID_STATE)
        await limits.check_currency(conn, order_id, money)
        cur = await conn.execute(
            "SELECT approver_user_id FROM approval_rules WHERE household_id = %s", (h,)
        )
        row = await cur.fetchone()
        if row is None:
            raise GuardRefused(NO_APPROVER)
        approver.append(str(row[0]))

    async def call() -> dict[str, Any]:
        return {
            "approval_id": str(approval_id),
            "order_id": str(order_id),
            "status": "pending",
            "amount_minor": money.amount_minor,
            "currency": money.currency,
            "approver_user_id": approver[0],
        }

    async def apply(conn: AsyncConnection, result: Any) -> None:
        await conn.execute(
            "INSERT INTO approvals"
            " (approval_id, run_id, household_id, order_id, amount_minor, currency)"
            " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (approval_id) DO NOTHING",
            (approval_id, ctx.run_id, household, order_id, money.amount_minor, money.currency),
        )
        if not await orders.transition(conn, order_id, "negotiating", "awaiting_approval"):
            raise RuntimeError(f"order {order_id} is no longer negotiating")
        await _start_approval_wait(conn, ctx, approval_id, order_id, approver[0], money)
        await _TRACES.write(
            ctx.run_id,
            ctx.agent_id,
            "approval",
            {
                "approval_id": str(approval_id),
                "status": "pending",
                "amount_minor": money.amount_minor,
                "currency": money.currency,
                "user_id": approver[0],
            },
            on_behalf_of=ctx.on_behalf_of,
            conversation_id=ctx.conversation_id,
            conn=conn,
        )

    return await run_guarded(
        ctx,
        tool=f"{AREA}.request_approval",
        relation=_household_relation("acts_for", household),
        limits=check,
        key=ids.idempotency_key(order_id, "approval", ctx.agent_id),
        request={
            "order_id": str(order_id),
            "amount": {"amount_minor": money.amount_minor, "currency": money.currency},
        },
        call=call,
        apply=apply,
    )


async def decide_approval(
    ctx: CallContext, *, approval_id: uuid.UUID, decision: str, user_id: uuid.UUID
) -> dict[str, Any]:
    """The approver's decision, from `api` with its service token.

    `user_id` must be `approval_rules.approver_user_id`. The update is conditional on
    `pending`: the result's `outcome` is `decided`, or `already_decided` with the current
    status (for example `expired` by the sweeper).
    """

    async def check(conn: AsyncConnection) -> None:
        if ctx.agent_id != API_AGENT:
            raise GuardRefused(AUTHZ_DENIED)
        if decision not in _DECISIONS:
            raise GuardRefused(INVALID_DECISION)
        cur = await conn.execute(
            "SELECT household_id FROM approvals WHERE approval_id = %s", (approval_id,)
        )
        row = await cur.fetchone()
        if row is None:
            raise GuardRefused(AUTHZ_DENIED)
        await limits.check_approver(conn, row[0], user_id)

    async def call() -> dict[str, Any]:
        async with db.get_pool().connection() as conn, conn.transaction():
            cur = await conn.execute(
                "UPDATE approvals SET status = %s, decided_at = now(), decided_by = %s"
                " WHERE approval_id = %s AND status = 'pending'"
                " RETURNING order_id, amount_minor, currency",
                (decision, user_id, approval_id),
            )
            row = await cur.fetchone()
            if row is not None:
                order_id, amount_minor, currency = row
                await _TRACES.write(
                    ctx.run_id,
                    ctx.agent_id,
                    "approval",
                    {
                        "approval_id": str(approval_id),
                        "status": decision,
                        "amount_minor": int(amount_minor),
                        "currency": str(currency).strip(),
                        "user_id": str(user_id),
                    },
                    on_behalf_of=ctx.on_behalf_of,
                    conversation_id=ctx.conversation_id,
                    conn=conn,
                )
                outcome, status = "decided", decision
            else:
                cur = await conn.execute(
                    "SELECT order_id, status FROM approvals WHERE approval_id = %s",
                    (approval_id,),
                )
                order_id, status = await cur.fetchone() or (None, None)
                outcome = "already_decided"
        return {
            "approval_id": str(approval_id),
            "order_id": str(order_id),
            "outcome": outcome,
            "status": status,
        }

    return await run_guarded(
        ctx,
        tool=f"{AREA}.decide_approval",
        relation=None,
        limits=check,
        key=None,
        request={
            "approval_id": str(approval_id),
            "decision": decision,
            "user_id": str(user_id),
        },
        call=call,
        apply=None,
    )


async def record_delivery(ctx: CallContext, *, order_id: uuid.UUID) -> dict[str, Any]:
    """One pantry event per SKU of a delivered weekly order; once per `(order_id, item)`."""
    household = await _run_household(ctx)

    async def check(conn: AsyncConnection) -> None:
        h = _require_household(household)
        order = await _order(conn, order_id, h)
        if (order["state"], order["kind"]) != ("delivered", "grocery"):
            raise GuardRefused(INVALID_STATE)

    async def call() -> dict[str, Any]:
        async with db.get_pool().connection() as conn, conn.transaction():
            cur = await conn.execute(
                "INSERT INTO pantry_events"
                " (household_id, item, pantry_item, kind, delta, unit, source, order_id, run_id)"
                " SELECT %s, oi.sku, ci.pantry_item, 'add', sum(oi.qty), 'each', 'delivery',"
                " oi.order_id, %s FROM order_items oi"
                " JOIN orders o ON o.order_id = oi.order_id"
                " LEFT JOIN catalog_items ci"
                " ON ci.agent_id = o.seller_agent_id AND ci.sku = oi.sku"
                " WHERE oi.order_id = %s GROUP BY oi.order_id, oi.sku, ci.pantry_item"
                " ON CONFLICT (order_id, item) WHERE order_id IS NOT NULL DO NOTHING",
                (household, ctx.run_id, order_id),
            )
        return {"order_id": str(order_id), "recorded": cur.rowcount}

    return await run_guarded(
        ctx,
        tool=f"{AREA}.record_delivery",
        relation=_household_relation("acts_for", household),
        limits=check,
        key=None,
        request={"order_id": str(order_id)},
        call=call,
        apply=None,
    )


async def get_household_allergens(ctx: CallContext) -> dict[str, Any]:
    """The sorted union of every member's allergen codes and nothing else (agents §6, §9).

    Relation `agent acts_for household:<run household>`; read only.
    """
    household = await _run_household(ctx)

    async def check(conn: AsyncConnection) -> None:
        _require_household(household)

    async def call() -> dict[str, Any]:
        async with db.get_pool().connection() as conn:
            by_member = await _member_allergens(conn, _require_household(household))
        return {"allergens": sorted({code for codes in by_member.values() for code in codes})}

    return await run_guarded(
        ctx,
        tool=f"{AREA}.get_household_allergens",
        relation=_household_relation("acts_for", household),
        limits=check,
        key=None,
        request={},
        call=call,
        apply=None,
    )


async def record_dislike(
    ctx: CallContext, *, user_id: uuid.UUID, value: str, action: str
) -> dict[str, Any]:
    """Add or remove the person's learned dislike (agents §9).

    Relation `agent acts_for user:<user_id>`. The value is checked again with
    `dislikes.check_value`; a bad one is refused with `invalid_value`. `add` returns
    `already_present` when any dislike row with the value exists, whatever its source,
    else `added`. `remove` deletes only the `learned` row: `removed`, or `not_learned`.
    Both branches are safe to repeat, so there is no key.
    """
    normalized = dislikes.check_value(value)

    async def check(conn: AsyncConnection) -> None:
        if normalized is None or action not in _DISLIKE_ACTIONS:
            raise GuardRefused(INVALID_VALUE)

    async def call() -> dict[str, Any]:
        async with db.get_pool().connection() as conn, conn.transaction():
            if action == "add":
                cur = await conn.execute(
                    "SELECT 1 FROM member_constraints"
                    " WHERE user_id = %s AND kind = 'dislike' AND lower(value) = %s",
                    (user_id, normalized),
                )
                if await cur.fetchone() is not None:
                    outcome = "already_present"
                else:
                    cur = await conn.execute(
                        "INSERT INTO member_constraints (user_id, kind, value, source)"
                        " VALUES (%s, 'dislike', %s, 'learned')"
                        " ON CONFLICT (user_id, value) WHERE source = 'learned' DO NOTHING",
                        (user_id, normalized),
                    )
                    outcome = "added" if cur.rowcount == 1 else "already_present"
            else:
                cur = await conn.execute(
                    "DELETE FROM member_constraints"
                    " WHERE user_id = %s AND value = %s AND source = 'learned'",
                    (user_id, normalized),
                )
                outcome = "removed" if cur.rowcount > 0 else "not_learned"
        return {"user_id": str(user_id), "value": normalized, "outcome": outcome}

    return await run_guarded(
        ctx,
        tool=f"{AREA}.record_dislike",
        relation=("acts_for", f"user:{user_id}"),
        limits=check,
        key=None,
        request={"user_id": str(user_id), "value": value, "action": action},
        call=call,
        apply=None,
    )


async def get_household_rules(ctx: CallContext) -> dict[str, Any]:
    """Take-turns, the turn index and the approver (agents §10).

    Relation `agent acts_for household:<run household>`; read only. With no
    `household_rules` row, take-turns is off and the index is 0.
    """
    household = await _run_household(ctx)

    async def check(conn: AsyncConnection) -> None:
        _require_household(household)

    async def call() -> dict[str, Any]:
        async with db.get_pool().connection() as conn:
            cur = await conn.execute(
                "SELECT take_turns, turn_index FROM household_rules WHERE household_id = %s",
                (household,),
            )
            rules = await cur.fetchone()
            cur = await conn.execute(
                "SELECT approver_user_id FROM approval_rules WHERE household_id = %s",
                (household,),
            )
            approver = await cur.fetchone()
        take_turns, turn_index = rules if rules is not None else (False, 0)
        return {
            "take_turns": bool(take_turns),
            "turn_index": int(turn_index),
            "approver_user_id": None if approver is None else str(approver[0]),
        }

    return await run_guarded(
        ctx,
        tool=f"{AREA}.get_household_rules",
        relation=_household_relation("acts_for", household),
        limits=check,
        key=None,
        request={},
        call=call,
        apply=None,
    )


async def advance_turn(ctx: CallContext, *, order_id: uuid.UUID) -> dict[str, Any]:
    """Move the turn on once for a delivered household dinner (agents §10).

    Relation `agent acts_for household:<run household>`. The order must be the
    household's `delivered` `dinner`, else `wrong_order_state`. One conditional update
    advances only while take-turns is on and only once per order, so a replay changes
    nothing and there is no key.
    """
    household = await _run_household(ctx)

    async def check(conn: AsyncConnection) -> None:
        h = _require_household(household)
        cur = await conn.execute(
            "SELECT household_id, kind, state FROM orders WHERE order_id = %s", (order_id,)
        )
        row = await cur.fetchone()
        if row is None or tuple(row) != (h, "dinner", "delivered"):
            raise GuardRefused(WRONG_ORDER_STATE)

    async def call() -> dict[str, Any]:
        async with db.get_pool().connection() as conn, conn.transaction():
            cur = await conn.execute(
                "UPDATE household_rules SET turn_index = turn_index + 1,"
                " last_turn_order_id = %s, updated_at = now()"
                " WHERE household_id = %s AND take_turns"
                " AND last_turn_order_id IS DISTINCT FROM %s",
                (order_id, household, order_id),
            )
        return {"advanced": cur.rowcount == 1}

    return await run_guarded(
        ctx,
        tool=f"{AREA}.advance_turn",
        relation=_household_relation("acts_for", household),
        limits=check,
        key=None,
        request={"order_id": str(order_id)},
        call=call,
        apply=None,
    )


async def set_take_turns(
    ctx: CallContext, *, order_id: uuid.UUID, on: bool, user_id: uuid.UUID
) -> dict[str, Any]:
    """The approver's take-turns tap, from `api` with its service token (agents §10).

    Built like `decide_approval`: no relation; the caller must be `system:api`, the
    order must exist (else `authz_denied`) and `user_id` must be its household's
    approver (else `wrong_approver`). `changed` says whether take-turns changed; writing
    the same value again is harmless, so there is no key.
    """
    household: list[str] = []

    async def check(conn: AsyncConnection) -> None:
        if ctx.agent_id != API_AGENT:
            raise GuardRefused(AUTHZ_DENIED)
        cur = await conn.execute("SELECT household_id FROM orders WHERE order_id = %s", (order_id,))
        row = await cur.fetchone()
        if row is None:
            raise GuardRefused(AUTHZ_DENIED)
        await limits.check_approver(conn, row[0], user_id)
        household.append(row[0])

    async def call() -> dict[str, Any]:
        async with db.get_pool().connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT take_turns FROM household_rules WHERE household_id = %s FOR UPDATE",
                (household[0],),
            )
            before = await cur.fetchone()
            await conn.execute(
                "INSERT INTO household_rules (household_id, take_turns, set_by)"
                " VALUES (%s, %s, %s) ON CONFLICT (household_id) DO UPDATE"
                " SET take_turns = EXCLUDED.take_turns, set_by = EXCLUDED.set_by,"
                " updated_at = now()",
                (household[0], on, user_id),
            )
        was_on = False if before is None else bool(before[0])
        return {"changed": was_on != on, "take_turns": on}

    return await run_guarded(
        ctx,
        tool=f"{AREA}.set_take_turns",
        relation=None,
        limits=check,
        key=None,
        request={"order_id": str(order_id), "on": on, "user_id": str(user_id)},
        call=call,
        apply=None,
    )


def _notice_pick_allowed(lines: Sequence[Any], line: int, pick: str) -> bool:
    """Whether `pick` is `drop` or one of `line`'s substitute SKUs in the notice."""
    for notice in lines or []:
        if int(notice["line"]) == line:
            return pick == limits.DROP or pick in {
                str(s["sku"]) for s in notice.get("substitutes") or []
            }
    return False


async def choose_shortfall_option(
    ctx: CallContext, *, shortfall_id: uuid.UUID, line: int, pick: str, user_id: uuid.UUID
) -> dict[str, Any]:
    """A member's pick for one shortfall line, from `api` with its service token (§10).

    `user_id` must be a member of the order's household and `pick` one of the line's
    substitute SKUs or `drop`. One conditional update sets the pick only while the row is
    `pending` and the line has none, so the first valid pick per line wins. When that
    pick completes every line, the same transaction closes the row and sends
    `shortfall_closed {reason: all_picked}` from `system:api`. The result's `outcome` is
    `chosen` or `already_decided`.
    """

    async def check(conn: AsyncConnection) -> None:
        if ctx.agent_id != API_AGENT:
            raise GuardRefused(AUTHZ_DENIED)
        cur = await conn.execute(
            "SELECT s.lines, o.household_id FROM shortfalls s"
            " JOIN orders o ON o.order_id = s.order_id WHERE s.shortfall_id = %s",
            (shortfall_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise limits.ShortfallNotFound()
        lines, household = row
        cur = await conn.execute(
            "SELECT 1 FROM household_members WHERE household_id = %s AND user_id = %s",
            (household, user_id),
        )
        if await cur.fetchone() is None:
            raise GuardRefused(AUTHZ_DENIED)
        if not _notice_pick_allowed(lines, line, pick):
            raise limits.PickNotInNotice(line)

    async def call() -> dict[str, Any]:
        async with db.get_pool().connection() as conn, conn.transaction():
            cur = await conn.execute(
                "UPDATE shortfalls SET picks = picks || jsonb_build_object(%s::text, %s::text)"
                " WHERE shortfall_id = %s AND status = 'pending' AND NOT (picks ? %s)"
                " RETURNING run_id, order_id, lines, picks",
                (str(line), pick, shortfall_id, str(line)),
            )
            row = await cur.fetchone()
            if row is None:
                cur = await conn.execute(
                    "SELECT status FROM shortfalls WHERE shortfall_id = %s", (shortfall_id,)
                )
                (status,) = await cur.fetchone() or (None,)
                return {
                    "shortfall_id": str(shortfall_id),
                    "line": line,
                    "pick": pick,
                    "outcome": "already_decided",
                    "status": status,
                }
            run_id, order_id, lines, picks = row
            status = "pending"
            if all(str(int(n["line"])) in picks for n in lines):
                cur = await conn.execute(
                    "UPDATE shortfalls SET status = 'closed', closed_at = now()"
                    " WHERE shortfall_id = %s AND status = 'pending'",
                    (shortfall_id,),
                )
                if cur.rowcount == 1:
                    status = "closed"
                    cur = await conn.execute(
                        "SELECT household_id FROM orders WHERE order_id = %s", (order_id,)
                    )
                    (household,) = await cur.fetchone() or (None,)
                    await outbox.send(
                        conn,
                        message_id=ids.shortfall_closed_id(shortfall_id),
                        run_id=run_id,
                        conversation_id=ids.shortfall_conversation_id(shortfall_id),
                        causation_id=None,
                        from_agent=API_AGENT,
                        to_agent=f"household:{household}",
                        type="shortfall_closed",
                        body={
                            "shortfall_id": str(shortfall_id),
                            "order_id": str(order_id),
                            "reason": "all_picked",
                        },
                    )
        return {
            "shortfall_id": str(shortfall_id),
            "line": line,
            "pick": pick,
            "outcome": "chosen",
            "status": status,
        }

    return await run_guarded(
        ctx,
        tool=f"{AREA}.choose_shortfall_option",
        relation=None,
        limits=check,
        key=None,
        request={
            "shortfall_id": str(shortfall_id),
            "line": line,
            "pick": pick,
            "user_id": str(user_id),
        },
        call=call,
        apply=None,
    )


async def authorize_amendment(
    ctx: CallContext,
    *,
    order_id: uuid.UUID,
    shortfall_id: uuid.UUID,
    picks: Mapping[Any, str],
) -> dict[str, Any]:
    """Check the closed shortfall's picks (§10 "Applying"); returns `{picks, refused}`.

    Relation `agent acts_for household:<run household>`; key
    `idempotency_key(order_id, "authorize_amendment", <agent>, scope=shortfall_id)`, the
    key `amend_order` reads. The limits are `limits.authorize_amendment`: each refused
    substitute becomes a drop in the result.
    """
    household = await _run_household(ctx)
    given = {str(int(line)): str(pick) for line, pick in picks.items()}
    result: list[dict[str, Any]] = []

    async def check(conn: AsyncConnection) -> None:
        h = _require_household(household)
        await _order(conn, order_id, h)
        result.append(
            await limits.authorize_amendment(
                conn,
                order_id=order_id,
                shortfall_id=shortfall_id,
                picks=given,
                member_allergens=await _member_allergens(conn, h),
                knowledge=_LazyKnowledge(),
            )
        )

    async def call() -> dict[str, Any]:
        return {"order_id": str(order_id), "shortfall_id": str(shortfall_id), **result[0]}

    return await run_guarded(
        ctx,
        tool=f"{AREA}.authorize_amendment",
        relation=_household_relation("acts_for", household),
        limits=check,
        key=ids.idempotency_key(order_id, "authorize_amendment", ctx.agent_id, scope=shortfall_id),
        request={
            "order_id": str(order_id),
            "shortfall_id": str(shortfall_id),
            "picks": given,
        },
        call=call,
        apply=None,
    )


# --- unguarded tools ------------------------------------------------------------------


@traced(AREA)
async def set_order_state(
    ctx: CallContext, *, order_id: str, expected: str | list[str], new: str
) -> dict[str, Any]:
    """Move the run household's order to `quoting`, `negotiating` or `cancelled`.

    Conditional on `expected`; `changed` says whether it moved. Any other target state, or
    a pair not in `orders.ALLOWED`, is refused with `state_not_allowed`.
    """
    if new not in SETTABLE_STATES:
        raise GuardRefused(STATE_NOT_ALLOWED)
    try:
        order_uuid = uuid.UUID(order_id)
    except ValueError:
        raise GuardRefused(AUTHZ_DENIED) from None
    household = _require_household(await _run_household(ctx))
    async with db.get_pool().connection() as conn, conn.transaction():
        await _order(conn, order_uuid, household)
        try:
            changed = await orders.transition(conn, order_uuid, expected, new)
        except ValueError:
            raise GuardRefused(STATE_NOT_ALLOWED) from None
        cur = await conn.execute("SELECT state FROM orders WHERE order_id = %s", (order_uuid,))
        (state,) = await cur.fetchone() or (None,)
    return {"order_id": order_id, "state": state, "changed": changed}


@traced(AREA)
async def get_pantry(ctx: CallContext) -> dict[str, Any]:
    """What the run's household has: the sum of its pantry events per item and unit, and,
    on eval households only, the estimate per vocabulary item (pantry design §4).

    `enabled` is computed here and nowhere else. When it is set and the vocabulary fails to
    load, this raises, and the agent records `pantry_unavailable`.
    """
    household = _require_household(await _run_household(ctx))
    enabled = pantry.is_enabled(ctx.env)
    vocab = pantry.load_vocabulary() if enabled else None
    async with db.get_pool().connection() as conn:
        cur = await conn.execute(
            "SELECT item, unit, sum(delta) FROM pantry_events WHERE household_id = %s"
            # COLLATE "C": the order is byte order on every database, whatever its locale.
            " GROUP BY item, unit HAVING sum(delta) > 0"
            ' ORDER BY item COLLATE "C", unit COLLATE "C"',
            (household,),
        )
        rows = await cur.fetchall()
        cur = await conn.execute("SELECT now()")
        row = await cur.fetchone()
        assert row is not None
        as_of: datetime = row[0]
        latest: list[dict[str, Any]] = []
        if vocab is not None:
            async with conn.cursor(row_factory=dict_row) as dcur:
                await dcur.execute(
                    "SELECT DISTINCT ON (pantry_item) pantry_item, event_id, kind, ts"
                    " FROM pantry_events WHERE household_id = %s AND pantry_item IS NOT NULL"
                    " ORDER BY pantry_item, event_id DESC",
                    (household,),
                )
                latest = await dcur.fetchall()
    estimates = (
        [
            asdict(pantry.estimate(event, vocab.window_days[event["pantry_item"]], as_of))
            for event in latest
            if event["pantry_item"] in vocab.window_days
        ]
        if vocab is not None
        else []
    )
    return {
        "household_id": household,
        "items": [{"item": item, "qty": _qty(qty), "unit": unit} for item, unit, qty in rows],
        "enabled": enabled,
        "as_of": as_of.astimezone(UTC).isoformat(),
        "estimates": estimates,
    }


@traced(AREA)
async def get_budget(ctx: CallContext) -> dict[str, Any]:
    """The run household's weekly budget and what remains of it this week."""
    household = _require_household(await _run_household(ctx))
    async with db.get_pool().connection() as conn:
        cur = await conn.execute(
            "SELECT amount_minor, currency FROM budgets"
            " WHERE household_id = %s AND period = 'weekly'",
            (household,),
        )
        row = await cur.fetchone()
        if row is None:
            raise LookupError(f"no weekly budget for household {household}")
        remaining = await orders.remaining_weekly_budget(conn, household)
    return {
        "household_id": household,
        "weekly": {"amount_minor": int(row[0]), "currency": str(row[1]).strip()},
        "remaining": {"amount_minor": remaining.amount_minor, "currency": remaining.currency},
    }


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (TypeError, ValueError):
        raise GuardRefused(AUTHZ_DENIED) from None


def _shortfall_lines(lines: Sequence[Any]) -> list[dict[str, Any]]:
    """The notice lines as stored, or `invalid_shortfall` (none, duplicates or bad shape)."""
    try:
        parsed = [ShortfallLine.model_validate(line) for line in lines]
    except ValidationError:
        raise GuardRefused(INVALID_SHORTFALL) from None
    if not parsed or len({line.line for line in parsed}) != len(parsed):
        raise GuardRefused(INVALID_SHORTFALL)
    return [line.model_dump(mode="json") for line in parsed]


def _aware(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise GuardRefused(INVALID_SHORTFALL) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GuardRefused(INVALID_SHORTFALL)
    return parsed


@traced(AREA)
async def open_shortfall(
    ctx: CallContext,
    *,
    shortfall_id: str,
    order_id: str,
    seller: str,
    lines: list[dict[str, Any]],
    due_at: str,
) -> dict[str, Any]:
    """Insert the `pending` shortfall row from a `shortfall_notice` (§10); once per id.

    The order must be the run household's, `shortfall_id` its shortfall
    (`ids.shortfall_id`) and `seller` its `seller_agent_id`. A replay inserts nothing;
    `opened` says whether this call inserted the row.
    """
    sf_uuid, order_uuid = _uuid(shortfall_id), _uuid(order_id)
    household = _require_household(await _run_household(ctx))
    stored = _shortfall_lines(lines)
    due = _aware(due_at)
    if sf_uuid != ids.shortfall_id(order_uuid):
        raise GuardRefused(INVALID_SHORTFALL)
    async with db.get_pool().connection() as conn, conn.transaction():
        await _order(conn, order_uuid, household)
        cur = await conn.execute(
            "SELECT seller_agent_id FROM orders WHERE order_id = %s", (order_uuid,)
        )
        (order_seller,) = await cur.fetchone() or (None,)
        if order_seller is None or order_seller != seller:
            raise GuardRefused(AUTHZ_DENIED)
        cur = await conn.execute(
            "INSERT INTO shortfalls (shortfall_id, run_id, order_id, seller_agent_id, lines,"
            " due_at) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (sf_uuid, ctx.run_id, order_uuid, seller, Jsonb(stored), due),
        )
        opened = cur.rowcount == 1
        cur = await conn.execute(
            "SELECT status FROM shortfalls WHERE shortfall_id = %s", (sf_uuid,)
        )
        (status,) = await cur.fetchone() or (None,)
    return {
        "shortfall_id": shortfall_id,
        "order_id": order_id,
        "status": status,
        "opened": opened,
    }


@traced(AREA)
async def get_shortfall(ctx: CallContext, *, shortfall_id: str) -> dict[str, Any]:
    """The run household's shortfall row: lines, picks (`line` → SKU or `drop`) and status."""
    sf_uuid = _uuid(shortfall_id)
    household = _require_household(await _run_household(ctx))
    async with db.get_pool().connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT s.order_id, s.seller_agent_id, s.lines, s.picks, s.status, s.due_at,"
                " s.closed_at, o.household_id FROM shortfalls s"
                " JOIN orders o ON o.order_id = s.order_id WHERE s.shortfall_id = %s",
                (sf_uuid,),
            )
            row = await cur.fetchone()
    if row is None or row["household_id"] != household:
        raise GuardRefused(AUTHZ_DENIED)
    return {
        "shortfall_id": shortfall_id,
        "order_id": str(row["order_id"]),
        "seller_agent_id": row["seller_agent_id"],
        "lines": row["lines"],
        "picks": row["picks"],
        "status": row["status"],
        "due_at": row["due_at"].isoformat(),
        "closed_at": None if row["closed_at"] is None else row["closed_at"].isoformat(),
    }


# --- MCP registration -----------------------------------------------------------------


async def _serve(mcp_ctx: Context, tool: Tool[dict[str, Any]], **args: Any) -> CallToolResult:
    try:
        ctx = await CallContext.from_meta(mcp_ctx.request_context.meta)
    except GuardRefused as e:
        return tool_error("refused", e.reason)
    async with tool_errors(ctx, AREA, tool.__name__) as err:
        body = await tool(ctx, **args)
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(body))],
            structured_content=body,
        )
    assert err.result is not None
    return err.result


@server.tool(name="get_constraints")
async def _get_constraints_tool(user_id: uuid.UUID, ctx: Context) -> CallToolResult:
    return await _serve(ctx, get_constraints, user_id=user_id)


@server.tool(name="create_order")
async def _create_order_tool(
    order_id: uuid.UUID, kind: Literal["grocery", "dinner"], ctx: Context
) -> CallToolResult:
    return await _serve(ctx, create_order, order_id=order_id, kind=kind)


@server.tool(name="set_order_state")
async def _set_order_state_tool(
    order_id: uuid.UUID, expected: str | list[str], new: str, ctx: Context
) -> CallToolResult:
    return await _serve(ctx, set_order_state, order_id=str(order_id), expected=expected, new=new)


@server.tool(name="get_pantry")
async def _get_pantry_tool(ctx: Context) -> CallToolResult:
    return await _serve(ctx, get_pantry)


@server.tool(name="get_budget")
async def _get_budget_tool(ctx: Context) -> CallToolResult:
    return await _serve(ctx, get_budget)


@server.tool(name="authorize_spend")
async def _authorize_spend_tool(
    order_id: uuid.UUID,
    quote_id: uuid.UUID,
    items: list[OrderLine],
    total: MoneyArg,
    ctx: Context,
) -> CallToolResult:
    return await _serve(
        ctx, authorize_spend, order_id=order_id, quote_id=quote_id, items=items, total=total
    )


@server.tool(name="request_approval")
async def _request_approval_tool(
    order_id: uuid.UUID, amount: MoneyArg, ctx: Context
) -> CallToolResult:
    return await _serve(ctx, request_approval, order_id=order_id, amount=amount)


@server.tool(name="decide_approval")
async def _decide_approval_tool(
    approval_id: uuid.UUID,
    decision: Literal["approved", "rejected"],
    user_id: uuid.UUID,
    ctx: Context,
) -> CallToolResult:
    return await _serve(
        ctx, decide_approval, approval_id=approval_id, decision=decision, user_id=user_id
    )


@server.tool(name="record_delivery")
async def _record_delivery_tool(order_id: uuid.UUID, ctx: Context) -> CallToolResult:
    return await _serve(ctx, record_delivery, order_id=order_id)


@server.tool(name="get_household_allergens")
async def _get_household_allergens_tool(ctx: Context) -> CallToolResult:
    return await _serve(ctx, get_household_allergens)


@server.tool(name="record_dislike")
async def _record_dislike_tool(
    user_id: uuid.UUID, value: str, action: Literal["add", "remove"], ctx: Context
) -> CallToolResult:
    return await _serve(ctx, record_dislike, user_id=user_id, value=value, action=action)


@server.tool(name="get_household_rules")
async def _get_household_rules_tool(ctx: Context) -> CallToolResult:
    return await _serve(ctx, get_household_rules)


@server.tool(name="advance_turn")
async def _advance_turn_tool(order_id: uuid.UUID, ctx: Context) -> CallToolResult:
    return await _serve(ctx, advance_turn, order_id=order_id)


@server.tool(name="set_take_turns")
async def _set_take_turns_tool(
    order_id: uuid.UUID, on: bool, user_id: uuid.UUID, ctx: Context
) -> CallToolResult:
    return await _serve(ctx, set_take_turns, order_id=order_id, on=on, user_id=user_id)


@server.tool(name="open_shortfall")
async def _open_shortfall_tool(
    shortfall_id: uuid.UUID,
    order_id: uuid.UUID,
    seller: str,
    lines: list[ShortfallLine],
    due_at: datetime,
    ctx: Context,
) -> CallToolResult:
    return await _serve(
        ctx,
        open_shortfall,
        shortfall_id=str(shortfall_id),
        order_id=str(order_id),
        seller=seller,
        lines=[line.model_dump(mode="json") for line in lines],
        due_at=due_at.isoformat(),
    )


@server.tool(name="get_shortfall")
async def _get_shortfall_tool(shortfall_id: uuid.UUID, ctx: Context) -> CallToolResult:
    return await _serve(ctx, get_shortfall, shortfall_id=str(shortfall_id))


@server.tool(name="choose_shortfall_option")
async def _choose_shortfall_option_tool(
    shortfall_id: uuid.UUID, line: int, pick: str, user_id: uuid.UUID, ctx: Context
) -> CallToolResult:
    return await _serve(
        ctx,
        choose_shortfall_option,
        shortfall_id=shortfall_id,
        line=line,
        pick=pick,
        user_id=user_id,
    )


@server.tool(name="authorize_amendment")
async def _authorize_amendment_tool(
    order_id: uuid.UUID, shortfall_id: uuid.UUID, picks: dict[int, str], ctx: Context
) -> CallToolResult:
    return await _serve(
        ctx, authorize_amendment, order_id=order_id, shortfall_id=shortfall_id, picks=picks
    )
