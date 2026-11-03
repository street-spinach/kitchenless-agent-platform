"""The sweeper: fires due timers (architecture item 26 and §4 timer table, amended by A12).

Every 5 seconds `sweep_once` claims due `pending` timers of a known kind with
`FOR UPDATE SKIP LOCKED`. For each one, in the same transaction, it acts by kind, sends the
resulting message through the outbox and marks the timer `done`:

*   `approval_expiry`: expire the approval if still `pending`; if it changed, set the order
    to `expired` and send `approval_decision(expired)` to the household agent.
*   `run_timeout`: `runs.finish(status=timeout)`, which sends the retry prompt.
*   `quote_round`: send `quote_round_closed` to the agent named in `ref.to`.
*   `shortfall_expiry` (agent-protocol §10, P17): close the shortfall if still `pending`;
    if it changed, send `shortfall_closed {reason: timeout}` to the household agent.

Kinds the sweeper does not know are never claimed and stay `pending` (agent-protocol §4).
A timer whose action raises is rolled back to its savepoint, stays `pending` and is tried
again on the next sweep.
"""

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from services.common import db, ids, orders, outbox, runs
from services.safety.scope import unscoped

__all__ = ["CLAIM_LIMIT", "KINDS", "SWEEPER", "SWEEP_SECONDS", "sweep_forever", "sweep_once"]

log = logging.getLogger(__name__)

SWEEPER = "system:sweeper"
SWEEP_SECONDS = 5.0
CLAIM_LIMIT = 8

_CLAIM_SQL = """
SELECT timer_id, kind, run_id, ref FROM timers
WHERE status = 'pending' AND due_at <= now() AND kind = ANY(%s)
ORDER BY due_at, timer_id
FOR UPDATE SKIP LOCKED
LIMIT %s
"""

_EXPIRE_APPROVAL_SQL = """
UPDATE approvals SET status = 'expired', decided_at = now()
WHERE approval_id = %s AND status = 'pending'
RETURNING order_id
"""

# Agent-protocol §10 "Closing early": only a `pending` row closes, so this timer and the
# courier's `picked_up` never both send `shortfall_closed`.
_CLOSE_SHORTFALL_SQL = """
UPDATE shortfalls s SET status = 'closed', closed_at = now()
FROM orders o
WHERE s.shortfall_id = %s AND s.status = 'pending' AND o.order_id = s.order_id
RETURNING s.order_id, s.run_id, o.household_id
"""
# The `shortfall_closed` reason when nobody answered in time (agent-protocol §10).
TIMEOUT = "timeout"

_DONE_SQL = "UPDATE timers SET status = 'done' WHERE timer_id = %s"


async def _approval_expiry(conn: AsyncConnection, timer: Mapping[str, Any]) -> None:
    approval_id = UUID(str(timer["ref"]["approval_id"]))
    cur = await unscoped(conn, "sweeper").execute(_EXPIRE_APPROVAL_SQL, (approval_id,))
    row = await cur.fetchone()
    if row is None:
        return  # already decided: the decision won
    (order_id,) = row
    await orders.transition(conn, order_id, "awaiting_approval", "expired")
    cur = await unscoped(conn, "sweeper").execute(
        "SELECT household_id FROM orders WHERE order_id = %s", (order_id,)
    )
    (household_id,) = await cur.fetchone() or (None,)
    if household_id is None:
        raise LookupError(f"order {order_id} not found")
    await outbox.send(
        conn,
        message_id=ids.message_id(ids.timer_event_id(timer["timer_id"]), 0),
        run_id=timer["run_id"],
        conversation_id=None,
        causation_id=None,
        from_agent=SWEEPER,
        to_agent=f"household:{household_id}",
        type="approval_decision",
        body={"approval_id": str(approval_id), "order_id": str(order_id), "status": "expired"},
    )


async def _run_timeout(conn: AsyncConnection, timer: Mapping[str, Any]) -> None:
    await runs.finish(conn, timer["run_id"], SWEEPER, "timeout")


async def _quote_round(conn: AsyncConnection, timer: Mapping[str, Any]) -> None:
    ref = timer["ref"]
    await outbox.send(
        conn,
        message_id=ids.message_id(ids.timer_event_id(timer["timer_id"]), 0),
        run_id=timer["run_id"],
        conversation_id=None,
        causation_id=None,
        from_agent=SWEEPER,
        to_agent=ref["to"],
        type="quote_round_closed",
        body={"order_id": ref["order_id"], "round": ref["round"]},
    )


async def _shortfall_expiry(conn: AsyncConnection, timer: Mapping[str, Any]) -> None:
    shortfall_id = UUID(str(timer["ref"]["shortfall_id"]))
    cur = await unscoped(conn, "sweeper").execute(_CLOSE_SHORTFALL_SQL, (shortfall_id,))
    row = await cur.fetchone()
    if row is None:
        return  # already closed: the last pick or the pickup won
    order_id, run_id, household_id = row
    await outbox.send(
        conn,
        message_id=ids.shortfall_closed_id(shortfall_id),
        run_id=run_id,
        conversation_id=ids.shortfall_conversation_id(shortfall_id),
        causation_id=None,
        from_agent=SWEEPER,
        to_agent=f"household:{household_id}",
        type="shortfall_closed",
        body={"shortfall_id": str(shortfall_id), "order_id": str(order_id), "reason": TIMEOUT},
    )


_ACTIONS: Mapping[str, Callable[[AsyncConnection, Mapping[str, Any]], Awaitable[None]]] = {
    "approval_expiry": _approval_expiry,
    "run_timeout": _run_timeout,
    "quote_round": _quote_round,
    "shortfall_expiry": _shortfall_expiry,
}
KINDS = frozenset(_ACTIONS)


async def sweep_once(conn: AsyncConnection, limit: int = CLAIM_LIMIT) -> int:
    """Fire up to `limit` due timers in one transaction; return how many were marked `done`."""
    fired = 0
    async with conn.transaction():
        async with unscoped(conn, "sweeper").cursor(row_factory=dict_row) as cur:
            await cur.execute(_CLAIM_SQL, (sorted(KINDS), limit))
            timers = await cur.fetchall()
        for timer in timers:
            try:
                async with conn.transaction():
                    await _ACTIONS[timer["kind"]](conn, timer)
                    await unscoped(conn, "sweeper").execute(_DONE_SQL, (timer["timer_id"],))
            except Exception:
                log.exception(
                    "timer %s (%s) failed; left pending", timer["timer_id"], timer["kind"]
                )
                continue
            fired += 1
    return fired


async def sweep_forever(
    *,
    pool: AsyncConnectionPool | None = None,
    stop: asyncio.Event | None = None,
    every: float = SWEEP_SECONDS,
) -> None:
    """Run `sweep_once` every `every` seconds until SIGTERM (or `stop`)."""
    pool = pool or db.get_pool()
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
        loop.add_signal_handler(signal.SIGTERM, stop.set)
    while not stop.is_set():
        try:
            async with pool.connection() as conn:
                while await sweep_once(conn) == CLAIM_LIMIT and not stop.is_set():
                    pass
        except Exception:
            log.exception("sweep failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every)
