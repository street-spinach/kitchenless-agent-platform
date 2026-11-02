"""The seller tools shared by `/mcp/grocery` and `/mcp/restaurant` (architecture §5, item 43).

*   `search_items(query, filters)`: the calling card's items that pass the exact filters,
    ranked by full-text match (§5 "Search").
*   `quote(items)`: price and availability for each requested `{sku, qty}`.
*   `place_order(order_id, quote_id, items_hash, delivery_agent_id)` *guarded*: relation
    `agent member_of org`; limits: the order is `authorized` with this seller and the same
    `items_hash`, and `limits.check_coverage(<delivery_agent_id card>, households.zone_id)`
    passes; key `idempotency_key(order_id, "place_order", <seller>)`. Its state change
    sets `placed`, `delivery_agent_id` and `external_order_id` together.

Only cards of the server's class may call its tools. Each tool picks its adapter from the
card's vendor: a `mock` card always uses the area's mock adapter; any other vendor goes
through `modes.adapter_for(vendor, ctx.modes)`, which never falls back to the mock.
`amend_order` is added by its own issue.
"""

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import BaseModel

from services.common import db, directory, ids, modes, orders
from services.common.directory import AgentCard
from services.common.protocol.schemas import AllergenCode, DietCode, Minor, Qty
from services.mcp.meta import AdapterFailure, CallContext, tool_error, tool_errors
from services.mcp.traced import Tool, traced
from services.safety import limits
from services.safety.guard import GuardRefused, run_guarded

__all__ = [
    "AUTHZ_DENIED",
    "INVALID_COURIER",
    "INVALID_STATE",
    "ITEMS_CHANGED",
    "QuoteItem",
    "SearchFilters",
    "SellerAdapter",
    "SellerTools",
    "register",
]

AUTHZ_DENIED = "authz_denied"
INVALID_STATE = "invalid_state"
# The order's items changed since the household authorized them (agent-protocol
# `items_changed`).
ITEMS_CHANGED = "items_changed"
# `delivery_agent_id` is not an enabled delivery card.
INVALID_COURIER = "invalid_courier"


class SellerAdapter(Protocol):
    """The grocery and restaurant adapter interface (item 44). Real and mock alike."""

    async def search_items(self, query: str, filters: Mapping[str, Any]) -> dict[str, Any]: ...

    async def quote(self, items: Sequence[Mapping[str, Any]]) -> dict[str, Any]: ...

    async def place_order(
        self,
        *,
        idempotency_key: str,
        order_id: str,
        lines: Sequence[Mapping[str, Any]],
        currency: str,
    ) -> dict[str, Any]: ...


class SearchFilters(BaseModel):
    """`search_items` filters: excluded allergens, required diets, zone and price cap."""

    allergen_codes: list[AllergenCode] = []
    diet_codes: list[DietCode] = []
    zone_id: str | None = None
    max_price_minor: Minor | None = None


class QuoteItem(BaseModel):
    sku: str
    qty: Qty


@dataclass(frozen=True)
class SellerTools:
    """The three seller tools of one area, bound to its mock adapter and vendor keys.

    `vendor_keys` maps a card's `vendor` to the `modes` vendor key of each tool.
    """

    area: str
    mock_adapter: type[Any]
    vendor_keys: Mapping[str, Mapping[str, str]]

    def adapter(self, card: AgentCard, tool: str, ctx: CallContext) -> SellerAdapter:
        if card.vendor == "mock":
            cls = self.mock_adapter
        else:
            vendor = self.vendor_keys.get(card.vendor or "", {}).get(tool)
            if vendor is None:
                raise AdapterFailure(modes.NOT_CONFIGURED, retryable=False)
            cls = modes.adapter_for(vendor, ctx.modes)
        return cls(card, ctx.env)

    async def seller_card(self, ctx: CallContext) -> AgentCard:
        async with db.get_pool().connection() as conn:
            try:
                return await directory.card(conn, ctx.agent_id)
            except directory.UnknownAgent:
                raise GuardRefused(AUTHZ_DENIED) from None

    def require_seller(self, card: AgentCard) -> None:
        if card.agent_class != self.area or not card.enabled:
            raise GuardRefused(AUTHZ_DENIED)

    def build(self) -> dict[str, Tool[dict[str, Any]]]:
        """The `search_items`, `quote` and `place_order` tool functions."""
        tools = self

        @traced(self.area)
        async def search_items(
            ctx: CallContext, *, query: str, filters: dict[str, Any]
        ) -> dict[str, Any]:
            card = await tools.seller_card(ctx)
            tools.require_seller(card)
            return await tools.adapter(card, "search_items", ctx).search_items(query, filters)

        @traced(self.area)
        async def quote(ctx: CallContext, *, items: list[dict[str, Any]]) -> dict[str, Any]:
            card = await tools.seller_card(ctx)
            tools.require_seller(card)
            return await tools.adapter(card, "quote", ctx).quote(items)

        async def place_order(
            ctx: CallContext,
            *,
            order_id: uuid.UUID,
            quote_id: uuid.UUID,
            items_hash: str,
            delivery_agent_id: str,
        ) -> dict[str, Any]:
            return await tools.place_order(
                ctx,
                order_id=order_id,
                quote_id=quote_id,
                items_hash=items_hash,
                delivery_agent_id=delivery_agent_id,
            )

        return {"search_items": search_items, "quote": quote, "place_order": place_order}

    async def place_order(
        self,
        ctx: CallContext,
        *,
        order_id: uuid.UUID,
        quote_id: uuid.UUID,
        items_hash: str,
        delivery_agent_id: str,
    ) -> dict[str, Any]:
        card = await self.seller_card(ctx)
        org = card.owner_ref if card.owner_ref.startswith("org:") else None
        key = ids.idempotency_key(order_id, "place_order", ctx.agent_id)
        order: dict[str, Any] = {}

        async def check(conn: AsyncConnection) -> None:
            self.require_seller(card)
            if org is None:
                raise GuardRefused(AUTHZ_DENIED)
            order.update(await _order(conn, order_id))
            if order["run_id"] != ctx.run_id or order["seller_agent_id"] != ctx.agent_id:
                raise GuardRefused(AUTHZ_DENIED)
            # `placed` with this courier is a replay; the guard returns the stored result.
            replay = order["state"] == "placed" and order["delivery_agent_id"] == delivery_agent_id
            if order["state"] != "authorized" and not replay:
                raise GuardRefused(INVALID_STATE)
            if order["items_hash"] != items_hash:
                raise GuardRefused(ITEMS_CHANGED)
            try:
                courier = await directory.card(conn, delivery_agent_id)
            except directory.UnknownAgent:
                raise GuardRefused(INVALID_COURIER) from None
            if courier.agent_class != "delivery" or not courier.enabled:
                raise GuardRefused(INVALID_COURIER)
            limits.check_coverage(courier, order["zone_id"])

        async def call() -> dict[str, Any]:
            placed = await self.adapter(card, "place_order", ctx).place_order(
                idempotency_key=ids.vendor_key(key),
                order_id=str(order_id),
                lines=order["lines"],
                currency=order["currency"],
            )
            return {
                "order_id": str(order_id),
                "quote_id": str(quote_id),
                "state": "placed",
                "external_order_id": placed["external_order_id"],
                "delivery_agent_id": delivery_agent_id,
                "total_minor": placed["total_minor"],
                "currency": placed["currency"],
            }

        async def apply(conn: AsyncConnection, result: Any) -> None:
            moved = await orders.transition(
                conn,
                order_id,
                "authorized",
                "placed",
                delivery_agent_id=delivery_agent_id,
                external_order_id=result["external_order_id"],
            )
            if not moved:
                raise RuntimeError(f"order {order_id} is no longer authorized")

        return await run_guarded(
            ctx,
            tool=f"{self.area}.place_order",
            relation=None if org is None else ("member_of", org),
            limits=check,
            key=key,
            request={
                "order_id": str(order_id),
                "quote_id": str(quote_id),
                "items_hash": items_hash,
                "delivery_agent_id": delivery_agent_id,
            },
            call=call,
            apply=apply,
        )


async def _order(conn: AsyncConnection, order_id: uuid.UUID) -> dict[str, Any]:
    """The order, its household's zone and its lines; refused when it does not exist."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT o.run_id, o.state, o.seller_agent_id, o.delivery_agent_id, o.items_hash,"
            " o.currency, h.zone_id FROM orders o"
            " JOIN households h ON h.household_id = o.household_id WHERE o.order_id = %s",
            (order_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise GuardRefused(AUTHZ_DENIED)
        await cur.execute(
            "SELECT sku, qty, unit_minor FROM order_items WHERE order_id = %s ORDER BY line",
            (order_id,),
        )
        lines = await cur.fetchall()
    return {
        **row,
        "currency": str(row["currency"]).strip(),
        "lines": [
            {"sku": r["sku"], "qty": Decimal(r["qty"]), "unit_minor": int(r["unit_minor"])}
            for r in lines
        ],
    }


# --- MCP registration -----------------------------------------------------------------


async def _serve(
    area: str, mcp_ctx: Context, tool: Tool[dict[str, Any]], **args: Any
) -> CallToolResult:
    try:
        ctx = await CallContext.from_meta(mcp_ctx.request_context.meta)
    except GuardRefused as e:
        return tool_error("refused", e.reason)
    async with tool_errors(ctx, area, tool.__name__) as err:
        body = await tool(ctx, **args)
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(body))],
            structured_content=body,
        )
    assert err.result is not None
    return err.result


def register(server: MCPServer, tools: Mapping[str, Tool[dict[str, Any]]], area: str) -> None:
    """Add the seller tools to `server`."""

    @server.tool(name="search_items")
    async def _search_items_tool(
        query: str, filters: SearchFilters, ctx: Context
    ) -> CallToolResult:
        return await _serve(
            area,
            ctx,
            tools["search_items"],
            query=query,
            filters=filters.model_dump(mode="json", exclude_none=True),
        )

    @server.tool(name="quote")
    async def _quote_tool(items: list[QuoteItem], ctx: Context) -> CallToolResult:
        return await _serve(
            area, ctx, tools["quote"], items=[i.model_dump(mode="json") for i in items]
        )

    @server.tool(name="place_order")
    async def _place_order_tool(
        order_id: uuid.UUID,
        quote_id: uuid.UUID,
        items_hash: str,
        delivery_agent_id: str,
        ctx: Context,
    ) -> CallToolResult:
        return await _serve(
            area,
            ctx,
            tools["place_order"],
            order_id=order_id,
            quote_id=quote_id,
            items_hash=items_hash,
            delivery_agent_id=delivery_agent_id,
        )
