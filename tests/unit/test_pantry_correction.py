"""`record_pantry_check`, the `/me/pantry` correction (experience.md §8, EX-I39, EX-CUJ 7)."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from services.common import db, pantry
from services.mcp.household import server as household
from services.mcp.meta import CallContext
from services.safety.guard import GuardRefused
from tests.unit import identity_world as w

pytestmark = pytest.mark.db

HOUSEHOLD = "h-1"


class World:
    def __init__(self, pg: str) -> None:
        with psycopg.connect(pg) as conn:
            w.add_household(conn, HOUSEHOLD, env="eval")
            self.admin = w.add_person(conn, HOUSEHOLD, "admin")
            self.member = w.add_person(conn, HOUSEHOLD, "member")
            self.child = w.add_person(conn, HOUSEHOLD, "child")
            self.outsider = w.add_person(conn, "h-2", "admin")
            conn.execute(
                "INSERT INTO agent_clients (agent_id, client_id, kind) VALUES (%s, %s, 'service')",
                (household.API_AGENT, w.API_CLIENT),
            )
            self.run = w.add_run(conn, HOUSEHOLD)


@pytest.fixture
def world(pg: str) -> World:
    return World(pg)


def _ctx(run_id: uuid.UUID, env: str = "eval", agent: str = household.API_AGENT) -> CallContext:
    return CallContext(
        run_id=run_id,
        agent_id=agent,
        conversation_id=None,
        attempt=1,
        token=w.service_token(household.API_AGENT),
        env=env,
        modes={},
        on_behalf_of=None,
        household_id=HOUSEHOLD,
    )


def _with_pool[T](pg: str, monkeypatch: pytest.MonkeyPatch, fn: Callable[[], Awaitable[T]]) -> T:
    async def go() -> T:
        async with AsyncConnectionPool(pg, min_size=1, max_size=3, open=False) as pool:
            monkeypatch.setattr(db, "get_pool", lambda: pool)
            return await fn()

    return asyncio.run(go())


def _check(
    pg: str,
    monkeypatch: pytest.MonkeyPatch,
    ctx: CallContext,
    person: w.Person,
    item: str = "rice",
    kind: str = "out",
) -> dict[str, Any]:
    return _with_pool(
        pg,
        monkeypatch,
        lambda: household.record_pantry_check(
            ctx, pantry_item=item, kind=kind, user_id=person.user_id
        ),
    )


def _events(pg: str) -> list[dict[str, Any]]:
    with psycopg.connect(pg) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT household_id, item, pantry_item, unit, delta, kind, source, order_id, run_id,"
            " actor_user_id FROM pantry_events ORDER BY event_id"
        )
        return cur.fetchall()


def _refusals(pg: str) -> list[str]:
    with psycopg.connect(pg) as conn:
        rows = conn.execute(
            "SELECT payload->>'reason' FROM agent_traces WHERE kind = 'tool_result'"
            " AND payload->>'tool' = 'record_pantry_check'"
            " AND (payload->>'refused')::boolean ORDER BY seq"
        ).fetchall()
    return [row[0] for row in rows]


def test_ran_out_writes_out(world: World, pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    out = _check(pg, monkeypatch, _ctx(world.run), world.member)
    assert out["pantry_item"] == "rice" and out["kind"] == "out"
    still = _check(pg, monkeypatch, _ctx(world.run), world.admin, "eggs", "verify")
    assert still["kind"] == "verify"
    assert _events(pg) == [
        {
            "household_id": HOUSEHOLD,
            "item": name,
            "pantry_item": name,
            "unit": "each",
            "delta": 0,
            "kind": kind,
            "source": "me_page",
            "order_id": None,
            "run_id": world.run,
            "actor_user_id": person.user_id,
        }
        for name, kind, person in (("rice", "out", world.member), ("eggs", "verify", world.admin))
    ]


def test_child_cannot_correct(world: World, pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(GuardRefused) as refused:
        _check(pg, monkeypatch, _ctx(world.run), world.child)
    assert refused.value.reason == household.NOT_ADULT
    # Someone else's admin is no adult of this household.
    with pytest.raises(GuardRefused) as refused:
        _check(pg, monkeypatch, _ctx(world.run), world.outsider)
    assert refused.value.reason == household.AUTHZ_DENIED
    assert _events(pg) == []
    assert _refusals(pg) == [household.NOT_ADULT, household.AUTHZ_DENIED]


@pytest.mark.parametrize("item", ["caviar", "white rice", "Rice", ""])
def test_unknown_item_refused(
    world: World, pg: str, monkeypatch: pytest.MonkeyPatch, item: str
) -> None:
    """Only a vocabulary name, as written: an alias or another spelling is refused."""
    with pytest.raises(GuardRefused) as refused:
        _check(pg, monkeypatch, _ctx(world.run), world.admin, item)
    assert refused.value.reason == household.UNKNOWN_PANTRY_ITEM
    assert _events(pg) == []


def test_add_kind_refused(world: World, pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(GuardRefused) as refused:
        _check(pg, monkeypatch, _ctx(world.run), world.admin, kind="add")
    assert refused.value.reason == household.INVALID_KIND
    assert _events(pg) == []


def test_pantry_off_refused(world: World, pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate (PT-OD4): a household whose pantry is off gets no correction."""
    with pytest.raises(GuardRefused) as refused:
        _check(pg, monkeypatch, _ctx(world.run, env="live"), world.admin)
    assert refused.value.reason == household.PANTRY_OFF
    assert _events(pg) == []


def test_only_api_may_call(world: World, pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(GuardRefused):
        _check(pg, monkeypatch, _ctx(world.run, agent=f"household:{HOUSEHOLD}"), world.admin)
    assert _events(pg) == []


def test_next_order_keeps_out_item(world: World, pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """EX-CUJ 7: rice is trusted, so the weekly order skips it; after "Ran out" it is kept
    with reason `out`, and `get_pantry`'s `items` do not move."""
    vocab = pantry.load_vocabulary()
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO pantry_events (household_id, item, pantry_item, kind, delta, unit, source)"
            " VALUES (%s, 'rice', 'rice', 'add', 1, 'each', 'test')",
            (HOUSEHOLD,),
        )

    def get_pantry() -> dict[str, Any]:
        return _with_pool(pg, monkeypatch, lambda: household.get_pantry(_ctx(world.run)))

    before = get_pantry()
    assert pantry.decide(vocab, before["estimates"], "rice")[0] == "pantry_skipped"
    _check(pg, monkeypatch, _ctx(world.run), world.admin)
    after = get_pantry()
    assert pantry.decide(vocab, after["estimates"], "rice")[:2] == ("pantry_kept", "out")
    assert after["items"] == before["items"]
    # "Still have it" trusts it again.
    _check(pg, monkeypatch, _ctx(world.run), world.admin, kind="verify")
    assert pantry.decide(vocab, get_pantry()["estimates"], "rice")[0] == "pantry_skipped"
