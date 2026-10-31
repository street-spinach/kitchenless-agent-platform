"""The message catalog and summary lines (agent-protocol §5, §8, P3).

Each of the 26 message types has one `CatalogRow`: sender and receiver classes, body schema,
summary template, party rule (check C5) and whether it is outbox only. Templates may name only
the slots in `SLOTS`; a template naming any other slot raises when this module is imported.
"""

import string
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from services.common.money import Money
from services.common.money import format as format_money
from services.common.protocol.schemas import SCHEMAS

SUMMARY_MAX = 120

# Party rule terms (§5 "Party rule" column). P4's check() resolves each term against the
# referenced row; a rule with several terms passes when any of them matches.
HH_ORDER = "household_of(order)"  # household:<orders.household_id>
HH_RUN = "household_of(run)"  # household:<runs.household_id>
MEMBER_OF_RUN = "member_of(run)"  # a consumer whose user is in household_members(run)
CONSUMER_OF_TRIGGER = "consumer_of(runs.trigger.user_id)"
EXTERNAL_IF_A2A_DEMO = "external:a2a-demo if runs.kind = a2a_demo"
ORDER_SELLER = "orders.seller_agent_id"
ORDER_COURIER = "orders.delivery_agent_id"
QUOTE_SELLER = "quotes.seller_agent_id"
QUOTE_BUYER = "quotes.buyer_agent_id"
QUOTE_OTHER_PARTY = "the other of quotes.buyer_agent_id, quotes.seller_agent_id"
SHORTFALL_SELLER = "shortfalls.seller_agent_id"
REF_TO_AGENT = "agent_messages.to_agent"  # the refused message's receiver
REF_FROM_AGENT = "agent_messages.from_agent"  # the refused message's sender
ANY_SYSTEM = "system:*"

PARTY_TERMS = frozenset(
    {
        HH_ORDER,
        HH_RUN,
        MEMBER_OF_RUN,
        CONSUMER_OF_TRIGGER,
        EXTERNAL_IF_A2A_DEMO,
        ORDER_SELLER,
        ORDER_COURIER,
        QUOTE_SELLER,
        QUOTE_BUYER,
        QUOTE_OTHER_PARTY,
        SHORTFALL_SELLER,
        REF_TO_AGENT,
        REF_FROM_AGENT,
        ANY_SYSTEM,
    }
)


@dataclass(frozen=True)
class PartyRule:
    """Which stored row a message refers to, and the terms its sender and receiver must equal.

    `None` means the side is not constrained by C5 (it still passes C3).
    """

    ref: Literal["order", "quote", "shortfall", "message", "run"]
    sender: tuple[str, ...] | None
    receiver: tuple[str, ...] | None

    def __post_init__(self) -> None:
        for side in (self.sender, self.receiver):
            for term in side or ():
                if term not in PARTY_TERMS:
                    raise ValueError(f"unknown party rule term {term!r}")


def _id8(field: str) -> Callable[[BaseModel], str]:
    def slot(body: BaseModel) -> str:
        value = getattr(body, field, None)
        return "-" if value is None else str(value)[:8]

    return slot


def _amount(field: str) -> Callable[[BaseModel], str]:
    def slot(body: BaseModel) -> str:
        minor = getattr(body, field, None)
        currency = getattr(body, "currency", None)
        if minor is None or currency is None:
            return "-"
        return format_money(Money(minor, currency), "en")

    return slot


def _hhmm(value: datetime | None) -> str:
    return "-" if value is None else value.strftime("%H:%M")


def _field(field: str) -> Callable[[BaseModel], str]:
    def slot(body: BaseModel) -> str:
        value = getattr(body, field, None)
        return "-" if value is None else str(value)

    return slot


def _window(body: BaseModel) -> str:
    window = getattr(body, "window", None)
    if window is None:
        return "-"
    return f"{_hhmm(window.start)}–{_hhmm(window.end)}"


def _picks(body: BaseModel) -> dict[int, str]:
    return getattr(body, "picks", None) or {}


def _n_subs(body: BaseModel) -> str:
    picks = _picks(body)
    if picks:
        return str(sum(1 for pick in picks.values() if pick != "drop"))
    lines = getattr(body, "lines", None) or []
    count = 0
    for line in lines:
        if getattr(line, "substituted_for", None) is not None or getattr(line, "substitutes", None):
            count += 1
    return str(count)


def _len(field: str) -> Callable[[BaseModel], str]:
    def slot(body: BaseModel) -> str:
        return str(len(getattr(body, field, None) or []))

    return slot


# The only slots a template may use (§8 allowlist). No slot reads names, member or user ids,
# phone numbers, allergen or diet codes, budgets, addresses, rationale or other free text.
SLOTS: dict[str, Callable[[BaseModel], str]] = {
    "order": _id8("order_id"),
    "quote": _id8("quote_id"),
    "shortfall": _id8("shortfall_id"),
    "ref": _id8("ref_message_id"),
    "total": _amount("total_minor"),
    "price": _amount("price_minor"),
    "n_items": _len("items"),
    "n_lines": _len("lines"),
    "n_offered": lambda body: str(
        sum(1 for line in getattr(body, "lines", None) or [] if line.offered_sku is not None)
    ),
    "n_subs": _n_subs,
    "n_drop": lambda body: str(sum(1 for pick in _picks(body).values() if pick == "drop")),
    "n_codes": lambda body: str(
        len(getattr(body, "allergens", None) or []) + len(getattr(body, "diets", None) or [])
    ),
    "round": _field("round"),
    "reason": _field("reason"),
    "code": _field("code"),
    "state": _field("state"),
    "status": _field("status"),
    "window": _window,
    "expires": lambda body: _hhmm(getattr(body, "expires_at", None)),
    "reply_by": lambda body: _hhmm(getattr(body, "reply_by", None)),
}


def template_slots(template: str) -> set[str]:
    """The slot names a template uses. Raises ValueError on a slot not in `SLOTS`."""
    names: set[str] = set()
    for _, name, spec, conversion in string.Formatter().parse(template):
        if name is None:
            continue
        if name not in SLOTS or spec or conversion:
            raise ValueError(f"template slot {{{name}}} is not allowed: {template!r}")
        names.add(name)
    return names


@dataclass(frozen=True)
class CatalogRow:
    type: str
    senders: frozenset[str]
    receivers: frozenset[str]
    schema: type[BaseModel]
    template: str
    party: PartyRule
    outbox_only: bool

    def __post_init__(self) -> None:
        if not self.template:
            raise ValueError(f"{self.type} has no summary template")
        template_slots(self.template)


_PEOPLE_AGENTS = frozenset({"consumer", "household"})
_SELLERS = frozenset({"grocery", "restaurant"})
_BUYERS = frozenset({"household", "external"})
_QUOTE_PARTIES = frozenset({"household", "external", "grocery", "restaurant", "delivery"})
_AGENTS = frozenset({"consumer", "household", "grocery", "restaurant", "delivery"})


def _row(
    type_: str,
    senders: set[str] | frozenset[str],
    receivers: set[str] | frozenset[str],
    template: str,
    party: PartyRule,
    *,
    outbox_only: bool = False,
) -> CatalogRow:
    return CatalogRow(
        type=type_,
        senders=frozenset(senders),
        receivers=frozenset(receivers),
        schema=SCHEMAS[type_],
        template=template,
        party=party,
        outbox_only=outbox_only,
    )


_ROWS = [
    # 1-5: people types
    _row(
        "user_message",
        {"channel"},
        {"consumer"},
        "Message from a member",
        PartyRule("run", None, (CONSUMER_OF_TRIGGER,)),
        outbox_only=True,
    ),
    _row(
        "start_weekly_order",
        {"consumer"},
        {"household"},
        "Start weekly order",
        PartyRule("run", (MEMBER_OF_RUN,), (HH_RUN,)),
        outbox_only=True,
    ),
    _row(
        "start_dinner",
        {"consumer"},
        {"household"},
        "Start dinner order",
        PartyRule("run", (MEMBER_OF_RUN,), (HH_RUN,)),
        outbox_only=True,
    ),
    _row(
        "constraints_request",
        {"household"},
        {"consumer"},
        "Constraints requested for order {order}",
        PartyRule("order", (HH_ORDER,), (MEMBER_OF_RUN,)),
        outbox_only=True,
    ),
    _row(
        "constraints",
        {"consumer"},
        {"household"},
        "Constraints received ({n_codes} codes)",
        PartyRule("order", (MEMBER_OF_RUN,), (HH_ORDER,)),
        outbox_only=True,
    ),
    # 6-12: negotiation
    _row(
        "quote_request",
        _BUYERS,
        _SELLERS,
        "Quote request for order {order}: {n_items} items, reply by {reply_by}",
        PartyRule("order", (HH_ORDER, EXTERNAL_IF_A2A_DEMO), None),
    ),
    _row(
        "offer",
        _SELLERS,
        _BUYERS,
        "Offer {total}: {n_offered} of {n_lines} items, {n_subs} substitutes, expires {expires}",
        PartyRule("quote", (QUOTE_SELLER,), (QUOTE_BUYER,)),
    ),
    _row(
        "counter_offer",
        _BUYERS,
        _SELLERS,
        "Counter {total} on quote {quote}, round {round}",
        PartyRule("quote", (QUOTE_BUYER,), (QUOTE_SELLER,)),
    ),
    _row(
        "accept",
        {"household"},
        _SELLERS,
        "Accept quote {quote} at {total}",
        PartyRule("quote", (QUOTE_BUYER,), (QUOTE_SELLER,)),
    ),
    _row(
        "reject",
        _QUOTE_PARTIES,
        _QUOTE_PARTIES,
        "Reject quote {quote}: {reason}",
        PartyRule("quote", (QUOTE_BUYER, QUOTE_SELLER), (QUOTE_OTHER_PARTY,)),
    ),
    _row(
        "cancel",
        _BUYERS,
        _SELLERS,
        "Cancel quote {quote}: {reason}",
        PartyRule("quote", (QUOTE_BUYER,), (QUOTE_SELLER,)),
    ),
    _row(
        "order_confirm",
        _SELLERS,
        {"household"},
        "Order {order} {state}: {n_lines} lines, {total}",
        PartyRule("order", (ORDER_SELLER,), (HH_ORDER,)),
    ),
    # 13-16: courier
    _row(
        "delivery_quote_request",
        _SELLERS,
        {"delivery"},
        "Courier quote request for order {order}, window {window}",
        PartyRule("order", (ORDER_SELLER,), None),
    ),
    _row(
        "delivery_quote",
        {"delivery"},
        _SELLERS,
        "Courier quote {price}, window {window}",
        PartyRule("quote", (QUOTE_SELLER,), (QUOTE_BUYER,)),
    ),
    _row(
        "delivery_request",
        _SELLERS,
        {"delivery"},
        "Delivery request for order {order}, window {window}",
        PartyRule("order", (ORDER_SELLER,), (ORDER_COURIER,)),
    ),
    _row(
        "delivery_booked",
        {"delivery"},
        _SELLERS | {"household"},
        "Courier booked for order {order}, window {window}",
        PartyRule("order", (ORDER_COURIER,), (ORDER_SELLER, HH_ORDER)),
    ),
    # 17-20: system and channel types
    _row(
        "order_status",
        {"system:api"},
        {"household"},
        "Order {order} is {state}",
        PartyRule("order", None, (HH_ORDER,)),
        outbox_only=True,
    ),
    _row(
        "approval_decision",
        {"system:api", "system:sweeper"},
        {"household"},
        "Approval {status} for order {order}",
        PartyRule("order", None, (HH_ORDER,)),
        outbox_only=True,
    ),
    _row(
        "quote_round_closed",
        {"system:sweeper"},
        {"household"} | _SELLERS,
        "Quote round closed for order {order}",
        PartyRule("order", None, (HH_ORDER, ORDER_SELLER)),
        outbox_only=True,
    ),
    _row(
        "whatsapp_out",
        _PEOPLE_AGENTS | {"system"},
        {"channel"},
        "WhatsApp message to a member",
        PartyRule("run", (HH_RUN, MEMBER_OF_RUN, ANY_SYSTEM), None),
        outbox_only=True,
    ),
    # 21: protocol errors (never to system:* or channel:*)
    _row(
        "protocol_error",
        _AGENTS,
        _AGENTS | {"external"},
        "Refused {ref}: {code}",
        PartyRule("message", (REF_TO_AGENT,), (REF_FROM_AGENT,)),
    ),
    # 22-26: shortfall
    _row(
        "shortfall_detected",
        {"system:api"},
        _SELLERS,
        "Shortfall raised for order {order}: {n_lines} lines",
        PartyRule("order", None, (ORDER_SELLER,)),
        outbox_only=True,
    ),
    _row(
        "shortfall_notice",
        _SELLERS,
        {"household"},
        "Shortfall on order {order}: {n_lines} lines missing, {n_subs} substitutes",
        PartyRule("order", (ORDER_SELLER,), (HH_ORDER,)),
    ),
    _row(
        "shortfall_relay",
        {"household"},
        {"consumer"},
        "Ask a member about shortfall {shortfall}",
        PartyRule("shortfall", (HH_ORDER,), (MEMBER_OF_RUN,)),
        outbox_only=True,
    ),
    _row(
        "shortfall_closed",
        {"system:api", "system:sweeper"},
        {"household"},
        "Shortfall {shortfall} closed: {reason}",
        PartyRule("shortfall", None, (HH_ORDER,)),
        outbox_only=True,
    ),
    _row(
        "shortfall_decision",
        {"household"},
        _SELLERS,
        "Shortfall {shortfall}: {n_subs} substituted, {n_drop} dropped",
        PartyRule("shortfall", (HH_ORDER,), (SHORTFALL_SELLER,)),
    ),
]

CATALOG: dict[str, CatalogRow] = {row.type: row for row in _ROWS}

if len(CATALOG) != len(_ROWS) or CATALOG.keys() != SCHEMAS.keys():
    raise RuntimeError("the catalog and SCHEMAS must list the same message types once each")


def render_summary(type: str, body: BaseModel | dict[str, Any]) -> str:
    """The summary line for a message: the row's template filled from the validated body.

    At most 120 characters; longer output is cut to 117 plus `...` (§8).
    """
    row = CATALOG[type]
    if not isinstance(body, row.schema):
        body = row.schema.model_validate(body)
    values = {name: SLOTS[name](body) for name in template_slots(row.template)}
    text = row.template.format(**values)
    if len(text) > SUMMARY_MAX:
        text = text[: SUMMARY_MAX - 3] + "..."
    return text
