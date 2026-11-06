"""Where agents and `api` get their tokens (identity-authz §12 and I3).

`TokenSource` is the one interface; `mcp_client.call_tool_as` is its only caller in a run.

*   `people_token(conn, run_id, agent_id)`: a delegated token. A consumer agent uses the
    grant of its card's `owner_ref` person; a household agent uses its household's one
    active grant (§12 "Which user's grant").
*   `service_token(agent_id)`: a business agent's or `api`'s token.
*   `sample_token(conn, agent_id, user_id)`: for the consent done page; no run, no cache.
*   `invalidate(run_id, agent_id)`: drop the cached token after a token refusal, so the
    next call gets a new one.

Tokens are cached in memory per `(run_id, agent_id)` and obtained again when less than
60 s remain. They are never written anywhere.

`MockSource` signs the claims a Keycloak delegated token carries (`iss`, `aud`, `sub`,
`act`, `azp`, `scope`, `exp`) with `MOCK_SIGNING_KEY`, and still requires an active grant
row, so revoke works in mock mode too. `get_source` picks the run's source: `mock` for an
eval household, otherwise `TOKEN_SOURCE` (I-D24).

`KeycloakSource` (build step 8a) is the live source: it refreshes the grant's stored offline
token, then exchanges it with the agent's own token as the actor, so the result has the
person as `sub` and the agent as `act` (I3). `keycloak_azp` and `token_service` are not built
(§12a), so choosing one fails closed with `IdentityUnavailable`.
"""

import time
from collections.abc import Callable
from typing import Any, Protocol
from uuid import UUID

import httpx
import jwt
from psycopg import AsyncConnection

from services.common import ids, modes
from services.common.config import get_settings
from services.identity import claims as c
from services.identity import grants, tokens
from services.identity.errors import GrantMissing, GrantRevoked, IdentityUnavailable

__all__ = [
    "KEYCLOAK",
    "MOCK",
    "REFRESH_BEFORE_SECONDS",
    "TOKEN_SECONDS",
    "KeycloakSource",
    "MockSource",
    "TokenSource",
    "get_source",
    "mock_claims",
    "sign_mock",
]

MOCK = "mock"
KEYCLOAK = "keycloak"
TOKEN_SECONDS = 300
REFRESH_BEFORE_SECONDS = 60
MOCK_KID = "mock"
API_CLIENT = "svc.api"  # the client of `tokens.API_AGENT` (identity-authz §3)
KEYCLOAK_TIMEOUT_SECONDS = 5.0  # §12a failure table
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
INVALID_GRANT = "invalid_grant"

Clock = Callable[[], float]


class TokenSource(Protocol):
    async def people_token(self, conn: AsyncConnection, run_id: UUID, agent_id: str) -> str: ...
    async def service_token(self, agent_id: str) -> str: ...
    async def sample_token(self, conn: AsyncConnection, agent_id: str, user_id: UUID) -> str: ...
    def invalidate(self, run_id: UUID | None, agent_id: str) -> None: ...


def mock_claims(
    *,
    sub: str,
    client_id: str,
    scopes: tuple[str, ...] | list[str] = (),
    delegated: bool,
    now: float,
    ttl_seconds: int = TOKEN_SECONDS,
    audience: str = c.MCP_AUDIENCE,
) -> dict[str, object]:
    """The claims of a Keycloak token, from the mock issuer. A delegated token carries
    `act.client_id` = the agent; a service token has no `act`."""
    payload: dict[str, object] = {
        "iss": tokens.mock_issuer(),
        "aud": audience,
        "sub": sub,
        "azp": client_id,
        "scope": " ".join(scopes),
        "iat": int(now),
        "exp": int(now) + ttl_seconds,
        "jti": ids.new_token_id(),
    }
    if delegated:
        payload["act"] = {"client_id": client_id, "sub": f"service-account-{client_id}"}
    return payload


def sign_mock(payload: dict[str, object]) -> str:
    """RS256 with `MOCK_SIGNING_KEY` (TEST ONLY, I-D26)."""
    return jwt.encode(
        payload, tokens.mock_private_key(), algorithm=tokens.ALGORITHM, headers={"kid": MOCK_KID}
    )


def _client_id(agent_id: str) -> str:
    # `system:api` is client `svc.api` (identity-authz §3), not `system.api`.
    return API_CLIENT if agent_id == tokens.API_AGENT else c.agent_id_to_client_id(agent_id)


def _service_sub(client_id: str) -> str:
    # Keycloak's service-account user for a client is named this way.
    return f"service-account-{client_id}"


async def _holder(conn: AsyncConnection, agent_id: str) -> grants.Grant:
    """The grant a people agent's tokens come from (§12 "Which user's grant")."""
    cur = await conn.execute(
        "SELECT class, owner_ref FROM agent_cards WHERE agent_id = %s AND enabled", (agent_id,)
    )
    row = await cur.fetchone()
    if row is None:
        raise GrantMissing(f"{agent_id} has no enabled card")
    cls, owner_ref = row
    if cls == "household":
        return await grants.load_active(conn, agent_id)
    kind, _, ref = owner_ref.partition(":")
    if cls != "consumer" or kind != "user":
        raise GrantMissing(f"{agent_id} is not a people agent")
    return await grants.load_active(conn, agent_id, UUID(ref))


async def _keycloak_sub(conn: AsyncConnection, user_id: UUID) -> str:
    cur = await conn.execute("SELECT keycloak_sub FROM users WHERE user_id = %s", (user_id,))
    row = await cur.fetchone()
    if row is None or not row[0]:
        # A token's `sub` must map back to the person (§8 step 1.5); fails closed.
        raise GrantMissing("the grant holder has no Keycloak user id")
    return str(row[0])


class MockSource:
    """Signed mock tokens with Keycloak's claims (§12 `mock`)."""

    name = MOCK

    def __init__(self, clock: Clock = time.time) -> None:
        self._clock = clock
        self._cache: dict[tuple[UUID | None, str], tuple[str, float]] = {}

    def _cached(self, key: tuple[UUID | None, str]) -> str | None:
        hit = self._cache.get(key)
        if hit is not None and hit[1] - self._clock() >= REFRESH_BEFORE_SECONDS:
            return hit[0]
        return None

    def _store(self, key: tuple[UUID | None, str], token: str) -> str:
        self._cache[key] = (token, self._clock() + TOKEN_SECONDS)
        return token

    def invalidate(self, run_id: UUID | None, agent_id: str) -> None:
        self._cache.pop((run_id, agent_id), None)

    async def _people(self, conn: AsyncConnection, grant: grants.Grant) -> str:
        sub = await _keycloak_sub(conn, grant.user_id)
        payload = mock_claims(
            sub=sub,
            client_id=c.agent_id_to_client_id(grant.agent_id),
            scopes=grant.scopes,
            delegated=True,
            now=self._clock(),
        )
        return sign_mock(payload)

    async def people_token(self, conn: AsyncConnection, run_id: UUID, agent_id: str) -> str:
        key = (run_id, agent_id)
        hit = self._cached(key)
        if hit is not None:
            return hit
        grant = await _holder(conn, agent_id)
        return self._store(key, await self._people(conn, grant))

    async def service_token(self, agent_id: str) -> str:
        key = (None, agent_id)
        hit = self._cached(key)
        if hit is not None:
            return hit
        client_id = _client_id(agent_id)
        payload = mock_claims(
            sub=_service_sub(client_id), client_id=client_id, delegated=False, now=self._clock()
        )
        return self._store(key, sign_mock(payload))

    async def sample_token(self, conn: AsyncConnection, agent_id: str, user_id: UUID) -> str:
        grant = await grants.load_active(conn, agent_id, user_id)
        return await self._people(conn, grant)

    def owner_token(self, org_id: str) -> str:
        """An owner app token for tests (`aud` = `owner-app`)."""
        payload = mock_claims(
            sub=f"owner.{org_id}",
            client_id="owner-app",
            delegated=False,
            now=self._clock(),
            audience=c.OWNER_AUDIENCE,
        )
        payload["org_id"] = org_id
        return sign_mock(payload)


class _GrantRefused(Exception):
    """The token endpoint answered 400 `invalid_grant`."""


class KeycloakSource:
    """Delegated tokens by token exchange (§12 `keycloak`, I3, §12a)."""

    name = KEYCLOAK

    def __init__(
        self,
        clock: Clock = time.time,
        transport: httpx.AsyncBaseTransport | None = None,
        token_url: str | None = None,
    ) -> None:
        self._clock = clock
        self._transport = transport
        self._token_url = token_url
        self._cache: dict[tuple[UUID | None, str], tuple[str, float]] = {}

    def _url(self) -> str:
        if self._token_url:
            return self._token_url
        keycloak_url = get_settings().keycloak_url
        if not keycloak_url:
            raise IdentityUnavailable("KEYCLOAK_URL is not set")
        return f"{keycloak_url.rstrip('/')}/realms/kitchenless/protocol/openid-connect/token"

    async def _post(self, client_id: str, form: dict[str, str]) -> dict[str, Any]:
        """One token endpoint call as `client_id`. Never logs or raises a token."""
        data = form | {
            "client_id": client_id,
            "client_assertion_type": ASSERTION_TYPE,
            "client_assertion": grants.client_assertion(client_id),
        }
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=KEYCLOAK_TIMEOUT_SECONDS
            ) as client:
                resp = await client.post(self._url(), data=data)
        except httpx.HTTPError as exc:
            raise IdentityUnavailable(f"token endpoint: {type(exc).__name__}") from None
        if resp.status_code == 200:
            try:
                body = resp.json()
                if isinstance(body["access_token"], str):
                    return dict(body)
            except (ValueError, KeyError, TypeError):
                pass
            raise IdentityUnavailable("token endpoint: malformed answer")
        if resp.status_code == 400 and _error_code(resp) == INVALID_GRANT:
            raise _GrantRefused
        raise IdentityUnavailable(f"token endpoint answered {resp.status_code}")

    def _cached(self, key: tuple[UUID | None, str]) -> str | None:
        hit = self._cache.get(key)
        if hit is not None and hit[1] - self._clock() >= REFRESH_BEFORE_SECONDS:
            return hit[0]
        return None

    def _store(self, key: tuple[UUID | None, str], body: dict[str, Any]) -> str:
        lifetime = body.get("expires_in")
        if isinstance(lifetime, bool) or not isinstance(lifetime, int):
            lifetime = TOKEN_SECONDS
        token = str(body["access_token"])
        self._cache[key] = (token, self._clock() + lifetime)
        return token

    def invalidate(self, run_id: UUID | None, agent_id: str) -> None:
        self._cache.pop((run_id, agent_id), None)
        self._cache.pop((None, agent_id), None)  # a people token's actor token

    async def service_token(self, agent_id: str) -> str:
        key = (None, agent_id)
        hit = self._cached(key)
        if hit is not None:
            return hit
        try:
            body = await self._post(_client_id(agent_id), {"grant_type": "client_credentials"})
        except _GrantRefused:
            raise IdentityUnavailable("token endpoint refused the client") from None
        return self._store(key, body)

    async def _exchange(self, conn: AsyncConnection, grant: grants.Grant) -> dict[str, Any]:
        """I3 steps 2 and 3: refresh the stored grant, then exchange it."""
        client_id = c.agent_id_to_client_id(grant.agent_id)
        try:
            refreshed = await self._post(
                client_id, {"grant_type": "refresh_token", "refresh_token": grants.decrypt(grant)}
            )
        except _GrantRefused:
            await grants.mark_refused(conn, grant.grant_id)
            await grants.load_active(conn, grant.agent_id, grant.user_id)  # the matching error
            raise GrantRevoked(f"the grant of {grant.agent_id} is revoked") from None
        actor = await self.service_token(grant.agent_id)
        try:
            return await self._post(
                client_id,
                {
                    "grant_type": EXCHANGE_GRANT,
                    "subject_token": refreshed["access_token"],
                    "subject_token_type": ACCESS_TOKEN_TYPE,
                    "actor_token": actor,
                    "actor_token_type": ACCESS_TOKEN_TYPE,
                },
            )
        except _GrantRefused:
            # The refresh just worked, so this says nothing about the stored grant.
            raise IdentityUnavailable("token exchange refused") from None

    async def people_token(self, conn: AsyncConnection, run_id: UUID, agent_id: str) -> str:
        key = (run_id, agent_id)
        hit = self._cached(key)
        if hit is not None:
            return hit
        grant = await _holder(conn, agent_id)
        return self._store(key, await self._exchange(conn, grant))

    async def sample_token(self, conn: AsyncConnection, agent_id: str, user_id: UUID) -> str:
        grant = await grants.load_active(conn, agent_id, user_id)
        return str((await self._exchange(conn, grant))["access_token"])


def _error_code(resp: httpx.Response) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    return body.get("error") if isinstance(body, dict) else None


_SOURCES: dict[str, TokenSource] = {}


def get_source(env: str, recorded: str | None = None) -> TokenSource:
    """The token source of a run: the one `runs.start` recorded in `runs.modes`, else
    `mock` for an eval household and `TOKEN_SOURCE` otherwise (I-D24)."""
    name = recorded or modes.token_source(env)
    if name not in (MOCK, KEYCLOAK):
        # `keycloak_azp` and `token_service` are not built (§12a); fail closed.
        raise IdentityUnavailable(f"token source {name} is not built")
    source = _SOURCES.get(name)
    if source is None:
        source = _SOURCES[name] = MockSource() if name == MOCK else KeycloakSource()
    return source
