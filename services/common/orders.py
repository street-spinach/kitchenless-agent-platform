"""Order state changes, the items hash, the weekly budget and amendments.

Architecture item 25 (amended by agent-protocol A19) and agent-protocol P15. Every state
change is a conditional update (`WHERE state = ANY(<expected>)`) and must be in `ALLOWED`
(architecture §8, amended by A7).
"""

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row

from services.common.money import Money

MAIN_PATH = (
    "draft",
    "quoting",
    "negotiating",
    "awaiting_approval",
    "authorized",
    "placed",
    "courier_booked",
    "in_delivery",
    "delivered",
)
TERMINAL = frozenset({"delivered", "cancelled", "expired", "failed"})
STATES = frozenset(MAIN_PATH) | TERMINAL
BEFORE_PLACED = MAIN_PATH[: MAIN_PATH.index("placed")]
# The budget counts orders that reached `placed` (architecture §8, Approval).
PLACED_OR_LATER = ("placed", "courier_booked", "in_delivery", "delivered")


def _allowed() -> frozenset[tuple[str, str]]:
    pairs = set(zip(MAIN_PATH, MAIN_PATH[1:], strict=False))
    pairs.add(("negotiating", "authorized"))  # approval is skipped when within budget
    pairs.add(("authorized", "quoting"))  # re-quote after an offer expired (A7)
    pairs.update((state, "cancelled") for state in BEFORE_PLACED)
    pairs.add(("awaiting_approval", "expired"))
    pairs.update((state, "failed") for state in STATES - TERMINAL)
    return frozenset(pairs)


# (from, to) pairs. `authorized → quoting` is allowed once per run; the household agent
# keeps that count (agent-protocol §7).
ALLOWED: frozenset[tuple[str, str]] = _allowed()

# Who may move an order into each state (architecture §8 "Who changes the order state",
# amended by A7: `failed` also by the seller through `amend_order`).
CHANGED_BY: Mapping[str, frozenset[str]] = {
    **{state: frozenset({"household"}) for state in BEFORE_PLACED + ("authorized",)},
    "cancelled": frozenset({"household"}),
    "placed": frozenset({"place_order"}),
    "courier_booked": frozenset({"book_delivery"}),
    "expired": frozenset({"sweeper"}),
    "in_delivery": frozenset({"apply_delivery_status"}),
    "delivered": frozenset({"apply_delivery_status"}),
    "failed": frozenset({"apply_delivery_status", "amend_order"}),
}

# Courier status → order state; `None` means no change (architecture §8, Q6).
COURIER_STATUS_MAP: Mapping[str, str | None] = {
    "booked": None,
    "created": None,
    "confirmed": None,
    "enroute_to_pickup": None,
    "arrived_at_pickup": None,
    "picked_up": "in_delivery",
    "enroute_to_dropoff": "in_delivery",
    "arrived_at_dropoff": "in_delivery",
    "delivered": "delivered",
    "failed": "failed",
    "cancelled": "failed",
}


async def transition(
    conn: AsyncConnection,
    order_id: UUID,
    expected: str | Iterable[str],
    new: str,
    **fields: Any,
) -> bool:
    """Move `order_id` to `new` if its state is one of `expected`; return whether it moved.

    Raises `ValueError` for a transition that is not in `ALLOWED`. `fields` are extra
    `orders` columns set in the same update.
    """
    expected_states = [expected] if isinstance(expected, str) else list(expected)
    if not expected_states:
        raise ValueError("expected is empty")
    refused = [state for state in expected_states if (state, new) not in ALLOWED]
    if refused:
        raise ValueError(f"transition not allowed: {refused} -> {new}")
    if "state" in fields or "order_id" in fields:
        raise ValueError("state and order_id cannot be set through fields")
    sets = [sql.SQL("state = %s"), sql.SQL("updated_at = now()")]
    sets += [sql.SQL("{} = %s").format(sql.Identifier(name)) for name in fields]
    query = sql.SQL("UPDATE orders SET {} WHERE order_id = %s AND state = ANY(%s)").format(
        sql.SQL(", ").join(sets)
    )
    cur = await conn.execute(query, (new, *fields.values(), order_id, expected_states))
    return cur.rowcount == 1


def _qty(qty: Any) -> str:
    """Canonical text for a quantity, so `2`, `2.0` and `Decimal('2.000')` hash alike."""
    return format(Decimal(str(qty)).normalize(), "f")


def _get(item: Any, name: str) -> Any:
    return item[name] if isinstance(item, Mapping) else getattr(item, name)


def items_hash(items: Iterable[Any]) -> str:
    """SHA-256 of the sorted `(sku, qty, unit_minor)` lines.

    `items` are mappings or objects with `sku`, `qty` and `unit_minor`.
    """
    lines = sorted(
        (str(_get(i, "sku")), _qty(_get(i, "qty")), int(_get(i, "unit_minor"))) for i in items
    )
    payload = json.dumps(lines, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


async def remaining_weekly_budget(
    conn: AsyncConnection, household_id: str, *, now: datetime | None = None
) -> Money:
    """The weekly budget minus this week's orders that are `placed` or later.

    The budget week starts Monday 00:00 in the household's time zone (architecture §3).
    `now` defaults to the database clock.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT b.amount_minor, b.currency,
                   COALESCE((
                     SELECT sum(o.total_minor) FROM orders o
                     WHERE o.household_id = h.household_id
                       AND o.state = ANY(%(states)s)
                       AND o.created_at >= (date_trunc('week', t.now AT TIME ZONE h.timezone)
                                            AT TIME ZONE h.timezone)
                   ), 0)::bigint AS spent
            FROM households h
            JOIN budgets b ON b.household_id = h.household_id AND b.period = 'weekly'
            CROSS JOIN (SELECT COALESCE(%(now)s::timestamptz, now()) AS now) t
            WHERE h.household_id = %(household_id)s
            """,
            {"states": list(PLACED_OR_LATER), "now": now, "household_id": household_id},
        )
        row = await cur.fetchone()
    if row is None:
        raise LookupError(f"no weekly budget for household {household_id}")
    currency = row["currency"].strip()
    return Money(int(row["amount_minor"]), currency) - Money(int(row["spent"]), currency)


@dataclass(frozen=True)
class AmendResult:
    """The order after `apply_amendment`."""

    lines: list[dict[str, Any]]  # remaining order_items rows, by line
    total_minor: int
    items_hash: str
    state: str
    substituted: list[int] = field(default_factory=list)
    dropped: list[int] = field(default_factory=list)


def _int_qty(qty: Any) -> int:
    value = Decimal(str(qty))
    if value != value.to_integral_value():
        raise ValueError(f"quantity must be whole, got {qty!r}")
    return int(value)


async def apply_amendment(
    conn: AsyncConnection,
    order_id: UUID,
    picks: Mapping[int, str],
    notice_lines: Sequence[Any],
) -> AmendResult:
    """Apply shortfall picks to an order in the caller's transaction (agent-protocol P15).

    `picks` maps a line to a substitute SKU or `"drop"`; `notice_lines` are the
    `shortfall_notice` lines (`ShortfallLine` or mappings). A notice line with no pick is
    dropped, because the store cannot deliver it. A substituted line takes the substitute's
    SKU, name, quantity and price, and `substituted_for` = the SKU it replaces. Dropped
    lines are removed. `total_minor` and `items_hash` are recomputed; when no line remains
    the order moves to `failed`.
    """
    picks = {int(line): pick for line, pick in picks.items()}
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT state FROM orders WHERE order_id = %s FOR UPDATE", (order_id,))
        order = await cur.fetchone()
        if order is None:
            raise LookupError(f"order {order_id} not found")
        await cur.execute(
            "SELECT line, sku FROM order_items WHERE order_id = %s ORDER BY line", (order_id,)
        )
        existing = {row["line"]: row["sku"] for row in await cur.fetchall()}

        unknown = set(picks) - {int(_get(n, "line")) for n in notice_lines}
        if unknown:
            raise ValueError(f"picks for lines not in the notice: {sorted(unknown)}")

        substituted: list[int] = []
        dropped: list[int] = []
        for notice in notice_lines:
            line = int(_get(notice, "line"))
            if line not in existing:
                raise ValueError(f"line {line} is not on order {order_id}")
            pick = picks.get(line, "drop")
            if pick == "drop":
                await cur.execute(
                    "DELETE FROM order_items WHERE order_id = %s AND line = %s", (order_id, line)
                )
                dropped.append(line)
                continue
            subs = {_get(s, "sku"): s for s in _get(notice, "substitutes") or []}
            if pick not in subs:
                raise ValueError(f"pick {pick!r} is not a substitute for line {line}")
            sub = subs[pick]
            await cur.execute(
                "UPDATE order_items SET sku = %s, name = %s, qty = %s, unit_minor = %s,"
                " substituted_for = %s WHERE order_id = %s AND line = %s",
                (
                    _get(sub, "sku"),
                    _get(sub, "name"),
                    _get(sub, "qty"),
                    _get(sub, "unit_minor"),
                    existing[line],
                    order_id,
                    line,
                ),
            )
            substituted.append(line)

        await cur.execute(
            "SELECT line, sku, name, qty, unit_minor, substituted_for FROM order_items"
            " WHERE order_id = %s ORDER BY line",
            (order_id,),
        )
        lines = await cur.fetchall()
        total = sum(_int_qty(row["qty"]) * int(row["unit_minor"]) for row in lines)
        new_hash = items_hash(lines)
        await cur.execute(
            "UPDATE orders SET total_minor = %s, items_hash = %s, updated_at = now()"
            " WHERE order_id = %s",
            (total, new_hash, order_id),
        )

    state = order["state"]
    if not lines:
        if not await transition(conn, order_id, state, "failed"):
            raise RuntimeError(f"order {order_id} changed state during the amendment")
        state = "failed"
    return AmendResult(
        lines=lines,
        total_minor=total,
        items_hash=new_hash,
        state=state,
        substituted=substituted,
        dropped=dropped,
    )
