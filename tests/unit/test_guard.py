"""The guard (architecture item 49, §5 guard steps; issue #42)."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from services.common import ids
from services.common.trace import TraceStore
from services.identity import fga, tokens
from services.safety import guard
from services.safety.guard import GuardRefused, run_guarded

pytestmark = pytest.mark.db

SECRET = "unit-test-mock-token-secret-32-bytes-long"
SELLER = "grocery:mock-us"
ORDER_ID = uuid.UUID("8a4f6c1e-0000-4000-8000-000000000001")
KEY = ids.idempotency_key(ORDER_ID, "place_order", SELLER)
REQUEST = {"order_id": str(ORDER_ID), "quote_id": "q-1", "items_hash": "abc"}
RESULT = {"vendor_order_id": "v-1", "state": "placed", "total_minor": 4200}


@dataclass(frozen=True)
class Ctx:
    run_id: uuid.UUID
    agent_id: str
    token: str | None
    on_behalf_of: str | None = "org:mock"
    conversation_id: uuid.UUID | None = None


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tokens, "_secret", lambda: SECRET)


def _token(act: str = SELLER) -> str:
    return tokens.MockIssuer(secret=SECRET).issue("user:us-1-a", act, ["orders"])


def _seed_run(pg: str, status: str = "running", ended: bool = False) -> uuid.UUID:
    run_id = uuid.uuid4()
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, kind, status, trigger, modes, ended_at)"
            " VALUES (%s, 'chat', %s, '{}', '{}', CASE WHEN %s THEN now() END)",
            (run_id, status, ended),
        )
    return run_id


def _traces(pg: str, run_id: uuid.UUID) -> list[dict[str, Any]]:
    with psycopg.connect(pg) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT kind, agent_id, on_behalf_of, payload FROM agent_traces"
            " WHERE run_id = %s ORDER BY seq",
            (run_id,),
        )
        return cur.fetchall()


def _with_pool[T](pg: str, monkeypatch: pytest.MonkeyPatch, fn: Callable[[], Awaitable[T]]) -> T:
    async def go() -> T:
        async with AsyncConnectionPool(pg, min_size=1, max_size=3, open=False) as pool:
            monkeypatch.setattr(guard.db, "get_pool", lambda: pool)
            return await fn()

    return asyncio.run(go())


def test_guard_order_of_checks(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed_run(pg)
    steps: list[str] = []
    real_validate, real_write = tokens.validate, TraceStore.write

    def validate(token: str) -> tokens.Claims:
        steps.append("token")
        return real_validate(token)

    def check(user: str, relation: str, object: str) -> bool:
        steps.append(f"fga {user} {relation} {object}")
        return True

    async def write(self: TraceStore, run: uuid.UUID, agent: str, kind: str, *a, **kw) -> int:
        steps.append(f"trace {kind}")
        return await real_write(self, run, agent, kind, *a, **kw)

    async def limits(conn: AsyncConnection) -> None:
        steps.append("limits")

    async def call() -> dict[str, Any]:
        steps.append("adapter")
        with psycopg.connect(pg) as conn:
            row = conn.execute(
                "SELECT status FROM idempotency_keys WHERE key = %s", (KEY,)
            ).fetchone()
        steps.append(f"key {row[0] if row else None}")
        return RESULT

    async def apply(conn: AsyncConnection, result: dict[str, Any]) -> None:
        steps.append("apply")

    monkeypatch.setattr(tokens, "validate", validate)
    monkeypatch.setattr(fga, "check", check)
    monkeypatch.setattr(TraceStore, "write", write)
    ctx = Ctx(run_id, SELLER, _token())

    result = _with_pool(
        pg,
        monkeypatch,
        lambda: run_guarded(
            ctx,
            tool="grocery.place_order",
            relation=("member_of", "org:mock"),
            limits=limits,
            key=KEY,
            request=REQUEST,
            call=call,
            apply=apply,
        ),
    )

    assert result == RESULT
    assert steps == [
        "token",
        f"fga agent:{SELLER} member_of org:mock",
        "limits",
        "trace tool_call",
        "adapter",
        "key in_progress",
        "apply",
        "trace tool_result",
    ]
    rows = _traces(pg, run_id)
    assert [r["kind"] for r in rows] == ["tool_call", "tool_result"]
    assert rows[0]["payload"] == {
        "server": "grocery",
        "tool": "place_order",
        "args": REQUEST,
        "idempotency_key": KEY,
    }
    assert rows[1]["payload"] == {
        "server": "grocery",
        "tool": "place_order",
        "result": RESULT,
        "refused": False,
        "reason": None,
    }
    assert {(r["agent_id"], r["on_behalf_of"]) for r in rows} == {(SELLER, "org:mock")}
    with psycopg.connect(pg) as conn:
        stored = conn.execute(
            "SELECT status, result FROM idempotency_keys WHERE key = %s", (KEY,)
        ).fetchone()
    assert stored == ("done", RESULT)

    # The same call again returns the stored result without calling the adapter.
    steps.clear()
    again = _with_pool(
        pg,
        monkeypatch,
        lambda: run_guarded(
            ctx,
            tool="grocery.place_order",
            relation=("member_of", "org:mock"),
            limits=limits,
            key=KEY,
            request=REQUEST,
            call=call,
            apply=apply,
        ),
    )
    assert again == RESULT
    assert "adapter" not in steps and "apply" not in steps


@pytest.mark.parametrize(("status", "ended"), [("completed", True), ("timeout", False)])
def test_ended_run_refused(
    pg: str, monkeypatch: pytest.MonkeyPatch, status: str, ended: bool
) -> None:
    run_id = _seed_run(pg, status=status, ended=ended)
    called: list[str] = []

    def validate(token: str) -> tokens.Claims:
        called.append("token")
        raise AssertionError("token checked after the run ended")

    async def call() -> dict[str, Any]:
        called.append("adapter")
        return RESULT

    monkeypatch.setattr(tokens, "validate", validate)

    with pytest.raises(GuardRefused) as exc:
        _with_pool(
            pg,
            monkeypatch,
            lambda: run_guarded(
                Ctx(run_id, SELLER, _token()),
                tool="grocery.place_order",
                relation=None,
                limits=None,
                key=KEY,
                request=REQUEST,
                call=call,
                apply=None,
            ),
        )

    assert exc.value.reason == "run_ended"
    assert called == []
    with psycopg.connect(pg) as conn:
        assert conn.execute("SELECT count(*) FROM idempotency_keys").fetchone() == (0,)


def test_trace_failure_blocks_adapter_call(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed_run(pg)
    called: list[str] = []

    async def write(self: TraceStore, *a: Any, **kw: Any) -> int:
        raise psycopg.OperationalError("trace store down")

    async def call() -> dict[str, Any]:
        called.append("adapter")
        return RESULT

    monkeypatch.setattr(fga, "check", lambda user, relation, object: True)
    monkeypatch.setattr(TraceStore, "write", write)

    with pytest.raises(psycopg.OperationalError):
        _with_pool(
            pg,
            monkeypatch,
            lambda: run_guarded(
                Ctx(run_id, SELLER, _token()),
                tool="grocery.place_order",
                relation=("member_of", "org:mock"),
                limits=None,
                key=KEY,
                request=REQUEST,
                call=call,
                apply=None,
            ),
        )

    assert called == []


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("no_token", "token_missing"),
        ("bad_token", "token_invalid"),
        ("other_agent", "act_mismatch"),
        ("fga_denied", "authz_denied"),
        ("limit", "needs_approval"),
        ("conflict", "idempotency_conflict"),
    ],
)
def test_refusal_writes_tool_result(
    pg: str, monkeypatch: pytest.MonkeyPatch, case: str, reason: str
) -> None:
    run_id = _seed_run(pg)
    called: list[str] = []
    token: str | None = {
        "no_token": None,
        "bad_token": "not-a-jwt",
        "other_agent": _token(act="grocery:someone-else"),
    }.get(case, _token())
    monkeypatch.setattr(fga, "check", lambda user, relation, object: case != "fga_denied")
    if case == "conflict":
        with psycopg.connect(pg) as conn:
            conn.execute(
                "INSERT INTO idempotency_keys (key, run_id, request_hash, status, result)"
                " VALUES (%s, %s, 'another-request', 'done', '{}')",
                (KEY, run_id),
            )

    async def limits(conn: AsyncConnection) -> None:
        if case == "limit":
            raise GuardRefused("needs_approval")

    async def call() -> dict[str, Any]:
        called.append("adapter")
        return RESULT

    with pytest.raises(GuardRefused) as exc:
        _with_pool(
            pg,
            monkeypatch,
            lambda: run_guarded(
                Ctx(run_id, SELLER, token),
                tool="grocery.place_order",
                relation=("member_of", "org:mock"),
                limits=limits,
                key=KEY,
                request=REQUEST,
                call=call,
                apply=None,
            ),
        )

    assert exc.value.reason == reason
    assert called == []
    rows = _traces(pg, run_id)
    assert [r["kind"] for r in rows] == ["tool_result"]
    assert rows[0]["payload"] == {
        "server": "grocery",
        "tool": "place_order",
        "result": None,
        "refused": True,
        "reason": reason,
    }
    assert (rows[0]["agent_id"], rows[0]["on_behalf_of"]) == (SELLER, "org:mock")
