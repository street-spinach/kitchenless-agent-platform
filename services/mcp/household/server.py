"""The `/mcp/household` MCP server (architecture §5 and item 43; agent-protocol P25).

`services/mcp/app.py` mounts `server`. Household tools use Postgres directly and have no
adapter (item 44). Guarded tools go through `services.safety.guard.run_guarded`; the
others use `@traced`. Every tool body runs inside `meta.tool_errors()`.

*   `get_constraints(user_id)` *guarded*: that person's allergy, diet and dislike rows.
*   `create_order(order_id, kind)` *guarded*: a `draft` order for the run's household.
*   `set_order_state(order_id, expected, new)`: only to `quoting`, `negotiating` or
    `cancelled`.
*   `get_pantry()`, `get_budget()`.
*   `authorize_spend(order_id, quote_id, items, total)` *guarded*: key scoped by
    `quote_id`; sets `authorized`, `orders.seller_agent_id` (from the quote), the total,
    the items hash and `order_items`.
*   `request_approval(order_id, amount)` *guarded*: a `pending` approval for the
    household's approver; sets `awaiting_approval`.
*   `decide_approval(approval_id, decision, user_id)` *guarded*, `api` service token.
*   `record_delivery(order_id)` *guarded*: one pantry event per SKU of a delivered order.

The shortfall tools (agent-protocol P25) are added by #68. The household is always the
run's household (`runs.household_id`), never an argument.
"""

import json
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import BaseModel

from services.common import db, ids, orders
from services.common.money import Money
from services.common.protocol.schemas import Currency, OrderLine
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
    "authorize_spend",
    "create_order",
    "decide_approval",
    "get_budget",
    "get_constraints",
    "get_pantry",
    "record_delivery",
    "request_approval",
    "server",
    "set_order_state",
]

AREA = "household"
API_AGENT = "system:api"

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
                "INSERT INTO pantry_events (household_id, item, delta, unit, source, order_id)"
                " SELECT %s, sku, sum(qty), 'each', 'delivery', order_id FROM order_items"
                " WHERE order_id = %s GROUP BY order_id, sku"
                " ON CONFLICT (order_id, item) WHERE order_id IS NOT NULL DO NOTHING",
                (household, order_id),
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
    """What the run's household has: the sum of its pantry events per item and unit."""
    household = _require_household(await _run_household(ctx))
    async with db.get_pool().connection() as conn:
        cur = await conn.execute(
            "SELECT item, unit, sum(delta) FROM pantry_events WHERE household_id = %s"
            " GROUP BY item, unit HAVING sum(delta) > 0 ORDER BY item, unit",
            (household,),
        )
        rows = await cur.fetchall()
    return {
        "household_id": household,
        "items": [{"item": item, "qty": _qty(qty), "unit": unit} for item, unit, qty in rows],
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
