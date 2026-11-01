"""The consumer: claim, step execution, commit rule and protocol errors (issue #35).

Architecture §4 "Message delivery" and "Step execution", item 20 (amended by A9, A18);
agent-protocol §6, §7 commit rule and P12.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from services.common import consumer as c
from services.common import ids

pytestmark = pytest.mark.db

HOUSEHOLD = "household:us-1"
OTHER_HOUSEHOLD = "household:us-2"
GROCERY = "grocery:mock-us"
SYSTEM = "system:api"


class World:
    """One seeded run: an eval household, an order and an open quote at `GROCERY`."""

    def __init__(self, pg: str, *, env: str = "eval", overrides: dict | None = None) -> None:
        self.pg = pg
        self.run_id = uuid.uuid4()
        self.order_id = uuid.uuid4()
        self.quote_id = uuid.uuid4()
        address = uuid.uuid4()
        trigger: dict[str, Any] = {"source": "test"}
        if overrides is not None:
            trigger["overrides"] = overrides
        with psycopg.connect(pg) as conn:
            for hh in ("us-1", "us-2"):
                conn.execute(
                    "INSERT INTO households (household_id, name, env, region, zone_id, timezone,"
                    " currency) VALUES (%s, %s, %s, 'us', 'us-oh-45202', 'America/New_York',"
                    " 'USD')",
                    (hh, hh, env),
                )
            for agent_id, cls, owner in (
                (HOUSEHOLD, "household", "household:us-1"),
                (OTHER_HOUSEHOLD, "household", "household:us-2"),
                (GROCERY, "grocery", "org:mock"),
            ):
                conn.execute(
                    "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
                    " VALUES (%s, %s, %s, 'us', 'agents')",
                    (agent_id, cls, owner),
                )
            conn.execute(
                "INSERT INTO household_addresses (address_ref, household_id, line1, city,"
                " region, postal_code, country) VALUES (%s, 'us-1', '1 Main', 'Cincinnati',"
                " 'OH', '45202', 'US')",
                (address,),
            )
            conn.execute(
                "INSERT INTO runs (run_id, household_id, kind, trigger, modes)"
                " VALUES (%s, 'us-1', 'weekly', %s, '{}')",
                (self.run_id, Jsonb(trigger)),
            )
            conn.execute(
                "INSERT INTO orders (order_id, run_id, household_id, kind, seller_agent_id,"
                " state, currency, address_ref) VALUES (%s, %s, 'us-1', 'grocery', %s,"
                " 'negotiating', 'USD', %s)",
                (self.order_id, self.run_id, GROCERY, address),
            )
            conn.execute(
                "INSERT INTO quotes (quote_id, run_id, order_id, conversation_id,"
                " seller_agent_id, buyer_agent_id, items, total_minor, currency, expires_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, '[]', 1248, 'USD', now() + interval '10 min')",
                (self.quote_id, self.run_id, self.order_id, uuid.uuid4(), GROCERY, HOUSEHOLD),
            )
        self._n = 0

    def accept_body(self) -> dict[str, Any]:
        return {
            "order_id": str(self.order_id),
            "quote_id": str(self.quote_id),
            "items_hash": "abc",
            "total_minor": 1248,
        }

    def insert(
        self,
        *,
        type: str = "accept",
        from_agent: str = HOUSEHOLD,
        to_agent: str = GROCERY,
        body: dict[str, Any] | None = None,
        status: str = "pending",
        claimed_ago: int | None = None,
    ) -> uuid.UUID:
        """Insert one inbound row; each later insert gets a later `created_at`."""
        self._n += 1
        message_id = uuid.uuid4()
        with psycopg.connect(self.pg) as conn:
            conn.execute(
                "INSERT INTO agent_messages (message_id, run_id, from_agent, to_agent, to_host,"
                " type, body, status, created_at, claimed_at) VALUES (%s, %s, %s, %s, 'agents',"
                " %s, %s, %s, now() - make_interval(secs => %s),"
                " CASE WHEN %s::int IS NULL THEN NULL"
                " ELSE now() - make_interval(secs => %s::int) END)",
                (
                    message_id,
                    self.run_id,
                    from_agent,
                    to_agent,
                    type,
                    Jsonb(self.accept_body() if body is None else body),
                    status,
                    1000 - self._n,
                    claimed_ago,
                    claimed_ago,
                ),
            )
        return message_id

    def message(self, message_id: uuid.UUID) -> dict[str, Any]:
        rows = self.query("SELECT * FROM agent_messages WHERE message_id = %s", (message_id,))
        return rows[0]

    def query(self, q: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with psycopg.connect(self.pg) as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(q, params)
            return cur.fetchall()

    def execute(self, q: str, params: tuple[Any, ...] = ()) -> None:
        with psycopg.connect(self.pg) as conn:
            conn.execute(q, params)

    def make_due(self, message_id: uuid.UUID) -> None:
        self.execute(
            "UPDATE agent_messages SET available_at = now() WHERE message_id = %s", (message_id,)
        )


def ok(_: c.Inbound) -> Awaitable[c.StepResult]:
    async def go() -> c.StepResult:
        return c.StepResult()

    return go()


def boom(_: c.Inbound) -> Awaitable[c.StepResult]:
    async def go() -> c.StepResult:
        raise RuntimeError("handler failed")

    return go()


def with_consumer[T](
    pg: str,
    handle: c.Handler,
    fn: Callable[[c.Consumer], Awaitable[T]],
    **kwargs: Any,
) -> T:
    async def go() -> T:
        pool = AsyncConnectionPool(pg, min_size=1, max_size=5, open=False)
        await pool.open()
        try:
            return await fn(c.Consumer(pool, "agents", handle, **kwargs))
        finally:
            await pool.close()

    return asyncio.run(go())


async def claim_and_run(consumer: c.Consumer) -> list[uuid.UUID]:
    claimed = await consumer.claim()
    await asyncio.gather(*(consumer.start(rec) for rec in claimed))
    return [rec["message_id"] for rec in claimed]


def step_once(pg: str, handle: c.Handler, **kwargs: Any) -> list[uuid.UUID]:
    return with_consumer(pg, handle, claim_and_run, **kwargs)


# Architecture tests (item 69). ---------------------------------------------------------


def test_claim_skips_locked_rows(pg: str) -> None:
    w = World(pg)
    locked = w.insert()
    free = w.insert()

    async def go(consumer: c.Consumer) -> list[uuid.UUID]:
        async with await psycopg.AsyncConnection.connect(pg) as other:
            await other.execute(
                "SELECT 1 FROM agent_messages WHERE message_id = %s FOR UPDATE", (locked,)
            )
            claimed = await consumer.claim()
            await other.rollback()
        return [rec["message_id"] for rec in claimed]

    assert with_consumer(pg, ok, go) == [free]
    assert w.message(locked)["status"] == "pending"
    assert w.message(free)["status"] == "processing"
    assert w.message(free)["attempts"] == 1
    assert w.message(free)["claimed_at"] is not None


def test_same_thread_messages_serialized(pg: str) -> None:
    w = World(pg)
    first, second = w.insert(), w.insert()
    events: list[tuple[str, uuid.UUID]] = []

    async def handle(msg: c.Inbound) -> c.StepResult:
        events.append(("start", msg.row.message_id))
        await asyncio.sleep(0.2)
        events.append(("end", msg.row.message_id))
        return c.StepResult()

    assert step_once(pg, handle) == [first, second]
    assert events == [("start", first), ("end", first), ("start", second), ("end", second)]
    assert w.message(first)["status"] == "processed"
    assert w.message(second)["status"] == "processed"


def test_no_transaction_open_during_handler(pg: str) -> None:
    w = World(pg)
    w.insert()
    seen: list[tuple[int, int]] = []

    def run(consumer: c.Consumer) -> Awaitable[list[uuid.UUID]]:
        async def handle(_: c.Inbound) -> c.StepResult:
            stats = consumer.pool.get_stats()
            in_use = stats["pool_size"] - stats["pool_available"]
            async with await psycopg.AsyncConnection.connect(pg, autocommit=True) as probe:
                cur = await probe.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                    " AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'"
                )
                row = await cur.fetchone()
            assert row is not None
            seen.append((in_use, row[0]))
            return c.StepResult()

        consumer.handle = handle
        return claim_and_run(consumer)

    with_consumer(pg, ok, run)
    assert seen == [(0, 0)]


def test_failed_step_backs_off_then_poison_after_5(pg: str) -> None:
    w = World(pg)
    mid = w.insert()
    finished: list[tuple[uuid.UUID, str, str, str]] = []

    async def finish_run(
        conn: psycopg.AsyncConnection, run_id: uuid.UUID, agent_id: str, status: str, reason: str
    ) -> bool:
        finished.append((run_id, agent_id, status, reason))
        return True

    for attempt in range(1, c.MAX_ATTEMPTS):
        step_once(pg, boom, finish_run=finish_run)
        row = w.message(mid)
        assert row["status"] == "pending"
        assert row["attempts"] == attempt
        assert "handler failed" in row["last_error"]
        [delay] = w.query(
            "SELECT extract(epoch FROM available_at - now()) AS s FROM agent_messages"
            " WHERE message_id = %s",
            (mid,),
        )
        assert 2**attempt - 2 < delay["s"] <= 2**attempt
        assert step_once(pg, boom, finish_run=finish_run) == []  # not due yet
        w.make_due(mid)
        assert finished == []

    step_once(pg, boom, finish_run=finish_run)
    row = w.message(mid)
    assert row["status"] == "failed"
    assert row["attempts"] == c.MAX_ATTEMPTS
    assert finished == [(w.run_id, GROCERY, "error", "poison_message")]


def test_stuck_processing_row_returned_to_pending(pg: str) -> None:
    w = World(pg)
    stuck = w.insert(status="processing", claimed_ago=121)
    fresh = w.insert(status="processing", claimed_ago=30)

    async def go(consumer: c.Consumer) -> int:
        return await consumer.recover()

    assert with_consumer(pg, ok, go) == 1
    assert w.message(stuck)["status"] == "pending"
    assert w.message(fresh)["status"] == "processing"


# Protocol tests (agent-protocol P40). --------------------------------------------------


def _wrong_party(w: World) -> uuid.UUID:
    # household:us-2 is a household but not the quote's buyer: C5 `wrong_party`.
    return w.insert(from_agent=OTHER_HOUSEHOLD)


def test_protocol_error_marks_failed_attempts_unchanged(pg: str) -> None:
    w = World(pg)
    mid = _wrong_party(w)
    called: list[c.Inbound] = []

    async def handle(msg: c.Inbound) -> c.StepResult:
        called.append(msg)
        return c.StepResult()

    step_once(pg, handle)
    row = w.message(mid)
    assert called == []
    assert row["status"] == "failed"
    assert row["attempts"] == 1
    assert row["last_error"] == "wrong_party"
    assert row["available_at"] <= row["claimed_at"]
    [trace] = w.query(
        "SELECT * FROM agent_traces WHERE run_id = %s AND kind = 'error'", (w.run_id,)
    )
    assert trace["agent_id"] == GROCERY
    assert trace["payload"]["where"] == "protocol"
    assert trace["payload"]["reason"] == "wrong_party"
    assert trace["payload"]["attempt"] == 1
    assert trace["payload"]["ref_message_id"] == str(mid)


def test_protocol_error_reply_sent_to_sender(pg: str) -> None:
    w = World(pg)
    mid = _wrong_party(w)
    step_once(pg, ok)
    [reply] = w.query("SELECT * FROM agent_messages WHERE type = 'protocol_error'")
    assert reply["message_id"] == ids.protocol_error_id(mid)
    assert reply["from_agent"] == GROCERY
    assert reply["to_agent"] == OTHER_HOUSEHOLD
    assert reply["causation_id"] == mid
    assert reply["status"] == "pending"
    assert reply["body"]["ref_message_id"] == str(mid)
    assert reply["body"]["code"] == "wrong_party"

    # The reply passes the receiver's own check (§6): no protocol_error loop.
    async def check_reply(consumer: c.Consumer) -> None:
        async with consumer.pool.connection() as conn:
            await c.check(conn, c._to_row(reply))

    with_consumer(pg, ok, check_reply)


def test_no_protocol_error_to_system_sender(pg: str) -> None:
    w = World(pg)
    mid = w.insert(from_agent=SYSTEM)  # `accept` is never from system: wrong_sender_class
    step_once(pg, ok)
    assert w.message(mid)["status"] == "failed"
    assert w.message(mid)["last_error"] == "wrong_sender_class"
    assert w.query("SELECT 1 FROM agent_messages WHERE type = 'protocol_error'") == []
    assert len(w.query("SELECT 1 FROM agent_traces WHERE kind = 'error'")) == 1


def test_no_protocol_error_reply_to_protocol_error(pg: str) -> None:
    w = World(pg)
    mid = w.insert(
        type="protocol_error",
        from_agent=HOUSEHOLD,
        body={"ref_message_id": str(uuid.uuid4()), "code": "wrong_party", "detail": "x"},
    )
    step_once(pg, ok)
    assert w.message(mid)["status"] == "failed"
    assert w.message(mid)["last_error"] == "unknown_reference"
    assert w.query("SELECT 1 FROM agent_messages WHERE message_id <> %s", (mid,)) == []


def test_protocol_error_does_not_end_run(pg: str) -> None:
    w = World(pg)
    _wrong_party(w)
    finished: list[Any] = []

    async def finish_run(*args: Any) -> bool:
        finished.append(args)
        return True

    step_once(pg, ok, finish_run=finish_run)
    assert finished == []
    [run] = w.query("SELECT status, ended_at FROM runs WHERE run_id = %s", (w.run_id,))
    assert run == {"status": "running", "ended_at": None}


def test_other_exceptions_still_retry(pg: str) -> None:
    w = World(pg)
    mid = w.insert()
    step_once(pg, boom)
    row = w.message(mid)
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["available_at"] > row["claimed_at"]
    assert w.query("SELECT 1 FROM agent_messages WHERE type = 'protocol_error'") == []
    assert w.query("SELECT 1 FROM agent_traces WHERE kind = 'error'") == []


def test_outbound_invalid_body_retries_not_protocol_error(pg: str) -> None:
    w = World(pg)
    mid = w.insert()

    async def handle(msg: c.Inbound) -> c.StepResult:
        bad = c.Outgoing(
            message_id=ids.message_id(msg.row.message_id, 0),
            from_agent=GROCERY,
            to_agent=HOUSEHOLD,
            type="order_confirm",
            body={"order_id": "not-a-uuid"},
        )
        return c.StepResult(messages=[bad])

    step_once(pg, handle)
    row = w.message(mid)
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["last_error"].startswith("OutboundInvalid")
    assert w.query("SELECT 1 FROM agent_messages WHERE message_id <> %s", (mid,)) == []


def _confirm(w: World, msg: c.Inbound) -> c.Outgoing:
    return c.Outgoing(
        message_id=ids.message_id(msg.row.message_id, 0),
        from_agent=GROCERY,
        to_agent=HOUSEHOLD,
        type="order_confirm",
        body={
            "order_id": str(w.order_id),
            "state": "placed",
            "lines": [
                {"line": 1, "sku": "SKU-1", "name": "Oat milk 1L", "qty": 2, "unit_minor": 399}
            ],
            "total_minor": 798,
            "currency": "USD",
        },
        causation_id=msg.row.message_id,
    )


def _accept_step(w: World, expected_round: int) -> c.Handler:
    async def handle(msg: c.Inbound) -> c.StepResult:
        return c.StepResult(
            messages=[_confirm(w, msg)],
            timers=[c.TimerCreate("quote_round", timedelta(seconds=10), {"to": GROCERY})],
            quote_updates=[
                c.QuoteUpdate(
                    quote_id=w.quote_id,
                    expected_status=("open", "countered"),
                    expected_round=expected_round,
                    expected_total_minor=1248,
                    require_live=True,
                    set={"status": "accepted"},
                )
            ],
        )

    return handle


def test_quote_update_commits_with_step(pg: str) -> None:
    w = World(pg)
    mid = w.insert()
    step_once(pg, _accept_step(w, expected_round=0))
    assert w.message(mid)["status"] == "processed"
    assert w.message(mid)["processed_at"] is not None
    [quote] = w.query("SELECT status FROM quotes WHERE quote_id = %s", (w.quote_id,))
    assert quote["status"] == "accepted"
    [confirm] = w.query("SELECT * FROM agent_messages WHERE type = 'order_confirm'")
    assert confirm["message_id"] == ids.message_id(mid, 0)
    [timer] = w.query("SELECT * FROM timers WHERE run_id = %s", (w.run_id,))
    assert timer["kind"] == "quote_round"
    assert timer["ref"] == {"to": GROCERY}


def test_failed_conditional_update_rolls_back_step(pg: str) -> None:
    w = World(pg)
    mid = w.insert()
    step_once(pg, _accept_step(w, expected_round=1))  # the stored round is 0
    row = w.message(mid)
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["last_error"].startswith("StaleUpdate")
    [quote] = w.query("SELECT status FROM quotes WHERE quote_id = %s", (w.quote_id,))
    assert quote["status"] == "open"
    assert w.query("SELECT 1 FROM agent_messages WHERE message_id <> %s", (mid,)) == []
    assert w.query("SELECT 1 FROM agent_traces WHERE run_id = %s", (w.run_id,)) == []
    assert w.query("SELECT 1 FROM timers WHERE run_id = %s", (w.run_id,)) == []


# Eval-only hooks (agent-protocol P12). -------------------------------------------------


@pytest.mark.parametrize("hook", [c.FAIL_FIRST_ATTEMPT, c.CRASH_BEFORE_FINISH])
def test_eval_hook_fails_first_attempt_of_first_message_once(pg: str, hook: str) -> None:
    w = World(pg, overrides={hook: "accept"})
    first, second = w.insert(), w.insert()
    calls: list[uuid.UUID] = []

    async def handle(msg: c.Inbound) -> c.StepResult:
        calls.append(msg.row.message_id)
        return c.StepResult()

    step_once(pg, handle)
    assert w.message(first)["status"] == "pending"
    assert w.message(first)["last_error"].startswith("InjectedFailure")
    assert w.message(second)["status"] == "processed"
    expected = [second] if hook == c.FAIL_FIRST_ATTEMPT else [first, second]
    assert calls == expected

    w.make_due(first)
    step_once(pg, handle)
    assert w.message(first)["status"] == "processed"
    assert w.message(first)["attempts"] == 2


@pytest.mark.parametrize("hook", [c.FAIL_FIRST_ATTEMPT, c.CRASH_BEFORE_FINISH])
def test_eval_hooks_ignored_for_live_households(pg: str, hook: str) -> None:
    w = World(pg, env="live", overrides={hook: "accept"})
    mid = w.insert()
    step_once(pg, ok)
    assert w.message(mid)["status"] == "processed"
    assert w.message(mid)["attempts"] == 1


# The loop and shutdown (architecture §4 "Shutdown"). -----------------------------------


def test_stop_stops_claiming_and_drains_in_flight(pg: str) -> None:
    w = World(pg)
    running = w.insert()

    async def go(consumer: c.Consumer) -> None:
        stop, entered = asyncio.Event(), asyncio.Event()

        async def handle(_: c.Inbound) -> c.StepResult:
            entered.set()
            await asyncio.sleep(0.3)
            return c.StepResult()

        consumer.handle = handle
        loop = asyncio.create_task(consumer.run(None, stop))
        await entered.wait()
        w.insert()  # arrives after stop: never claimed
        stop.set()
        await asyncio.wait_for(loop, timeout=5)

    with_consumer(pg, ok, go)
    assert w.message(running)["status"] == "processed"
    assert [r["status"] for r in w.query("SELECT status FROM agent_messages")].count("pending") == 1
