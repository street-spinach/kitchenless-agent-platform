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
        fresh = await idempotency.claim(conn, KEY, retry_run, "h1")
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


def _concurrent_claims(pg: str, run_a: uuid.UUID, run_b: uuid.UUID) -> list[object]:
    async def go() -> list[object]:
        async with (
            await psycopg.AsyncConnection.connect(pg) as a,
            await psycopg.AsyncConnection.connect(pg) as b,
        ):
            return list(
                await asyncio.gather(
                    idempotency.claim(a, KEY, run_a, "h1"),
                    idempotency.claim(b, KEY, run_b, "h1"),
                )
            )

    return asyncio.run(go())


def _key_count(pg: str) -> tuple[int, ...] | None:
    with psycopg.connect(pg) as conn:
        return conn.execute(
            "SELECT count(*) FROM idempotency_keys WHERE key = %s", (KEY,)
        ).fetchone()


def test_concurrent_claim_one_wins(pg: str) -> None:
    results = _concurrent_claims(pg, _seed_run(pg), _seed_run(pg))
    assert sorted(type(r).__name__ for r in results) == ["Conflict", "New"]
    assert _key_count(pg) == (1,)


def test_same_run_in_progress_is_taken_over(pg: str) -> None:
    run_id = _seed_run(pg)

    async def go(conn: psycopg.AsyncConnection) -> list[object]:
        first = await idempotency.claim(conn, KEY, run_id, "h1")
        return [first, await idempotency.claim(conn, KEY, run_id, "h1")]

    first, retry = _run(pg, go)
    assert first == New()
    assert retry == New()  # under 60 s, same run: the retry after an adapter failure
    with psycopg.connect(pg) as conn:
        row = conn.execute(
            "SELECT run_id, status FROM idempotency_keys WHERE key = %s", (KEY,)
        ).fetchone()
    assert row == (run_id, "in_progress")


def test_other_run_in_progress_conflicts(pg: str) -> None:
    run_id = _seed_run(pg)
    other_run = _seed_run(pg)

    async def go(conn: psycopg.AsyncConnection) -> list[object]:
        first = await idempotency.claim(conn, KEY, run_id, "h1")
        return [first, await idempotency.claim(conn, KEY, other_run, "h1")]

    first, other = _run(pg, go)
    assert first == New()
    assert other == Conflict()
    with psycopg.connect(pg) as conn:
        row = conn.execute("SELECT run_id FROM idempotency_keys WHERE key = %s", (KEY,)).fetchone()
    assert row == (run_id,)


def test_concurrent_claims_same_run_both_new(pg: str) -> None:
    run_id = _seed_run(pg)
    results = _concurrent_claims(pg, run_id, run_id)
    assert results == [New(), New()]
    assert _key_count(pg) == (1,)


def test_second_complete_returns_first_result(pg: str) -> None:
    run_id = _seed_run(pg)
    first_result = {"vendor_order_id": "v-1", "state": "placed", "total_minor": 4200}
    second_result = {"vendor_order_id": "v-1", "state": "placed", "total_minor": 9999}

    async def go(conn: psycopg.AsyncConnection) -> list[object]:
        await idempotency.claim(conn, KEY, run_id, "h1")
        await idempotency.claim(conn, KEY, run_id, "h1")
        async with conn.transaction():
            first = await idempotency.complete(conn, KEY, first_result)
        async with conn.transaction():
            second = await idempotency.complete(conn, KEY, second_result)
        return [first, second]

    first, second = _run(pg, go)
    assert first is None
    assert second == Done(first_result)
    with psycopg.connect(pg) as conn:
        row = conn.execute(
            "SELECT status, result FROM idempotency_keys WHERE key = %s", (KEY,)
        ).fetchone()
    assert row == ("done", first_result)


def test_complete_unclaimed_key_raises(pg: str) -> None:
    async def go(conn: psycopg.AsyncConnection) -> None:
        async with conn.transaction():
            await idempotency.complete(conn, KEY, {"state": "placed"})

    with pytest.raises(RuntimeError, match="never claimed"):
        _run(pg, go)
