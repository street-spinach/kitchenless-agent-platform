"""The panel's identity view and revoke button (identity-authz I29, §11; issue #126)."""

import uuid
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from services.identity import keycloak_admin
from tests.unit import api_world as w
from tests.unit import identity_world as iw

pytestmark = pytest.mark.db

AGENT = f"household:{w.HOUSEHOLD_ID}"
SELLER = "grocery:mock-us"


@pytest.fixture(autouse=True)
def _keycloak(monkeypatch: pytest.MonkeyPatch) -> None:
    async def revoke_consent(keycloak_sub: str, client_id: str) -> None:
        return None

    monkeypatch.setattr(keycloak_admin, "revoke_consent", revoke_consent)
    w.patch_settings(monkeypatch)


def _grant(pg: str) -> uuid.UUID:
    w.seed(pg)
    with psycopg.connect(pg) as conn:
        conn.execute("DELETE FROM agent_grants WHERE agent_id = %s", (AGENT,))
        return iw.grant_household(conn, w.HOUSEHOLD_ID, w.ADMIN)


def _identity(pg: str) -> dict[str, Any]:
    response = w.client(pg).get("/panel/identity", params={"household": w.HOUSEHOLD_ID})
    assert response.status_code == 200
    return response.json()


def _placed_order(pg: str) -> uuid.UUID:
    order_id, run_id, address = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, household_id, kind, trigger, modes)"
            " VALUES (%s, %s, 'weekly', %s, '{}')",
            (run_id, w.HOUSEHOLD_ID, Jsonb({"source": "test"})),
        )
        conn.execute(
            "INSERT INTO household_addresses"
            " (address_ref, household_id, line1, city, region, postal_code, country)"
            " VALUES (%s, %s, '1 Main St', 'Cincinnati', 'OH', '45202', 'US')",
            (address, w.HOUSEHOLD_ID),
        )
        conn.execute(
            "INSERT INTO orders (order_id, run_id, household_id, kind, state, currency,"
            " address_ref) VALUES (%s, %s, %s, 'grocery', 'placed', 'USD', %s)",
            (order_id, run_id, w.HOUSEHOLD_ID, address),
        )
        conn.execute(
            "INSERT INTO agent_traces (run_id, seq, agent_id, kind, payload)"
            " VALUES (%s, 1, %s, 'tool_call', %s)",
            (
                run_id,
                AGENT,
                Jsonb(
                    {
                        "server": "household",
                        "tool": "authorize_spend",
                        "args": {"secret_arg": "not shown"},
                        "idempotency_key": None,
                        "sub_user_id": str(w.ADMIN),
                        "act_agent_id": AGENT,
                        "grant_id": None,
                        "token_source": "mock",
                        "authz": {"relation": "can_spend", "allowed": True},
                    }
                ),
            ),
        )
    return order_id


def test_panel_revoke_audited_via_panel(pg: str) -> None:
    grant_id = _grant(pg)
    response = w.client(pg).post(f"/panel/grants/{grant_id}/revoke")
    assert response.status_code == 200
    [grant] = w.query(
        pg, "SELECT status, revoked_by FROM agent_grants WHERE grant_id = %s", grant_id
    )
    assert grant == {"status": "revoked", "revoked_by": None}
    [audit] = w.query(
        pg,
        "SELECT actor_user_id, detail FROM audit_events"
        " WHERE kind = 'consent_revoked' AND grant_id = %s",
        grant_id,
    )
    assert audit["actor_user_id"] is None
    assert audit["detail"]["via"] == "panel"
    # §11 step 3: the notice goes out, as for a person's revoke.
    assert w.query(pg, "SELECT 1 FROM agent_messages WHERE body->>'template' = 'consent_revoked'")


def test_panel_revoke_refusals(pg: str) -> None:
    grant_id = _grant(pg)
    client = w.client(pg)
    assert client.post("/panel/grants/nope/revoke").status_code == 400
    assert client.post(f"/panel/grants/{uuid.uuid4()}/revoke").status_code == 404
    assert client.post(f"/panel/grants/{grant_id}/revoke").status_code == 200
    assert client.post(f"/panel/grants/{grant_id}/revoke").status_code == 409


def test_page_has_revoke_button_per_active_grant_only(pg: str) -> None:
    grant_id = _grant(pg)
    client = w.client(pg)
    headers = {"accept": "text/html"}
    params = {"household": w.HOUSEHOLD_ID}
    action = f"/panel/grants/{grant_id}/revoke"
    assert action in client.get("/panel/identity", params=params, headers=headers).text
    client.post(action)
    page = client.get("/panel/identity", params=params, headers=headers).text
    assert action not in page
    assert "re-consent needed" in page


def test_revoked_grant_flags_reconsent_and_placed_order(pg: str) -> None:
    grant_id = _grant(pg)
    order_id = _placed_order(pg)
    before = _identity(pg)
    assert before["orders"] == []
    [row] = [g for g in before["grants"] if g["grant_id"] == str(grant_id)]
    assert row["flags"] == []

    assert w.client(pg).post(f"/panel/grants/{grant_id}/revoke").status_code == 200
    after = _identity(pg)
    [row] = [g for g in after["grants"] if g["grant_id"] == str(grant_id)]
    assert row["flags"] == ["re-consent needed"]
    assert after["orders"] == [
        {"order_id": str(order_id), "state": "placed", "flag": "placed before revoke"}
    ]


def test_claims_view_shows_identity_fields_only(pg: str) -> None:
    _grant(pg)
    _placed_order(pg)
    [call] = _identity(pg)["last_guarded_calls"]
    assert call["sub_user_id"] == str(w.ADMIN)
    assert call["act_agent_id"] == AGENT
    assert call["authz"]["relation"] == "can_spend"
    assert "not shown" not in str(call)


def test_unknown_household(pg: str) -> None:
    client = w.client(pg)
    assert client.get("/panel/identity").status_code == 400
    assert client.get("/panel/identity", params={"household": "nope"}).status_code == 404
