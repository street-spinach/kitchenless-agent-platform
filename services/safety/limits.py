"""Guard limits: budget, allergens, approver, currency, coverage, window and `a2a_demo`.

Architecture item 51 (amended 2026-11-02, #44) and agent-protocol P28 (the
`authorize_spend` part). Every limit is enforced in code and raises a `GuardRefused`
subclass with a fixed wire reason, so a broken limit fails closed through the guard.

`authorize_spend()` runs the `authorize_spend` limits in the §5 order (amended by agents
AG-A8, #186, and identity-authz §8 step 3): allergens, approval needed (the band and the
budget as one check, `check_approval_needed`, identity-authz §9), child requester,
currency, `a2a_demo`.

`authorize_amendment()` and `check_amend_order()` are the shortfall amendment limits
(agent-protocol §10 "Applying" and "Amending", P28, #67).
"""

import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from psycopg import AsyncConnection

from services.common import ids, money, orders
from services.common.directory import AgentCard
from services.common.money import Money
from services.mcp.knowledge.adapters.neo4j_knowledge import Knowledge
from services.safety.guard import GuardRefused
from services.safety.scope import unscoped

__all__ = [
    "A2ADemoOrder",
    "AllergenConflict",
    "AllergenUnknown",
    "AmendmentNotAuthorized",
    "CurrencyMismatch",
    "KnowledgeUnavailable",
    "NeedsApproval",
    "OutOfZone",
    "OutsideWindow",
    "PickNotInNotice",
    "ShortfallNotFound",
    "ShortfallPending",
    "WrongApprover",
    "WrongOrderState",
    "approval_limits",
    "authorize_amendment",
    "authorize_spend",
    "check_amend_order",
    "check_a2a_demo",
    "check_allergens",
    "check_approval_needed",
    "check_approver",
    "check_budget",
    "check_child_requester",
    "check_coverage",
    "check_currency",
    "check_window",
]


class NeedsApproval(GuardRefused):
    """`detail.limits` names the limits that were hit (`band`, `budget`), computed in code
    (identity-authz §9); empty for the child-requester rule."""

    def __init__(self, hit: Sequence[str] = ()) -> None:
        super().__init__("needs_approval")
        self.detail: dict[str, list[str]] = {"limits": list(hit)}


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


class ShortfallNotFound(GuardRefused):
    """No `shortfalls` row for this shortfall, order (and, for `amend_order`, seller)."""

    def __init__(self) -> None:
        super().__init__("shortfall_not_found")


class ShortfallPending(GuardRefused):
    """The shortfall is still `pending`: members may still pick."""

    def __init__(self) -> None:
        super().__init__("shortfall_pending")


class PickNotInNotice(GuardRefused):
    """A pick names a line, or a substitute SKU, that is not in the shortfall notice."""

    def __init__(self, line: int) -> None:
        super().__init__("pick_not_in_notice")
        self.line = line


class WrongOrderState(GuardRefused):
    """The order is in no state an amendment may change (not before `delivered`)."""

    def __init__(self, state: str | None) -> None:
        super().__init__("wrong_order_state")
        self.state = state


class AmendmentNotAuthorized(GuardRefused):
    """`amend_order` picks differ from the stored `authorize_amendment` result (or none)."""

    def __init__(self) -> None:
        super().__init__("amendment_not_authorized")


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
    if not await _approval_covers(conn, household_id, order_id, total):
        raise NeedsApproval([LIMIT_BUDGET])


LIMIT_BAND = "band"
LIMIT_BUDGET = "budget"


async def _band(conn: AsyncConnection, household_id: str) -> int | None:
    """`approval_rules.per_order_minor`, or `None` when it is null or there are no rules."""
    cur = await unscoped(conn, "limits").execute(
        "SELECT per_order_minor FROM approval_rules WHERE household_id = %s", (household_id,)
    )
    row = await cur.fetchone()
    return None if row is None or row[0] is None else int(row[0])


async def approval_limits(
    conn: AsyncConnection, household_id: str, total: Money, *, require_budget: bool = True
) -> list[str]:
    """The limits `total` is over (identity-authz §9), in the order `band`, `budget`.

    `band`: the household's `per_order_minor` is set and `total` is above it (in the
    household's currency, the budget's). `budget`: `total` is above what is left of the
    weekly budget. A total in another currency than the budget raises `CurrencyMismatch`.

    A household with no weekly budget raises `LookupError` (the spend check fails closed).
    `request_approval`, which only records the limits for an approval it asks anyway,
    passes `require_budget=False`: no budget then means no `budget` limit.
    """
    try:
        remaining: Money | None = await orders.remaining_weekly_budget(conn, household_id)
    except LookupError:
        if require_budget:
            raise
        remaining = None
    try:
        over_budget = remaining is not None and total > remaining
    except money.CurrencyMismatch:
        raise CurrencyMismatch() from None
    band = await _band(conn, household_id)
    hit: list[str] = []
    if band is not None and total.amount_minor > band:
        hit.append(LIMIT_BAND)
    if over_budget:
        hit.append(LIMIT_BUDGET)
    return hit


async def check_approval_needed(
    conn: AsyncConnection, household_id: str, order_id: uuid.UUID, total: Money
) -> None:
    """One approval, two triggers (identity-authz §9, I20).

    Raise `NeedsApproval` with `detail.limits` when `total` is over the band, the budget or
    both, unless the order has an `approved` approval of at least `total` in the same
    currency (a re-quote at or below the approved total stays covered).
    """
    hit = await approval_limits(conn, household_id, total)
    if hit and not await _approval_covers(conn, household_id, order_id, total):
        raise NeedsApproval(hit)


async def _approval_covers(
    conn: AsyncConnection, household_id: str, order_id: uuid.UUID, total: Money
) -> bool:
    """True when the order has an `approved` approval of at least `total`, same currency."""
    cur = await unscoped(conn, "limits").execute(
        "SELECT 1 FROM approvals WHERE order_id = %s AND household_id = %s"
        " AND status = 'approved' AND amount_minor >= %s AND currency = %s LIMIT 1",
        (order_id, household_id, total.amount_minor, total.currency),
    )
    return await cur.fetchone() is not None


async def check_child_requester(
    conn: AsyncConnection, household_id: str, order_id: uuid.UUID, total: Money
) -> None:
    """Raise `NeedsApproval` when a `child` member started the order's run (agents §9).

    The requester is the run's `trigger.user_id`. It applies to every order a child
    starts, whatever its total or kind, unless an `approved` approval for the order covers
    `total` (the same rule as `check_budget`).
    """
    cur = await unscoped(conn, "limits").execute(
        "SELECT hm.role FROM orders o JOIN runs r ON r.run_id = o.run_id"
        " JOIN household_members hm ON hm.household_id = o.household_id"
        " AND hm.user_id::text = r.trigger->>'user_id'"
        " WHERE o.order_id = %s",
        (order_id,),
    )
    row = await cur.fetchone()
    if row is None or row[0] != "child":
        return
    if not await _approval_covers(conn, household_id, order_id, total):
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
    cur = await unscoped(conn, "limits").execute(
        "SELECT approver_user_id FROM approval_rules WHERE household_id = %s", (household_id,)
    )
    row = await cur.fetchone()
    if row is None or str(row[0]) != str(user_id):
        raise WrongApprover()


async def check_currency(conn: AsyncConnection, order_id: uuid.UUID, total: Money) -> None:
    """Raise `CurrencyMismatch` unless `total` is in `orders.currency` (also for no order)."""
    cur = await unscoped(conn, "limits").execute(
        "SELECT currency FROM orders WHERE order_id = %s", (order_id,)
    )
    row = await cur.fetchone()
    if row is None or str(row[0]).strip() != total.currency:
        raise CurrencyMismatch()


async def check_a2a_demo(conn: AsyncConnection, order_id: uuid.UUID) -> None:
    """Raise `A2ADemoOrder` when the order's run is an `a2a_demo` run (or has no run)."""
    cur = await unscoped(conn, "limits").execute(
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
    """The `authorize_spend` limits, in order: allergens, approval needed (band and budget,
    identity-authz §9), child, currency, `a2a_demo`."""
    await check_allergens(items, member_allergens, knowledge)
    await check_approval_needed(conn, household_id, order_id, total)
    await check_child_requester(conn, household_id, order_id, total)
    await check_currency(conn, order_id, total)
    await check_a2a_demo(conn, order_id)


# Shortfall amendments (agent-protocol §10 "Applying" and "Amending", P28).

DROP = "drop"
# Substitutes need `courier_booked`; drops need a state before `delivered`. A shortfall only
# exists once the courier is booked, so the states before `delivered` are these two.
SUBSTITUTE_STATES = frozenset({"courier_booked"})
DROP_STATES = frozenset({"courier_booked", "in_delivery"})


def _get(obj: Any, name: str) -> Any:
    return obj[name] if isinstance(obj, Mapping) else getattr(obj, name)


def _normalize_picks(picks: Mapping[Any, Any]) -> dict[int, str]:
    return {int(line): str(pick) for line, pick in picks.items()}


def _within_line_total(substitute: Any, line_total_minor: Any) -> bool:
    try:
        cost = Decimal(str(_get(substitute, "qty"))) * Decimal(str(_get(substitute, "unit_minor")))
        return cost <= Decimal(str(line_total_minor))
    except (InvalidOperation, KeyError, AttributeError, TypeError, ValueError):
        return False


async def _allergen_refused(
    skus: Sequence[str], member_allergens: Mapping[Any, Iterable[str]], knowledge: Knowledge
) -> set[str]:
    """The SKUs to refuse: allergen hits and unknown SKUs; all of them if the graph fails."""
    codes = sorted({code for member in member_allergens.values() for code in member})
    if not codes or not skus:
        return set()
    try:
        result = await knowledge.check_allergens(list(skus), codes)
    except Exception:
        return set(skus)
    return {hit.sku for hit in result.hits} | set(result.unknown_skus)


async def authorize_amendment(
    conn: AsyncConnection,
    *,
    order_id: uuid.UUID,
    shortfall_id: uuid.UUID,
    picks: Mapping[Any, str],
    member_allergens: Mapping[Any, Iterable[str]],
    knowledge: Knowledge,
) -> dict[str, Any]:
    """The `authorize_amendment` limits. Returns `{picks, refused}` (§10).

    `picks` maps each picked line (as a string, the form stored under the idempotency key)
    to its final pick, with every refused substitute turned into `"drop"`; `refused` lists
    the refused lines in order.

    Raises `ShortfallNotFound` (no row for this shortfall and order), `ShortfallPending`,
    `PickNotInNotice` (a line or SKU not in the notice) and `WrongOrderState` (drops need
    a state before `delivered`). Then each substitute is refused, and becomes a drop, when:

    *   the order is not `courier_booked`;
    *   its `qty × unit_minor` is above its line's `line_total_minor`;
    *   it conflicts with any member's allergies, or is unknown to the knowledge graph, or
        the graph call fails (every substitute is then refused).
    """
    cur = await unscoped(conn, "limits").execute(
        "SELECT s.lines, s.status, o.state FROM shortfalls s"
        " JOIN orders o ON o.order_id = s.order_id"
        " WHERE s.shortfall_id = %s AND s.order_id = %s",
        (shortfall_id, order_id),
    )
    row = await cur.fetchone()
    if row is None:
        raise ShortfallNotFound()
    lines, status, state = row
    if status == "pending":
        raise ShortfallPending()

    notice = {int(_get(line, "line")): line for line in lines or []}
    final = _normalize_picks(picks)
    substitutes: dict[int, Any] = {}
    for line, pick in final.items():
        if line not in notice:
            raise PickNotInNotice(line)
        if pick == DROP:
            continue
        offered = {str(_get(s, "sku")): s for s in _get(notice[line], "substitutes") or []}
        if pick not in offered:
            raise PickNotInNotice(line)
        substitutes[line] = offered[pick]

    if state not in DROP_STATES:
        raise WrongOrderState(state)

    refused: set[int] = set()
    if state not in SUBSTITUTE_STATES:
        refused.update(substitutes)
    for line, sub in substitutes.items():
        if not _within_line_total(sub, _get(notice[line], "line_total_minor")):
            refused.add(line)

    remaining = {line: final[line] for line in substitutes if line not in refused}
    unsafe = await _allergen_refused(
        list(dict.fromkeys(remaining.values())), member_allergens, knowledge
    )
    refused.update(line for line, sku in remaining.items() if sku in unsafe)

    for line in refused:
        final[line] = DROP
    return {
        "picks": {str(line): pick for line, pick in sorted(final.items())},
        "refused": sorted(refused),
    }


async def check_amend_order(
    conn: AsyncConnection,
    *,
    order_id: uuid.UUID,
    shortfall_id: uuid.UUID,
    seller_agent_id: str,
    household_agent_id: str,
    picks: Mapping[Any, str],
) -> None:
    """The `amend_order` limits.

    The shortfall must exist for this seller and order (`ShortfallNotFound`), and `picks`
    must equal the `authorize_amendment` result stored under
    `idempotency_key(order_id, "authorize_amendment", household_agent_id,
    scope=shortfall_id)` (`AmendmentNotAuthorized`, also when there is no finished result).
    """
    cur = await unscoped(conn, "limits").execute(
        "SELECT 1 FROM shortfalls"
        " WHERE shortfall_id = %s AND order_id = %s AND seller_agent_id = %s",
        (shortfall_id, order_id, seller_agent_id),
    )
    if await cur.fetchone() is None:
        raise ShortfallNotFound()
    key = ids.idempotency_key(
        order_id, "authorize_amendment", household_agent_id, scope=shortfall_id
    )
    cur = await unscoped(conn, "limits").execute(
        "SELECT result FROM idempotency_keys WHERE key = %s AND status = 'done'", (key,)
    )
    row = await cur.fetchone()
    stored = row[0] if row is not None else None
    if not isinstance(stored, Mapping) or not isinstance(stored.get("picks"), Mapping):
        raise AmendmentNotAuthorized()
    try:
        authorized = _normalize_picks(stored["picks"])
        given = _normalize_picks(picks)
    except (TypeError, ValueError):
        raise AmendmentNotAuthorized() from None
    if given != authorized:
        raise AmendmentNotAuthorized()
