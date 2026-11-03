"""Keycloak Admin API calls (identity-authz §3, §6, §11 and I22), HTTP mocked."""

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from services.identity import keycloak_admin as ka
from services.identity.errors import IdentityUnavailable

BASE = "http://keycloak:8080/auth"
ADMIN = f"{BASE}/admin/realms/kitchenless"
CERT = "MIIBfakeCERTbase64DER"
TEMPLATE_UUID = "11111111-1111-4111-8111-111111111111"
NEW_UUID = "22222222-2222-4222-8222-222222222222"
FGAP_UUID = "33333333-3333-4333-8333-333333333333"
POLICY_ID = "44444444-4444-4444-8444-444444444444"
EXISTING_AGENT = "55555555-5555-4555-8555-555555555555"

Handler = Callable[[httpx.Request], httpx.Response | None]


class FakeKeycloak:
    """Answers the token endpoint itself and every other request from `routes`, keyed by
    `(method, path)`. Records every request."""

    def __init__(self, routes: dict[tuple[str, str], Handler]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/auth/realms/kitchenless/protocol/openid-connect/token":
            return httpx.Response(200, json={"access_token": "admin-token", "expires_in": 300})
        handler = self.routes.get((request.method, request.url.path))
        if handler is None:
            return httpx.Response(404)
        resp = handler(request)
        return resp if resp is not None else httpx.Response(204)

    def admin(self) -> ka.KeycloakAdmin:
        return ka.KeycloakAdmin(
            BASE, "onboarding-secret", CERT, transport=httpx.MockTransport(self)
        )

    def sent(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if (r.method, r.url.path) == (method, path)]


def _body(request: httpx.Request) -> Any:
    return json.loads(request.content)


TEMPLATE = {
    "id": TEMPLATE_UUID,
    "clientId": "template.household",
    "name": "template.household",
    "enabled": False,
    "clientAuthenticatorType": "client-jwt",
    "consentRequired": True,
    "attributes": {
        "jwt.credential.certificate": "old",
        "standard.token.exchange.enabled": "true",
    },
    "protocolMappers": [{"id": "m-1", "name": "audience kitchenless-mcp"}],
    "optionalClientScopes": ["offline_access", "delegation:client"],
}


def _client_routes(policy: dict[str, Any]) -> dict[tuple[str, str], Handler]:
    def clients(request: httpx.Request) -> httpx.Response:
        wanted = request.url.params["clientId"]
        found = {
            "template.household": [TEMPLATE],
            "admin-permissions": [{"id": FGAP_UUID, "clientId": "admin-permissions"}],
        }.get(wanted, [])
        return httpx.Response(200, json=found)

    def put_policy(request: httpx.Request) -> httpx.Response:
        policy.update(_body(request))
        return httpx.Response(201)

    base = f"/auth/admin/realms/kitchenless/clients/{FGAP_UUID}/authz/resource-server/policy"
    return {
        ("GET", "/auth/admin/realms/kitchenless/clients"): clients,
        ("POST", "/auth/admin/realms/kitchenless/clients"): lambda r: httpx.Response(
            201, headers={"Location": f"{ADMIN}/clients/{NEW_UUID}"}
        ),
        ("GET", base): lambda r: httpx.Response(
            200, json=[{"id": POLICY_ID, "name": "agents-may-delegate", "type": "client"}]
        ),
        ("GET", f"{base}/client/{POLICY_ID}"): lambda r: httpx.Response(200, json=dict(policy)),
        ("PUT", f"{base}/client/{POLICY_ID}"): put_policy,
    }


def test_create_user_409_returns_existing_id() -> None:
    fake = FakeKeycloak(
        {
            ("POST", "/auth/admin/realms/kitchenless/users"): lambda r: httpx.Response(409),
            ("GET", "/auth/admin/realms/kitchenless/users"): lambda r: httpx.Response(
                200, json=[{"id": "kc-user-1", "username": "+15135550101"}]
            ),
        }
    )
    user_id = asyncio.run(fake.admin().create_user("+15135550101", "Maya", {"k": ["v"]}))
    assert user_id == "kc-user-1"
    [created] = fake.sent("POST", "/auth/admin/realms/kitchenless/users")
    assert _body(created) == {
        "username": "+15135550101",
        "enabled": True,
        "firstName": "Maya",
        "attributes": {"k": ["v"]},
    }
    assert created.headers["Authorization"] == "Bearer admin-token"
    [lookup] = fake.sent("GET", "/auth/admin/realms/kitchenless/users")
    assert dict(lookup.url.params) == {"username": "+15135550101", "exact": "true"}
    # The admin token is `client_credentials` for `svc.onboarding`.
    token = fake.requests[0]
    assert b"grant_type=client_credentials" in token.content
    assert b"client_id=svc.onboarding" in token.content

    # A new user's id comes from the Location header.
    fake.routes[("POST", "/auth/admin/realms/kitchenless/users")] = lambda r: httpx.Response(
        201, headers={"Location": f"{ADMIN}/users/kc-user-2"}
    )
    assert asyncio.run(fake.admin().create_user("child.hh-1.1", "Sam")) == "kc-user-2"


def test_create_agent_client_sets_certificate() -> None:
    policy: dict[str, Any] = {"id": POLICY_ID, "name": "agents-may-delegate", "clients": []}
    fake = FakeKeycloak(_client_routes(policy))
    kc_uuid = asyncio.run(
        fake.admin().create_agent_client("household:hh-abc", "template.household")
    )
    assert kc_uuid == NEW_UUID
    [created] = fake.sent("POST", "/auth/admin/realms/kitchenless/clients")
    rep = _body(created)
    assert (rep["clientId"], rep["name"], rep["enabled"]) == (
        "household.hh-abc",
        "household.hh-abc",
        True,
    )
    assert rep["attributes"] == {
        "jwt.credential.certificate": CERT,
        "standard.token.exchange.enabled": "true",
    }
    # A copy of the template, without the template's ids.
    assert "id" not in rep and all("id" not in m for m in rep["protocolMappers"])
    assert (rep["consentRequired"], rep["clientAuthenticatorType"]) == (True, "client-jwt")
    assert rep["optionalClientScopes"] == TEMPLATE["optionalClientScopes"]
    assert TEMPLATE["enabled"] is False  # the template itself is untouched


def test_create_agent_client_joins_delegate_policy() -> None:
    policy: dict[str, Any] = {
        "id": POLICY_ID,
        "name": "agents-may-delegate",
        "type": "client",
        "clients": [EXISTING_AGENT],
    }
    fake = FakeKeycloak(_client_routes(policy))
    asyncio.run(fake.admin().create_agent_client("consumer:hh-abc.1", "template.household"))
    base = f"/auth/admin/realms/kitchenless/clients/{FGAP_UUID}/authz/resource-server/policy"
    [lookup] = fake.sent("GET", base)
    assert lookup.url.params["name"] == "agents-may-delegate"
    [put] = fake.sent("PUT", f"{base}/client/{POLICY_ID}")
    assert _body(put)["clients"] == [EXISTING_AGENT, NEW_UUID]
    assert policy["clients"] == [EXISTING_AGENT, NEW_UUID]

    # A retry finds the client and the policy membership, and changes nothing.
    existing = {"id": NEW_UUID, "clientId": "consumer.hh-abc.1"}
    clients = fake.routes[("GET", "/auth/admin/realms/kitchenless/clients")]
    fake.routes[("GET", "/auth/admin/realms/kitchenless/clients")] = lambda r: (
        httpx.Response(200, json=[existing])
        if r.url.params["clientId"] == "consumer.hh-abc.1"
        else clients(r)
    )
    fake.requests.clear()
    again = asyncio.run(fake.admin().create_agent_client("consumer:hh-abc.1", "template.household"))
    assert again == NEW_UUID
    assert fake.sent("POST", "/auth/admin/realms/kitchenless/clients") == []
    assert fake.sent("PUT", f"{base}/client/{POLICY_ID}") == []


def test_magic_link_single_use_body() -> None:
    def magic(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"user_id": "kc-1", "link": "https://kc/link", "sent": False}
        )

    fake = FakeKeycloak({("POST", "/auth/realms/kitchenless/magic-link"): magic})
    link = asyncio.run(
        fake.admin().magic_link("+15135550101", "https://kitchenless.test/me/callback", "st")
    )
    assert link == "https://kc/link"
    [call] = fake.sent("POST", "/auth/realms/kitchenless/magic-link")
    assert call.headers["Authorization"] == "Bearer admin-token"
    assert _body(call) == {
        "username": "+15135550101",
        "client_id": "me-pages",
        "redirect_uri": "https://kitchenless.test/me/callback",
        "expiration_seconds": 120,
        "reusable": False,
        "state": "st",
        "scope": "openid",
    }


def test_revoke_consent_and_outage() -> None:
    path = "/auth/admin/realms/kitchenless/users/kc-1/consents/household.us-1"
    fake = FakeKeycloak({("DELETE", path): lambda r: httpx.Response(204)})
    asyncio.run(fake.admin().revoke_consent("kc-1", "household.us-1"))
    assert len(fake.sent("DELETE", path)) == 1
    fake.routes[("DELETE", path)] = lambda r: httpx.Response(503)
    with pytest.raises(IdentityUnavailable):
        asyncio.run(fake.admin().revoke_consent("kc-1", "household.us-1"))
    fake.routes[("DELETE", path)] = lambda r: httpx.Response(403)
    with pytest.raises(ka.KeycloakError):
        asyncio.run(fake.admin().revoke_consent("kc-1", "household.us-1"))
