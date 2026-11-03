"""OpenFGA structural tuples, reconcile and outages (identity-authz §7, I9)."""

import asyncio
import socket
import threading
import time
import types
import uuid
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from authz import apply as authz_apply
from services.identity import fga
from services.identity.errors import AuthzUnavailable
from services.safety import guard
from services.safety.guard import GuardRefused, run_guarded
from tests.unit import identity_world as w

U_ADMIN = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
U_MEMBER = uuid.UUID("00000000-0000-4000-8000-0000000000a2")
U_CHILD = uuid.UUID("00000000-0000-4000-8000-0000000000a3")

EXPECTED = {
    (f"user:{U_ADMIN}", "admin", "household:h-1"),
    (f"user:{U_MEMBER}", "member", "household:h-1"),
    (f"user:{U_CHILD}", "child", "household:h-1"),
    ("agent:consumer.h-1.1", "acts_for", f"user:{U_ADMIN}"),
    ("agent:household.h-1", "acts_for", "household:h-1"),
    ("agent:household.h-1", "can_spend", "budget:h-1"),
    ("agent:grocery.mock-us", "member_of", "org:mock"),
    ("agent:delivery.mock-us", "member_of", "org:courier"),
    ("agent:delivery.mock-us", "can_deliver_for", "household:h-1"),
}


def _seed(conn: psycopg.Connection) -> None:
    for hid, zone in (("h-1", "z-1"), ("h-2", "z-9")):
        conn.execute(
            "INSERT INTO households (household_id, name, region, zone_id, timezone, currency)"
            " VALUES (%s, %s, 'us', %s, 'America/New_York', 'USD')",
            (hid, hid, zone),
        )
    for user_id, role in ((U_ADMIN, "admin"), (U_MEMBER, "member"), (U_CHILD, "child")):
        conn.execute("INSERT INTO users (user_id, display_name) VALUES (%s, %s)", (user_id, role))
        conn.execute(
            "INSERT INTO household_members (household_id, user_id, role) VALUES ('h-1', %s, %s)",
            (user_id, role),
        )
    cards = [
        ("consumer:h-1.1", "consumer", f"user:{U_ADMIN}", ["z-1"], True),
        ("household:h-1", "household", "household:h-1", ["z-1"], True),
        ("grocery:mock-us", "grocery", "org:mock", ["z-1"], True),
        ("delivery:mock-us", "delivery", "org:courier", ["z-1"], True),
        ("grocery:disabled", "grocery", "org:mock", ["z-1"], False),
        ("channel:whatsapp", "channel", "system", [], True),
    ]
    for agent_id, cls, owner_ref, zones, enabled in cards:
        conn.execute(
            "INSERT INTO agent_cards (agent_id, class, owner_ref, region, zone_ids,"
            " host_process, enabled) VALUES (%s, %s, %s, 'us', %s, 'agents', %s)",
            (agent_id, cls, owner_ref, zones, enabled),
        )


def _setup(pg: str, fake: Any) -> psycopg.Connection:
    conn = psycopg.connect(pg)
    _seed(conn)
    conn.commit()
    authz_apply.apply(conn, fake)
    return conn


def _store(fake: Any) -> set[tuple[str, str, str]]:
    (store_id,) = fake.tuples
    return fake.tuples[store_id]


def _reconciled(conn: psycopg.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT detail FROM audit_events WHERE kind = 'fga_reconciled' ORDER BY event_id"
    ).fetchall()
    return [r[0] for r in rows]


@pytest.mark.db
def test_reconcile_idempotent(pg: str, fake_openfga: Any) -> None:
    with _setup(pg, fake_openfga) as conn:
        assert fga.reconcile(conn) == len(EXPECTED)
        assert _store(fake_openfga) == EXPECTED
        assert fga.reconcile(conn) == 0
        assert _store(fake_openfga) == EXPECTED
        details = _reconciled(conn)
    assert len(details) == len(EXPECTED)
    assert {(d["user"], d["relation"], d["object"]) for d in details} == EXPECTED
    assert {d["action"] for d in details} == {"write"}


@pytest.mark.db
def test_reconcile_restores_deleted_tuple(pg: str, fake_openfga: Any) -> None:
    lost = ("agent:household.h-1", "can_spend", "budget:h-1")
    stray = ("agent:grocery.disabled", "member_of", "org:mock")
    with _setup(pg, fake_openfga) as conn:
        fga.reconcile(conn)
        _store(fake_openfga).discard(lost)
        _store(fake_openfga).add(stray)
        assert fga.reconcile(conn) == 2
        assert _store(fake_openfga) == EXPECTED
        details = _reconciled(conn)[len(EXPECTED) :]
    assert sorted((d["action"], d["object"]) for d in details) == [
        ("delete", "org:mock"),
        ("write", "budget:h-1"),
    ]


@pytest.mark.db
def test_reconcile_leaves_unmanaged_relations(pg: str, fake_openfga: Any) -> None:
    owner = (f"user:{U_ADMIN}", "owner", "org:mock")  # no Postgres source yet (I27)
    with _setup(pg, fake_openfga) as conn:
        _store(fake_openfga).add(owner)
        fga.reconcile(conn)
    assert owner in _store(fake_openfga)


@pytest.mark.db
def test_reconcile_audit_rolls_back_when_openfga_fails(pg: str, fake_openfga: Any) -> None:
    with _setup(pg, fake_openfga) as conn:
        fake_openfga.fail = AuthzUnavailable("down")
        with pytest.raises(AuthzUnavailable):
            fga.reconcile(conn)
        conn.rollback()
        assert _reconciled(conn) == []


@pytest.mark.db
def test_sync_member_and_agent_write_missing_tuples(pg: str, fake_openfga: Any) -> None:
    with _setup(pg, fake_openfga) as conn:
        fga.sync_member(conn, "h-1", U_CHILD)
        fga.sync_agent(conn, "household:h-1")
        fga.sync_agent(conn, "household:h-1")  # already there: nothing to write
    assert _store(fake_openfga) == {
        (f"user:{U_CHILD}", "child", "household:h-1"),
        ("agent:household.h-1", "acts_for", "household:h-1"),
        ("agent:household.h-1", "can_spend", "budget:h-1"),
    }


@pytest.mark.db
def test_check_returns_model_id(
    pg: str, fake_openfga: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _setup(pg, fake_openfga) as conn:
        fga.reconcile(conn)
        (model_id,) = conn.execute("SELECT model_id FROM fga_models").fetchone()
    monkeypatch.setattr(fga, "get_settings", lambda: types.SimpleNamespace(database_url=pg))
    assert fga.check("agent:household.h-1", "can_spend", "budget:h-1") == (True, model_id)
    assert fga.check("agent:grocery.mock-us", "acts_for", "household:h-1") == (False, model_id)


@pytest.mark.db
def test_check_without_model_fails_closed(
    pg: str, fake_openfga: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fga, "get_settings", lambda: types.SimpleNamespace(database_url=pg))
    with pytest.raises(AuthzUnavailable):
        fga.check("agent:household.h-1", "can_spend", "budget:h-1")


def test_timeout_raises_authz_unavailable() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    held: list[socket.socket] = []
    threading.Thread(target=lambda: held.append(listener.accept()[0]), daemon=True).start()
    client = fga.Client(f"http://127.0.0.1:{listener.getsockname()[1]}", "key")
    started = time.monotonic()
    try:
        with pytest.raises(AuthzUnavailable) as exc:
            client.check("store", "model", "agent:household.h-1", "acts_for", "household:h-1")
        elapsed = time.monotonic() - started
    finally:
        for conn in held:
            conn.close()
        listener.close()
    assert exc.value.reason == "authz_unavailable"
    assert fga.TIMEOUT_MILLISEC / 1000 - 0.1 <= elapsed < fga.TIMEOUT_MILLISEC / 1000 + 1


def test_connection_error_raises_authz_unavailable() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()  # nothing listens on the port now
    with pytest.raises(AuthzUnavailable):
        fga.Client(f"http://127.0.0.1:{port}", "key").check(
            "store", "model", "agent:household.h-1", "acts_for", "household:h-1"
        )


SELLER = "grocery:mock-us"


@dataclass
class _Ctx:
    run_id: uuid.UUID
    agent_id: str
    token: str | None
    on_behalf_of: str | None = "org:mock"
    conversation_id: uuid.UUID | None = None
    env: str = "live"
    # Set by the guard (identity-authz I6).
    claims: Any = None
    authz_form: str | None = None


@pytest.mark.db
def test_authz_timeout_not_denial(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """An OpenFGA outage reaches the guard's caller as `AuthzUnavailable`, never as an
    `authz_denied` refusal, and the adapter is not called (I-G9)."""
    run_id = uuid.uuid4()
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, kind, status, trigger, modes)"
            " VALUES (%s, 'chat', 'running', '{}', '{}')",
            (run_id,),
        )
        w.add_agent(conn, SELLER, "org:mock")
    token = w.service_token(SELLER)

    def check(user: str, relation: str, object: str) -> tuple[bool, str]:
        raise AuthzUnavailable("timeout")

    monkeypatch.setattr(fga, "check", check)
    called: list[str] = []

    async def call() -> dict[str, Any]:
        called.append("adapter")
        return {}

    async def go() -> None:
        async with AsyncConnectionPool(pg, min_size=1, max_size=3, open=False) as pool:
            monkeypatch.setattr(guard.db, "get_pool", lambda: pool)
            await run_guarded(
                _Ctx(run_id, SELLER, token),
                tool="grocery.place_order",
                relation=("member_of", "org:mock"),
                limits=None,
                key=None,
                request={},
                call=call,
                apply=None,
            )

    with pytest.raises(AuthzUnavailable) as exc:
        asyncio.run(go())
    assert not isinstance(exc.value, GuardRefused)
    assert called == []
    with psycopg.connect(pg) as conn:
        refusals = conn.execute(
            "SELECT payload FROM agent_traces WHERE run_id = %s"
            " AND payload->>'reason' = 'authz_denied'",
            (run_id,),
        ).fetchall()
    assert refusals == []
