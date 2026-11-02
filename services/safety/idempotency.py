"""The idempotency store (architecture item 50, §5 guard step 4).

`claim()` runs one `INSERT ... ON CONFLICT DO NOTHING RETURNING`. If nothing was inserted it
locks the existing row with `SELECT ... FOR UPDATE` and decides:

*   different hash: `Conflict`;
*   same hash, `done`: `Done(result)`, the stored result;
*   same hash, `in_progress`, claimed by the caller's own run: taken over, `New`, whatever
    its age (adapters design §9). The client's single retry after an adapter failure
    reaches the vendor again; it sends the same vendor key, so the vendor deduplicates;
*   same hash, `in_progress`, claimed by another run, for more than 60 s: taken over, `New`;
*   same hash, `in_progress`, claimed by another run, younger than 60 s: `Conflict`, a call
    still in flight.

Because two calls from one run can both hold the key, `complete()` is "first complete
wins" (§5 guard step 7): it stores the result only while the key is `in_progress`. A later
completer gets `Done(stored_result)` back and must not apply the tool's state change again.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

__all__ = ["STALE_SECONDS", "Conflict", "Done", "New", "claim", "complete"]

STALE_SECONDS = 60


@dataclass(frozen=True)
class New:
    """The caller owns the key and should call the adapter."""


@dataclass(frozen=True)
class Done:
    """The key already finished with the same request; `result` is the stored result."""

    result: Any


@dataclass(frozen=True)
class Conflict:
    """The key is held by a different request, or by another run still in flight."""


_INSERT_SQL = """
INSERT INTO idempotency_keys (key, run_id, request_hash, status)
VALUES (%s, %s, %s, 'in_progress')
ON CONFLICT (key) DO NOTHING
RETURNING key
"""
_SELECT_SQL = f"""
SELECT request_hash, status, result, run_id,
       updated_at < now() - interval '{STALE_SECONDS} seconds' AS stale
FROM idempotency_keys WHERE key = %s
FOR UPDATE
"""
_TAKE_OVER_SQL = """
UPDATE idempotency_keys SET run_id = %s, updated_at = now() WHERE key = %s
"""
_COMPLETE_SQL = """
UPDATE idempotency_keys SET result = %s, status = 'done', updated_at = now()
WHERE key = %s AND status = 'in_progress'
"""
_STORED_SQL = """
SELECT status, result FROM idempotency_keys WHERE key = %s
"""


async def claim(
    conn: AsyncConnection, key: str, run_id: uuid.UUID, request_hash: str
) -> New | Done | Conflict:
    """Claim `key` for this request. The claim is committed with the caller's transaction."""
    async with conn.transaction():
        cur = await conn.execute(_INSERT_SQL, (key, run_id, request_hash))
        if await cur.fetchone() is not None:
            return New()
        cur = await conn.execute(_SELECT_SQL, (key,))
        row = await cur.fetchone()
        if row is None:  # pragma: no cover - keys are never deleted
            raise RuntimeError(f"idempotency key {key!r} vanished during claim")
        stored_hash, status, result, holder, stale = row
        if stored_hash != request_hash:
            return Conflict()
        if status == "done":
            return Done(result)
        if holder == run_id or stale:
            await conn.execute(_TAKE_OVER_SQL, (run_id, key))
            return New()
        return Conflict()


async def complete(conn: AsyncConnection, key: str, result: Any) -> Done | None:
    """Store `result` under `key` and mark it `done`, in the caller's transaction.

    First complete wins: the result is stored only while the key is `in_progress`, and then
    this returns `None`. If another call already completed the key, nothing is written and
    the stored result comes back as `Done(stored_result)`.
    """
    cur = await conn.execute(_COMPLETE_SQL, (Jsonb(result), key))
    if cur.rowcount == 1:
        return None
    cur = await conn.execute(_STORED_SQL, (key,))
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError(f"idempotency key {key!r} was never claimed")
    status, stored = row
    if status != "done":  # pragma: no cover - a key is either in_progress or done
        raise RuntimeError(f"idempotency key {key!r} has unexpected status {status!r}")
    return Done(stored)
