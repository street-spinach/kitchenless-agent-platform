"""The trace store (architecture item 18 and §10, agent-protocol P13; issue #28)."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from services.common.trace import KINDS, TraceStore

pytestmark = pytest.mark.db

_PAYLOADS: dict[str, dict[str, Any]] = {
    "run_start": {"kind": "weekly", "household_id": "h1", "modes": {}, "trigger": "api"},
    "message": {
        "from": "consumer:u1",
        "to": "household:h1",
        "type": "user_message",
        "body": {},
        "summary": "user asks for the weekly order",
        "causation_id": None,
    },
    "tool_call": {"server": "grocery", "tool": "quote", "args": {}, "idempotency_key": None},
    "tool_result": {
        "server": "grocery",
        "tool": "quote",
        "result": {},
        "refused": False,
        "reason": None,
    },
    "decision": {"node": "pick_store", "choice": "grocery:a", "explanation": "cheapest"},
    "approval": {
        "approval_id": "a1",
        "status": "pending",
        "amount_minor": 1200,
        "currency": "USD",
        "user_id": "u1",
    },
    "data_share": {"address_ref": "addr:1", "recipient": "delivery:d1", "purpose": "drop"},
    "error": {"where": "protocol", "reason": "bad_body", "attempt": 1},
    "outcome": {"status": "completed", "reason": None, "summary": "order placed"},
}


def _with_store[T](pg: str, fn: Callable[[TraceStore], Awaitable[T]]) -> T:
    async def go() -> T:
        async with AsyncConnectionPool(pg, min_size=1, max_size=2, open=False) as pool:
            return await fn(TraceStore(pool))

    return asyncio.run(go())


def _rows(pg: str, run_id: uuid.UUID) -> list[dict[str, Any]]:
    with psycopg.connect(pg) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM agent_traces WHERE run_id = %s ORDER BY seq", (run_id,))
        return cur.fetchall()


def test_write_takes_run_id_per_call(pg: str) -> None:
    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    conversation_id, message_id = uuid.uuid4(), uuid.uuid4()

    async def go(store: TraceStore) -> list[int]:
        decision = _PAYLOADS["decision"]
        return [
            await store.write(run_a, "household:h1", "decision", decision, on_behalf_of=None),
            await store.write(run_b, "grocery:a", "decision", decision, on_behalf_of="org:o1"),
            await store.write(
                run_a,
                "household:h1",
                "message",
                _PAYLOADS["message"],
                on_behalf_of="household:h1",
                tokens_in=10,
                tokens_out=5,
                conversation_id=conversation_id,
                message_id=message_id,
                model="stub",
                latency_ms=42,
            ),
        ]

    assert _with_store(pg, go) == [1, 1, 2]

    rows_a = _rows(pg, run_a)
    assert [(r["seq"], r["kind"]) for r in rows_a] == [(1, "decision"), (2, "message")]
    message = rows_a[1]
    assert message["payload"] == _PAYLOADS["message"]
    assert (message["agent_id"], message["on_behalf_of"]) == ("household:h1", "household:h1")
    assert (message["tokens_in"], message["tokens_out"]) == (10, 5)
    assert (message["conversation_id"], message["message_id"]) == (conversation_id, message_id)
    assert (message["model"], message["latency_ms"]) == ("stub", 42)

    rows_b = _rows(pg, run_b)
    assert [(r["seq"], r["agent_id"], r["on_behalf_of"]) for r in rows_b] == [
        (1, "grocery:a", "org:o1")
    ]


def test_write_inside_callers_transaction(pg: str) -> None:
    run_id = uuid.uuid4()

    async def go(store: TraceStore) -> None:
        async with await psycopg.AsyncConnection.connect(pg) as conn:
            async with conn.transaction(force_rollback=True):
                await store.write(
                    run_id, "x:1", "error", _PAYLOADS["error"], on_behalf_of=None, conn=conn
                )
                cur = await conn.execute(
                    "SELECT count(*) FROM agent_traces WHERE run_id = %s", (run_id,)
                )
                assert await cur.fetchone() == (1,)

    _with_store(pg, go)
    assert _rows(pg, run_id) == []


def test_new_kinds_accepted(pg: str) -> None:
    assert KINDS == {
        "message",
        "tool_call",
        "tool_result",
        "decision",
        "approval",
        "outcome",
        "run_start",
        "error",
        "data_share",
    }
    run_id = uuid.uuid4()
    error_with_ref = {**_PAYLOADS["error"], "ref_message_id": str(uuid.uuid4())}

    async def go(store: TraceStore) -> None:
        for kind in ("run_start", "error", "data_share"):
            await store.write(run_id, "x:1", kind, _PAYLOADS[kind], on_behalf_of=None)
        await store.write(run_id, "x:1", "error", error_with_ref, on_behalf_of=None)

    _with_store(pg, go)
    rows = _rows(pg, run_id)
    assert [r["kind"] for r in rows] == ["run_start", "error", "data_share", "error"]
    assert rows[3]["payload"] == error_with_ref


def test_unknown_kind_rejected(pg: str) -> None:
    run_id = uuid.uuid4()

    async def go(store: TraceStore) -> None:
        await store.write(run_id, "x:1", "chatter", {}, on_behalf_of=None)

    with pytest.raises(ValueError, match="unknown trace kind"):
        _with_store(pg, go)
    assert _rows(pg, run_id) == []


def test_missing_payload_key_rejected(pg: str) -> None:
    run_id = uuid.uuid4()
    assert set(_PAYLOADS) == KINDS

    async def go(store: TraceStore) -> list[tuple[str, str]]:
        accepted = []
        for kind, full in _PAYLOADS.items():
            for key in full:
                payload = {k: v for k, v in full.items() if k != key}
                try:
                    await store.write(run_id, "x:1", kind, payload, on_behalf_of=None)
                except ValueError as exc:
                    assert f"missing required keys ['{key}']" in str(exc)
                else:
                    accepted.append((kind, key))
        return accepted

    assert _with_store(pg, go) == []
    assert _rows(pg, run_id) == []
