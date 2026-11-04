"""The story of one order (showcase.md §4): the facts of its agent work, shown to people.

`build_story(q, order_id)` reads rows the platform already writes, through `scope.scoped`,
and builds every sentence in code: no writes, no tool calls, no model (SG6). A member's name
never appears; allergen and diet codes show as `codes.LABELS` words, the union the order was
quoted with. `without_spending` removes what experience §4 keeps from an adult who didn't
start the order (per-order spending and approvals).

Where a check has no readable row:

*   Allergens: `limits.authorize_spend` checks them first, so they come from the
    `authorize_spend` guard rows.
*   Diets have no guard check: the seller's filtered search applies them, with the codes it
    saves on the quote, so they come from the chosen seller's `quotes.constraints`.
*   Budget from `orders.total_minor` against `budgets`; approval from `approvals`.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from services.common.codes import LABELS
from services.common.money import Money
from services.common.money import format as money_format
from services.safety import scope

__all__ = [
    "Behind",
    "Check",
    "CourierQuote",
    "SellerRow",
    "Story",
    "Summary",
    "Turn",
    "build_story",
    "without_spending",
]

LOCALE = "en"
YOUR_AGENT = "Your household agent"
YOUR_APPROVER = "your household's approver"
# Agents with no organisation name, by class. An agent id is never shown.
_UNNAMED = {"grocery": "A store", "restaurant": "A restaurant", "delivery": "A courier"}
_OWN_CLASSES = ("household", "consumer", "channel")

# Pills (showcase §3.1) as `(text, class)`, the class after `pill-` in me.css.
_PASSED = ("Passed", "ok")
_NOT_RUN = ("Not run yet", "quiet")
_REFUSED = ("Refused", "refused")
_WON = ("Won", "ok")
_NOT_CHOSEN = ("Not chosen", "quiet")
_NO_REPLY = ("No reply in time", "quiet")
_NO_OFFER = ("No offer", "quiet")
_WAITING = ("Waiting", "quiet")
_APPROVAL_PILLS = {
    "pending": ("Needs approval", "attention"),
    "approved": ("Approved", "ok"),
    "rejected": ("Refused", "refused"),
    "expired": ("Expired", "quiet"),
}
_STATE_PILLS = {
    "awaiting_approval": "attention",
    "cancelled": "refused",
    "failed": "refused",
    "expired": "quiet",
}

BUDGET_CHECK = "Budget"
APPROVAL_CHECK = "Approval"
AUTHORIZE_SPEND = "authorize_spend"
# `limits.authorize_spend` runs allergens first, so `needs_approval` means they passed.
_ALLERGEN_REFUSALS = ("allergen_conflict", "allergen_unknown")
_AFTER_ALLERGENS = "needs_approval"
# An order in these states is still being decided, so an open quote is not yet a loser.
_DECIDING = ("draft", "quoting", "negotiating", "awaiting_approval")

_STORY_MESSAGES = (
    "quote_request",
    "offer",
    "counter_offer",
    "accept",
    "reject",
    "delivery_quote",
    "delivery_booked",
)
_NEGOTIATION = ("offer", "counter_offer", "accept", "reject")


@dataclass(frozen=True)
class Summary:
    title: str
    seller: str | None
    state: str
    state_pill: str
    window: str | None
    total: str | None
    budget: str | None
    # The budget bar, in minor units; None when the two can't be compared.
    bar_value: int | None
    bar_max: int | None


@dataclass(frozen=True)
class SellerRow:
    name: str
    first_offer: str | None
    final_offer: str | None
    unavailable: int
    outcome: str
    outcome_pill: str


@dataclass(frozen=True)
class Turn:
    who: str
    action: str
    amount: str | None
    rationale: str | None
    dot: str


@dataclass(frozen=True)
class Check:
    name: str
    detail: str
    result: str
    result_pill: str


@dataclass(frozen=True)
class CourierQuote:
    name: str
    fee: str | None
    window: str | None
    chosen: bool


@dataclass(frozen=True)
class Behind:
    agents: int
    messages: int
    model_decisions: int
    rule_decisions: int
    tokens: int
    elapsed: str


@dataclass(frozen=True)
class Story:
    summary: Summary
    sellers: list[SellerRow]
    turns: list[Turn]
    checks: list[Check]
    couriers: list[CourierQuote]
    behind: Behind
    spending_hidden: bool = False


_ORDER_SQL = """
SELECT o.kind, o.state, o.total_minor, o.currency, o.seller_agent_id, o.delivery_agent_id,
       lower(o.delivery_window), upper(o.delivery_window), h.timezone
FROM orders o JOIN households h ON h.household_id = o.household_id
WHERE o.household_id = %(household)s AND o.order_id = %(order)s
"""

_BUDGET_SQL = """
SELECT amount_minor, currency FROM budgets
WHERE household_id = %(household)s AND period = 'weekly'
"""

# `quotes` also holds the couriers' quotes (their seller is the courier, their buyer the store).
_QUOTES_SQL = """
SELECT q.seller_agent_id, q.total_minor, q.currency, q.status, q.items, q.constraints
FROM quotes q
JOIN orders o ON o.order_id = q.order_id
JOIN agent_cards c ON c.agent_id = q.seller_agent_id
WHERE o.household_id = %(household)s AND o.order_id = %(order)s AND c.class <> 'delivery'
ORDER BY q.created_at, q.quote_id
"""

_MESSAGES_SQL = """
SELECT m.type, m.from_agent, m.to_agent, m.body
FROM agent_messages m
JOIN orders o ON o.run_id = m.run_id AND m.body->>'order_id' = o.order_id::text
WHERE o.household_id = %(household)s AND o.order_id = %(order)s AND m.type = ANY(%(types)s)
ORDER BY m.created_at, m.message_id
"""

_SPEND_CHECKS_SQL = """
SELECT (t.payload->>'refused')::boolean, t.payload->>'reason'
FROM agent_traces t JOIN orders o ON o.run_id = t.run_id
WHERE o.household_id = %(household)s AND o.order_id = %(order)s
  AND t.kind = 'tool_result' AND t.payload->>'tool' = %(tool)s
ORDER BY t.seq
"""

_LINES_SQL = """
SELECT count(*) FROM order_items i JOIN orders o ON o.order_id = i.order_id
WHERE o.household_id = %(household)s AND o.order_id = %(order)s
"""

_APPROVAL_SQL = """
SELECT status, decided_at FROM approvals
WHERE household_id = %(household)s AND order_id = %(order)s
ORDER BY requested_at DESC LIMIT 1
"""

# As the presenter view's totals: a decision with no `source` used the model if it names one.
_BEHIND_SQL = """
SELECT count(DISTINCT t.agent_id),
       count(*) FILTER (WHERE t.kind = 'message'),
       count(*) FILTER (WHERE t.kind = 'decision' AND coalesce(t.payload->>'source',
         CASE WHEN t.model IS NULL THEN 'rule_default' ELSE 'model' END) = 'model'),
       count(*) FILTER (WHERE t.kind = 'decision' AND coalesce(t.payload->>'source',
         CASE WHEN t.model IS NULL THEN 'rule_default' ELSE 'model' END) = 'rule_default'),
       coalesce(sum(coalesce(t.tokens_in, 0) + coalesce(t.tokens_out, 0)), 0),
       coalesce(extract(epoch FROM max(t.ts) - min(t.ts)), 0)
FROM agent_traces t JOIN orders o ON o.run_id = t.run_id
WHERE o.household_id = %(household)s AND o.order_id = %(order)s
"""

_CARDS_SQL = """
SELECT c.agent_id, c.class, g.name
FROM agent_cards c LEFT JOIN organizations g ON c.owner_ref = 'org:' || g.org_id
WHERE c.agent_id = ANY(%(agents)s) AND EXISTS (SELECT 1 FROM orders o
  WHERE o.household_id = %(household)s AND o.order_id = %(order)s)
"""


def _money(amount_minor: int, currency: str) -> str:
    return money_format(Money(int(amount_minor), str(currency).strip()), LOCALE)


def _clock(at: datetime) -> str:
    return f"{at:%I:%M %p}".lstrip("0").lower()


def _day(at: datetime) -> str:
    return f"{at:%a} {at.day} {at:%b}"


def _window(start: datetime | None, end: datetime | None, zone: ZoneInfo) -> str | None:
    if start is None or end is None:
        return None
    start, end = start.astimezone(zone), end.astimezone(zone)
    return f"{_day(start)} {_clock(start)}-{_clock(end)}"


def _elapsed(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 60} min {whole % 60} s" if whole >= 60 else f"{whole} s"


def _words(codes: Iterable[str]) -> list[str]:
    return sorted(LABELS.get(c, c.replace("_", " ")) for c in codes)


def _name(agent: str | None, cards: Mapping[str, tuple[str, str | None]]) -> str:
    cls, org = cards.get(agent or "", (None, None))
    if cls in _OWN_CLASSES:
        return YOUR_AGENT
    return org or _UNNAMED.get(cls or "", "An agent")


def _seller_rows(
    state: str,
    quotes: list[Any],
    messages: list[Any],
    cards: Mapping[str, tuple[str, str | None]],
) -> list[SellerRow]:
    """One row per seller asked or quoting, first asked first. A counter overwrites the
    quote's total, so the first offer comes from the seller's first `offer` message. A
    losing quote stays `open`; only the order's progress says it wasn't chosen."""
    by_seller: dict[str, list[Any]] = {}
    for message in messages:
        if message[0] == "quote_request":
            by_seller.setdefault(message[2], [])
    for quote in quotes:
        by_seller.setdefault(quote[0], []).append(quote)
    first: dict[str, Mapping[str, Any]] = {}
    for kind, sender, _, body in messages:
        if kind == "offer":
            first.setdefault(sender, body)
    rows = []
    for seller, seller_quotes in by_seller.items():
        statuses = {q[3] for q in seller_quotes}
        if not seller_quotes:
            outcome = _NO_OFFER
        elif "accepted" in statuses:
            outcome = _WON
        elif "open" in statuses or "countered" in statuses:
            outcome = _WAITING if state in _DECIDING else _NOT_CHOSEN
        elif "rejected" in statuses:
            outcome = _NOT_CHOSEN
        else:
            outcome = _NO_REPLY
        final = seller_quotes[-1] if seller_quotes else None
        lines = (final[4].get("lines") or []) if final else []
        opening = first.get(seller)
        first_total = None
        if opening is not None:
            first_total = _money(opening["total_minor"], opening["currency"])
        elif final is not None:
            first_total = _money(final[1], final[2])
        rows.append(
            SellerRow(
                name=_name(seller, cards),
                first_offer=first_total,
                final_offer=_money(final[1], final[2]) if final else None,
                unavailable=sum(1 for line in lines if line.get("offered_sku") is None),
                outcome=outcome[0],
                outcome_pill=outcome[1],
            )
        )
    return rows


def _turn(message: Any, currency: str, cards: Mapping[str, tuple[str, str | None]]) -> Turn:
    kind, sender, _, body = message
    total = body.get("total_minor")
    amount = None if total is None else _money(total, body.get("currency") or currency)
    who = _name(sender, cards)
    if kind == "offer":
        action = "offered" if not body.get("round") else "came back with"
        return Turn(who, action, amount, body.get("rationale") or None, "ok")
    if kind == "counter_offer":
        return Turn(who, "countered", amount, None, "attention")
    if kind == "accept":
        return Turn(who, "accepted", amount, None, "ok")
    return Turn(who, "said no", None, None, "quiet")


def _allergen_check(codes: list[str], spend_checks: list[Any], lines: int) -> Check:
    """The last `authorize_spend` that reached the allergen check decides. A refused offer
    earlier in the run stays visible in the detail."""
    reached = [
        reason
        for refused, reason in spend_checks
        if not refused or reason in (*_ALLERGEN_REFUSALS, _AFTER_ALLERGENS)
    ]
    if not reached:
        result = _NOT_RUN
    else:
        result = _REFUSED if reached[-1] in _ALLERGEN_REFUSALS else _PASSED
    parts = [", ".join(_words(codes)) if codes else "No allergens saved"]
    if lines:
        parts.append(f"{lines} lines checked")
    refusals = sum(1 for reason in reached if reason in _ALLERGEN_REFUSALS)
    if refusals and result is _PASSED:
        parts.append(f"{refusals} offer{'' if refusals == 1 else 's'} refused")
    return Check("Allergens", " · ".join(parts), *result)


def _diet_check(codes: list[str], chosen: str | None, quotes: list[Any]) -> Check:
    """`orders.seller_agent_id` is set by `authorize_spend`. Its quote's `constraints` are
    the filters the seller's search ran with; a quote without them is not a pass."""
    if not codes:
        return Check("Diet", "No diets saved", "None saved", "quiet")
    detail = ", ".join(_words(codes))
    if chosen is None:
        return Check("Diet", detail, *_NOT_RUN)
    applied = [set(q[5].get("diets") or []) for q in quotes if q[0] == chosen]
    if applied and set(codes) <= applied[-1]:
        return Check("Diet", detail, *_PASSED)
    return Check("Diet", detail, "Not confirmed", "attention")


def _budget_check(total: int | None, currency: str, budget: Any) -> Check:
    if budget is None:
        return Check(BUDGET_CHECK, "No weekly budget saved", "None saved", "quiet")
    limit, limit_currency = int(budget[0]), str(budget[1]).strip()
    if total is None:
        return Check(BUDGET_CHECK, f"Limit {_money(limit, limit_currency)}", *_NOT_RUN)
    detail = f"{_money(total, currency)} of {_money(limit, limit_currency)} this week"
    if limit_currency != currency.strip():
        return Check(BUDGET_CHECK, detail, "Not confirmed", "attention")
    if total > limit:
        return Check(BUDGET_CHECK, detail, "Over budget", "attention")
    return Check(BUDGET_CHECK, detail, *_PASSED)


def _approval_check(approval: Any, total: int | None, zone: ZoneInfo) -> Check:
    if approval is None:
        if total is None:
            return Check(APPROVAL_CHECK, "No approval asked yet", *_NOT_RUN)
        return Check(
            APPROVAL_CHECK, "Within the rules, so nobody had to approve", "Not needed", "ok"
        )
    status, decided_at = approval
    pill = _APPROVAL_PILLS.get(status, _NOT_RUN)
    if status == "pending":
        return Check(APPROVAL_CHECK, f"Waiting for {YOUR_APPROVER}", *pill)
    detail = f"{status.capitalize()}"
    if status in ("approved", "rejected"):
        detail += f" by {YOUR_APPROVER}"
    if decided_at is not None:
        at = decided_at.astimezone(zone)
        detail += f", {_day(at)} {_clock(at)}"
    return Check(APPROVAL_CHECK, detail, *pill)


def _couriers(
    messages: list[Any],
    chosen: str | None,
    zone: ZoneInfo,
    cards: Mapping[str, tuple[str, str | None]],
) -> list[CourierQuote]:
    """Each courier's latest quote. `delivery_booked` goes to both the store and the
    household, so it only names the courier when `orders.delivery_agent_id` doesn't."""
    if chosen is None:
        chosen = next((m[1] for m in messages if m[0] == "delivery_booked"), None)
    latest = {m[1]: m[3] for m in messages if m[0] == "delivery_quote"}
    rows = []
    for courier, body in latest.items():
        window = body.get("window") or {}
        start, end = window.get("start"), window.get("end")
        rows.append(
            CourierQuote(
                name=_name(courier, cards),
                fee=_money(body["price_minor"], body["currency"]),
                window=_window(
                    datetime.fromisoformat(start) if start else None,
                    datetime.fromisoformat(end) if end else None,
                    zone,
                ),
                chosen=courier == chosen,
            )
        )
    return rows


async def build_story(q: scope.Scoped, order_id: UUID) -> Story:
    """The story of `order_id`, one of the scope's household's orders. Raises `LookupError`
    for any other order. Reads only."""
    orders = await q.fetch(_ORDER_SQL, order=order_id)
    if not orders:
        raise LookupError(f"order {order_id} is not in this household")
    kind, state, total, currency, seller, courier, start, end, timezone = orders[0]
    zone = ZoneInfo(timezone)
    currency = str(currency).strip()
    total = None if total is None else int(total)

    budgets = await q.fetch(_BUDGET_SQL)
    quotes = await q.fetch(_QUOTES_SQL, order=order_id)
    messages = await q.fetch(_MESSAGES_SQL, order=order_id, types=list(_STORY_MESSAGES))
    spend_checks = await q.fetch(_SPEND_CHECKS_SQL, order=order_id, tool=AUTHORIZE_SPEND)
    lines = (await q.fetch(_LINES_SQL, order=order_id))[0][0]
    approvals = await q.fetch(_APPROVAL_SQL, order=order_id)
    agents, sent, by_model, by_rule, tokens, seconds = (await q.fetch(_BEHIND_SQL, order=order_id))[
        0
    ]

    named = {seller, courier, *(row[0] for row in quotes)}
    for _, sender, receiver, _ in messages:
        named.update((sender, receiver))
    cards = {
        agent: (cls, org)
        for agent, cls, org in await q.fetch(
            _CARDS_SQL, order=order_id, agents=[a for a in named if a]
        )
    }

    budget = budgets[0] if budgets else None
    same_currency = budget is not None and str(budget[1]).strip() == currency
    asked = [m[3].get("constraints") or {} for m in messages if m[0] == "quote_request"]
    allergens = sorted({code for c in asked for code in c.get("allergens") or []})
    diets = sorted({code for c in asked for code in c.get("diets") or []})

    return Story(
        summary=Summary(
            title="Groceries" if kind == "grocery" else "Dinner",
            seller=_name(seller, cards) if seller else None,
            state=state,
            state_pill=_STATE_PILLS.get(state, "ok"),
            window=_window(start, end, zone),
            total=None if total is None else _money(total, currency),
            budget=_money(budget[0], budget[1]) if budget is not None else None,
            bar_value=min(total, int(budget[0])) if same_currency and total is not None else None,
            bar_max=int(budget[0]) if same_currency and total is not None else None,
        ),
        sellers=_seller_rows(state, quotes, messages, cards),
        turns=[_turn(m, currency, cards) for m in messages if m[0] in _NEGOTIATION],
        checks=[
            _allergen_check(allergens, spend_checks, int(lines)),
            _diet_check(diets, seller, quotes),
            _budget_check(total, currency, budget),
            _approval_check(approvals[0] if approvals else None, total, zone),
        ],
        couriers=_couriers(messages, courier, zone, cards),
        behind=Behind(
            agents=int(agents),
            messages=int(sent),
            model_decisions=int(by_model),
            rule_decisions=int(by_rule),
            tokens=int(tokens),
            elapsed=_elapsed(float(seconds)),
        ),
    )


def without_spending(story: Story) -> Story:
    """`story` for an adult who may not see this order's spending and approvals (experience
    §4): no amounts, no rationale (a seller's may quote one), no budget or approval check."""
    return replace(
        story,
        summary=replace(story.summary, total=None, budget=None, bar_value=None, bar_max=None),
        sellers=[replace(s, first_offer=None, final_offer=None) for s in story.sellers],
        turns=[replace(t, amount=None, rationale=None) for t in story.turns],
        checks=[c for c in story.checks if c.name not in (BUDGET_CHECK, APPROVAL_CHECK)],
        couriers=[replace(c, fee=None) for c in story.couriers],
        spending_hidden=True,
    )
