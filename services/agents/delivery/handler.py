"""The delivery class handler (architecture item 30, agent-protocol P24).

The handler is stateless (architecture §4). It reads only the rows it needs and returns
its `quotes` insert with its messages in one `StepResult`, so a replayed step sends the same
message ids and changes nothing twice.

*   `delivery_quote_request`: `/mcp/delivery quote_delivery` gives the price and the
    windows the courier's capacity allows inside the requested window. One
    `choose(choose_window)` call over that set, with the earliest window as its rule
    default. The courier inserts its `quotes` row (itself as seller, the store as buyer,
    `expires_at = now() + COURIER_QUOTE_TTL_SECONDS`) and replies `delivery_quote` with the
    chosen window, that `expires_at` and the rationale. With no allowed window it sends
    nothing and writes a `decision` trace; the store's round then closes without it.
*   `delivery_request`: the window must be the one this courier quoted for the order. Then
    `book_delivery(order_id, address_ref, window)`; the server keys it
    `idempotency_key(order_id, "book_delivery", <courier>)` from the caller. Then
    `delivery_booked` to the seller and the order's household agent.
*   `protocol_error`: a `decision` trace only.

**Model proposes, code decides** (architecture §4): a pick outside the allowed set gives
the rule default.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from services.agents.common.model import Allowed, Choice, OneOf
from services.common import ids, mcp_client
from services.common.config import get_settings
from services.common.consumer import Inbound, Outgoing, QuoteInsert, StepFailed, StepResult
from services.common.directory import AgentCard
from services.common.mcp_client import ToolRefused
from services.common.protocol.schemas import Window
from services.common.run_context import RunContext
from services.identity.tokens import MockIssuer

__all__ = [
    "AREA",
    "HANDLED_TYPES",
    "CourierStore",
    "DeliveryHandler",
    "OwnQuote",
    "PgCourierStore",
    "UnsupportedMessage",
    "allowed_windows",
    "handle",
]

log = logging.getLogger(__name__)

AREA = "delivery"
HANDLED_TYPES = frozenset({"delivery_quote_request", "delivery_request", "protocol_error"})


class UnsupportedMessage(ValueError):
    """The delivery handler has no path for this message type."""


@dataclass(frozen=True)
class OwnQuote:
    """The courier's own `quotes` row for an order, as `delivery_request` checks it."""

    quote_id: UUID
    order_id: UUID
    seller_agent_id: str
    buyer_agent_id: str
    window: Mapping[str, Any] | None


class CourierStore(Protocol):
    """The rows a courier step reads. `PgCourierStore` reads Postgres; tests use fakes."""

    async def quote(self, quote_id: UUID) -> OwnQuote | None: ...

    async def household(self, order_id: UUID) -> str | None: ...


class Chooser(Protocol):
    async def choose(
        self,
        ctx: RunContext,
        point: str,
        allowed: Allowed,
        default: Any,
        inputs: Mapping[str, Any],
    ) -> Choice: ...


class TraceSink(Protocol):
    async def write(
        self, run_id: Any, agent_id: str, kind: str, payload: dict[str, Any], **cols: Any
    ) -> int: ...


CallTool = Callable[..., Awaitable[dict[str, Any]]]
IssueToken = Callable[[str, str, Sequence[str]], str]
Clock = Callable[[], datetime]


def _mock_token(sub: str, act: str, scopes: Sequence[str]) -> str:
    # Every run uses the mock issuer until identity-authz replaces the token source (Q4).
    return MockIssuer().issue(sub, act, list(scopes))


def _now() -> datetime:
    return datetime.now(UTC)


def _parse(window: Any) -> Window | None:
    try:
        return Window.model_validate(window)
    except (ValueError, TypeError):
        return None


def allowed_windows(windows: Any, requested: Window) -> list[dict[str, str]]:
    """The adapter's windows that are well formed and inside `requested`, in start order."""
    out: list[tuple[datetime, dict[str, str]]] = []
    for raw in windows if isinstance(windows, list) else []:
        win = _parse(raw)
        if win is None:
            continue
        if requested.start <= win.start < win.end <= requested.end:
            out.append((win.start, win.model_dump(mode="json")))
    return [w for _, w in sorted(out, key=lambda o: o[0])]


class _Step:
    """One inbound message and the messages and quote row it produces."""

    def __init__(self, msg: Inbound, card: AgentCard) -> None:
        self.msg = msg
        self.card = card
        self.messages: list[Outgoing] = []
        self.inserts: list[QuoteInsert] = []

    def send(
        self, to_agent: str, type: str, body: dict[str, Any], *, conversation_id: UUID | None
    ) -> None:
        self.messages.append(
            Outgoing(
                message_id=ids.message_id(self.msg.row.message_id, len(self.messages)),
                from_agent=self.card.agent_id,
                to_agent=to_agent,
                type=type,
                body={"schema_version": 1, **body},
                conversation_id=conversation_id,
                causation_id=self.msg.row.message_id,
            )
        )

    def result(self) -> StepResult:
        return StepResult(messages=self.messages, quote_inserts=self.inserts)


class DeliveryHandler:
    """The delivery class handler, `handle(msg, card) -> StepResult`."""

    def __init__(
        self,
        *,
        model: Chooser,
        traces: TraceSink,
        store: CourierStore,
        call_tool: CallTool = mcp_client.call_tool,
        issue_token: IssueToken = _mock_token,
        clock: Clock = _now,
        quote_ttl_seconds: int | None = None,
    ) -> None:
        self.model = model
        self.traces = traces
        self.store = store
        self.call_tool = call_tool
        self.issue_token = issue_token
        self.clock = clock
        self.quote_ttl = timedelta(
            seconds=quote_ttl_seconds
            if quote_ttl_seconds is not None
            else get_settings().courier_quote_ttl_seconds
        )

    async def __call__(self, msg: Inbound, card: AgentCard) -> StepResult:
        t = msg.row.type
        if t not in HANDLED_TYPES:
            raise UnsupportedMessage(f"the {AREA} handler does not handle {t!r}")
        if card.agent_class != AREA:
            raise UnsupportedMessage(f"{card.agent_id} is not a {AREA} card")
        step = _Step(msg, card)
        await getattr(self, f"_{t}")(step)
        return step.result()

    # Helpers ------------------------------------------------------------------------------

    def _ctx(self, step: _Step) -> RunContext:
        return RunContext(
            run_id=step.msg.row.run_id,
            agent_id=step.card.agent_id,
            conversation_id=step.msg.row.conversation_id,
        )

    async def _tool(self, step: _Step, name: str, args: dict[str, Any]) -> dict[str, Any]:
        # A courier acts for its own org with a service token, as sellers do.
        token = self.issue_token(step.card.owner_ref, step.card.agent_id, ())
        return await self.call_tool(AREA, name, args, ctx=self._ctx(step), token=token)

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

    # delivery_quote_request ---------------------------------------------------------------

    async def _delivery_quote_request(self, step: _Step) -> None:
        body = step.msg.body
        row = step.msg.row
        if row.conversation_id is None:
            raise StepFailed("handler", "no_conversation")
        requested: Window = body.window  # type: ignore[attr-defined]
        result = await self._tool(
            step,
            "quote_delivery",
            {
                "order_id": str(body.order_id),  # type: ignore[attr-defined]
                "zone_id": body.zone_id,  # type: ignore[attr-defined]
                "window": requested.model_dump(mode="json"),
            },
        )
        price = result.get("price_minor")
        currency = result.get("currency")
        if isinstance(price, bool) or not isinstance(price, int) or price < 0:
            raise StepFailed("handler", "bad_courier_price")
        if not isinstance(currency, str) or (step.card.currency and currency != step.card.currency):
            raise StepFailed("handler", "currency_mismatch")
        windows = allowed_windows(result.get("windows"), requested)
        if not windows:
            await self._decision(step, "choose_window", "no_window")
            return

        allowed = OneOf(windows)
        default = windows[0]
        message = {k: v for k, v in body.model_dump(mode="json").items() if k != "rationale"}
        choice = await self.model.choose(
            self._ctx(step),
            "choose_window",
            allowed,
            default,
            {
                "message": {"type": row.type, "body": message},
                "price_minor": price,
                "allowed": allowed.describe(),
            },
        )
        # Code decides: a pick outside the allowed set gives the rule default.
        window = choice.value if allowed.check(choice.value) else default

        quote_id = ids.quote_id(row.message_id)
        expires_at = self.clock() + self.quote_ttl
        step.inserts.append(
            QuoteInsert(
                quote_id=quote_id,
                run_id=row.run_id,
                order_id=body.order_id,  # type: ignore[attr-defined]
                conversation_id=row.conversation_id,
                seller_agent_id=step.card.agent_id,
                buyer_agent_id=row.from_agent,
                items={"window": window},
                total_minor=price,
                currency=currency,
                expires_at=expires_at,
            )
        )
        step.send(
            row.from_agent,
            "delivery_quote",
            {
                "order_id": str(body.order_id),  # type: ignore[attr-defined]
                "quote_id": str(quote_id),
                "window": window,
                "price_minor": price,
                "currency": currency,
                "expires_at": expires_at.isoformat(),
                "rationale": choice.rationale,
            },
            conversation_id=row.conversation_id,
        )

    # delivery_request ---------------------------------------------------------------------

    async def _delivery_request(self, step: _Step) -> None:
        body = step.msg.body
        row = step.msg.row
        order_id: UUID = body.order_id  # type: ignore[attr-defined]
        window: Window = body.window  # type: ignore[attr-defined]
        quote = await self.store.quote(body.quote_id)  # type: ignore[attr-defined]
        if (
            quote is None
            or quote.seller_agent_id != step.card.agent_id
            or quote.buyer_agent_id != row.from_agent
            or quote.order_id != order_id
        ):
            raise StepFailed("handler", "unknown_quote")
        quoted = _parse(quote.window)
        if quoted is None or (quoted.start, quoted.end) != (window.start, window.end):
            # Book only the window this courier's capacity allowed in its own quote.
            raise StepFailed("handler", "window_mismatch")
        household = await self.store.household(order_id)
        if household is None:
            raise StepFailed("handler", "unknown_order")

        try:
            booked = await self._tool(
                step,
                "book_delivery",
                {
                    "order_id": str(order_id),
                    "address_ref": str(body.address_ref),  # type: ignore[attr-defined]
                    "window": window.model_dump(mode="json"),
                },
            )
        except ToolRefused as refused:
            # Fails closed: nothing is booked and no `delivery_booked` is sent.
            raise StepFailed("book_delivery", refused.reason) from None

        booked_window = _parse(booked.get("window")) or window
        update = {
            "order_id": str(order_id),
            "state": "courier_booked",
            "delivery_agent_id": step.card.agent_id,
            "window": booked_window.model_dump(mode="json"),
        }
        step.send(row.from_agent, "delivery_booked", update, conversation_id=row.conversation_id)
        # The courier is in no conversation with the household.
        step.send(household, "delivery_booked", update, conversation_id=None)

    # protocol_error -----------------------------------------------------------------------

    async def _protocol_error(self, step: _Step) -> None:
        await self._decision(step, "protocol_error", str(step.msg.body.code))  # type: ignore[attr-defined]


# The host's handler. Built on first use, after the host has opened the pool.


class PgCourierStore:
    """`CourierStore` over Postgres: one short connection per read, no transaction held."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def _one(self, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        from psycopg.rows import dict_row

        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()

    async def quote(self, quote_id: UUID) -> OwnQuote | None:
        row = await self._one(
            "SELECT quote_id, order_id, seller_agent_id, buyer_agent_id,"
            " items->'window' AS window FROM quotes WHERE quote_id = %s",
            (quote_id,),
        )
        return None if row is None else OwnQuote(**row)

    async def household(self, order_id: UUID) -> str | None:
        row = await self._one("SELECT household_id FROM orders WHERE order_id = %s", (order_id,))
        return None if row is None else f"household:{row['household_id']}"


def build_default() -> DeliveryHandler:
    """The host's handler, over the shared pool, trace store and model client."""
    from services.agents.common.model import ModelClient, PgRunState
    from services.common import db
    from services.common.trace import TraceStore

    pool = db.get_pool()
    traces = TraceStore(pool)
    return DeliveryHandler(
        model=ModelClient(traces, PgRunState(pool)),
        traces=traces,
        store=PgCourierStore(pool),
    )


_default: DeliveryHandler | None = None


async def handle(msg: Inbound, card: AgentCard) -> StepResult:
    """The delivery class handler registered in `services.agents.host.HANDLERS`."""
    global _default
    if _default is None:
        _default = build_default()
    return await _default(msg, card)
