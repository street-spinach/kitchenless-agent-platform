"""Runs: start, finish, retry target (architecture item 24 and §10, amended by A13 and for
#36; agent-protocol P14; issue #36)."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from services.common import ids, modes, runs
from services.common.config import Settings
from services.identity.errors import GrantMissing, GrantRevoked
from tests.unit import identity_world as w

pytestmark = pytest.mark.db

HOUSEHOLD_ID = "us-1"
HOUSEHOLD = "household:us-1"
GROCERY = "grocery:mock-us"
WHATSAPP = "channel:whatsapp"
USER = uuid.UUID("22222222-2222-4222-8222-222222222222")
ADDRESS = uuid.UUID("33333333-3333-4333-8333-333333333333")


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
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
    return settings


@pytest.fixture
def world(pg: str) -> str:
    with psycopg.connect(pg) as conn:
        for agent_id, cls, owner, host in (
            (HOUSEHOLD, "household", "household:us-1", "agents"),
            (GROCERY, "grocery", "org:mock", "agents"),
            (WHATSAPP, "channel", "system", "api"),
        ):
            conn.execute(
                "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
                " VALUES (%s, %s, %s, 'us', %s)",
                (agent_id, cls, owner, host),
            )
        conn.execute(
            "INSERT INTO households (household_id, name, region, zone_id, timezone, currency)"
            " VALUES (%s, 'Test', 'us', 'us-oh-45202', 'America/New_York', 'USD')",
            (HOUSEHOLD_ID,),
        )
        conn.execute(
            "INSERT INTO household_addresses"
            " (address_ref, household_id, line1, city, region, postal_code, country)"
            " VALUES (%s, %s, '1 Main St', 'Cincinnati', 'OH', '45202', 'US')",
            (ADDRESS, HOUSEHOLD_ID),
        )
        # `runs.start` needs the household agent's active grant (identity-authz I39).
        w.grant_household(conn, HOUSEHOLD_ID)
    return pg


def _run[T](pg: str, fn: Callable[[psycopg.AsyncConnection], Awaitable[T]]) -> T:
    async def go() -> T:
        async with await psycopg.AsyncConnection.connect(pg) as conn:
            return await fn(conn)

    return asyncio.run(go())


def _trigger(**extra: Any) -> dict[str, Any]:
    return {"source": "whatsapp", "user_id": str(USER), "inbound_id": "wamid.1", **extra}


def _start(pg: str, *, kind: str = "weekly", **kwargs: Any) -> uuid.UUID:
    kwargs.setdefault("household_id", HOUSEHOLD_ID)
    kwargs.setdefault("trigger", _trigger())
    return _run(pg, lambda conn: runs.start(conn, kind=kind, **kwargs))


def _finish(pg: str, run_id: uuid.UUID, *args: Any, **kwargs: Any) -> bool:
    return _run(pg, lambda conn: runs.finish(conn, run_id, *args, **kwargs))


def _query(pg: str, sql: str, *params: Any) -> list[dict[str, Any]]:
    with psycopg.connect(pg) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _traces(pg: str, run_id: uuid.UUID, kind: str) -> list[dict[str, Any]]:
    return _query(
        pg, "SELECT * FROM agent_traces WHERE run_id = %s AND kind = %s ORDER BY seq", run_id, kind
    )


def _messages(pg: str, run_id: uuid.UUID) -> list[dict[str, Any]]:
    return _query(pg, "SELECT * FROM agent_messages WHERE run_id = %s", run_id)


def _run_row(pg: str, run_id: uuid.UUID) -> dict[str, Any]:
    [row] = _query(pg, "SELECT * FROM runs WHERE run_id = %s", run_id)
    return row


def _add_order(pg: str, run_id: uuid.UUID, state: str, delivery_agent_id: str | None) -> None:
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
            " VALUES ('delivery:mock-us', 'delivery', 'org:mock', 'us', 'agents')"
            " ON CONFLICT DO NOTHING"
        )
        conn.execute(
            "INSERT INTO orders (order_id, run_id, household_id, kind, state, currency,"
            " address_ref, delivery_agent_id)"
            " VALUES (%s, %s, %s, 'grocery', %s, 'USD', %s, %s)",
            (uuid.uuid4(), run_id, HOUSEHOLD_ID, state, ADDRESS, delivery_agent_id),
        )


def _ended_error_run(pg: str) -> uuid.UUID:
    run_id = _start(pg)
    assert _finish(pg, run_id, HOUSEHOLD, "error", "model_unavailable")
    return run_id


def test_eval_household_run_pins_mock_under_mode_real(
    world: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Adapters §3: the Square sandbox is not split by env, so eval runs never pin `real`.
    env = {
        "SERVICE_NAME": "migrate",
        "DATABASE_URL": "x",
        "LOG_LEVEL": "info",
        "OPENFGA_PRESHARED_KEY": "x",
        "OPENFGA_API_URL": "x",
        "MODE": "real",
    }
    settings = Settings.from_env(env)
    monkeypatch.setattr(runs, "get_settings", lambda: settings)
    monkeypatch.setattr(modes, "get_settings", lambda: settings)
    with psycopg.connect(world) as conn:
        conn.execute("UPDATE households SET env = 'eval' WHERE household_id = %s", (HOUSEHOLD_ID,))

    run_id = _start(world)

    # Every vendor mock, and the run's token source (identity-authz §12) mock too.
    all_mock = {**dict.fromkeys(modes.VENDORS, "mock"), "token_source": "mock"}
    assert _run_row(world, run_id)["modes"] == all_mock
    [start_row] = _traces(world, run_id, "run_start")
    assert start_row["payload"]["modes"] == all_mock

    # The same settings for a live household pin the Square keys to `real`.
    with psycopg.connect(world) as conn:
        conn.execute("UPDATE households SET env = 'live' WHERE household_id = %s", (HOUSEHOLD_ID,))
    live = _run_row(world, _start(world, trigger=_trigger(inbound_id="wamid.2")))["modes"]
    assert live["square_retail"] == "real"
    assert live["doordash"] == "mock"


def test_finish_writes_single_outcome(world: str) -> None:
    run_id = _start(world)
    [start_row] = _traces(world, run_id, "run_start")
    assert start_row["agent_id"] == runs.RUNS_AGENT
    assert start_row["payload"]["kind"] == "weekly"
    assert set(start_row["payload"]["modes"]) == modes.VENDORS | {"token_source"}
    [timer] = _query(world, "SELECT * FROM timers WHERE run_id = %s", run_id)
    assert timer["kind"] == "run_timeout"
    assert _run(world, lambda conn: runs.is_ended(conn, run_id)) is False

    assert _finish(world, run_id, HOUSEHOLD, "completed", None, "Order delivered") is True

    row = _run_row(world, run_id)
    assert (row["status"], row["reason"]) == ("completed", None)
    assert row["ended_at"] is not None
    [outcome] = _traces(world, run_id, "outcome")
    assert outcome["agent_id"] == HOUSEHOLD
    assert outcome["on_behalf_of"] == "household:us-1"
    assert outcome["payload"] == {
        "status": "completed",
        "reason": None,
        "summary": "Order delivered",
    }
    assert _messages(world, run_id) == []  # no retry prompt for a completed run
    assert _run(world, lambda conn: runs.is_ended(conn, run_id)) is True


def test_second_finish_is_noop(world: str) -> None:
    run_id = _start(world)
    assert _finish(world, run_id, HOUSEHOLD, "error", "model_unavailable") is True
    assert _finish(world, run_id, "system:sweeper", "timeout") is False
    assert _finish(world, run_id, HOUSEHOLD, "error", "model_unavailable") is False

    row = _run_row(world, run_id)
    assert (row["status"], row["reason"]) == ("error", "model_unavailable")
    assert len(_traces(world, run_id, "outcome")) == 1
    assert len(_messages(world, run_id)) == 1  # the replay inserted no second prompt


def test_start_same_run_id_twice_inserts_once(world: str) -> None:
    run_id = uuid.uuid4()
    assert _start(world, run_id=run_id) == run_id
    assert _start(world, run_id=run_id) == run_id
    assert len(_traces(world, run_id, "run_start")) == 1
    assert len(_query(world, "SELECT 1 FROM timers WHERE run_id = %s", run_id)) == 1


def test_start_refuses_trigger_without_user_id(world: str) -> None:
    with pytest.raises(ValueError, match="user_id"):
        _start(world, trigger={"source": "whatsapp", "inbound_id": "wamid.2"})


def test_finish_refuses_unknown_status(world: str) -> None:
    run_id = _start(world)
    with pytest.raises(ValueError):
        _finish(world, run_id, HOUSEHOLD, "awaiting_approval")
    assert _run_row(world, run_id)["status"] == "running"


@pytest.mark.parametrize("status", ["error", "timeout"])
def test_finish_error_sends_retry_prompt(world: str, status: str) -> None:
    run_id = _start(world)
    assert _finish(world, run_id, HOUSEHOLD, status, "model_unavailable") is True

    [msg] = _messages(world, run_id)
    assert msg["message_id"] == ids.retry_prompt_id(run_id)
    assert (msg["from_agent"], msg["to_agent"], msg["to_host"]) == (
        runs.RUNS_AGENT,
        WHATSAPP,
        "api",
    )
    assert msg["type"] == "whatsapp_out"
    assert (msg["conversation_id"], msg["causation_id"]) == (None, None)
    assert msg["body"] == {
        "user_id": str(USER),
        "template": "retry_prompt",
        "params": {},
        "buttons": [],
    }
    [trace] = _traces(world, run_id, "message")
    assert trace["agent_id"] == runs.RUNS_AGENT
    assert trace["on_behalf_of"] is None


def test_retry_prompt_in_same_transaction_as_outcome(world: str) -> None:
    run_id = _start(world)

    async def rolled_back(conn: psycopg.AsyncConnection) -> None:
        with pytest.raises(RuntimeError):
            async with conn.transaction():
                assert await runs.finish(conn, run_id, HOUSEHOLD, "error", "model_unavailable")
                raise RuntimeError("caller fails after finish")

    _run(world, rolled_back)
    assert _run_row(world, run_id)["status"] == "running"
    assert _traces(world, run_id, "outcome") == []
    assert _messages(world, run_id) == []


@pytest.mark.parametrize("reason", sorted(runs.NO_RETRY_PROMPT))
def test_finish_no_retry_reason_sends_no_prompt(world: str, reason: str) -> None:
    assert runs.NO_RETRY_PROMPT == {"delivery_failed", "nothing_to_deliver", "no_offers"}
    run_id = _start(world)
    assert _finish(world, run_id, HOUSEHOLD, "error", reason) is True
    assert _run_row(world, run_id)["reason"] == reason
    assert len(_traces(world, run_id, "outcome")) == 1
    assert _messages(world, run_id) == []

    # A poison finish with a seller's agent_id still sends the prompt, from system:runs.
    poisoned = _start(world)
    assert _finish(world, poisoned, GROCERY, "error", "poison_message") is True
    [outcome] = _traces(world, poisoned, "outcome")
    assert (outcome["agent_id"], outcome["on_behalf_of"]) == (GROCERY, "org:mock")
    [msg] = _messages(world, poisoned)
    assert (msg["from_agent"], msg["message_id"]) == (
        runs.RUNS_AGENT,
        ids.retry_prompt_id(poisoned),
    )


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_finish_non_error_sends_no_prompt(world: str, status: str) -> None:
    run_id = _start(world)
    assert _finish(world, run_id, HOUSEHOLD, status, "approval_expired") is True
    assert _messages(world, run_id) == []


def test_retry_target_refused_after_placed(world: str) -> None:
    open_run = _ended_error_run(world)
    _add_order(world, open_run, "negotiating", None)
    assert _run(world, lambda conn: runs.retry_target(conn, USER)) == open_run

    placed = _ended_error_run(world)
    _add_order(world, placed, "placed", "delivery:mock-us")
    assert _run(world, lambda conn: runs.retry_target(conn, USER)) is None

    # Failed after placed (delivery_agent_id was set with `placed`): still refused.
    failed_after_placed = _ended_error_run(world)
    _add_order(world, failed_after_placed, "failed", "delivery:mock-us")
    assert _run(world, lambda conn: runs.retry_target(conn, USER)) is None


def test_retry_target_none_when_latest_run_not_error(world: str) -> None:
    _ended_error_run(world)
    done = _start(world)
    _finish(world, done, HOUSEHOLD, "completed")
    assert _run(world, lambda conn: runs.retry_target(conn, USER)) is None
    assert _run(world, lambda conn: runs.retry_target(conn, uuid.uuid4())) is None


def test_retry_target_none_after_24_hours(world: str) -> None:
    old = _ended_error_run(world)
    with psycopg.connect(world) as conn:
        conn.execute(
            "UPDATE runs SET started_at = now() - interval '25 hours' WHERE run_id = %s", (old,)
        )
    assert _run(world, lambda conn: runs.retry_target(conn, USER)) is None


def _source_run(pg: str, source: str, status: str, *, household_id: str | None) -> uuid.UUID:
    trigger = {"source": source, "user_id": str(USER)}
    run_id = _start(pg, kind="chat", household_id=household_id, trigger=trigger)
    assert _finish(pg, run_id, runs.RUNS_AGENT, status, "model_unavailable")
    return run_id


def test_code_only_run_not_retry_target(world: str) -> None:
    # Experience EX-A16: a failed code-only run (no household) is never the target.
    _source_run(world, "code", "error", household_id=None)
    assert _run(world, lambda conn: runs.retry_target(conn, USER)) is None

    # A finished code-only run does not hide the failed run before it (EX-A15).
    failed = _ended_error_run(world)
    _source_run(world, "code", "completed", household_id=None)
    assert _run(world, lambda conn: runs.retry_target(conn, USER)) == failed


def test_reminder_never_retry_target(world: str) -> None:
    for status in ("error", "timeout"):
        _source_run(world, "reminder", status, household_id=None)
        assert _run(world, lambda conn: runs.retry_target(conn, USER)) is None


def test_tap_run_not_retry_target(world: str) -> None:
    # An `ob:` tap run has a household but is still never the target.
    _source_run(world, "tap", "error", household_id=HOUSEHOLD_ID)
    assert _run(world, lambda conn: runs.retry_target(conn, USER)) is None


def test_failed_chat_run_still_retry_target(world: str) -> None:
    failed = _ended_error_run(world)
    for source in ("code", "join", "tap", "me", "reminder"):
        household_id = None if source in ("code", "reminder") else HOUSEHOLD_ID
        _source_run(world, source, "error", household_id=household_id)
        _source_run(world, source, "completed", household_id=household_id)
    # A run with no household and no excluded source is skipped too.
    _source_run(world, "whatsapp", "error", household_id=None)
    assert _run(world, lambda conn: runs.retry_target(conn, USER)) == failed


def test_retry_starts_new_run_with_retry_of(world: str) -> None:
    old = _ended_error_run(world)
    target = _run(world, lambda conn: runs.retry_target(conn, USER))
    assert target == old

    new = _start(world, trigger=_trigger(inbound_id="wamid.retry", retry_of=target))

    assert new != old
    row = _run_row(world, new)
    assert row["status"] == "running"
    assert row["trigger"]["retry_of"] == str(old)
    [start_row] = _traces(world, new, "run_start")
    assert start_row["payload"]["trigger"]["retry_of"] == str(old)
    assert _run_row(world, old)["status"] == "error"  # the old run never changes
    # The latest run is now the retry, which is still running.
    assert _run(world, lambda conn: runs.retry_target(conn, USER)) is None


def test_a2a_demo_run_sends_no_retry_prompt(world: str, _settings: Settings) -> None:
    with psycopg.connect(world) as conn:
        conn.execute(
            "INSERT INTO households (household_id, name, region, zone_id, timezone, currency)"
            " VALUES ('a2a-demo', 'A2A demo', 'us', 'us-oh-45202', 'America/New_York', 'USD')"
        )
    message_id = uuid.uuid4()
    run_id = _start(
        world,
        kind="a2a_demo",
        household_id="a2a-demo",
        run_id=ids.a2a_run_id(message_id),
        trigger={"source": "a2a", "inbound_id": message_id},  # a UUID, as P20 passes it
    )
    assert run_id == ids.a2a_run_id(message_id)
    [timer] = _query(
        world,
        "SELECT extract(epoch FROM due_at - created_at) AS s FROM timers WHERE run_id = %s",
        run_id,
    )
    assert abs(float(timer["s"]) - _settings.a2a_demo_timeout_seconds) < 5

    assert _finish(world, run_id, "system:sweeper", "timeout") is True
    assert _run_row(world, run_id)["status"] == "timeout"
    assert len(_traces(world, run_id, "outcome")) == 1
    assert _messages(world, run_id) == []


def test_run_timeout_uses_run_timeout_seconds(world: str, _settings: Settings) -> None:
    run_id = _start(world)
    [timer] = _query(
        world,
        "SELECT extract(epoch FROM due_at - created_at) AS s FROM timers WHERE run_id = %s",
        run_id,
    )
    assert abs(float(timer["s"]) - _settings.run_timeout_seconds) < 5


# --- identity (identity-authz I39, build step 5) -------------------------------------------


def _set_grants(pg: str, status: str) -> None:
    with psycopg.connect(pg) as conn:
        conn.execute("UPDATE agent_grants SET status = %s", (status,))


def test_start_refused_without_household_grant(world: str) -> None:
    with psycopg.connect(world) as conn:
        conn.execute("DELETE FROM agent_grants")
    with pytest.raises(GrantMissing):
        _start(world)
    assert _query(world, "SELECT run_id FROM runs") == []


def test_start_refused_after_household_grant_revoked(world: str) -> None:
    _set_grants(world, "revoked")
    with pytest.raises(GrantRevoked):
        _start(world)
    assert _query(world, "SELECT run_id FROM runs") == []


@pytest.mark.parametrize("kind", ["hello", "a2a_demo"])
def test_start_grant_exempt_kinds(world: str, kind: str) -> None:
    _set_grants(world, "revoked")
    assert _run_row(world, _start(world, kind=kind))["status"] == "running"


def test_start_without_household_needs_no_grant(world: str) -> None:
    _set_grants(world, "revoked")
    run_id = _start(world, kind="chat", household_id=None)
    assert _run_row(world, run_id)["household_id"] is None


def test_start_records_token_source(world: str) -> None:
    assert _run_row(world, _start(world))["modes"]["token_source"] == "mock"


def test_finish_grant_revoked_fails_unplaced_orders(world: str) -> None:
    run_id = _start(world)
    _add_order(world, run_id, "authorized", None)
    _add_order(world, run_id, "placed", "delivery:mock-us")
    assert _finish(world, run_id, HOUSEHOLD, "error", "grant_revoked")
    states = sorted(
        r["state"] for r in _query(world, "SELECT state FROM orders WHERE run_id = %s", run_id)
    )
    assert states == ["failed", "placed"]


def test_finish_other_error_leaves_orders(world: str) -> None:
    run_id = _start(world)
    _add_order(world, run_id, "authorized", None)
    assert _finish(world, run_id, HOUSEHOLD, "error", "model_unavailable")
    [order] = _query(world, "SELECT state FROM orders WHERE run_id = %s", run_id)
    assert order["state"] == "authorized"
