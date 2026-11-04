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

The setup tools (experience §9, EX-I26), for the consumer agent:

*   `get_setup_state()` *guarded*: `{role, has_budget, has_proposal, currency}` for the
    person.
*   `propose_setup(answer)` *guarded*, admins only: the validated `SetupAnswer` stored as
    the household's `pending` proposal, with `phones_taken`.
*   `apply_setup(proposal_id, answer, removal, child_user_ids, user_id)` *guarded*, `api`
    service token, admins only: "Yes, save", "No", and each removal's second tap (EX-I27).
*   `complete_join(code, user_id)` *guarded*, `api` service token: moves a join code's
    `pending` onto the new user and marks the code used (EX-I31). Until then the codes of
    open join codes are part of the household's merged allergens and constraints.

The pantry correction (experience §8, EX-I39):

*   `record_pantry_check(pantry_item, kind, user_id)` *guarded*, `api` service token, adults
    only: one `verify` or `out` event for a vocabulary item.

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
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError

from services.common import db, dislikes, ids, orders, outbox, pantry, runs
from services.common.config import get_settings
from services.common.money import Money
from services.common.money import format as money_format
from services.common.protocol.schemas import (
    AllergenCode,
    Currency,
    DietCode,
    OrderLine,
    SetupAnswer,
    ShortfallLine,
)
from services.common.trace import TraceStore
from services.identity import audit
from services.mcp.knowledge.adapters.neo4j_knowledge import AllergenCheck
from services.mcp.knowledge.server import get_knowledge
from services.mcp.meta import CallContext, tool_error, tool_errors
from services.mcp.traced import Tool, traced
from services.safety import limits, scope
from services.safety.guard import GuardRefused, run_guarded
from services.safety.scope import unscoped

__all__ = [
    "SETTABLE_STATES",
    "MoneyArg",
    "advance_turn",
    "apply_setup",
    "authorize_amendment",
    "authorize_spend",
    "choose_shortfall_option",
    "complete_join",
    "create_order",
    "decide_approval",
    "format_reply_by",
    "get_budget",
    "get_constraints",
    "get_household_allergens",
    "get_household_rules",
    "get_pantry",
    "get_setup_state",
    "get_shortfall",
    "open_shortfall",
    "propose_setup",
    "record_delivery",
    "record_dislike",
    "record_pantry_check",
    "request_approval",
    "server",
    "set_order_state",
    "set_take_turns",
    "setup_removals",
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
# `ctx.authz_form` of `get_constraints` (identity-authz §8 step 2).
CONSTRAINTS_FORM_PERSON = "person"
CONSTRAINTS_FORM_HOUSEHOLD = "household"
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
# `propose_setup` (experience §9): the person is a member but not an admin.
NOT_ADMIN = "not_admin"
# `propose_setup`: the answer does not match `SetupAnswer` once unknown codes are moved.
INVALID_ANSWER = "invalid_answer"
# `apply_setup`: the proposal expired, was superseded or was discarded (§9 `setup_expired`).
SETUP_EXPIRED = "setup_expired"
_SETUP_ANSWERS = frozenset({"yes", "no", "remove", "keep"})
_REMOVAL_ANSWERS = frozenset({"remove", "keep"})
_JOIN_CODE_TRIES = 3
# `record_pantry_check` (experience EX-I39): the person is not an adult of the household, the
# item is not a vocabulary name, or the household's pantry is off (PT-OD4).
NOT_ADULT = "not_adult"
UNKNOWN_PANTRY_ITEM = "unknown_pantry_item"
PANTRY_OFF = "pantry_off"
_ADULT_ROLES = frozenset({"admin", "member"})
_PANTRY_CHECK_KINDS = frozenset({"verify", "out"})
_PANTRY_CHECK_SOURCE = "me_page"
# `complete_join`: not the household's open code for this person's phone (experience §9).
JOIN_CODE_INVALID = "join_code_invalid"

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


def _scoped(conn: AsyncConnection, household: str | None) -> scope.Scoped:
    """Queries bound to the run's household (`runs.household_id`, identity-authz §8
    "Scoping helper"); with no household the call is refused `authz_denied`."""
    return scope.scoped(conn, scope.HouseholdScope(_require_household(household), "run"))


async def _order(conn: AsyncConnection, order_id: uuid.UUID, household: str) -> dict[str, Any]:
    """The order, refused like `addresses.resolve` when it is missing or another household's."""
    rows = await _scoped(conn, household).fetch(
        "SELECT household_id, kind, state, currency FROM orders"
        " WHERE order_id = %(order)s AND household_id = %(household)s",
        order=order_id,
    )
    if not rows:
        raise GuardRefused(AUTHZ_DENIED)
    household_id, kind, state, currency = rows[0]
    return {"household_id": household_id, "kind": kind, "state": state, "currency": currency}


async def _open_join_codes(
    conn: AsyncConnection, household: str | None
) -> list[tuple[str, dict[str, list[str]]]]:
    """`(code, pending)` of the household's unused, unexpired join codes (experience EX-A9).
    `pending` is `{allergies: [...], diets: [...]}`, as `apply_setup` and the `join_code`
    setup key write it; a row of any other shape raises, so nothing is ordered without it."""
    rows = await _scoped(conn, household).fetch(
        "SELECT code, pending FROM join_codes WHERE household_id = %(household)s"
        " AND used_at IS NULL AND expires_at > now() ORDER BY code"
    )
    return [(str(code), pending) for code, pending in rows]


async def _member_allergens(conn: AsyncConnection, household: str) -> dict[str, list[str]]:
    """Each member's allergen codes, plus one `join:<code>` entry per open join code."""
    rows = await _scoped(conn, household).fetch(
        "SELECT m.user_id, c.value FROM household_members m"
        " JOIN member_constraints c ON c.user_id = m.user_id AND c.kind = 'allergy'"
        " WHERE m.household_id = %(household)s ORDER BY m.user_id, c.value"
    )
    allergens: dict[str, list[str]] = {}
    for user_id, code in rows:
        allergens.setdefault(str(user_id), []).append(code)
    for code, pending in await _open_join_codes(conn, household):
        allergens[f"join:{code}"] = sorted(pending["allergies"])
    return allergens


async def _is_member(conn: AsyncConnection, household: str | None, user_id: Any) -> bool:
    """Whether `user_id` is a member of the run's household (refused with no household)."""
    rows = await _scoped(conn, household).fetch(
        "SELECT 1 FROM household_members WHERE household_id = %(household)s AND user_id = %(user)s",
        user=user_id,
    )
    return bool(rows)


def _money(value: MoneyArg | Mapping[str, Any]) -> Money:
    return (value if isinstance(value, MoneyArg) else MoneyArg.model_validate(value)).money()


def _qty(value: Decimal) -> int | str:
    return int(value) if value == value.to_integral_value() else format(value.normalize(), "f")


# --- guarded tools --------------------------------------------------------------------


async def get_constraints(ctx: CallContext, *, user_id: uuid.UUID) -> dict[str, Any]:
    """The person's own constraint rows. Relation `agent acts_for user:<user_id>`.

    Called by a household agent (identity-authz §8 step 2, I-D10, I-CUJ 11), the form is
    `household`: relation `agent acts_for household:<h>` for the run's household, and the
    result is the `allergy` and `diet` rows of every member of `h`, never `dislike`. The
    named `user_id` must be a member of `h`.
    """
    if ids.parse_agent_id(ctx.agent_id)[0] == "household":
        return await _household_constraints(ctx, user_id)
    household = await _run_household(ctx)

    async def call() -> dict[str, Any]:
        async with db.get_pool().connection() as conn:
            rows = await _scoped(conn, household).fetch(
                "SELECT c.kind, c.value FROM member_constraints c"
                " JOIN household_members m ON m.user_id = c.user_id"
                " AND m.household_id = %(household)s"
                " WHERE c.user_id = %(user)s ORDER BY c.kind, c.value",
                user=user_id,
            )
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
        authz_form=CONSTRAINTS_FORM_PERSON,
    )


async def _household_constraints(ctx: CallContext, user_id: uuid.UUID) -> dict[str, Any]:
    """The `household` form of `get_constraints`."""
    household = await _run_household(ctx)

    async def check(conn: AsyncConnection) -> None:
        if not await _is_member(conn, household, user_id):
            raise GuardRefused(AUTHZ_DENIED)

    async def call() -> dict[str, Any]:
        async with db.get_pool().connection() as conn:
            rows = await _scoped(conn, household).fetch(
                "SELECT m.user_id, c.kind, c.value FROM household_members m"
                " LEFT JOIN member_constraints c ON c.user_id = m.user_id"
                "  AND c.kind IN ('allergy', 'diet')"
                " WHERE m.household_id = %(household)s ORDER BY m.user_id, c.kind, c.value"
            )
            open_codes = await _open_join_codes(conn, household)
        members: dict[str, dict[str, Any]] = {}
        for member, kind, value in rows:
            entry = members.setdefault(
                str(member), {"user_id": str(member), "allergies": [], "diets": []}
            )
            if kind == "allergy":
                entry["allergies"].append(value)
            elif kind == "diet":
                entry["diets"].append(value)
        # A joining adult is protected before they join (experience EX-A9).
        for code, pending in open_codes:
            members[f"join:{code}"] = {
                "user_id": f"join:{code}",
                "allergies": sorted(pending["allergies"]),
                "diets": sorted(pending["diets"]),
            }
        return {"household_id": household, "members": list(members.values())}

    return await run_guarded(
        ctx,
        tool=f"{AREA}.get_constraints",
        relation=_household_relation("acts_for", household),
        limits=check,
        key=None,
        request={"user_id": str(user_id)},
        call=call,
        apply=None,
        authz_form=CONSTRAINTS_FORM_HOUSEHOLD,
    )


async def create_order(ctx: CallContext, *, order_id: uuid.UUID, kind: str) -> dict[str, Any]:
    """A `draft` order for the run's household, in its currency and at its address."""
    household = await _run_household(ctx)

    async def check(conn: AsyncConnection) -> None:
        h = _require_household(household)
        if kind not in ("grocery", "dinner"):
            raise GuardRefused(INVALID_KIND)
        # An order id already used by another household is refused.
        other = await _scoped(conn, h).fetch(
            "SELECT 1 FROM orders WHERE order_id = %(order)s"
            " AND household_id IS DISTINCT FROM %(household)s",
            order=order_id,
        )
        if other:
            raise GuardRefused(AUTHZ_DENIED)

    async def call() -> dict[str, Any]:
        return {"order_id": str(order_id), "kind": kind, "state": "draft"}

    async def apply(conn: AsyncConnection, result: Any) -> None:
        q = _scoped(conn, household)
        cur = await q.execute(
            "INSERT INTO orders (order_id, run_id, household_id, kind, currency, address_ref)"
            " SELECT %(order)s, %(run)s, h.household_id, %(kind)s, h.currency, a.address_ref"
            " FROM households h JOIN household_addresses a ON a.household_id = h.household_id"
            " WHERE h.household_id = %(household)s ORDER BY a.created_at, a.address_ref LIMIT 1"
            " ON CONFLICT (order_id) DO NOTHING",
            order=order_id,
            run=ctx.run_id,
            kind=kind,
        )
        if cur.rowcount == 0:
            found = await q.fetch(
                "SELECT 1 FROM orders WHERE order_id = %(order)s AND household_id = %(household)s",
                order=order_id,
            )
            if not found:
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
        q = _scoped(conn, household)
        await q.execute(
            "DELETE FROM order_items WHERE order_id = %(order)s AND order_id IN"
            " (SELECT order_id FROM orders WHERE household_id = %(household)s)",
            order=order_id,
        )
        for i in lines:
            await q.execute(
                "INSERT INTO order_items"
                " (order_id, line, sku, name, qty, unit_minor, substituted_for)"
                " SELECT o.order_id, %(line)s, %(sku)s, %(name)s, %(qty)s, %(unit)s, %(sub)s"
                " FROM orders o WHERE o.order_id = %(order)s AND o.household_id = %(household)s",
                order=order_id,
                line=i.line,
                sku=i.sku,
                name=i.name,
                qty=i.qty,
                unit=i.unit_minor,
                sub=i.substituted_for,
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
    hit: list[str] = []

    async def check(conn: AsyncConnection) -> None:
        h = _require_household(household)
        order = await _order(conn, order_id, h)
        if order["state"] not in ("negotiating", "awaiting_approval"):
            raise GuardRefused(INVALID_STATE)
        await limits.check_currency(conn, order_id, money)
        rows = await _scoped(conn, h).fetch(
            "SELECT approver_user_id FROM approval_rules WHERE household_id = %(household)s"
        )
        if not rows:
            raise GuardRefused(NO_APPROVER)
        approver.append(str(rows[0][0]))
        # Which limits sent the order here (identity-authz §9), stored in
        # `approvals.limits`; empty when only the child-requester rule applies.
        hit.extend(await limits.approval_limits(conn, h, money, require_budget=False))

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
        await _scoped(conn, household).execute(
            "INSERT INTO approvals"
            " (approval_id, run_id, household_id, order_id, amount_minor, currency, limits)"
            " VALUES (%(approval)s, %(run)s, %(household)s, %(order)s, %(amount)s,"
            " %(currency)s, %(limits)s) ON CONFLICT (approval_id) DO NOTHING",
            approval=approval_id,
            run=ctx.run_id,
            order=order_id,
            amount=money.amount_minor,
            currency=money.currency,
            limits=hit,
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

    `user_id` must be `approval_rules.approver_user_id`. The approval must belong to the
    run's household (the call's run is the approval's run). The update is conditional on
    `pending`: the result's `outcome` is `decided`, or `already_decided` with the current
    status (for example `expired` by the sweeper). A decision writes the
    `approval_decided` audit event in the same transaction (identity-authz §14, I30).
    """
    household = await _run_household(ctx)

    async def check(conn: AsyncConnection) -> None:
        if ctx.agent_id != API_AGENT:
            raise GuardRefused(AUTHZ_DENIED)
        if decision not in _DECISIONS:
            raise GuardRefused(INVALID_DECISION)
        rows = await _scoped(conn, household).fetch(
            "SELECT 1 FROM approvals"
            " WHERE approval_id = %(approval)s AND household_id = %(household)s",
            approval=approval_id,
        )
        if not rows:
            raise GuardRefused(AUTHZ_DENIED)
        await limits.check_approver(conn, household, user_id)

    async def call() -> dict[str, Any]:
        async with db.get_pool().connection() as conn, conn.transaction():
            q = _scoped(conn, household)
            cur = await q.execute(
                "UPDATE approvals SET status = %(decision)s, decided_at = now(),"
                " decided_by = %(user)s WHERE approval_id = %(approval)s"
                " AND household_id = %(household)s AND status = 'pending'"
                " RETURNING order_id, amount_minor, currency, limits",
                decision=decision,
                user=user_id,
                approval=approval_id,
            )
            row = await cur.fetchone()
            if row is not None:
                order_id, amount_minor, currency, hit = row
                await audit.record(
                    conn,
                    "approval_decided",
                    actor_user_id=user_id,
                    agent_id=ctx.agent_id,
                    household_id=household,
                    run_id=ctx.run_id,
                    detail={
                        "approval_id": str(approval_id),
                        "order_id": str(order_id),
                        "decision": decision,
                        "amount_minor": int(amount_minor),
                        "currency": str(currency).strip(),
                        "limits": list(hit or []),
                    },
                )
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
                rows = await q.fetch(
                    "SELECT order_id, status FROM approvals"
                    " WHERE approval_id = %(approval)s AND household_id = %(household)s",
                    approval=approval_id,
                )
                order_id, status = rows[0] if rows else (None, None)
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
            cur = await _scoped(conn, household).execute(
                "INSERT INTO pantry_events"
                " (household_id, item, pantry_item, kind, delta, unit, source, order_id, run_id)"
                " SELECT %(household)s, oi.sku, ci.pantry_item, 'add', sum(oi.qty), 'each',"
                " 'delivery', oi.order_id, %(run)s FROM order_items oi"
                " JOIN orders o ON o.order_id = oi.order_id"
                " LEFT JOIN catalog_items ci"
                " ON ci.agent_id = o.seller_agent_id AND ci.sku = oi.sku"
                " WHERE oi.order_id = %(order)s AND o.household_id = %(household)s"
                " GROUP BY oi.order_id, oi.sku, ci.pantry_item"
                " ON CONFLICT (order_id, item) WHERE order_id IS NOT NULL DO NOTHING",
                run=ctx.run_id,
                order=order_id,
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
    """The sorted union of every member's allergen codes and those of the household's open
    join codes, and nothing else (agents §6, §9; experience EX-A9).

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
    household = await _run_household(ctx)

    async def check(conn: AsyncConnection) -> None:
        if normalized is None or action not in _DISLIKE_ACTIONS:
            raise GuardRefused(INVALID_VALUE)
        if not await _is_member(conn, household, user_id):
            raise GuardRefused(AUTHZ_DENIED)

    async def call() -> dict[str, Any]:
        async with db.get_pool().connection() as conn, conn.transaction():
            q = _scoped(conn, household)
            if action == "add":
                found = await q.fetch(
                    "SELECT 1 FROM member_constraints WHERE user_id = %(user)s"
                    " AND user_id IN (SELECT user_id FROM household_members"
                    " WHERE household_id = %(household)s)"
                    " AND kind = 'dislike' AND lower(value) = %(value)s",
                    user=user_id,
                    value=normalized,
                )
                if found:
                    outcome = "already_present"
                else:
                    cur = await q.execute(
                        "INSERT INTO member_constraints (user_id, kind, value, source)"
                        " SELECT user_id, 'dislike', %(value)s, 'learned'"
                        " FROM household_members"
                        " WHERE household_id = %(household)s AND user_id = %(user)s"
                        " ON CONFLICT (user_id, value) WHERE source = 'learned' DO NOTHING",
                        user=user_id,
                        value=normalized,
                    )
                    outcome = "added" if cur.rowcount == 1 else "already_present"
            else:
                cur = await q.execute(
                    "DELETE FROM member_constraints WHERE user_id = %(user)s"
                    " AND user_id IN (SELECT user_id FROM household_members"
                    " WHERE household_id = %(household)s)"
                    " AND value = %(value)s AND source = 'learned'",
                    user=user_id,
                    value=normalized,
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
            q = _scoped(conn, household)
            found = await q.fetch(
                "SELECT take_turns, turn_index FROM household_rules"
                " WHERE household_id = %(household)s"
            )
            approvers = await q.fetch(
                "SELECT approver_user_id FROM approval_rules WHERE household_id = %(household)s"
            )
        rules = found[0] if found else None
        approver = approvers[0] if approvers else None
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
        rows = await _scoped(conn, household).fetch(
            "SELECT kind, state FROM orders"
            " WHERE order_id = %(order)s AND household_id = %(household)s",
            order=order_id,
        )
        if not rows or tuple(rows[0]) != ("dinner", "delivered"):
            raise GuardRefused(WRONG_ORDER_STATE)

    async def call() -> dict[str, Any]:
        async with db.get_pool().connection() as conn, conn.transaction():
            cur = await _scoped(conn, household).execute(
                "UPDATE household_rules SET turn_index = turn_index + 1,"
                " last_turn_order_id = %(order)s, updated_at = now()"
                " WHERE household_id = %(household)s AND take_turns"
                " AND last_turn_order_id IS DISTINCT FROM %(order)s",
                order=order_id,
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
    order must be the run's household's (else `authz_denied`) and `user_id` must be its
    approver (else `wrong_approver`). `changed` says whether take-turns changed; writing
    the same value again is harmless, so there is no key.
    """
    household = await _run_household(ctx)

    async def check(conn: AsyncConnection) -> None:
        if ctx.agent_id != API_AGENT:
            raise GuardRefused(AUTHZ_DENIED)
        await _order(conn, order_id, _require_household(household))
        await limits.check_approver(conn, household, user_id)

    async def call() -> dict[str, Any]:
        async with db.get_pool().connection() as conn, conn.transaction():
            q = _scoped(conn, household)
            found = await q.fetch(
                "SELECT take_turns FROM household_rules"
                " WHERE household_id = %(household)s FOR UPDATE"
            )
            before = found[0] if found else None
            await q.execute(
                "INSERT INTO household_rules (household_id, take_turns, set_by)"
                " VALUES (%(household)s, %(on)s, %(user)s) ON CONFLICT (household_id) DO UPDATE"
                " SET take_turns = EXCLUDED.take_turns, set_by = EXCLUDED.set_by,"
                " updated_at = now()",
                on=on,
                user=user_id,
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
    `chosen` or `already_decided`. The shortfall's order must be the run's household's
    (the call's run is the shortfall's run).
    """
    household = await _run_household(ctx)

    async def check(conn: AsyncConnection) -> None:
        if ctx.agent_id != API_AGENT:
            raise GuardRefused(AUTHZ_DENIED)
        rows = await _scoped(conn, household).fetch(
            "SELECT s.lines FROM shortfalls s"
            " JOIN orders o ON o.order_id = s.order_id"
            " WHERE s.shortfall_id = %(shortfall)s AND o.household_id = %(household)s",
            shortfall=shortfall_id,
        )
        if not rows:
            raise limits.ShortfallNotFound()
        (lines,) = rows[0]
        if not await _is_member(conn, household, user_id):
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
        state = (await _order(conn, order_uuid, household))["state"]
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
        q = _scoped(conn, household)
        rows = await q.fetch(
            "SELECT item, unit, sum(delta) FROM pantry_events WHERE household_id = %(household)s"
            # COLLATE "C": the order is byte order on every database, whatever its locale.
            " GROUP BY item, unit HAVING sum(delta) > 0"
            ' ORDER BY item COLLATE "C", unit COLLATE "C"'
        )
        cur = await conn.execute("SELECT now()")
        row = await cur.fetchone()
        assert row is not None
        as_of: datetime = row[0]
        latest: list[dict[str, Any]] = []
        if vocab is not None:
            found = await q.fetch(
                "SELECT DISTINCT ON (pantry_item) pantry_item, event_id, kind, ts"
                " FROM pantry_events"
                " WHERE household_id = %(household)s AND pantry_item IS NOT NULL"
                " ORDER BY pantry_item, event_id DESC"
            )
            latest = [
                {"pantry_item": item, "event_id": event_id, "kind": kind, "ts": ts}
                for item, event_id, kind, ts in found
            ]
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
        found = await _scoped(conn, household).fetch(
            "SELECT amount_minor, currency FROM budgets"
            " WHERE household_id = %(household)s AND period = 'weekly'"
        )
        if not found:
            raise LookupError(f"no weekly budget for household {household}")
        row = found[0]
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
        found = await _scoped(conn, household).fetch(
            "SELECT seller_agent_id FROM orders"
            " WHERE order_id = %(order)s AND household_id = %(household)s",
            order=order_uuid,
        )
        if not found:
            raise GuardRefused(AUTHZ_DENIED)
        (order_seller,) = found[0]
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
        found = await _scoped(conn, household).fetch(
            "SELECT s.order_id, s.seller_agent_id, s.lines, s.picks, s.status, s.due_at,"
            " s.closed_at FROM shortfalls s"
            " JOIN orders o ON o.order_id = s.order_id"
            " WHERE s.shortfall_id = %(shortfall)s AND o.household_id = %(household)s",
            shortfall=sf_uuid,
        )
    if not found:
        raise GuardRefused(AUTHZ_DENIED)
    keys = ("order_id", "seller_agent_id", "lines", "picks", "status", "due_at", "closed_at")
    row = dict(zip(keys, found[0], strict=True))
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


# --- setup tools (experience §9, EX-I26) -----------------------------------------------

_SETUP_CODES = {
    "add_allergies": frozenset(c.value for c in AllergenCode),
    "remove_allergies": frozenset(c.value for c in AllergenCode),
    "add_diets": frozenset(c.value for c in DietCode),
    "remove_diets": frozenset(c.value for c in DietCode),
}


def _acting_user(ctx: CallContext) -> uuid.UUID | None:
    """The person a consumer agent acts for (`on_behalf_of = "user:<id>"`), else None."""
    kind, _, value = (ctx.on_behalf_of or "").partition(":")
    if kind != "user":
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _user_relation(user: uuid.UUID | None) -> tuple[str, str] | None:
    # With no person there is nothing to check; the limits then refuse `authz_denied`.
    return None if user is None else ("acts_for", f"user:{user}")


async def _role(conn: AsyncConnection, household: str | None, user: uuid.UUID) -> str | None:
    rows = await _scoped(conn, household).fetch(
        "SELECT role FROM household_members"
        " WHERE household_id = %(household)s AND user_id = %(user)s",
        user=user,
    )
    return None if not rows else str(rows[0][0])


def _sort_codes(raw: Any) -> dict[str, Any]:
    """§9 step 4, first part: codes outside the enums move to `unmatched`, then the answer
    must match `SetupAnswer` (else `invalid_answer`)."""
    if not isinstance(raw, Mapping):
        raise GuardRefused(INVALID_ANSWER)
    unmatched = raw.get("unmatched") or []
    people = raw.get("people") or []
    if not isinstance(unmatched, list) or not isinstance(people, list):
        raise GuardRefused(INVALID_ANSWER)
    unmatched = [str(word) for word in unmatched]
    sorted_people: list[dict[str, Any]] = []
    for person in people:
        if not isinstance(person, Mapping):
            raise GuardRefused(INVALID_ANSWER)
        out = dict(person)
        for field, known in _SETUP_CODES.items():
            values = out.get(field) or []
            if not isinstance(values, list):
                raise GuardRefused(INVALID_ANSWER)
            kept: list[str] = []
            for value in values:
                if isinstance(value, str) and value in known:
                    if value not in kept:
                        kept.append(value)
                elif str(value) not in unmatched:
                    unmatched.append(str(value))
            out[field] = kept
        sorted_people.append(out)
    try:
        parsed = SetupAnswer.model_validate(
            {**raw, "people": sorted_people, "unmatched": unmatched}
        )
    except ValidationError:
        raise GuardRefused(INVALID_ANSWER) from None
    return parsed.model_dump(mode="json")


async def get_setup_state(ctx: CallContext) -> dict[str, Any]:
    """`{role, has_budget, has_proposal, currency}` for the person the consumer agent acts
    for, in the run's household (§9 step 1). Relation `agent acts_for user:<that person>`;
    read only.

    `has_proposal` is whether the household has ever had a proposal, whatever its status.
    `currency` is the household's (`households.currency`): the setup prompt asks for the
    budget in its minor units and the restate shows it (#296).
    """
    household = await _run_household(ctx)
    user = _acting_user(ctx)

    async def check(conn: AsyncConnection) -> None:
        _require_household(household)
        if user is None or await _role(conn, household, user) is None:
            raise GuardRefused(AUTHZ_DENIED)

    async def call() -> dict[str, Any]:
        assert user is not None
        async with db.get_pool().connection() as conn:
            q = _scoped(conn, household)
            role = await _role(conn, household, user)
            budget = await q.fetch(
                "SELECT 1 FROM budgets WHERE household_id = %(household)s LIMIT 1"
            )
            proposal = await q.fetch(
                "SELECT 1 FROM onboarding_proposals WHERE household_id = %(household)s LIMIT 1"
            )
            currency = await q.fetch(
                "SELECT currency FROM households WHERE household_id = %(household)s"
            )
        return {
            "role": role,
            "has_budget": bool(budget),
            "has_proposal": bool(proposal),
            "currency": str(currency[0][0]).strip() if currency else None,
        }

    return await run_guarded(
        ctx,
        tool=f"{AREA}.get_setup_state",
        relation=_user_relation(user),
        limits=check,
        key=None,
        request={},
        call=call,
        apply=None,
    )


async def propose_setup(ctx: CallContext, *, answer: Mapping[str, Any]) -> dict[str, Any]:
    """Store the validated setup answer as the household's `pending` proposal (§9 step 4).

    The caller must act for an admin of the run's household (else `not_admin`). Codes
    outside the enums move to `unmatched`; a name matching a member (case-insensitive)
    gets that member's `user_id`; a phone already in another household is dropped and
    returned in `phones_taken` (a yes/no lookup only). The row id is
    `ids.proposal_id(run_id)`, so a replayed run stores nothing new and returns the stored
    answer; a new row makes earlier `pending` proposals `superseded`. Expires in 1 h.
    """
    household = await _run_household(ctx)
    user = _acting_user(ctx)
    proposal = ids.proposal_id(ctx.run_id)
    sorted_answer: dict[str, Any] = {}

    async def check(conn: AsyncConnection) -> None:
        _require_household(household)
        if user is None:
            raise GuardRefused(AUTHZ_DENIED)
        role = await _role(conn, household, user)
        if role is None:
            raise GuardRefused(AUTHZ_DENIED)
        if role != "admin":
            raise GuardRefused(NOT_ADMIN)
        sorted_answer.update(_sort_codes(answer))

    async def call() -> dict[str, Any]:
        stored = dict(sorted_answer)
        async with db.get_pool().connection() as conn, conn.transaction():
            q = _scoped(conn, household)
            members = {
                str(name).strip().lower(): str(member)
                for member, name in await q.fetch(
                    "SELECT m.user_id, u.display_name FROM household_members m"
                    " JOIN users u ON u.user_id = m.user_id"
                    " WHERE m.household_id = %(household)s"
                )
            }
            taken: list[str] = []
            people: list[dict[str, Any]] = []
            for person in stored["people"]:
                person = dict(person)
                member = members.get(person["name"].strip().lower())
                if member is not None:
                    person["user_id"] = member
                phone = person.get("phone")
                if phone is not None:
                    cur = await unscoped(conn, "setup_phone_check").execute(
                        "SELECT EXISTS (SELECT 1 FROM channel_identities c"
                        " WHERE c.phone_e164 = %(phone)s AND NOT EXISTS (SELECT 1"
                        " FROM household_members m WHERE m.user_id = c.user_id"
                        " AND m.household_id = %(household)s))",
                        {"phone": phone, "household": household},
                    )
                    row = await cur.fetchone()
                    if row is None or row[0]:
                        person["phone"] = None
                        if phone not in taken:
                            taken.append(phone)
                people.append(person)
            stored = {**stored, "people": people, "phones_taken": taken}
            cur = await q.execute(
                "INSERT INTO onboarding_proposals"
                " (proposal_id, household_id, proposed_by, answer, expires_at)"
                " VALUES (%(proposal)s, %(household)s, %(user)s, %(answer)s,"
                " now() + interval '1 hour') ON CONFLICT DO NOTHING",
                proposal=proposal,
                user=user,
                answer=Jsonb(stored),
            )
            if cur.rowcount == 1:
                await q.execute(
                    "UPDATE onboarding_proposals SET status = 'superseded'"
                    " WHERE household_id = %(household)s AND status = 'pending'"
                    " AND proposal_id <> %(proposal)s",
                    proposal=proposal,
                )
            found = await q.fetch(
                "SELECT answer FROM onboarding_proposals"
                " WHERE proposal_id = %(proposal)s AND household_id = %(household)s",
                proposal=proposal,
            )
        if not found:
            raise GuardRefused(AUTHZ_DENIED)
        kept = dict(found[0][0])
        phones_taken = list(kept.pop("phones_taken", []))
        return {"proposal_id": str(proposal), "answer": kept, "phones_taken": phones_taken}

    return await run_guarded(
        ctx,
        tool=f"{AREA}.propose_setup",
        relation=_user_relation(user),
        limits=check,
        key=None,
        request={"answer": dict(answer) if isinstance(answer, Mapping) else answer},
        call=call,
        apply=None,
    )


def setup_removals(answer: Mapping[str, Any]) -> list[dict[str, str]]:
    """The proposal's removals, numbered by position (§9 "Removals"): for each person
    matched to a member (`user_id`), in answer order, `remove_allergies` then
    `remove_diets`. A removal for someone who is not yet a member has no row to remove and
    is not numbered. The restate and the confirm buttons use the same numbering."""
    out: list[dict[str, str]] = []
    for person in answer.get("people") or []:
        member = person.get("user_id")
        if not member:
            continue
        for field, kind in (("remove_allergies", "allergy"), ("remove_diets", "diet")):
            for code in person.get(field) or []:
                out.append(
                    {"user_id": str(member), "name": person["name"], "kind": kind, "value": code}
                )
    return out


def _new_children(answer: Mapping[str, Any]) -> list[str]:
    """Names of the children the answer adds (no `user_id`): ingest creates each one."""
    return [
        p["name"] for p in answer.get("people") or [] if p.get("child") and not p.get("user_id")
    ]


async def _proposal(
    conn: AsyncConnection, household: str | None, proposal_id: uuid.UUID, *, lock: bool = False
) -> tuple[str, bool, dict[str, Any]] | None:
    """`(status, expired, answer)` of the household's proposal, or None."""
    rows = await _scoped(conn, household).fetch(
        "SELECT status, expires_at <= now(), answer FROM onboarding_proposals"
        " WHERE proposal_id = %(proposal)s AND household_id = %(household)s"
        + (" FOR UPDATE" if lock else ""),
        proposal=proposal_id,
    )
    if not rows:
        return None
    status, expired, stored = rows[0]
    return str(status), bool(expired), dict(stored)


async def apply_setup(
    ctx: CallContext,
    *,
    proposal_id: uuid.UUID,
    answer: str,
    removal: int | None,
    child_user_ids: Mapping[str, uuid.UUID],
    user_id: uuid.UUID,
) -> dict[str, Any]:
    """An admin's tap on a setup proposal (§9 "Yes, save", "Removals"), from `api` with its
    service token. `user_id` is the tapper (the run trigger's person), checked to be an
    admin of the run's household (else `not_admin`).

    *   `yes` (proposal `pending`, unexpired; else `setup_expired`): in one transaction,
        the additions to `member_constraints` (`source = 'setup'`) for matched members and
        for each new child (`child_user_ids`, keyed by the name as written; every new child
        must be there and be a child of the household, else `invalid_answer`), the weekly
        budget in the household currency, one join code per new adult with a phone
        (holding that adult's codes), and the status `applied`, or `awaiting_remove` when
        there are removals. A new adult with no phone is listed in `saved.not_saved`.
    *   `no`: `discarded`.
    *   `remove` / `keep` with `removal` (an index of `setup_removals`): deletes that
        member's row, or leaves it; the last answer makes the proposal `applied`.

    A repeated tap returns `outcome: already_decided` and writes nothing. `saved` is what
    the `yes` saved (stored on the proposal, so a repeat returns the same), else None.
    """
    household = await _run_household(ctx)
    decision = answer

    async def check(conn: AsyncConnection) -> None:
        if ctx.agent_id != API_AGENT:
            raise GuardRefused(AUTHZ_DENIED)
        if decision not in _SETUP_ANSWERS or (decision in _REMOVAL_ANSWERS) != (
            removal is not None
        ):
            raise GuardRefused(INVALID_ANSWER)
        if await _role(conn, household, user_id) != "admin":
            raise GuardRefused(NOT_ADMIN)
        found = await _proposal(conn, household, proposal_id)
        if found is None:
            raise GuardRefused(AUTHZ_DENIED)
        status, expired, stored = found
        if decision in ("yes", "no"):
            if status == "pending" and expired:
                raise GuardRefused(SETUP_EXPIRED)
            if status == "superseded" or (status == "discarded" and decision == "yes"):
                raise GuardRefused(SETUP_EXPIRED)
            if decision == "yes" and status == "pending":
                for name in _new_children(stored):
                    child = child_user_ids.get(name)
                    if child is None or await _role(conn, household, child) != "child":
                        raise GuardRefused(INVALID_ANSWER)
            return
        # `remove` / `keep`: only after "Yes, save".
        if status in ("superseded", "discarded"):
            raise GuardRefused(SETUP_EXPIRED)
        if status == "pending":
            raise GuardRefused(INVALID_ANSWER)
        if not isinstance(removal, int) or not 0 <= removal < len(setup_removals(stored)):
            raise GuardRefused(INVALID_ANSWER)

    def result(status: str, outcome: str, stored: Mapping[str, Any]) -> dict[str, Any]:
        decided = stored.get("removal_decisions") or {}
        count = len(setup_removals(stored)) if stored.get("applied") is not None else 0
        return {
            "proposal_id": str(proposal_id),
            "status": status,
            "outcome": outcome,
            "saved": stored.get("applied"),
            "removals_pending": [n for n in range(count) if str(n) not in decided],
        }

    async def save(conn: AsyncConnection, stored: dict[str, Any]) -> dict[str, Any]:
        q = _scoped(conn, household)
        constraints: list[dict[str, str]] = []
        join_codes: list[dict[str, str]] = []
        not_saved: list[str] = []
        for person in stored.get("people") or []:
            codes = [("allergy", c) for c in person.get("add_allergies") or []] + [
                ("diet", c) for c in person.get("add_diets") or []
            ]
            target = person.get("user_id") or (
                child_user_ids.get(person["name"]) if person.get("child") else None
            )
            if target is not None:
                for kind, value in codes:
                    cur = await q.execute(
                        "INSERT INTO member_constraints (user_id, kind, value, source)"
                        " SELECT m.user_id, %(kind)s, %(value)s, 'setup'"
                        " FROM household_members m"
                        " WHERE m.household_id = %(household)s AND m.user_id = %(user)s"
                        " AND NOT EXISTS (SELECT 1 FROM member_constraints c"
                        " WHERE c.user_id = m.user_id AND c.kind = %(kind)s"
                        " AND c.value = %(value)s)",
                        user=target,
                        kind=kind,
                        value=value,
                    )
                    if cur.rowcount == 1:
                        constraints.append({"user_id": str(target), "kind": kind, "value": value})
            elif person.get("phone"):
                pending = {
                    "allergies": list(person.get("add_allergies") or []),
                    "diets": list(person.get("add_diets") or []),
                }
                for _ in range(_JOIN_CODE_TRIES):
                    code = ids.new_join_code()
                    cur = await q.execute(
                        "INSERT INTO join_codes (code, household_id, phone_e164, display_name,"
                        " pending, created_by, expires_at)"
                        " VALUES (%(code)s, %(household)s, %(phone)s, %(name)s, %(pending)s,"
                        " %(user)s, now() + interval '7 days') ON CONFLICT DO NOTHING",
                        code=code,
                        phone=person["phone"],
                        name=person["name"],
                        pending=Jsonb(pending),
                        user=user_id,
                    )
                    if cur.rowcount == 1:
                        join_codes.append(
                            {"name": person["name"], "phone": person["phone"], "code": code}
                        )
                        break
                else:
                    raise RuntimeError("no free join code")  # 40 bits: never in practice
            else:
                not_saved.append(person["name"])
        budget_minor = stored.get("budget_minor")
        budget_set = False
        if budget_minor is not None:
            had = await q.fetch("SELECT 1 FROM budgets WHERE household_id = %(household)s")
            await q.execute(
                "INSERT INTO budgets (household_id, period, amount_minor, currency)"
                " SELECT household_id, 'weekly', %(amount)s, currency FROM households"
                " WHERE household_id = %(household)s"
                " ON CONFLICT (household_id, period) DO UPDATE"
                " SET amount_minor = EXCLUDED.amount_minor, currency = EXCLUDED.currency",
                amount=int(budget_minor),
            )
            budget_set = not had
        return {
            "constraints": constraints,
            "budget_minor": budget_minor,
            "budget_set": budget_set,
            "join_codes": join_codes,
            "not_saved": not_saved,
        }

    async def call() -> dict[str, Any]:
        async with db.get_pool().connection() as conn, conn.transaction():
            q = _scoped(conn, household)
            found = await _proposal(conn, household, proposal_id, lock=True)
            if found is None:  # checked by the limits; never in practice
                return result("missing", "expired", {})
            status, expired, stored = found
            if decision == "yes":
                if status in ("applied", "awaiting_remove"):
                    return result(status, "already_decided", stored)
                if status != "pending" or expired:
                    return result(status, "expired", stored)
                stored = {**stored, "applied": await save(conn, stored), "removal_decisions": {}}
                status = "awaiting_remove" if setup_removals(stored) else "applied"
            elif decision == "no":
                if status != "pending":
                    return result(status, "already_decided", stored)
                status = "discarded"
            else:
                assert removal is not None
                decided = dict(stored.get("removal_decisions") or {})
                if status != "awaiting_remove" or str(removal) in decided:
                    return result(status, "already_decided", stored)
                target = setup_removals(stored)[removal]
                if decision == "remove":
                    await q.execute(
                        "DELETE FROM member_constraints WHERE user_id = %(user)s"
                        " AND user_id IN (SELECT user_id FROM household_members"
                        " WHERE household_id = %(household)s)"
                        " AND kind = %(kind)s AND value = %(value)s",
                        user=uuid.UUID(target["user_id"]),
                        kind=target["kind"],
                        value=target["value"],
                    )
                decided[str(removal)] = decision
                stored = {**stored, "removal_decisions": decided}
                if len(decided) == len(setup_removals(stored)):
                    status = "applied"
            await q.execute(
                "UPDATE onboarding_proposals SET status = %(status)s, answer = %(answer)s,"
                " decided_by = %(user)s, decided_at = now()"
                " WHERE proposal_id = %(proposal)s AND household_id = %(household)s",
                status=status,
                answer=Jsonb(stored),
                user=user_id,
                proposal=proposal_id,
            )
        return result(status, "done", stored)

    return await run_guarded(
        ctx,
        tool=f"{AREA}.apply_setup",
        relation=None,
        limits=check,
        key=None,
        request={
            "proposal_id": str(proposal_id),
            "answer": decision,
            "removal": removal,
            "child_user_ids": {str(k): str(v) for k, v in child_user_ids.items()},
            "user_id": str(user_id),
        },
        call=call,
        apply=None,
    )


async def record_pantry_check(
    ctx: CallContext, *, pantry_item: str, kind: str, user_id: uuid.UUID
) -> dict[str, Any]:
    """An adult's "Still have it" or "Ran out" on `/me/pantry` (experience §8, EX-I39), from
    `api` with its service token. `user_id` is the signed-in person, checked to be an adult
    of the run's household (a child is `not_adult`, anyone else `authz_denied`). `pantry_item`
    is a vocabulary name as written, not an alias. A household whose pantry is off is refused
    `pantry_off`. `delta` is 0, so `get_pantry`'s `items` do not change.
    """
    household = await _run_household(ctx)

    async def check(conn: AsyncConnection) -> None:
        if ctx.agent_id != API_AGENT:
            raise GuardRefused(AUTHZ_DENIED)
        if kind not in _PANTRY_CHECK_KINDS:
            raise GuardRefused(INVALID_KIND)
        role = await _role(conn, household, user_id)
        if role is None:
            raise GuardRefused(AUTHZ_DENIED)
        if role not in _ADULT_ROLES:
            raise GuardRefused(NOT_ADULT)
        if not pantry.is_enabled(ctx.env):
            raise GuardRefused(PANTRY_OFF)
        if pantry_item not in pantry.load_vocabulary().window_days:
            raise GuardRefused(UNKNOWN_PANTRY_ITEM)

    async def call() -> dict[str, Any]:
        async with db.get_pool().connection() as conn, conn.transaction():
            cur = await _scoped(conn, household).execute(
                "INSERT INTO pantry_events (household_id, item, pantry_item, unit, delta, kind,"
                " source, run_id, actor_user_id)"
                " VALUES (%(household)s, %(item)s, %(item)s, 'each', 0, %(kind)s, %(source)s,"
                " %(run)s, %(user)s) RETURNING event_id",
                item=pantry_item,
                kind=kind,
                source=_PANTRY_CHECK_SOURCE,
                run=ctx.run_id,
                user=user_id,
            )
            row = await cur.fetchone()
        assert row is not None
        return {"event_id": row[0], "pantry_item": pantry_item, "kind": kind}

    return await run_guarded(
        ctx,
        tool=f"{AREA}.record_pantry_check",
        relation=None,
        limits=check,
        key=None,
        request={"pantry_item": pantry_item, "kind": kind, "user_id": str(user_id)},
        call=call,
        apply=None,
    )


async def complete_join(ctx: CallContext, *, code: str, user_id: uuid.UUID) -> dict[str, Any]:
    """Finish a join (experience §9, EX-I31), from `api` with its service token. `user_id`
    is the new user: a member of the run's household whose phone is the code's.

    In one transaction, moves the code's `pending` into `member_constraints`
    (`source = 'join'`) and marks the code used. A code of another household, another
    phone, expired or used by someone else is refused `join_code_invalid`. The same user
    repeating a used code gets `outcome: already_joined` and nothing is written.
    """
    household = await _run_household(ctx)

    async def find(conn: AsyncConnection, *, lock: bool = False) -> tuple[Any, ...] | None:
        """`(pending, used_by, expired)` if the code is the household's and the user's."""
        rows = await _scoped(conn, household).fetch(
            "SELECT j.pending, j.used_by, j.expires_at <= now() FROM join_codes j"
            " JOIN channel_identities ci ON ci.phone_e164 = j.phone_e164"
            " AND ci.user_id = %(user)s"
            " JOIN household_members m ON m.user_id = ci.user_id"
            " AND m.household_id = j.household_id"
            " WHERE j.code = %(code)s AND j.household_id = %(household)s"
            + (" FOR UPDATE OF j" if lock else ""),
            code=code,
            user=user_id,
        )
        return tuple(rows[0]) if rows else None

    def refused(found: tuple[Any, ...] | None) -> bool:
        if found is None:
            return True
        _, used_by, expired = found
        return used_by != user_id if used_by is not None else bool(expired)

    async def check(conn: AsyncConnection) -> None:
        if ctx.agent_id != API_AGENT:
            raise GuardRefused(AUTHZ_DENIED)
        _require_household(household)
        if refused(await find(conn)):
            raise GuardRefused(JOIN_CODE_INVALID)

    async def call() -> dict[str, Any]:
        async with db.get_pool().connection() as conn, conn.transaction():
            q = _scoped(conn, household)
            found = await find(conn, lock=True)
            if found is None or refused(found):  # checked by the limits; a lost race only
                raise GuardRefused(JOIN_CODE_INVALID)
            pending, used_by, _ = found
            if used_by is not None:
                return {"outcome": "already_joined"}
            codes = [("allergy", c) for c in pending["allergies"]] + [
                ("diet", c) for c in pending["diets"]
            ]
            for kind, value in codes:
                await q.execute(
                    "INSERT INTO member_constraints (user_id, kind, value, source)"
                    " SELECT m.user_id, %(kind)s, %(value)s, 'join'"
                    " FROM household_members m"
                    " WHERE m.household_id = %(household)s AND m.user_id = %(user)s"
                    " AND NOT EXISTS (SELECT 1 FROM member_constraints c"
                    " WHERE c.user_id = m.user_id AND c.kind = %(kind)s"
                    " AND c.value = %(value)s)",
                    user=user_id,
                    kind=kind,
                    value=value,
                )
            await q.execute(
                "UPDATE join_codes SET used_at = now(), used_by = %(user)s"
                " WHERE code = %(code)s AND household_id = %(household)s",
                user=user_id,
                code=code,
            )
        return {"outcome": "joined"}

    return await run_guarded(
        ctx,
        tool=f"{AREA}.complete_join",
        relation=None,
        limits=check,
        key=None,
        request={"code": code, "user_id": str(user_id)},
        call=call,
        apply=None,
    )


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


@server.tool(name="get_setup_state")
async def _get_setup_state_tool(ctx: Context) -> CallToolResult:
    return await _serve(ctx, get_setup_state)


@server.tool(name="propose_setup")
async def _propose_setup_tool(answer: dict[str, Any], ctx: Context) -> CallToolResult:
    return await _serve(ctx, propose_setup, answer=answer)


@server.tool(name="apply_setup")
async def _apply_setup_tool(
    proposal_id: uuid.UUID,
    answer: Literal["yes", "no", "remove", "keep"],
    removal: int | None,
    child_user_ids: dict[str, uuid.UUID],
    user_id: uuid.UUID,
    ctx: Context,
) -> CallToolResult:
    return await _serve(
        ctx,
        apply_setup,
        proposal_id=proposal_id,
        answer=answer,
        removal=removal,
        child_user_ids=child_user_ids,
        user_id=user_id,
    )


@server.tool(name="record_pantry_check")
async def _record_pantry_check_tool(
    pantry_item: str, kind: Literal["verify", "out"], user_id: uuid.UUID, ctx: Context
) -> CallToolResult:
    return await _serve(
        ctx, record_pantry_check, pantry_item=pantry_item, kind=kind, user_id=user_id
    )


@server.tool(name="complete_join")
async def _complete_join_tool(code: str, user_id: uuid.UUID, ctx: Context) -> CallToolResult:
    return await _serve(ctx, complete_join, code=code, user_id=user_id)


@server.tool(name="authorize_amendment")
async def _authorize_amendment_tool(
    order_id: uuid.UUID, shortfall_id: uuid.UUID, picks: dict[int, str], ctx: Context
) -> CallToolResult:
    return await _serve(
        ctx, authorize_amendment, order_id=order_id, shortfall_id=shortfall_id, picks=picks
    )
