"""`services/api/ingest.py` (architecture item 33; issues #40 and #63)."""

import asyncio
import uuid
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from services.agents import sweeper
from services.api import ingest as ingest_module
from services.api.ingest import (
    API_AGENT,
    APPROVAL_EXPIRED,
    ME_LINK,
    MEDIA_NOT_SUPPORTED,
    NOT_APPROVER,
    REMINDERS_OFF,
    REMINDERS_ON,
    TAKE_TURNS_SAVED,
    Content,
    ingest,
)
from services.api.whatsapp import sender
from services.common import db, ids, runs
from services.common.config import Settings
from services.common.consumer import Inbound
from services.common.mcp_client import ToolRefused
from services.common.protocol.a2a import AgentMessageRow
from services.common.protocol.schemas import WhatsAppOut
from services.common.run_context import RunContext
from services.identity import fga, links, onboarding
from services.identity.keycloak_admin import KeycloakError
from services.mcp.household import server as household
from services.mcp.meta import CallContext
from services.safety.guard import GuardRefused
from tests.unit import api_world as w
from tests.unit import identity_world as iw

pytestmark = pytest.mark.db

CHANNEL = "whatsapp"
HOUSEHOLD_AGENT = f"household:{w.HOUSEHOLD_ID}"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    w.patch_settings(monkeypatch)
    monkeypatch.setattr(household, "get_settings", runs.get_settings)  # the patched settings
    monkeypatch.setattr(fga, "check", lambda user, relation, object: (True, "model-1"))


@pytest.fixture
def world(pg: str) -> str:
    w.seed(pg)
    return pg


def _ingest(pg: str, external_id: str, content: Content, phone: str = w.MEMBER_PHONE):
    return w.run(pg, lambda conn: ingest(conn, CHANNEL, external_id, phone, content))


def _messages(pg: str, run_id: uuid.UUID) -> list[dict]:
    return w.query(pg, "SELECT * FROM agent_messages WHERE run_id = %s", run_id)


def _run_row(pg: str, run_id: uuid.UUID) -> dict:
    [row] = w.query(pg, "SELECT * FROM runs WHERE run_id = %s", run_id)
    return row


def test_text_starts_run_and_sends_user_message(world: str) -> None:
    run_id = _ingest(world, "wamid.1", Content("text", text="Weekly shop please"))
    assert run_id is not None
    inbound = ids.inbound_id(CHANNEL, "wamid.1")
    run = _run_row(world, run_id)
    assert (run["kind"], run["household_id"], run["status"]) == ("chat", w.HOUSEHOLD_ID, "running")
    assert run["trigger"] == {
        "source": CHANNEL,
        "user_id": str(w.MEMBER),
        "inbound_id": str(inbound),
        "text": "Weekly shop please",
    }
    [msg] = _messages(world, run_id)
    assert msg["message_id"] == ids.message_id(inbound, 0)
    assert (msg["from_agent"], msg["to_agent"], msg["type"]) == (
        w.WHATSAPP,
        w.MEMBER_CONSUMER,
        "user_message",
    )
    assert msg["body"] == {"inbound_id": str(inbound)}
    [inbound_row] = w.query(world, "SELECT run_id FROM inbound_messages")
    assert inbound_row["run_id"] == run_id


def test_duplicate_inbound_ignored(world: str) -> None:
    first = _ingest(world, "wamid.dup", Content("text", text="Weekly shop"))
    second = _ingest(world, "wamid.dup", Content("text", text="Weekly shop"))
    assert first is not None and second is None
    assert len(w.query(world, "SELECT * FROM runs")) == 1
    assert len(w.query(world, "SELECT * FROM agent_messages")) == 1
    assert len(w.query(world, "SELECT * FROM inbound_messages")) == 1


def test_ack_does_not_wait_for_agents(world: str) -> None:
    # No agent host is running: the route answers once the rows are committed, and the
    # consumer's message is still waiting for it.
    resp = w.client(world).post(
        "/internal/sim/whatsapp",
        json={"phone": w.MEMBER_PHONE, "text": "Weekly shop", "external_id": "sim.1"},
    )
    assert resp.status_code == 200
    run_id = uuid.UUID(resp.json()["run_id"])
    assert _run_row(world, run_id)["status"] == "running"
    [msg] = _messages(world, run_id)
    assert (msg["type"], msg["status"]) == ("user_message", "pending")


def test_media_gets_not_supported_reply(world: str) -> None:
    run_id = _ingest(world, "wamid.media", Content("media"))
    assert run_id is not None
    run = _run_row(world, run_id)
    assert (run["status"], run["reason"]) == ("completed", MEDIA_NOT_SUPPORTED)
    [msg] = _messages(world, run_id)
    assert (msg["from_agent"], msg["to_agent"], msg["type"]) == (
        API_AGENT,
        w.WHATSAPP,
        "whatsapp_out",
    )
    assert msg["body"] == {
        "user_id": str(w.MEMBER),
        "template": MEDIA_NOT_SUPPORTED,
        "params": {},
        "buttons": [],
    }


def test_unknown_phone_starts_nothing(world: str) -> None:
    assert _ingest(world, "wamid.x", Content("text", text="hi"), w.UNKNOWN_PHONE) is None
    assert w.query(world, "SELECT * FROM runs") == []
    assert w.query(world, "SELECT * FROM agent_messages") == []


# Approval buttons (#63) ------------------------------------------------------------------
#
# The approval is requested through the real `/mcp/household request_approval`, and taps go
# through the real `decide_approval`, both in process on the test database.


async def _call_tool(server: str, tool: str, args: dict, *, ctx: RunContext, **kw: Any) -> dict:
    """`mcp_client.call_tool_as` for the in-process household server: it obtains `api`'s
    service token itself, so `ingest` passes none (identity-authz I6, I39)."""
    assert server == "household"
    assert kw == {}, "ingest must not pass a token"
    assert ctx.agent_id == API_AGENT
    call = CallContext(
        run_id=ctx.run_id,
        agent_id=ctx.agent_id,
        conversation_id=ctx.conversation_id,
        attempt=1,
        token=iw.service_token(ctx.agent_id),
        env="eval",
        modes={},
        on_behalf_of=None,
    )
    fn = {
        "decide_approval": household.decide_approval,
        "set_take_turns": household.set_take_turns,
        "apply_setup": household.apply_setup,
        "complete_join": household.complete_join,
    }[tool]
    uuids = ("approval_id", "user_id", "order_id", "proposal_id")
    typed = {k: uuid.UUID(v) if k in uuids else v for k, v in args.items()}
    if "child_user_ids" in typed:
        typed["child_user_ids"] = {k: uuid.UUID(v) for k, v in typed["child_user_ids"].items()}
    try:
        return await fn(call, **typed)
    except GuardRefused as refused:
        raise ToolRefused(refused.reason) from None


def _keycloak_sub(pg: str, user_id: uuid.UUID) -> str:
    [row] = w.query(pg, "SELECT keycloak_sub FROM users WHERE user_id = %s", user_id)
    return row["keycloak_sub"]


def _with_pool[T](pg: str, monkeypatch: pytest.MonkeyPatch, fn: Any) -> T:
    async def go() -> T:
        async with AsyncConnectionPool(pg, min_size=1, max_size=3, open=False) as pool:
            monkeypatch.setattr(db, "get_pool", lambda: pool)
            async with await psycopg.AsyncConnection.connect(pg) as conn:
                return await fn(conn)

    return asyncio.run(go())


def _request_approval(pg: str, monkeypatch: pytest.MonkeyPatch, ttl: int | None = None) -> dict:
    """A weekly run whose negotiating order asks the admin to approve 7600 USD."""
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO household_addresses"
            " (address_ref, household_id, line1, city, region, postal_code, country)"
            " VALUES (%s, %s, '1 Main St', 'Cincinnati', 'OH', '45202', 'US')",
            (ids.seed_address_ref(w.HOUSEHOLD_ID), w.HOUSEHOLD_ID),
        )
        conn.execute(
            "INSERT INTO approval_rules (household_id, approver_user_id) VALUES (%s, %s)",
            (w.HOUSEHOLD_ID, w.ADMIN),
        )
        conn.execute("UPDATE users SET display_name = 'Asha' WHERE user_id = %s", (w.ADMIN,))
    trigger: dict[str, Any] = {"user_id": w.ADMIN}
    if ttl is not None:
        trigger["overrides"] = {"APPROVAL_TTL_SECONDS": ttl}

    async def go(conn: psycopg.AsyncConnection) -> dict:
        run_id = await runs.start(conn, household_id=w.HOUSEHOLD_ID, kind="weekly", trigger=trigger)
        order_id = ids.order_id(run_id)
        await conn.execute(
            "INSERT INTO orders (order_id, run_id, household_id, kind, state, currency,"
            " address_ref) VALUES (%s, %s, %s, 'grocery', 'negotiating', 'USD', %s)",
            (order_id, run_id, w.HOUSEHOLD_ID, ids.seed_address_ref(w.HOUSEHOLD_ID)),
        )
        await conn.commit()
        ctx = CallContext(
            run_id=run_id,
            agent_id=HOUSEHOLD_AGENT,
            conversation_id=None,
            attempt=1,
            token=iw.people_token(HOUSEHOLD_AGENT, _keycloak_sub(pg, w.ADMIN)),
            env="eval",
            modes={},
            on_behalf_of=HOUSEHOLD_AGENT,
        )
        amount = {"amount_minor": 7600, "currency": "USD"}
        result = await household.request_approval(ctx, order_id=order_id, amount=amount)
        return {**result, "run_id": run_id}

    return _with_pool(pg, monkeypatch, go)


def _tap(
    pg: str,
    monkeypatch: pytest.MonkeyPatch,
    external_id: str,
    approval_id: str,
    decision: str,
    phone: str = w.ADMIN_PHONE,
) -> uuid.UUID | None:
    button = ids.approval_button(uuid.UUID(approval_id), decision)

    async def go(conn: psycopg.AsyncConnection) -> uuid.UUID | None:
        return await ingest(
            conn,
            CHANNEL,
            external_id,
            phone,
            Content("button", button=button),
            call_tool=_call_tool,
        )

    return _with_pool(pg, monkeypatch, go)


def _timers(pg: str, run_id: uuid.UUID) -> list[dict]:
    return w.query(
        pg, "SELECT kind, status, ref FROM timers WHERE run_id = %s ORDER BY timer_id", run_id
    )


def _decisions(pg: str, approval_id: str) -> list[dict]:
    return w.query(
        pg,
        "SELECT * FROM agent_messages WHERE type = 'approval_decision'"
        " AND body->>'approval_id' = %s ORDER BY created_at",
        approval_id,
    )


def test_request_approval_prompts_approver_and_pauses_run_timeout(
    world: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval = _request_approval(world, monkeypatch, ttl=5)
    run_id, approval_id = approval["run_id"], approval["approval_id"]
    assert approval["approver_user_id"] == str(w.ADMIN)
    timers = _timers(world, run_id)
    assert [(t["kind"], t["status"]) for t in timers] == [
        ("run_timeout", "cancelled"),
        ("approval_expiry", "pending"),
    ]
    assert timers[1]["ref"]["approval_id"] == approval_id
    [due] = w.query(
        world,
        "SELECT due_at - now() <= interval '5 seconds' AS soon FROM timers"
        " WHERE run_id = %s AND kind = 'approval_expiry'",
        run_id,
    )
    assert due["soon"]
    [prompt] = w.query(world, "SELECT * FROM agent_messages WHERE type = 'whatsapp_out'")
    assert prompt["message_id"] == ids.approval_prompt_id(uuid.UUID(approval_id))
    assert (prompt["from_agent"], prompt["to_agent"]) == (HOUSEHOLD_AGENT, w.WHATSAPP)
    assert prompt["body"]["user_id"] == str(w.ADMIN)
    assert prompt["body"]["template"] == "approval_request"
    # Experience EX-I2: `reply_by` is the expiry timer's `due_at` in the household's zone.
    [timer] = w.query(
        world,
        "SELECT t.due_at, t.created_at, h.timezone FROM timers t, households h"
        " WHERE t.run_id = %s AND t.kind = 'approval_expiry' AND h.household_id = %s",
        run_id,
        w.HOUSEHOLD_ID,
    )
    reply_by = household.format_reply_by(
        timer["due_at"], timer["created_at"], ZoneInfo(timer["timezone"])
    )
    assert prompt["body"]["params"] == {"amount": "$76.00", "reply_by": reply_by}
    assert [b["payload"] for b in prompt["body"]["buttons"]] == [
        f"ap:{approval_id}:approved",
        f"ap:{approval_id}:rejected",
    ]


def test_approver_tap_sends_decision_and_restarts_run_timeout(
    world: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval = _request_approval(world, monkeypatch)
    run_id, approval_id = approval["run_id"], approval["approval_id"]
    assert _tap(world, monkeypatch, "wamid.ok", approval_id, "approved") == run_id

    [row] = w.query(world, "SELECT status, decided_by FROM approvals")
    assert (row["status"], row["decided_by"]) == ("approved", w.ADMIN)
    [decision] = _decisions(world, approval_id)
    assert decision["message_id"] == ids.approval_decision_id(uuid.UUID(approval_id))
    assert (decision["from_agent"], decision["to_agent"]) == (API_AGENT, HOUSEHOLD_AGENT)
    assert decision["body"]["status"] == "approved"
    assert decision["body"]["order_id"] == str(ids.order_id(run_id))
    # The expiry timer is cancelled and the run gets a new run_timeout (§4 timers).
    assert [(t["kind"], t["status"]) for t in _timers(world, run_id)] == [
        ("run_timeout", "cancelled"),
        ("approval_expiry", "cancelled"),
        ("run_timeout", "pending"),
    ]

    # A second tap decides nothing again and sends nothing new.
    assert _tap(world, monkeypatch, "wamid.again", approval_id, "rejected") == run_id
    assert len(_decisions(world, approval_id)) == 1
    assert [r["status"] for r in w.query(world, "SELECT status FROM approvals")] == ["approved"]
    pending = [t["kind"] for t in _timers(world, run_id) if t["status"] == "pending"]
    assert pending == ["run_timeout"]


def test_other_member_tap_refused_and_approval_stays_pending(
    world: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CUJ 7.
    approval = _request_approval(world, monkeypatch)
    run_id, approval_id = approval["run_id"], approval["approval_id"]
    tapped = _tap(world, monkeypatch, "wamid.b", approval_id, "approved", phone=w.MEMBER_PHONE)
    assert tapped == run_id

    assert [r["status"] for r in w.query(world, "SELECT status FROM approvals")] == ["pending"]
    assert _decisions(world, approval_id) == []
    inbound = ids.inbound_id(CHANNEL, "wamid.b")
    [reply] = w.query(
        world, "SELECT * FROM agent_messages WHERE message_id = %s", ids.message_id(inbound, 0)
    )
    assert (reply["from_agent"], reply["to_agent"], reply["type"]) == (
        API_AGENT,
        w.WHATSAPP,
        "whatsapp_out",
    )
    assert reply["body"]["user_id"] == str(w.MEMBER)
    assert reply["body"]["template"] == NOT_APPROVER
    assert reply["body"]["params"] == {"approver": "Asha"}
    [refusal] = w.query(
        world,
        "SELECT payload FROM agent_traces WHERE run_id = %s AND kind = 'tool_result'"
        " AND payload->>'tool' = 'decide_approval'",
        run_id,
    )
    assert refusal["payload"]["refused"] is True
    assert refusal["payload"]["reason"] == "wrong_approver"


@pytest.mark.parametrize("run_cancelled", [False, True])
def test_late_approval_after_expiry_refused(
    world: str, monkeypatch: pytest.MonkeyPatch, run_cancelled: bool
) -> None:
    # CUJ 3: the sweeper expired the approval (and, once the household agent has handled
    # it, the run is `cancelled`); the approver's late tap changes nothing.
    approval = _request_approval(world, monkeypatch)
    run_id, approval_id = approval["run_id"], approval["approval_id"]
    with psycopg.connect(world) as conn:
        conn.execute("UPDATE timers SET due_at = now() WHERE kind = 'approval_expiry'")

    async def expire(conn: psycopg.AsyncConnection) -> None:
        assert await sweeper.sweep_once(conn) == 1
        if run_cancelled:
            assert await runs.finish(conn, run_id, HOUSEHOLD_AGENT, "cancelled", "approval_expired")
        await conn.commit()

    _with_pool(world, monkeypatch, expire)
    [expired] = _decisions(world, approval_id)
    assert expired["from_agent"] == sweeper.SWEEPER

    assert _tap(world, monkeypatch, "wamid.late", approval_id, "approved") == run_id

    [row] = w.query(world, "SELECT status, decided_by FROM approvals")
    assert (row["status"], row["decided_by"]) == ("expired", None)
    [order] = w.query(world, "SELECT state FROM orders")
    assert order["state"] == "expired"
    assert _decisions(world, approval_id) == [expired]  # no approval_decision from api
    inbound = ids.inbound_id(CHANNEL, "wamid.late")
    [reply] = w.query(
        world, "SELECT * FROM agent_messages WHERE message_id = %s", ids.message_id(inbound, 0)
    )
    assert reply["body"]["user_id"] == str(w.ADMIN)
    assert reply["body"]["template"] == APPROVAL_EXPIRED
    assert [t["kind"] for t in _timers(world, run_id) if t["status"] == "pending"] == []
    [run] = w.query(world, "SELECT status FROM runs WHERE run_id = %s", run_id)
    assert run["status"] == ("cancelled" if run_cancelled else "running")


def test_unknown_approval_button_starts_nothing(
    world: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _tap(world, monkeypatch, "wamid.u", str(uuid.uuid4()), "approved") is None
    assert w.query(world, "SELECT * FROM runs") == []
    assert w.query(world, "SELECT * FROM agent_messages") == []


def test_button_starts_nothing_until_approvals_are_built(world: str) -> None:
    assert _ingest(world, "wamid.b", Content("button", button="approve:1")) is None
    assert w.query(world, "SELECT * FROM runs") == []


# The take-turns tap (agents §10, AG7; AG-CUJ 8) ------------------------------------------
#
# The tap goes through the real `set_take_turns`, in process on the test database.


def _completed_dinner(pg: str) -> uuid.UUID:
    """A household dinner that was delivered and whose run has ended; Asha approves."""
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO household_addresses"
            " (address_ref, household_id, line1, city, region, postal_code, country)"
            " VALUES (%s, %s, '1 Main St', 'Cincinnati', 'OH', '45202', 'US')",
            (ids.seed_address_ref(w.HOUSEHOLD_ID), w.HOUSEHOLD_ID),
        )
        conn.execute(
            "INSERT INTO approval_rules (household_id, approver_user_id) VALUES (%s, %s)",
            (w.HOUSEHOLD_ID, w.ADMIN),
        )

    async def go(conn: psycopg.AsyncConnection) -> uuid.UUID:
        trigger = {"user_id": w.ADMIN}
        run_id = await runs.start(conn, household_id=w.HOUSEHOLD_ID, kind="dinner", trigger=trigger)
        order_id = ids.order_id(run_id)
        await conn.execute(
            "INSERT INTO orders (order_id, run_id, household_id, kind, state, currency,"
            " address_ref) VALUES (%s, %s, %s, 'dinner', 'delivered', 'USD', %s)",
            (order_id, run_id, w.HOUSEHOLD_ID, ids.seed_address_ref(w.HOUSEHOLD_ID)),
        )
        assert await runs.finish(conn, run_id, HOUSEHOLD_AGENT, "completed")
        await conn.commit()
        return order_id

    return w.run(pg, go)


def _tt_tap(
    pg: str,
    monkeypatch: pytest.MonkeyPatch,
    external_id: str,
    button: str,
    phone: str = w.ADMIN_PHONE,
) -> uuid.UUID | None:
    async def go(conn: psycopg.AsyncConnection) -> uuid.UUID | None:
        return await ingest(
            conn,
            CHANNEL,
            external_id,
            phone,
            Content("button", button=button),
            call_tool=_call_tool,
        )

    return _with_pool(pg, monkeypatch, go)


def _take_turns_row(pg: str) -> list[dict]:
    return w.query(pg, "SELECT take_turns, set_by FROM household_rules")


def _set_take_turns_traces(pg: str, run_id: uuid.UUID) -> list[dict]:
    return w.query(
        pg,
        "SELECT payload FROM agent_traces WHERE run_id = %s AND kind = 'tool_result'"
        " AND payload->>'tool' = 'set_take_turns'",
        run_id,
    )


def test_tt_tap_after_order_completed_sets_rule(
    world: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    order_id = _completed_dinner(world)
    button = ids.take_turns_button(order_id, True)
    tap_run = _tt_tap(world, monkeypatch, "wamid.tt", button)
    assert tap_run is not None and tap_run != order_id
    # A new `chat` run for the tapping member, ended `completed`.
    run = _run_row(world, tap_run)
    assert (run["kind"], run["household_id"], run["status"]) == (
        "chat",
        w.HOUSEHOLD_ID,
        "completed",
    )
    assert run["trigger"]["user_id"] == str(w.ADMIN)
    [inbound_row] = w.query(world, "SELECT run_id FROM inbound_messages")
    assert inbound_row["run_id"] == tap_run
    # One `set_take_turns` call, in the new run.
    [call] = _set_take_turns_traces(world, tap_run)
    assert call["payload"]["refused"] is False
    assert _take_turns_row(world) == [{"take_turns": True, "set_by": w.ADMIN}]
    # One `take_turns_saved` reply.
    inbound = ids.inbound_id(CHANNEL, "wamid.tt")
    [reply] = _messages(world, tap_run)
    assert reply["message_id"] == ids.message_id(inbound, 0)
    assert (reply["from_agent"], reply["to_agent"], reply["type"]) == (
        API_AGENT,
        w.WHATSAPP,
        "whatsapp_out",
    )
    assert reply["body"] == {
        "user_id": str(w.ADMIN),
        "template": TAKE_TURNS_SAVED,
        "params": {"state": "on"},
        "buttons": [],
    }
    # A later "Stop taking turns" tap switches it back.
    off = _tt_tap(world, monkeypatch, "wamid.tt2", ids.take_turns_button(order_id, False))
    assert off is not None
    [reply] = _messages(world, off)
    assert reply["body"]["params"] == {"state": "off"}
    assert _take_turns_row(world) == [{"take_turns": False, "set_by": w.ADMIN}]


def test_tt_tap_replay_calls_tool_once(world: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # A redelivered webhook (same `external_id`) is dropped by the inbound dedupe, even
    # though the tap commits its run before the tool call.
    order_id = _completed_dinner(world)
    button = ids.take_turns_button(order_id, True)
    runs_before = len(w.query(world, "SELECT run_id FROM runs"))
    tap_run = _tt_tap(world, monkeypatch, "wamid.replay", button)
    assert tap_run is not None
    assert _tt_tap(world, monkeypatch, "wamid.replay", button) is None
    # One new run, one `set_take_turns` call, one `take_turns_saved` reply.
    assert len(w.query(world, "SELECT run_id FROM runs")) == runs_before + 1
    [call] = w.query(
        world,
        "SELECT run_id, payload FROM agent_traces WHERE kind = 'tool_result'"
        " AND payload->>'tool' = 'set_take_turns'",
    )
    assert call["run_id"] == tap_run
    assert call["payload"]["refused"] is False
    [reply] = w.query(world, "SELECT * FROM agent_messages WHERE type = 'whatsapp_out'")
    assert reply["run_id"] == tap_run
    assert reply["body"]["template"] == TAKE_TURNS_SAVED
    assert _take_turns_row(world) == [{"take_turns": True, "set_by": w.ADMIN}]
    [inbound_row] = w.query(world, "SELECT run_id FROM inbound_messages")
    assert inbound_row["run_id"] == tap_run


def test_tt_tap_unchanged_sends_nothing(world: str, monkeypatch: pytest.MonkeyPatch) -> None:
    order_id = _completed_dinner(world)
    # Take-turns is off already (no row).
    tap_run = _tt_tap(world, monkeypatch, "wamid.same", ids.take_turns_button(order_id, False))
    assert tap_run is not None
    assert _run_row(world, tap_run)["status"] == "completed"
    assert len(_set_take_turns_traces(world, tap_run)) == 1
    assert _messages(world, tap_run) == []


def test_tt_tap_by_non_approver_replies(world: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Experience EX-I2: a `wrong_approver` refusal names the approver; nothing changes.
    order_id = _completed_dinner(world)
    with psycopg.connect(world) as conn:
        conn.execute("UPDATE users SET display_name = 'Asha' WHERE user_id = %s", (w.ADMIN,))
    button = ids.take_turns_button(order_id, True)
    tap_run = _tt_tap(world, monkeypatch, "wamid.other", button, phone=w.MEMBER_PHONE)
    assert tap_run is not None
    assert _run_row(world, tap_run)["status"] == "completed"
    [refusal] = _set_take_turns_traces(world, tap_run)
    assert refusal["payload"]["refused"] is True
    assert refusal["payload"]["reason"] == "wrong_approver"
    assert _take_turns_row(world) == []
    inbound = ids.inbound_id(CHANNEL, "wamid.other")
    [reply] = _messages(world, tap_run)
    assert reply["message_id"] == ids.message_id(inbound, 0)
    assert (reply["from_agent"], reply["to_agent"], reply["type"]) == (
        API_AGENT,
        w.WHATSAPP,
        "whatsapp_out",
    )
    assert reply["body"] == {
        "user_id": str(w.MEMBER),
        "template": "take_turns_not_approver",
        "params": {"approver": "Asha"},
        "buttons": [],
    }


def test_tt_tap_unknown_order_sends_nothing(world: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _completed_dinner(world)
    button = ids.take_turns_button(uuid.uuid4(), True)
    tap_run = _tt_tap(world, monkeypatch, "wamid.unknown", button)
    assert tap_run is not None
    [refusal] = _set_take_turns_traces(world, tap_run)
    assert refusal["payload"]["reason"] == "authz_denied"
    assert _take_turns_row(world) == []
    assert _messages(world, tap_run) == []


@pytest.mark.parametrize("payload", ["tt:x:on", "tt:{order}:no", "tt:{order}", "tt:"])
def test_malformed_tt_payload_ignored(
    world: str, monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    order_id = _completed_dinner(world)
    before = w.query(world, "SELECT run_id FROM runs")
    button = payload.format(order=order_id)
    assert _tt_tap(world, monkeypatch, "wamid.bad", button) is None
    assert w.query(world, "SELECT run_id FROM runs") == before
    assert w.query(world, "SELECT * FROM agent_messages WHERE type = 'whatsapp_out'") == []
    assert _take_turns_row(world) == []


def test_retry_resends_original_request_in_new_run(world: str) -> None:
    old = _ingest(world, "wamid.orig", Content("text", text="Weekly shop"))
    assert old is not None
    assert w.run(world, lambda conn: runs.finish(conn, old, API_AGENT, "error", "model_down"))

    new = _ingest(world, "wamid.retry", Content("text", text=" RETRY "))
    assert new is not None and new != old
    trigger = _run_row(world, new)["trigger"]
    assert trigger["retry_of"] == str(old)
    assert trigger["text"] == "Weekly shop"
    retry_inbound = ids.inbound_id(CHANNEL, "wamid.retry")
    [msg] = _messages(world, new)
    assert msg["message_id"] == ids.message_id(retry_inbound, 0)
    assert msg["body"] == {"inbound_id": str(ids.inbound_id(CHANNEL, "wamid.orig"))}


def test_retry_without_target_is_normal_text(world: str) -> None:
    run_id = _ingest(world, "wamid.r", Content("text", text="retry"))
    assert run_id is not None
    trigger = _run_row(world, run_id)["trigger"]
    assert "retry_of" not in trigger and trigger["text"] == "retry"


# Window state (experience §5, EX-I10) ----------------------------------------------------


def _last_inbound(pg: str, phone: str) -> Any:
    [row] = w.query(
        pg, "SELECT last_inbound_at FROM channel_identities WHERE phone_e164 = %s", phone
    )
    return row["last_inbound_at"]


def _received_at(pg: str, external_id: str) -> Any:
    [row] = w.query(
        pg, "SELECT received_at FROM inbound_messages WHERE external_id = %s", external_id
    )
    return row["received_at"]


def test_last_inbound_set_once_per_message(world: str) -> None:
    w.query(world, "UPDATE channel_identities SET last_inbound_at = NULL RETURNING phone_e164")
    assert _ingest(world, "wamid.w1", Content("text", text="Weekly shop")) is not None
    first = _received_at(world, "wamid.w1")
    assert _last_inbound(world, w.MEMBER_PHONE) == first
    # Only the sender's phone moves.
    assert _last_inbound(world, w.ADMIN_PHONE) is None

    # A duplicate delivery returns no row and updates nothing.
    w.query(
        world,
        "UPDATE channel_identities SET last_inbound_at = NULL WHERE phone_e164 = %s"
        " RETURNING phone_e164",
        w.MEMBER_PHONE,
    )
    assert _ingest(world, "wamid.w1", Content("text", text="Weekly shop")) is None
    assert _last_inbound(world, w.MEMBER_PHONE) is None

    # Every content kind counts, media included.
    assert _ingest(world, "wamid.w2", Content("media")) is not None
    assert _last_inbound(world, w.MEMBER_PHONE) == _received_at(world, "wamid.w2")

    # An unknown phone writes no channel identity.
    assert _ingest(world, "wamid.w3", Content("text", text="hi"), phone=w.UNKNOWN_PHONE) is None
    assert (
        w.query(world, "SELECT * FROM channel_identities WHERE phone_e164 = %s", w.UNKNOWN_PHONE)
        == []
    )


def test_last_inbound_set_for_button_taps(world: str) -> None:
    w.query(world, "UPDATE channel_identities SET last_inbound_at = NULL RETURNING phone_e164")
    # A button that starts nothing still counts as the person writing.
    assert _ingest(world, "wamid.tap", Content("button", button="approve:1")) is None
    assert _last_inbound(world, w.MEMBER_PHONE) == _received_at(world, "wamid.tap")
    assert _ingest(world, "wamid.sf", Content("button", button="sf:not-a-uuid:1:drop")) is None
    assert _last_inbound(world, w.MEMBER_PHONE) == _received_at(world, "wamid.sf")


# Text commands and page taps (experience §7, §10; EX-I34) -------------------------------


def _patch_links(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings.from_env(
        {
            "SERVICE_NAME": "migrate",
            "DATABASE_URL": "x",
            "LOG_LEVEL": "info",
            "OPENFGA_PRESHARED_KEY": "x",
            "OPENFGA_API_URL": "x",
        }
    )
    monkeypatch.setattr(links, "get_settings", lambda: settings)


def _code_run_reply(pg: str, run_id: uuid.UUID, inbound: str) -> dict:
    """The one `whatsapp_out` of a code-only run, after checking the run (experience §3)."""
    run = _run_row(pg, run_id)
    assert (run["kind"], run["household_id"], run["status"]) == ("chat", None, "completed")
    assert run["trigger"] == {
        "source": "code",
        "user_id": str(w.MEMBER),
        "inbound_id": str(ids.inbound_id(CHANNEL, inbound)),
    }
    [msg] = _messages(pg, run_id)
    assert msg["message_id"] == ids.message_id(ids.inbound_id(CHANNEL, inbound), 0)
    assert (msg["from_agent"], msg["to_agent"], msg["type"]) == (
        API_AGENT,
        w.WHATSAPP,
        "whatsapp_out",
    )
    assert msg["body"]["user_id"] == str(w.MEMBER)
    return msg


def _send(pg: str, msg: dict) -> dict:
    """Send one `whatsapp_out` through the sim sender; return its `sim_outbound` body."""
    row = AgentMessageRow(
        message_id=msg["message_id"],
        run_id=msg["run_id"],
        conversation_id=None,
        causation_id=None,
        from_agent=msg["from_agent"],
        to_agent=msg["to_agent"],
        type=msg["type"],
        body=msg["body"],
    )
    inbound = Inbound(row=row, attempts=1, body=WhatsAppOut.model_validate(msg["body"]))
    asyncio.run(sender.Sender(w.connect_to(pg))(inbound))
    [out] = w.query(pg, "SELECT body FROM sim_outbound WHERE run_id = %s", msg["run_id"])
    return out["body"]


def _link_rows(pg: str) -> list[dict]:
    return w.query(
        pg, "SELECT purpose, target_user_id, sent_to_user_id, agent_ids FROM consent_links"
    )


def _me_link(next_page: str | None) -> dict:
    link: dict = {"household_id": w.HOUSEHOLD_ID, "agent_ids": [], "purpose": "me"}
    if next_page is not None:
        link["next"] = next_page
    return link


@pytest.mark.parametrize(
    ("text", "next_page"), [("my page", None), (" My Page ", None), ("MY AGENTS", "agents")]
)
def test_my_page_sends_link(
    world: str, monkeypatch: pytest.MonkeyPatch, text: str, next_page: str | None
) -> None:
    _patch_links(monkeypatch)
    run_id = _ingest(world, "wamid.page", Content("text", text=text))
    assert run_id is not None
    msg = _code_run_reply(world, run_id, "wamid.page")
    assert msg["body"]["template"] == ME_LINK
    assert msg["body"]["link"] == _me_link(next_page)
    # No token is stored in the message (identity-authz I-G8): the sender issues it.
    assert _link_rows(world) == []
    sent = _send(world, msg)["graph"]["text"]["body"]
    assert sent.startswith("Here's your page. The link works for 10 minutes: https://")
    assert "/me/l/" in sent
    assert ("?next=" in sent) == (next_page is not None)
    assert _link_rows(world) == [
        {
            "purpose": "me",
            "target_user_id": w.MEMBER,
            "sent_to_user_id": w.MEMBER,
            "agent_ids": [],
        }
    ]


def test_my_household_sends_link(world: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_links(monkeypatch)
    run_id = _ingest(world, "wamid.hh", Content("text", text="my household"))
    assert run_id is not None
    msg = _code_run_reply(world, run_id, "wamid.hh")
    assert msg["body"]["template"] == ME_LINK
    assert msg["body"]["link"] == _me_link("household")
    assert _send(world, msg)["graph"]["text"]["body"].endswith("?next=household")
    # Not a command: a longer text goes to the consumer agent as before.
    other = _ingest(world, "wamid.hh2", Content("text", text="my household is hungry"))
    assert other is not None
    assert _run_row(world, other)["household_id"] == w.HOUSEHOLD_ID
    [user_msg] = _messages(world, other)
    assert user_msg["type"] == "user_message"


def _reminders_off_at(pg: str, user_id: uuid.UUID = w.MEMBER) -> Any:
    [row] = w.query(
        pg, "SELECT reminders_off_at FROM channel_identities WHERE user_id = %s", user_id
    )
    return row["reminders_off_at"]


def test_reminders_off_on(world: str) -> None:
    assert _reminders_off_at(world) is None
    off = _ingest(world, "wamid.off", Content("text", text="Reminders off"))
    assert off is not None
    msg = _code_run_reply(world, off, "wamid.off")
    assert msg["body"] == {
        "user_id": str(w.MEMBER),
        "template": REMINDERS_OFF,
        "params": {},
        "buttons": [],
    }
    first = _reminders_off_at(world)
    assert first is not None

    # The "Stop these" tap does the same and keeps the first opt-out time.
    tap = _ingest(world, "wamid.rs", Content("button", button=ids.REMINDER_STOP))
    assert tap is not None
    assert _code_run_reply(world, tap, "wamid.rs")["body"]["template"] == REMINDERS_OFF
    assert _reminders_off_at(world) == first

    on = _ingest(world, "wamid.on", Content("text", text="reminders on"))
    assert on is not None
    assert _code_run_reply(world, on, "wamid.on")["body"]["template"] == REMINDERS_ON
    assert _reminders_off_at(world) is None
    # Only the person who wrote is changed.
    assert _reminders_off_at(world, w.ADMIN) is None
    # A code-only run is never the retry target (experience EX-A16).
    assert w.run(world, lambda conn: runs.retry_target(conn, w.MEMBER)) is None


@pytest.mark.parametrize("page", ["household", "pantry", "story"])
def test_page_button_sends_link(world: str, monkeypatch: pytest.MonkeyPatch, page: str) -> None:
    _patch_links(monkeypatch)
    external_id = f"wamid.me.{page}"
    run_id = _ingest(world, external_id, Content("button", button=ids.page_button(page)))
    assert run_id is not None
    msg = _code_run_reply(world, run_id, external_id)
    assert msg["body"]["template"] == ME_LINK
    assert msg["body"]["link"] == _me_link(page)
    assert _send(world, msg)["graph"]["text"]["body"].endswith(f"?next={page}")
    [link] = _link_rows(world)
    assert link["purpose"] == "me"
    [inbound] = w.query(world, "SELECT run_id FROM inbound_messages")
    assert inbound["run_id"] == run_id


@pytest.mark.parametrize("payload", ["me:", "me:agents", "me:renew:x", "rs:start", "rs:"])
def test_malformed_page_or_stop_button_ignored(world: str, payload: str) -> None:
    assert _ingest(world, "wamid.bad", Content("button", button=payload)) is None
    assert w.query(world, "SELECT * FROM runs") == []
    assert _reminders_off_at(world) is None


@pytest.mark.parametrize(
    ("phone", "user_id", "agent_ids"),
    [
        (w.MEMBER_PHONE, w.MEMBER, [w.MEMBER_CONSUMER]),
        (w.ADMIN_PHONE, w.ADMIN, [w.ADMIN_CONSUMER, HOUSEHOLD_AGENT]),
    ],
)
def test_renew_button_sends_consent_link(
    world: str,
    monkeypatch: pytest.MonkeyPatch,
    phone: str,
    user_id: uuid.UUID,
    agent_ids: list[str],
) -> None:
    _patch_links(monkeypatch)
    run_id = _ingest(world, "wamid.renew", Content("button", button="me:renew"), phone=phone)
    assert run_id is not None
    run = _run_row(world, run_id)
    assert (run["household_id"], run["status"], run["trigger"]["source"]) == (
        None,
        "completed",
        "code",
    )
    [msg] = _messages(world, run_id)
    assert msg["body"]["user_id"] == str(user_id)
    assert msg["body"]["template"] == ME_LINK
    assert msg["body"]["link"] == {
        "household_id": w.HOUSEHOLD_ID,
        "agent_ids": agent_ids,
        "purpose": "consent",
    }
    assert "?next=" not in _send(world, msg)["graph"]["text"]["body"]
    assert _link_rows(world) == [
        {
            "purpose": "consent",
            "target_user_id": user_id,
            "sent_to_user_id": user_id,
            "agent_ids": agent_ids,
        }
    ]


# Setup taps (experience §9, EX-I30) ------------------------------------------------------
#
# The tap goes through the real `create_child` (an eval household, so no Keycloak) and the
# real `apply_setup`, in process on the test database.

JOIN_PHONE = "+15550100377"
BUSINESS_NUMBER = "+15550109999"
SETUP_ANSWER: dict[str, Any] = {
    "people": [
        {"name": "Asha", "child": False, "user_id": str(w.ADMIN), "add_allergies": ["sesame"]},
        {"name": "Mira", "child": True, "add_allergies": ["peanuts"]},
        {"name": "Kavi", "child": True, "add_diets": ["vegan"]},
    ],
    "budget_minor": 15000,
    "unmatched": [],
}


@pytest.fixture
def setup_world(world: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """The admin is Asha; OpenFGA writes are recorded, not sent; `api` knows its number."""
    with psycopg.connect(world) as conn:
        conn.execute("UPDATE users SET display_name = 'Asha' WHERE user_id = %s", (w.ADMIN,))
    monkeypatch.setattr(fga, "ensure", lambda tuples: None)
    env = {
        "SERVICE_NAME": "migrate",
        "DATABASE_URL": "x",
        "LOG_LEVEL": "info",
        "OPENFGA_PRESHARED_KEY": "x",
        "OPENFGA_API_URL": "x",
        "WHATSAPP_NUMBER": BUSINESS_NUMBER,
    }
    settings = Settings.from_env(env)
    monkeypatch.setattr(ingest_module, "get_settings", lambda: settings)
    return world


def _propose(pg: str, answer: dict[str, Any] | None = None, *, expired: bool = False) -> uuid.UUID:
    proposal_id = uuid.uuid4()
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO onboarding_proposals"
            " (proposal_id, household_id, proposed_by, answer, expires_at)"
            " VALUES (%s, %s, %s, %s, now() + %s * interval '1 hour')",
            (
                proposal_id,
                w.HOUSEHOLD_ID,
                w.ADMIN,
                Jsonb(SETUP_ANSWER if answer is None else answer),
                -1 if expired else 1,
            ),
        )
    return proposal_id


class _Calls:
    """Records `create_child` and `apply_setup` calls in order; `fail_on` makes
    `create_child` raise a Keycloak error for that name."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail_on = fail_on

    async def add_child(
        self, conn: psycopg.AsyncConnection, admin: uuid.UUID, household_id: str, name: str
    ) -> uuid.UUID:
        self.calls.append(("create_child", name))
        if name == self.fail_on:
            raise KeycloakError("POST /users answered 503")
        return await onboarding.create_child(conn, admin, household_id, name)

    async def call_tool(self, server: str, tool: str, args: dict, *, ctx: RunContext) -> dict:
        self.calls.append((tool, args))
        return await _call_tool(server, tool, args, ctx=ctx)


def _ob_tap(
    pg: str,
    monkeypatch: pytest.MonkeyPatch,
    external_id: str,
    button: str,
    calls: _Calls | None = None,
    phone: str = w.ADMIN_PHONE,
) -> uuid.UUID | None:
    calls = calls or _Calls()

    async def go(conn: psycopg.AsyncConnection) -> uuid.UUID | None:
        return await ingest(
            conn,
            CHANNEL,
            external_id,
            phone,
            Content("button", button=button),
            call_tool=calls.call_tool,
            add_child=calls.add_child,
        )

    return _with_pool(pg, monkeypatch, go)


def _replies(pg: str, run_id: uuid.UUID, external_id: str) -> list[dict]:
    """The tap's replies by ordinal `ids.message_id(inbound, n)`, in order."""
    inbound = ids.inbound_id(CHANNEL, external_id)
    rows = {m["message_id"]: m for m in _messages(pg, run_id) if m["type"] == "whatsapp_out"}
    out = []
    for n in range(len(rows)):
        msg = rows.pop(ids.message_id(inbound, n))
        assert (msg["from_agent"], msg["to_agent"]) == (API_AGENT, w.WHATSAPP)
        out.append(msg["body"])
    assert rows == {}, "every reply has an ordinal"
    return out


def _children(pg: str) -> list[dict]:
    return w.query(
        pg,
        "SELECT u.display_name, m.user_id FROM household_members m"
        " JOIN users u ON u.user_id = m.user_id"
        " WHERE m.household_id = %s AND m.role = 'child' ORDER BY u.display_name",
        w.HOUSEHOLD_ID,
    )


def _proposal_status(pg: str, proposal_id: uuid.UUID) -> str:
    [row] = w.query(
        pg, "SELECT status FROM onboarding_proposals WHERE proposal_id = %s", proposal_id
    )
    return row["status"]


def _saved(pg: str) -> dict[str, Any]:
    return {
        "constraints": w.query(
            pg, "SELECT user_id, kind, value FROM member_constraints ORDER BY user_id, kind, value"
        ),
        "budgets": w.query(pg, "SELECT amount_minor FROM budgets"),
        "join_codes": w.query(pg, "SELECT code FROM join_codes"),
    }


def _body(template: str, params: dict | None = None, buttons: list | None = None) -> dict:
    return {
        "user_id": str(w.ADMIN),
        "template": template,
        "params": params or {},
        "buttons": buttons or [],
    }


def test_setup_tap_calls_create_child_then_apply(
    setup_world: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = _propose(setup_world)
    calls = _Calls()
    run_id = _ob_tap(setup_world, monkeypatch, "wamid.ob", ids.setup_button(proposal, "yes"), calls)
    assert run_id is not None
    # A new household `chat` run for the tapper, `source: "tap"`, ended `completed`.
    run = _run_row(setup_world, run_id)
    assert (run["kind"], run["household_id"], run["status"], run["reason"]) == (
        "chat",
        w.HOUSEHOLD_ID,
        "completed",
        "apply_setup",
    )
    assert (run["trigger"]["source"], run["trigger"]["user_id"]) == ("tap", str(w.ADMIN))
    [inbound_row] = w.query(setup_world, "SELECT run_id FROM inbound_messages")
    assert inbound_row["run_id"] == run_id
    # `create_child` for each new child, in answer order, then `apply_setup` with their ids.
    kids = {c["display_name"]: str(c["user_id"]) for c in _children(setup_world)}
    assert sorted(kids) == ["Kavi", "Mira"]
    assert calls.calls == [
        ("create_child", "Mira"),
        ("create_child", "Kavi"),
        (
            "apply_setup",
            {
                "proposal_id": str(proposal),
                "answer": "yes",
                "removal": None,
                "child_user_ids": kids,
                "user_id": str(w.ADMIN),
            },
        ),
    ]
    assert _proposal_status(setup_world, proposal) == "applied"
    saved = _saved(setup_world)
    assert sorted(
        (str(c["user_id"]), c["kind"], c["value"]) for c in saved["constraints"]
    ) == sorted(
        [
            (str(w.ADMIN), "allergy", "sesame"),
            (kids["Mira"], "allergy", "peanuts"),
            (kids["Kavi"], "diet", "vegan"),
        ]
    )
    assert saved["budgets"] == [{"amount_minor": 15000}]
    # The tap again: no second child, `already_decided`.
    again = _ob_tap(setup_world, monkeypatch, "wamid.ob2", ids.setup_button(proposal, "yes"))
    assert again is not None
    assert len(_children(setup_world)) == 2
    assert _replies(setup_world, again, "wamid.ob2") == [_body("already_decided")]
    assert _saved(setup_world) == saved


def test_setup_tap_keycloak_failure_writes_nothing(
    setup_world: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = _propose(setup_world)
    before = _saved(setup_world)
    calls = _Calls(fail_on="Kavi")
    button = ids.setup_button(proposal, "yes")
    run_id = _ob_tap(setup_world, monkeypatch, "wamid.kc", button, calls)
    assert run_id is not None
    # Mira was created before Kavi failed; neither is kept, and `apply_setup` is not called.
    assert calls.calls == [("create_child", "Mira"), ("create_child", "Kavi")]
    assert _children(setup_world) == []
    assert (
        w.query(
            setup_world,
            "SELECT 1 FROM agent_cards WHERE starts_with(agent_id, %s)",
            f"consumer:{w.HOUSEHOLD_ID}.",
        )
        == []
    )
    assert _saved(setup_world) == before
    assert _proposal_status(setup_world, proposal) == "pending"
    assert _replies(setup_world, run_id, "wamid.kc") == [_body("retry_prompt")]
    run = _run_row(setup_world, run_id)
    assert (run["status"], run["reason"]) == ("completed", "create_child_failed")
    # The tap can simply be sent again.
    retried = _ob_tap(setup_world, monkeypatch, "wamid.kc2", button)
    assert retried is not None
    assert [c["display_name"] for c in _children(setup_world)] == ["Kavi", "Mira"]
    assert _proposal_status(setup_world, proposal) == "applied"


def test_setup_tap_no_discards(setup_world: str, monkeypatch: pytest.MonkeyPatch) -> None:
    proposal = _propose(setup_world)
    before = _saved(setup_world)
    calls = _Calls()
    run_id = _ob_tap(setup_world, monkeypatch, "wamid.no", ids.setup_button(proposal, "no"), calls)
    assert run_id is not None
    # No child is created for "No".
    assert [c[0] for c in calls.calls] == ["apply_setup"]
    assert _children(setup_world) == []
    assert _saved(setup_world) == before
    assert _proposal_status(setup_world, proposal) == "discarded"
    assert _replies(setup_world, run_id, "wamid.no") == [_body("setup_discarded")]
    # "Yes" after "No" has expired.
    late = _ob_tap(setup_world, monkeypatch, "wamid.no2", ids.setup_button(proposal, "yes"))
    assert late is not None
    assert _children(setup_world) == []
    assert _replies(setup_world, late, "wamid.no2") == [_body("setup_expired")]


def test_setup_tap_remove_then_keep(setup_world: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with psycopg.connect(setup_world) as conn:
        for kind, value in (("allergy", "sesame"), ("diet", "vegan")):
            conn.execute(
                "INSERT INTO member_constraints (user_id, kind, value) VALUES (%s, %s, %s)",
                (w.MEMBER, kind, value),
            )
    answer = {
        "people": [
            {
                "name": "Sam",
                "child": False,
                "user_id": str(w.MEMBER),
                "remove_allergies": ["sesame"],
                "remove_diets": ["vegan"],
            }
        ],
        "budget_minor": None,
        "unmatched": [],
    }
    proposal = _propose(setup_world, answer)

    def sams() -> list[tuple[str, str]]:
        rows = w.query(
            setup_world, "SELECT kind, value FROM member_constraints WHERE user_id = %s", w.MEMBER
        )
        return sorted((r["kind"], r["value"]) for r in rows)

    yes = _ob_tap(setup_world, monkeypatch, "wamid.rm", ids.setup_button(proposal, "yes"))
    assert yes is not None
    assert [r["template"] for r in _replies(setup_world, yes, "wamid.rm")] == [
        "setup_saved",
        "setup_remove_confirm",
        "setup_remove_confirm",
    ]
    assert sams() == [("allergy", "sesame"), ("diet", "vegan")]  # nothing removed yet

    remove = _ob_tap(setup_world, monkeypatch, "wamid.rm0", ids.setup_button(proposal, "remove", 0))
    assert remove is not None
    assert sams() == [("diet", "vegan")]
    assert _replies(setup_world, remove, "wamid.rm0") == []  # not the last removal
    assert _proposal_status(setup_world, proposal) == "awaiting_remove"

    keep = _ob_tap(setup_world, monkeypatch, "wamid.rm1", ids.setup_button(proposal, "keep", 1))
    assert keep is not None
    assert sams() == [("diet", "vegan")]
    assert _replies(setup_world, keep, "wamid.rm1") == [_body("setup_saved")]
    assert _proposal_status(setup_world, proposal) == "applied"

    # A second answer to the same removal is already decided and changes nothing.
    late = _ob_tap(setup_world, monkeypatch, "wamid.rm2", ids.setup_button(proposal, "keep", 0))
    assert late is not None
    assert _replies(setup_world, late, "wamid.rm2") == [_body("already_decided")]
    assert sams() == [("diet", "vegan")]


@pytest.mark.parametrize("children", [True, False])
def test_setup_tap_non_admin(
    setup_world: str, monkeypatch: pytest.MonkeyPatch, children: bool
) -> None:
    answer = SETUP_ANSWER if children else {**SETUP_ANSWER, "people": SETUP_ANSWER["people"][:1]}
    proposal = _propose(setup_world, answer)
    before = _saved(setup_world)
    calls = _Calls()
    button = ids.setup_button(proposal, "yes")
    run_id = _ob_tap(setup_world, monkeypatch, "wamid.na", button, calls, phone=w.MEMBER_PHONE)
    assert run_id is not None
    # With new children, `create_child` refuses the member and `apply_setup` is not
    # called; without, `apply_setup` refuses. Either way nothing is written.
    tools = [c[0] for c in calls.calls]
    assert tools == (["create_child"] if children else ["apply_setup"])
    assert _children(setup_world) == []
    assert _saved(setup_world) == before
    assert _proposal_status(setup_world, proposal) == "pending"
    [reply] = _replies(setup_world, run_id, "wamid.na")
    assert reply == {
        "user_id": str(w.MEMBER),
        "template": "setup_not_admin",
        "params": {"admin": "Asha"},
        "buttons": [],
    }
    run = _run_row(setup_world, run_id)
    assert (run["status"], run["reason"]) == ("completed", "not_admin")


def test_setup_tap_expired(setup_world: str, monkeypatch: pytest.MonkeyPatch) -> None:
    expired = _propose(setup_world, expired=True)
    older = _propose(setup_world)
    with psycopg.connect(setup_world) as conn:
        conn.execute(
            "UPDATE onboarding_proposals SET status = 'superseded' WHERE proposal_id = %s",
            (older,),
        )
    before = _saved(setup_world)
    for n, proposal in enumerate((expired, older)):
        for answer in ("yes", "no"):
            calls = _Calls()
            external_id = f"wamid.exp{n}{answer}"
            button = ids.setup_button(proposal, answer)  # type: ignore[arg-type]
            run_id = _ob_tap(setup_world, monkeypatch, external_id, button, calls)
            assert run_id is not None
            # No child is created for a proposal that cannot be applied.
            assert [c[0] for c in calls.calls] == ["apply_setup"]
            assert _replies(setup_world, run_id, external_id) == [_body("setup_expired")]
    assert _children(setup_world) == []
    assert _saved(setup_world) == before


def test_setup_tap_reply_order(setup_world: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with psycopg.connect(setup_world) as conn:
        conn.execute("UPDATE users SET display_name = 'Sam' WHERE user_id = %s", (w.MEMBER,))
        conn.execute(
            "INSERT INTO member_constraints (user_id, kind, value) VALUES (%s, 'allergy', 'fish')",
            (w.MEMBER,),
        )
    answer = {
        "people": [
            {"name": "Mira", "child": True, "add_allergies": ["peanuts"]},
            {"name": "Sam", "child": False, "user_id": str(w.MEMBER), "remove_allergies": ["fish"]},
            {"name": "Ravi", "phone": JOIN_PHONE, "child": False, "add_allergies": ["milk"]},
        ],
        "budget_minor": 12000,
        "unmatched": [],
    }
    proposal = _propose(setup_world, answer)
    run_id = _ob_tap(setup_world, monkeypatch, "wamid.order", ids.setup_button(proposal, "yes"))
    assert run_id is not None
    [code] = w.query(setup_world, "SELECT code FROM join_codes")
    # setup_saved, then the removal confirms, a join_link per new adult, and setup_complete
    # because the budget was just set; ordinals 0, 1, 2, 3.
    assert _replies(setup_world, run_id, "wamid.order") == [
        _body("setup_saved"),
        _body(
            "setup_remove_confirm",
            {"value": "fish", "name": "Sam"},
            [
                {"payload": ids.setup_button(proposal, "remove", 0), "title": "Yes, remove"},
                {"payload": ids.setup_button(proposal, "keep", 0), "title": "Keep"},
            ],
        ),
        _body(
            "join_link",
            {
                "name": "Ravi",
                "phone": JOIN_PHONE,
                "link": f"https://wa.me/15550109999?text=JOIN%20{code['code']}",
            },
        ),
        _body("setup_complete"),
    ]
    # Every reply passes the sender's validation.
    for body in _replies(setup_world, run_id, "wamid.order"):
        WhatsAppOut.model_validate(body)


# Joining (experience §3, §9, §16; EX-I32) -----------------------------------------------

JOINER_PHONE = "+15550100777"
JOIN_CODE = "K7Q2M3XD"
JOIN_TEXT = Content("text", text=f"  join {JOIN_CODE.lower()} ")


def _open_code(pg: str, *, phone: str = JOINER_PHONE, days: int = 7) -> None:
    w.query(
        pg,
        "INSERT INTO join_codes (code, household_id, phone_e164, display_name, pending,"
        " created_by, expires_at)"
        " VALUES (%s, %s, %s, 'Sam', %s::jsonb, %s, now() + make_interval(days => %s))"
        " RETURNING code",
        JOIN_CODE,
        w.HOUSEHOLD_ID,
        phone,
        '{"allergies": ["peanuts"], "diets": ["vegetarian"]}',
        w.ADMIN,
        days,
    )


def _join(
    pg: str,
    monkeypatch: pytest.MonkeyPatch,
    external_id: str,
    content: Content = JOIN_TEXT,
    phone: str = JOINER_PHONE,
    call_tool: Any = _call_tool,
) -> tuple[uuid.UUID | None, list[str]]:
    """Ingest one message; return the run and the direct replies, by template."""
    replies: list[str] = []

    async def unknown(to: str) -> None:
        replies.append(f"unknown_number:{to}")

    async def retry(to: str) -> None:
        replies.append(f"retry_prompt:{to}")

    async def go(conn: psycopg.AsyncConnection) -> uuid.UUID | None:
        return await ingest(
            conn,
            CHANNEL,
            external_id,
            phone,
            content,
            call_tool=call_tool,
            reply_unknown=unknown,
            reply_retry=retry,
        )

    return _with_pool(pg, monkeypatch, go), replies


@pytest.fixture
def tuples(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    written: list[Any] = []
    monkeypatch.setattr(fga, "ensure", written.extend)
    return written


def _joiner(pg: str) -> uuid.UUID:
    [row] = w.query(
        pg, "SELECT user_id FROM channel_identities WHERE phone_e164 = %s", JOINER_PHONE
    )
    return row["user_id"]


def _join_row(pg: str) -> dict:
    [row] = w.query(pg, "SELECT used_at, used_by FROM join_codes WHERE code = %s", JOIN_CODE)
    return row


def _nobody_joined(pg: str) -> None:
    assert w.query(pg, "SELECT 1 FROM channel_identities WHERE phone_e164 = %s", JOINER_PHONE) == []
    assert w.query(pg, "SELECT * FROM runs") == []
    assert _join_row(pg)["used_at"] is None


def test_join_valid(world: str, monkeypatch: pytest.MonkeyPatch, tuples: list[Any]) -> None:
    _open_code(world)

    run_id, replies = _join(world, monkeypatch, "wamid.join")

    assert run_id is not None and replies == []
    user = _joiner(world)
    inbound = ids.inbound_id(CHANNEL, "wamid.join")
    run = _run_row(world, run_id)
    assert (run["kind"], run["household_id"], run["status"], run["reason"]) == (
        "chat",
        w.HOUSEHOLD_ID,
        "completed",
        "joined",
    )
    assert run["trigger"] == {
        "source": "join",
        "user_id": str(user),
        "inbound_id": str(inbound),
    }
    [member] = w.query(
        world, "SELECT household_id, role FROM household_members WHERE user_id = %s", user
    )
    assert member == {"household_id": w.HOUSEHOLD_ID, "role": "member"}
    rows = w.query(
        world,
        "SELECT kind, value, source FROM member_constraints WHERE user_id = %s ORDER BY kind",
        user,
    )
    assert rows == [
        {"kind": "allergy", "value": "peanuts", "source": "join"},
        {"kind": "diet", "value": "vegetarian", "source": "join"},
    ]
    assert _join_row(world)["used_by"] == user

    messages = {m["message_id"]: m for m in _messages(world, run_id)}
    assert len(messages) == 2
    joined = messages[ids.message_id(inbound, 0)]["body"]
    assert joined["template"] == "joined"
    assert joined["user_id"] == str(user)
    [house] = w.query(world, "SELECT name FROM households WHERE household_id = %s", w.HOUSEHOLD_ID)
    assert joined["params"] == {"name": "Sam", "household": house["name"]}
    consent = messages[ids.message_id(inbound, 1)]["body"]
    assert consent["template"] == ME_LINK
    [card] = w.query(world, "SELECT agent_id FROM agent_cards WHERE owner_ref = %s", f"user:{user}")
    assert consent["link"] == {
        "household_id": w.HOUSEHOLD_ID,
        "agent_ids": [card["agent_id"]],
        "purpose": "consent",
    }
    [decision] = w.query(
        world, "SELECT payload FROM agent_traces WHERE run_id = %s AND kind = 'decision'", run_id
    )
    assert (decision["payload"]["node"], decision["payload"]["choice"]) == ("join", "joined")
    [inbound_row] = w.query(world, "SELECT run_id FROM inbound_messages")
    assert inbound_row["run_id"] == run_id


def test_join_wrong_phone(world: str, monkeypatch: pytest.MonkeyPatch, tuples: list[Any]) -> None:
    _open_code(world)

    run_id, replies = _join(world, monkeypatch, "wamid.x", phone=w.UNKNOWN_PHONE)

    assert run_id is None
    assert replies == [f"unknown_number:{w.UNKNOWN_PHONE}"]
    _nobody_joined(world)
    assert tuples == []


def test_join_expired_code_gets_unknown_number(
    world: str, monkeypatch: pytest.MonkeyPatch, tuples: list[Any]
) -> None:
    _open_code(world, days=-1)

    run_id, replies = _join(world, monkeypatch, "wamid.x")

    assert run_id is None and replies == [f"unknown_number:{JOINER_PHONE}"]
    _nobody_joined(world)


def test_join_joined_inside_window(
    world: str, monkeypatch: pytest.MonkeyPatch, tuples: list[Any]
) -> None:
    _open_code(world)

    run_id, _ = _join(world, monkeypatch, "wamid.join")

    assert run_id is not None
    assert _last_inbound(world, JOINER_PHONE) == _received_at(world, "wamid.join")
    # Through the sim sender: only a message inside the window is sent as text.
    w.query(world, "UPDATE channel_identities SET channel_mode = 'sim' RETURNING 1")
    inbound = ids.inbound_id(CHANNEL, "wamid.join")
    [msg] = [m for m in _messages(world, run_id) if m["message_id"] == ids.message_id(inbound, 0)]
    assert "Welcome, Sam." in _send(world, msg)["graph"]["text"]["body"]


def test_unknown_phone_reply_unchanged(
    world: str, monkeypatch: pytest.MonkeyPatch, tuples: list[Any]
) -> None:
    _open_code(world)

    for n, text in enumerate(("hi", "JOIN", "JOIN AAAAAAAA", f"JOIN {JOIN_CODE} please")):
        run_id, replies = _join(world, monkeypatch, f"wamid.{n}", Content("text", text=text))
        assert run_id is None and replies == [f"unknown_number:{JOINER_PHONE}"], text
    _nobody_joined(world)


def test_join_create_person_failure_writes_nothing(
    world: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_code(world)

    def down(tuples: Any) -> None:
        raise ConnectionError("openfga is down")

    monkeypatch.setattr(fga, "ensure", down)
    run_id, replies = _join(world, monkeypatch, "wamid.join")

    assert run_id is None and replies == [f"retry_prompt:{JOINER_PHONE}"]
    _nobody_joined(world)
    assert w.query(world, "SELECT 1 FROM users WHERE display_name = 'Sam'") == []

    # The JOIN can be repeated.
    monkeypatch.setattr(fga, "ensure", lambda tuples: None)
    run_id, replies = _join(world, monkeypatch, "wamid.again")
    assert run_id is not None and replies == []
    assert _join_row(world)["used_by"] == _joiner(world)


def test_join_refused_can_be_sent_again(
    world: str, monkeypatch: pytest.MonkeyPatch, tuples: list[Any]
) -> None:
    """The person is registered but `complete_join` refused: the code stays open, and the
    repeated JOIN from the now-known phone finishes the join."""
    _open_code(world)

    async def refuse(*args: Any, **kwargs: Any) -> dict:
        raise ToolRefused("join_code_invalid")

    run_id, _ = _join(world, monkeypatch, "wamid.join", call_tool=refuse)

    assert run_id is not None
    run = _run_row(world, run_id)
    assert (run["status"], run["reason"]) == ("error", "join_code_invalid")
    assert [m["body"]["template"] for m in _messages(world, run_id)] == ["retry_prompt"]
    assert _join_row(world)["used_at"] is None

    again, _ = _join(world, monkeypatch, "wamid.again")

    assert again is not None and again != run_id
    assert _run_row(world, again)["status"] == "completed"
    assert _join_row(world)["used_by"] == _joiner(world)


def test_join_from_another_households_member_is_normal_text(
    world: str, monkeypatch: pytest.MonkeyPatch, tuples: list[Any]
) -> None:
    _open_code(world, phone=w.REAL_PHONE)

    run_id, replies = _join(world, monkeypatch, "wamid.x", phone=w.REAL_PHONE)

    assert replies == []
    assert run_id is not None and _run_row(world, run_id)["trigger"]["source"] == CHANNEL
    assert _join_row(world)["used_at"] is None


def test_join_refused_grant_keeps_code_open(
    world: str, monkeypatch: pytest.MonkeyPatch, tuples: list[Any]
) -> None:
    _open_code(world)
    w.query(
        world,
        "UPDATE agent_grants SET status = 'revoked', revoke_reason = 'user'"
        " WHERE agent_id = %s RETURNING 1",
        HOUSEHOLD_AGENT,
    )

    run_id, replies = _join(world, monkeypatch, "wamid.join")

    assert run_id is not None and replies == []
    run = _run_row(world, run_id)
    assert (run["household_id"], run["status"]) == (None, "completed")
    assert _join_row(world)["used_at"] is None
