"""Keycloak Admin API calls for onboarding, sign-in and revoke (identity-authz §3, §6, §11
and I22). Used only by `api`.

*   `admin_token()`: a `client_credentials` token for `svc.onboarding` (its secret is
    `KC_ONBOARDING_SECRET`).
*   `create_user(username, display_name, attributes)` returns the Keycloak user id; a 409
    means the user exists, and its id is fetched.
*   `create_agent_client(agent_id, template)` copies the template client, sets `clientId`,
    `enabled` and the signing certificate, and adds the new client to the
    `agents-may-delegate` FGAP v2 policy (without it Keycloak silently drops the
    delegation scope).
*   `magic_link(username, redirect_uri, state, expiration_s=120)` asks the magic-link
    extension for one sign-in URL to `me-pages`. `reusable` defaults to true there, so
    `false` is always sent (spike #111).
*   `revoke_consent(keycloak_user_id, client_id)`: the per-client consent revoke.
*   `username(keycloak_user_id)` and `redeem_code(client_id, code, redirect_uri)` serve
    `routes/me.py`: the magic link needs the username, and the callback redeems codes
    with `private_key_jwt` (I24).

Connection errors, timeouts and 5xx raise `IdentityUnavailable`; any other unexpected
status raises `KeycloakError`. Nothing here logs a token, a secret or a link.
"""

import copy
from typing import Any

import httpx

from services.common import ids
from services.common.config import get_settings
from services.identity import grants
from services.identity.errors import IdentityUnavailable

__all__ = [
    "DELEGATE_POLICY",
    "ME_PAGES",
    "ONBOARDING_CLIENT",
    "REALM",
    "KeycloakAdmin",
    "KeycloakError",
    "admin_token",
    "create_agent_client",
    "create_user",
    "default",
    "magic_link",
    "redeem_code",
    "revoke_consent",
    "username",
]

REALM = "kitchenless"
ONBOARDING_CLIENT = "svc.onboarding"
ME_PAGES = "me-pages"
FGAP_CLIENT = "admin-permissions"
DELEGATE_POLICY = "agents-may-delegate"
CERT_ATTRIBUTE = "jwt.credential.certificate"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
MAGIC_LINK_SECONDS = 120
TIMEOUT_SECONDS = 10.0


class KeycloakError(RuntimeError):
    """Keycloak answered with a status this module does not expect."""


class KeycloakAdmin:
    """One Keycloak realm, reached at `base_url` (the internal `KEYCLOAK_URL`)."""

    def __init__(
        self,
        base_url: str,
        onboarding_secret: str | None,
        signing_cert: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._secret = onboarding_secret
        self._cert = signing_cert
        self._transport = transport

    # --- HTTP ---------------------------------------------------------------------------

    @property
    def _realm(self) -> str:
        return f"{self.base_url}/realms/{REALM}"

    @property
    def _admin(self) -> str:
        return f"{self.base_url}/admin/realms/{REALM}"

    async def _request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=TIMEOUT_SECONDS
            ) as client:
                resp = await client.request(method, url, **kw)
        except httpx.HTTPError as exc:
            raise IdentityUnavailable(f"Keycloak did not answer: {type(exc).__name__}") from exc
        if resp.status_code >= 500:
            raise IdentityUnavailable(f"Keycloak answered {resp.status_code}")
        return resp

    async def _admin_call(
        self, method: str, path: str, *, ok: tuple[int, ...], **kw: Any
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {await self.admin_token()}"}
        resp = await self._request(method, f"{self._admin}{path}", headers=headers, **kw)
        if resp.status_code not in ok:
            raise KeycloakError(f"{method} {path.split('?')[0]} answered {resp.status_code}")
        return resp

    @staticmethod
    def _created_id(resp: httpx.Response) -> str:
        location = resp.headers.get("Location", "")
        created = location.rstrip("/").rsplit("/", 1)[-1]
        if not created:
            raise KeycloakError("Keycloak created a resource without a Location")
        return created

    # --- calls --------------------------------------------------------------------------

    async def admin_token(self) -> str:
        if not self._secret:
            raise RuntimeError("KC_ONBOARDING_SECRET is required")
        resp = await self._request(
            "POST",
            f"{self._realm}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": ONBOARDING_CLIENT,
                "client_secret": self._secret,
            },
        )
        if resp.status_code != 200:
            raise KeycloakError(f"svc.onboarding token answered {resp.status_code}")
        return str(resp.json()["access_token"])

    async def create_user(
        self, username: str, display_name: str, attributes: dict[str, list[str]] | None = None
    ) -> str:
        body = {
            "username": username,
            "enabled": True,
            "firstName": display_name,
            "attributes": attributes or {},
        }
        resp = await self._admin_call("POST", "/users", ok=(201, 409), json=body)
        if resp.status_code == 201:
            return self._created_id(resp)
        found = await self._admin_call(
            "GET", "/users", ok=(200,), params={"username": username, "exact": "true"}
        )
        users = [u for u in found.json() if u.get("username") == username.lower()]
        if len(users) != 1:
            raise KeycloakError("user exists but could not be found by username")
        return str(users[0]["id"])

    async def _client(self, client_id: str) -> dict[str, Any] | None:
        resp = await self._admin_call("GET", "/clients", ok=(200,), params={"clientId": client_id})
        found = [c for c in resp.json() if c.get("clientId") == client_id]
        return found[0] if found else None

    async def create_agent_client(self, agent_id: str, template: str) -> str:
        """The new (or existing) client's Keycloak uuid, joined to `agents-may-delegate`."""
        if not self._cert:
            raise RuntimeError("AGENT_SIGNING_CERT is required")
        client_id = ids.keycloak_client_id(agent_id)
        existing = await self._client(client_id)
        if existing is not None:
            kc_uuid = str(existing["id"])
        else:
            source = await self._client(template)
            if source is None:
                raise KeycloakError(f"template client {template} not found")
            rep = copy.deepcopy(source)
            rep.pop("id", None)
            for mapper in rep.get("protocolMappers") or []:
                mapper.pop("id", None)
            rep["clientId"] = client_id
            rep["name"] = client_id
            rep["enabled"] = True
            rep["attributes"] = {**(rep.get("attributes") or {}), CERT_ATTRIBUTE: self._cert}
            resp = await self._admin_call("POST", "/clients", ok=(201,), json=rep)
            kc_uuid = self._created_id(resp)
        await self._join_delegate_policy(kc_uuid)
        return kc_uuid

    async def _join_delegate_policy(self, kc_uuid: str) -> None:
        fgap = await self._client(FGAP_CLIENT)
        if fgap is None:
            raise KeycloakError("the admin-permissions client is missing (FGAP v2 off?)")
        base = f"/clients/{fgap['id']}/authz/resource-server/policy"
        found = await self._admin_call("GET", base, ok=(200,), params={"name": DELEGATE_POLICY})
        policies = [p for p in found.json() if p.get("name") == DELEGATE_POLICY]
        if not policies:
            raise KeycloakError(f"policy {DELEGATE_POLICY} not found")
        path = f"{base}/client/{policies[0]['id']}"
        policy = (await self._admin_call("GET", path, ok=(200,))).json()
        clients = list(policy.get("clients") or [])
        if kc_uuid in clients:
            return
        policy["clients"] = [*clients, kc_uuid]
        await self._admin_call("PUT", path, ok=(200, 201, 204), json=policy)

    async def magic_link(
        self,
        username: str,
        redirect_uri: str,
        state: str,
        expiration_s: int = MAGIC_LINK_SECONDS,
    ) -> str:
        headers = {"Authorization": f"Bearer {await self.admin_token()}"}
        body = {
            "username": username,
            "client_id": ME_PAGES,
            "redirect_uri": redirect_uri,
            "expiration_seconds": expiration_s,
            "reusable": False,
            "state": state,
            "scope": "openid",
        }
        resp = await self._request("POST", f"{self._realm}/magic-link", headers=headers, json=body)
        if resp.status_code != 200:
            raise KeycloakError(f"magic-link answered {resp.status_code}")
        return str(resp.json()["link"])

    async def revoke_consent(self, keycloak_user_id: str, client_id: str) -> None:
        await self._admin_call(
            "DELETE", f"/users/{keycloak_user_id}/consents/{client_id}", ok=(204,)
        )

    async def username(self, keycloak_user_id: str) -> str:
        resp = await self._admin_call("GET", f"/users/{keycloak_user_id}", ok=(200,))
        return str(resp.json()["username"])

    async def redeem_code(self, client_id: str, code: str, redirect_uri: str) -> dict[str, Any]:
        """The token response for an authorization code, authenticating as `client_id`
        with a `private_key_jwt` assertion. A refused code raises `KeycloakError`."""
        resp = await self._request(
            "POST",
            f"{self._realm}/protocol/openid-connect/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_assertion_type": ASSERTION_TYPE,
                "client_assertion": grants.client_assertion(client_id),
            },
        )
        if resp.status_code != 200:
            raise KeycloakError(f"code redemption for {client_id} answered {resp.status_code}")
        return dict(resp.json())


def default() -> KeycloakAdmin:
    """The realm named by the settings. Raises if `KEYCLOAK_URL` is not set."""
    settings = get_settings()
    if not settings.keycloak_url:
        raise RuntimeError("KEYCLOAK_URL is required")
    return KeycloakAdmin(
        settings.keycloak_url, settings.kc_onboarding_secret, settings.agent_signing_cert
    )


async def admin_token() -> str:
    return await default().admin_token()


async def create_user(
    username: str, display_name: str, attributes: dict[str, list[str]] | None = None
) -> str:
    return await default().create_user(username, display_name, attributes)


async def create_agent_client(agent_id: str, template: str) -> str:
    return await default().create_agent_client(agent_id, template)


async def magic_link(
    username: str, redirect_uri: str, state: str, expiration_s: int = MAGIC_LINK_SECONDS
) -> str:
    return await default().magic_link(username, redirect_uri, state, expiration_s)


async def revoke_consent(keycloak_user_id: str, client_id: str) -> None:
    await default().revoke_consent(keycloak_user_id, client_id)


async def username(keycloak_user_id: str) -> str:
    return await default().username(keycloak_user_id)


async def redeem_code(client_id: str, code: str, redirect_uri: str) -> dict[str, Any]:
    return await default().redeem_code(client_id, code, redirect_uri)
