"""The WhatsApp window: sends inside it, holds and skips outside it (experience §5, EX-I12).

No test calls the real Graph API: real phones use a fake `send_real`."""

import asyncio
import uuid
from typing import Any

import pytest

from services.api.whatsapp import sender as s
from services.api.whatsapp import templates
from services.common.config import Settings
from services.common.consumer import Inbound
from services.common.protocol.a2a import AgentMessageRow
from services.common.protocol.schemas import WhatsAppOut
from tests.unit import api_world as w
from tests.unit.test_config import full_env

APPROVAL_ID = str(uuid.uuid4())
APPROVAL = {
    "template": "approval_request",
    "params": {"amount": "$76.00", "reply_by": "6:30 pm"},
    "buttons": [
        {"payload": f"ap:{APPROVAL_ID}:approved", "title": "Approve"},
        {"payload": f"ap:{APPROVAL_ID}:rejected", "title": "Reject"},
    ],
    "ref": {"kind": "approval", "id": APPROVAL_ID},
}
RETRY = {"template": "retry_prompt"}


@pytest.fixture
def world(pg: str, monkeypatch: pytest.MonkeyPatch) -> str:
    w.patch_settings(monkeypatch)
    w.seed(pg)
    return pg


def _out(world: str, body: dict[str, Any], user_id: uuid.UUID = w.MEMBER) -> Inbound:
    body = {"user_id": str(user_id), "params": {}, "buttons": [], **body}
    message_id, run_id = w.sent_message(world, "system:runs", w.WHATSAPP, "whatsapp_out", body)
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


def _send(world: str, msg: Inbound, send_real: s.SendReal = s.not_built) -> None:
    asyncio.run(s.Sender(w.connect_to(world), send_real=send_real)(msg))


def _last_inbound(world: str, phone: str, ago: str | None) -> None:
    w.query(
        world,
        "UPDATE channel_identities SET last_inbound_at = now() - %s::interval"
        " WHERE phone_e164 = %s RETURNING 1",
        ago,
        phone,
    )


def _sends(world: str) -> list[dict[str, Any]]:
    return w.query(world, "SELECT * FROM whatsapp_sends")


def _decisions(world: str, msg: Inbound) -> list[dict[str, Any]]:
    rows = w.query(
        world,
        "SELECT * FROM agent_traces WHERE run_id = %s AND kind = 'decision'",
        msg.row.run_id,
    )
    return [r["payload"] for r in rows]


@pytest.mark.db
def test_inside_window_sends(world: str) -> None:
    msg = _out(world, APPROVAL)
    _send(world, msg)
    [out] = w.query(world, "SELECT * FROM sim_outbound")
    assert out["body"]["message_id"] == str(msg.row.message_id)
    [send] = _sends(world)
    assert (send["message_id"], send["user_id"]) == (msg.row.message_id, w.MEMBER)
    assert (send["outcome"], send["reason"]) == (s.SENT, None)
    assert send["graph_message_id"] is None  # the sim path has no Graph id
    [decision] = _decisions(world, msg)
    assert (decision["node"], decision["choice"]) == (s.WINDOW_NODE, s.SENT)
    assert w.query(world, "SELECT * FROM held_messages") == []


@pytest.mark.db
@pytest.mark.parametrize("ago", ["30 hours", "23 hours 50 minutes", None])
def test_outside_window_holds(world: str, ago: str | None) -> None:
    # NULL `last_inbound_at` is outside; so is anything older than 23 h 45 min.
    _last_inbound(world, w.MEMBER_PHONE, ago)
    msg = _out(world, APPROVAL)
    _send(world, msg)
    assert w.query(world, "SELECT * FROM sim_outbound") == []
    [held] = w.query(world, "SELECT * FROM held_messages")
    assert (held["message_id"], held["user_id"]) == (msg.row.message_id, w.MEMBER)
    assert (held["ref_kind"], held["ref_id"], held["status"]) == (
        "approval",
        APPROVAL_ID,
        "pending",
    )
    assert held["body"] == msg.body.model_dump(mode="json")
    [send] = _sends(world)
    assert (send["outcome"], send["reason"]) == (s.HELD, None)
    [decision] = _decisions(world, msg)
    assert (decision["node"], decision["choice"]) == (s.WINDOW_NODE, s.HELD)


@pytest.mark.db
@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (RETRY, s.OUTSIDE_WINDOW),
        # A holdable key with a `ref.kind` that may not hold it: not held (fails closed).
        ({**APPROVAL, "ref": {"kind": "order", "id": APPROVAL_ID}}, s.OUTSIDE_WINDOW),
        ({**APPROVAL, "ref": None}, s.OUTSIDE_WINDOW),
        (
            {
                "template": "reminder_delivery_today",
                "params": {"store": "Fresh", "window": "5-7 pm"},
                "ref": {"kind": "reminder", "id": "1"},
            },
            s.NO_TEMPLATE,
        ),
    ],
)
def test_outside_window_skips_unholdable(world: str, body: dict[str, Any], reason: str) -> None:
    _last_inbound(world, w.MEMBER_PHONE, "30 hours")
    msg = _out(world, body)
    _send(world, msg)
    assert w.query(world, "SELECT * FROM sim_outbound") == []
    assert w.query(world, "SELECT * FROM held_messages") == []
    [send] = _sends(world)
    assert (send["outcome"], send["reason"]) == (s.SKIPPED, reason)
    [decision] = _decisions(world, msg)
    assert (decision["choice"], decision["reason"]) == (s.SKIPPED, reason)


@pytest.mark.db
@pytest.mark.parametrize("key", sorted(templates.MODEL_KEYS))
def test_model_text_never_templated(world: str, key: str) -> None:
    # Every template setting configured: model-written keys still have no Meta template,
    # and are never held, whatever `ref` they carry.
    env = {
        **full_env("api"),
        **{t.setting.upper(): t.name for t in templates.TEMPLATES.values()},
    }
    settings = Settings.from_env(env)
    assert all(templates.meta_name(k, settings) for k in templates.TEMPLATES)
    assert templates.meta_name(key, settings) is None
    assert not any(templates.holdable(key, kind) for kind in ("approval", "order", "grant"))

    _last_inbound(world, w.MEMBER_PHONE, "30 hours")
    msg = _out(
        world,
        {"template": key, "params": {"text": "Try soup."}, "ref": {"kind": "order", "id": "o"}},
    )
    _send(world, msg)
    assert w.query(world, "SELECT * FROM held_messages") == []
    [send] = _sends(world)
    assert (send["outcome"], send["reason"]) == (s.SKIPPED, s.OUTSIDE_WINDOW)


def test_meta_name_empty_setting_is_none() -> None:
    settings = Settings.from_env(full_env("api"))
    assert [templates.meta_name(k, settings) for k in templates.TEMPLATES] == [None] * len(
        templates.TEMPLATES
    )
    assert templates.meta_name("retry_prompt", settings) is None


@pytest.mark.db
def test_sim_applies_window(world: str) -> None:
    # The sim sender applies the real sender's window check (EX-A4): nothing reaches the
    # sim phone outside it; just inside the margin, it sends.
    _last_inbound(world, w.MEMBER_PHONE, "23 hours 50 minutes")
    _send(world, _out(world, RETRY))
    assert w.query(world, "SELECT * FROM sim_outbound") == []

    _last_inbound(world, w.MEMBER_PHONE, "23 hours 40 minutes")
    msg = _out(world, RETRY)
    _send(world, msg)
    [out] = w.query(world, "SELECT * FROM sim_outbound")
    assert out["body"]["message_id"] == str(msg.row.message_id)
    assert sorted(r["outcome"] for r in _sends(world)) == [s.SENT, s.SKIPPED]


@pytest.mark.db
def test_hold_idempotent_on_replay(world: str) -> None:
    _last_inbound(world, w.MEMBER_PHONE, "30 hours")
    msg = _out(world, APPROVAL)
    _send(world, msg)
    _send(world, msg)  # a replayed step
    assert len(w.query(world, "SELECT * FROM held_messages")) == 1
    assert len(_sends(world)) == 1
    assert len(_decisions(world, msg)) == 1


@pytest.mark.db
def test_graph_message_id_stored(world: str) -> None:
    calls: list[str] = []

    async def graph(phone: str, body: dict[str, Any]) -> str | None:
        calls.append(phone)
        return "wamid.HBgL"

    msg = _out(world, RETRY, w.LIVE_ADMIN)
    _send(world, msg, graph)
    _send(world, msg, graph)  # a replayed step sends nothing again
    assert calls == [w.REAL_PHONE]
    [send] = _sends(world)
    assert (send["outcome"], send["graph_message_id"]) == (s.SENT, "wamid.HBgL")


@pytest.mark.db
def test_real_131047_continues_outside(world: str) -> None:
    # §5 step 7: Graph says the person is outside after all; the free-form send is not
    # retried, and the message is held (no template until EX-I13).
    calls: list[str] = []

    async def graph(phone: str, body: dict[str, Any]) -> str | None:
        calls.append(phone)
        raise s.GraphRefused(s.OUTSIDE_WINDOW_CODE)

    msg = _out(world, APPROVAL, w.LIVE_ADMIN)
    _send(world, msg, graph)
    assert calls == [w.REAL_PHONE]
    [held] = w.query(world, "SELECT * FROM held_messages")
    assert held["user_id"] == w.LIVE_ADMIN
    [send] = _sends(world)
    assert (send["outcome"], send["graph_message_id"]) == (s.HELD, None)


@pytest.mark.db
def test_real_outside_window_never_calls_graph(world: str) -> None:
    _last_inbound(world, w.REAL_PHONE, "30 hours")
    calls: list[str] = []

    async def graph(phone: str, body: dict[str, Any]) -> str | None:
        calls.append(phone)
        return "wamid.X"

    _send(world, _out(world, RETRY, w.LIVE_ADMIN), graph)
    assert calls == []
    [send] = _sends(world)
    assert (send["outcome"], send["reason"]) == (s.SKIPPED, s.OUTSIDE_WINDOW)
