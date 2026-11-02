"""Mock store backend: search, stock, pricing and orders (architecture item 45, §5).

Everything works over the `mock_*` rows of one card and one `env` (the call's). Active
`mock_events` rows for that card and `env` apply (item 45, amended 2026-11-02, #47):

*   `adapter_down`: every call raises `AdapterFailure("adapter_5xx", retryable=True)`.
*   `out_of_stock` (`params: {sku}`): that SKU's available quantity is 0.
*   `kitchen_overloaded` applies to restaurants only (`services/mocks/restaurants.py`).

A SKU's available quantity is `mock_stock.qty` (0 without a row), or 0 while an
`out_of_stock` event names it. `place_order` decrements `mock_stock.qty` conditionally, in
the same transaction as the `mock_orders` insert, and is idempotent on `idempotency_key`:
a replay returns the stored order and decrements nothing. `amend_order` replaces the
stored order's lines and total after a shortfall (agent-protocol P27).

Search applies the exact filters (allergen codes, diet codes, stock, zone and price) and
then ranks by `ts_rank` over the item's `search` tsvector (§5 "Search").
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from services.common import db
from services.mcp.meta import AdapterFailure

__all__ = [
    "ADAPTER_DOWN",
    "INVALID_REQUEST",
    "OUT_OF_STOCK",
    "SEARCH_LIMIT",
    "MockShop",
    "active_events",
    "search",
]

# AdapterFailure reasons (item 45, amended 2026-11-02).
ADAPTER_DOWN = "adapter_5xx"
OUT_OF_STOCK = "out_of_stock"
INVALID_REQUEST = "adapter_4xx"

SEARCH_LIMIT = 10
_TABLES = frozenset({"catalog_items", "menu_items"})


class _Replay(Exception):
    """Raised inside the order transaction to roll it back when the key already exists."""


def _table(name: str) -> sql.Identifier:
    if name not in _TABLES:
        raise ValueError(f"items table must be one of {sorted(_TABLES)}, got {name!r}")
    return sql.Identifier(name)


async def active_events(conn: AsyncConnection, env: str, agent_id: str) -> list[tuple[str, Any]]:
    """`(kind, params)` of every active `mock_events` row for this card and `env`."""
    cur = await conn.execute(
        "SELECT kind, params FROM mock_events"
        " WHERE env = %s AND target_agent_id = %s AND active ORDER BY id",
        (env, agent_id),
    )
    return [(kind, params) for kind, params in await cur.fetchall()]


def _out_of_stock_skus(events: Sequence[tuple[str, Any]]) -> set[str]:
    return {
        str(params["sku"])
        for kind, params in events
        if kind == "out_of_stock" and isinstance(params, Mapping) and params.get("sku")
    }


def _filter_codes(filters: Mapping[str, Any], name: str) -> list[str]:
    value = filters.get(name) or []
    if isinstance(value, str) or not all(isinstance(v, str) for v in value):
        raise AdapterFailure(INVALID_REQUEST, retryable=False)
    return sorted(set(value))


_SEARCH_SQL = """
SELECT i.sku, i.name, i.brand, i.allergens, i.diets, i.price_minor, i.currency,
       COALESCE(s.qty, 0) AS available_qty
FROM {table} i
LEFT JOIN mock_stock s ON s.env = %(env)s AND s.agent_id = i.agent_id AND s.sku = i.sku
CROSS JOIN plainto_tsquery('simple', %(query)s) q
WHERE i.agent_id = %(agent_id)s
  AND NOT (i.allergens && %(allergens)s::text[])
  AND i.diets @> %(diets)s::text[]
  AND (%(max_price)s::bigint IS NULL OR i.price_minor <= %(max_price)s::bigint)
  AND COALESCE(s.qty, 0) > 0
  AND NOT (i.sku = ANY(%(out_of_stock)s::text[]))
  AND (numnode(q) = 0 OR i.search @@ q)
ORDER BY ts_rank(i.search, q) DESC, i.sku
LIMIT %(limit)s
"""


async def search(
    conn: AsyncConnection,
    *,
    table: str,
    env: str,
    agent_id: str,
    zone_ids: Sequence[str],
    query: str,
    filters: Mapping[str, Any],
    out_of_stock: Sequence[str] = (),
    limit: int = SEARCH_LIMIT,
) -> list[dict[str, Any]]:
    """The card's items that pass every filter, best `ts_rank` match first.

    `filters` may hold `allergen_codes` (items with any of them are excluded),
    `diet_codes` (items must have all of them), `zone_id` (the card must cover it) and
    `max_price_minor`. Only items with available stock are returned. An empty query
    matches every item; otherwise an item must match the query.
    """
    zone_id = filters.get("zone_id")
    if zone_id is not None and zone_id not in zone_ids:
        return []
    max_price = filters.get("max_price_minor")
    if max_price is not None and (isinstance(max_price, bool) or not isinstance(max_price, int)):
        raise AdapterFailure(INVALID_REQUEST, retryable=False)
    params = {
        "env": env,
        "agent_id": agent_id,
        "query": query or "",
        "allergens": _filter_codes(filters, "allergen_codes"),
        "diets": _filter_codes(filters, "diet_codes"),
        "max_price": max_price,
        "out_of_stock": sorted(out_of_stock),
        "limit": limit,
    }
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql.SQL(_SEARCH_SQL).format(table=_table(table)), params)
        rows = await cur.fetchall()
    return [
        {
            "sku": r["sku"],
            "name": r["name"],
            "brand": r["brand"],
            "allergens": list(r["allergens"]),
            "diets": list(r["diets"]),
            "unit_minor": int(r["price_minor"]),
            "currency": str(r["currency"]).strip(),
            "available_qty": int(r["available_qty"]),
        }
        for r in rows
    ]


def _whole_qty(qty: Any) -> int:
    """A positive whole quantity, or `AdapterFailure(adapter_4xx)`."""
    if isinstance(qty, bool | float):
        raise AdapterFailure(INVALID_REQUEST, retryable=False)
    try:
        value = Decimal(str(qty))
    except InvalidOperation:
        raise AdapterFailure(INVALID_REQUEST, retryable=False) from None
    if not value.is_finite() or value != value.to_integral_value() or value < 1:
        raise AdapterFailure(INVALID_REQUEST, retryable=False)
    return int(value)


class MockShop:
    """The mock backend of one store card in one `env`.

    `table` is the card's items table: `catalog_items` (stores) or `menu_items`
    (restaurants). `MockRestaurant` (`services/mocks/restaurants.py`) adds
    `kitchen_overloaded`.
    """

    table = "catalog_items"

    def __init__(self, *, env: str, agent_id: str, zone_ids: Sequence[str] = ()) -> None:
        self.env = env
        self.agent_id = agent_id
        self.zone_ids = tuple(zone_ids)

    async def _events(self, conn: AsyncConnection, tool: str) -> list[tuple[str, Any]]:
        """The active events; raises the failure an event sets for `tool`."""
        events = await active_events(conn, self.env, self.agent_id)
        if any(kind == "adapter_down" for kind, _ in events):
            raise AdapterFailure(ADAPTER_DOWN, retryable=True)
        return events

    async def search_items(self, query: str, filters: Mapping[str, Any]) -> dict[str, Any]:
        async with db.get_pool().connection() as conn:
            events = await self._events(conn, "search_items")
            items = await search(
                conn,
                table=self.table,
                env=self.env,
                agent_id=self.agent_id,
                zone_ids=self.zone_ids,
                query=query,
                filters=filters,
                out_of_stock=sorted(_out_of_stock_skus(events)),
            )
        return {"items": items}

    async def quote(self, items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Price and availability per requested `{sku, qty}`, in request order.

        A line is unavailable when its available quantity is below `qty`; an unknown SKU
        is unavailable and has no name or price.
        """
        wanted = [(str(i["sku"]), _whole_qty(i["qty"])) for i in items]
        async with db.get_pool().connection() as conn:
            events = await self._events(conn, "quote")
            gone = _out_of_stock_skus(events)
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    sql.SQL(
                        "SELECT i.sku, i.name, i.price_minor, i.currency,"
                        " COALESCE(s.qty, 0) AS qty FROM {table} i"
                        " LEFT JOIN mock_stock s ON s.env = %s AND s.agent_id = i.agent_id"
                        " AND s.sku = i.sku"
                        " WHERE i.agent_id = %s AND i.sku = ANY(%s)"
                    ).format(table=_table(self.table)),
                    (self.env, self.agent_id, [sku for sku, _ in wanted]),
                )
                known = {r["sku"]: r for r in await cur.fetchall()}
        lines = []
        currency = None
        for sku, qty in wanted:
            row = known.get(sku)
            if row is None:
                lines.append(
                    {"sku": sku, "name": None, "qty": qty, "unit_minor": None, "available": False}
                )
                continue
            currency = str(row["currency"]).strip()
            available = 0 if sku in gone else int(row["qty"])
            lines.append(
                {
                    "sku": sku,
                    "name": row["name"],
                    "qty": qty,
                    "unit_minor": int(row["price_minor"]),
                    "available": available >= qty,
                }
            )
        return {"currency": currency, "lines": lines}

    async def place_order(
        self,
        *,
        idempotency_key: str,
        order_id: str,
        lines: Sequence[Mapping[str, Any]],
        currency: str,
    ) -> dict[str, Any]:
        """Place the order once per `idempotency_key` (the vendor key).

        `lines` are the authorized order lines (`sku`, `qty`, `unit_minor`). Every line's
        stock is decremented in the transaction that inserts the `mock_orders` row; a line
        that can't be filled raises `AdapterFailure("out_of_stock")` and changes nothing.
        `external_order_id` is the vendor key, as for the mock courier (item 46).
        """
        wanted = [(str(i["sku"]), _whole_qty(i["qty"]), int(i["unit_minor"])) for i in lines]
        if not wanted:
            raise AdapterFailure(INVALID_REQUEST, retryable=False)
        body = {
            "order_id": str(order_id),
            "lines": [{"sku": s, "qty": q, "unit_minor": u} for s, q, u in wanted],
            "total_minor": sum(q * u for _, q, u in wanted),
            "currency": currency,
        }
        async with db.get_pool().connection() as conn:
            events = await self._events(conn, "place_order")
            gone = _out_of_stock_skus(events)
            try:
                async with conn.transaction():
                    # A concurrent call with the same key waits here until the first one
                    # commits or rolls back.
                    cur = await conn.execute(
                        "INSERT INTO mock_orders"
                        " (external_order_id, env, agent_id, idempotency_key, body, state)"
                        " VALUES (%s, %s, %s, %s, %s, 'placed')"
                        " ON CONFLICT DO NOTHING RETURNING external_order_id",
                        (idempotency_key, self.env, self.agent_id, idempotency_key, Jsonb(body)),
                    )
                    if await cur.fetchone() is None:
                        raise _Replay()
                    # Lock rows in SKU order so two orders never deadlock.
                    for sku, qty in sorted(self._per_sku(wanted).items()):
                        if sku in gone:
                            raise AdapterFailure(OUT_OF_STOCK, retryable=False)
                        cur = await conn.execute(
                            "UPDATE mock_stock SET qty = qty - %s"
                            " WHERE env = %s AND agent_id = %s AND sku = %s AND qty >= %s",
                            (qty, self.env, self.agent_id, sku, qty),
                        )
                        if cur.rowcount != 1:
                            raise AdapterFailure(OUT_OF_STOCK, retryable=False)
            except _Replay:
                return await self._stored(conn, idempotency_key)
        return self._result(idempotency_key, "placed", body)

    async def amend_order(
        self,
        *,
        external_order_id: str,
        lines: Sequence[Mapping[str, Any]],
        currency: str,
    ) -> dict[str, Any]:
        """Replace the placed order's lines and total in its `mock_orders` body.

        The row is the one `place_order` stored under its key (`external_order_id`). Stock
        is not changed. An order of another card or `env` raises `adapter_4xx`.
        """
        wanted = [(str(i["sku"]), _whole_qty(i["qty"]), int(i["unit_minor"])) for i in lines]
        async with db.get_pool().connection() as conn:
            await self._events(conn, "amend_order")
            async with conn.transaction():
                cur = await conn.execute(
                    "SELECT body FROM mock_orders WHERE external_order_id = %s"
                    " AND env = %s AND agent_id = %s FOR UPDATE",
                    (external_order_id, self.env, self.agent_id),
                )
                row = await cur.fetchone()
                if row is None:
                    raise AdapterFailure(INVALID_REQUEST, retryable=False)
                body = {
                    **row[0],
                    "lines": [{"sku": s, "qty": q, "unit_minor": u} for s, q, u in wanted],
                    "total_minor": sum(q * u for _, q, u in wanted),
                    "currency": currency,
                }
                cur = await conn.execute(
                    "UPDATE mock_orders SET body = %s WHERE external_order_id = %s RETURNING state",
                    (Jsonb(body), external_order_id),
                )
                (state,) = await cur.fetchone() or (None,)
        return self._result(external_order_id, state, body)

    @staticmethod
    def _per_sku(wanted: Sequence[tuple[str, int, int]]) -> dict[str, int]:
        per_sku: dict[str, int] = {}
        for sku, qty, _ in wanted:
            per_sku[sku] = per_sku.get(sku, 0) + qty
        return per_sku

    async def _stored(self, conn: AsyncConnection, idempotency_key: str) -> dict[str, Any]:
        cur = await conn.execute(
            "SELECT external_order_id, state, body FROM mock_orders"
            " WHERE idempotency_key = %s AND env = %s AND agent_id = %s",
            (idempotency_key, self.env, self.agent_id),
        )
        row = await cur.fetchone()
        if row is None:
            # The key belongs to another card or env: a vendor would refuse it.
            raise AdapterFailure(INVALID_REQUEST, retryable=False)
        external_order_id, state, body = row
        return self._result(external_order_id, state, body)

    @staticmethod
    def _result(external_order_id: str, state: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """The minimal typed result: ids, state and amounts (§5)."""
        return {
            "external_order_id": external_order_id,
            "state": state,
            "total_minor": int(body["total_minor"]),
            "currency": body["currency"],
        }
