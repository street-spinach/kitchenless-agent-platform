"""`complete_join` (experience.md §9, EX-I31)."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from services.common import db
from services.identity import fga
from services.mcp.household import server as household
from services.mcp.meta import CallContext
from services.safety.guard import GuardRefused
from tests.unit import identity_world as w

pytestmark = pytest.mark.db

HOUSEHOLD = "h-1"
OTHER = "h-2"
CODE = "K7Q2M3XD"
PHONE = "+15550100302"
OTHER_PHONE = "+15550100303"
PENDING = {"allergies": ["peanuts"], "diets": ["vegetarian"]}


@pytest.fixture(autouse=True)
def _auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fga, "check", lambda user, relation, object: (True, "model-1"))


class World:
    def __init__(self, pg: str) -> None:
        with psycopg.connect(pg) as conn:
            self.house = w.add_world(conn, HOUSEHOLD)
            self.joiner = w.add_person(conn, HOUSEHOLD, "member")
            self.stranger = w.add_person(conn, HOUSEHOLD, "member")
            self.outsider = w.add_person(conn, OTHER)
            for person, phone in (
                (self.joiner, PHONE),
                (self.stranger, OTHER_PHONE),
                (self.outsider, "+15550100399"),
            ):
                conn.execute(
                    "INSERT INTO channel_identities (phone_e164, user_id, channel_mode)"
                    " VALUES (%s, %s, 'real')",
                    (phone, person.user_id),
                )
            conn.execute(
                "INSERT INTO agent_clients (agent_id, client_id, kind) VALUES (%s, %s, 'service')",
                (household.API_AGENT, w.API_CLIENT),
            )
            self.run = w.add_run(conn, HOUSEHOLD)
            self.other_run = w.add_run(conn, OTHER)

    def add_code(self, pg: str, *, days: int = 7, household_id: str = HOUSEHOLD) -> None:
        with psycopg.connect(pg) as conn:
            conn.execute(
                "INSERT INTO join_codes (code, household_id, phone_e164, display_name, pending,"
                " created_by, expires_at)"
                " VALUES (%s, %s, %s, 'Sam', %s, %s, now() + make_interval(days => %s))",
                (CODE, household_id, PHONE, Jsonb(PENDING), self.house.admin.user_id, days),
            )


@pytest.fixture
def world(pg: str) -> World:
    return World(pg)


def _api(run_id: uuid.UUID, agent: str = household.API_AGENT) -> CallContext:
    return CallContext(
        run_id=run_id,
        agent_id=agent,
        conversation_id=None,
        attempt=1,
        token=w.service_token(agent),
        env="live",
        modes={},
        on_behalf_of=None,
    )


def _with_pool[T](pg: str, monkeypatch: pytest.MonkeyPatch, fn: Callable[[], Awaitable[T]]) -> T:
    async def go() -> T:
        async with AsyncConnectionPool(pg, min_size=1, max_size=3, open=False) as pool:
            monkeypatch.setattr(db, "get_pool", lambda: pool)
            return await fn()

    return asyncio.run(go())


def _complete(
    pg: str,
    monkeypatch: pytest.MonkeyPatch,
    run_id: uuid.UUID,
    user: uuid.UUID,
    code: str = CODE,
) -> dict[str, Any]:
    return _with_pool(
        pg, monkeypatch, lambda: household.complete_join(_api(run_id), code=code, user_id=user)
    )


def _query(pg: str, sql: str, *params: Any) -> list[dict[str, Any]]:
    with psycopg.connect(pg) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _constraints(pg: str, user: uuid.UUID) -> list[tuple[str, str, str]]:
    rows = _query(
        pg,
        "SELECT kind, value, source FROM member_constraints WHERE user_id = %s ORDER BY kind",
        user,
    )
    return [(r["kind"], r["value"], r["source"]) for r in rows]


def _code_row(pg: str) -> dict[str, Any]:
    [row] = _query(pg, "SELECT used_at, used_by FROM join_codes WHERE code = %s", CODE)
    return row


def _assert_refused(
    pg: str,
    monkeypatch: pytest.MonkeyPatch,
    run_id: uuid.UUID,
    user: uuid.UUID,
    reason: str = household.JOIN_CODE_INVALID,
) -> None:
    with pytest.raises(GuardRefused) as e:
        _complete(pg, monkeypatch, run_id, user)
    assert e.value.reason == reason


def test_complete_join_moves_codes(world: World, pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    world.add_code(pg)

    result = _complete(pg, monkeypatch, world.run, world.joiner.user_id)

    assert result == {"outcome": "joined"}
    assert _constraints(pg, world.joiner.user_id) == [
        ("allergy", "peanuts", "join"),
        ("diet", "vegetarian", "join"),
    ]
    row = _code_row(pg)
    assert row["used_at"] is not None and row["used_by"] == world.joiner.user_id


def test_complete_join_repeat_writes_nothing(
    world: World, pg: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.add_code(pg)
    _complete(pg, monkeypatch, world.run, world.joiner.user_id)
    used_at = _code_row(pg)["used_at"]

    result = _complete(pg, monkeypatch, world.run, world.joiner.user_id)

    assert result == {"outcome": "already_joined"}
    assert len(_constraints(pg, world.joiner.user_id)) == 2
    assert _code_row(pg)["used_at"] == used_at


def test_wrong_phone_refused(world: World, pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    world.add_code(pg)

    _assert_refused(pg, monkeypatch, world.run, world.stranger.user_id)

    assert _constraints(pg, world.stranger.user_id) == []
    assert _code_row(pg)["used_at"] is None


def test_expired_code_refused(world: World, pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    world.add_code(pg, days=-1)

    _assert_refused(pg, monkeypatch, world.run, world.joiner.user_id)

    assert _constraints(pg, world.joiner.user_id) == []
    assert _code_row(pg)["used_at"] is None


def test_used_code_refused_for_another_user(
    world: World, pg: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.add_code(pg)
    _complete(pg, monkeypatch, world.run, world.joiner.user_id)

    _assert_refused(pg, monkeypatch, world.run, world.stranger.user_id)

    assert _code_row(pg)["used_by"] == world.joiner.user_id


def test_another_households_code_refused(
    world: World, pg: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.add_code(pg)

    _assert_refused(pg, monkeypatch, world.other_run, world.joiner.user_id)

    assert _code_row(pg)["used_at"] is None


def test_only_system_api_may_complete(
    world: World, pg: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.add_code(pg)
    agent = "grocery:mock-us"
    with psycopg.connect(pg) as conn:
        w.add_agent(conn, agent, "org:mock-grocery-us")

    with pytest.raises(GuardRefused):
        _with_pool(
            pg,
            monkeypatch,
            lambda: household.complete_join(
                _api(world.run, agent), code=CODE, user_id=world.joiner.user_id
            ),
        )

    assert _code_row(pg)["used_at"] is None
