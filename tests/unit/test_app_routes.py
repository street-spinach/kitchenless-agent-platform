"""The owner app's `/app/rules` route (identity-authz §10, I32). Owner tokens come from
`MockSource.owner_token`; OpenFGA is a set of tuples behind `fga.check`."""

import time
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from starlette.testclient import TestClient

from services.api import main as api
from services.identity import claims as c
from services.identity import fga, token_source
from services.identity.errors import AuthzUnavailable
from tests.unit import api_world as aw

db = pytest.mark.db

ORG = "square-restaurant-us"
OTHER_ORG = "mock-restaurant-us"
RESTAURANT = "restaurant:square-us"
OTHER_RESTAURANT = "restaurant:mock-us"
FLOORS = {"SQ-DISH": 1140}


class FakeFga:
    def __init__(self) -> None:
        self.tuples: set[tuple[str, str, str]] = set()
        self.checks: list[tuple[str, str, str]] = []
        self.fail = False

    def check(self, user: str, relation: str, object: str) -> tuple[bool, str]:
        self.checks.append((user, relation, object))
        if self.fail:
            raise AuthzUnavailable("timeout")
        return (user, relation, object) in self.tuples, "model-1"


@pytest.fixture
def owner_fga(monkeypatch: pytest.MonkeyPatch) -> FakeFga:
    fake = FakeFga()
    monkeypatch.setattr(fga, "check", fake.check)
    return fake


def _owner_sub(org_id: str) -> str:
    return f"owner.{org_id}"  # `MockSource.owner_token`'s subject


def _token(org_id: str = ORG) -> str:
    return token_source.MockSource().owner_token(org_id)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def world(pg: str, owner_fga: FakeFga) -> str:
    with psycopg.connect(pg) as conn:
        for org_id in (ORG, OTHER_ORG):
            conn.execute(
                "INSERT INTO organizations (org_id, name, region) VALUES (%s, %s, 'us')",
                (org_id, org_id),
            )
        for agent_id, org_id in ((RESTAURANT, ORG), (OTHER_RESTAURANT, OTHER_ORG)):
            conn.execute(
                "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
                " VALUES (%s, 'restaurant', %s, 'us', 'agents')",
                (agent_id, f"org:{org_id}"),
            )
            conn.execute(
                "INSERT INTO business_rules (agent_id, kind, value) VALUES (%s, 'floors', %s)",
                (agent_id, Jsonb(FLOORS)),
            )
    owner_fga.tuples.add((f"user:{_owner_sub(ORG)}", "owner", f"org:{ORG}"))
    return pg


def _client(pg: str) -> TestClient:
    return TestClient(api.create_app(aw.connect_to(pg)))


def _rules(pg: str) -> dict[tuple[str, str], Any]:
    rows = aw.query(pg, "SELECT agent_id, kind, value FROM business_rules")
    return {(r["agent_id"], r["kind"]): r["value"] for r in rows}


def _audits(pg: str) -> list[dict[str, Any]]:
    return aw.query(pg, "SELECT * FROM audit_events WHERE kind = 'owner_rule_changed'")


@db
def test_owner_rule_requires_owner(world: str, owner_fga: FakeFga) -> None:
    client = _client(world)
    url = f"/app/rules?org_id={ORG}"
    body = {"max_discount_pct": 4}
    # No token, a bad token, and a token for another audience are refused before OpenFGA.
    assert client.post(url, json=body).status_code == 401
    assert client.post(url, json=body, headers=_auth("not-a-jwt")).status_code == 401
    now = time.time()
    mcp_token = token_source.sign_mock(
        token_source.mock_claims(
            sub=_owner_sub(ORG), client_id="owner-app", delegated=False, now=now
        )
    )
    resp = client.post(url, json=body, headers=_auth(mcp_token))
    assert (resp.status_code, resp.json()["error"]) == (401, c.TOKEN_INVALID_AUD)
    expired = token_source.sign_mock(
        token_source.mock_claims(
            sub=_owner_sub(ORG),
            client_id="owner-app",
            delegated=False,
            now=now - 3600,
            audience=c.OWNER_AUDIENCE,
        )
    )
    resp = client.post(url, json=body, headers=_auth(expired))
    assert (resp.status_code, resp.json()["error"]) == (401, c.TOKEN_EXPIRED)
    assert owner_fga.checks == []

    # A valid owner token for another org: `user owner org:<o>` fails, nothing changes.
    other = _auth(_token(OTHER_ORG))
    resp = client.post(url, json=body, headers=other)
    assert (resp.status_code, resp.json()["error"]) == (403, c.AUTHZ_DENIED)
    assert client.get(url, headers=other).status_code == 403
    assert owner_fga.checks[-1] == (f"user:{_owner_sub(OTHER_ORG)}", "owner", f"org:{ORG}")
    # An OpenFGA outage is not a pass.
    owner_fga.fail = True
    assert client.post(url, json=body, headers=_auth(_token())).status_code == 503
    owner_fga.fail = False
    assert (RESTAURANT, "max_discount_pct") not in _rules(world)
    assert _audits(world) == []

    # Out-of-range or malformed bodies, and a missing org, are refused.
    owner = _auth(_token())
    for bad in (
        {"max_discount_pct": 101},
        {"max_discount_pct": -1},
        {"max_discount_pct": "4"},
        {"max_discount_pct": True},
        {"max_discount_pct": 4, "x": 1},
        {},
    ):
        assert client.post(url, json=bad, headers=owner).status_code == 400, bad
    assert client.post("/app/rules", json=body, headers=owner).status_code == 400

    # The owner may.
    resp = client.post(url, json=body, headers=owner)
    assert resp.status_code == 200
    assert resp.json() == {"org_id": ORG, "max_discount_pct": 4}
    assert client.get(url, headers=owner).json() == {"org_id": ORG, "max_discount_pct": 4}
    rules = _rules(world)
    assert rules[(RESTAURANT, "max_discount_pct")] == 4
    assert (OTHER_RESTAURANT, "max_discount_pct") not in rules
    assert rules[(RESTAURANT, "floors")] == FLOORS


@db
def test_owner_rule_audited(world: str) -> None:
    client = _client(world)
    url = f"/app/rules?org_id={ORG}"
    owner = _auth(_token())
    assert client.get(url, headers=owner).json() == {"org_id": ORG, "max_discount_pct": None}
    assert client.post(url, json={"max_discount_pct": 4}, headers=owner).status_code == 200
    assert client.post(url, json={"max_discount_pct": 10}, headers=owner).status_code == 200
    first, second = sorted(_audits(world), key=lambda r: r["event_id"])
    assert first["org_id"] == ORG and first["household_id"] is None
    assert first["detail"] == {
        "rule": "max_discount_pct",
        "from": None,
        "to": 4,
        "agents": [RESTAURANT],
        "owner_sub": _owner_sub(ORG),
    }
    assert (second["detail"]["from"], second["detail"]["to"]) == (4, 10)
    assert _rules(world)[(RESTAURANT, "max_discount_pct")] == 10


@db
def test_other_app_paths_stay_reserved(world: str) -> None:
    resp = _client(world).get("/app/orders/1", headers=_auth(_token()))
    assert resp.status_code == 501
