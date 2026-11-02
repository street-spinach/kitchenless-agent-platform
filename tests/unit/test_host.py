"""The agent host: dispatch by `to_agent` and the `/health` backlog (issue #39).

Architecture item 27 (amended by agent-protocol A20); agent-protocol P18 (health part).
"""

import asyncio
import uuid
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from pydantic import BaseModel
from starlette.testclient import TestClient

from services.agents import host
from services.common import directory, runs
from services.common.consumer import Inbound, StepResult
from services.common.directory import AgentCard
from services.common.protocol.a2a import AgentMessageRow


class _Body(BaseModel):
    pass


GROCERY = "grocery:mock-us"
RESTAURANT = "restaurant:mock-us"
DELIVERY = "delivery:mock-us"


def _card(agent_id: str, cls: str) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        agent_class=cls,
        owner_ref="org:mock",
        region="us",
        zone_ids=("us-oh-45202",),
        currency="USD",
        host_process="agents",
        vendor=None,
    )


def _inbound(to_agent: str) -> Inbound:
    row = AgentMessageRow(
        message_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        conversation_id=None,
        causation_id=None,
        from_agent="household:us-1",
        to_agent=to_agent,
        type="quote_request",
        body={},
    )
    return Inbound(row=row, attempts=1, body=_Body())


def test_dispatch_routes_by_to_agent() -> None:
    calls: list[tuple[str, str, str]] = []

    def handler(name: str) -> host.ClassHandler:
        async def handle(msg: Inbound, card: AgentCard) -> StepResult:
            calls.append((name, msg.row.to_agent, card.agent_id))
            return StepResult()

        return handle

    dispatch = host.Dispatcher(
        [_card(GROCERY, "grocery"), _card(RESTAURANT, "restaurant"), _card(DELIVERY, "delivery")],
        {"grocery": handler("grocery"), "restaurant": handler("restaurant")},
    )

    async def go() -> None:
        assert await dispatch(_inbound(RESTAURANT)) == StepResult()
        await dispatch(_inbound(GROCERY))
        # A loaded card whose class has no handler, and a card this host did not load,
        # both fail the step (retried, then poisoned) rather than being dropped.
        for to_agent in (DELIVERY, "grocery:not-loaded"):
            with pytest.raises(host.NoHandler):
                await dispatch(_inbound(to_agent))

    asyncio.run(go())
    assert calls == [
        ("restaurant", RESTAURANT, RESTAURANT),
        ("grocery", GROCERY, GROCERY),
    ]


def test_serve_passes_runs_finish_to_consumer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The final-attempt wrapper: the consumer's poison rule calls `runs.finish(error)`."""
    seen: dict[str, Any] = {}

    class FakePool:
        async def open(self) -> None: ...

        async def close(self) -> None:
            seen["closed"] = True

        def connection(self) -> Any:
            class Ctx:
                async def __aenter__(self) -> None:
                    return None

                async def __aexit__(self, *exc: object) -> None:
                    return None

            return Ctx()

    async def hosted(conn: Any, host_process: str) -> list[AgentCard]:
        seen["host_process"] = host_process
        return [_card(GROCERY, "grocery")]

    async def consume(hostname: str, handle: Any, **kw: Any) -> None:
        seen.update(consume_host=hostname, handle=handle, **kw)
        await kw["stop"].wait()

    async def sweep(**kw: Any) -> None:
        await kw["stop"].wait()

    class FakeServer:
        def __init__(self, config: Any) -> None:
            self.should_exit = False

        async def serve(self) -> None:
            await asyncio.sleep(0)  # let the loops start
            seen["stop"].set()  # as SIGTERM would
            while not self.should_exit:
                await asyncio.sleep(0)

    monkeypatch.setattr(host.db, "get_pool", lambda: FakePool())
    monkeypatch.setattr(directory, "hosted", hosted)
    monkeypatch.setattr(host, "consume_forever", consume)
    monkeypatch.setattr(host.sweeper, "sweep_forever", sweep)
    monkeypatch.setattr(host.uvicorn, "Server", FakeServer)

    asyncio.run(host.serve({}))
    assert seen["host_process"] == "agents"
    assert seen["consume_host"] == "agents"
    assert seen["finish_run"] is runs.finish
    assert isinstance(seen["handle"], host.Dispatcher)
    assert set(seen["handle"].cards) == {GROCERY}
    assert seen["closed"] is True


def test_health_reports_card_count_and_backlog_unreadable() -> None:
    async def check_db() -> bool:
        return False

    async def no_backlog() -> None:
        return None

    app = host.create_app(check_db, get_backlog=no_backlog, card_count=lambda: 3)
    assert TestClient(app).get("/health").json() == {
        "status": "ok",
        "service": "agents",
        "postgres": False,
        "cards": 3,
        "backlog": None,
    }


@pytest.mark.db
def test_health_reports_backlog(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = uuid.uuid4()
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, household_id, kind, trigger, modes)"
            " VALUES (%s, NULL, 'a2a_demo', '{}', '{}')",
            (run_id,),
        )
        for to_host, status, age in (
            ("agents", "pending", 30),
            ("agents", "pending", 5),
            ("agents", "processing", 1),
            ("agents", "processed", 600),
            ("agents", "failed", 600),
            ("external", "pending", 900),
        ):
            conn.execute(
                "INSERT INTO agent_messages (message_id, run_id, from_agent, to_agent, to_host,"
                " type, body, status, created_at)"
                " VALUES (%s, %s, 'household:us-1', 'grocery:mock-us', %s, 'quote_request',"
                " %s, %s, now() - make_interval(secs => %s))",
                (uuid.uuid4(), run_id, to_host, Jsonb({}), status, age),
            )

    settings = type("S", (), {"database_url": pg})()
    monkeypatch.setattr(host, "get_settings", lambda: settings)
    body = TestClient(host.create_app(card_count=lambda: 2)).get("/health").json()

    assert body["postgres"] is True
    assert body["cards"] == 2
    backlog = body["backlog"]
    assert set(backlog) == {"agents", "api"}  # `external` is excluded
    assert backlog["api"] == {"pending": 0, "processing": 0, "oldest_pending_seconds": None}
    agents = backlog["agents"]
    assert (agents["pending"], agents["processing"]) == (2, 1)
    assert 30 <= agents["oldest_pending_seconds"] < 60


@pytest.mark.db
def test_hosted_lists_enabled_cards_of_host(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        for agent_id, cls, host_process, enabled in (
            (GROCERY, "grocery", "agents", True),
            (RESTAURANT, "restaurant", "agents", False),
            ("channel:whatsapp", "channel", "api", True),
        ):
            conn.execute(
                "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process,"
                " enabled) VALUES (%s, %s, 'org:mock', 'us', %s, %s)",
                (agent_id, cls, host_process, enabled),
            )

    async def go() -> list[AgentCard]:
        async with await psycopg.AsyncConnection.connect(pg) as conn:
            return await directory.hosted(conn, "agents")

    assert [c.agent_id for c in asyncio.run(go())] == [GROCERY]
