"""The MCP call context and error results (architecture item 41, amended 2026-11-01)."""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from services.common import db
from services.mcp import meta
from services.mcp.meta import AdapterFailure, CallContext, tool_error, tool_errors
from services.safety.guard import GuardRefused

AGENT = "household:us-1"


def _seed(pg: str, *, owner_ref: str = "household:us-1", env: str = "eval") -> uuid.UUID:
    run_id = uuid.uuid4()
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO households (household_id, name, env, region, zone_id, timezone,"
            " currency) VALUES ('us-1', 'Us One', %s, 'us', 'z1', 'America/New_York', 'USD')",
            (env,),
        )
        conn.execute(
            "INSERT INTO agent_cards"
            " (agent_id, class, owner_ref, region, zone_ids, currency, host_process)"
            " VALUES (%s, 'household', %s, 'us', ARRAY['z1'], 'USD', 'agents')",
            (AGENT, owner_ref),
        )
        conn.execute(
            "INSERT INTO runs (run_id, household_id, kind, trigger, modes)"
            " VALUES (%s, 'us-1', 'weekly', '{}', '{\"doordash\": \"mock\"}')",
            (run_id,),
        )
    return run_id


def _with_pool[T](pg: str, monkeypatch: pytest.MonkeyPatch, fn: Callable[[], Awaitable[T]]) -> T:
    async def go() -> T:
        async with AsyncConnectionPool(pg, min_size=1, max_size=3, open=False) as pool:
            monkeypatch.setattr(db, "get_pool", lambda: pool)
            return await fn()

    return asyncio.run(go())


def _from_meta(pg: str, monkeypatch: pytest.MonkeyPatch, m: dict[str, Any]) -> CallContext:
    return _with_pool(pg, monkeypatch, lambda: CallContext.from_meta(m))


def _ctx(run_id: uuid.UUID, on_behalf_of: str | None = "household:us-1") -> CallContext:
    return CallContext(
        run_id=run_id,
        agent_id=AGENT,
        conversation_id=uuid.uuid4(),
        attempt=2,
        token="t",
        env="eval",
        modes={},
        on_behalf_of=on_behalf_of,
    )


def _traces(pg: str, run_id: uuid.UUID) -> list[dict[str, Any]]:
    with psycopg.connect(pg) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT kind, agent_id, on_behalf_of, conversation_id, payload FROM agent_traces"
            " WHERE run_id = %s ORDER BY seq",
            (run_id,),
        )
        return cur.fetchall()


@pytest.mark.db
def test_from_meta_reads_token_from_header_context(
    pg: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(pg)
    conversation_id = uuid.uuid4()
    m = {
        "run_id": str(run_id),
        "agent_id": AGENT,
        "conversation_id": str(conversation_id),
        "attempt": 2,
        "token": "from-meta",
    }

    reset = meta.set_current_token("from-header")
    try:
        ctx = _from_meta(pg, monkeypatch, m)
    finally:
        meta.reset_current_token(reset)

    assert ctx.token == "from-header"
    assert (ctx.run_id, ctx.agent_id, ctx.conversation_id, ctx.attempt) == (
        run_id,
        AGENT,
        conversation_id,
        2,
    )
    assert (ctx.env, ctx.modes) == ("eval", {"doordash": "mock"})
    assert meta.current_token() is None


@pytest.mark.parametrize(
    "m",
    [
        None,
        {},
        {"agent_id": AGENT},
        {"run_id": str(uuid.uuid4())},
        {"run_id": "not-a-uuid", "agent_id": AGENT},
        {"run_id": str(uuid.uuid4()), "agent_id": ""},
    ],
)
def test_from_meta_refuses_missing_run_or_agent(m: dict[str, Any] | None) -> None:
    with pytest.raises(GuardRefused) as e:
        asyncio.run(CallContext.from_meta(m))
    assert e.value.reason == meta.META_REFUSED


@pytest.mark.db
def test_from_meta_refuses_agent_without_card(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed(pg)
    with pytest.raises(GuardRefused) as e:
        _from_meta(pg, monkeypatch, {"run_id": str(run_id), "agent_id": "household:nobody"})
    assert e.value.reason == meta.META_REFUSED


@pytest.mark.db
@pytest.mark.parametrize(
    ("owner_ref", "expected"), [("household:us-1", "household:us-1"), ("system", None)]
)
def test_from_meta_sets_on_behalf_of_from_card_owner_ref(
    pg: str, monkeypatch: pytest.MonkeyPatch, owner_ref: str, expected: str | None
) -> None:
    run_id = _seed(pg, owner_ref=owner_ref)
    ctx = _from_meta(pg, monkeypatch, {"run_id": str(run_id), "agent_id": AGENT})
    assert ctx.on_behalf_of == expected
    assert (ctx.attempt, ctx.conversation_id) == (1, None)


def test_tool_error_shape() -> None:
    result = tool_error("unavailable", "adapter_5xx", retryable=True)
    body = {"kind": "unavailable", "reason": "adapter_5xx", "retryable": True}
    assert result.is_error is True
    assert result.structured_content == body
    assert json.loads(result.content[0].text) == body
    assert tool_error("refused", "authz_denied").structured_content["retryable"] is False


@pytest.mark.db
def test_tool_errors_maps_guard_refused_and_adapter_failure(
    pg: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(pg)
    ctx = _ctx(run_id)

    async def run(exc: Exception | None) -> Any:
        async with tool_errors(ctx, "household", "authorize_spend") as err:
            if exc is not None:
                raise exc
            return "ok"
        return err.result

    async def go() -> list[Any]:
        return [
            await run(None),
            await run(GuardRefused("authz_denied")),
            await run(AdapterFailure("adapter_timeout", retryable=True)),
            await run(AdapterFailure("adapter_4xx", retryable=False)),
        ]

    ok, refused, timeout, client_error = _with_pool(pg, monkeypatch, go)

    assert ok == "ok"
    assert refused.is_error and refused.structured_content == {
        "kind": "refused",
        "reason": "authz_denied",
        "retryable": False,
    }
    assert timeout.structured_content == {
        "kind": "unavailable",
        "reason": "adapter_timeout",
        "retryable": True,
    }
    assert client_error.structured_content == {
        "kind": "unavailable",
        "reason": "adapter_4xx",
        "retryable": False,
    }
    # Only the adapter failures write an error row.
    assert [r["kind"] for r in _traces(pg, run_id)] == ["error", "error"]

    # Any other exception propagates.
    with pytest.raises(RuntimeError):
        _with_pool(pg, monkeypatch, lambda: run(RuntimeError("bug")))


@pytest.mark.db
@pytest.mark.parametrize("on_behalf_of", ["household:us-1", None])
def test_tool_errors_error_row_has_where_attempt_and_on_behalf_of(
    pg: str, monkeypatch: pytest.MonkeyPatch, on_behalf_of: str | None
) -> None:
    run_id = _seed(pg)
    ctx = _ctx(run_id, on_behalf_of)

    async def go() -> None:
        async with tool_errors(ctx, "delivery", "book_delivery"):
            raise AdapterFailure("adapter_5xx", retryable=True)

    _with_pool(pg, monkeypatch, go)

    [row] = _traces(pg, run_id)
    assert row["kind"] == "error"
    assert row["payload"] == {
        "where": "delivery.book_delivery",
        "reason": "adapter_5xx",
        "attempt": 2,
    }
    assert (row["agent_id"], row["on_behalf_of"], row["conversation_id"]) == (
        AGENT,
        on_behalf_of,
        ctx.conversation_id,
    )
