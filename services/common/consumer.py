"""The message consumer: claim, step execution and the commit rule.

Architecture §4 "Message delivery" and "Step execution", item 20 (amended by agent-protocol
A9, A18); agent-protocol §6, §7 commit rule and P12.

Each claimed `agent_messages` row runs as one **step**:

1.  Take the in-process `asyncio.Lock` for the step's lock key (`thread_id` for graphs;
    `quote_id`, else `order_id`, for handlers), so one key's messages run one at a time in
    `created_at` order.
2.  In one short transaction, run `protocol.check` (C1-C5). A `ProtocolError` fails the row
    at once (no retry, no poison rule), writes an `error` trace and sends `protocol_error`
    back to the sender (never to `system:*` or `channel:*`, never for a `protocol_error`).
3.  Run the handler with no transaction or pooled connection held.
4.  One short final transaction applies the `StepResult`: the conditional `quotes` and
    `shortfalls` updates, the outgoing messages, the timer changes and the `processed` mark.
    A conditional update that changes no row rolls the whole step back, and it is retried.

The whole step is capped at 75 seconds. Any other failure returns the row to `pending` with
`available_at = now() + 2^attempts` seconds; after 5 attempts the row is `failed` and the
run is finished with `status=error, reason=poison_message`.
"""

import asyncio
import contextlib
import logging
import signal
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from services.common import db, directory, ids, outbox
from services.common.protocol import OutboundInvalid, ProtocolError, check
from services.common.protocol.a2a import AgentMessageRow
from services.common.trace import TraceStore

__all__ = [
    "NOW",
    "Consumer",
    "Inbound",
    "InjectedFailure",
    "Outgoing",
    "QuoteUpdate",
    "ShortfallUpdate",
    "StaleUpdate",
    "StepResult",
    "TimerCancel",
    "TimerCreate",
    "consume_forever",
    "lock_key",
]

log = logging.getLogger(__name__)

CLAIM_LIMIT = 8
POLL_SECONDS = 2.0
STEP_TIMEOUT_SECONDS = 75.0
MAX_ATTEMPTS = 5
RECOVER_EVERY_SECONDS = 60.0
STUCK_AFTER_SECONDS = 120
DRAIN_SECONDS = 25.0
POISON_REASON = "poison_message"

# Agent classes run as LangGraph graphs; their lock key is the thread id (§4).
GRAPH_CLASSES = frozenset({"consumer", "household"})
# Senders that never receive a `protocol_error` (agent-protocol §6 step 3).
NO_REPLY_CLASSES = frozenset({"system", "channel"})

# Eval-only hooks read from `runs.trigger.overrides` (agent-protocol P12).
FAIL_FIRST_ATTEMPT = "FAIL_FIRST_ATTEMPT"
CRASH_BEFORE_FINISH = "CRASH_BEFORE_FINISH"


class _Now:
    """Sentinel for `now()` in a conditional update's `set`."""

    def __repr__(self) -> str:
        return "NOW"


NOW: Any = _Now()

_CLAIM_SQL = """
UPDATE agent_messages SET status='processing', attempts=attempts+1, claimed_at=now()
WHERE message_id IN (SELECT message_id FROM agent_messages
                     WHERE to_host=%(host)s AND status='pending' AND available_at <= now()
                     ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT %(limit)s)
RETURNING *
"""

_RECOVER_SQL = """
UPDATE agent_messages SET status='pending'
WHERE to_host=%s AND status='processing' AND claimed_at < now() - make_interval(secs => %s)
"""

_PROCESSED_SQL = """
UPDATE agent_messages SET status='processed', processed_at=now()
WHERE message_id=%s AND status='processing'
"""

_RETRY_SQL = """
UPDATE agent_messages
SET status='pending', available_at=now() + make_interval(secs => %s), last_error=%s
WHERE message_id=%s AND status='processing'
"""

_FAILED_SQL = """
UPDATE agent_messages SET status='failed', last_error=%s
WHERE message_id=%s AND status='processing'
"""

_HOOKS_SQL = """
SELECT r.trigger->'overrides' AS overrides, h.env
FROM runs r LEFT JOIN households h ON h.household_id = r.household_id
WHERE r.run_id = %s
"""

_FIRST_OF_TYPE_SQL = """
SELECT message_id FROM agent_messages WHERE run_id=%s AND type=%s
ORDER BY created_at, message_id LIMIT 1
"""

# `TraceStore.write(..., conn=...)` never touches its pool (as in outbox.py).
_TRACES = TraceStore(None)  # type: ignore[arg-type]


class StaleUpdate(Exception):
    """A conditional `quotes` or `shortfalls` update changed no row (agent-protocol §7)."""


class InjectedFailure(RuntimeError):
    """Raised by the eval-only `FAIL_FIRST_ATTEMPT` and `CRASH_BEFORE_FINISH` hooks."""


@dataclass(frozen=True)
class Inbound:
    """A claimed message as the handler sees it: the row, its attempt and the parsed body."""

    row: AgentMessageRow
    attempts: int
    body: BaseModel


@dataclass(frozen=True)
class Outgoing:
    """One message the step sends through `outbox.send` in its final transaction.

    `run_id` defaults to the inbound message's run.
    """

    message_id: UUID
    from_agent: str
    to_agent: str
    type: str
    body: dict[str, Any]
    conversation_id: UUID | None = None
    causation_id: UUID | None = None
    run_id: UUID | None = None
    trace_extra: dict[str, Any] | None = None


@dataclass(frozen=True)
class QuoteUpdate:
    """`UPDATE quotes SET <set> WHERE quote_id AND status IN <expected> AND round = <round>`.

    `expected_total_minor` adds `total_minor = <total>`; `require_live` adds
    `expires_at > now()` (the `accept` update in agent-protocol §7).
    """

    quote_id: UUID
    expected_status: tuple[str, ...]
    expected_round: int
    set: Mapping[str, Any]
    expected_total_minor: int | None = None
    require_live: bool = False


@dataclass(frozen=True)
class ShortfallUpdate:
    """`UPDATE shortfalls SET <set> WHERE shortfall_id AND status IN <expected>`."""

    shortfall_id: UUID
    expected_status: tuple[str, ...]
    set: Mapping[str, Any]


@dataclass(frozen=True)
class TimerCreate:
    """A new `timers` row. A `timedelta` `due_at` is relative to the database's `now()`."""

    kind: str
    due_at: datetime | timedelta
    ref: dict[str, Any] = field(default_factory=dict)
    run_id: UUID | None = None


@dataclass(frozen=True)
class TimerCancel:
    """Cancel this run's `pending` timers of `kind` whose `ref` contains `ref`."""

    kind: str
    ref: dict[str, Any] = field(default_factory=dict)
    run_id: UUID | None = None


@dataclass(frozen=True)
class StepResult:
    """What a step commits in its one final transaction (architecture §4, A18)."""

    messages: Sequence[Outgoing] = ()
    timers: Sequence[TimerCreate | TimerCancel] = ()
    quote_updates: Sequence[QuoteUpdate] = ()
    shortfall_updates: Sequence[ShortfallUpdate] = ()


Handler = Callable[[Inbound], Awaitable[StepResult]]


class FinishRun(Protocol):
    """The shape of `runs.finish` (architecture item 24), called by the poison rule."""

    async def __call__(
        self, conn: AsyncConnection, run_id: UUID, agent_id: str, status: str, reason: str
    ) -> bool: ...


def _class_of(agent_id: str) -> str | None:
    try:
        return ids.parse_agent_id(agent_id)[0]
    except ValueError:
        return None


def lock_key(row: AgentMessageRow) -> str:
    """The step's lock key (architecture §4 step execution 1, agent-protocol §7 ordering).

    Graphs lock on `thread_id`; handlers on the body's `quote_id`, else its `order_id`.
    A handler message naming neither falls back to the receiver's thread id.
    """
    if _class_of(row.to_agent) not in GRAPH_CLASSES and isinstance(row.body, dict):
        for name in ("quote_id", "order_id"):
            value = row.body.get(name)
            if value:
                return f"{name}:{value}"
    return ids.thread_id(row.to_agent, row.run_id)


def _to_row(rec: Mapping[str, Any]) -> AgentMessageRow:
    return AgentMessageRow(
        message_id=rec["message_id"],
        run_id=rec["run_id"],
        conversation_id=rec["conversation_id"],
        causation_id=rec["causation_id"],
        from_agent=rec["from_agent"],
        to_agent=rec["to_agent"],
        type=rec["type"],
        body=rec["body"],
    )


def _set_clause(values: Mapping[str, Any]) -> tuple[sql.Composable, list[Any]]:
    parts: list[sql.Composable] = []
    params: list[Any] = []
    for column, value in values.items():
        if value is NOW:
            parts.append(sql.SQL("{} = now()").format(sql.Identifier(column)))
            continue
        if isinstance(value, dict | list):
            value = Jsonb(value)
        parts.append(sql.SQL("{} = %s").format(sql.Identifier(column)))
        params.append(value)
    if not parts:
        raise ValueError("a conditional update must set at least one column")
    return sql.SQL(", ").join(parts), params


async def _apply_quote(conn: AsyncConnection, u: QuoteUpdate) -> None:
    set_sql, params = _set_clause(u.set)
    where = sql.SQL("quote_id = %s AND status = ANY(%s) AND round = %s")
    params += [u.quote_id, list(u.expected_status), u.expected_round]
    if u.require_live:
        where += sql.SQL(" AND expires_at > now()")
    if u.expected_total_minor is not None:
        where += sql.SQL(" AND total_minor = %s")
        params.append(u.expected_total_minor)
    cur = await conn.execute(
        sql.SQL("UPDATE quotes SET {} WHERE {}").format(set_sql, where), params
    )
    if cur.rowcount != 1:
        raise StaleUpdate(f"quote {u.quote_id} is not in {u.expected_status}/{u.expected_round}")


async def _apply_shortfall(conn: AsyncConnection, u: ShortfallUpdate) -> None:
    set_sql, params = _set_clause(u.set)
    params += [u.shortfall_id, list(u.expected_status)]
    cur = await conn.execute(
        sql.SQL("UPDATE shortfalls SET {} WHERE shortfall_id = %s AND status = ANY(%s)").format(
            set_sql
        ),
        params,
    )
    if cur.rowcount != 1:
        raise StaleUpdate(f"shortfall {u.shortfall_id} is not in {u.expected_status}")


async def _apply_timer(conn: AsyncConnection, t: TimerCreate | TimerCancel, run_id: UUID) -> None:
    if isinstance(t, TimerCreate):
        due = sql.SQL("now() + %s") if isinstance(t.due_at, timedelta) else sql.SQL("%s")
        await conn.execute(
            sql.SQL(
                "INSERT INTO timers (kind, run_id, due_at, ref) VALUES (%s, %s, {}, %s)"
            ).format(due),
            (t.kind, t.run_id or run_id, t.due_at, Jsonb(t.ref)),
        )
    else:
        await conn.execute(
            "UPDATE timers SET status='cancelled'"
            " WHERE run_id=%s AND kind=%s AND status='pending' AND ref @> %s",
            (t.run_id or run_id, t.kind, Jsonb(t.ref)),
        )


async def _on_behalf_of(conn: AsyncConnection, agent_id: str) -> str | None:
    """The receiver's `owner_ref`, `None` for `system` (architecture §3)."""
    if _class_of(agent_id) in (None, "system"):
        return None
    try:
        owner = (await directory.card(conn, agent_id)).owner_ref
    except directory.UnknownAgent:
        return None
    return None if owner == "system" else owner


@dataclass
class _Hooks:
    fail_first_attempt: bool = False
    crash_before_finish: bool = False


class Consumer:
    """Claims and runs this host's messages. `consume_forever` drives it."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        host: str,
        handle: Handler,
        *,
        workers: int = 4,
        finish_run: FinishRun | None = None,
    ) -> None:
        self.pool = pool
        self.host = host
        self.handle = handle
        self.workers = workers
        self.finish_run = finish_run
        self._slots = asyncio.Semaphore(workers)
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_users: dict[str, int] = {}
        self.in_flight: set[asyncio.Task[None]] = set()

    # Claim and recovery -------------------------------------------------------------------

    async def claim(self, limit: int = CLAIM_LIMIT) -> list[dict[str, Any]]:
        """Claim up to `limit` (at most 8) due rows of this host, oldest first."""
        limit = max(0, min(limit, CLAIM_LIMIT))
        if limit == 0:
            return []
        async with self.pool.connection() as conn, conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(_CLAIM_SQL, {"host": self.host, "limit": limit})
                rows = await cur.fetchall()
        return sorted(rows, key=lambda r: (r["created_at"], str(r["message_id"])))

    async def recover(self) -> int:
        """Return rows stuck in `processing` for more than 120 s to `pending`."""
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(_RECOVER_SQL, (self.host, STUCK_AFTER_SECONDS))
            return cur.rowcount

    def start(self, rec: dict[str, Any]) -> asyncio.Task[None]:
        """Start one claimed row as a step. Tasks started in order take their lock in order."""
        task = asyncio.create_task(self.run_step(rec))
        self.in_flight.add(task)
        task.add_done_callback(self.in_flight.discard)
        return task

    # One step ----------------------------------------------------------------------------

    async def run_step(self, rec: dict[str, Any]) -> None:
        """Run one claimed row: lock, check, handle, commit; or refuse, retry or poison."""
        row = _to_row(rec)
        attempts: int = rec["attempts"]
        async with self._lock(lock_key(row)), self._slots:
            try:
                async with asyncio.timeout(STEP_TIMEOUT_SECONDS):
                    await self._step(row, attempts)
            except asyncio.CancelledError:
                raise  # shutdown: the stuck-row rule replays it
            except Exception as exc:
                await self._fail(row, attempts, exc)

    async def _step(self, row: AgentMessageRow, attempts: int) -> None:
        async with self.pool.connection() as conn:
            try:
                async with conn.transaction():
                    body = await check(conn, row)
                    hooks = await self._hooks(conn, row, attempts)
            except ProtocolError as exc:
                await self._refuse(conn, row, attempts, exc)
                return

        if hooks.fail_first_attempt:
            raise InjectedFailure(f"{FAIL_FIRST_ATTEMPT}: {row.type}")
        # No transaction or pooled connection is held while the handler runs (§4 step 2).
        result = await self.handle(Inbound(row=row, attempts=attempts, body=body))
        if hooks.crash_before_finish:
            raise InjectedFailure(f"{CRASH_BEFORE_FINISH}: {row.type}")

        async with self.pool.connection() as conn, conn.transaction():
            await self._commit(conn, row, result)

    async def _commit(self, conn: AsyncConnection, row: AgentMessageRow, r: StepResult) -> None:
        """The step's final transaction (architecture §4 step 3, agent-protocol §7)."""
        for q in r.quote_updates:
            await _apply_quote(conn, q)
        for s in r.shortfall_updates:
            await _apply_shortfall(conn, s)
        for m in r.messages:
            await outbox.send(
                conn,
                message_id=m.message_id,
                run_id=m.run_id or row.run_id,
                conversation_id=m.conversation_id,
                causation_id=m.causation_id,
                from_agent=m.from_agent,
                to_agent=m.to_agent,
                type=m.type,
                body=m.body,
                trace_extra=m.trace_extra,
            )
        for t in r.timers:
            await _apply_timer(conn, t, row.run_id)
        cur = await conn.execute(_PROCESSED_SQL, (row.message_id,))
        if cur.rowcount != 1:
            raise StaleUpdate(f"message {row.message_id} is no longer processing")

    async def _hooks(self, conn: AsyncConnection, row: AgentMessageRow, attempts: int) -> _Hooks:
        """The eval-only hooks for this message, ignored unless the household is `eval`."""
        hooks = _Hooks()
        if attempts != 1:
            return hooks
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_HOOKS_SQL, (row.run_id,))
            run = await cur.fetchone()
        overrides = (run or {}).get("overrides")
        if not run or run["env"] != "eval" or not isinstance(overrides, dict):
            return hooks
        fail = overrides.get(FAIL_FIRST_ATTEMPT) == row.type
        crash = overrides.get(CRASH_BEFORE_FINISH) == row.type
        if not (fail or crash):
            return hooks
        cur = await conn.execute(_FIRST_OF_TYPE_SQL, (row.run_id, row.type))
        first = await cur.fetchone()
        if first is None or first[0] != row.message_id:
            return hooks
        return _Hooks(fail_first_attempt=fail, crash_before_finish=crash)

    async def _refuse(
        self, conn: AsyncConnection, row: AgentMessageRow, attempts: int, exc: ProtocolError
    ) -> None:
        """Agent-protocol §6: fail at once, trace, and reply `protocol_error` when allowed."""
        async with conn.transaction():
            cur = await conn.execute(_FAILED_SQL, (exc.code, row.message_id))
            if cur.rowcount != 1:
                return
            await _TRACES.write(
                row.run_id,
                row.to_agent,
                "error",
                {
                    "where": "protocol",
                    "reason": exc.code,
                    "attempt": attempts,
                    "ref_message_id": str(row.message_id),
                    "detail": exc.detail,
                },
                on_behalf_of=await _on_behalf_of(conn, row.to_agent),
                conversation_id=row.conversation_id,
                conn=conn,
            )
            if _class_of(row.from_agent) in NO_REPLY_CLASSES | {None}:
                return
            if row.type == "protocol_error":
                return
            try:
                await outbox.send(
                    conn,
                    message_id=ids.protocol_error_id(row.message_id),
                    run_id=row.run_id,
                    conversation_id=row.conversation_id,
                    causation_id=row.message_id,
                    from_agent=row.to_agent,
                    to_agent=row.from_agent,
                    type="protocol_error",
                    body={
                        "ref_message_id": str(row.message_id),
                        "code": exc.code,
                        "detail": exc.detail,
                    },
                )
            except (OutboundInvalid, directory.UnknownAgent) as err:
                # The pair cannot carry a protocol_error (for example an unknown sender);
                # the row is still failed and traced.
                log.warning("protocol_error reply not sent for %s: %s", row.message_id, err)

    async def _fail(self, row: AgentMessageRow, attempts: int, exc: BaseException) -> None:
        """Back off and retry, or after the last attempt mark `failed` and finish the run."""
        error = f"{type(exc).__name__}: {exc}"[:500]
        log.warning("step failed for %s (attempt %s): %s", row.message_id, attempts, error)
        async with self.pool.connection() as conn:
            if attempts < MAX_ATTEMPTS:
                async with conn.transaction():
                    await conn.execute(_RETRY_SQL, (2**attempts, error, row.message_id))
                return
            async with conn.transaction():
                cur = await conn.execute(_FAILED_SQL, (error, row.message_id))
                if cur.rowcount != 1:
                    return
            if self.finish_run is None:
                log.error("poison message %s: no finish_run configured", row.message_id)
                return
            try:
                async with conn.transaction():
                    await self.finish_run(conn, row.run_id, row.to_agent, "error", POISON_REASON)
            except Exception:
                # The row stays failed; the run's run_timeout timer still ends the run.
                log.exception("finish_run failed for poison message %s", row.message_id)

    @contextlib.asynccontextmanager
    async def _lock(self, key: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._lock_users[key] = self._lock_users.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._lock_users[key] -= 1
            if self._lock_users[key] == 0:
                del self._lock_users[key]
                del self._locks[key]

    # The loop ----------------------------------------------------------------------------

    async def run(self, listen: AsyncConnection | None, stop: asyncio.Event) -> None:
        """Claim while not stopped; on stop, wait up to 25 s for steps in flight."""
        wake = asyncio.Event()
        reader = asyncio.create_task(self._read_notifies(listen, wake)) if listen else None
        loop = asyncio.get_running_loop()
        next_recover = 0.0
        try:
            while not stop.is_set():
                if loop.time() >= next_recover:
                    await self.recover()
                    next_recover = loop.time() + RECOVER_EVERY_SECONDS
                for rec in await self.claim(self.workers - len(self.in_flight)):
                    self.start(rec).add_done_callback(lambda _t: wake.set())
                wake.clear()
                waits = [asyncio.create_task(wake.wait()), asyncio.create_task(stop.wait())]
                await asyncio.wait(waits, timeout=POLL_SECONDS, return_when="FIRST_COMPLETED")
                for w in waits:
                    w.cancel()
        finally:
            if reader:
                reader.cancel()
            await self.drain()

    async def drain(self, timeout: float = DRAIN_SECONDS) -> None:
        """Wait for steps in flight; cancel what is left (replayed by the stuck-row rule)."""
        if not self.in_flight:
            return
        _, pending = await asyncio.wait(set(self.in_flight), timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending)

    @staticmethod
    async def _read_notifies(listen: AsyncConnection, wake: asyncio.Event) -> None:
        async for _ in listen.notifies():
            wake.set()


async def consume_forever(
    host: str,
    handle: Handler,
    *,
    workers: int = 4,
    finish_run: FinishRun | None = None,
    pool: AsyncConnectionPool | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    """Consume `host`'s messages until SIGTERM (or `stop`), then drain for up to 25 s.

    `finish_run` is `runs.finish`, which the poison rule calls; the agent host passes it.
    """
    pool = pool or db.get_pool()
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
        loop.add_signal_handler(signal.SIGTERM, stop.set)
    listen = await db.listen_connection(f"a2a_{host}")
    try:
        consumer = Consumer(pool, host, handle, workers=workers, finish_run=finish_run)
        await consumer.run(listen, stop)
    finally:
        await listen.close()
