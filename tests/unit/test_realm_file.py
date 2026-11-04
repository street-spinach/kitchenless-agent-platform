"""The Keycloak realm import file (identity-authz I19, I27; issue #121)."""

import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from services.common import ids
from services.identity.grants import CLASS_SCOPES

REPO = Path(__file__).resolve().parents[2]
REALM = REPO / "services/identity/realm/kitchenless.json"
DATA = REPO / "services/seed/data"

CERT = "${AGENT_SIGNING_CERT}"
CALLBACK = "https://${DOMAIN}/me/callback"
OWNER_REDIRECT = "https://${DOMAIN}/owner/auth"
OWNER_ORIGIN = "https://${DOMAIN}"
AUDIENCE_MAPPER = "oidc-audience-mapper"
# The class scopes the code requests (grants.CLASS_SCOPES): every one must be a default
# scope of its clients, or Keycloak leaves it out of the token and the tool is refused.
CONSUMER_SCOPES = set(CLASS_SCOPES["consumer"])
HOUSEHOLD_SCOPES = set(CLASS_SCOPES["household"])
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


def test_owner_app_redirect_and_origin() -> None:
    """experience.md EX-I44: the owner app signs in by PKCE at /owner/auth, and only there."""
    realm = json.loads(REALM.read_text())
    owner = {c["clientId"]: c for c in realm["clients"]}["owner-app"]
    assert owner["redirectUris"] == [OWNER_REDIRECT]
    assert owner["webOrigins"] == [OWNER_ORIGIN]
    assert owner["publicClient"] is True
    assert owner["standardFlowEnabled"] is True
    assert owner["implicitFlowEnabled"] is False
    assert owner["directAccessGrantsEnabled"] is False
    assert owner["attributes"]["pkce.code.challenge.method"] == "S256"


# showcase.md §3.3: the consent screen's wording for each class scope.
CONSENT_TEXT = {
    "constraints:read": "Read your household's allergies, diets and dislikes",
    "constraints:write": "Save allergies, diets and dislikes you tell it",
    "pantry:read": "See what's at home",
    "pantry:write": "Record what's at home or run out",
    "meals:plan": "Plan meals for you",
    "orders:place": "Place orders within your budget and approval rules",
    "approvals:request": "Ask your approver before spending above the band",
}
THEME = REPO / "services/identity/realm/theme/kitchenless/login"
DOCKERFILE = REPO / "infra/keycloak/Dockerfile"
THEME_COPY = (
    "COPY --chown=keycloak:keycloak services/identity/realm/theme/kitchenless/ "
    "/opt/keycloak/themes/kitchenless/"
)


def test_login_theme_is_kitchenless() -> None:
    """showcase.md SC-A5, §3.3: the realm uses the `kitchenless` login theme the image
    ships; the theme is CSS and images on keycloak.v2, with no template overrides."""
    realm = json.loads(REALM.read_text())
    assert realm["loginTheme"] == "kitchenless"
    props = dict(
        line.split("=", 1)
        for line in (THEME / "theme.properties").read_text().splitlines()
        if line and not line.startswith("#")
    )
    assert props["parent"] == "keycloak.v2"
    assert props["import"] == "common/keycloak"
    assert props["styles"].split() == ["css/styles.css", "css/kitchenless.css"]
    assert not list(THEME.rglob("*.ftl")), "the theme must not override templates (§3.3)"
    assert (THEME / "resources/img/spinach-icon.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert "url(../img/spinach-icon.png)" in (THEME / "resources/css/kitchenless.css").read_text()
    assert THEME_COPY in DOCKERFILE.read_text()


def test_every_class_scope_has_consent_text() -> None:
    """showcase.md §3.3: every scope in grants.CLASS_SCOPES is a realm client scope shown on
    the consent screen with its §3.3 text."""
    from services.identity.grants import CLASS_SCOPES

    realm = json.loads(REALM.read_text())
    scopes = {s["name"]: s for s in realm["clientScopes"]}
    class_scopes = {name for names in CLASS_SCOPES.values() for name in names}
    assert class_scopes == set(CONSENT_TEXT)
    for name in sorted(class_scopes):
        assert name in scopes, f"realm has no client scope {name}"
        attrs = scopes[name]["attributes"]
        assert attrs["display.on.consent.screen"] == "true", name
        assert attrs["consent.screen.text"] == CONSENT_TEXT[name], name
