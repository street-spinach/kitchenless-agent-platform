"""The sweeper (architecture item 26, §4 timer table amended by A12; agent-protocol §4;
issue #38)."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from services.agents import sweeper
from services.common import consumer, ids, modes, runs
from services.common.config import Settings

pytestmark = pytest.mark.db

HOUSEHOLD_ID = "us-1"
HOUSEHOLD = "household:us-1"
GROCERY = "grocery:mock-us"
WHATSAPP = "channel:whatsapp"
USER = uuid.UUID("22222222-2222-4222-8222-222222222222")
ADDRESS = uuid.UUID("33333333-3333-4333-8333-333333333333")


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    env = {"SERVICE_NAME": "migrate", "DATABASE_URL": "x", "LOG_LEVEL": "info"}
    settings = Settings.from_env(env)
    monkeypatch.setattr(runs, "get_settings", lambda: settings)
    monkeypatch.setattr(modes, "get_settings", lambda: settings)
    return settings


@pytest.fixture
def world(pg: str) -> str:
    with psycopg.connect(pg) as conn:
        for agent_id, cls, owner, host in (
            (HOUSEHOLD, "household", "household:us-1", "agents"),
            (GROCERY, "grocery", "org:mock", "agents"),
            (WHATSAPP, "channel", "system", "api"),
        ):
            conn.execute(
                "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
                " VALUES (%s, %s, %s, 'us', %s)",
                (agent_id, cls, owner, host),
            )
        conn.execute(
            "INSERT INTO households (household_id, name, region, zone_id, timezone, currency)"
            " VALUES (%s, 'Test', 'us', 'us-oh-45202', 'America/New_York', 'USD')",
            (HOUSEHOLD_ID,),
        )
        conn.execute(
            "INSERT INTO household_addresses"
            " (address_ref, household_id, line1, city, region, postal_code, country)"
            " VALUES (%s, %s, '1 Main St', 'Cincinnati', 'OH', '45202', 'US')",
            (ADDRESS, HOUSEHOLD_ID),
        )
    return pg


def _run[T](pg: str, fn: Callable[[psycopg.AsyncConnection], Awaitable[T]]) -> T:
    async def go() -> T:
        async with await psycopg.AsyncConnection.connect(pg) as conn:
            return await fn(conn)

    return asyncio.run(go())


def _sweep(pg: str) -> int:
    return _run(pg, sweeper.sweep_once)


def _query(pg: str, sql: str, *params: Any) -> list[dict[str, Any]]:
    with psycopg.connect(pg) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _exec(pg: str, sql: str, *params: Any) -> None:
    with psycopg.connect(pg) as conn:
        conn.execute(sql, params)


def _start(pg: str) -> uuid.UUID:
    trigger = {"source": "whatsapp", "user_id": str(USER), "inbound_id": "wamid.1"}
    return _run(
        pg, lambda conn: runs.start(conn, household_id=HOUSEHOLD_ID, kind="weekly", trigger=trigger)
    )


def _add_order(pg: str, run_id: uuid.UUID, state: str) -> uuid.UUID:
    order_id = uuid.uuid4()
    _exec(
        pg,
        "INSERT INTO orders (order_id, run_id, household_id, kind, state, currency, address_ref)"
        " VALUES (%s, %s, %s, 'grocery', %s, 'USD', %s)",
        order_id,
        run_id,
        HOUSEHOLD_ID,
        state,
        ADDRESS,
    )
    return order_id


def _add_approval(pg: str, run_id: uuid.UUID, order_id: uuid.UUID) -> uuid.UUID:
    approval_id = uuid.uuid4()
    _exec(
        pg,
        "INSERT INTO approvals (approval_id, run_id, household_id, order_id, amount_minor,"
        " currency) VALUES (%s, %s, %s, %s, 12000, 'USD')",
        approval_id,
        run_id,
        HOUSEHOLD_ID,
        order_id,
    )
    return approval_id


def _add_timer(pg: str, kind: str, run_id: uuid.UUID, ref: dict[str, Any]) -> int:
    with psycopg.connect(pg) as conn:
        cur = conn.execute(
            "INSERT INTO timers (kind, run_id, due_at, ref)"
            " VALUES (%s, %s, now() - interval '1 second', %s) RETURNING timer_id",
            (kind, run_id, Jsonb(ref)),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def _timer(pg: str, timer_id: int) -> dict[str, Any]:
    [row] = _query(pg, "SELECT * FROM timers WHERE timer_id = %s", timer_id)
    return row


def _make_run_timeout_due(pg: str, run_id: uuid.UUID) -> None:
    _exec(
        pg,
        "UPDATE timers SET due_at = now() - interval '1 second'"
        " WHERE run_id = %s AND kind = 'run_timeout'",
        run_id,
    )


def _apply_timers(pg: str, run_id: uuid.UUID, *changes: Any) -> None:
    """Apply timer changes the way a step's final transaction does (architecture §4)."""

    async def go(conn: psycopg.AsyncConnection) -> None:
        async with conn.transaction():
            for change in changes:
                await consumer._apply_timer(conn, change, run_id)

    _run(pg, go)


def _messages(pg: str, run_id: uuid.UUID) -> list[dict[str, Any]]:
    return _query(pg, "SELECT * FROM agent_messages WHERE run_id = %s", run_id)


def _awaiting_approval(pg: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, int]:
    run_id = _start(pg)
    order_id = _add_order(pg, run_id, "awaiting_approval")
    approval_id = _add_approval(pg, run_id, order_id)
    timer_id = _add_timer(
        pg, "approval_expiry", run_id, {"approval_id": str(approval_id), "order_id": str(order_id)}
    )
    return run_id, order_id, approval_id, timer_id


def test_approval_expiry_conditional_update(world: str) -> None:
    run_id, order_id, approval_id, timer_id = _awaiting_approval(world)

    assert _sweep(world) == 1

    [approval] = _query(world, "SELECT * FROM approvals WHERE approval_id = %s", approval_id)
    assert approval["status"] == "expired"
    [order] = _query(world, "SELECT state FROM orders WHERE order_id = %s", order_id)
    assert order["state"] == "expired"
    assert _timer(world, timer_id)["status"] == "done"
    [msg] = _messages(world, run_id)
    assert msg["message_id"] == ids.message_id(ids.timer_event_id(timer_id), 0)
    assert (msg["from_agent"], msg["to_agent"], msg["type"]) == (
        sweeper.SWEEPER,
        HOUSEHOLD,
        "approval_decision",
    )
    assert msg["body"] == {
        "approval_id": str(approval_id),
        "order_id": str(order_id),
        "status": "expired",
    }

    # Fires once: a second sweep finds nothing due.
    assert _sweep(world) == 0
    assert len(_messages(world, run_id)) == 1

    # An approval already decided is not changed and nothing is sent.
    run2, order2, approval2, timer2 = _awaiting_approval(world)
    _exec(world, "UPDATE approvals SET status = 'approved' WHERE approval_id = %s", approval2)
    assert _sweep(world) == 1
    [approval] = _query(world, "SELECT status FROM approvals WHERE approval_id = %s", approval2)
    assert approval["status"] == "approved"
    [order] = _query(world, "SELECT state FROM orders WHERE order_id = %s", order2)
    assert order["state"] == "awaiting_approval"
    assert _timer(world, timer2)["status"] == "done"
    assert _messages(world, run2) == []


def test_approve_vs_expire_race_one_wins(world: str) -> None:
    run_id, order_id, approval_id, timer_id = _awaiting_approval(world)
    approve_sql = (
        "UPDATE approvals SET status = 'approved', decided_at = now()"
        " WHERE approval_id = %s AND status = 'pending'"
    )

    async def race() -> tuple[int, int]:
        async with (
            await psycopg.AsyncConnection.connect(world) as approver,
            await psycopg.AsyncConnection.connect(world) as sweeping,
        ):
            async with approver.transaction():
                cur = await approver.execute(approve_sql, (approval_id,))
                approved = cur.rowcount
                # The sweeper now waits on the approval row lock held by the approver.
                sweep = asyncio.create_task(sweeper.sweep_once(sweeping))
                await asyncio.sleep(0.5)
                assert not sweep.done()
            fired = await sweep
            return approved, fired

    approved, fired = asyncio.run(race())
    assert (approved, fired) == (1, 1)
    [approval] = _query(world, "SELECT status FROM approvals WHERE approval_id = %s", approval_id)
    assert approval["status"] == "approved"
    [order] = _query(world, "SELECT state FROM orders WHERE order_id = %s", order_id)
    assert order["state"] == "awaiting_approval"
    assert _messages(world, run_id) == []
    assert _timer(world, timer_id)["status"] == "done"

    # The other order: expiry first, then a late approve changes nothing.
    run2, order2, approval2, _ = _awaiting_approval(world)
    assert _sweep(world) == 1
    with psycopg.connect(world) as conn:
        assert conn.execute(approve_sql, (approval2,)).rowcount == 0
    [approval] = _query(world, "SELECT status FROM approvals WHERE approval_id = %s", approval2)
    assert approval["status"] == "expired"
    assert [m["type"] for m in _messages(world, run2)] == ["approval_decision"]


def test_run_timeout_cancelled_while_awaiting_approval(world: str) -> None:
    run_id = _start(world)
    _make_run_timeout_due(world, run_id)
    # request_approval: cancel run_timeout and create approval_expiry (architecture §4).
    _apply_timers(world, run_id, consumer.TimerCancel("run_timeout"))

    assert _sweep(world) == 0
    [run] = _query(world, "SELECT status FROM runs WHERE run_id = %s", run_id)
    assert run["status"] == "running"
    assert _messages(world, run_id) == []

    # After the decision a new run_timeout starts; when it is due the run times out.
    _apply_timers(world, run_id, consumer.TimerCreate("run_timeout", timedelta(seconds=-1)))
    assert _sweep(world) == 1
    [run] = _query(world, "SELECT status, reason FROM runs WHERE run_id = %s", run_id)
    assert (run["status"], run["reason"]) == ("timeout", None)
    [prompt] = _messages(world, run_id)
    assert (prompt["to_agent"], prompt["type"]) == (WHATSAPP, "whatsapp_out")
    assert prompt["message_id"] == ids.retry_prompt_id(run_id)
    statuses = _query(
        world, "SELECT status FROM timers WHERE run_id = %s ORDER BY timer_id", run_id
    )
    assert [t["status"] for t in statuses] == ["cancelled", "done"]


def test_run_timeout_cancelled_at_courier_booked(world: str) -> None:
    run_id = _start(world)
    _add_order(world, run_id, "courier_booked")
    _make_run_timeout_due(world, run_id)
    # book_delivery's step cancels run_timeout (architecture §4).
    _apply_timers(world, run_id, consumer.TimerCancel("run_timeout"))

    assert _sweep(world) == 0
    [run] = _query(world, "SELECT status FROM runs WHERE run_id = %s", run_id)
    assert run["status"] == "running"
    assert _messages(world, run_id) == []
    [timer] = _query(world, "SELECT status FROM timers WHERE run_id = %s", run_id)
    assert timer["status"] == "cancelled"


def test_quote_round_sends_closed_to_ref_to(world: str) -> None:
    run_id = _start(world)
    order_id = _add_order(world, run_id, "quoting")
    _exec(world, "UPDATE timers SET status = 'cancelled' WHERE run_id = %s", run_id)
    timer_id = _add_timer(
        world, "quote_round", run_id, {"order_id": str(order_id), "round": "quote", "to": HOUSEHOLD}
    )

    assert _sweep(world) == 1
    [msg] = _messages(world, run_id)
    assert msg["message_id"] == ids.message_id(ids.timer_event_id(timer_id), 0)
    assert (msg["from_agent"], msg["to_agent"], msg["type"]) == (
        sweeper.SWEEPER,
        HOUSEHOLD,
        "quote_round_closed",
    )
    assert msg["body"] == {"order_id": str(order_id), "round": "quote"}
    assert _timer(world, timer_id)["status"] == "done"


def test_unknown_timer_kind_left_pending(world: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _start(world)
    order_id = _add_order(world, run_id, "courier_booked")
    _exec(world, "UPDATE timers SET status = 'cancelled' WHERE run_id = %s", run_id)
    # The `timers.kind` CHECK allows only kinds the sweeper now knows, so a sweeper that
    # does not know `shortfall_expiry` (as before #69) stands in for an unknown kind.
    monkeypatch.setattr(sweeper, "KINDS", sweeper.KINDS - {"shortfall_expiry"})
    unknown = _add_timer(world, "shortfall_expiry", run_id, {"shortfall_id": str(uuid.uuid4())})
    known = _add_timer(
        world,
        "quote_round",
        run_id,
        {"order_id": str(order_id), "round": "delivery", "to": GROCERY},
    )

    assert _sweep(world) == 1
    assert _timer(world, unknown)["status"] == "pending"
    assert _timer(world, known)["status"] == "done"
    [msg] = _messages(world, run_id)
    assert (msg["to_agent"], msg["type"]) == (GROCERY, "quote_round_closed")

    assert _sweep(world) == 0
    assert _timer(world, unknown)["status"] == "pending"


def _add_shortfall(pg: str, run_id: uuid.UUID, order_id: uuid.UUID) -> uuid.UUID:
    shortfall_id = ids.shortfall_id(order_id)
    _exec(pg, "UPDATE orders SET seller_agent_id = %s WHERE order_id = %s", GROCERY, order_id)
    _exec(
        pg,
        "INSERT INTO shortfalls (shortfall_id, run_id, order_id, seller_agent_id, lines, due_at)"
        " VALUES (%s, %s, %s, %s, '[]', now())",
        shortfall_id,
        run_id,
        order_id,
        GROCERY,
    )
    return shortfall_id


def test_shortfall_expiry_closes_pending_once(world: str) -> None:
    run_id = _start(world)
    order_id = _add_order(world, run_id, "courier_booked")
    _exec(world, "UPDATE timers SET status = 'cancelled' WHERE run_id = %s", run_id)
    shortfall_id = _add_shortfall(world, run_id, order_id)
    ref = {"shortfall_id": str(shortfall_id), "order_id": str(order_id)}
    timer_id = _add_timer(world, "shortfall_expiry", run_id, ref)

    assert _sweep(world) == 1

    [row] = _query(world, "SELECT * FROM shortfalls WHERE shortfall_id = %s", shortfall_id)
    assert row["status"] == "closed" and row["closed_at"] is not None
    assert _timer(world, timer_id)["status"] == "done"
    [msg] = _messages(world, run_id)
    assert msg["message_id"] == ids.shortfall_closed_id(shortfall_id)
    assert msg["conversation_id"] == ids.shortfall_conversation_id(shortfall_id)
    assert (msg["from_agent"], msg["to_agent"], msg["type"]) == (
        sweeper.SWEEPER,
        HOUSEHOLD,
        "shortfall_closed",
    )
    assert msg["body"] == {
        "shortfall_id": str(shortfall_id),
        "order_id": str(order_id),
        "reason": "timeout",
    }

    # Once: a second timer for the same shortfall finds it closed and sends nothing.
    again = _add_timer(world, "shortfall_expiry", run_id, ref)
    assert _sweep(world) == 1
    assert _timer(world, again)["status"] == "done"
    assert len(_messages(world, run_id)) == 1

    # A shortfall the last pick (or the pickup) already closed is left alone.
    run2 = _start(world)
    order2 = _add_order(world, run2, "courier_booked")
    _exec(world, "UPDATE timers SET status = 'cancelled' WHERE run_id = %s", run2)
    shortfall2 = _add_shortfall(world, run2, order2)
    _exec(world, "UPDATE shortfalls SET status = 'closed' WHERE shortfall_id = %s", shortfall2)
    timer2 = _add_timer(world, "shortfall_expiry", run2, {"shortfall_id": str(shortfall2)})
    assert _sweep(world) == 1
    assert _timer(world, timer2)["status"] == "done"
    [row] = _query(world, "SELECT status FROM shortfalls WHERE shortfall_id = %s", shortfall2)
    assert row["status"] == "closed"
    assert _messages(world, run2) == []


def test_failing_timer_left_pending_others_fire(world: str) -> None:
    run_id = _start(world)
    order_id = _add_order(world, run_id, "quoting")
    _exec(world, "UPDATE timers SET status = 'cancelled' WHERE run_id = %s", run_id)
    bad = _add_timer(world, "quote_round", run_id, {"order_id": str(order_id)})  # no round, to
    good = _add_timer(
        world, "quote_round", run_id, {"order_id": str(order_id), "round": "quote", "to": HOUSEHOLD}
    )

    assert _sweep(world) == 1
    assert _timer(world, bad)["status"] == "pending"
    assert _timer(world, good)["status"] == "done"
    assert len(_messages(world, run_id)) == 1
