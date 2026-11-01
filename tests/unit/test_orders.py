"""Order transitions, items hash, weekly budget and amendments (architecture item 25, P15)."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from services.common import orders
from services.common.money import Money
from services.common.protocol.schemas import ShortfallLine

db = pytest.mark.db


@dataclass
class World:
    dsn: str
    run_id: uuid.UUID
    address_ref: uuid.UUID


@pytest.fixture
def world(pg: str) -> World:
    run_id, address_ref = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO households (household_id, name, region, zone_id, timezone, currency)"
            " VALUES ('hh1', 'H', 'us', 'z1', 'America/New_York', 'USD')"
        )
        conn.execute(
            "INSERT INTO budgets (household_id, period, amount_minor, currency)"
            " VALUES ('hh1', 'weekly', 10000, 'USD')"
        )
        conn.execute(
            "INSERT INTO household_addresses"
            " (address_ref, household_id, line1, city, region, postal_code, country)"
            " VALUES (%s, 'hh1', '1 Main', 'C', 'R', '00000', 'US')",
            (address_ref,),
        )
        conn.execute(
            "INSERT INTO runs (run_id, household_id, kind, trigger, modes)"
            " VALUES (%s, 'hh1', 'weekly', %s, '{}')",
            (run_id, Jsonb({"source": "whatsapp"})),
        )
    return World(dsn=pg, run_id=run_id, address_ref=address_ref)


def _order(
    w: World,
    state: str,
    *,
    total_minor: int | None = None,
    created_at: datetime | None = None,
    lines: Sequence[tuple[str, str, int, int]] = (),
) -> uuid.UUID:
    order_id = uuid.uuid4()
    with psycopg.connect(w.dsn) as conn:
        conn.execute(
            "INSERT INTO orders (order_id, run_id, household_id, kind, state, total_minor,"
            " currency, address_ref, created_at)"
            " VALUES (%s, %s, 'hh1', 'grocery', %s, %s, 'USD', %s, COALESCE(%s, now()))",
            (order_id, w.run_id, state, total_minor, w.address_ref, created_at),
        )
        for line, (sku, name, qty, unit_minor) in enumerate(lines, start=1):
            conn.execute(
                "INSERT INTO order_items (order_id, line, sku, name, qty, unit_minor)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (order_id, line, sku, name, qty, unit_minor),
            )
    return order_id


def _run[T](w: World, fn: Callable[[psycopg.AsyncConnection], Awaitable[T]]) -> T:
    async def go() -> T:
        async with await psycopg.AsyncConnection.connect(w.dsn) as conn:
            return await fn(conn)

    return asyncio.run(go())


def _row(w: World, order_id: uuid.UUID) -> dict[str, Any]:
    with psycopg.connect(w.dsn, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT state, total_minor, items_hash FROM orders WHERE order_id = %s", (order_id,)
        ).fetchone()
        items = conn.execute(
            "SELECT line, sku, name, qty, unit_minor, substituted_for FROM order_items"
            " WHERE order_id = %s ORDER BY line",
            (order_id,),
        ).fetchall()
    assert row is not None
    return {**row, "items": items}


def test_allowed_table_and_items_hash() -> None:
    assert ("authorized", "quoting") in orders.ALLOWED
    assert ("awaiting_approval", "expired") in orders.ALLOWED
    assert ("negotiating", "expired") not in orders.ALLOWED
    assert "amend_order" in orders.CHANGED_BY["failed"]
    a = [{"sku": "B", "qty": 1, "unit_minor": 450}, {"sku": "A", "qty": 2, "unit_minor": 399}]
    b = [
        {"sku": "A", "qty": Decimal("2.000"), "unit_minor": 399},
        {"sku": "B", "qty": 1.0, "unit_minor": 450},
    ]
    assert orders.items_hash(a) == orders.items_hash(b)
    assert orders.items_hash(a) != orders.items_hash(a[:1])
    assert len(orders.items_hash(a)) == 64


@db
def test_conditional_transition(world: World) -> None:
    order_id = _order(world, "draft")

    async def go(conn: psycopg.AsyncConnection) -> tuple[bool, bool]:
        first = await orders.transition(conn, order_id, "draft", "quoting")
        second = await orders.transition(conn, order_id, "draft", "quoting")  # stale expected
        await conn.commit()
        return first, second

    assert _run(world, go) == (True, False)
    assert _row(world, order_id)["state"] == "quoting"

    # A transition outside ALLOWED is refused before any update.
    with pytest.raises(ValueError):
        _run(world, lambda conn: orders.transition(conn, order_id, "quoting", "placed"))

    # The re-quote: authorized -> quoting, with extra fields set in the same update.
    requote = _order(world, "authorized", total_minor=500)

    async def back(conn: psycopg.AsyncConnection) -> bool:
        moved = await orders.transition(conn, requote, ["authorized"], "quoting", total_minor=None)
        await conn.commit()
        return moved

    assert _run(world, back) is True
    assert _row(world, requote)["state"] == "quoting"
    assert _row(world, requote)["total_minor"] is None


@db
def test_cancel_refused_after_placed(world: World) -> None:
    for state in ("draft", "quoting", "negotiating", "awaiting_approval", "authorized"):
        assert (state, "cancelled") in orders.ALLOWED
    for state in ("placed", "courier_booked", "in_delivery", "delivered"):
        order_id = _order(world, state)
        with pytest.raises(ValueError):
            _run(
                world,
                lambda conn, o=order_id, s=state: orders.transition(conn, o, s, "cancelled"),
            )
        assert _row(world, order_id)["state"] == state


@db
def test_courier_status_map_forward_only(world: World) -> None:
    path = orders.MAIN_PATH
    booked = path.index("courier_booked")
    for status, state in orders.COURIER_STATUS_MAP.items():
        assert state is None or state == "failed" or path.index(state) > booked, status
    assert orders.COURIER_STATUS_MAP["cancelled"] == "failed"
    assert orders.COURIER_STATUS_MAP["enroute_to_dropoff"] == "in_delivery"

    # A late `picked_up` after `delivered` does not move the order back.
    order_id = _order(world, "delivered")
    with pytest.raises(ValueError):
        _run(world, lambda conn: orders.transition(conn, order_id, "delivered", "in_delivery"))

    async def late(conn: psycopg.AsyncConnection) -> bool:
        return await orders.transition(
            conn, order_id, "courier_booked", orders.COURIER_STATUS_MAP["picked_up"]
        )

    assert _run(world, late) is False
    assert _row(world, order_id)["state"] == "delivered"


@db
def test_remaining_budget_week_starts_monday_local(world: World) -> None:
    # 2026-11-02 is a Monday; New York is UTC-5 that day.
    now = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)  # Monday 10:00 local
    _order(world, "placed", total_minor=3000, created_at=datetime(2026, 11, 2, 5, 30, tzinfo=UTC))
    _order(world, "delivered", total_minor=1000, created_at=datetime(2026, 11, 2, 6, 0, tzinfo=UTC))
    # Sunday 23:30 local, though already Monday in UTC: last week.
    _order(world, "placed", total_minor=2000, created_at=datetime(2026, 11, 2, 4, 30, tzinfo=UTC))
    # This week, but not placed or later.
    _order(world, "authorized", total_minor=700, created_at=datetime(2026, 11, 2, 7, 0, tzinfo=UTC))
    _order(world, "cancelled", total_minor=900, created_at=datetime(2026, 11, 2, 7, 0, tzinfo=UTC))

    remaining = _run(world, lambda conn: orders.remaining_weekly_budget(conn, "hh1", now=now))
    assert remaining == Money(6000, "USD")


NOTICE = [
    ShortfallLine(
        line=1,
        sku="SKU-1",
        name="Oat milk 1L",
        qty_missing=2,
        line_total_minor=798,
        substitutes=[{"sku": "SKU-9", "name": "Soy milk 1L", "qty": 2, "unit_minor": 350}],
    ),
    ShortfallLine(
        line=2, sku="SKU-2", name="Wheat bread", qty_missing=1, line_total_minor=450, substitutes=[]
    ),
]


@db
def test_amend_order_updates_lines_total_and_hash(world: World) -> None:
    lines = [
        ("SKU-1", "Oat milk 1L", 2, 399),
        ("SKU-2", "Wheat bread", 1, 450),
        ("SKU-3", "Eggs", 1, 300),
    ]
    order_id = _order(world, "courier_booked", total_minor=1548, lines=lines)

    async def go(conn: psycopg.AsyncConnection) -> orders.AmendResult:
        result = await orders.apply_amendment(conn, order_id, {1: "SKU-9", 2: "drop"}, NOTICE)
        await conn.commit()
        return result

    result = _run(world, go)
    row = _row(world, order_id)
    assert row["state"] == "courier_booked" == result.state
    assert [(i["line"], i["sku"], i["substituted_for"]) for i in row["items"]] == [
        (1, "SKU-9", "SKU-1"),
        (3, "SKU-3", None),
    ]
    assert row["items"][0]["name"] == "Soy milk 1L"
    assert row["total_minor"] == 2 * 350 + 300 == result.total_minor
    expected_hash = orders.items_hash(
        [
            {"sku": "SKU-9", "qty": 2, "unit_minor": 350},
            {"sku": "SKU-3", "qty": 1, "unit_minor": 300},
        ]
    )
    assert row["items_hash"] == expected_hash == result.items_hash
    assert (result.substituted, result.dropped) == ([1], [2])


@db
def test_amend_order_rolls_back_with_caller_transaction(world: World) -> None:
    order_id = _order(
        world,
        "courier_booked",
        total_minor=1248,
        lines=[("SKU-1", "Oat milk 1L", 2, 399), ("SKU-2", "Wheat bread", 1, 450)],
    )

    async def go(conn: psycopg.AsyncConnection) -> None:
        await orders.apply_amendment(conn, order_id, {1: "drop", 2: "drop"}, NOTICE)
        await conn.rollback()

    _run(world, go)
    row = _row(world, order_id)
    assert row["state"] == "courier_booked"
    assert len(row["items"]) == 2


@db
def test_all_lines_dropped_fails_order(world: World) -> None:
    lines = [("SKU-1", "Oat milk 1L", 2, 399), ("SKU-2", "Wheat bread", 1, 450)]
    order_id = _order(world, "in_delivery", total_minor=1248, lines=lines)

    async def go(conn: psycopg.AsyncConnection) -> orders.AmendResult:
        result = await orders.apply_amendment(conn, order_id, {1: "drop", 2: "drop"}, NOTICE)
        await conn.commit()
        return result

    result = _run(world, go)
    row = _row(world, order_id)
    assert row["state"] == "failed" == result.state
    assert row["items"] == []
    assert row["total_minor"] == 0
    assert row["items_hash"] == orders.items_hash([])
