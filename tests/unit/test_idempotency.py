"""The idempotency store (architecture item 50, §5 guard step 4; issue #42)."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable

import psycopg
import pytest

from services.safety import idempotency
from services.safety.idempotency import Conflict, Done, New

pytestmark = pytest.mark.db

KEY = "8a4f6c1e-0000-4000-8000-000000000001:place_order:grocery:mock-us"


def _seed_run(pg: str) -> uuid.UUID:
    run_id = uuid.uuid4()
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, kind, trigger, modes) VALUES (%s, 'chat', '{}', '{}')",
            (run_id,),
        )
    return run_id


def _run[T](pg: str, fn: Callable[[psycopg.AsyncConnection], Awaitable[T]]) -> T:
    async def go() -> T:
        async with await psycopg.AsyncConnection.connect(pg) as conn:
            return await fn(conn)

    return asyncio.run(go())


def test_same_key_same_hash_returns_stored(pg: str) -> None:
    run_id = _seed_run(pg)
    stored = {"vendor_order_id": "v-1", "state": "placed", "total_minor": 4200}

    async def go(conn: psycopg.AsyncConnection) -> list[object]:
        first = await idempotency.claim(conn, KEY, run_id, "h1")
        async with conn.transaction():
            await idempotency.complete(conn, KEY, stored)
        second = await idempotency.claim(conn, KEY, run_id, "h1")
        return [first, second]

    first, second = _run(pg, go)
    assert first == New()
    assert second == Done(stored)


def test_same_key_different_hash_conflicts(pg: str) -> None:
    run_id = _seed_run(pg)

    async def go(conn: psycopg.AsyncConnection) -> list[object]:
        first = await idempotency.claim(conn, KEY, run_id, "h1")
        async with conn.transaction():
            await idempotency.complete(conn, KEY, {"state": "placed"})
        return [first, await idempotency.claim(conn, KEY, run_id, "h2")]

    first, second = _run(pg, go)
    assert first == New()
    assert second == Conflict()


def test_stale_in_progress_reclaimable(pg: str) -> None:
    run_id = _seed_run(pg)
    retry_run = _seed_run(pg)

    async def go(conn: psycopg.AsyncConnection) -> list[object]:
        first = await idempotency.claim(conn, KEY, run_id, "h1")
        fresh = await idempotency.claim(conn, KEY, run_id, "h1")
        await conn.execute(
            "UPDATE idempotency_keys SET updated_at = now() - interval '61 seconds' WHERE key = %s",
            (KEY,),
        )
        await conn.commit()
        return [first, fresh, await idempotency.claim(conn, KEY, retry_run, "h1")]

    first, fresh, stale = _run(pg, go)
    assert first == New()
    assert fresh == Conflict()  # still in flight, younger than 60 s
    assert stale == New()
    with psycopg.connect(pg) as conn:
        row = conn.execute(
            "SELECT run_id, status, updated_at > now() - interval '10 seconds'"
            " FROM idempotency_keys WHERE key = %s",
            (KEY,),
        ).fetchone()
    assert row == (retry_run, "in_progress", True)


def test_concurrent_claim_one_wins(pg: str) -> None:
    run_id = _seed_run(pg)

    async def go() -> list[object]:
        async with (
            await psycopg.AsyncConnection.connect(pg) as a,
            await psycopg.AsyncConnection.connect(pg) as b,
        ):
            return list(
                await asyncio.gather(
                    idempotency.claim(a, KEY, run_id, "h1"),
                    idempotency.claim(b, KEY, run_id, "h1"),
                )
            )

    results = asyncio.run(go())
    assert sorted(type(r).__name__ for r in results) == ["Conflict", "New"]
    with psycopg.connect(pg) as conn:
        count = conn.execute(
            "SELECT count(*) FROM idempotency_keys WHERE key = %s", (KEY,)
        ).fetchone()
    assert count == (1,)
