"""The `@traced` wrapper for unguarded tools (architecture item 42; issue #45)."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from services.common import db
from services.mcp.meta import CallContext
from services.mcp.traced import traced
from services.safety.guard import GuardRefused

pytestmark = pytest.mark.db

AGENT = "household:us-1"
PANTRY = {"items": [{"sku": "milk-1l", "qty": 1}]}


def _seed_run(pg: str, ended: bool = False) -> uuid.UUID:
    run_id = uuid.uuid4()
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, kind, status, trigger, modes, ended_at)"
            " VALUES (%s, 'chat', %s, '{}', '{}', CASE WHEN %s THEN now() END)",
            (run_id, "completed" if ended else "running", ended),
        )
    return run_id


def _ctx(run_id: uuid.UUID) -> CallContext:
    return CallContext(
        run_id=run_id,
        agent_id=AGENT,
        conversation_id=uuid.uuid4(),
        attempt=1,
        token="t",
        env="live",
        modes={},
        on_behalf_of="household:us-1",
    )


def _traces(pg: str, run_id: uuid.UUID) -> list[dict[str, Any]]:
    with psycopg.connect(pg) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT kind, agent_id, on_behalf_of, conversation_id, payload FROM agent_traces"
            " WHERE run_id = %s ORDER BY seq",
            (run_id,),
        )
        return cur.fetchall()


def _with_pool[T](pg: str, monkeypatch: pytest.MonkeyPatch, fn: Callable[[], Awaitable[T]]) -> T:
    async def go() -> T:
        async with AsyncConnectionPool(pg, min_size=1, max_size=3, open=False) as pool:
            monkeypatch.setattr(db, "get_pool", lambda: pool)
            return await fn()

    return asyncio.run(go())


def test_traced_writes_one_call_and_result(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed_run(pg)
    ctx = _ctx(run_id)
    calls: list[dict[str, Any]] = []

    @traced("household")
    async def get_pantry(ctx: CallContext, **args: Any) -> dict[str, Any]:
        calls.append(args)
        return PANTRY

    result = _with_pool(pg, monkeypatch, lambda: get_pantry(ctx, zone_id="z1"))

    assert result == PANTRY
    assert calls == [{"zone_id": "z1"}]
    rows = _traces(pg, run_id)
    assert [r["kind"] for r in rows] == ["tool_call", "tool_result"]
    assert rows[0]["payload"] == {
        "server": "household",
        "tool": "get_pantry",
        "args": {"zone_id": "z1"},
        "idempotency_key": None,
    }
    assert rows[1]["payload"] == {
        "server": "household",
        "tool": "get_pantry",
        "result": PANTRY,
        "refused": False,
        "reason": None,
    }
    for row in rows:
        assert (row["agent_id"], row["on_behalf_of"], row["conversation_id"]) == (
            AGENT,
            "household:us-1",
            ctx.conversation_id,
        )


def test_traced_refuses_ended_run(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed_run(pg, ended=True)
    called = False

    @traced("household")
    async def get_budget(ctx: CallContext) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    with pytest.raises(GuardRefused) as e:
        _with_pool(pg, monkeypatch, lambda: get_budget(_ctx(run_id)))

    assert e.value.reason == "run_ended"
    assert not called
    [row] = _traces(pg, run_id)
    assert row["kind"] == "tool_result"
    assert row["payload"] == {
        "server": "household",
        "tool": "get_budget",
        "result": None,
        "refused": True,
        "reason": "run_ended",
    }
