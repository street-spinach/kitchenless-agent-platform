"""`TokenSource` and `MockSource` (identity-authz I3, §12)."""

import asyncio
import uuid
from typing import Any
from urllib.parse import parse_qsl

import httpx
import jwt
import psycopg
import pytest
from psycopg import AsyncConnection

from services.identity import claims as c
from services.identity import grants, token_source, tokens
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


def test_api_service_token_is_client_svc_api() -> None:
    # `system:api` is client `svc.api` in `agent_clients` (identity-authz §3). The derived
    # `system.api` has no client row, so validation refused every approval tap.
    token = asyncio.run(token_source.MockSource(clock=Clock()).service_token("system:api"))
    claims = _claims(token)
    assert (claims["azp"], claims["sub"]) == ("svc.api", "service-account-svc.api")
    assert "act" not in claims
    seller = _claims(asyncio.run(token_source.MockSource(clock=Clock()).service_token(SELLER)))
    assert seller["azp"] == "grocery.mock-us"


def test_new_token_after_refusal_once() -> None:
    clock = Clock()
    source = token_source.MockSource(clock=clock)
    first = asyncio.run(source.service_token(SELLER))
    source.invalidate(None, SELLER)  # what `call_tool_as` does after a token refusal
    second = asyncio.run(source.service_token(SELLER))
    assert second != first
    assert asyncio.run(source.service_token(SELLER)) == second  # cached again


def test_unbuilt_sources_fail_closed() -> None:
    """`keycloak_azp` and `token_service` are not built (§12a): choosing one is an outage,
    never a fallback to mock tokens."""
    for name in ("keycloak_azp", "token_service"):
        with pytest.raises(IdentityUnavailable):
            token_source.get_source("live", name)
    assert isinstance(token_source.get_source("eval"), token_source.MockSource)
    assert isinstance(token_source.get_source("live", "mock"), token_source.MockSource)
    assert isinstance(token_source.get_source("live", "keycloak"), token_source.KeycloakSource)


# --- KeycloakSource (I3, build step 8a) -------------------------------------------------------

TOKEN_URL = "http://keycloak.test/token"


class FakeKeycloak:
    """The token endpoint: records each form and answers by `grant_type`."""

    def __init__(self, **answers: tuple[int, dict[str, Any]]) -> None:
        self.answers = answers
        self.forms: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        form = dict(parse_qsl(request.content.decode()))
        self.forms.append(form)
        kind = form["grant_type"].rsplit(":", 1)[-1].replace("-", "_")
        n = sum(f["grant_type"] == form["grant_type"] for f in self.forms)
        default = (200, {"access_token": f"{kind}-{n}", "expires_in": 300})
        status, body = self.answers.get(kind, default)
        return httpx.Response(status, json=body)

    def kinds(self) -> list[str]:
        return [f["grant_type"].rsplit(":", 1)[-1] for f in self.forms]


def _grant(agent_id: str = "household:h-1") -> grants.Grant:
    return grants.Grant(
        grant_id=uuid.uuid4(),
        agent_id=agent_id,
        agent_kind="household",
        user_id=uuid.uuid4(),
        consented_by=uuid.uuid4(),
        household_id="h-1",
        scopes=("orders:place",),
        status="active",
        refresh_ciphertext=b"sealed",
        key_version=1,
        expires_at=None,  # type: ignore[arg-type]
    )


class Marks:
    def __init__(self) -> None:
        self.refused: list[uuid.UUID] = []


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch) -> Marks:
    """`KeycloakSource` with no database or keys: a fixed grant and client assertion."""
    marks = Marks()

    async def holder(conn: Any, agent_id: str) -> grants.Grant:
        return _grant(agent_id)

    async def mark_refused(conn: Any, grant_id: uuid.UUID) -> None:
        marks.refused.append(grant_id)

    async def load_active(conn: Any, agent_id: str, user_id: Any = None) -> grants.Grant:
        raise grants.GrantRevoked("revoked")

    monkeypatch.setattr(token_source, "_holder", holder)
    monkeypatch.setattr(grants, "mark_refused", mark_refused)
    monkeypatch.setattr(grants, "load_active", load_active)
    monkeypatch.setattr(grants, "decrypt", lambda grant: "offline-token")
    monkeypatch.setattr(grants, "client_assertion", lambda client_id: f"assertion-{client_id}")
    return marks


def _source(fake: Any, clock: Clock | None = None) -> token_source.KeycloakSource:
    return token_source.KeycloakSource(
        clock or Clock(), httpx.MockTransport(fake), token_url=TOKEN_URL
    )


def _people_live(source: token_source.KeycloakSource, run_id: uuid.UUID | None = None) -> str:
    return asyncio.run(source.people_token(None, run_id or uuid.uuid4(), "household:h-1"))  # type: ignore[arg-type]


def test_keycloak_exchange_uses_the_spike_parameters(live: Marks) -> None:
    fake = FakeKeycloak()
    token = _people_live(_source(fake))
    assert token == "token_exchange-1"
    assert fake.kinds() == ["refresh_token", "client_credentials", "token-exchange"]
    refresh, actor, exchange = fake.forms
    assert refresh["refresh_token"] == "offline-token"
    assert actor["client_id"] == "household.h-1"
    assert exchange["subject_token"] == "refresh_token-1"
    assert exchange["actor_token"] == "client_credentials-1"
    assert exchange["subject_token_type"] == token_source.ACCESS_TOKEN_TYPE
    assert exchange["actor_token_type"] == token_source.ACCESS_TOKEN_TYPE
    assert "audience" not in exchange  # I3: no `audience` parameter
    for form in fake.forms:
        assert form["client_id"] == "household.h-1"
        assert form["client_assertion"] == "assertion-household.h-1"
        assert form["client_assertion_type"] == token_source.ASSERTION_TYPE


def test_keycloak_cache_reuses_until_60s_left(live: Marks) -> None:
    fake, clock, run_id = FakeKeycloak(), Clock(), uuid.uuid4()
    source = _source(fake, clock)
    first = _people_live(source, run_id)
    clock.now += 239  # 61 s left
    assert _people_live(source, run_id) == first
    clock.now += 2  # 59 s left
    assert _people_live(source, run_id) != first


def test_keycloak_new_token_after_refusal(live: Marks) -> None:
    fake, run_id = FakeKeycloak(), uuid.uuid4()
    source = _source(fake)
    first = _people_live(source, run_id)
    source.invalidate(run_id, "household:h-1")
    assert _people_live(source, run_id) != first
    assert fake.kinds().count("client_credentials") == 2  # the actor token is new too


def test_keycloak_service_token_for_api_uses_svc_api(live: Marks) -> None:
    fake = FakeKeycloak()
    source = _source(fake)
    assert asyncio.run(source.service_token(tokens.API_AGENT)) == "client_credentials-1"
    assert fake.forms[0]["client_id"] == "svc.api"
    assert asyncio.run(source.service_token(tokens.API_AGENT)) == "client_credentials-1"
    assert len(fake.forms) == 1  # cached


@pytest.mark.parametrize("step", ["refresh_token", "client_credentials", "token_exchange"])
@pytest.mark.parametrize("status", [500, 503])
def test_identity_unavailable_on_5xx(live: Marks, step: str, status: int) -> None:
    source = _source(FakeKeycloak(**{step: (status, {})}))
    with pytest.raises(IdentityUnavailable):
        _people_live(source)
    assert live.refused == []  # an outage never touches the grant


def test_identity_unavailable_on_connection_error(live: Marks) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(IdentityUnavailable):
        _people_live(_source(fail))
    assert live.refused == []


@pytest.mark.parametrize(
    "body", [{}, {"access_token": 5}, {"token": "x"}], ids=["empty", "not-a-string", "no-token"]
)
def test_malformed_answer_is_an_outage(live: Marks, body: dict[str, Any]) -> None:
    with pytest.raises(IdentityUnavailable):
        _people_live(_source(FakeKeycloak(refresh_token=(200, body))))


def test_invalid_grant_on_refresh_marks_the_grant(live: Marks) -> None:
    fake = FakeKeycloak(refresh_token=(400, {"error": "invalid_grant"}))
    with pytest.raises(grants.GrantRevoked):
        _people_live(_source(fake))
    assert len(live.refused) == 1
    assert fake.kinds() == ["refresh_token"]  # no exchange after a refusal


def test_other_400_on_refresh_leaves_the_grant(live: Marks) -> None:
    fake = FakeKeycloak(refresh_token=(400, {"error": "invalid_client"}))
    with pytest.raises(IdentityUnavailable):
        _people_live(_source(fake))
    assert live.refused == []


def test_invalid_grant_on_exchange_leaves_the_grant(live: Marks) -> None:
    """The refresh just worked, so an exchange refusal says nothing about the stored grant."""
    fake = FakeKeycloak(token_exchange=(400, {"error": "invalid_grant"}))
    with pytest.raises(IdentityUnavailable):
        _people_live(_source(fake))
    assert live.refused == []


def test_keycloak_sample_token_has_no_cache(live: Marks, monkeypatch: pytest.MonkeyPatch) -> None:
    async def load_active(conn: Any, agent_id: str, user_id: Any = None) -> grants.Grant:
        return _grant(agent_id)

    monkeypatch.setattr(grants, "load_active", load_active)
    fake = FakeKeycloak()
    source = _source(fake)
    token = asyncio.run(source.sample_token(None, "household:h-1", uuid.uuid4()))  # type: ignore[arg-type]
    assert token == "token_exchange-1"
    assert [k for k in source._cache if k[0] is not None] == []


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


@pytest.mark.db
def test_keycloak_invalid_grant_revokes_the_stored_grant(
    pg: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused refresh stores the revoke, and the run sees `GrantRevoked`, not an outage."""
    with psycopg.connect(pg) as conn:
        world = w.add_world(conn)
    monkeypatch.setattr(grants, "decrypt", lambda grant: "offline-token")
    monkeypatch.setattr(grants, "client_assertion", lambda client_id: "assertion")
    source = _source(FakeKeycloak(refresh_token=(400, {"error": "invalid_grant"})))

    async def go() -> None:
        async with await AsyncConnection.connect(pg) as conn:
            try:
                await source.people_token(conn, uuid.uuid4(), world.household_agent)
            finally:
                await conn.commit()  # as `mcp_client._token` does on a `GrantError`

    with pytest.raises(grants.GrantRevoked):
        asyncio.run(go())
    with psycopg.connect(pg) as conn:
        status, reason = conn.execute(
            "SELECT status, revoke_reason FROM agent_grants WHERE grant_id = %s",
            (world.grants[world.household_agent],),
        ).fetchone()
    assert (status, reason) == ("revoked", "keycloak_invalid_grant")
