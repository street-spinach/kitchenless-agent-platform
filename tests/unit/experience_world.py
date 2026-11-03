"""Rows for the experience metric tests: the two views (EX-I21) and the panel (EX-I22)."""

import uuid
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


def household(conn: psycopg.Connection, household_id: str, tz: str = "America/New_York") -> None:
    conn.execute(
        "INSERT INTO households (household_id, name, env, region, zone_id, timezone, currency)"
        " VALUES (%s, 'Test', 'eval', 'us', 'us-oh-45202', %s, 'USD')",
        (household_id, tz),
    )


def person(
    conn: psycopg.Connection, household_id: str, name: str, role: str = "admin"
) -> uuid.UUID:
    user_id = uuid.uuid4()
    conn.execute("INSERT INTO users (user_id, display_name) VALUES (%s, %s)", (user_id, name))
    conn.execute(
        "INSERT INTO household_members (household_id, user_id, role) VALUES (%s, %s, %s)",
        (household_id, user_id, role),
    )
    return user_id


def run(conn: psycopg.Connection, household_id: str | None) -> uuid.UUID:
    run_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO runs (run_id, household_id, kind, trigger, modes)"
        " VALUES (%s, %s, 'chat', %s, %s)",
        (run_id, household_id, Jsonb({"source": "test"}), Jsonb({})),
    )
    return run_id


def message(
    conn: psycopg.Connection,
    run_id: uuid.UUID,
    payloads: list[str],
    message_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """A `whatsapp_out` message with one button per payload."""
    message_id = message_id or uuid.uuid4()
    body: dict[str, Any] = {
        "key": "test",
        "buttons": [{"payload": p, "title": f"b{n}"} for n, p in enumerate(payloads)],
    }
    conn.execute(
        "INSERT INTO agent_messages"
        " (message_id, run_id, from_agent, to_agent, to_host, type, body, status)"
        " VALUES (%s, %s, 'system:api', 'channel:whatsapp', 'api', 'whatsapp_out', %s,"
        " 'processed')",
        (message_id, run_id, Jsonb(body)),
    )
    return message_id


def send(
    conn: psycopg.Connection,
    user_id: uuid.UUID,
    at: datetime,
    payloads: list[str],
    *,
    outcome: str = "sent",
    status: str | None = None,
    household_id: str | None = None,
    message_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """A message to `user_id` and its `whatsapp_sends` row, created at `at`."""
    message_id = message(conn, run(conn, household_id), payloads, message_id)
    conn.execute(
        "INSERT INTO whatsapp_sends (message_id, user_id, outcome, status, created_at)"
        " VALUES (%s, %s, %s, %s, %s)",
        (message_id, user_id, outcome, status, at),
    )
    return message_id


def held(
    conn: psycopg.Connection, user_id: uuid.UUID, message_id: uuid.UUID, status: str, at: datetime
) -> int:
    cur = conn.execute(
        "INSERT INTO held_messages (message_id, user_id, body, ref_kind, ref_id, status,"
        " created_at) VALUES (%s, %s, '{}', 'approval', 'x', %s, %s) RETURNING held_id",
        (message_id, user_id, status, at),
    )
    row = cur.fetchone()
    assert row is not None
    return int(row[0])


def order(
    conn: psycopg.Connection, household_id: str, state: str, at: datetime
) -> tuple[uuid.UUID, uuid.UUID]:
    """An order in `state`, updated at `at`. Returns `(order_id, run_id)`."""
    address_ref, order_id, run_id = uuid.uuid4(), uuid.uuid4(), run(conn, household_id)
    conn.execute(
        "INSERT INTO household_addresses (address_ref, household_id, line1, city, region,"
        " postal_code, country) VALUES (%s, %s, '1 Main St', 'Cincinnati', 'OH', '45202', 'US')",
        (address_ref, household_id),
    )
    conn.execute(
        "INSERT INTO orders (order_id, run_id, household_id, kind, state, currency, address_ref,"
        " created_at, updated_at) VALUES (%s, %s, %s, 'grocery', %s, 'USD', %s, %s, %s)",
        (order_id, run_id, household_id, state, address_ref, at, at),
    )
    return order_id, run_id


def approval(conn: psycopg.Connection, household_id: str, status: str, at: datetime) -> None:
    order_id, run_id = order(conn, household_id, "awaiting_approval", at)
    conn.execute(
        "INSERT INTO approvals (approval_id, run_id, household_id, order_id, amount_minor,"
        " currency, status, requested_at) VALUES (%s, %s, %s, %s, 4250, 'USD', %s, %s)",
        (uuid.uuid4(), run_id, household_id, order_id, status, at),
    )


def tool_result(conn: psycopg.Connection, household_id: str, refused: bool, at: datetime) -> None:
    conn.execute(
        "INSERT INTO agent_traces (run_id, seq, ts, agent_id, kind, payload)"
        " VALUES (%s, 0, %s, 'household:x', 'tool_result', %s)",
        (run(conn, household_id), at, Jsonb({"tool": "authorize_spend", "refused": refused})),
    )


def audit(conn: psycopg.Connection, user_id: uuid.UUID, kind: str, at: datetime) -> None:
    conn.execute(
        "INSERT INTO audit_events (ts, kind, subject_user_id) VALUES (%s, %s, %s)",
        (at, kind, user_id),
    )
