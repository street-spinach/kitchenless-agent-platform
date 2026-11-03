"""The setup tools `get_setup_state`, `propose_setup` and `apply_setup` (experience.md §9,
EX-I26, EX-I27)."""

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
        "currency": "USD",
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
        "currency": "USD",
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


# --- apply_setup (EX-I27) -------------------------------------------------------------------


def _api(run_id: uuid.UUID) -> CallContext:
    return CallContext(
        run_id=run_id,
        agent_id=household.API_AGENT,
        conversation_id=None,
        attempt=1,
        token=w.service_token(household.API_AGENT),
        env="live",
        modes={},
        on_behalf_of=None,
        household_id=HOUSEHOLD,
    )


@pytest.fixture
def api(world: World, pg: str) -> World:
    """`system:api`'s client row (it has no card, identity-authz §3) and a new child, as
    `create_child` makes her at the tap, after the proposal (so her name is not matched)."""
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO agent_clients (agent_id, client_id, kind) VALUES (%s, %s, 'service')",
            (household.API_AGENT, w.API_CLIENT),
        )
        mira = w.add_person(conn, HOUSEHOLD, "child")
        conn.execute("UPDATE users SET display_name = 'Mira R' WHERE user_id = %s", (mira.user_id,))
    world.mira = mira  # type: ignore[attr-defined]
    return world


def _apply(
    pg: str,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    proposal: str,
    answer: str,
    *,
    removal: int | None = None,
    children: dict[str, uuid.UUID] | None = None,
    user: uuid.UUID | None = None,
) -> dict[str, Any]:
    run = world.new_run(pg)
    kids = {"Mira": world.mira.user_id} if children is None else children  # type: ignore[attr-defined]
    return _with_pool(
        pg,
        monkeypatch,
        lambda: household.apply_setup(
            _api(run),
            proposal_id=uuid.UUID(proposal),
            answer=answer,
            removal=removal,
            child_user_ids=kids,
            user_id=user or world.admin.user_id,
        ),
    )


def _rows(pg: str) -> dict[str, Any]:
    return {
        "constraints": sorted(
            (str(r["user_id"]), r["kind"], r["value"], r["source"])
            for r in _query(pg, "SELECT user_id, kind, value, source FROM member_constraints")
        ),
        "budgets": _query(pg, "SELECT amount_minor, currency FROM budgets"),
        "join_codes": _query(
            pg, "SELECT code, phone_e164, display_name, pending, expires_at FROM join_codes"
        ),
    }


SETUP = {
    "people": [
        {"name": "Asha", "child": False, "add_allergies": ["sesame"]},
        {"name": "Mira", "child": True, "add_allergies": ["peanuts"], "add_diets": ["vegan"]},
        {"name": "Ravi", "phone": "+15550100377", "child": False, "add_allergies": ["milk"]},
        {"name": "Dev", "child": False, "add_diets": ["halal"]},
    ],
    "budget_minor": 15000,
}


def _proposed(pg: str, monkeypatch: pytest.MonkeyPatch, world: World, answer: Any = None) -> str:
    out = _propose(pg, monkeypatch, _admin(world, world.new_run(pg)), answer or SETUP)
    return str(out["proposal_id"])


def test_apply_nothing_saved_without_yes(
    api: World, pg: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = _rows(pg)
    proposal = _proposed(pg, monkeypatch, api)
    assert _rows(pg) == empty  # a proposal alone saves nothing
    out = _apply(pg, monkeypatch, api, proposal, "no")
    assert (out["status"], out["outcome"], out["saved"]) == ("discarded", "done", None)
    assert _rows(pg) == empty
    # "No" again is already decided; "Yes" after "No" is expired. Nothing is written.
    assert _apply(pg, monkeypatch, api, proposal, "no")["outcome"] == "already_decided"
    with pytest.raises(GuardRefused) as e:
        _apply(pg, monkeypatch, api, proposal, "yes")
    assert e.value.reason == household.SETUP_EXPIRED
    # A removal tap before "Yes" is refused.
    other = _proposed(pg, monkeypatch, api)
    with pytest.raises(GuardRefused) as e:
        _apply(pg, monkeypatch, api, other, "keep", removal=0)
    assert e.value.reason == household.INVALID_ANSWER
    assert _rows(pg) == empty


def test_apply_member_refused(api: World, pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    proposal = _proposed(pg, monkeypatch, api)
    for tapper in (api.member.user_id, api.mira.user_id, api.outsider.user_id):  # type: ignore[attr-defined]
        with pytest.raises(GuardRefused) as e:
            _apply(pg, monkeypatch, api, proposal, "yes", user=tapper)
        assert e.value.reason == household.NOT_ADMIN
    # A consumer agent's token may not call it at all.
    with pytest.raises(GuardRefused):
        _with_pool(
            pg,
            monkeypatch,
            lambda: household.apply_setup(
                _admin(api, api.new_run(pg)),
                proposal_id=uuid.UUID(proposal),
                answer="yes",
                removal=None,
                child_user_ids={},
                user_id=api.admin.user_id,
            ),
        )
    # A new child missing from `child_user_ids`, or not a child of the household, refuses.
    for kids in ({}, {"Mira": api.member.user_id}):
        with pytest.raises(GuardRefused) as e:
            _apply(pg, monkeypatch, api, proposal, "yes", children=kids)
        assert e.value.reason == household.INVALID_ANSWER
    assert _rows(pg)["constraints"] == []
    assert _proposals(pg)[0]["status"] == "pending"


def test_apply_expired_refused(api: World, pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    proposal = _proposed(pg, monkeypatch, api)
    with psycopg.connect(pg) as conn:
        conn.execute(
            "UPDATE onboarding_proposals SET expires_at = now() - interval '1 second'"
            " WHERE proposal_id = %s",
            (uuid.UUID(proposal),),
        )
    for answer in ("yes", "no"):
        with pytest.raises(GuardRefused) as e:
            _apply(pg, monkeypatch, api, proposal, answer)
        assert e.value.reason == household.SETUP_EXPIRED
    # Superseded by a newer answer: expired as well.
    older = _proposed(pg, monkeypatch, api)
    _proposed(pg, monkeypatch, api)
    with pytest.raises(GuardRefused) as e:
        _apply(pg, monkeypatch, api, older, "yes")
    assert e.value.reason == household.SETUP_EXPIRED
    assert _rows(pg)["constraints"] == [] and _rows(pg)["budgets"] == []


def test_apply_idempotent(api: World, pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    proposal = _proposed(pg, monkeypatch, api)
    first = _apply(pg, monkeypatch, api, proposal, "yes")
    assert (first["status"], first["outcome"], first["removals_pending"]) == (
        "applied",
        "done",
        [],
    )
    saved = first["saved"]
    mira = str(api.mira.user_id)  # type: ignore[attr-defined]
    admin = str(api.admin.user_id)
    assert sorted((c["user_id"], c["kind"], c["value"]) for c in saved["constraints"]) == sorted(
        [(admin, "allergy", "sesame"), (mira, "allergy", "peanuts"), (mira, "diet", "vegan")]
    )
    [code] = saved["join_codes"]
    assert (code["name"], code["phone"]) == ("Ravi", "+15550100377")
    assert len(code["code"]) == 8 and set(code["code"]) <= set(ids._JOIN_ALPHABET)
    assert saved["not_saved"] == ["Dev"]  # an adult with no phone cannot be invited
    rows = _rows(pg)
    assert {c[3] for c in rows["constraints"]} == {"setup"}
    [join] = rows["join_codes"]
    assert join["code"] == code["code"]
    assert join["pending"] == {"allergies": ["milk"], "diets": []}
    [row] = _proposals(pg)
    assert row["status"] == "applied"
    decided = _query(pg, "SELECT decided_by, decided_at FROM onboarding_proposals")[0]
    assert decided["decided_by"] == api.admin.user_id and decided["decided_at"] is not None

    again = _apply(pg, monkeypatch, api, proposal, "yes")
    assert again["outcome"] == "already_decided"
    assert again["saved"] == saved
    assert _rows(pg) == rows  # nothing written twice
    assert _apply(pg, monkeypatch, api, proposal, "no")["outcome"] == "already_decided"
    assert _proposals(pg)[0]["status"] == "applied"


def test_apply_remove_by_index(api: World, pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    kavi = str(api.member.user_id)
    with psycopg.connect(pg) as conn:
        for kind, value in (("allergy", "sesame"), ("diet", "vegan"), ("allergy", "fish")):
            conn.execute(
                "INSERT INTO member_constraints (user_id, kind, value) VALUES (%s, %s, %s)",
                (api.member.user_id, kind, value),
            )
    answer = {
        "people": [
            {
                "name": "Kavi",
                "child": False,
                "remove_allergies": ["sesame"],
                "remove_diets": ["vegan"],
            },
            {"name": "Asha", "child": False, "add_allergies": ["peanuts"]},
            # Not a member yet: nothing to remove, so not numbered.
            {"name": "Zoe", "child": False, "remove_allergies": ["fish"]},
        ],
    }
    proposal = _proposed(pg, monkeypatch, api, answer)
    stored = _proposals(pg)[0]["answer"]
    assert household.setup_removals(stored) == [
        {"user_id": kavi, "name": "Kavi", "kind": "allergy", "value": "sesame"},
        {"user_id": kavi, "name": "Kavi", "kind": "diet", "value": "vegan"},
    ]

    out = _apply(pg, monkeypatch, api, proposal, "yes", children={})
    assert (out["status"], out["removals_pending"]) == ("awaiting_remove", [0, 1])

    def kavis() -> list[tuple[str, str]]:
        return sorted((c[1], c[2]) for c in _rows(pg)["constraints"] if c[0] == kavi)

    # Nothing is removed by "Yes, save" alone.
    assert kavis() == [("allergy", "fish"), ("allergy", "sesame"), ("diet", "vegan")]
    out = _apply(pg, monkeypatch, api, proposal, "remove", removal=1)
    assert (out["status"], out["outcome"], out["removals_pending"]) == (
        "awaiting_remove",
        "done",
        [0],
    )
    assert kavis() == [("allergy", "fish"), ("allergy", "sesame")]
    # The same removal answered again: already decided, nothing changes.
    again = _apply(pg, monkeypatch, api, proposal, "keep", removal=1)
    assert again["outcome"] == "already_decided"
    for bad in (2, -1):
        with pytest.raises(GuardRefused) as e:
            _apply(pg, monkeypatch, api, proposal, "remove", removal=bad)
        assert e.value.reason == household.INVALID_ANSWER
    out = _apply(pg, monkeypatch, api, proposal, "keep", removal=0)
    assert (out["status"], out["removals_pending"]) == ("applied", [])
    assert kavis() == [("allergy", "fish"), ("allergy", "sesame")]
    assert _apply(pg, monkeypatch, api, proposal, "remove", removal=0)["outcome"] == (
        "already_decided"
    )
    assert kavis() == [("allergy", "fish"), ("allergy", "sesame")]


def test_apply_budget_in_household_currency(
    api: World, pg: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with psycopg.connect(pg) as conn:
        conn.execute("UPDATE households SET currency = 'GBP' WHERE household_id = %s", (HOUSEHOLD,))
    first = _apply(
        pg, monkeypatch, api, _proposed(pg, monkeypatch, api, {"budget_minor": 12000}), "yes"
    )
    assert (first["saved"]["budget_minor"], first["saved"]["budget_set"]) == (12000, True)
    assert _rows(pg)["budgets"] == [{"amount_minor": 12000, "currency": "GBP"}]
    # A later change updates it; the budget was not "just set" this time.
    second = _apply(
        pg, monkeypatch, api, _proposed(pg, monkeypatch, api, {"budget_minor": 9000}), "yes"
    )
    assert (second["saved"]["budget_minor"], second["saved"]["budget_set"]) == (9000, False)
    assert _rows(pg)["budgets"] == [{"amount_minor": 9000, "currency": "GBP"}]
    # No budget in the answer leaves it alone.
    third = _apply(
        pg,
        monkeypatch,
        api,
        _proposed(
            pg,
            monkeypatch,
            api,
            {"people": [{"name": "Asha", "child": False, "add_diets": ["keto"]}]},
        ),
        "yes",
    )
    assert third["saved"]["budget_set"] is False
    assert _rows(pg)["budgets"] == [{"amount_minor": 9000, "currency": "GBP"}]
