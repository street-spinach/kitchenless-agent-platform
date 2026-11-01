"""The outbox send() (architecture item 19, agent-protocol P11; issue #34)."""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from services.common import outbox
from services.common.protocol import OutboundInvalid
from tests.unit.protocol.conftest import SAMPLES

pytestmark = pytest.mark.db

CONSUMER = "consumer:us-1-a"
HOUSEHOLD = "household:us-1"
GROCERY = "grocery:mock-us"
EXTERNAL = "external:a2a-demo"


def _seed(pg: str) -> uuid.UUID:
    run_id = uuid.uuid4()
    with psycopg.connect(pg) as conn:
        for agent_id, cls, owner, host in (
            (CONSUMER, "consumer", "user:us-1-a", "agents"),
            (HOUSEHOLD, "household", "household:us-1", "agents"),
            (GROCERY, "grocery", "org:mock", "agents"),
            (EXTERNAL, "external", "system", "external"),
        ):
            conn.execute(
                "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
                " VALUES (%s, %s, %s, 'us', %s)",
                (agent_id, cls, owner, host),
            )
        conn.execute(
            "INSERT INTO runs (run_id, kind, trigger, modes) VALUES (%s, 'chat', '{}', '{}')",
            (run_id,),
        )
    return run_id


def _args(run_id: uuid.UUID, **overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "message_id": uuid.uuid4(),
        "run_id": run_id,
        "conversation_id": None,
        "causation_id": None,
        "from_agent": CONSUMER,
        "to_agent": HOUSEHOLD,
        "type": "start_weekly_order",
        "body": SAMPLES["start_weekly_order"],
    }
    return {**args, **overrides}


def _run[T](pg: str, fn: Callable[[psycopg.AsyncConnection], Awaitable[T]]) -> T:
    async def go() -> T:
        async with await psycopg.AsyncConnection.connect(pg) as conn:
            return await fn(conn)

    return asyncio.run(go())


def _send(pg: str, **args: Any) -> bool:
    async def go(conn: psycopg.AsyncConnection) -> bool:
        async with conn.transaction():
            return await outbox.send(conn, **args)

    return _run(pg, go)


def _rows(pg: str, table: str, run_id: uuid.UUID) -> list[dict[str, Any]]:
    with psycopg.connect(pg) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"SELECT * FROM {table} WHERE run_id = %s", (run_id,))
        return cur.fetchall()


def test_send_writes_message_and_trace_in_one_transaction(pg: str) -> None:
    run_id = _seed(pg)
    args = _args(run_id)

    async def rolled_back(conn: psycopg.AsyncConnection) -> None:
        await conn.execute("LISTEN a2a_agents")
        await conn.commit()
        with pytest.raises(RuntimeError):
            async with conn.transaction():
                assert await outbox.send(conn, **args) is True
                raise RuntimeError("caller step fails after send")
        assert [n async for n in conn.notifies(timeout=0.2)] == []

    _run(pg, rolled_back)
    assert _rows(pg, "agent_messages", run_id) == []
    assert _rows(pg, "agent_traces", run_id) == []

    async def committed(conn: psycopg.AsyncConnection) -> list[str]:
        await conn.execute("LISTEN a2a_agents")
        await conn.commit()
        async with conn.transaction():
            assert await outbox.send(conn, **args) is True
        return [n.channel async for n in conn.notifies(timeout=0.5, stop_after=1)]

    assert _run(pg, committed) == ["a2a_agents"]
    [message] = _rows(pg, "agent_messages", run_id)
    assert message["to_host"] == "agents"
    assert message["status"] == "pending"
    assert message["processed_at"] is None
    assert message["body"] == SAMPLES["start_weekly_order"]
    [trace] = _rows(pg, "agent_traces", run_id)
    assert trace["kind"] == "message"
    assert trace["agent_id"] == CONSUMER
    assert trace["on_behalf_of"] == "user:us-1-a"


def test_duplicate_message_id_inserted_once(pg: str) -> None:
    run_id = _seed(pg)
    args = _args(run_id)
    assert _send(pg, **args) is True
    assert _send(pg, **{**args, "body": {"kind": "dinner"}}) is False
    [message] = _rows(pg, "agent_messages", run_id)
    assert message["body"] == SAMPLES["start_weekly_order"]
    assert len(_rows(pg, "agent_traces", run_id)) == 1


def test_replay_same_id_inserts_once(pg: str) -> None:
    run_id = _seed(pg)
    cause = uuid.uuid4()
    replays = [_args(run_id, message_id=uuid.uuid5(cause, str(n))) for n in (0, 1)]

    first = [_send(pg, **args) for args in replays]
    second = [_send(pg, **args) for args in replays]

    assert first == [True, True]
    assert second == [False, False]
    assert len(_rows(pg, "agent_messages", run_id)) == 2
    assert len(_rows(pg, "agent_traces", run_id)) == 2


def test_external_host_row_inserted_processed(pg: str) -> None:
    run_id = _seed(pg)
    args = _args(run_id, from_agent=GROCERY, to_agent=EXTERNAL, type="offer", body=SAMPLES["offer"])

    async def go(conn: psycopg.AsyncConnection) -> list[str]:
        await conn.execute("LISTEN a2a_external")
        await conn.commit()
        async with conn.transaction():
            assert await outbox.send(conn, **args) is True
        return [n.channel async for n in conn.notifies(timeout=0.2)]

    assert _run(pg, go) == []
    [message] = _rows(pg, "agent_messages", run_id)
    assert message["to_host"] == "external"
    assert message["status"] == "processed"
    assert message["processed_at"] is not None
    [trace] = _rows(pg, "agent_traces", run_id)
    assert trace["on_behalf_of"] == "org:mock"


def test_trace_has_summary_and_causation_id(pg: str) -> None:
    run_id = _seed(pg)
    cause = uuid.uuid4()
    args = _args(run_id, causation_id=cause, trace_extra={"node": "start"})
    assert _send(pg, **args) is True
    [trace] = _rows(pg, "agent_traces", run_id)
    payload = trace["payload"]
    assert payload["summary"] == "Start weekly order"
    assert payload["causation_id"] == str(cause)
    assert payload["from"] == CONSUMER
    assert payload["to"] == HOUSEHOLD
    assert payload["type"] == "start_weekly_order"
    assert payload["body"] == SAMPLES["start_weekly_order"]
    assert payload["node"] == "start"
    json.dumps(payload)


def test_trace_message_id_column_set(pg: str) -> None:
    run_id = _seed(pg)
    conversation_id = uuid.uuid4()
    args = _args(run_id, conversation_id=conversation_id)
    assert _send(pg, **args) is True
    [trace] = _rows(pg, "agent_traces", run_id)
    assert trace["message_id"] == args["message_id"]
    assert trace["conversation_id"] == conversation_id
    [message] = _rows(pg, "agent_messages", run_id)
    assert message["conversation_id"] == conversation_id


@pytest.mark.parametrize(
    ("type", "from_agent", "body"),
    [
        ("start_weekly_order", CONSUMER, {"kind": "not-a-kind"}),
        ("start_weekly_order", GROCERY, SAMPLES["start_weekly_order"]),
        ("no_such_type", CONSUMER, {}),
    ],
    ids=["invalid_body", "wrong_sender_class", "unknown_type"],
)
def test_invalid_body_raises_outbound_invalid(
    pg: str, type: str, from_agent: str, body: dict[str, Any]
) -> None:
    run_id = _seed(pg)
    args = _args(run_id, type=type, from_agent=from_agent, body=body)
    with pytest.raises(OutboundInvalid):
        _send(pg, **args)
    assert _rows(pg, "agent_messages", run_id) == []
    assert _rows(pg, "agent_traces", run_id) == []
