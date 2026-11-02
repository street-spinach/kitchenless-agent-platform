"""The grocery and restaurant handler: quote, counter, accept, the courier round and the
shortfall.

Architecture item 30 (amended by agent-protocol A8) and agent-protocol §7, §10 and P23.
`services/agents/grocery/handler.py` and
`services/agents/restaurant/handler.py` each bind one `SellerHandler` to their area.

The handler is stateless (architecture §4): each step reads the `quotes` row it needs,
decides in code, and returns every `quotes` change as a conditional update (or, for a new
quote, an insert) in its `StepResult`. The consumer commits those changes with the outgoing
messages and timers in one transaction (agent-protocol §7 commit rule), so a replayed step
sends the same message ids and changes nothing twice.

*   `quote_request`: for each requested item, `search_items` with the request's allergen,
    diet and zone filters, then one `quote` call for prices and stock. A missing item gets
    substitute candidates from the same filtered search; one `choose(rank_substitutes)`
    call. The new quote saves `constraints` and `expires_at = now() + OFFER_TTL_SECONDS`,
    and the `offer` copies that `expires_at`.
*   `counter_offer`: the arrival table, then one `choose(respond_to_counter)`. "agree"
    and `counter` set `countered` with `round + 1` and reply `offer {final: true}` (A8);
    `reject` sets `rejected`, `closed_by = seller`.
*   `accept`: the arrival table; when live with the same `items_hash` and total, the
    conditional update to `accepted`, one `delivery_quote_request` per in-zone courier and
    the 10 s courier `quote_round` timer. Nothing waits.
*   `reject`, `cancel` (buyer): `rejected`, `closed_by = buyer`; `cancel` after `placed`
    answers `reject {reason: already_placed}`.
*   `quote_round_closed` (`round = delivery`): the cheapest live courier quote inside the
    requested window, then `place_order`, `order_confirm` and `delivery_request`. With no
    courier: `rejected`, `closed_by = seller` and `reject {reason: no_courier}`.
*   A step that rejects a quote whose buyer is `external:a2a-demo` also finishes the run
    `cancelled` (`a2a_demo_closed`, §11).
*   `shortfall_detected` (§10): for each missing order line, `search_items` with the
    allergen and diet codes saved on the accepted quote, one `quote` call, and only
    candidates whose `qty × unit_minor` is at most the line's `line_total_minor`; one
    `choose(rank_substitutes)` call keeps up to 2 per line. A line with no candidate is
    offered as drop only. Sends `shortfall_notice` with `reply_by = now() +
    SHORTFALL_REPLY_SECONDS` (the run override wins).
*   `shortfall_decision`: one `amend_order` call, then `order_confirm` with the resulting
    lines; `state=failed, reason=nothing_to_deliver` when no line remains. A refused
    `amend_order` changes nothing (a `decision` trace only).
*   `protocol_error`: a `decision` trace only.

**Model proposes, code decides** (architecture §4). Each choice point computes its allowed
set and rule default in code, makes one `choose` call, and checks the pick again before
using it. The model's inputs hold only the inbound message, the card's own
`business_rules` and the allowed set.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from services.agents.common import seller_prompts
from services.agents.common.model import (
    TEMPLATES,
    Actions,
    Allowed,
    Choice,
    PerItem,
    Range,
    Subset,
)
from services.common import ids, mcp_client, orders
from services.common.config import get_settings
from services.common.consumer import (
    Inbound,
    Outgoing,
    QuoteInsert,
    QuoteUpdate,
    RunFinish,
    StepFailed,
    StepResult,
    TimerCreate,
)
from services.common.directory import AgentCard
from services.common.mcp_client import ToolRefused
from services.common.run_context import RunContext
from services.identity.tokens import MockIssuer

__all__ = [
    "A2A_DEMO_BUYER",
    "COURIER_ROUND_SECONDS",
    "HANDLED_TYPES",
    "MAX_SUBSTITUTES",
    "CourierQuote",
    "OrderInfo",
    "SellerHandler",
    "SellerStore",
    "StoredQuote",
    "UnsupportedMessage",
    "allowed_counter",
    "courier_window",
    "floor_total",
]

log = logging.getLogger(__name__)

A2A_DEMO_BUYER = "external:a2a-demo"
A2A_CLOSED = "a2a_demo_closed"
# The store's courier round (architecture item 30).
COURIER_ROUND_SECONDS = 10
# Substitute candidates per missing item (architecture §4 `rank_substitutes`).
MAX_SUBSTITUTES = 3
# Substitutes per shortfall line: 2 plus "Skip it" fit WhatsApp's 3 buttons (§10).
MAX_SHORTFALL_SUBSTITUTES = 2
SHORTFALL_REPLY_OVERRIDE = "SHORTFALL_REPLY_SECONDS"
NOTHING_TO_DELIVER = "nothing_to_deliver"
# `quotes.round` is 0-2 in the protocol schema (P2).
MAX_ROUND = 2
_COUNTER_TEMPLATE = TEMPLATES["respond_to_counter"]
LIVE = ("open", "countered")
PLACED_OR_LATER = frozenset(
    {"placed", "courier_booked", "in_delivery", "delivered", "failed", "cancelled", "expired"}
)
# End states reached only before `placed` (architecture §8 order states).
NEVER_PLACED = frozenset({"cancelled", "expired"})
HANDLED_TYPES = frozenset(
    {
        "quote_request",
        "counter_offer",
        "accept",
        "reject",
        "cancel",
        "quote_round_closed",
        "shortfall_detected",
        "shortfall_decision",
        "protocol_error",
    }
)


class UnsupportedMessage(ValueError):
    """The seller handler has no path for this message type."""


@dataclass(frozen=True)
class StoredQuote:
    """The `quotes` columns the seller reads (architecture item 11, `constraints` and
    `closed_by` from agent-protocol §7).

    For a seller quote, `items` is `{"lines": [...], "window": {...}, "zone_id": ...}`:
    the offered lines and the request's window and zone, which the courier round needs.
    """

    quote_id: UUID
    run_id: UUID
    order_id: UUID
    conversation_id: UUID
    seller_agent_id: str
    buyer_agent_id: str
    items: Any
    total_minor: int
    currency: str
    round: int
    expires_at: datetime
    status: str
    constraints: Mapping[str, Any]
    closed_by: str | None = None

    @property
    def lines(self) -> list[dict[str, Any]]:
        items = self.items if isinstance(self.items, Mapping) else {}
        return list(items.get("lines") or [])

    def live(self, now: datetime) -> bool:
        return self.status in LIVE and self.expires_at > now


@dataclass(frozen=True)
class CourierQuote:
    """A courier's `quotes` row for the order, with the store as buyer (catalog row 14)."""

    quote_id: UUID
    conversation_id: UUID
    seller_agent_id: str
    total_minor: int
    currency: str
    expires_at: datetime
    status: str
    window: Mapping[str, Any] | None


@dataclass(frozen=True)
class OrderInfo:
    """What the courier round and `cancel` read from the order: no address, only its ref."""

    state: str
    address_ref: UUID
    zone_id: str


class SellerStore(Protocol):
    """The rows a seller step reads. `PgSellerStore` reads Postgres; tests use fakes."""

    async def quote(self, quote_id: UUID) -> StoredQuote | None: ...

    async def accepted_quote(self, order_id: UUID, seller: str) -> StoredQuote | None: ...

    async def courier_quotes(self, order_id: UUID, store: str) -> Sequence[CourierQuote]: ...

    async def order(self, order_id: UUID) -> OrderInfo | None: ...

    async def rules(self, agent_id: str) -> Mapping[str, Any]: ...

    async def couriers(self, region: str, zone_id: str, currency: str) -> Sequence[str]: ...

    async def order_lines(self, order_id: UUID) -> Sequence[Mapping[str, Any]]: ...

    async def overrides(self, run_id: UUID) -> Mapping[str, Any]: ...


CallTool = Callable[..., Awaitable[dict[str, Any]]]
IssueToken = Callable[[str, str, Sequence[str]], str]
Clock = Callable[[], datetime]


class Chooser(Protocol):
    async def choose(
        self,
        ctx: RunContext,
        point: str,
        allowed: Allowed,
        default: Any,
        inputs: Mapping[str, Any],
        guidance: str | None = None,
    ) -> Choice: ...


class TraceSink(Protocol):
    async def write(
        self, run_id: Any, agent_id: str, kind: str, payload: dict[str, Any], **cols: Any
    ) -> int: ...


def _mock_token(sub: str, act: str, scopes: Sequence[str]) -> str:
    # Every run uses the mock issuer until identity-authz replaces the token source (Q4).
    return MockIssuer().issue(sub, act, list(scopes))


def _now() -> datetime:
    return datetime.now(UTC)


def _time(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def floor_total(lines: Sequence[Mapping[str, Any]], rules: Mapping[str, Any]) -> int:
    """Σ qty × the item's floor unit price (`business_rules` kind `floors`) over the
    offered lines. An item with no floor uses its quoted unit price (architecture §4)."""
    floors = rules.get("floors") if isinstance(rules.get("floors"), Mapping) else {}
    total = 0
    for line in lines:
        sku = line.get("offered_sku")
        if not sku:
            continue
        floor = floors.get(sku)
        has_floor = isinstance(floor, int) and not isinstance(floor, bool)
        unit = floor if has_floor else int(line["unit_minor"])
        total += int(line["qty"]) * unit
    return total


def allowed_counter(
    *, buyer_total: int, current_total: int, floor: int, round: int
) -> tuple[Actions, dict[str, Any]]:
    """The `respond_to_counter` allowed set and rule default (architecture §4).

    `reject` always; `agree` only at or above the floor total; `counter` only while the
    round is 0, between the floor total and the current total. No change once the round is
    at its maximum. Default: `agree` if allowed, else `counter` at the floor total if
    allowed, else `reject`.
    """
    actions: dict[str, Allowed | None] = {"reject": None}
    can_change = round < MAX_ROUND
    if can_change and buyer_total >= floor:
        actions["agree"] = None
    if can_change and round == 0 and floor <= current_total:
        actions["counter"] = Range(floor, current_total)
    if "agree" in actions:
        default: dict[str, Any] = {"action": "agree"}
    elif "counter" in actions:
        default = {"action": "counter", "arg": floor}
    else:
        default = {"action": "reject"}
    return Actions(actions), default


def courier_window(quote: CourierQuote) -> dict[str, str] | None:
    """The courier quote's window (`items.window`), or `None` if it has none."""
    win = quote.window
    if not isinstance(win, Mapping) or not win.get("start") or not win.get("end"):
        return None
    return {"start": str(win["start"]), "end": str(win["end"])}


def _within(inner: Mapping[str, Any], outer: Mapping[str, Any]) -> bool:
    try:
        start, end = _time(inner["start"]), _time(inner["end"])
        return _time(outer["start"]) <= start < end <= _time(outer["end"])
    except (KeyError, TypeError, ValueError):
        return False


def _order_lines(lines: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The offered lines as order lines, exactly as the household authorizes them."""
    return [
        {
            "line": line["line"],
            "sku": line["offered_sku"],
            "name": line["offered_name"] or line["offered_sku"],
            "qty": line["qty"],
            "unit_minor": line["unit_minor"],
            "substituted_for": line.get("substituted_for"),
        }
        for line in lines
        if line.get("offered_sku")
    ]


@dataclass(frozen=True)
class _Want:
    """A line `_price` prices candidates for: only its quantity."""

    qty: int


def _substitute_query(item: str) -> str | None:
    """The broader query for a missing item's substitutes: its last word, if different."""
    words = item.split()
    return words[-1] if len(words) > 1 else None


class _Step:
    """One inbound message: its replies, quote changes, timers and run outcome."""

    def __init__(self, msg: Inbound, card: AgentCard) -> None:
        self.msg = msg
        self.card = card
        self.messages: list[Outgoing] = []
        self.inserts: list[QuoteInsert] = []
        self.updates: list[QuoteUpdate] = []
        self.timers: list[TimerCreate] = []
        self.finish: RunFinish | None = None

    def send(
        self,
        to_agent: str,
        type: str,
        body: dict[str, Any],
        *,
        conversation_id: UUID | None,
    ) -> UUID:
        message_id = ids.message_id(self.msg.row.message_id, len(self.messages))
        self.messages.append(
            Outgoing(
                message_id=message_id,
                from_agent=self.card.agent_id,
                to_agent=to_agent,
                type=type,
                body={"schema_version": 1, **body},
                conversation_id=conversation_id,
                causation_id=self.msg.row.message_id,
            )
        )
        return message_id

    def reject(self, quote: StoredQuote, reason: str) -> None:
        body = {"order_id": str(quote.order_id), "quote_id": str(quote.quote_id), "reason": reason}
        self.send(quote.buyer_agent_id, "reject", body, conversation_id=quote.conversation_id)

    def close(self, quote: StoredQuote, status: str, closed_by: str | None) -> None:
        """Move the quote from its stored status and round to `status` (commit rule)."""
        values: dict[str, Any] = {"status": status}
        if closed_by is not None:
            values["closed_by"] = closed_by
        self.updates.append(QuoteUpdate(quote.quote_id, (quote.status,), quote.round, values))
        if status == "rejected" and quote.buyer_agent_id == A2A_DEMO_BUYER:
            self.finish = RunFinish("cancelled", A2A_CLOSED)

    def result(self) -> StepResult:
        return StepResult(
            messages=self.messages,
            timers=self.timers,
            quote_inserts=self.inserts,
            quote_updates=self.updates,
            finish=self.finish,
        )


class SellerHandler:
    """The class handler of one seller area, `handle(msg, card) -> StepResult`."""

    def __init__(
        self,
        area: str,
        *,
        model: Chooser,
        traces: TraceSink,
        store: SellerStore,
        call_tool: CallTool = mcp_client.call_tool,
        issue_token: IssueToken = _mock_token,
        clock: Clock = _now,
        offer_ttl_seconds: int | None = None,
        shortfall_reply_seconds: int | None = None,
    ) -> None:
        self.area = area
        self.model = model
        self.traces = traces
        self.store = store
        self.call_tool = call_tool
        self.issue_token = issue_token
        self.clock = clock
        self.offer_ttl = timedelta(
            seconds=offer_ttl_seconds
            if offer_ttl_seconds is not None
            else get_settings().offer_ttl_seconds
        )
        self._shortfall_reply_seconds = shortfall_reply_seconds

    async def __call__(self, msg: Inbound, card: AgentCard) -> StepResult:
        t = msg.row.type
        if t not in HANDLED_TYPES:
            raise UnsupportedMessage(f"the {self.area} handler does not handle {t!r}")
        if card.agent_class != self.area:
            raise UnsupportedMessage(f"{card.agent_id} is not a {self.area} card")
        step = _Step(msg, card)
        await getattr(self, f"_{t}")(step)
        return step.result()

    # Helpers ------------------------------------------------------------------------------

    def _ctx(self, step: _Step, conversation_id: UUID | None = None) -> RunContext:
        return RunContext(
            run_id=step.msg.row.run_id,
            agent_id=step.card.agent_id,
            conversation_id=conversation_id or step.msg.row.conversation_id,
        )

    async def _tool(
        self, step: _Step, name: str, args: dict[str, Any], conversation_id: UUID | None = None
    ) -> dict[str, Any]:
        # A seller acts for its own org with a service token (identity-authz §8 step 1b).
        token = self.issue_token(step.card.owner_ref, step.card.agent_id, ())
        return await self.call_tool(
            self.area, name, args, ctx=self._ctx(step, conversation_id), token=token
        )

    async def _decision(self, step: _Step, node: str, choice: str) -> None:
        await self.traces.write(
            step.msg.row.run_id,
            step.card.agent_id,
            "decision",
            {
                "node": node,
                "choice": choice,
                "explanation": "",
                "ref": str(step.msg.row.message_id),
            },
            on_behalf_of=step.card.owner_ref,
            conversation_id=step.msg.row.conversation_id,
        )

    async def _quote_of(self, step: _Step) -> StoredQuote:
        quote_id = step.msg.body.quote_id  # type: ignore[attr-defined]
        quote = await self.store.quote(quote_id)
        if quote is None or quote.seller_agent_id != step.card.agent_id:
            # C4 and C5 already passed, so the row exists and is ours; fail closed anyway.
            raise StepFailed("handler", "unknown_quote")
        return quote

    # quote_request ------------------------------------------------------------------------

    async def _quote_request(self, step: _Step) -> None:
        body = step.msg.body
        row = step.msg.row
        if row.conversation_id is None:
            raise StepFailed("handler", "no_conversation")
        if step.card.currency and body.currency != step.card.currency:
            raise StepFailed("handler", "currency_mismatch")
        constraints = {
            "allergens": sorted(str(a) for a in body.constraints.allergens),
            "diets": sorted(str(d) for d in body.constraints.diets),
        }
        filters = {
            "allergen_codes": constraints["allergens"],
            "diet_codes": constraints["diets"],
            "zone_id": body.zone_id,
        }

        # Candidates per line: the item's filtered search, then (if missing) a broader one.
        hits: list[list[dict[str, Any]]] = []
        for item in body.items:
            found = await self._tool(step, "search_items", {"query": item.item, "filters": filters})
            hits.append(list(found.get("items") or [])[:MAX_SUBSTITUTES])
        priced = await self._price(step, body.items, hits)
        offered: dict[int, dict[str, Any]] = {}
        for line in range(len(body.items)):
            first = next((p for p in priced[line] if p["available"]), None)
            if first is not None:
                offered[line] = first

        candidates: dict[str, list[str]] = {}
        names: dict[str, dict[str, Any]] = {}
        for line, item in enumerate(body.items):
            if line in offered:
                continue
            tried = {h["sku"] for h in hits[line]}
            query = _substitute_query(item.item)
            more: list[dict[str, Any]] = []
            if query is not None:
                found = await self._tool(step, "search_items", {"query": query, "filters": filters})
                more = [h for h in found.get("items") or [] if h["sku"] not in tried]
            subs = (await self._price(step, [item], [more[:MAX_SUBSTITUTES]]))[0]
            available = [p for p in subs if p["available"]]
            candidates[str(line)] = [p["sku"] for p in available]
            for p in available:
                names[p["sku"]] = p

        # One choice per step, even with nothing missing, so every offer has a rationale.
        allowed = PerItem({k: Subset(v, MAX_SUBSTITUTES) for k, v in candidates.items()})
        default = {k: list(v) for k, v in candidates.items()}
        rules = await self.store.rules(step.card.agent_id)
        choice = await self.model.choose(
            self._ctx(step),
            "rank_substitutes",
            allowed,
            default,
            self._inputs(step, rules, allowed),
            guidance=seller_prompts.guidance("rank_substitutes", self.area),
        )
        picks = choice.value if allowed.check(choice.value) else default

        lines: list[dict[str, Any]] = []
        for line, item in enumerate(body.items):
            hit = offered.get(line)
            substituted_for = None
            if hit is None and picks.get(str(line)):
                hit = names[picks[str(line)][0]]
                substituted_for = item.item
            lines.append(
                {
                    "line": line,
                    "requested_item": item.item,
                    "offered_sku": hit["sku"] if hit else None,
                    "offered_name": hit["name"] if hit else None,
                    "qty": item.qty,
                    "unit_minor": int(hit["unit_minor"]) if hit else 0,
                    "substituted_for": substituted_for,
                }
            )
        total = sum(line["qty"] * line["unit_minor"] for line in lines if line["offered_sku"])
        quote_id = ids.quote_id(row.message_id)
        expires_at = self.clock() + self.offer_ttl
        window = body.window.model_dump(mode="json")
        step.inserts.append(
            QuoteInsert(
                quote_id=quote_id,
                run_id=row.run_id,
                order_id=body.order_id,
                conversation_id=row.conversation_id,
                seller_agent_id=step.card.agent_id,
                buyer_agent_id=row.from_agent,
                items={"lines": lines, "window": window, "zone_id": body.zone_id},
                total_minor=total,
                currency=body.currency,
                expires_at=expires_at,
                constraints=constraints,
            )
        )
        step.send(
            row.from_agent,
            "offer",
            {
                "order_id": str(body.order_id),
                "quote_id": str(quote_id),
                "round": 0,
                "lines": lines,
                "total_minor": total,
                "currency": body.currency,
                "expires_at": expires_at.isoformat(),
                "final": False,
                "rationale": choice.rationale,
            },
            conversation_id=row.conversation_id,
        )

    async def _price(
        self,
        step: _Step,
        items: Sequence[Any],
        hits: Sequence[Sequence[Mapping[str, Any]]],
        currency: str | None = None,
    ) -> list[list[dict[str, Any]]]:
        """One `quote` call for every (line, candidate) pair; priced candidates per line.

        `items` need a `qty`; `currency` defaults to the inbound `quote_request`'s."""
        if currency is None:
            currency = step.msg.body.currency  # type: ignore[attr-defined]
        wanted = [
            (line, str(h["sku"]), item.qty)
            for line, (item, found) in enumerate(zip(items, hits, strict=True))
            for h in found
        ]
        out: list[list[dict[str, Any]]] = [[] for _ in items]
        if not wanted:
            return out
        result = await self._tool(
            step, "quote", {"items": [{"sku": sku, "qty": qty} for _, sku, qty in wanted]}
        )
        priced = list(result.get("lines") or [])
        if len(priced) != len(wanted):
            raise StepFailed("handler", "quote_mismatch")
        for (line, sku, _), p in zip(wanted, priced, strict=True):
            ok = (
                p.get("sku") == sku
                and bool(p.get("available"))
                and isinstance(p.get("unit_minor"), int)
                and result.get("currency") == currency
            )
            out[line].append({**p, "available": ok})
        return out

    @staticmethod
    def _inputs(step: _Step, rules: Mapping[str, Any], allowed: Allowed) -> dict[str, Any]:
        """The model's inputs: the inbound message, the card's own rules, the allowed set."""
        message = {
            k: v for k, v in step.msg.body.model_dump(mode="json").items() if k != "rationale"
        }
        return {
            "message": {"type": step.msg.row.type, "body": message},
            "rules": dict(rules),
            "allowed": allowed.describe(),
        }

    # counter_offer ------------------------------------------------------------------------

    async def _counter_offer(self, step: _Step) -> None:
        quote = await self._quote_of(step)
        now = self.clock()
        if quote.status in LIVE and quote.expires_at <= now:
            step.close(quote, "expired", None)
            step.reject(quote, "expired")
            return
        if quote.status == "expired":
            step.reject(quote, "expired")
            return
        if quote.status not in LIVE:
            step.reject(quote, "closed")
            return

        rules = await self.store.rules(step.card.agent_id)
        floor = floor_total(quote.lines, rules)
        buyer_total = int(step.msg.body.total_minor)  # type: ignore[attr-defined]
        allowed, default = allowed_counter(
            buyer_total=buyer_total,
            current_total=quote.total_minor,
            floor=floor,
            round=quote.round,
        )
        choice = await self.model.choose(
            self._ctx(step),
            "respond_to_counter",
            allowed,
            default,
            self._inputs(step, rules, allowed),
            guidance=seller_prompts.guidance("respond_to_counter", self.area),
        )
        # Code decides: an answer outside the allowed set gives the rule default.
        pick = choice.value if allowed.check(choice.value) else default
        action = pick["action"]
        if action == "reject":
            step.close(quote, "rejected", "seller")
            step.reject(quote, "price")
            return
        total = buyer_total if action == "agree" else int(pick["arg"])
        if total < floor:
            raise StepFailed("handler", "below_floor")  # unreachable: the allowed set says so
        new_round = quote.round + 1
        expires_at = now + self.offer_ttl
        step.updates.append(
            QuoteUpdate(
                quote.quote_id,
                (quote.status,),
                quote.round,
                {
                    "status": "countered",
                    "total_minor": total,
                    "round": new_round,
                    "expires_at": expires_at,
                },
            )
        )
        step.send(
            quote.buyer_agent_id,
            "offer",
            {
                "order_id": str(quote.order_id),
                "quote_id": str(quote.quote_id),
                "round": new_round,
                "lines": quote.lines,
                "total_minor": total,
                "currency": quote.currency,
                "expires_at": expires_at.isoformat(),
                "final": new_round >= 1,
                "rationale": choice.rationale if pick == choice.value else _COUNTER_TEMPLATE,
            },
            conversation_id=quote.conversation_id,
        )

    # accept -------------------------------------------------------------------------------

    async def _accept(self, step: _Step) -> None:
        body = step.msg.body
        quote = await self._quote_of(step)
        now = self.clock()
        if quote.status in LIVE and quote.expires_at <= now:
            step.close(quote, "expired", None)
            step.reject(quote, "expired")
            return
        if quote.status == "expired":
            step.reject(quote, "expired")
            return
        if quote.status == "rejected":
            step.reject(quote, "closed")
            return
        if quote.status == "accepted":
            await self._decision(step, "accept", "duplicate_accept")
            return
        if body.items_hash != orders.items_hash(_order_lines(quote.lines)):  # type: ignore[attr-defined]
            step.reject(quote, "items_changed")
            return
        if body.total_minor != quote.total_minor:  # type: ignore[attr-defined]
            step.reject(quote, "price")
            return

        step.updates.append(
            QuoteUpdate(
                quote.quote_id,
                (quote.status,),
                quote.round,
                {"status": "accepted"},
                expected_total_minor=quote.total_minor,
                require_live=True,
            )
        )
        items = quote.items if isinstance(quote.items, Mapping) else {}
        zone_id = str(items.get("zone_id") or "")
        window = items.get("window")
        if not zone_id or not isinstance(window, Mapping):
            raise StepFailed("handler", "quote_has_no_window")
        couriers = await self.store.couriers(step.card.region, zone_id, quote.currency)
        request = {
            "order_id": str(quote.order_id),
            "zone_id": zone_id,
            "window": dict(window),
            "pickup_agent_id": step.card.agent_id,
        }
        for courier in couriers:
            message_id = ids.message_id(step.msg.row.message_id, len(step.messages))
            step.send(
                courier,
                "delivery_quote_request",
                request,
                conversation_id=ids.conversation_id(message_id),
            )
        # The round ends on a timer; no step slot is held while couriers answer.
        step.timers.append(
            TimerCreate(
                kind="quote_round",
                due_at=timedelta(seconds=COURIER_ROUND_SECONDS),
                ref={
                    "order_id": str(quote.order_id),
                    "round": "delivery",
                    "to": step.card.agent_id,
                },
            )
        )

    # reject and cancel (buyer) ------------------------------------------------------------

    async def _reject(self, step: _Step) -> None:
        quote = await self._quote_of(step)
        if quote.status in LIVE:
            step.close(quote, "rejected", "buyer")
            return
        await self._decision(step, "reject", f"ignored_{quote.status}")

    async def _cancel(self, step: _Step) -> None:
        quote = await self._quote_of(step)
        order = await self.store.order(quote.order_id)
        # An order that ended before `placed` (`expired` approval, `cancelled`) still closes
        # its open quote (CUJ 3); only a placed order answers `already_placed`.
        if order is None or order.state in PLACED_OR_LATER - NEVER_PLACED:
            step.reject(quote, "already_placed")
            return
        if quote.status in (*LIVE, "accepted"):
            step.close(quote, "rejected", "buyer")
            return
        await self._decision(step, "cancel", f"ignored_{quote.status}")

    # The courier round --------------------------------------------------------------------

    async def _quote_round_closed(self, step: _Step) -> None:
        body = step.msg.body
        if body.round != "delivery":  # type: ignore[attr-defined]
            await self._decision(step, "quote_round_closed", "not_delivery_round")
            return
        quote = await self.store.accepted_quote(body.order_id, step.card.agent_id)  # type: ignore[attr-defined]
        if quote is None:
            await self._decision(step, "quote_round_closed", "quote_not_accepted")
            return
        items = quote.items if isinstance(quote.items, Mapping) else {}
        requested = items.get("window") if isinstance(items.get("window"), Mapping) else None
        now = self.clock()
        offers = []
        for c in await self.store.courier_quotes(quote.order_id, step.card.agent_id):
            win = courier_window(c)
            if (
                c.status == "open"
                and c.expires_at > now
                and c.currency == quote.currency
                and win is not None
                and requested is not None
                and _within(win, requested)
            ):
                offers.append((c.total_minor, _time(win["start"]), c.seller_agent_id, c, win))
        if not offers:
            step.close(quote, "rejected", "seller")
            step.reject(quote, "no_courier")
            return
        _, _, courier, chosen, win = min(offers, key=lambda o: o[:3])

        # The step stays a no-op unless the quote is still `accepted` at commit.
        step.updates.append(
            QuoteUpdate(quote.quote_id, ("accepted",), quote.round, {"status": "accepted"})
        )
        lines = _order_lines(quote.lines)
        try:
            placed = await self._tool(
                step,
                "place_order",
                {
                    "order_id": str(quote.order_id),
                    "quote_id": str(quote.quote_id),
                    "items_hash": orders.items_hash(lines),
                    "delivery_agent_id": courier,
                },
                quote.conversation_id,
            )
        except ToolRefused as refused:
            # Fails closed: nothing is placed, and the household is told (§7 seller reject).
            step.updates.clear()
            step.close(quote, "rejected", "seller")
            reason = "items_changed" if refused.reason == "items_changed" else "other"
            step.reject(quote, reason)
            return
        order = await self.store.order(quote.order_id)
        if order is None:
            raise StepFailed("handler", "unknown_order")
        step.send(
            quote.buyer_agent_id,
            "order_confirm",
            {
                "order_id": str(quote.order_id),
                "state": placed.get("state", "placed"),
                "lines": lines,
                "total_minor": quote.total_minor,
                "currency": quote.currency,
                "delivery_agent_id": courier,
                "window": win,
            },
            conversation_id=quote.conversation_id,
        )
        step.send(
            courier,
            "delivery_request",
            {
                "order_id": str(quote.order_id),
                "quote_id": str(chosen.quote_id),
                "window": win,
                "zone_id": order.zone_id,
                "address_ref": str(order.address_ref),
            },
            conversation_id=chosen.conversation_id,
        )

    # The shortfall (§10) ---------------------------------------------------------------

    async def _reply_seconds(self, run_id: UUID) -> int:
        override = (await self.store.overrides(run_id)).get(SHORTFALL_REPLY_OVERRIDE)
        if isinstance(override, int) and not isinstance(override, bool) and override > 0:
            return override
        if self._shortfall_reply_seconds is not None:
            return self._shortfall_reply_seconds
        return get_settings().shortfall_reply_seconds

    async def _shortfall_detected(self, step: _Step) -> None:
        body = step.msg.body
        order_id: UUID = body.order_id  # type: ignore[attr-defined]
        quote = await self.store.accepted_quote(order_id, step.card.agent_id)
        if quote is None:
            raise StepFailed("handler", "quote_not_accepted")
        by_sku = {str(row["sku"]): row for row in await self.store.order_lines(order_id)}
        skus = list(dict.fromkeys(str(s) for s in body.skus))  # type: ignore[attr-defined]
        if not skus or any(sku not in by_sku for sku in skus):
            # `api` checked every SKU is a line of the order; fail closed anyway.
            raise StepFailed("handler", "unknown_line")
        missing = [by_sku[sku] for sku in skus]

        # The household's codes, saved on the accepted quote: never its members' profiles.
        items = quote.items if isinstance(quote.items, Mapping) else {}
        constraints = quote.constraints or {}
        filters = {
            "allergen_codes": sorted(str(a) for a in constraints.get("allergens") or []),
            "diet_codes": sorted(str(d) for d in constraints.get("diets") or []),
            "zone_id": str(items.get("zone_id") or ""),
        }
        requested = {int(line["line"]): line.get("requested_item") for line in quote.lines}

        notice: list[dict[str, Any]] = []
        candidates: dict[str, list[str]] = {}
        found_by_sku: dict[str, dict[str, Any]] = {}
        for row in missing:
            line = int(row["line"])
            qty = int(Decimal(str(row["qty"])))
            line_total = qty * int(row["unit_minor"])
            item = str(requested.get(line) or row["name"])
            query = _substitute_query(item) or item
            found = await self._tool(step, "search_items", {"query": query, "filters": filters})
            hits = [h for h in found.get("items") or [] if str(h["sku"]) not in by_sku]
            priced = await self._price(
                step, [_Want(qty)], [hits[:MAX_SUBSTITUTES]], currency=quote.currency
            )
            # The total can never rise: a substitute costs at most the line it replaces.
            keep = [p for p in priced[0] if p["available"] and qty * p["unit_minor"] <= line_total]
            candidates[str(line)] = [str(p["sku"]) for p in keep]
            for p in keep:
                found_by_sku[str(p["sku"])] = p
            notice.append(
                {
                    "line": line,
                    "sku": str(row["sku"]),
                    "name": str(row["name"]),
                    "qty_missing": qty,
                    "line_total_minor": line_total,
                }
            )

        allowed = PerItem({k: Subset(v, MAX_SHORTFALL_SUBSTITUTES) for k, v in candidates.items()})
        default = {k: v[:MAX_SHORTFALL_SUBSTITUTES] for k, v in candidates.items()}
        rules = await self.store.rules(step.card.agent_id)
        choice = await self.model.choose(
            self._ctx(step),
            "rank_substitutes",
            allowed,
            default,
            self._inputs(step, rules, allowed),
            guidance=seller_prompts.guidance("rank_substitutes", self.area),
        )
        # Code decides: a pick outside the allowed set gives the rule default.
        picks = choice.value if allowed.check(choice.value) else default
        for entry in notice:
            entry["substitutes"] = [
                {
                    "sku": sku,
                    "name": str(found_by_sku[sku].get("name") or sku),
                    "qty": entry["qty_missing"],
                    "unit_minor": int(found_by_sku[sku]["unit_minor"]),
                }
                for sku in picks.get(str(entry["line"])) or []
            ]

        shortfall_id = ids.shortfall_id(order_id)
        reply_by = self.clock() + timedelta(seconds=await self._reply_seconds(step.msg.row.run_id))
        step.send(
            quote.buyer_agent_id,
            "shortfall_notice",
            {
                "order_id": str(order_id),
                "shortfall_id": str(shortfall_id),
                "lines": notice,
                "reply_by": reply_by.isoformat(),
            },
            conversation_id=ids.shortfall_conversation_id(shortfall_id),
        )

    async def _shortfall_decision(self, step: _Step) -> None:
        body = step.msg.body
        order_id: UUID = body.order_id  # type: ignore[attr-defined]
        shortfall_id: UUID = body.shortfall_id  # type: ignore[attr-defined]
        picks = {str(line): str(pick) for line, pick in body.picks.items()}  # type: ignore[attr-defined]
        conversation = ids.shortfall_conversation_id(shortfall_id)
        try:
            amended = await self._tool(
                step,
                "amend_order",
                {"order_id": str(order_id), "shortfall_id": str(shortfall_id), "picks": picks},
                conversation,
            )
        except ToolRefused as refused:
            # Fails closed: the order is not changed.
            await self._decision(step, "shortfall_decision", f"amend_refused_{refused.reason}")
            return
        confirm: dict[str, Any] = {
            "order_id": str(order_id),
            "state": amended["state"],
            "lines": list(amended.get("lines") or []),
            "total_minor": int(amended["total_minor"]),
            "currency": amended["currency"],
        }
        if amended["state"] == "failed":
            confirm["reason"] = NOTHING_TO_DELIVER
        step.send(step.msg.row.from_agent, "order_confirm", confirm, conversation_id=conversation)

    # protocol_error -----------------------------------------------------------------------

    async def _protocol_error(self, step: _Step) -> None:
        await self._decision(step, "protocol_error", str(step.msg.body.code))  # type: ignore[attr-defined]


# The host's handler. Built on first use, after the host has opened the pool.

_QUOTE_COLUMNS = (
    "quote_id, run_id, order_id, conversation_id, seller_agent_id, buyer_agent_id, items,"
    " total_minor, currency, round, expires_at, status, constraints, closed_by"
)


def _stored(row: Mapping[str, Any]) -> StoredQuote:
    return StoredQuote(**{**row, "currency": str(row["currency"]).strip()})


class PgSellerStore:
    """`SellerStore` over Postgres: one short connection per read, no transaction held."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def _one(self, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        from psycopg.rows import dict_row

        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()

    async def quote(self, quote_id: UUID) -> StoredQuote | None:
        row = await self._one(
            f"SELECT {_QUOTE_COLUMNS} FROM quotes WHERE quote_id = %s", (quote_id,)
        )
        return None if row is None else _stored(row)

    async def accepted_quote(self, order_id: UUID, seller: str) -> StoredQuote | None:
        row = await self._one(
            f"SELECT {_QUOTE_COLUMNS} FROM quotes WHERE order_id = %s AND seller_agent_id = %s"
            " AND status = 'accepted' ORDER BY created_at DESC LIMIT 1",
            (order_id, seller),
        )
        return None if row is None else _stored(row)

    async def courier_quotes(self, order_id: UUID, store: str) -> list[CourierQuote]:
        from psycopg.rows import dict_row

        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT q.quote_id, q.conversation_id, q.seller_agent_id, q.total_minor,"
                " q.currency, q.expires_at, q.status, q.items->'window' AS window"
                " FROM quotes q JOIN agent_cards c ON c.agent_id = q.seller_agent_id"
                " WHERE q.order_id = %s AND q.buyer_agent_id = %s AND c.class = 'delivery'"
                " ORDER BY q.seller_agent_id",
                (order_id, store),
            )
            rows = await cur.fetchall()
        return [CourierQuote(**{**r, "currency": str(r["currency"]).strip()}) for r in rows]

    async def order(self, order_id: UUID) -> OrderInfo | None:
        row = await self._one(
            "SELECT o.state, o.address_ref, h.zone_id FROM orders o"
            " JOIN households h ON h.household_id = o.household_id WHERE o.order_id = %s",
            (order_id,),
        )
        return None if row is None else OrderInfo(**row)

    async def rules(self, agent_id: str) -> dict[str, Any]:
        from psycopg.rows import dict_row

        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT kind, value FROM business_rules WHERE agent_id = %s ORDER BY kind",
                (agent_id,),
            )
            return {r["kind"]: r["value"] for r in await cur.fetchall()}

    async def couriers(self, region: str, zone_id: str, currency: str) -> list[str]:
        from services.common import directory

        async with self._pool.connection() as conn:
            cards = await directory.find(conn, "delivery", region, zone_id, currency)
        return [c.agent_id for c in cards]

    async def order_lines(self, order_id: UUID) -> list[dict[str, Any]]:
        from psycopg.rows import dict_row

        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT line, sku, name, qty, unit_minor, substituted_for FROM order_items"
                " WHERE order_id = %s ORDER BY line",
                (order_id,),
            )
            return list(await cur.fetchall())

    async def overrides(self, run_id: UUID) -> dict[str, Any]:
        row = await self._one(
            "SELECT trigger->'overrides' AS overrides FROM runs WHERE run_id = %s", (run_id,)
        )
        value = None if row is None else row["overrides"]
        return value if isinstance(value, dict) else {}


def build_default(area: str) -> SellerHandler:
    """The host's handler for `area`, over the shared pool, trace store and model client."""
    from services.agents.common.model import ModelClient, PgRunState
    from services.common import db
    from services.common.trace import TraceStore

    pool = db.get_pool()
    traces = TraceStore(pool)
    return SellerHandler(
        area,
        model=ModelClient(traces, PgRunState(pool)),
        traces=traces,
        store=PgSellerStore(pool),
    )
