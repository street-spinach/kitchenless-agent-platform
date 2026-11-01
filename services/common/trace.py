"""The trace store (architecture item 18 and §10, agent-protocol P13).

`TraceStore` is not bound to a run: every `write` names its `run_id`. Several agents can
write to the same run, so `seq` is allocated in Postgres under a per-run advisory lock, with
the same SQL as the Phase 0 writer (`services/agents/common/trace.py`, which `hello` keeps
using unchanged).
"""

import uuid
from collections.abc import Mapping
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

__all__ = ["KINDS", "REQUIRED_KEYS", "TraceStore", "validate"]

# Required payload keys per kind (architecture §10; `message` per agent-protocol P13).
# Keys that may hold `null` (`causation_id`, `idempotency_key`) must still be present.
# Optional keys, such as `ref_message_id` on `error`, are accepted and not required.
REQUIRED_KEYS: Mapping[str, frozenset[str]] = {
    "run_start": frozenset({"kind", "household_id", "modes", "trigger"}),
    "message": frozenset({"from", "to", "type", "body", "summary", "causation_id"}),
    "tool_call": frozenset({"server", "tool", "args", "idempotency_key"}),
    "tool_result": frozenset({"server", "tool", "result", "refused", "reason"}),
    "decision": frozenset({"node", "choice", "explanation"}),
    "approval": frozenset({"approval_id", "status", "amount_minor", "currency", "user_id"}),
    "data_share": frozenset({"address_ref", "recipient", "purpose"}),
    "error": frozenset({"where", "reason", "attempt"}),
    "outcome": frozenset({"status", "reason", "summary"}),
}

# The Phase 0 kinds plus `run_start`, `error` and `data_share`.
KINDS = frozenset(REQUIRED_KEYS)

_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))"
_INSERT_SQL = """
INSERT INTO agent_traces
  (run_id, seq, agent_id, on_behalf_of, kind, payload, tokens_in, tokens_out,
   conversation_id, message_id, model, latency_ms)
SELECT %(run_id)s, COALESCE(MAX(seq), 0) + 1, %(agent_id)s, %(on_behalf_of)s, %(kind)s,
       %(payload)s, %(tokens_in)s, %(tokens_out)s,
       %(conversation_id)s, %(message_id)s, %(model)s, %(latency_ms)s
FROM agent_traces WHERE run_id = %(run_id)s
RETURNING seq
"""


def validate(kind: str, payload: Any) -> None:
    """Raise `ValueError` for an unknown kind or a payload missing a required key."""
    if kind not in KINDS:
        raise ValueError(f"unknown trace kind {kind!r}; expected one of {sorted(KINDS)}")
    if not isinstance(payload, dict):
        raise TypeError("trace payload must be a dict")
    missing = REQUIRED_KEYS[kind] - payload.keys()
    if missing:
        raise ValueError(f"{kind} trace payload is missing required keys {sorted(missing)}")


class TraceStore:
    """Writes `agent_traces` rows for any run."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def write(
        self,
        run_id: uuid.UUID,
        agent_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        on_behalf_of: str | None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        conversation_id: uuid.UUID | None = None,
        message_id: uuid.UUID | None = None,
        model: str | None = None,
        latency_ms: int | None = None,
        conn: AsyncConnection | None = None,
    ) -> int:
        """Write one row and return its `seq` within the run.

        With `conn`, the row is written inside the caller's transaction. Without it, a pool
        connection is used and the row is committed on return.
        """
        validate(kind, payload)
        params = {
            "run_id": run_id,
            "agent_id": agent_id,
            "on_behalf_of": on_behalf_of,
            "kind": kind,
            "payload": Jsonb(payload),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "model": model,
            "latency_ms": latency_ms,
        }
        if conn is not None:
            return await _insert(conn, params)
        async with self._pool.connection() as own:
            return await _insert(own, params)


async def _insert(conn: AsyncConnection, params: dict[str, Any]) -> int:
    async with conn.transaction():
        await conn.execute(_LOCK_SQL, (str(params["run_id"]),))
        cur = await conn.execute(_INSERT_SQL, params)
        row = await cur.fetchone()
    assert row is not None
    return row[0]
