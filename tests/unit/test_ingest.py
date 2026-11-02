"""`services/api/ingest.py` (architecture item 33; issues #40 and #63)."""

import asyncio
import uuid
from typing import Any

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from services.agents import sweeper
from services.api.ingest import (
    API_AGENT,
    APPROVAL_EXPIRED,
    MEDIA_NOT_SUPPORTED,
    NOT_APPROVER,
    Content,
    ingest,
)
from services.common import db, ids, runs
from services.common.mcp_client import ToolRefused
from services.common.run_context import RunContext
from services.identity import fga, tokens
from services.mcp.household import server as household
from services.mcp.meta import CallContext
from services.safety.guard import GuardRefused
from tests.unit import api_world as w

pytestmark = pytest.mark.db

CHANNEL = "whatsapp"
SECRET = "unit-test-mock-token-secret-32-bytes-long"
HOUSEHOLD_AGENT = f"household:{w.HOUSEHOLD_ID}"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    w.patch_settings(monkeypatch)
    monkeypatch.setattr(household, "get_settings", runs.get_settings)  # the patched settings
    monkeypatch.setattr(tokens, "_secret", lambda: SECRET)
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


async def _call_tool(server: str, tool: str, args: dict, *, ctx: RunContext, token: str) -> dict:
    """`mcp_client.call_tool` for the in-process household server."""
    assert server == "household"
    call = CallContext(
        run_id=ctx.run_id,
        agent_id=ctx.agent_id,
        conversation_id=ctx.conversation_id,
        attempt=1,
        token=token,
        env="eval",
        modes={},
        on_behalf_of=None,
    )
    fn = {"decide_approval": household.decide_approval}[tool]
    typed = {k: uuid.UUID(v) if k in ("approval_id", "user_id") else v for k, v in args.items()}
    try:
        return await fn(call, **typed)
    except GuardRefused as refused:
        raise ToolRefused(refused.reason) from None


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
            token=tokens.MockIssuer().issue(
                f"user:{w.ADMIN}", HOUSEHOLD_AGENT, ["approvals:request"]
            ),
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
    assert prompt["body"]["params"] == {"amount": "$76.00"}
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
