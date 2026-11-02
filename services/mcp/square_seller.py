"""`SquareSeller`, the base of the two Square adapters (adapters §7, file 26).

`SquareRetailAdapter` (`services/mcp/grocery/adapters/square.py`) and
`SquareRestaurantAdapter` (`services/mcp/restaurant/adapters/square.py`) differ only in
their items table and their Settings names.

*   `search_items` and `quote` read our `catalog_items` / `menu_items` exactly as the mocks
    do (`services/mocks/stores.search`), with stock from the table's `in_stock` flag, not
    `mock_stock`: Square has no allergen or diet codes and holds none of our items. `quote`
    prices from `price_minor`; build step 0 found no taxes on ad-hoc lines
    (`docs/spikes/vendors.md` run 2), so `calculate_order` is not used.
*   `place_order` sends one `POST /orders` through `square_client`: ad-hoc lines (our name,
    quantity, price and SKU as the note), `reference_id` = `order_id`, and a `PICKUP`
    fulfilment whose display name is the order id. No household data is sent. It refuses
    with `adapter_not_configured` before any call unless `square_env == "sandbox"` and the
    token and location id are set.
*   Square returns the original order (HTTP 200) when a key is reused with a different body
    (run 2). So after every create, the returned reference id, line notes (our SKUs) and
    quantities must equal what was sent, or it raises `vendor_key_reused`. Then the
    currency (`currency_mismatch`) and the total against the authorised lines
    (`total_mismatch`) are checked. All three are not retryable.
*   There is no `amend_order` (adapters §8): `OPERATIONS` is `{"place_order"}`.
"""

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from psycopg import sql
from psycopg.rows import dict_row

from services.common import db
from services.common.config import get_settings
from services.common.directory import AgentCard
from services.mcp import square_client
from services.mcp.meta import AdapterFailure
from services.mocks.stores import INVALID_REQUEST, _whole_qty, search

__all__ = ["CURRENCY_MISMATCH", "SANDBOX", "TOTAL_MISMATCH", "SquareSeller"]

SANDBOX = "sandbox"
CURRENCY_MISMATCH = "currency_mismatch"
TOTAL_MISMATCH = "total_mismatch"
# A 2xx order that can't be read: the client's "2xx without an order" rule.
_UNREADABLE = square_client.SERVER_ERROR


def _fail(reason: str, retryable: bool = False) -> AdapterFailure:
    return AdapterFailure(reason, retryable, vendor=square_client.VENDOR)


class SquareSeller:
    """One Square seller card (`card`) in the call's `env`."""

    OPERATIONS: ClassVar[frozenset[str]] = frozenset({"place_order"})
    table: ClassVar[str]
    token_setting: ClassVar[str]
    location_setting: ClassVar[str]

    def __init__(self, card: AgentCard, env: str) -> None:
        self.card = card
        self.env = env
        self.agent_id = card.agent_id
        self.zone_ids = tuple(card.zone_ids)

    async def search_items(self, query: str, filters: Mapping[str, Any]) -> dict[str, Any]:
        async with db.get_pool().connection() as conn:
            items = await search(
                conn,
                table=self.table,
                env=self.env,
                agent_id=self.agent_id,
                zone_ids=self.zone_ids,
                query=query,
                filters=filters,
                mock_stock=False,
            )
        return {"items": items}

    async def _items(self, skus: Sequence[str]) -> dict[str, dict[str, Any]]:
        async with (
            db.get_pool().connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                sql.SQL(
                    "SELECT sku, name, price_minor, currency, in_stock FROM {table}"
                    " WHERE agent_id = %s AND sku = ANY(%s)"
                ).format(table=sql.Identifier(self.table)),
                (self.agent_id, list(skus)),
            )
            return {r["sku"]: r for r in await cur.fetchall()}

    async def quote(self, items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Price and availability per requested `{sku, qty}`, in request order.

        A line is available when the item is `in_stock`; an unknown SKU is unavailable and
        has no name or price.
        """
        wanted = [(str(i["sku"]), _whole_qty(i["qty"])) for i in items]
        known = await self._items([sku for sku, _ in wanted])
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
            lines.append(
                {
                    "sku": sku,
                    "name": row["name"],
                    "qty": qty,
                    "unit_minor": int(row["price_minor"]),
                    "available": bool(row["in_stock"]),
                }
            )
        return {"currency": currency, "lines": lines}

    def _config(self) -> tuple[str, str]:
        """`(token, location_id)`, or `adapter_not_configured` outside the sandbox."""
        settings = get_settings()
        token = getattr(settings, self.token_setting)
        location_id = getattr(settings, self.location_setting)
        if settings.square_env != SANDBOX or not token or not location_id:
            raise _fail(square_client.NOT_CONFIGURED)
        return token, location_id

    async def place_order(
        self,
        *,
        idempotency_key: str,
        order_id: str,
        lines: Sequence[Mapping[str, Any]],
        currency: str,
    ) -> dict[str, Any]:
        """Create the Square order once per `idempotency_key` (the vendor key).

        `lines` are the authorised order lines (`sku`, `qty`, `unit_minor`). Returns the
        mock's minimal result: Square's order id, `placed`, the total and the currency.
        """
        token, location_id = self._config()
        wanted = [(str(i["sku"]), _whole_qty(i["qty"]), int(i["unit_minor"])) for i in lines]
        if not wanted:
            raise _fail(INVALID_REQUEST)
        if currency != self.card.currency:
            raise _fail(CURRENCY_MISMATCH)
        known = await self._items([sku for sku, _, _ in wanted])
        if any(sku not in known for sku, _, _ in wanted):
            raise _fail(INVALID_REQUEST)
        sent = [
            {"sku": sku, "name": known[sku]["name"], "qty": qty, "unit_minor": unit}
            for sku, qty, unit in wanted
        ]
        order = await square_client.client_for(token).create_order(
            idempotency_key=idempotency_key,
            location_id=location_id,
            reference_id=str(order_id),
            lines=sent,
            currency=currency,
        )
        return self._check(order, str(order_id), wanted, currency)

    @staticmethod
    def _check(
        order: Mapping[str, Any],
        order_id: str,
        wanted: Sequence[tuple[str, int, int]],
        currency: str,
    ) -> dict[str, Any]:
        """Compare the returned order with what was sent; return the minimal result."""
        try:
            external_order_id = str(order["id"])
            returned = [
                (str(line.get("note")), int(line["quantity"])) for line in order["line_items"]
            ]
            total = order["total_money"]
            total_minor, total_currency = int(total["amount"]), str(total["currency"])
        except (KeyError, TypeError, ValueError):
            raise _fail(_UNREADABLE, retryable=True) from None
        if order.get("reference_id") != order_id or returned != [(s, q) for s, q, _ in wanted]:
            raise _fail(square_client.KEY_REUSED)
        if total_currency != currency:
            raise _fail(CURRENCY_MISMATCH)
        if total_minor != sum(q * u for _, q, u in wanted):
            raise _fail(TOTAL_MISMATCH)
        return {
            "external_order_id": external_order_id,
            "state": "placed",
            "total_minor": total_minor,
            "currency": total_currency,
        }
