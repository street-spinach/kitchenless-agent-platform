"""The seed's identity rows: clients, Keycloak ids and mock grants (identity-authz I27)."""

from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from services.common import ids
from services.identity import grants
from services.seed import __main__ as seed_mod

pytestmark = pytest.mark.db

US1_PHONES = ["+15550109901", "+15550109902", "+15550109903"]


def _seed(pg: str, token_source: str = "mock") -> None:
    with psycopg.connect(pg) as conn:
        seed_mod.seed(conn, "all", us1_phones=US1_PHONES, token_source=token_source)


def _rows(pg: str, sql: str, *params: Any) -> list[dict[str, Any]]:
    with psycopg.connect(pg) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _grants(pg: str) -> dict[str, dict[str, Any]]:
    rows = _rows(pg, "SELECT * FROM agent_grants")
    return {r["agent_id"]: r for r in rows}


def test_seed_renews_mock_grants(pg: str) -> None:
    _seed(pg)
    first = _grants(pg)
    households = {r["household_id"] for r in first.values()}
    # Eval households and the simulated us-2 under mock; never the real us-1.
    assert households == {"us-1-eval", "us-2-eval", "us-2"}
    assert all(r["status"] == "active" for r in first.values())
    for agent_id, row in first.items():
        kind = agent_id.partition(":")[0]
        assert row["agent_kind"] == kind
        assert row["scopes"] == list(grants.CLASS_SCOPES[kind])
        assert row["grant_id"] == ids.seed_grant_id(agent_id, row["user_id"])
    # The household grant's holder is the approver; a child's grant is consented by them.
    admin = ids.seed_user_id("us-1-a-eval")
    assert first["household:us-1-eval"]["user_id"] == admin
    child = first["consumer:us-1-c-eval"]
    assert (child["user_id"], child["consented_by"]) == (ids.seed_user_id("us-1-c-eval"), admin)

    # A revoked eval grant, and an aged one, are renewed by the next seed.
    with psycopg.connect(pg) as conn:
        conn.execute(
            "UPDATE agent_grants SET status = 'revoked', revoke_reason = 'user',"
            " revoked_at = now() WHERE agent_id = 'household:us-1-eval'"
        )
        conn.execute(
            "UPDATE agent_grants SET expires_at = now() + interval '1 day'"
            " WHERE agent_id = 'consumer:us-2-a-eval'"
        )
    _seed(pg)
    second = _grants(pg)
    assert set(second) == set(first)  # the same rows, upserted
    renewed = second["household:us-1-eval"]
    assert (renewed["status"], renewed["revoke_reason"], renewed["revoked_at"]) == (
        "active",
        None,
        None,
    )
    assert (
        second["consumer:us-2-a-eval"]["expires_at"] > first["consumer:us-2-a-eval"]["expires_at"]
    )


def test_seed_live_source_grants_eval_households_only(pg: str) -> None:
    _seed(pg, token_source="keycloak")
    households = {r["household_id"] for r in _grants(pg).values()}
    assert households == {"us-1-eval", "us-2-eval"}  # us-2 consents through links


def test_seed_leaves_a_real_consent_alone(pg: str) -> None:
    _seed(pg)
    with psycopg.connect(pg) as conn:
        conn.execute("UPDATE agent_grants SET status = 'revoked'")
        conn.execute(
            "INSERT INTO agent_grants (grant_id, agent_id, agent_kind, user_id, consented_by,"
            " household_id, scopes, refresh_ciphertext, key_version, expires_at)"
            " VALUES (gen_random_uuid(), 'household:us-2', 'household', %s, %s, 'us-2',"
            " '{orders:place}', 'x', 1, now() + interval '30 days')",
            (ids.seed_user_id("us-2-a"), ids.seed_user_id("us-2-a")),
        )
    _seed(pg)  # must not fail on the one-active-household-grant index
    active = _rows(
        pg,
        "SELECT grant_id FROM agent_grants WHERE agent_id = 'household:us-2' AND status = 'active'",
    )
    assert len(active) == 1
    assert active[0]["grant_id"] != ids.seed_grant_id("household:us-2", ids.seed_user_id("us-2-a"))


def test_seed_writes_agent_clients_and_keycloak_ids(pg: str) -> None:
    _seed(pg)
    clients = {r["agent_id"]: r for r in _rows(pg, "SELECT * FROM agent_clients")}
    assert (clients["system:api"]["client_id"], clients["system:api"]["kind"]) == (
        "svc.api",
        "service",
    )
    assert (clients["household:us-1"]["client_id"], clients["household:us-1"]["kind"]) == (
        "household.us-1",
        "people",
    )
    assert clients["consumer:us-1-a"]["kind"] == "people"
    assert (clients["grocery:mock-us"]["client_id"], clients["grocery:mock-us"]["kind"]) == (
        "grocery.mock-us",
        "service",
    )
    cards = {
        r["agent_id"]
        for r in _rows(
            pg,
            "SELECT agent_id FROM agent_cards WHERE class IN"
            " ('consumer', 'household', 'grocery', 'restaurant', 'delivery')",
        )
    }
    assert cards <= set(clients)
    data = seed_mod.load_data()
    for hh in data["households"]:
        for m in hh["members"]:
            [user] = _rows(
                pg,
                "SELECT keycloak_sub FROM users WHERE user_id = %s",
                ids.seed_user_id(m["member_id"]),
            )
            assert user["keycloak_sub"] == str(m["keycloak_user_id"])
