"""Address resolution for `book_delivery` (architecture item 52, §5 "The address path", G5).

`resolve()` loads the exact delivery address only for the order's chosen courier and only
for the order's own `address_ref`. The address goes to the adapter call and nowhere else.
The one record of the release is a `data_share` trace row with `address_ref`, the
recipient and the purpose, never an address field. It is called only from
`/mcp/delivery book_delivery`; the guard has already checked the OpenFGA relation.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from services.common import db
from services.common.trace import TraceStore
from services.safety.guard import GuardContext, GuardRefused
from services.safety.scope import unscoped

__all__ = ["PURPOSE", "Address", "resolve"]

PURPOSE = "delivery"


@dataclass(frozen=True)
class Address:
    """One `household_addresses` row. Never logged, traced or stored in a tool result."""

    address_ref: uuid.UUID
    line1: str
    line2: str | None
    city: str
    region: str
    postal_code: str
    country: str
    lat: Decimal | None
    lng: Decimal | None


async def resolve(
    conn: AsyncConnection, ctx: GuardContext, address_ref: uuid.UUID, order_id: uuid.UUID
) -> Address:
    """Return the address for `order_id`, for the order's delivery agent only.

    Refuses with `authz_denied` unless `orders.delivery_agent_id = ctx.agent_id` and
    `orders.address_ref = address_ref`. Writes the `data_share` trace row in `conn`.
    """
    async with unscoped(conn, "addresses").cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT o.delivery_agent_id, o.address_ref AS order_address_ref,"
            " a.address_ref, a.line1, a.line2, a.city, a.region, a.postal_code, a.country,"
            " a.lat, a.lng"
            " FROM orders o JOIN household_addresses a"
            "   ON a.address_ref = o.address_ref AND a.household_id = o.household_id"
            " WHERE o.order_id = %s",
            (order_id,),
        )
        row = await cur.fetchone()
    if (
        row is None
        or row["delivery_agent_id"] is None
        or row["delivery_agent_id"] != ctx.agent_id
        or str(row["order_address_ref"]) != str(address_ref)
    ):
        raise GuardRefused("authz_denied")

    await TraceStore(db.get_pool()).write(
        ctx.run_id,
        ctx.agent_id,
        "data_share",
        {"address_ref": str(row["address_ref"]), "recipient": ctx.agent_id, "purpose": PURPOSE},
        on_behalf_of=ctx.on_behalf_of,
        conversation_id=ctx.conversation_id,
        conn=conn,
    )
    return Address(
        address_ref=row["address_ref"],
        line1=row["line1"],
        line2=row["line2"],
        city=row["city"],
        region=row["region"],
        postal_code=row["postal_code"],
        country=row["country"].strip(),
        lat=row["lat"],
        lng=row["lng"],
    )
