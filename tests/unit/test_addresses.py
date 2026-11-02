"""Address resolution for `book_delivery` (architecture item 52, G5; issue #44)."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from services.safety import addresses
from services.safety.guard import GuardRefused

pytestmark = pytest.mark.db

COURIER = "delivery:mock-us"
OTHER_COURIER = "delivery:other-us"
ADDRESS_FIELDS = ("line1", "line2", "city", "region", "postal_code", "country", "lat", "lng")
LINE1 = "742 Evergreen Terrace"
POSTAL = "97403"


@dataclass(frozen=True)
class Ctx:
    run_id: uuid.UUID
    agent_id: str
    token: str | None = "t"
    on_behalf_of: str | None = "org:mock"
    conversation_id: uuid.UUID | None = None


@dataclass(frozen=True)
class World:
    dsn: str
    run_id: uuid.UUID
    order_id: uuid.UUID
    address_ref: uuid.UUID
    other_address_ref: uuid.UUID


@pytest.fixture
def world(pg: str) -> World:
    run_id, order_id = uuid.uuid4(), uuid.uuid4()
    address_ref, other_address_ref = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO households (household_id, name, region, zone_id, timezone, currency)"
            " VALUES ('hh1', 'H', 'us', 'z1', 'America/New_York', 'USD')"
        )
        for ref, line1 in ((address_ref, LINE1), (other_address_ref, "1 Other St")):
            conn.execute(
                "INSERT INTO household_addresses"
                " (address_ref, household_id, line1, city, region, postal_code, country)"
                " VALUES (%s, 'hh1', %s, 'Springfield', 'OR', %s, 'US')",
                (ref, line1, POSTAL),
            )
        for agent_id in (COURIER, OTHER_COURIER):
            conn.execute(
                "INSERT INTO agent_cards (agent_id, class, owner_ref, region, zone_ids,"
                " currency, host_process) VALUES (%s, 'delivery', 'org:mock', 'us',"
                " '{z1}', 'USD', 'agents')",
                (agent_id,),
            )
        conn.execute(
            "INSERT INTO runs (run_id, household_id, kind, trigger, modes)"
            " VALUES (%s, 'hh1', 'weekly', '{}', '{}')",
            (run_id,),
        )
        conn.execute(
            "INSERT INTO orders (order_id, run_id, household_id, kind, state,"
            " delivery_agent_id, currency, address_ref)"
            " VALUES (%s, %s, 'hh1', 'grocery', 'placed', %s, 'USD', %s)",
            (order_id, run_id, COURIER, address_ref),
        )
    return World(pg, run_id, order_id, address_ref, other_address_ref)


def _run[T](w: World, fn: Callable[[psycopg.AsyncConnection], Awaitable[T]]) -> T:
    async def go() -> T:
        async with AsyncConnectionPool(w.dsn, min_size=1, max_size=2, open=False) as pool:
            original = addresses.db.get_pool
            addresses.db.get_pool = lambda: pool  # type: ignore[method-assign]
            try:
                async with pool.connection() as conn:
                    return await fn(conn)
            finally:
                addresses.db.get_pool = original  # type: ignore[method-assign]

    return asyncio.run(go())


def _traces(w: World) -> list[dict[str, Any]]:
    with psycopg.connect(w.dsn) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT kind, agent_id, payload FROM agent_traces WHERE run_id = %s ORDER BY seq",
            (w.run_id,),
        )
        return cur.fetchall()


def test_resolve_requires_delivery_agent_of_order(world: World) -> None:
    w = world
    address = _run(
        w, lambda conn: addresses.resolve(conn, Ctx(w.run_id, COURIER), w.address_ref, w.order_id)
    )
    assert (address.address_ref, address.line1, address.postal_code) == (
        w.address_ref,
        LINE1,
        POSTAL,
    )

    refused = [
        (OTHER_COURIER, w.address_ref, w.order_id),  # not the order's courier
        (COURIER, w.other_address_ref, w.order_id),  # not the order's address
        (COURIER, w.address_ref, uuid.uuid4()),  # no such order
    ]
    for agent_id, ref, order_id in refused:
        with pytest.raises(GuardRefused) as e:
            _run(
                w,
                lambda conn, a=agent_id, r=ref, o=order_id: addresses.resolve(
                    conn, Ctx(w.run_id, a), r, o
                ),
            )
        assert e.value.reason == "authz_denied"

    # Only the one successful release wrote a data_share row.
    assert [t["kind"] for t in _traces(w)] == ["data_share"]


def test_data_share_row_has_no_address_fields(world: World) -> None:
    w = world
    _run(w, lambda conn: addresses.resolve(conn, Ctx(w.run_id, COURIER), w.address_ref, w.order_id))
    (row,) = _traces(w)
    assert row["kind"] == "data_share"
    assert row["agent_id"] == COURIER
    assert row["payload"] == {
        "address_ref": str(w.address_ref),
        "recipient": COURIER,
        "purpose": addresses.PURPOSE,
    }
    assert not set(ADDRESS_FIELDS) & set(row["payload"])
    text = str(row["payload"])
    for value in (LINE1, POSTAL, "Springfield"):
        assert value not in text
