"""Runs: one start and exactly one outcome (architecture item 24 and §10, amended by A13 and
for #36; agent-protocol P14).

`start` inserts the `runs` row with its modes pinned (all `mock` for an `eval` household,
adapters §3), writes `run_start` and creates the `run_timeout` timer. `finish` is the only
writer of `outcome`: the first call that moves the run out of `running`/`awaiting_approval`
wins, and every later call changes nothing. When that call ends the run with `error` or
`timeout`, it also sends the retry prompt from `system:runs`, in the same transaction,
unless the run is `a2a_demo` or the reason is in `NO_RETRY_PROMPT` (those callers send
their own message).

`pause_timeout` and `resume_timeout` stop and restart `run_timeout` around an approval
(architecture §4 timer table; #63).
"""

import json
import uuid
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from services.common import ids, outbox
from services.common.config import get_settings
from services.common.modes import pin_for_env, resolve_modes
from services.common.outbox import _on_behalf_of
from services.common.trace import TraceStore

__all__ = [
    "END_STATUSES",
    "NO_RETRY_PROMPT",
    "RETRY_PROMPT_TEMPLATE",
    "RUNS_AGENT",
    "finish",
    "is_ended",
    "pause_timeout",
    "resume_timeout",
    "retry_target",
    "start",
]

# The sender of `run_start` and of the retry prompt (architecture §3).
RUNS_AGENT = "system:runs"
WHATSAPP = "channel:whatsapp"
RETRY_PROMPT_TEMPLATE = "retry_prompt"

# Reasons whose callers send their own message instead (architecture §9, agent-protocol §12).
NO_RETRY_PROMPT = frozenset({"delivery_failed", "nothing_to_deliver", "no_offers"})

END_STATUSES = frozenset({"completed", "cancelled", "error", "timeout"})
_RETRY_STATUSES = frozenset({"error", "timeout"})
_OPEN_STATUSES = ("running", "awaiting_approval")
# `place_order` sets `placed` and `delivery_agent_id` together (architecture §8), so a set
# `delivery_agent_id` means the order reached `placed` even if it later `failed`.
_PLACED_OR_LATER = ("placed", "courier_booked", "in_delivery", "delivered")
A2A_DEMO = "a2a_demo"
# Runs a person never retries (experience EX-A16): code-only replies, JOIN, `ob:` taps,
# `/me` POSTs and reminders. They are skipped, so they never hide a failed run (EX-A15).
_NOT_RETRY_SOURCES = ("code", "join", "tap", "me", "reminder")
RETRY_WINDOW = "24 hours"

# `TraceStore.write(..., conn=...)` never touches the pool.
_TRACES = TraceStore(None)  # type: ignore[arg-type]

_INSERT_RUN_SQL = """
INSERT INTO runs (run_id, household_id, kind, trigger, modes)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (run_id) DO NOTHING
RETURNING run_id
"""

_INSERT_TIMER_SQL = """
INSERT INTO timers (kind, run_id, due_at)
VALUES ('run_timeout', %s, now() + make_interval(secs => %s::float8))
"""

_FINISH_SQL = """
UPDATE runs SET status = %s, reason = %s, ended_at = now()
WHERE run_id = %s AND status IN ('running', 'awaiting_approval')
RETURNING kind, trigger
"""

_RETRY_TARGET_SQL = f"""
SELECT r.run_id, r.status,
       EXISTS (SELECT 1 FROM orders o WHERE o.run_id = r.run_id
               AND (o.state IN {_PLACED_OR_LATER!r} OR o.delivery_agent_id IS NOT NULL))
FROM runs r
WHERE r.trigger->>'user_id' = %s AND r.started_at > now() - interval '{RETRY_WINDOW}'
  AND r.household_id IS NOT NULL
  AND coalesce(r.trigger->>'source', '') NOT IN {_NOT_RETRY_SOURCES!r}
ORDER BY r.started_at DESC
LIMIT 1
"""


async def start(
    conn: AsyncConnection,
    *,
    household_id: str | None,
    kind: str,
    trigger: dict[str, Any],
    run_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Start a run in the caller's transaction and return its `run_id`.

    `run_id` is passed by callers that derive it (`ids.a2a_run_id`); starting the same
    `run_id` again inserts nothing, writes no second `run_start` and creates no second timer.
    Every run except `a2a_demo` needs `trigger.user_id` (it is who gets the retry prompt).
    """
    if kind != A2A_DEMO and not trigger.get("user_id"):
        raise ValueError(f"a {kind} run needs trigger.user_id")
    # Callers pass UUIDs (`inbound_id`, `retry_of`, `user_id`); store them as JSON strings.
    trigger = json.loads(json.dumps(trigger, default=str))
    run_id = run_id or ids.new_run_id()
    settings = get_settings()
    timeout = (
        settings.a2a_demo_timeout_seconds if kind == A2A_DEMO else settings.run_timeout_seconds
    )
    async with conn.transaction():
        # An eval household's run never reaches a real vendor (adapters §3). A run with no
        # household counts as `live`, as `CallContext.from_meta` does.
        cur = await conn.execute(
            "SELECT env FROM households WHERE household_id = %s", (household_id,)
        )
        row = await cur.fetchone()
        modes = pin_for_env(await resolve_modes(conn), row[0] if row else "live")
        cur = await conn.execute(
            _INSERT_RUN_SQL, (run_id, household_id, kind, Jsonb(trigger), Jsonb(modes))
        )
        if await cur.fetchone() is None:
            return run_id
        await _TRACES.write(
            run_id,
            RUNS_AGENT,
            "run_start",
            {"kind": kind, "household_id": household_id, "modes": modes, "trigger": trigger},
            on_behalf_of=None,
            conn=conn,
        )
        await conn.execute(_INSERT_TIMER_SQL, (run_id, timeout))
    return run_id


_PAUSE_TIMEOUT_SQL = """
UPDATE timers SET status = 'cancelled'
WHERE run_id = %s AND kind = 'run_timeout' AND status = 'pending'
"""

_RESUME_TIMEOUT_SQL = """
INSERT INTO timers (kind, run_id, due_at)
SELECT 'run_timeout', run_id, now() + make_interval(secs => %s::float8)
FROM runs WHERE run_id = %s AND status IN ('running', 'awaiting_approval')
  AND NOT EXISTS (SELECT 1 FROM timers WHERE run_id = %s AND kind = 'run_timeout'
                  AND status = 'pending')
"""


async def pause_timeout(conn: AsyncConnection, run_id: uuid.UUID) -> None:
    """Cancel the run's pending `run_timeout` while an approval is pending (§4 timers)."""
    await conn.execute(_PAUSE_TIMEOUT_SQL, (run_id,))


async def resume_timeout(conn: AsyncConnection, run_id: uuid.UUID) -> None:
    """A new `run_timeout` once an approval is decided (§4 timers): the full timeout again,
    for an open run with no pending one."""
    cur = await conn.execute("SELECT kind FROM runs WHERE run_id = %s", (run_id,))
    row = await cur.fetchone()
    if row is None:
        return
    settings = get_settings()
    timeout = (
        settings.a2a_demo_timeout_seconds if row[0] == A2A_DEMO else settings.run_timeout_seconds
    )
    await conn.execute(_RESUME_TIMEOUT_SQL, (timeout, run_id, run_id))


async def finish(
    conn: AsyncConnection,
    run_id: uuid.UUID,
    agent_id: str,
    status: str,
    reason: str | None = None,
    summary: str | None = None,
) -> bool:
    """Apply the outcome rule (§10) in the caller's transaction; return whether it changed
    the run. Only the call that changes the run writes `outcome` and sends a retry prompt."""
    if status not in END_STATUSES:
        raise ValueError(f"status must be one of {sorted(END_STATUSES)}, got {status!r}")
    async with conn.transaction():
        cur = await conn.execute(_FINISH_SQL, (status, reason, run_id))
        row = await cur.fetchone()
        if row is None:
            return False
        kind, trigger = row
        await _TRACES.write(
            run_id,
            agent_id,
            "outcome",
            {"status": status, "reason": reason, "summary": summary},
            on_behalf_of=await _on_behalf_of(conn, agent_id),
            conn=conn,
        )
        if status in _RETRY_STATUSES and kind != A2A_DEMO and reason not in NO_RETRY_PROMPT:
            await outbox.send(
                conn,
                message_id=ids.retry_prompt_id(run_id),
                run_id=run_id,
                conversation_id=None,
                causation_id=None,
                from_agent=RUNS_AGENT,
                to_agent=WHATSAPP,
                type="whatsapp_out",
                body={
                    "user_id": trigger["user_id"],
                    "template": RETRY_PROMPT_TEMPLATE,
                    "params": {},
                    "buttons": [],
                },
            )
    return True


async def is_ended(conn: AsyncConnection, run_id: uuid.UUID) -> bool:
    """True if the run has an outcome. An unknown run counts as ended (fails closed)."""
    cur = await conn.execute("SELECT status FROM runs WHERE run_id = %s", (run_id,))
    row = await cur.fetchone()
    return row is None or row[0] not in _OPEN_STATUSES


async def retry_target(conn: AsyncConnection, user_id: uuid.UUID) -> uuid.UUID | None:
    """The user's latest run in the last 24 h if it ended `error` or `timeout` and its order
    never reached `placed`; otherwise `None` (architecture §9 "Retry"). Runs with no
    household or a `trigger.source` in `_NOT_RETRY_SOURCES` are skipped (experience EX-A16)."""
    cur = await conn.execute(_RETRY_TARGET_SQL, (str(user_id),))
    row = await cur.fetchone()
    if row is None:
        return None
    target, status, placed = row
    if status not in _RETRY_STATUSES or placed:
        return None
    return target
