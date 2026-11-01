"""The idempotency store (architecture item 50, §5 guard step 4).

`claim()` runs one `INSERT ... ON CONFLICT DO NOTHING RETURNING`. If nothing was inserted it
locks the existing row with `SELECT ... FOR UPDATE` and decides:

*   same hash, `done`: `Done(result)`, the stored result;
*   same hash, `in_progress` for more than 60 s: taken over, `New`;
*   different hash: `Conflict`.

A same-hash `in_progress` row younger than 60 s is another call still in flight. The
design's result type has no other case for it, so it is a `Conflict` too: only one of two
concurrent claims wins.
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
    """The key is held by a different request, or by the same request still in flight."""


_INSERT_SQL = """
INSERT INTO idempotency_keys (key, run_id, request_hash, status)
VALUES (%s, %s, %s, 'in_progress')
ON CONFLICT (key) DO NOTHING
RETURNING key
"""
_SELECT_SQL = f"""
SELECT request_hash, status, result,
       updated_at < now() - interval '{STALE_SECONDS} seconds' AS stale
FROM idempotency_keys WHERE key = %s
FOR UPDATE
"""
_TAKE_OVER_SQL = """
UPDATE idempotency_keys SET run_id = %s, updated_at = now() WHERE key = %s
"""
_COMPLETE_SQL = """
UPDATE idempotency_keys SET result = %s, status = 'done', updated_at = now() WHERE key = %s
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
        stored_hash, status, result, stale = row
        if stored_hash != request_hash:
            return Conflict()
        if status == "done":
            return Done(result)
        if stale:
            await conn.execute(_TAKE_OVER_SQL, (run_id, key))
            return New()
        return Conflict()


async def complete(conn: AsyncConnection, key: str, result: Any) -> None:
    """Store `result` under `key` and mark it `done`, in the caller's transaction."""
    cur = await conn.execute(_COMPLETE_SQL, (Jsonb(result), key))
    if cur.rowcount != 1:
        raise RuntimeError(f"idempotency key {key!r} was never claimed")
