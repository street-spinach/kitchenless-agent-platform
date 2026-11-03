"""Caller tokens for guarded tools (identity-authz §8 step 1 and I2, architecture item 53).

`validate(conn, token, ctx)` runs §8 step 1, items 1 to 6, and returns `Claims`:

1.  The signature, RS256 only, against the active issuer's keys (§13). Keycloak's JWKS is
    cached for 10 minutes and fetched again once on an unknown `kid`.
2.  `iss` is the active issuer. A mock-issued token (`https://${DOMAIN}/mock`) is accepted
    only under `TOKEN_SOURCE=mock` or when the run's household has `env = 'eval'` (I-D24);
    otherwise `token_invalid_iss`.
3.  `aud` holds `kitchenless-mcp` (a string or a list); `exp` is in the future, 30 s skew.
4.  The agent: `agent_clients.kind` of the token's `azp` decides the kind. A delegated
    (`people`) token names its agent in `act.client_id` (`azp` under `keycloak_azp`,
    I-D18), and one with no `act` is refused `act_missing`; a service token names it in
    `azp`. The agent must be `ctx.agent_id` (`act_mismatch`).
5.  A delegated token's `sub` is mapped to a person through `users.keycloak_sub`, and
    `grants.load_active` requires that person's active grant for the agent.
6.  `Claims(user_id, agent_id, kind, scopes, grant_id, token_source)`.

`validate_owner(token)` runs the same signature checks on an owner app token (audience
`owner-app`, §10 and I32) and returns its `sub`.

Refusals raise `TokenRefused(reason)` or a `GrantError`; a key fetch that fails raises
`IdentityUnavailable`. `TOOL_SCOPES` is the §8 step 1b table, which the guard checks.
"""

import functools
import json
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from psycopg import AsyncConnection

from services.common.config import get_settings
from services.identity import claims as c
from services.identity import grants
from services.identity.claims import Claims
from services.identity.errors import GrantMissing, IdentityUnavailable, TokenRefused

__all__ = [
    "ALGORITHM",
    "JWKS_TTL_SECONDS",
    "SKEW_SECONDS",
    "TOOL_SCOPES",
    "Claims",
    "Jwks",
    "ToolScope",
    "check_tool_scope",
    "mock_issuer",
    "mock_private_key",
    "validate",
    "validate_owner",
]

ALGORITHM = "RS256"
SKEW_SECONDS = 30
JWKS_TTL_SECONDS = 600
JWKS_TIMEOUT_SECONDS = 5.0
MOCK_SOURCE = "mock"
API_AGENT = "system:api"


@dataclass(frozen=True)
class ToolScope:
    """One row of the §8 step 1b table.

    `caller`: `delegated` (a people token) or `service`. `agent_classes`: the calling
    agent's class (`household`, `consumer`, `grocery`, ...; `system` for `svc.api`).
    `scope`: the people scope a delegated token needs, or `None`.
    """

    caller: Literal["delegated", "service"]
    agent_classes: frozenset[str]
    scope: str | None = None


def _delegated(*classes: str, scope: str | None) -> ToolScope:
    return ToolScope("delegated", frozenset(classes), scope)


def _service(*classes: str) -> ToolScope:
    return ToolScope("service", frozenset(classes))


# §8 step 1b, with the agents amendment (#186). A tool with no row is refused.
TOOL_SCOPES: Mapping[str, ToolScope] = {
    "get_constraints": _delegated("consumer", "household", scope="constraints:read"),
    "create_order": _delegated("household", scope="orders:place"),
    "authorize_spend": _delegated("household", scope="orders:place"),
    "authorize_amendment": _delegated("household", scope="orders:place"),
    "request_approval": _delegated("household", scope="approvals:request"),
    "record_delivery": _delegated("household", scope="pantry:write"),
    "place_order": _service("grocery", "restaurant"),
    "amend_order": _service("grocery", "restaurant"),
    "book_delivery": _service("delivery"),
    "decide_approval": _service("system"),
    "choose_shortfall_option": _service("system"),
    "get_household_allergens": _delegated("household", scope="constraints:read"),
    "record_dislike": _delegated("consumer", scope="constraints:write"),
    "get_household_rules": _delegated("household", scope=None),
    "advance_turn": _delegated("household", scope="orders:place"),
    "set_take_turns": _service("system"),
    # Experience EX-A11 (EX-I26): setup by chat, for the consumer agent.
    "get_setup_state": _delegated("consumer", scope=None),
    "propose_setup": _delegated("consumer", scope="constraints:write"),
    "apply_setup": _service("system"),
    "complete_join": _service("system"),
}


def check_tool_scope(tool: str, claims: Claims) -> None:
    """§8 step 1b: refuse with `scope_missing` unless `claims` may call `tool`.

    `svc.api` is the only `system` client (`system:api`), so a `system` row admits only it.
    """
    row = TOOL_SCOPES.get(tool)
    agent_class = claims.agent_id.partition(":")[0]
    if row is None:
        raise TokenRefused(c.SCOPE_MISSING, f"{tool} has no scope row")
    expected = "people" if row.caller == "delegated" else "service"
    if claims.kind != expected or agent_class not in row.agent_classes:
        raise TokenRefused(c.SCOPE_MISSING)
    if row.agent_classes == {"system"} and claims.agent_id != API_AGENT:
        raise TokenRefused(c.SCOPE_MISSING)
    if row.scope is not None and row.scope not in claims.scopes:
        raise TokenRefused(c.SCOPE_MISSING)


class _Ctx(Protocol):
    @property
    def agent_id(self) -> str: ...
    @property
    def env(self) -> str: ...


# --- keys ---------------------------------------------------------------------------------


def mock_issuer() -> str:
    """The mock issuer, `https://${DOMAIN}/mock` (§13)."""
    return f"https://{get_settings().domain}/mock"


@functools.cache
def _load_private(pem: str) -> RSAPrivateKey:
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, RSAPrivateKey):
        raise ValueError("MOCK_SIGNING_KEY must be an RSA key")
    return key


def mock_private_key() -> RSAPrivateKey:
    """The private half of `MOCK_SIGNING_KEY` (TEST ONLY, I-D26)."""
    pem = get_settings().mock_signing_key
    if not pem:
        raise TokenRefused(c.TOKEN_INVALID, "MOCK_SIGNING_KEY is not set")
    return _load_private(pem)


def _mock_public_key() -> RSAPublicKey:
    return mock_private_key().public_key()


class Jwks:
    """A JWKS cache: keys for 10 minutes, one refetch on an unknown `kid`."""

    def __init__(self, url: str, ttl_seconds: int = JWKS_TTL_SECONDS) -> None:
        self.url = url
        self.ttl_seconds = ttl_seconds
        self._keys: dict[str, Any] = {}
        self._fetched_at: float | None = None
        self._lock = threading.Lock()

    def _fetch(self) -> None:
        try:
            response = httpx.get(self.url, timeout=JWKS_TIMEOUT_SECONDS)
        except httpx.TransportError as e:
            raise IdentityUnavailable(type(e).__name__) from None
        if response.status_code >= 500:
            raise IdentityUnavailable(f"jwks status {response.status_code}")
        if response.status_code >= 400:
            raise IdentityUnavailable(f"jwks status {response.status_code}")
        keys: dict[str, Any] = {}
        for jwk in response.json().get("keys", []):
            if jwk.get("kty") != "RSA" or jwk.get("use", "sig") != "sig" or "kid" not in jwk:
                continue
            keys[jwk["kid"]] = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
        self._keys = keys
        self._fetched_at = time.monotonic()

    def key(self, kid: str | None) -> Any:
        if not kid:
            raise TokenRefused(c.TOKEN_INVALID, "no kid")
        with self._lock:
            stale = (
                self._fetched_at is None or time.monotonic() - self._fetched_at > self.ttl_seconds
            )
            if stale:
                self._fetch()
            if kid not in self._keys:
                self._fetch()  # once, on an unknown kid
            if kid not in self._keys:
                raise TokenRefused(c.TOKEN_INVALID, "unknown kid")
            return self._keys[kid]


_jwks_by_url: dict[str, Jwks] = {}


def _jwks_url(token_source: str) -> str:
    if token_source == "token_service":
        return "http://api:8000/internal/token-jwks"
    keycloak_url = get_settings().keycloak_url
    if not keycloak_url:
        raise IdentityUnavailable("KEYCLOAK_URL is not set")
    return f"{keycloak_url.rstrip('/')}/realms/kitchenless/protocol/openid-connect/certs"


def _live_key(token_source: str, kid: str | None) -> Any:
    url = _jwks_url(token_source)
    jwks = _jwks_by_url.setdefault(url, Jwks(url))
    return jwks.key(kid)


# --- validation ---------------------------------------------------------------------------


def _unverified(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        header = jwt.get_unverified_header(token)
        payload = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        raise TokenRefused(c.TOKEN_INVALID) from None
    return header, payload


def _decode(token: str, key: Any, issuer: str, audience: str = c.MCP_AUDIENCE) -> dict[str, Any]:
    try:
        return jwt.decode(
            token,
            key,
            algorithms=[ALGORITHM],
            audience=audience,
            issuer=issuer,
            leeway=SKEW_SECONDS,
            options={"require": ["iss", "aud", "exp", "sub", "azp"]},
        )
    except jwt.ExpiredSignatureError:
        raise TokenRefused(c.TOKEN_EXPIRED) from None
    except jwt.InvalidAudienceError:
        raise TokenRefused(c.TOKEN_INVALID_AUD) from None
    except jwt.InvalidIssuerError:
        raise TokenRefused(c.TOKEN_INVALID_ISS) from None
    except jwt.PyJWTError:
        raise TokenRefused(c.TOKEN_INVALID) from None


async def _client(conn: AsyncConnection, client_id: Any) -> tuple[str, str]:
    """`(agent_id, kind)` of a client, or `unknown_agent`."""
    if not isinstance(client_id, str) or not client_id:
        raise TokenRefused(c.UNKNOWN_AGENT)
    cur = await conn.execute(
        "SELECT agent_id, kind FROM agent_clients WHERE client_id = %s", (client_id,)
    )
    row = await cur.fetchone()
    if row is None:
        raise TokenRefused(c.UNKNOWN_AGENT)
    return row[0], row[1]


async def _person(conn: AsyncConnection, sub: Any) -> UUID:
    """The `user_id` whose Keycloak id is `sub`. An unknown person can hold no grant."""
    cur = await conn.execute("SELECT user_id FROM users WHERE keycloak_sub = %s", (str(sub),))
    row = await cur.fetchone()
    if row is None:
        raise GrantMissing("no person for the token's sub")
    return row[0]


async def validate(conn: AsyncConnection, token: str, ctx: _Ctx) -> Claims:
    """§8 step 1. Raises `TokenRefused`, a `GrantError` or `IdentityUnavailable`."""
    if not token:
        raise TokenRefused(c.TOKEN_MISSING)
    settings = get_settings()
    header, unverified = _unverified(token)
    if header.get("alg") != ALGORITHM:
        raise TokenRefused(c.TOKEN_INVALID, "algorithm")
    source = settings.token_source or MOCK_SOURCE
    iss = unverified.get("iss")
    mock = mock_issuer()
    if iss == mock and (source == MOCK_SOURCE or ctx.env == "eval"):
        key: Any = _mock_public_key()
        token_source = MOCK_SOURCE
    elif iss == settings.issuer_url and source != MOCK_SOURCE:
        key = _live_key(source, header.get("kid"))
        token_source = source
    else:
        raise TokenRefused(c.TOKEN_INVALID_ISS)
    payload = _decode(token, key, iss)

    _, kind = await _client(conn, payload["azp"])
    if kind == "people":
        if token_source == "keycloak_azp":
            client_id = payload["azp"]
        else:
            act = payload.get("act")
            if not isinstance(act, Mapping) or not act.get("client_id"):
                raise TokenRefused(c.ACT_MISSING)
            client_id = act["client_id"]
    else:
        client_id = payload["azp"]
    agent_id, agent_kind = await _client(conn, client_id)
    if agent_id != ctx.agent_id or agent_kind != kind:
        raise TokenRefused(c.ACT_MISMATCH)

    scope = payload.get("scope", "")
    scopes = frozenset(scope.split()) if isinstance(scope, str) else frozenset()
    if kind == "service":
        return Claims(None, agent_id, "service", scopes, None, token_source)
    user_id = await _person(conn, payload["sub"])
    grant = await grants.load_active(conn, agent_id, user_id)
    return Claims(user_id, agent_id, "people", scopes, grant.grant_id, token_source)


def validate_owner(token: str) -> str:
    """The owner app's bearer token for `/app/*` (§10, I32): the §8 step 1 signature checks
    (RS256, the active issuer, audience `owner-app`, expiry). Returns the token's `sub`;
    the caller then checks `user owner org:<o>`. A mock-issued token is accepted only under
    `TOKEN_SOURCE=mock`: an owner call has no run, so no eval household. Raises
    `TokenRefused` or `IdentityUnavailable`."""
    if not token:
        raise TokenRefused(c.TOKEN_MISSING)
    settings = get_settings()
    header, unverified = _unverified(token)
    if header.get("alg") != ALGORITHM:
        raise TokenRefused(c.TOKEN_INVALID, "algorithm")
    source = settings.token_source or MOCK_SOURCE
    iss = unverified.get("iss")
    if iss == mock_issuer() and source == MOCK_SOURCE:
        key: Any = _mock_public_key()
    elif iss == settings.issuer_url and source != MOCK_SOURCE:
        key = _live_key(source, header.get("kid"))
    else:
        raise TokenRefused(c.TOKEN_INVALID_ISS)
    payload = _decode(token, key, iss, audience=c.OWNER_AUDIENCE)
    sub = payload["sub"]
    if not isinstance(sub, str) or not sub:
        raise TokenRefused(c.TOKEN_INVALID, "sub")
    return sub
