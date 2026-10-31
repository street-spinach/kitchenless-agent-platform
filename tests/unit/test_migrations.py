"""Migrations 0001-0003 apply in order to an empty database (issue #25)."""

import uuid

import psycopg
import pytest

from migrations.apply import apply

pytestmark = pytest.mark.db

TABLES = {
    "agent_traces",
    "users",
    "organizations",
    "households",
    "household_members",
    "member_constraints",
    "budgets",
    "approval_rules",
    "household_addresses",
    "channel_identities",
    "agent_cards",
    "business_rules",
}


def _card(conn: psycopg.Connection, agent_id: str, cls: str, host: str) -> None:
    conn.execute(
        "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
        " VALUES (%s, %s, 'system', 'us', %s)",
        (agent_id, cls, host),
    )


def test_applies_in_order_on_empty_database(empty_schema_dsn: str) -> None:
    done = apply(empty_schema_dsn)

    assert done[:3] == [
        "0001_agent_traces.sql",
        "0002_core_entities.sql",
        "0003_agent_directory.sql",
    ]
    assert apply(empty_schema_dsn) == []
    with psycopg.connect(empty_schema_dsn) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
        ).fetchall()
    assert TABLES <= {r[0] for r in rows}


def test_every_new_table_has_created_at(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND column_name = 'created_at'"
        ).fetchall()
    assert TABLES - {"agent_traces"} <= {r[0] for r in rows}


def test_agent_cards_accept_external(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        _card(conn, "external:a2a-demo", "external", "external")
        _card(conn, "channel:whatsapp", "channel", "api")
        row = conn.execute(
            "SELECT zone_ids, config, enabled FROM agent_cards WHERE agent_id = 'external:a2a-demo'"
        ).fetchone()
    assert row == ([], {}, True)


@pytest.mark.parametrize(("cls", "host"), [("courier", "agents"), ("grocery", "mcp")])
def test_agent_cards_reject_unknown_class_or_host(pg: str, cls: str, host: str) -> None:
    with psycopg.connect(pg) as conn, pytest.raises(psycopg.errors.CheckViolation):
        _card(conn, "x:1", cls, host)


@pytest.mark.parametrize(
    ("kind", "value", "ok"),
    [
        ("allergy", "tree_nuts", True),
        ("allergy", "nuts", False),
        ("diet", "vegetarian", True),
        ("diet", "paleo", False),
        ("dislike", "olives", True),
        ("taste", "sweet", False),
    ],
)
def test_member_constraints_use_fixed_codes(pg: str, kind: str, value: str, ok: bool) -> None:
    user_id = uuid.uuid4()
    with psycopg.connect(pg) as conn:
        conn.execute("INSERT INTO users (user_id, display_name) VALUES (%s, 'A')", (user_id,))
        insert = "INSERT INTO member_constraints (user_id, kind, value) VALUES (%s, %s, %s)"
        if ok:
            conn.execute(insert, (user_id, kind, value))
        else:
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(insert, (user_id, kind, value))
