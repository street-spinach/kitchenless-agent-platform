"""Grants: create, load, revoke, expire, encrypt (identity-authz I4, §6, §11)."""

import asyncio
import base64
import types
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from services.identity import grants, tokens
from services.identity.errors import GrantExpired, GrantMissing, GrantRevoked
from tests.unit import identity_world as w

pytestmark = pytest.mark.db

OFFLINE = "offline-token-plaintext-eyJhbGciOiJSUzI1NiJ9.payload.signature"
KEY = base64.b64encode(bytes(range(32))).decode()


@pytest.fixture(autouse=True)
def _grant_key(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = types.SimpleNamespace(grant_encryption_key=KEY, grant_key_version=3)
    monkeypatch.setattr(grants, "get_settings", lambda: settings)


def _run[T](pg: str, fn: Callable[[AsyncConnection], Awaitable[T]]) -> T:
    async def go() -> T:
        async with await AsyncConnection.connect(pg) as conn:
            try:
                return await fn(conn)
            finally:
                await conn.commit()

    return asyncio.run(go())


def _create(pg: str, agent_id: str, person: w.Person, **kw: Any) -> uuid.UUID:
    kind = agent_id.partition(":")[0]
    args: dict[str, Any] = {
        "agent_id": agent_id,
        "agent_kind": kind,
        "user_id": person.user_id,
        "consented_by": person.user_id,
        "household_id": "h-1",
        "scopes": grants.CLASS_SCOPES[kind],
        "offline_token": OFFLINE,
        **kw,
    }
    return _run(pg, lambda conn: grants.create(conn, **args))


def _rows(pg: str, sql: str, *params: Any) -> list[dict[str, Any]]:
    with psycopg.connect(pg) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _grant(pg: str, grant_id: uuid.UUID) -> dict[str, Any]:
    [row] = _rows(pg, "SELECT * FROM agent_grants WHERE grant_id = %s", grant_id)
    return row


def _audits(pg: str, kind: str) -> list[dict[str, Any]]:
    return _rows(pg, "SELECT * FROM audit_events WHERE kind = %s ORDER BY event_id", kind)


@pytest.fixture
def world(pg: str) -> w.Household:
    with psycopg.connect(pg) as conn:
        world = w.add_world(conn)
        conn.execute("DELETE FROM agent_grants")  # each test creates its own
    return world


def test_create_encrypts_and_audits(pg: str, world: w.Household) -> None:
    grant_id = _create(pg, world.household_agent, world.admin)
    row = _grant(pg, grant_id)
    assert row["status"] == "active"
    assert row["key_version"] == 3
    loaded = _run(pg, lambda conn: grants.load_active(conn, world.household_agent))
    assert loaded.grant_id == grant_id
    assert grants.decrypt(loaded) == OFFLINE
    [audit] = _audits(pg, "consent_granted")
    assert (audit["grant_id"], audit["agent_id"], audit["actor_user_id"]) == (
        grant_id,
        world.household_agent,
        world.admin.user_id,
    )


def test_grant_ciphertext_not_plaintext(pg: str, world: w.Household) -> None:
    grant_id = _create(pg, world.household_agent, world.admin)
    data = bytes(_grant(pg, grant_id)["refresh_ciphertext"])
    assert OFFLINE.encode() not in data
    assert len(data) == grants.NONCE_BYTES + len(OFFLINE) + 16  # nonce || ciphertext || tag
    # The grant id is the additional data: the ciphertext moved to another grant fails.
    loaded = _run(pg, lambda conn: grants.load_active(conn, world.household_agent))
    moved = grants.Grant(**{**loaded.__dict__, "grant_id": uuid.uuid4()})
    with pytest.raises(Exception):  # noqa: B017 - cryptography's InvalidTag
        grants.decrypt(moved)


def test_second_consumer_grant_replaces_first(pg: str, world: w.Household) -> None:
    first = _create(pg, world.consumer_agent, world.admin)
    second = _create(pg, world.consumer_agent, world.admin)
    assert _grant(pg, first)["status"] == "revoked"
    assert _grant(pg, first)["revoke_reason"] == "replaced"
    assert _grant(pg, second)["status"] == "active"
    [revoked] = _audits(pg, "consent_revoked")
    assert revoked["grant_id"] == first


def test_second_admin_replaces_household_grant(pg: str, world: w.Household) -> None:
    with psycopg.connect(pg) as conn:
        other = w.add_person(conn, world.household_id, role="admin")
    first = _create(pg, world.household_agent, world.admin)
    second = _create(pg, world.household_agent, other)
    assert _grant(pg, first)["status"] == "revoked"
    assert _grant(pg, second)["user_id"] == other.user_id
    active = _rows(
        pg,
        "SELECT grant_id FROM agent_grants WHERE agent_id = %s AND status = 'active'",
        world.household_agent,
    )
    assert [r["grant_id"] for r in active] == [second]


def test_grant_expires_after_30_days(pg: str, world: w.Household) -> None:
    grant_id = _create(pg, world.household_agent, world.admin)
    row = _grant(pg, grant_id)
    assert abs(row["expires_at"] - row["created_at"] - timedelta(days=30)) < timedelta(minutes=1)
    with psycopg.connect(pg) as conn:
        conn.execute(
            "UPDATE agent_grants SET expires_at = now() - interval '1 second' WHERE grant_id = %s",
            (grant_id,),
        )
    with pytest.raises(GrantExpired):
        _run(pg, lambda conn: grants.load_active(conn, world.household_agent))
    assert _grant(pg, grant_id)["status"] == "expired"
    assert [a["grant_id"] for a in _audits(pg, "grant_expired")] == [grant_id]
    # Later loads stay refused and write no second audit row.
    with pytest.raises(GrantExpired):
        _run(pg, lambda conn: grants.load_active(conn, world.household_agent))
    assert len(_audits(pg, "grant_expired")) == 1


def test_missing_grant_raises_grant_missing(pg: str, world: w.Household) -> None:
    with pytest.raises(GrantMissing):
        _run(pg, lambda conn: grants.load_active(conn, world.household_agent))
    with pytest.raises(GrantMissing):
        _run(pg, lambda conn: grants.load_active(conn, world.consumer_agent, world.admin.user_id))


def test_revoke_is_one_row_and_audited(pg: str, world: w.Household) -> None:
    grant_id = _create(pg, world.household_agent, world.admin)
    assert _run(pg, lambda conn: grants.revoke(conn, grant_id, world.admin.user_id, "user"))
    row = _grant(pg, grant_id)
    assert (row["status"], row["revoked_by"], row["revoke_reason"]) == (
        "revoked",
        world.admin.user_id,
        "user",
    )
    assert row["revoked_at"] is not None
    [audit] = _audits(pg, "consent_revoked")
    assert audit["grant_id"] == grant_id
    # A second revoke changes nothing and writes nothing.
    assert not _run(pg, lambda conn: grants.revoke(conn, grant_id, None, "user"))
    assert len(_audits(pg, "consent_revoked")) == 1
    with pytest.raises(GrantRevoked):
        _run(pg, lambda conn: grants.load_active(conn, world.household_agent))


def test_mark_refused_rule(pg: str, world: w.Household) -> None:
    live = _create(pg, world.household_agent, world.admin)
    past = _create(pg, world.consumer_agent, world.admin)
    with psycopg.connect(pg) as conn:
        conn.execute(
            "UPDATE agent_grants SET expires_at = now() - interval '1 second' WHERE grant_id = %s",
            (past,),
        )
    _run(pg, lambda conn: grants.mark_refused(conn, live))
    _run(pg, lambda conn: grants.mark_refused(conn, past))
    assert (_grant(pg, live)["status"], _grant(pg, live)["revoke_reason"]) == (
        "revoked",
        "keycloak_invalid_grant",
    )
    assert _grant(pg, past)["status"] == "expired"
    assert [a["grant_id"] for a in _audits(pg, "consent_revoked")] == [live]
    assert [a["grant_id"] for a in _audits(pg, "grant_expired")] == [past]


def test_revoked_agent_next_guarded_call_refused(pg: str, world: w.Household) -> None:
    grant_id = _create(pg, world.household_agent, world.admin)
    token = w.people_token(world.household_agent, world.admin.keycloak_sub)
    ctx = types.SimpleNamespace(agent_id=world.household_agent, env="live")
    claims = _run(pg, lambda conn: tokens.validate(conn, token, ctx))
    assert claims.grant_id == grant_id
    _run(pg, lambda conn: grants.revoke(conn, grant_id, world.admin.user_id, "user"))
    # The same, still unexpired token is refused on the very next call (I-D19).
    with pytest.raises(GrantRevoked):
        _run(pg, lambda conn: tokens.validate(conn, token, ctx))
