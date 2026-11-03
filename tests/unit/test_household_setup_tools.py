"""The setup tools `get_setup_state` and `propose_setup` (experience.md §9, EX-I26)."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from services.common import db, ids
from services.identity import fga
from services.mcp.household import server as household
from services.mcp.meta import CallContext
from services.safety.guard import GuardRefused
from tests.unit import identity_world as w

pytestmark = pytest.mark.db

HOUSEHOLD = "h-1"
OTHER = "h-2"
MEMBER_AGENT = f"consumer:{HOUSEHOLD}-m"


@pytest.fixture(autouse=True)
def _auth(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    checks: list[tuple[str, str, str]] = []

    def check(user: str, relation: str, object: str) -> tuple[bool, str]:
        checks.append((user, relation, object))
        return True, "model-1"

    monkeypatch.setattr(fga, "check", check)
    return checks


class World:
    def __init__(self, pg: str) -> None:
        with psycopg.connect(pg) as conn:
            self.house = w.add_world(conn, HOUSEHOLD)
            self.admin = self.house.admin
            self.member = w.add_person(conn, HOUSEHOLD, "member")
            w.add_agent(conn, MEMBER_AGENT, f"user:{self.member.user_id}")
            w.add_grant(conn, MEMBER_AGENT, self.member, HOUSEHOLD)
            self.outsider = w.add_person(conn, OTHER)
            for person, name in ((self.admin, "Asha"), (self.member, "Kavi")):
                conn.execute(
                    "UPDATE users SET display_name = %s WHERE user_id = %s",
                    (name, person.user_id),
                )
            for person, phone in (
                (self.member, "+15550100302"),
                (self.outsider, "+15550100399"),
            ):
                conn.execute(
                    "INSERT INTO channel_identities (phone_e164, user_id, channel_mode)"
                    " VALUES (%s, %s, 'sim')",
                    (phone, person.user_id),
                )
            self.run = w.add_run(conn, HOUSEHOLD)

    def new_run(self, pg: str) -> uuid.UUID:
        with psycopg.connect(pg) as conn:
            return w.add_run(conn, HOUSEHOLD)


@pytest.fixture
def world(pg: str) -> World:
    return World(pg)


def _ctx(run_id: uuid.UUID, agent: str, person: w.Person) -> CallContext:
    return CallContext(
        run_id=run_id,
        agent_id=agent,
        conversation_id=None,
        attempt=1,
        token=w.people_token(agent, person.keycloak_sub, w.CONSUMER_SCOPES),
        env="live",
        modes={},
        on_behalf_of=f"user:{person.user_id}",
        household_id=HOUSEHOLD,
    )


def _admin(world: World, run_id: uuid.UUID | None = None) -> CallContext:
    return _ctx(run_id or world.run, world.house.consumer_agent, world.admin)


def _with_pool[T](pg: str, monkeypatch: pytest.MonkeyPatch, fn: Callable[[], Awaitable[T]]) -> T:
    async def go() -> T:
        async with AsyncConnectionPool(pg, min_size=1, max_size=3, open=False) as pool:
            monkeypatch.setattr(db, "get_pool", lambda: pool)
            return await fn()

    return asyncio.run(go())


def _query(pg: str, sql: str, *params: Any) -> list[dict[str, Any]]:
    with psycopg.connect(pg) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _proposals(pg: str) -> list[dict[str, Any]]:
    return _query(
        pg,
        "SELECT proposal_id, household_id, proposed_by, answer, status,"
        " expires_at - created_at AS ttl FROM onboarding_proposals ORDER BY created_at",
    )


def _propose(
    pg: str, monkeypatch: pytest.MonkeyPatch, ctx: CallContext, answer: dict[str, Any]
) -> dict[str, Any]:
    return _with_pool(pg, monkeypatch, lambda: household.propose_setup(ctx, answer=answer))


def _state(pg: str, monkeypatch: pytest.MonkeyPatch, ctx: CallContext) -> dict[str, Any]:
    return _with_pool(pg, monkeypatch, lambda: household.get_setup_state(ctx))


ANSWER = {
    "people": [
        {"name": "Mira", "phone": None, "child": True, "add_allergies": ["peanuts"]},
        {"name": "Ravi", "phone": "+15550100377", "child": False, "add_diets": ["vegan"]},
    ],
    "budget_minor": 15000,
    "unmatched": [],
}


def test_setup_state(
    world: World, pg: str, monkeypatch: pytest.MonkeyPatch, _auth: list[tuple[str, str, str]]
) -> None:
    admin = _admin(world)
    assert _state(pg, monkeypatch, admin) == {
        "role": "admin",
        "has_budget": False,
        "has_proposal": False,
    }
    assert (
        fga.agent_object(world.house.consumer_agent),
        "acts_for",
        f"user:{world.admin.user_id}",
    ) in _auth

    member = _ctx(world.run, MEMBER_AGENT, world.member)
    assert _state(pg, monkeypatch, member)["role"] == "member"

    _propose(pg, monkeypatch, admin, ANSWER)
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO budgets (household_id, period, amount_minor, currency)"
            " VALUES (%s, 'weekly', 15000, 'USD')",
            (HOUSEHOLD,),
        )
    assert _state(pg, monkeypatch, admin) == {
        "role": "admin",
        "has_budget": True,
        "has_proposal": True,
    }

    # Someone outside the run's household, or no person at all, is refused.
    with psycopg.connect(pg) as conn:
        w.add_agent(conn, "consumer:h-2-a", f"user:{world.outsider.user_id}")
        w.add_grant(conn, "consumer:h-2-a", world.outsider, OTHER)
    for ctx in (
        _ctx(world.run, "consumer:h-2-a", world.outsider),
        CallContext(**{**admin.__dict__, "on_behalf_of": f"household:{HOUSEHOLD}"}),
    ):
        with pytest.raises(GuardRefused) as e:
            _state(pg, monkeypatch, ctx)
        assert e.value.reason == household.AUTHZ_DENIED


def test_propose_member_refused(world: World, pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    member = _ctx(world.run, MEMBER_AGENT, world.member)
    with pytest.raises(GuardRefused) as e:
        _propose(pg, monkeypatch, member, ANSWER)
    assert e.value.reason == household.NOT_ADMIN
    assert _proposals(pg) == []


def test_propose_replay_one_row(world: World, pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _propose(pg, monkeypatch, _admin(world), ANSWER)
    again = _propose(pg, monkeypatch, _admin(world), {**ANSWER, "budget_minor": 99})
    assert first == again  # the replay returns what the first call stored
    assert first["proposal_id"] == str(ids.proposal_id(world.run))
    [row] = _proposals(pg)
    assert row["proposal_id"] == ids.proposal_id(world.run)
    assert (row["household_id"], row["proposed_by"], row["status"]) == (
        HOUSEHOLD,
        world.admin.user_id,
        "pending",
    )
    assert row["ttl"].total_seconds() == pytest.approx(3600, abs=5)
    assert row["answer"]["budget_minor"] == 15000
    assert first["answer"]["people"][0]["add_allergies"] == ["peanuts"]
    assert first["answer"]["people"][1]["add_diets"] == ["vegan"]


def test_propose_supersedes_pending(world: World, pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    first_run, second_run = world.run, world.new_run(pg)
    _propose(pg, monkeypatch, _admin(world, first_run), ANSWER)
    _propose(pg, monkeypatch, _admin(world, second_run), {**ANSWER, "budget_minor": 20000})
    status = {r["proposal_id"]: r["status"] for r in _proposals(pg)}
    assert status == {
        ids.proposal_id(first_run): "superseded",
        ids.proposal_id(second_run): "pending",
    }
    # Replaying the first run stores nothing and never supersedes the newer proposal.
    _propose(pg, monkeypatch, _admin(world, first_run), ANSWER)
    assert {r["proposal_id"]: r["status"] for r in _proposals(pg)} == status
    # Only pending ones are superseded: an applied proposal stays applied.
    with psycopg.connect(pg) as conn:
        conn.execute(
            "UPDATE onboarding_proposals SET status = 'applied' WHERE proposal_id = %s",
            (ids.proposal_id(second_run),),
        )
    third_run = world.new_run(pg)
    _propose(pg, monkeypatch, _admin(world, third_run), ANSWER)
    status = {r["proposal_id"]: r["status"] for r in _proposals(pg)}
    assert status[ids.proposal_id(second_run)] == "applied"
    assert status[ids.proposal_id(third_run)] == "pending"


def test_propose_phone_taken(world: World, pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    answer = {
        "people": [
            # In another household: dropped and returned.
            {"name": "Dev", "phone": "+15550100399", "child": False},
            # Already this household's member Kavi (named case-insensitively): kept.
            {
                "name": " kavi ",
                "phone": "+15550100302",
                "child": False,
                "add_allergies": ["sesame"],
            },
            # Unknown: kept.
            {"name": "Ravi", "phone": "+15550100377", "child": False},
        ],
    }
    out = _propose(pg, monkeypatch, _admin(world), answer)
    assert out["phones_taken"] == ["+15550100399"]
    people = {p["name"].strip(): p for p in out["answer"]["people"]}
    assert people["Dev"]["phone"] is None
    assert "user_id" not in people["Dev"]
    assert people["kavi"]["phone"] == "+15550100302"
    assert people["kavi"]["user_id"] == str(world.member.user_id)
    assert people["Ravi"]["phone"] == "+15550100377"
    # The stored answer never holds the dropped phone.
    [row] = _proposals(pg)
    assert "+15550100399" not in str(row["answer"]["people"])


def test_propose_unknown_code_unmatched(
    world: World, pg: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = {
        "people": [
            {
                "name": "Mira",
                "child": True,
                "add_allergies": ["tree_nuts", "pineapple", "tree_nuts"],
                "add_diets": ["vegan", "paleo"],
                "remove_allergies": ["kiwi"],
                "remove_diets": ["keto"],
            }
        ],
        "unmatched": ["the cat"],
    }
    out = _propose(pg, monkeypatch, _admin(world), answer)
    [mira] = out["answer"]["people"]
    assert mira["add_allergies"] == ["tree_nuts"]
    assert mira["add_diets"] == ["vegan"]
    assert mira["remove_allergies"] == []
    assert mira["remove_diets"] == ["keto"]
    assert out["answer"]["unmatched"] == ["the cat", "pineapple", "kiwi", "paleo"]

    # A shape that is not a `SetupAnswer` even then is refused, and nothing is stored.
    for bad in ({"people": "Mira"}, {"people": [{"child": True}]}, {"budget_minor": -1}):
        with pytest.raises(GuardRefused) as e:
            _propose(pg, monkeypatch, _admin(world, world.new_run(pg)), bad)
        assert e.value.reason == household.INVALID_ANSWER
    assert len(_proposals(pg)) == 1
