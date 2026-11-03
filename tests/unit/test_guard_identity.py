"""The guard's identity steps (identity-authz §8 steps 1, 1b and 2, §14; I20)."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from evals import checks
from services.common import db
from services.identity import claims as c
from services.identity import fga, tokens
from services.identity.claims import Claims
from services.identity.errors import TokenRefused
from services.mcp.household import server as household
from services.mcp.meta import CallContext
from services.safety.guard import GuardRefused
from tests.unit import identity_world as w

HOUSEHOLD = "h-1"
HOUSEHOLD_AGENT = f"household:{HOUSEHOLD}"
SELLER = "grocery:mock-us"

# --- step 1b: the §8 table (no database) ------------------------------------------------

CLASS_AGENT = {
    "consumer": "consumer:h-1-a",
    "household": HOUSEHOLD_AGENT,
    "grocery": SELLER,
    "restaurant": "restaurant:mock-us",
    "delivery": "delivery:mock-us",
    "system": "system:api",
}


def _claims(agent_id: str, kind: str, scopes: frozenset[str] = frozenset()) -> Claims:
    people = kind == "people"
    return Claims(
        uuid.uuid4() if people else None,
        agent_id,
        kind,  # type: ignore[arg-type]
        scopes,
        uuid.uuid4() if people else None,
        "mock",
    )


def _refused(tool: str, claims: Claims) -> bool:
    try:
        tokens.check_tool_scope(tool, claims)
    except TokenRefused as e:
        assert e.reason == c.SCOPE_MISSING
        return True
    return False


def test_tool_scope_table() -> None:
    """Every row of §8 step 1b, allowed and refused (with the agents amendment)."""
    all_scopes = frozenset(
        s for row in tokens.TOOL_SCOPES.values() if row.scope for s in [row.scope]
    )
    for tool, row in tokens.TOOL_SCOPES.items():
        kind = "people" if row.caller == "delegated" else "service"
        other_kind = "service" if kind == "people" else "people"
        scopes = frozenset({row.scope}) if row.scope else frozenset()
        for cls, agent_id in CLASS_AGENT.items():
            allowed = cls in row.agent_classes
            # The right caller kind and scope: allowed exactly for the listed classes.
            assert _refused(tool, _claims(agent_id, kind, scopes)) is not allowed, (tool, cls)
            # The wrong caller kind is always refused, whatever the scopes.
            assert _refused(tool, _claims(agent_id, other_kind, all_scopes)), (tool, cls)
        if row.scope:
            for cls in row.agent_classes:
                # A delegated token without the scope (but with every other) is refused.
                assert _refused(tool, _claims(CLASS_AGENT[cls], kind, all_scopes - {row.scope}))
    assert _refused("no_such_tool", _claims(HOUSEHOLD_AGENT, "people", all_scopes))


def test_system_rows_admit_only_svc_api() -> None:
    assert not _refused("decide_approval", _claims("system:api", "service"))
    assert _refused("decide_approval", _claims("system:runs", "service"))


def test_scope_table_matches_design() -> None:
    expect = {
        "get_constraints": ("delegated", {"consumer", "household"}, "constraints:read"),
        "create_order": ("delegated", {"household"}, "orders:place"),
        "authorize_spend": ("delegated", {"household"}, "orders:place"),
        "authorize_amendment": ("delegated", {"household"}, "orders:place"),
        "request_approval": ("delegated", {"household"}, "approvals:request"),
        "record_delivery": ("delegated", {"household"}, "pantry:write"),
        "place_order": ("service", {"grocery", "restaurant"}, None),
        "amend_order": ("service", {"grocery", "restaurant"}, None),
        "book_delivery": ("service", {"delivery"}, None),
        "decide_approval": ("service", {"system"}, None),
        "choose_shortfall_option": ("service", {"system"}, None),
        "get_household_allergens": ("delegated", {"household"}, "constraints:read"),
        "record_dislike": ("delegated", {"consumer"}, "constraints:write"),
        "get_household_rules": ("delegated", {"household"}, None),
        "advance_turn": ("delegated", {"household"}, "orders:place"),
        "set_take_turns": ("service", {"system"}, None),
        "get_setup_state": ("delegated", {"consumer"}, None),
        "propose_setup": ("delegated", {"consumer"}, "constraints:write"),
    }
    actual = {
        tool: (row.caller, set(row.agent_classes), row.scope)
        for tool, row in tokens.TOOL_SCOPES.items()
    }
    assert actual == expect


# --- the household form of get_constraints (db) -------------------------------------------


@pytest.fixture
def fga_checks(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    seen: list[tuple[str, str, str]] = []

    def check(user: str, relation: str, object: str) -> tuple[bool, str]:
        seen.append((user, relation, object))
        return True, "model-1"

    monkeypatch.setattr(fga, "check", check)
    return seen


class World:
    def __init__(self, pg: str) -> None:
        with psycopg.connect(pg) as conn:
            self.house = w.add_world(conn, HOUSEHOLD)
            self.member = w.add_person(conn, HOUSEHOLD, "member")
            self.child = w.add_person(conn, HOUSEHOLD, "child")
            self.outsider = w.add_person(conn, "h-2")
            # The child's consumer agent exists but has no grant (I-CUJ 10, 11).
            w.add_agent(conn, "consumer:h-1-c", f"user:{self.child.user_id}")
            w.add_agent(conn, SELLER, "org:mock-grocery-us")
            for user, kind, value in (
                (self.house.admin.user_id, "allergy", "tree_nuts"),
                (self.member.user_id, "diet", "vegetarian"),
                (self.member.user_id, "dislike", "mushrooms"),
                (self.child.user_id, "allergy", "peanuts"),
                (self.child.user_id, "dislike", "broccoli"),
                (self.outsider.user_id, "allergy", "crustaceans"),
            ):
                conn.execute(
                    "INSERT INTO member_constraints (user_id, kind, value, source)"
                    " VALUES (%s, %s, %s, 'seed')",
                    (user, kind, value),
                )
            self.run_id = w.add_run(conn, HOUSEHOLD)

    def ctx(self, agent_id: str, token: str) -> CallContext:
        return CallContext(
            run_id=self.run_id,
            agent_id=agent_id,
            conversation_id=None,
            attempt=1,
            token=token,
            env="live",
            modes={"token_source": "mock"},
            on_behalf_of=agent_id,
            household_id=HOUSEHOLD,
        )

    def household_ctx(self) -> CallContext:
        token = w.people_token(HOUSEHOLD_AGENT, self.house.admin.keycloak_sub)
        return self.ctx(HOUSEHOLD_AGENT, token)


def _with_pool[T](pg: str, monkeypatch: pytest.MonkeyPatch, fn: Callable[[], Awaitable[T]]) -> T:
    async def go() -> T:
        async with AsyncConnectionPool(pg, min_size=1, max_size=3, open=False) as pool:
            monkeypatch.setattr(db, "get_pool", lambda: pool)
            return await fn()

    return asyncio.run(go())


def _constraints(
    pg: str, monkeypatch: pytest.MonkeyPatch, ctx: CallContext, user_id: uuid.UUID
) -> dict[str, Any]:
    return _with_pool(pg, monkeypatch, lambda: household.get_constraints(ctx, user_id=user_id))


def _traces(pg: str, run_id: uuid.UUID) -> list[dict[str, Any]]:
    with psycopg.connect(pg) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT seq, kind, agent_id, payload FROM agent_traces WHERE run_id = %s ORDER BY seq",
            (run_id,),
        )
        return cur.fetchall()


@pytest.mark.db
def test_household_form_returns_all_members_allergies(
    pg: str, monkeypatch: pytest.MonkeyPatch, fga_checks: list
) -> None:
    world = World(pg)
    ctx = world.household_ctx()
    result = _constraints(pg, monkeypatch, ctx, world.house.admin.user_id)
    by_user = {m["user_id"]: m for m in result["members"]}
    assert result["household_id"] == HOUSEHOLD
    assert by_user == {
        str(world.house.admin.user_id): {
            "user_id": str(world.house.admin.user_id),
            "allergies": ["tree_nuts"],
            "diets": [],
        },
        str(world.member.user_id): {
            "user_id": str(world.member.user_id),
            "allergies": [],
            "diets": ["vegetarian"],
        },
        str(world.child.user_id): {
            "user_id": str(world.child.user_id),
            "allergies": ["peanuts"],
            "diets": [],
        },
    }
    assert fga_checks == [("agent:household.h-1", "acts_for", "household:h-1")]
    assert ctx.authz_form == household.CONSTRAINTS_FORM_HOUSEHOLD


@pytest.mark.db
def test_household_form_hides_dislikes(
    pg: str, monkeypatch: pytest.MonkeyPatch, fga_checks: list
) -> None:
    world = World(pg)
    result = _constraints(pg, monkeypatch, world.household_ctx(), world.member.user_id)
    assert "dislikes" not in result
    assert all(set(m) == {"user_id", "allergies", "diets"} for m in result["members"])
    assert "mushrooms" not in str(result) and "broccoli" not in str(result)


@pytest.mark.db
def test_household_agent_reads_child_allergies_without_consumer_grant(
    pg: str, monkeypatch: pytest.MonkeyPatch, fga_checks: list
) -> None:
    world = World(pg)
    result = _constraints(pg, monkeypatch, world.household_ctx(), world.child.user_id)
    [child] = [m for m in result["members"] if m["user_id"] == str(world.child.user_id)]
    assert child["allergies"] == ["peanuts"]
    with psycopg.connect(pg) as conn:
        grants = conn.execute(
            "SELECT count(*) FROM agent_grants WHERE agent_id = 'consumer:h-1-c'"
        ).fetchone()
    assert grants == (0,)


@pytest.mark.db
def test_household_form_refuses_a_non_member(
    pg: str, monkeypatch: pytest.MonkeyPatch, fga_checks: list
) -> None:
    world = World(pg)
    with pytest.raises(GuardRefused) as e:
        _constraints(pg, monkeypatch, world.household_ctx(), world.outsider.user_id)
    assert e.value.reason == "authz_denied"


@pytest.mark.db
def test_consumer_form_unchanged(
    pg: str, monkeypatch: pytest.MonkeyPatch, fga_checks: list
) -> None:
    world = World(pg)
    agent = world.house.consumer_agent
    token = w.people_token(agent, world.house.admin.keycloak_sub, w.CONSUMER_SCOPES)
    ctx = world.ctx(agent, token)
    result = _constraints(pg, monkeypatch, ctx, world.house.admin.user_id)
    assert result["allergies"] == ["tree_nuts"]
    assert fga_checks == [("agent:consumer.h-1-a", "acts_for", f"user:{world.house.admin.user_id}")]
    assert ctx.authz_form == household.CONSTRAINTS_FORM_PERSON


@pytest.mark.db
def test_business_agent_cannot_call_household_tools(
    pg: str, monkeypatch: pytest.MonkeyPatch, fga_checks: list
) -> None:
    world = World(pg)
    ctx = world.ctx(SELLER, w.service_token(SELLER))
    with pytest.raises(GuardRefused) as e:
        _constraints(pg, monkeypatch, ctx, world.house.admin.user_id)
    assert e.value.reason == c.SCOPE_MISSING
    with pytest.raises(GuardRefused) as e:
        _with_pool(
            pg,
            monkeypatch,
            lambda: household.create_order(ctx, order_id=uuid.uuid4(), kind="grocery"),
        )
    assert e.value.reason == c.SCOPE_MISSING
    assert fga_checks == []
    # The refusal rows name the seller as the acting agent, with no person or grant.
    refused = [r["payload"] for r in _traces(pg, world.run_id) if r["kind"] == "tool_result"]
    assert [(p["refused"], p["act_agent_id"], p["grant_id"]) for p in refused] == [
        (True, SELLER, None),
        (True, SELLER, None),
    ]


@pytest.mark.db
def test_guarded_call_trace_has_identity_fields(
    pg: str, monkeypatch: pytest.MonkeyPatch, fga_checks: list
) -> None:
    world = World(pg)
    _constraints(pg, monkeypatch, world.household_ctx(), world.house.admin.user_id)
    rows = _traces(pg, world.run_id)
    guarded = [r for r in rows if r["kind"] in ("tool_call", "tool_result")]
    assert [r["kind"] for r in guarded] == ["tool_call", "tool_result"]
    for row in guarded:
        p = row["payload"]
        assert p["sub_user_id"] == str(world.house.admin.user_id)
        assert p["act_agent_id"] == HOUSEHOLD_AGENT
        assert p["grant_id"] == str(world.house.grants[HOUSEHOLD_AGENT])
        assert p["token_source"] == "mock"
        assert p["authz"] == {
            "relation": "acts_for",
            "object": "household:h-1",
            "allowed": True,
            "model_id": "model-1",
        }
    # The eval check reads the same rows (tools-guarded-calls-have-identity).
    trace = [{**r, "kind": r["kind"]} for r in rows]
    assert checks.guarded_calls_have_identity(trace, {})[0]
    # No token in the trace (safety-no-tokens-persisted).
    assert checks.no_jwt_in_traces_or_checkpoints(
        trace, {}, db={"checkpoints": [], "agent_messages": []}
    )[0]
