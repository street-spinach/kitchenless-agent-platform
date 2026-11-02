"""Guard limits: budget, allergens, approver, currency, coverage, window and `a2a_demo`.

Architecture item 51 (amended 2026-11-02, #44) and agent-protocol P28 (the
`authorize_spend` part). Every limit is enforced in code and raises a `GuardRefused`
subclass with a fixed wire reason, so a broken limit fails closed through the guard.

`authorize_spend()` runs the `authorize_spend` limits in the §5 order: allergens, budget,
currency, `a2a_demo`.
"""

import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from psycopg import AsyncConnection

from services.common import money, orders
from services.common.directory import AgentCard
from services.common.money import Money
from services.mcp.knowledge.adapters.neo4j_knowledge import Knowledge
from services.safety.guard import GuardRefused

__all__ = [
    "A2ADemoOrder",
    "AllergenConflict",
    "AllergenUnknown",
    "CurrencyMismatch",
    "KnowledgeUnavailable",
    "NeedsApproval",
    "OutOfZone",
    "OutsideWindow",
    "WrongApprover",
    "authorize_spend",
    "check_a2a_demo",
    "check_allergens",
    "check_approver",
    "check_budget",
    "check_coverage",
    "check_currency",
    "check_window",
]


class NeedsApproval(GuardRefused):
    def __init__(self) -> None:
        super().__init__("needs_approval")


class WrongApprover(GuardRefused):
    def __init__(self) -> None:
        super().__init__("wrong_approver")


class AllergenConflict(GuardRefused):
    def __init__(self, sku: str, code: str) -> None:
        super().__init__("allergen_conflict")
        self.sku = sku
        self.code = code


class AllergenUnknown(GuardRefused):
    """The SKU is not in the knowledge graph, so it could not be checked."""

    def __init__(self, sku: str) -> None:
        super().__init__("allergen_unknown")
        self.sku = sku


class KnowledgeUnavailable(GuardRefused):
    def __init__(self) -> None:
        super().__init__("knowledge_unavailable")


class CurrencyMismatch(GuardRefused):
    def __init__(self) -> None:
        super().__init__("currency_mismatch")


class OutOfZone(GuardRefused):
    def __init__(self) -> None:
        super().__init__("out_of_zone")


class OutsideWindow(GuardRefused):
    def __init__(self) -> None:
        super().__init__("outside_window")


class A2ADemoOrder(GuardRefused):
    def __init__(self) -> None:
        super().__init__("a2a_demo_order")


class WindowLike(Protocol):
    """The protocol `Window` (`services/common/protocol/schemas.py`)."""

    @property
    def start(self) -> datetime: ...
    @property
    def end(self) -> datetime: ...


def _sku(item: Any) -> str:
    return str(item["sku"] if isinstance(item, Mapping) else item.sku)


async def check_budget(
    conn: AsyncConnection, household_id: str, order_id: uuid.UUID, total: Money
) -> None:
    """Raise `NeedsApproval` when `total` is over the remaining weekly budget.

    An order is covered when it has an `approved` approval with `amount_minor >= total`
    in the same currency; this also covers a re-quoted offer at or below the approved
    total. A total in another currency than the budget raises `CurrencyMismatch`.
    """
    remaining = await orders.remaining_weekly_budget(conn, household_id)
    try:
        over = total > remaining
    except money.CurrencyMismatch:
        raise CurrencyMismatch() from None
    if not over:
        return
    cur = await conn.execute(
        "SELECT 1 FROM approvals WHERE order_id = %s AND household_id = %s"
        " AND status = 'approved' AND amount_minor >= %s AND currency = %s LIMIT 1",
        (order_id, household_id, total.amount_minor, total.currency),
    )
    if await cur.fetchone() is None:
        raise NeedsApproval()


async def check_allergens(
    items: Iterable[Any], member_allergens: Mapping[Any, Iterable[str]], knowledge: Knowledge
) -> None:
    """Check `items` (each with a `sku`) against every member's allergen codes.

    `member_allergens` maps each member to their allergen codes. With no codes at all the
    knowledge graph is not called. The first hit raises `AllergenConflict(sku, code)`;
    with no hits, the first unknown SKU raises `AllergenUnknown(sku)`. Any exception from
    the knowledge call raises `KnowledgeUnavailable` (fails closed).
    """
    codes = sorted({code for member in member_allergens.values() for code in member})
    if not codes:
        return
    skus = list(dict.fromkeys(_sku(item) for item in items))
    try:
        result = await knowledge.check_allergens(skus, codes)
    except Exception:
        raise KnowledgeUnavailable() from None
    if result.hits:
        hit = result.hits[0]
        raise AllergenConflict(hit.sku, hit.allergen_code)
    if result.unknown_skus:
        raise AllergenUnknown(result.unknown_skus[0])


async def check_approver(conn: AsyncConnection, household_id: str, user_id: Any) -> None:
    """Raise `WrongApprover` unless `user_id` is the household's `approval_rules` approver."""
    cur = await conn.execute(
        "SELECT approver_user_id FROM approval_rules WHERE household_id = %s", (household_id,)
    )
    row = await cur.fetchone()
    if row is None or str(row[0]) != str(user_id):
        raise WrongApprover()


async def check_currency(conn: AsyncConnection, order_id: uuid.UUID, total: Money) -> None:
    """Raise `CurrencyMismatch` unless `total` is in `orders.currency` (also for no order)."""
    cur = await conn.execute("SELECT currency FROM orders WHERE order_id = %s", (order_id,))
    row = await cur.fetchone()
    if row is None or str(row[0]).strip() != total.currency:
        raise CurrencyMismatch()


async def check_a2a_demo(conn: AsyncConnection, order_id: uuid.UUID) -> None:
    """Raise `A2ADemoOrder` when the order's run is an `a2a_demo` run (or has no run)."""
    cur = await conn.execute(
        "SELECT r.kind FROM orders o JOIN runs r ON r.run_id = o.run_id WHERE o.order_id = %s",
        (order_id,),
    )
    row = await cur.fetchone()
    if row is None or row[0] == "a2a_demo":
        raise A2ADemoOrder()


def check_coverage(card: AgentCard, zone_id: str) -> None:
    """Raise `OutOfZone` unless `zone_id in card.zone_ids`."""
    if zone_id not in card.zone_ids:
        raise OutOfZone()


def check_window(window: WindowLike, now: datetime) -> None:
    """Raise `OutsideWindow` unless `window.start < window.end` and `window.end > now`.

    Per-card hours and capacity are not checked here (architecture item 51, amended
    2026-11-02). Naive datetimes are refused.
    """
    times = (window.start, window.end, now)
    if any(t.tzinfo is None or t.utcoffset() is None for t in times):
        raise OutsideWindow()
    if not (window.start < window.end and window.end > now):
        raise OutsideWindow()


async def authorize_spend(
    conn: AsyncConnection,
    *,
    household_id: str,
    order_id: uuid.UUID,
    items: Sequence[Any],
    total: Money,
    member_allergens: Mapping[Any, Iterable[str]],
    knowledge: Knowledge,
) -> None:
    """The `authorize_spend` limits, in the §5 order: allergens, budget, currency, `a2a_demo`."""
    await check_allergens(items, member_allergens, knowledge)
    await check_budget(conn, household_id, order_id, total)
    await check_currency(conn, order_id, total)
    await check_a2a_demo(conn, order_id)
