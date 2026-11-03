"""`tokens.validate` and `TOOL_SCOPES` (identity-authz I2, §8 steps 1 and 1b)."""

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from psycopg import AsyncConnection

from services.identity import claims as c
from services.identity import grants, token_source, tokens
from services.identity.errors import GrantMissing, GrantRevoked, IdentityUnavailable, TokenRefused
from tests.unit import identity_world as w

HOUSEHOLD = "household:h-1"
CONSUMER = "consumer:h-1-a"
SELLER = "grocery:mock-us"
SUB = "7f1b8a52-5b0e-4d43-9d0e-1d6f3c0a0001"
USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
GRANT_ID = uuid.UUID("00000000-0000-4000-8000-0000000000aa")

CLIENTS = {
    "household.h-1": (HOUSEHOLD, "people"),
    "consumer.h-1-a": (CONSUMER, "people"),
    "grocery.mock-us": (SELLER, "service"),
    "svc.api": ("system:api", "service"),
}


class _Cursor:
    def __init__(self, row: Any) -> None:
        self.row = row

    async def fetchone(self) -> Any:
        return self.row


class FakeConn:
    """Answers the two lookups `validate` makes: `agent_clients` and `users`."""

    async def execute(self, sql: str, params: tuple[Any, ...]) -> _Cursor:
        if "FROM agent_clients" in sql:
            return _Cursor(CLIENTS.get(params[0]))
        if "FROM users" in sql:
            return _Cursor((USER_ID,) if params[0] == SUB else None)
        raise AssertionError(sql)


class Ctx:
    def __init__(self, agent_id: str, env: str = "live") -> None:
        self.agent_id = agent_id
        self.env = env


def _grant(agent_id: str = HOUSEHOLD) -> grants.Grant:
    return grants.Grant(
        grant_id=GRANT_ID,
        agent_id=agent_id,
        agent_kind=agent_id.partition(":")[0],
        user_id=USER_ID,
        consented_by=USER_ID,
        household_id="h-1",
        scopes=grants.CLASS_SCOPES["household"],
        status="active",
        refresh_ciphertext=b"x",
        key_version=1,
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )


@pytest.fixture
def active_grant(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, uuid.UUID | None]]:
    loaded: list[tuple[str, uuid.UUID | None]] = []

    async def load_active(conn: Any, agent_id: str, user_id: uuid.UUID | None = None) -> Any:
        loaded.append((agent_id, user_id))
        return _grant(agent_id)

    monkeypatch.setattr(grants, "load_active", load_active)
    return loaded


def _validate(token: str, agent_id: str = HOUSEHOLD, env: str = "live") -> tokens.Claims:
    return asyncio.run(tokens.validate(FakeConn(), token, Ctx(agent_id, env)))  # type: ignore[arg-type]


def _refused(token: str, agent_id: str = HOUSEHOLD, env: str = "live") -> str:
    with pytest.raises(TokenRefused) as e:
        _validate(token, agent_id, env)
    return e.value.reason


def test_validate_accepts_mock_delegated_token(active_grant: list) -> None:
    claims = _validate(w.people_token(HOUSEHOLD, SUB))
    assert claims == tokens.Claims(
        USER_ID, HOUSEHOLD, "people", frozenset(w.HOUSEHOLD_SCOPES), GRANT_ID, "mock"
    )
    assert active_grant == [(HOUSEHOLD, USER_ID)]


def test_validate_accepts_service_token_without_grant(active_grant: list) -> None:
    claims = _validate(w.service_token(SELLER), SELLER)
    assert (claims.kind, claims.user_id, claims.grant_id) == ("service", None, None)
    assert active_grant == []


def test_validate_rejects_token_missing() -> None:
    assert _refused("") == c.TOKEN_MISSING


def test_validate_rejects_token_expired(active_grant: list) -> None:
    old = time.time() - 400  # expired 100 s ago: beyond the 30 s skew
    assert _refused(w.people_token(HOUSEHOLD, SUB, now=old)) == c.TOKEN_EXPIRED


def test_validate_allows_30s_skew(active_grant: list) -> None:
    token = w.people_token(HOUSEHOLD, SUB, now=time.time() - 310)  # expired 10 s ago
    assert _validate(token).agent_id == HOUSEHOLD


def test_validate_rejects_token_invalid_iss(active_grant: list) -> None:
    token = w.people_token(HOUSEHOLD, SUB, iss="https://elsewhere/mock")
    assert _refused(token) == c.TOKEN_INVALID_ISS


def test_validate_rejects_token_invalid_aud(active_grant: list) -> None:
    assert _refused(w.people_token(HOUSEHOLD, SUB, aud="owner-app")) == c.TOKEN_INVALID_AUD


def test_validate_accepts_aud_list(active_grant: list) -> None:
    token = w.people_token(HOUSEHOLD, SUB, aud=["account", c.MCP_AUDIENCE])
    assert _validate(token).agent_id == HOUSEHOLD


def test_validate_rejects_act_missing(active_grant: list) -> None:
    assert _refused(w.people_token(HOUSEHOLD, SUB, act=None)) == c.ACT_MISSING


def test_validate_rejects_act_mismatch(active_grant: list) -> None:
    token = w.people_token(HOUSEHOLD, SUB)
    assert _refused(token, agent_id=CONSUMER) == c.ACT_MISMATCH


def test_validate_rejects_service_token_for_other_agent(active_grant: list) -> None:
    assert _refused(w.service_token(SELLER), agent_id="system:api") == c.ACT_MISMATCH


def test_validate_rejects_unknown_agent(active_grant: list) -> None:
    token = w.people_token(HOUSEHOLD, SUB, act={"client_id": "household.nobody"})
    assert _refused(token) == c.UNKNOWN_AGENT
    assert _refused(w.people_token(HOUSEHOLD, SUB, azp="nobody.x")) == c.UNKNOWN_AGENT


def test_validate_rejects_hs256(active_grant: list) -> None:
    payload = jwt.decode(w.people_token(HOUSEHOLD, SUB), options={"verify_signature": False})
    forged = jwt.encode(payload, "a-shared-secret-at-least-32-bytes-long", algorithm="HS256")
    assert _refused(forged) == c.TOKEN_INVALID


def test_validate_rejects_other_signing_key(active_grant: list) -> None:
    payload = jwt.decode(w.people_token(HOUSEHOLD, SUB), options={"verify_signature": False})
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(payload, other, algorithm="RS256", headers={"kid": "mock"})
    assert _refused(forged) == c.TOKEN_INVALID


@pytest.mark.parametrize("claim", ["exp", "sub", "azp", "aud", "iss"])
def test_validate_rejects_token_without_required_claim(active_grant: list, claim: str) -> None:
    payload = token_source.mock_claims(
        sub=SUB, client_id="household.h-1", scopes=(), delegated=True, now=time.time()
    )
    del payload[claim]
    reason = _refused(token_source.sign_mock(payload))
    assert reason == (c.TOKEN_INVALID_ISS if claim == "iss" else c.TOKEN_INVALID)


def test_validate_rejects_payload_changed_after_signing(active_grant: list) -> None:
    header, payload, signature = w.people_token(HOUSEHOLD, SUB).split(".")
    other = w.people_token(CONSUMER, SUB).split(".")[1]  # a validly encoded other payload
    assert _refused(f"{header}.{other}.{signature}", agent_id=CONSUMER) == c.TOKEN_INVALID


@pytest.mark.parametrize("token", ["garbage", "a.b.c", "eyJhbGciOiJub25lIn0.e30."])
def test_validate_rejects_garbage(active_grant: list, token: str) -> None:
    assert _refused(token) == c.TOKEN_INVALID


def test_validate_unknown_sub_is_grant_missing(active_grant: list) -> None:
    with pytest.raises(GrantMissing):
        _validate(w.people_token(HOUSEHOLD, "not-a-known-sub"))


def test_mock_token_refused_under_live_source_for_live_household(
    identity_settings: Any, active_grant: list
) -> None:
    identity_settings.token_source = "keycloak"
    assert _refused(w.people_token(HOUSEHOLD, SUB), env="live") == c.TOKEN_INVALID_ISS
    # An eval household's run accepts the mock issuer under any source (I-D24).
    assert _validate(w.people_token(HOUSEHOLD, SUB), env="eval").token_source == "mock"


# --- azp mode and the live issuer's JWKS -------------------------------------------------


@pytest.fixture
def live_key(identity_settings: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A Keycloak stand-in: a JWKS served by a fake `httpx.get`, counting fetches."""
    identity_settings.keycloak_url = "http://keycloak:8080/auth"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    state: dict[str, Any] = {"key": key, "kids": ["kid-1"], "fetches": 0, "status": 200}

    class Response:
        def __init__(self) -> None:
            self.status_code = state["status"]

        def json(self) -> dict[str, Any]:
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
            return {"keys": [{**jwk, "kid": kid, "use": "sig"} for kid in state["kids"]]}

    def get(url: str, timeout: float) -> Response:
        assert url == "http://keycloak:8080/auth/realms/kitchenless/protocol/openid-connect/certs"
        state["fetches"] += 1
        return Response()

    monkeypatch.setattr(tokens.httpx, "get", get)
    return state


def _live_token(state: dict[str, Any], kid: str, **claims: Any) -> str:
    now = int(time.time())
    payload = {
        "iss": "https://localhost/auth/realms/kitchenless",
        "aud": c.MCP_AUDIENCE,
        "sub": SUB,
        "azp": "household.h-1",
        "scope": "orders:place",
        "iat": now,
        "exp": now + 300,
        **claims,
    }
    return jwt.encode(payload, state["key"], algorithm="RS256", headers={"kid": kid})


def test_azp_mode_reads_agent_from_azp(
    identity_settings: Any, live_key: dict[str, Any], active_grant: list
) -> None:
    identity_settings.token_source = "keycloak_azp"
    claims = _validate(_live_token(live_key, "kid-1"))  # no `act`
    assert (claims.agent_id, claims.token_source) == (HOUSEHOLD, "keycloak_azp")
    # Under `keycloak` the same token has no `act` and is refused.
    identity_settings.token_source = "keycloak"
    assert _refused(_live_token(live_key, "kid-1")) == c.ACT_MISSING


def test_jwks_refetch_on_unknown_kid(
    identity_settings: Any, live_key: dict[str, Any], active_grant: list
) -> None:
    identity_settings.token_source = "keycloak"
    act = {"client_id": "household.h-1"}
    assert _validate(_live_token(live_key, "kid-1", act=act)).token_source == "keycloak"
    assert _validate(_live_token(live_key, "kid-1", act=act)).agent_id == HOUSEHOLD
    assert live_key["fetches"] == 1  # cached for 10 minutes
    live_key["kids"] = ["kid-1", "kid-2"]  # Keycloak rotated in a new key
    assert _validate(_live_token(live_key, "kid-2", act=act)).agent_id == HOUSEHOLD
    assert live_key["fetches"] == 2
    assert _refused(_live_token(live_key, "kid-9", act=act)) == c.TOKEN_INVALID
    assert live_key["fetches"] == 3  # one refetch per unknown kid, then refused


def test_jwks_outage_is_identity_unavailable(
    identity_settings: Any, live_key: dict[str, Any]
) -> None:
    identity_settings.token_source = "keycloak"
    live_key["status"] = 503
    with pytest.raises(IdentityUnavailable):
        _validate(_live_token(live_key, "kid-1", act={"client_id": "household.h-1"}))


# --- TOOL_SCOPES ---------------------------------------------------------------------------


def test_tool_scopes_cover_guarded_tools() -> None:
    """`TOOL_SCOPES` names exactly the tools that run the guard (§8 step 1b)."""
    import ast
    import pathlib

    guarded: set[str] = set()
    for path in pathlib.Path("services/mcp").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "run_guarded"):
                continue
            for kw in node.keywords:
                if kw.arg == "tool" and isinstance(kw.value, ast.JoinedStr):
                    tail = kw.value.values[-1]
                    assert isinstance(tail, ast.Constant), path
                    guarded.add(str(tail.value).lstrip("."))
                elif kw.arg == "tool" and isinstance(kw.value, ast.Constant):
                    guarded.add(str(kw.value.value).partition(".")[2])
    assert guarded, "no run_guarded calls found"
    assert guarded == set(tokens.TOOL_SCOPES)


# --- db ---------------------------------------------------------------------------------------


def _db_validate(pg: str, token: str, agent_id: str, env: str = "live") -> tokens.Claims:
    async def go() -> tokens.Claims:
        async with await AsyncConnection.connect(pg) as conn:
            try:
                return await tokens.validate(conn, token, Ctx(agent_id, env))
            finally:
                await conn.commit()

    return asyncio.run(go())


@pytest.mark.db
def test_validate_requires_active_grant(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        world = w.add_world(conn)
    token = w.people_token(world.household_agent, world.admin.keycloak_sub)
    claims = _db_validate(pg, token, world.household_agent)
    assert claims.grant_id == world.grants[world.household_agent]
    with psycopg.connect(pg) as conn:
        conn.execute(
            "UPDATE agent_grants SET status = 'revoked' WHERE grant_id = %s",
            (claims.grant_id,),
        )
    with pytest.raises(GrantRevoked):
        _db_validate(pg, token, world.household_agent)


@pytest.mark.db
def test_mock_token_refused_for_live_household(pg: str, identity_settings: Any) -> None:
    with psycopg.connect(pg) as conn:
        world = w.add_world(conn)
    identity_settings.token_source = "keycloak"
    token = w.people_token(world.household_agent, world.admin.keycloak_sub)
    with pytest.raises(TokenRefused) as e:
        _db_validate(pg, token, world.household_agent, env="live")
    assert e.value.reason == c.TOKEN_INVALID_ISS
