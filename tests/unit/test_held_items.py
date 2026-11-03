"""Held items released when the person next writes (experience §5 "Release", EX-I20, XG3).

No test calls the real Graph API: the member's phone is a sim phone."""

import asyncio
import uuid
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from services.api.ingest import (
    API_AGENT,
    HELD_CANCELLED_LINE,
    HELD_NODE,
    HELD_RELEASE_CAP,
    Content,
    ingest,
    release_held,
)
from services.api.whatsapp import sender as s
from services.common import ids
from services.common.consumer import Inbound
from services.common.protocol.a2a import AgentMessageRow
from services.common.protocol.schemas import WhatsAppOut
from tests.unit import api_world as w

pytestmark = pytest.mark.db

CHANNEL = "whatsapp"


@pytest.fixture
def world(pg: str, monkeypatch: pytest.MonkeyPatch) -> str:
    w.patch_settings(monkeypatch)
    w.seed(pg)
    return pg


def _order(pg: str, age: str = "1 hour") -> tuple[uuid.UUID, uuid.UUID]:
    """An order created `age` ago, in its own weekly run. Returns `(order_id, run_id)`."""
    order_id, run_id, address = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, household_id, kind, trigger, modes)"
            " VALUES (%s, %s, 'weekly', %s, %s)",
            (run_id, w.HOUSEHOLD_ID, Jsonb({"source": "test"}), Jsonb({})),
        )
        conn.execute(
            "INSERT INTO household_addresses"
            " (address_ref, household_id, line1, city, region, postal_code, country)"
            " VALUES (%s, %s, '1 Main St', 'Cincinnati', 'OH', '45202', 'US')",
            (address, w.HOUSEHOLD_ID),
        )
        conn.execute(
            "INSERT INTO orders (order_id, run_id, household_id, kind, state, currency,"
            " address_ref, created_at) VALUES"
            " (%s, %s, %s, 'grocery', 'awaiting_approval', 'USD', %s, now() - %s::interval)",
            (order_id, run_id, w.HOUSEHOLD_ID, address, age),
        )
    return order_id, run_id


def _approval(pg: str, due_in: str, status: str = "pending") -> uuid.UUID:
    """An approval in `status` whose pending `approval_expiry` timer is due in `due_in`."""
    order_id, run_id = _order(pg)
    approval_id = uuid.uuid4()
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO approvals (approval_id, run_id, household_id, order_id, amount_minor,"
            " currency, status) VALUES (%s, %s, %s, %s, 7600, 'USD', %s)",
            (approval_id, run_id, w.HOUSEHOLD_ID, order_id, status),
        )
        conn.execute(
            "INSERT INTO timers (kind, run_id, due_at, ref)"
            " VALUES ('approval_expiry', %s, now() + %s::interval, %s)",
            (run_id, due_in, Jsonb({"approval_id": str(approval_id), "order_id": str(order_id)})),
        )
    return approval_id


def _approval_body(approval_id: uuid.UUID, amount: str = "$76.00") -> dict[str, Any]:
    return {
        "template": "approval_request",
        "params": {"amount": amount, "reply_by": "6:30 pm"},
        "buttons": [
            {"payload": f"ap:{approval_id}:approved", "title": "Approve"},
            {"payload": f"ap:{approval_id}:rejected", "title": "Reject"},
        ],
        "ref": {"kind": "approval", "id": str(approval_id)},
    }


def _order_body(order_id: uuid.UUID, text: str = "Your order was delivered.") -> dict[str, Any]:
    return {
        "template": "order_summary",
        "params": {"text": text},
        "ref": {"kind": "order", "id": str(order_id)},
    }


def _full(body: dict[str, Any]) -> dict[str, Any]:
    return {"user_id": str(w.MEMBER), "params": {}, "buttons": [], **body}


def _hold(pg: str, body: dict[str, Any], ago: str = "1 hour") -> int:
    """A held message for the member, created `ago`. Returns its `held_id`."""
    body = _full(body)
    message_id, _ = w.sent_message(pg, "system:runs", w.WHATSAPP, "whatsapp_out", body)
    [row] = w.query(
        pg,
        "INSERT INTO held_messages (message_id, user_id, body, ref_kind, ref_id, created_at)"
        " VALUES (%s, %s, %s, %s, %s, now() - %s::interval) RETURNING held_id",
        message_id,
        w.MEMBER,
        Jsonb(body),
        body["ref"]["kind"],
        body["ref"]["id"],
        ago,
    )
    return int(row["held_id"])


def _write(pg: str, external_id: str, content: Content | None = None) -> uuid.UUID | None:
    content = content or Content("text", text="hi")
    return w.run(pg, lambda conn: ingest(conn, CHANNEL, external_id, w.MEMBER_PHONE, content))


def _replies(pg: str, run_id: uuid.UUID) -> list[dict[str, Any]]:
    """The `whatsapp_out` messages `api` wrote on `run_id`, in insert order."""
    return w.query(
        pg,
        "SELECT * FROM agent_messages WHERE run_id = %s AND from_agent = %s"
        " AND type = 'whatsapp_out' ORDER BY created_at, message_id",
        run_id,
        API_AGENT,
    )


def _status(pg: str, held_id: int) -> str:
    [row] = w.query(pg, "SELECT status FROM held_messages WHERE held_id = %s", held_id)
    return str(row["status"])


def _decisions(pg: str) -> list[dict[str, Any]]:
    rows = w.query(pg, "SELECT payload FROM agent_traces WHERE kind = 'decision' ORDER BY ts, seq")
    return [r["payload"] for r in rows if r["payload"].get("node") == HELD_NODE]


def _templates(pg: str, run_id: uuid.UUID) -> list[str]:
    return [m["body"]["template"] for m in _replies(pg, run_id)]


@pytest.mark.parametrize(
    ("status", "due_in"),
    [("expired", "-1 minute"), ("pending", "-1 minute"), ("pending", "4 minutes")],
)
def test_expired_approval_dropped(world: str, status: str, due_in: str) -> None:
    """XG3: an approval past expiry (or due within 5 minutes) is dropped, never sent; the
    person gets `held_cancelled_line` instead."""
    held = _hold(world, _approval_body(_approval(world, due_in, status)))
    run_id = _write(world, "wamid.1")
    assert run_id is not None
    assert _status(world, held) == "dropped"
    [line] = _replies(world, run_id)
    assert line["body"]["template"] == HELD_CANCELLED_LINE
    assert line["body"]["params"] == {"amount": "$76.00"}
    assert line["body"]["user_id"] == str(w.MEMBER)
    assert line["message_id"] == ids.held_cancelled_id(ids.inbound_id(CHANNEL, "wamid.1"))
    assert not [
        m
        for m in w.query(world, "SELECT * FROM agent_messages WHERE run_id = %s", run_id)
        if m["body"].get("template") == "approval_request"
    ]
    [decision] = _decisions(world)
    assert (decision["choice"], decision["reason"]) == ("dropped", "stale")


def test_fresh_approval_released(world: str) -> None:
    """EX-CUJ 4 variant: still pending with more than 5 minutes left, so it is sent."""
    approval_id = _approval(world, "2 hours")
    held = _hold(world, _approval_body(approval_id))
    run_id = _write(world, "wamid.1")
    assert run_id is not None
    [msg] = _replies(world, run_id)
    assert msg["message_id"] == ids.held_release_id(held)
    assert msg["body"] == _full(_approval_body(approval_id))
    assert _status(world, held) == "released"


def test_decided_approval_dropped_without_line(world: str) -> None:
    """An approval the person already decided is dropped, and no cancelled line is sent."""
    held = _hold(world, _approval_body(_approval(world, "2 hours", "approved")))
    run_id = _write(world, "wamid.1")
    assert run_id is not None
    assert _status(world, held) == "dropped"
    assert _replies(world, run_id) == []


def test_stale_order_dropped(world: str) -> None:
    order_id, _ = _order(world, age="73 hours")
    held = _hold(world, _order_body(order_id))
    run_id = _write(world, "wamid.1")
    assert run_id is not None
    assert _status(world, held) == "dropped"
    assert _replies(world, run_id) == []


def test_release_cap(world: str) -> None:
    """XG3: at most 3 items per message; the rest wait for the next one. The cancelled line
    does not count toward the 3."""
    order_id, _ = _order(world)
    held = [_hold(world, _order_body(order_id, f"n{n}"), f"{10 - n} minutes") for n in range(5)]
    _hold(world, _approval_body(_approval(world, "-1 minute")), "20 minutes")
    first = _write(world, "wamid.1")
    assert first is not None
    assert _templates(world, first).count("order_summary") == HELD_RELEASE_CAP
    assert _templates(world, first).count(HELD_CANCELLED_LINE) == 1
    assert [_status(world, h) for h in held] == ["released"] * 3 + ["pending"] * 2
    second = _write(world, "wamid.2")
    assert second is not None
    assert _templates(world, second) == ["order_summary", "order_summary"]
    assert [_status(world, h) for h in held] == ["released"] * 5


def test_release_oldest_first(world: str) -> None:
    order_id, _ = _order(world)
    newest = _hold(world, _order_body(order_id, "newest"), "1 minute")
    oldest = _hold(world, _order_body(order_id, "oldest"), "3 hours")
    middle = _hold(world, _order_body(order_id, "middle"), "1 hour")
    second = _hold(world, _order_body(order_id, "second"), "2 hours")
    run_id = _write(world, "wamid.1")
    assert run_id is not None
    sent = [m["message_id"] for m in _replies(world, run_id)]
    assert sorted(sent) == sorted(ids.held_release_id(h) for h in (oldest, second, middle))
    assert _status(world, newest) == "pending"
    assert [d["choice"] for d in _decisions(world)] == ["released"] * 3


def test_cancelled_line_once(world: str) -> None:
    """Two dropped approvals: one `held_cancelled_line`, with the oldest one's amount, and
    none on the next message."""
    _hold(world, _approval_body(_approval(world, "-1 minute"), "$12.00"), "2 hours")
    _hold(world, _approval_body(_approval(world, "-1 minute", "expired"), "$76.00"), "1 hour")
    first = _write(world, "wamid.1")
    assert first is not None
    [line] = _replies(world, first)
    assert line["body"]["template"] == HELD_CANCELLED_LINE
    assert line["body"]["params"] == {"amount": "$12.00"}
    second = _write(world, "wamid.2")
    assert second is not None
    assert _replies(world, second) == []
    assert [d["choice"] for d in _decisions(world)] == ["dropped", "dropped"]


def test_release_idempotent_on_replay(world: str) -> None:
    """A redelivered message, or `release_held` run again for the same message, sends
    nothing more and writes no more decisions."""
    order_id, _ = _order(world)
    held = _hold(world, _order_body(order_id))
    _hold(world, _approval_body(_approval(world, "-1 minute")))
    run_id = _write(world, "wamid.1")
    assert run_id is not None
    before = _replies(world, run_id)
    assert len(before) == 2
    assert _write(world, "wamid.1") is None  # a duplicate delivery

    async def again(conn: psycopg.AsyncConnection) -> None:
        async with conn.transaction():
            await release_held(conn, w.MEMBER, run_id, ids.inbound_id(CHANNEL, "wamid.1"))

    w.run(world, again)
    assert _replies(world, run_id) == before
    assert len(_decisions(world)) == 2
    assert _status(world, held) == "released"


def test_release_writes_one_send_row(world: str) -> None:
    """Held by the sender outside the window, released when the member writes, then sent:
    the held message keeps its one `held` row and the released one gets one `sent` row, also
    when the send step is replayed (XG1 counts it once)."""
    approval_id = _approval(world, "2 hours")
    w.query(
        world,
        "UPDATE channel_identities SET last_inbound_at = now() - interval '30 hours'"
        " WHERE phone_e164 = %s RETURNING 1",
        w.MEMBER_PHONE,
    )
    body = _full(_approval_body(approval_id))
    message_id, run_id = w.sent_message(world, "system:runs", w.WHATSAPP, "whatsapp_out", body)
    sender = s.Sender(w.connect_to(world))
    asyncio.run(sender(_inbound(message_id, run_id, "system:runs", body)))
    [held] = w.query(world, "SELECT * FROM held_messages")
    assert held["status"] == "pending"

    released_run = _write(world, "wamid.1")
    assert released_run is not None
    [msg] = _replies(world, released_run)
    assert msg["message_id"] == ids.held_release_id(held["held_id"])
    out = _inbound(msg["message_id"], released_run, API_AGENT, msg["body"])
    asyncio.run(sender(out))
    asyncio.run(sender(out))  # a replayed step

    sends = w.query(world, "SELECT message_id, outcome FROM whatsapp_sends ORDER BY created_at")
    assert [(r["message_id"], r["outcome"]) for r in sends] == [
        (message_id, s.HELD),
        (msg["message_id"], s.SENT),
    ]
    [sim] = w.query(world, "SELECT body FROM sim_outbound")
    assert sim["body"]["message_id"] == str(msg["message_id"])


def test_release_on_media_path(world: str) -> None:
    """Every known-person path releases, before its own reply."""
    order_id, _ = _order(world)
    held = _hold(world, _order_body(order_id))
    run_id = _write(world, "wamid.1", Content("media"))
    assert run_id is not None
    assert _templates(world, run_id) == ["order_summary", "media_not_supported"]
    assert _status(world, held) == "released"


def _inbound(
    message_id: uuid.UUID, run_id: uuid.UUID, from_agent: str, body: dict[str, Any]
) -> Inbound:
    row = AgentMessageRow(
        message_id=message_id,
        run_id=run_id,
        conversation_id=None,
        causation_id=None,
        from_agent=from_agent,
        to_agent=w.WHATSAPP,
        type="whatsapp_out",
        body=body,
    )
    return Inbound(row=row, attempts=1, body=WhatsAppOut.model_validate(body))
