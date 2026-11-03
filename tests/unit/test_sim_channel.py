"""The simulated WhatsApp channel: `/internal/sim/*` and the sender's `sim` branch
(architecture items 35, 39; issue #40)."""

import asyncio
import uuid
from typing import Any

import pytest

from services.api.whatsapp.sender import RealSendUnavailable, Sender
from services.common.consumer import Inbound
from services.common.protocol.a2a import AgentMessageRow
from services.common.protocol.schemas import WhatsAppOut
from tests.unit import api_world as w

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    w.patch_settings(monkeypatch)


@pytest.fixture
def world(pg: str) -> str:
    w.seed(pg)
    return pg


def _whatsapp_out(pg: str, user_id: uuid.UUID) -> Inbound:
    body = {"user_id": str(user_id), "template": "retry_prompt", "params": {}, "buttons": []}
    message_id, run_id = w.sent_message(pg, "system:runs", w.WHATSAPP, "whatsapp_out", body)
    row = AgentMessageRow(
        message_id=message_id,
        run_id=run_id,
        conversation_id=None,
        causation_id=None,
        from_agent="system:runs",
        to_agent=w.WHATSAPP,
        type="whatsapp_out",
        body=body,
    )
    return Inbound(row=row, attempts=1, body=WhatsAppOut.model_validate(body))


@pytest.mark.parametrize("phone", [w.REAL_PHONE, w.UNKNOWN_PHONE])
def test_sim_route_only_for_sim_phones(world: str, phone: str) -> None:
    resp = w.client(world).post(
        "/internal/sim/whatsapp", json={"phone": phone, "text": "hi", "external_id": "sim.1"}
    )
    assert resp.status_code == 403
    assert w.query(world, "SELECT * FROM inbound_messages") == []
    assert w.query(world, "SELECT * FROM runs") == []


def test_sim_outbound_never_calls_graph_api(world: str) -> None:
    graph_calls: list[tuple[str, dict[str, Any]]] = []

    async def graph(phone: str, body: dict[str, Any]) -> None:
        graph_calls.append((phone, body))

    sender = Sender(w.connect_to(world), send_real=graph)
    msg = _whatsapp_out(world, w.MEMBER)
    asyncio.run(sender(msg))
    asyncio.run(sender(msg))  # a replayed step inserts nothing again

    assert graph_calls == []
    [row] = w.query(world, "SELECT * FROM sim_outbound")
    assert (row["phone_e164"], row["run_id"]) == (w.MEMBER_PHONE, msg.row.run_id)
    assert row["body"]["message_id"] == str(msg.row.message_id)
    assert row["body"]["template"] == "retry_prompt"

    resp = w.client(world).get(f"/internal/sim/outbox?phone={w.MEMBER_PHONE}&after_id=0")
    assert [m["id"] for m in resp.json()["messages"]] == [row["id"]]
    resp = w.client(world).get(
        "/internal/sim/outbox", params={"phone": w.MEMBER_PHONE, "after_id": 0}
    )  # the same phone with its `+` encoded as `%2B`
    assert [m["id"] for m in resp.json()["messages"]] == [row["id"]]
    resp = w.client(world).get(f"/internal/sim/outbox?phone={w.MEMBER_PHONE}&after_id={row['id']}")
    assert resp.json()["messages"] == []


def test_real_phone_goes_to_real_sender_and_fails_closed_until_built(world: str) -> None:
    calls: list[str] = []

    async def graph(phone: str, body: dict[str, Any]) -> None:
        calls.append(phone)

    sent = _whatsapp_out(world, w.LIVE_ADMIN)
    asyncio.run(Sender(w.connect_to(world), send_real=graph)(sent))
    assert calls == [w.REAL_PHONE]
    with pytest.raises(RealSendUnavailable):
        asyncio.run(Sender(w.connect_to(world))(_whatsapp_out(world, w.LIVE_ADMIN)))
    assert w.query(world, "SELECT * FROM sim_outbound") == []
