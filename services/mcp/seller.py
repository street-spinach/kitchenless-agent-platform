"""The seller tools shared by `/mcp/grocery` and `/mcp/restaurant` (architecture §5, item 43).

*   `search_items(query, filters)`: the calling card's items that pass the exact filters,
    ranked by full-text match (§5 "Search").
*   `quote(items)`: price and availability for each requested `{sku, qty}`.
*   `place_order(order_id, quote_id, items_hash, delivery_agent_id)` *guarded*: relation
    `agent member_of org`; limits: the order is `authorized` with this seller and the same
    `items_hash`, and `limits.check_coverage(<delivery_agent_id card>, households.zone_id)`
    passes; key `idempotency_key(order_id, "place_order", <seller>)`. Its state change
    sets `placed`, `delivery_agent_id` and `external_order_id` together.
*   `amend_order(order_id, shortfall_id, picks)` *guarded* (agent-protocol §10
    "Amending", P26): relation `agent member_of org`; limits: the order is this seller's
    and `courier_booked` or `in_delivery`, and `limits.check_amend_order` passes; key
    `idempotency_key(order_id, "amend_order", <seller>, scope=shortfall_id)`. On an
    `in_delivery` order every substitute becomes a drop in the same call (reported in
    `refused`). Its state change is `orders.apply_amendment` and
    `shortfalls.status = 'applied'`.

Only cards of the server's class may call its tools. Each tool picks its adapter from the
card's vendor key, `vendors.card_vendor_key(area, card, tool)`: a `mock` card (`None`)
always uses the area's mock adapter; any other key goes through
`modes.adapter_for(key, ctx.modes)`, which never falls back to the mock.

`place_order` and `amend_order` resolve the adapter before the guard. The first limit
refuses with `unsupported` when the tool is not in the adapter class's `OPERATIONS`
(adapters §8), so the refusal comes before the idempotency key is claimed.
"""

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, ClassVar, Protocol

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import BaseModel

from services.common import db, directory, ids, modes, orders, vendors
from services.common.directory import AgentCard
from services.common.protocol.schemas import AllergenCode, DietCode, Minor, Qty
from services.mcp.meta import CallContext, tool_error, tool_errors
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
    "UNSUPPORTED",
    "register",
]

AUTHZ_DENIED = "authz_denied"
INVALID_STATE = "invalid_state"
# The order's items changed since the household authorized them (agent-protocol
# `items_changed`).
ITEMS_CHANGED = "items_changed"
# `delivery_agent_id` is not an enabled delivery card.
INVALID_COURIER = "invalid_courier"
# The card's adapter class does not support this guarded tool (adapters §8).
UNSUPPORTED = "unsupported"
# `amend_order` changes an order only after the courier is booked and before `delivered`.
_AMEND_STATES = frozenset({"courier_booked", "in_delivery"})


class SellerAdapter(Protocol):
    """The grocery and restaurant adapter interface (item 44). Real and mock alike.

    `OPERATIONS` names the guarded seller tools the class supports (adapters §8).
    """

    OPERATIONS: ClassVar[frozenset[str]]

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

    async def amend_order(
        self,
        *,
        external_order_id: str,
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
    """The seller tools of one area, bound to its mock adapter."""

    area: str
    mock_adapter: type[Any]

    def adapter(self, card: AgentCard, tool: str, ctx: CallContext) -> SellerAdapter:
        key = vendors.card_vendor_key(self.area, card, tool)
        cls = self.mock_adapter if key is None else modes.adapter_for(key, ctx.modes)
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

        async def amend_order(
            ctx: CallContext,
            *,
            order_id: uuid.UUID,
            shortfall_id: uuid.UUID,
            picks: Mapping[Any, str],
        ) -> dict[str, Any]:
            return await tools.amend_order(
                ctx, order_id=order_id, shortfall_id=shortfall_id, picks=picks
            )

        return {
            "search_items": search_items,
            "quote": quote,
            "place_order": place_order,
            "amend_order": amend_order,
        }

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
        adapter = self.adapter(card, "place_order", ctx)
        org = card.owner_ref if card.owner_ref.startswith("org:") else None
        key = ids.idempotency_key(order_id, "place_order", ctx.agent_id)
        order: dict[str, Any] = {}

        async def check(conn: AsyncConnection) -> None:
            if "place_order" not in type(adapter).OPERATIONS:
                raise GuardRefused(UNSUPPORTED)
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
            placed = await adapter.place_order(
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

    async def amend_order(
        self,
        ctx: CallContext,
        *,
        order_id: uuid.UUID,
        shortfall_id: uuid.UUID,
        picks: Mapping[Any, str],
    ) -> dict[str, Any]:
        card = await self.seller_card(ctx)
        adapter = self.adapter(card, "amend_order", ctx)
        org = card.owner_ref if card.owner_ref.startswith("org:") else None
        given = {str(int(line)): str(pick) for line, pick in picks.items()}
        final: dict[str, str] = {}
        refused: list[int] = []
        order: dict[str, Any] = {}

        async def check(conn: AsyncConnection) -> None:
            if "amend_order" not in type(adapter).OPERATIONS:
                raise GuardRefused(UNSUPPORTED)
            self.require_seller(card)
            if org is None:
                raise GuardRefused(AUTHZ_DENIED)
            order.update(await _order(conn, order_id))
            if order["run_id"] != ctx.run_id or order["seller_agent_id"] != ctx.agent_id:
                raise GuardRefused(AUTHZ_DENIED)
            await limits.check_amend_order(
                conn,
                order_id=order_id,
                shortfall_id=shortfall_id,
                seller_agent_id=ctx.agent_id,
                household_agent_id=f"household:{order['household_id']}",
                picks=given,
            )
            cur = await conn.execute(
                "SELECT lines, status FROM shortfalls WHERE shortfall_id = %s", (shortfall_id,)
            )
            lines, status = await cur.fetchone() or (None, None)
            # An `applied` shortfall is a replay; the guard returns the stored result.
            if status != "applied" and order["state"] not in _AMEND_STATES:
                raise GuardRefused(INVALID_STATE)
            if order["external_order_id"] is None:
                raise GuardRefused(INVALID_STATE)
            order["notice"] = lines
            # Substitutes need `courier_booked`: in delivery, each becomes a drop (§10).
            for line, pick in sorted(given.items(), key=lambda kv: int(kv[0])):
                if order["state"] == "in_delivery" and pick != limits.DROP:
                    final[line] = limits.DROP
                    refused.append(int(line))
                else:
                    final[line] = pick

        def amend_picks() -> dict[int, str]:
            return {int(line): pick for line, pick in final.items()}

        async def call() -> dict[str, Any]:
            # The amended lines, computed by `apply_amendment` in a rolled-back transaction
            # so the vendor gets exactly the lines step 7 will store.
            async with db.get_pool().connection() as conn:
                async with conn.transaction(force_rollback=True):
                    preview = await orders.apply_amendment(
                        conn, order_id, amend_picks(), order["notice"]
                    )
            lines = [_wire_line(row) for row in preview.lines]
            amended = await adapter.amend_order(
                external_order_id=order["external_order_id"],
                lines=[
                    {"sku": i["sku"], "qty": i["qty"], "unit_minor": i["unit_minor"]} for i in lines
                ],
                currency=order["currency"],
            )
            return {
                "order_id": str(order_id),
                "shortfall_id": str(shortfall_id),
                "state": preview.state,
                "picks": final,
                "refused": refused,
                "substituted": preview.substituted,
                "dropped": preview.dropped,
                "lines": lines,
                "total_minor": preview.total_minor,
                "currency": order["currency"],
                "items_hash": preview.items_hash,
                "external_order_id": amended["external_order_id"],
            }

        async def apply(conn: AsyncConnection, result: Any) -> None:
            applied = await orders.apply_amendment(conn, order_id, amend_picks(), order["notice"])
            # The order may have moved to `in_delivery` since the limits ran: never apply a
            # substitute then, and never store lines other than the ones sent to the vendor.
            if applied.substituted and applied.state != "courier_booked":
                raise RuntimeError(f"order {order_id} left courier_booked during amend_order")
            if applied.items_hash != result["items_hash"]:
                raise RuntimeError(f"order {order_id} changed during amend_order")
            cur = await conn.execute(
                "UPDATE shortfalls SET status = 'applied'"
                " WHERE shortfall_id = %s AND status = 'closed'",
                (shortfall_id,),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"shortfall {shortfall_id} is no longer closed")

        return await run_guarded(
            ctx,
            tool=f"{self.area}.amend_order",
            relation=None if org is None else ("member_of", org),
            limits=check,
            key=ids.idempotency_key(order_id, "amend_order", ctx.agent_id, scope=shortfall_id),
            request={
                "order_id": str(order_id),
                "shortfall_id": str(shortfall_id),
                "picks": given,
            },
            call=call,
            apply=apply,
        )


def _wire_line(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "line": int(row["line"]),
        "sku": row["sku"],
        "name": row["name"],
        "qty": int(Decimal(row["qty"])),
        "unit_minor": int(row["unit_minor"]),
        "substituted_for": row["substituted_for"],
    }


async def _order(conn: AsyncConnection, order_id: uuid.UUID) -> dict[str, Any]:
    """The order, its household's zone and its lines; refused when it does not exist."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT o.run_id, o.household_id, o.state, o.seller_agent_id, o.delivery_agent_id,"
            " o.external_order_id, o.items_hash, o.currency, h.zone_id FROM orders o"
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

    @server.tool(name="amend_order")
    async def _amend_order_tool(
        order_id: uuid.UUID, shortfall_id: uuid.UUID, picks: dict[int, str], ctx: Context
    ) -> CallToolResult:
        return await _serve(
            area,
            ctx,
            tools["amend_order"],
            order_id=order_id,
            shortfall_id=shortfall_id,
            picks=picks,
        )
