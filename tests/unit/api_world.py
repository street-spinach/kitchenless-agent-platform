"""Shared rows and helpers for the `api` ingest, sim channel and runs route tests (#40)."""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from starlette.testclient import TestClient

from services.api import main as api
from services.common import modes, runs
from services.common.config import Settings

HOUSEHOLD_ID = "us-2-eval"
LIVE_HOUSEHOLD_ID = "us-2"
ADMIN = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
MEMBER = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
LIVE_ADMIN = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
ADMIN_PHONE = "+15550100201"
MEMBER_PHONE = "+15550100202"
REAL_PHONE = "+15550100301"
UNKNOWN_PHONE = "+15550100999"
ADMIN_CONSUMER = "consumer:us-2-a-eval"
MEMBER_CONSUMER = "consumer:us-2-b-eval"
WHATSAPP = "channel:whatsapp"


def seed(pg: str) -> None:
    """Two households: `us-2-eval` (sim phones) and live `us-2` (one real phone)."""
    with psycopg.connect(pg) as conn:
        for hid, env in ((HOUSEHOLD_ID, "eval"), (LIVE_HOUSEHOLD_ID, "live")):
            conn.execute(
                "INSERT INTO households"
                " (household_id, name, env, region, zone_id, timezone, currency)"
                " VALUES (%s, 'Test', %s, 'us', 'us-oh-45202', 'America/New_York', 'USD')",
                (hid, env),
            )
        people = (
            (MEMBER, HOUSEHOLD_ID, "member", MEMBER_PHONE, "sim", MEMBER_CONSUMER),
            (ADMIN, HOUSEHOLD_ID, "admin", ADMIN_PHONE, "sim", ADMIN_CONSUMER),
            (LIVE_ADMIN, LIVE_HOUSEHOLD_ID, "admin", REAL_PHONE, "real", "consumer:us-2-a"),
        )
        for user_id, hid, role, phone, mode, consumer in people:
            conn.execute(
                "INSERT INTO users (user_id, display_name) VALUES (%s, 'Member')", (user_id,)
            )
            conn.execute(
                "INSERT INTO household_members (household_id, user_id, role) VALUES (%s, %s, %s)",
                (hid, user_id, role),
            )
            conn.execute(
                "INSERT INTO channel_identities (phone_e164, user_id, channel_mode)"
                " VALUES (%s, %s, %s)",
                (phone, user_id, mode),
            )
            _card(conn, consumer, "consumer", f"user:{user_id}", "agents")
        _card(conn, f"household:{HOUSEHOLD_ID}", "household", f"household:{HOUSEHOLD_ID}", "agents")
        _card(conn, WHATSAPP, "channel", "system", "api")


def _card(conn: psycopg.Connection, agent_id: str, cls: str, owner: str, host: str) -> None:
    conn.execute(
        "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
        " VALUES (%s, %s, %s, 'us', %s)",
        (agent_id, cls, owner, host),
    )


def patch_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {
        "SERVICE_NAME": "migrate",
        "DATABASE_URL": "x",
        "LOG_LEVEL": "info",
        "OPENFGA_PRESHARED_KEY": "x",
        "OPENFGA_API_URL": "x",
    }
    settings = Settings.from_env(env)
    monkeypatch.setattr(runs, "get_settings", lambda: settings)
    monkeypatch.setattr(modes, "get_settings", lambda: settings)


def run[T](pg: str, fn: Callable[[psycopg.AsyncConnection], Awaitable[T]]) -> T:
    async def go() -> T:
        async with await psycopg.AsyncConnection.connect(pg) as conn:
            return await fn(conn)

    return asyncio.run(go())


def query(pg: str, sql: str, *params: Any) -> list[dict[str, Any]]:
    with psycopg.connect(pg) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def connect_to(pg: str) -> Callable[[], contextlib.AbstractAsyncContextManager[Any]]:
    @contextlib.asynccontextmanager
    async def connect() -> AsyncIterator[psycopg.AsyncConnection]:
        async with await psycopg.AsyncConnection.connect(pg) as conn:
            yield conn

    return connect


def client(pg: str) -> TestClient:
    return TestClient(api.create_app(connect_to(pg)))
