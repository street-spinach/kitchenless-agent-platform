"""The Keycloak realm import file (identity-authz I19, I27; issue #121)."""

import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from services.common import ids

REPO = Path(__file__).resolve().parents[2]
REALM = REPO / "services/identity/realm/kitchenless.json"
DATA = REPO / "services/seed/data"

CERT = "${AGENT_SIGNING_CERT}"
CALLBACK = "https://${DOMAIN}/me/callback"
AUDIENCE_MAPPER = "oidc-audience-mapper"
CONSUMER_SCOPES = {"constraints:read", "pantry:read", "meals:plan"}
HOUSEHOLD_SCOPES = {
    "constraints:read",
    "pantry:read",
    "pantry:write",
    "orders:place",
    "approvals:request",
}
# The only values that may hold a secret, and the placeholder each must be.
SECRET_KEYS = {"secret", "value", "jwt.credential.certificate", "client_secret", "password"}
PLACEHOLDER = re.compile(r"^\$\{[A-Z][A-Z0-9_]*\}$")


def _yaml(name: str) -> Any:
    return yaml.safe_load((DATA / name).read_text())


def _walk(obj: Any, path: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield f"{path}.{k}", k, v
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")


def _audiences(client: dict[str, Any]) -> set[str]:
    return {
        m["config"]["included.client.audience"]
        for m in client.get("protocolMappers", [])
        if m["protocolMapper"] == AUDIENCE_MAPPER
    }


def test_realm_file_shape() -> None:
    raw = REALM.read_text()
    realm = json.loads(raw)
    households = _yaml("households.yaml")["households"]
    agents = _yaml("agents.yaml")
    clients = {c["clientId"]: c for c in realm["clients"]}

    # Every §3 client is present: one per live people agent, the templates, one per business
    # agent card (I-D23), and the fixed service, page, app and audience clients.
    people = [
        ids.keycloak_client_id(f"household:{hh['household_id']}")
        for hh in households
        if hh["env"] == "live" and hh["members"]
    ] + [
        ids.keycloak_client_id(f"consumer:{m['member_id']}")
        for hh in households
        if hh["env"] == "live"
        for m in hh["members"]
    ]
    templates = ["template.consumer", "template.household"]
    business = [ids.keycloak_client_id(c["agent_id"]) for c in agents["business"]]
    fixed = ["svc.api", "svc.onboarding", "me-pages", "owner-app", "kitchenless-mcp"]
    missing = set(people + templates + business + fixed) - set(clients)
    assert not missing, f"realm is missing clients {sorted(missing)}"

    # People agents and their templates: consent, a service account (the actor token),
    # standard token exchange, the MCP audience and the shared signing certificate.
    for cid in people + templates:
        c = clients[cid]
        assert c["consentRequired"] is True, cid
        assert c["serviceAccountsEnabled"] is True, cid
        assert c["publicClient"] is False, cid
        assert c["clientAuthenticatorType"] == "client-jwt", cid
        assert c["attributes"]["standard.token.exchange.enabled"] == "true", cid
        assert c["attributes"]["jwt.credential.certificate"] == CERT, cid
        assert _audiences(c) == {"kitchenless-mcp"}, cid
        assert c["redirectUris"] == [CALLBACK], cid
        assert {"offline_access", "delegation:client"} <= set(c["optionalClientScopes"]), cid
        want = (
            HOUSEHOLD_SCOPES
            if cid.startswith(("household.", "template.household"))
            else (CONSUMER_SCOPES)
        )
        assert set(c["defaultClientScopes"]) & (CONSUMER_SCOPES | HOUSEHOLD_SCOPES) == want, cid
    for cid in templates:
        assert clients[cid]["enabled"] is False, cid
    for cid in people:
        assert clients[cid]["enabled"] is True, cid

    # Business agents and svc.api: service account only, private_key_jwt, MCP audience.
    for cid in business + ["svc.api"]:
        c = clients[cid]
        assert c["serviceAccountsEnabled"] is True, cid
        assert c["standardFlowEnabled"] is False, cid
        assert c.get("consentRequired") is False, cid
        assert c["clientAuthenticatorType"] == "client-jwt", cid
        assert c["attributes"]["jwt.credential.certificate"] == CERT, cid
        assert _audiences(c) == {"kitchenless-mcp"}, cid
    assert clients["me-pages"]["attributes"]["jwt.credential.certificate"] == CERT
    assert clients["me-pages"]["redirectUris"] == [CALLBACK]
    assert clients["owner-app"]["publicClient"] is True
    assert clients["owner-app"]["attributes"]["pkce.code.challenge.method"] == "S256"
    assert _audiences(clients["owner-app"]) == {"owner-app"}
    assert clients["svc.onboarding"]["secret"] == "${KC_ONBOARDING_SECRET}"

    # §6 scopes exist; VERIFY_PROFILE is off; events on; FGAP v2 delegate policy and
    # permission name every people-agent client.
    scopes = {s["name"] for s in realm["clientScopes"]}
    assert CONSUMER_SCOPES | HOUSEHOLD_SCOPES | {"basic", "delegation:client"} <= scopes
    actions = {a["alias"]: a for a in realm["requiredActions"]}
    assert actions["VERIFY_PROFILE"]["enabled"] is False
    assert realm["eventsEnabled"] is True and realm["adminEventsEnabled"] is True
    assert realm["eventsExpiration"] == 30 * 86400
    assert realm["adminPermissionsEnabled"] is True
    policies = {
        p["name"]: p for p in clients["admin-permissions"]["authorizationSettings"]["policies"]
    }
    assert set(json.loads(policies["agents-may-delegate"]["config"]["clients"])) == set(people)
    perm = policies["agents-delegate-users"]["config"]
    assert json.loads(perm["scopes"]) == ["delegate"]
    assert json.loads(perm["resources"]) == ["Users"]
    assert json.loads(perm["applyPolicies"]) == ["agents-may-delegate"]

    # No literal secrets: every secret-bearing value is a ${VAR} placeholder, no PEM text,
    # and never the `${env.VAR}` form (Keycloak leaves it as literal text, spike #111).
    assert "${env." not in raw
    assert "BEGIN" not in raw and "PRIVATE KEY" not in raw
    for path, key, value in _walk(realm):
        if key in SECRET_KEYS and isinstance(value, str):
            assert PLACEHOLDER.match(value), f"{path} is not a placeholder"

    # Seeded users: no `:` in usernames, the default roles, and the seed's fixed ids.
    users = realm["users"]
    for u in users:
        assert ":" not in u["username"], u["username"]
        assert u["realmRoles"] == ["default-roles-kitchenless"], u["username"]
    seeded = {u["id"]: u for u in users if "id" in u}
    want_ids = {m["keycloak_user_id"] for hh in households for m in hh["members"]} | {
        o["keycloak_user_id"] for o in agents["owners"]
    }
    assert set(seeded) == want_ids
    assert all(str(UUID(i)) == i for i in want_ids)
    members = [m for hh in households for m in hh["members"]]
    assert len({m["keycloak_user_id"] for m in members}) == len(members)

    # Usernames (§4): us-1 from SEED_US1_PHONE_<n>, everyone else a fixed non-phone name.
    for hh in households:
        for n, m in enumerate(hh["members"], start=1):
            want = (
                f"${{SEED_US1_PHONE_{n}}}"
                if hh.get("phones_from_env")
                else f"sim.{hh['household_id']}.{n}"
            )
            assert seeded[m["keycloak_user_id"]]["username"] == want
    for o in agents["owners"]:
        owner = seeded[o["keycloak_user_id"]]
        assert owner["username"] == f"owner.{o['org_id']}"
        assert owner["credentials"] == [
            {"type": "password", "value": "${OWNER_DEMO_PASSWORD}", "temporary": False}
        ]
    assert {o["org_id"] for o in agents["owners"]} == {
        org["org_id"] for org in agents["organizations"]
    }
