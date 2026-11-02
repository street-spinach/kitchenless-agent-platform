"""The `/mcp/delivery` MCP server (architecture §5 and item 43).

`services/mcp/app.py` mounts `server`. Only enabled delivery cards may call its tools.

*   `quote_delivery(order_id, zone_id, window)`: `{price_minor, currency, windows}`.
*   `book_delivery(order_id, address_ref, window)` *guarded*: relation `agent
    can_deliver_for household:<the order's household>`; limits: `orders.delivery_agent_id`
    is the caller, `limits.check_coverage(<caller card>, households.zone_id)` and
    `limits.check_window(window, now)`. The address comes only from `addresses.resolve`,
    which writes the `data_share` row. Key `idempotency_key(order_id, "book_delivery",
    <courier>)`. Its state change sets `courier_booked`, `external_delivery_id` and
    `delivery_window` together.
*   `get_delivery_status(order_id)`: the courier's state of the order's delivery, for the
    order's courier only.

Each tool picks its adapter from the card's vendor: a `mock` card always uses
`MockCourierAdapter`; a `doordash` card goes through `modes.adapter_for("doordash",
ctx.modes)`, which never falls back to the mock.
"""

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.range import Range

from services.common import db, directory, ids, modes, orders
from services.common.directory import AgentCard
from services.common.protocol.schemas import Window
from services.mcp.delivery.adapters.mock_courier import MockCourierAdapter
from services.mcp.meta import AdapterFailure, CallContext, tool_error, tool_errors
from services.mcp.traced import Tool, traced
from services.safety import addresses, limits
from services.safety.guard import GuardRefused, run_guarded

__all__ = [
    "AUTHZ_DENIED",
    "INVALID_STATE",
    "CourierAdapter",
    "book_delivery",
    "get_delivery_status",
    "quote_delivery",
    "server",
]

AREA = "delivery"
AUTHZ_DENIED = "authz_denied"
INVALID_STATE = "invalid_state"
# The card vendor that resolves through `modes` (vendor key `doordash`).
_VENDOR_KEYS = {"doordash": "doordash"}
# `courier_booked` is accepted so a replayed call returns its stored result.
_BOOK_FROM = ("placed", "courier_booked")

server = MCPServer(name=AREA)


class CourierAdapter(Protocol):
    """The delivery adapter interface (item 44). Real and mock alike."""

    async def quote_delivery(self, zone_id: str, window: Window) -> dict[str, Any]: ...

    async def book_delivery(
        self,
        *,
        idempotency_key: str,
        order_id: str,
        zone_id: str,
        window: Window,
        address: addresses.Address,
    ) -> dict[str, Any]: ...

    async def get_delivery_status(self, external_delivery_id: str) -> dict[str, Any]: ...


def _adapter(card: AgentCard, ctx: CallContext) -> CourierAdapter:
    if card.vendor == "mock":
        cls: type[Any] = MockCourierAdapter
    else:
        vendor = _VENDOR_KEYS.get(card.vendor or "")
        if vendor is None:
            raise AdapterFailure(modes.NOT_CONFIGURED, retryable=False)
        cls = modes.adapter_for(vendor, ctx.modes)
    return cls(card, ctx.env)


async def _courier_card(ctx: CallContext) -> AgentCard:
    async with db.get_pool().connection() as conn:
        try:
            card = await directory.card(conn, ctx.agent_id)
        except directory.UnknownAgent:
            raise GuardRefused(AUTHZ_DENIED) from None
    return card


def _require_courier(card: AgentCard) -> None:
    if card.agent_class != AREA or not card.enabled:
        raise GuardRefused(AUTHZ_DENIED)


def _window(window: Window | Mapping[str, Any]) -> Window:
    return window if isinstance(window, Window) else Window.model_validate(window)


def _wire_window(window: Window) -> dict[str, str]:
    return window.model_dump(mode="json")


async def _order(conn: AsyncConnection, order_id: uuid.UUID) -> dict[str, Any] | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT o.run_id, o.state, o.delivery_agent_id, o.household_id,"
            " o.external_delivery_id, h.zone_id FROM orders o"
            " JOIN households h ON h.household_id = o.household_id WHERE o.order_id = %s",
            (order_id,),
        )
        return await cur.fetchone()


# --- tools ----------------------------------------------------------------------------


@traced(AREA)
async def quote_delivery(
    ctx: CallContext, *, order_id: str, zone_id: str, window: dict[str, Any]
) -> dict[str, Any]:
    """The courier's fee and the free slots inside `window` for `zone_id`."""
    card = await _courier_card(ctx)
    _require_courier(card)
    return await _adapter(card, ctx).quote_delivery(zone_id, _window(window))


async def book_delivery(
    ctx: CallContext,
    *,
    order_id: uuid.UUID,
    address_ref: uuid.UUID,
    window: Window | Mapping[str, Any],
) -> dict[str, Any]:
    """Book the order's delivery with the calling courier (guarded, §5)."""
    win = _window(window)
    card = await _courier_card(ctx)
    async with db.get_pool().connection() as conn:
        found = await _order(conn, order_id)
    household = None if found is None else found["household_id"]
    key = ids.idempotency_key(order_id, "book_delivery", ctx.agent_id)
    order: dict[str, Any] = {}
    address: list[addresses.Address] = []

    async def check(conn: AsyncConnection) -> None:
        _require_courier(card)
        row = await _order(conn, order_id)
        if row is None or row["run_id"] != ctx.run_id or row["household_id"] != household:
            raise GuardRefused(AUTHZ_DENIED)
        # The §5 limit: the order's chosen courier is the caller.
        if row["delivery_agent_id"] != ctx.agent_id:
            raise GuardRefused(AUTHZ_DENIED)
        if row["state"] not in _BOOK_FROM:
            raise GuardRefused(INVALID_STATE)
        limits.check_coverage(card, row["zone_id"])
        limits.check_window(win, datetime.now(UTC))
        order.update(row)
        # Last, so the `data_share` row is only written once every other limit passed.
        address.append(await addresses.resolve(conn, ctx, address_ref, order_id))

    async def call() -> dict[str, Any]:
        booked = await _adapter(card, ctx).book_delivery(
            idempotency_key=ids.vendor_key(key),
            order_id=str(order_id),
            zone_id=order["zone_id"],
            window=win,
            address=address[0],
        )
        return {
            "order_id": str(order_id),
            "state": "courier_booked",
            "external_delivery_id": booked["external_delivery_id"],
            "window": booked["window"],
        }

    async def apply(conn: AsyncConnection, result: Any) -> None:
        moved = await orders.transition(
            conn,
            order_id,
            "placed",
            "courier_booked",
            external_delivery_id=result["external_delivery_id"],
            delivery_window=Range(win.start, win.end, "[)"),
        )
        if not moved:
            raise RuntimeError(f"order {order_id} is no longer placed")

    return await run_guarded(
        ctx,
        tool=f"{AREA}.book_delivery",
        relation=None if household is None else ("can_deliver_for", f"household:{household}"),
        limits=check,
        key=key,
        request={
            "order_id": str(order_id),
            "address_ref": str(address_ref),
            "window": _wire_window(win),
        },
        call=call,
        apply=apply,
    )


@traced(AREA)
async def get_delivery_status(ctx: CallContext, *, order_id: str) -> dict[str, Any]:
    """The courier's state of the order's delivery. Only the order's courier may ask."""
    card = await _courier_card(ctx)
    _require_courier(card)
    async with db.get_pool().connection() as conn:
        row = await _order(conn, uuid.UUID(order_id))
    if row is None or row["delivery_agent_id"] != ctx.agent_id:
        raise GuardRefused(AUTHZ_DENIED)
    if row["external_delivery_id"] is None:
        raise GuardRefused(INVALID_STATE)
    status = await _adapter(card, ctx).get_delivery_status(row["external_delivery_id"])
    return {"order_id": order_id, **status}


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


@server.tool(name="quote_delivery")
async def _quote_delivery_tool(
    order_id: uuid.UUID, zone_id: str, window: Window, ctx: Context
) -> CallToolResult:
    return await _serve(
        ctx,
        quote_delivery,
        order_id=str(order_id),
        zone_id=zone_id,
        window=_wire_window(window),
    )


@server.tool(name="book_delivery")
async def _book_delivery_tool(
    order_id: uuid.UUID, address_ref: uuid.UUID, window: Window, ctx: Context
) -> CallToolResult:
    return await _serve(
        ctx, book_delivery, order_id=order_id, address_ref=address_ref, window=window
    )


@server.tool(name="get_delivery_status")
async def _get_delivery_status_tool(order_id: uuid.UUID, ctx: Context) -> CallToolResult:
    return await _serve(ctx, get_delivery_status, order_id=str(order_id))
