"""`TokenSource` and `MockSource` (identity-authz I3, §12)."""

import asyncio
import uuid
from typing import Any

import jwt
import psycopg
import pytest
from psycopg import AsyncConnection

from services.identity import claims as c
from services.identity import grants, token_source
from services.identity.errors import IdentityUnavailable
from tests.unit import identity_world as w

SELLER = "grocery:mock-us"


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _claims(token: str) -> dict[str, Any]:
    return jwt.decode(token, options={"verify_signature": False})


def test_cache_reuses_until_60s_left() -> None:
    clock = Clock()
    source = token_source.MockSource(clock=clock)
    first = asyncio.run(source.service_token(SELLER))
    clock.now += token_source.TOKEN_SECONDS - token_source.REFRESH_BEFORE_SECONDS  # 60 s left
    assert asyncio.run(source.service_token(SELLER)) == first
    clock.now += 1  # 59 s left: a new token
    second = asyncio.run(source.service_token(SELLER))
    assert second != first
    assert _claims(second)["exp"] == int(clock.now) + token_source.TOKEN_SECONDS


def test_new_token_after_refusal_once() -> None:
    clock = Clock()
    source = token_source.MockSource(clock=clock)
    first = asyncio.run(source.service_token(SELLER))
    source.invalidate(None, SELLER)  # what `call_tool_as` does after a token refusal
    second = asyncio.run(source.service_token(SELLER))
    assert second != first
    assert asyncio.run(source.service_token(SELLER)) == second  # cached again


def test_identity_unavailable_on_5xx() -> None:
    """A live source is not built in build step 5 (KeycloakSource lands in step 8), so
    choosing one fails closed as an outage, never by falling back to mock tokens."""
    for name in ("keycloak", "keycloak_azp", "token_service"):
        with pytest.raises(IdentityUnavailable):
            token_source.get_source("live", name)
    assert isinstance(token_source.get_source("eval"), token_source.MockSource)
    assert isinstance(token_source.get_source("live", "mock"), token_source.MockSource)


def test_sample_token_has_no_run(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid.uuid4()
    grant = grants.Grant(
        grant_id=uuid.uuid4(),
        agent_id="household:h-1",
        agent_kind="household",
        user_id=user_id,
        consented_by=user_id,
        household_id="h-1",
        scopes=grants.CLASS_SCOPES["household"],
        status="active",
        refresh_ciphertext=b"x",
        key_version=1,
        expires_at=None,  # type: ignore[arg-type]
    )
    loaded: list[tuple[str, uuid.UUID | None]] = []

    async def load_active(conn: Any, agent_id: str, user_id: uuid.UUID | None = None) -> Any:
        loaded.append((agent_id, user_id))
        return grant

    async def keycloak_sub(conn: Any, user_id: uuid.UUID) -> str:
        return "sub-1"

    monkeypatch.setattr(grants, "load_active", load_active)
    monkeypatch.setattr(token_source, "_keycloak_sub", keycloak_sub)
    source = token_source.MockSource()
    a = asyncio.run(source.sample_token(None, "household:h-1", user_id))  # type: ignore[arg-type]
    b = asyncio.run(source.sample_token(None, "household:h-1", user_id))  # type: ignore[arg-type]
    assert loaded == [("household:h-1", user_id)] * 2  # no cache: the grant is read each time
    assert a != b  # a fresh token each time (new jti)
    assert source._cache == {}
    assert _claims(a)["sub"] == "sub-1"
    assert _claims(a)["act"]["client_id"] == "household.h-1"


# --- db ---------------------------------------------------------------------------------------


def _people(pg: str, agent_id: str, run_id: uuid.UUID | None = None) -> str:
    async def go() -> str:
        async with await AsyncConnection.connect(pg) as conn:
            return await token_source.MockSource().people_token(
                conn, run_id or uuid.uuid4(), agent_id
            )

    return asyncio.run(go())


@pytest.mark.db
def test_run_exchanges_from_stored_grant(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        world = w.add_world(conn)
    claims = _claims(_people(pg, world.consumer_agent))
    assert claims["sub"] == world.admin.keycloak_sub
    assert claims["act"]["client_id"] == c.agent_id_to_client_id(world.consumer_agent)
    assert claims["azp"] == c.agent_id_to_client_id(world.consumer_agent)
    assert claims["aud"] == c.MCP_AUDIENCE
    assert set(claims["scope"].split()) == set(grants.CLASS_SCOPES["consumer"])
    # Without an active grant there is no token (revoke works in mock mode too).
    with psycopg.connect(pg) as conn:
        conn.execute("UPDATE agent_grants SET status = 'revoked'")
    with pytest.raises(grants.GrantRevoked):
        _people(pg, world.consumer_agent)


@pytest.mark.db
def test_household_agent_uses_household_grant(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        world = w.add_world(conn)
        member = w.add_person(conn, world.household_id, role="member")
    # The household agent's token carries the holder of its one household grant (the
    # admin), whoever the run is for (§12 "Which user's grant").
    claims = _claims(_people(pg, world.household_agent))
    assert claims["sub"] == world.admin.keycloak_sub
    assert claims["sub"] != member.keycloak_sub
    assert claims["act"]["client_id"] == c.agent_id_to_client_id(world.household_agent)


@pytest.mark.db
def test_invalid_grant_marks_grant(pg: str) -> None:
    """`MockSource` never reaches Keycloak, so no code path in step 5 sees `invalid_grant`;
    this checks the rule the Keycloak source will call (`grants.mark_refused`)."""
    with psycopg.connect(pg) as conn:
        world = w.add_world(conn)

    async def go(grant_id: uuid.UUID) -> None:
        async with await AsyncConnection.connect(pg) as conn:
            await grants.mark_refused(conn, grant_id)
            await conn.commit()

    live = world.grants[world.household_agent]
    asyncio.run(go(live))
    with psycopg.connect(pg) as conn:
        status, reason = conn.execute(
            "SELECT status, revoke_reason FROM agent_grants WHERE grant_id = %s", (live,)
        ).fetchone()
    assert (status, reason) == ("revoked", "keycloak_invalid_grant")
