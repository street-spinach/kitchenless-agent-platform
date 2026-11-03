"""WhatsApp status callbacks (experience §5 "Status callbacks", EX-I16, XG4)."""

import hashlib
import hmac
import json
import uuid
from typing import Any

import pytest
from starlette.testclient import TestClient

from services.api import main as api
from services.api.routes import whatsapp
from services.api.whatsapp import sender as s
from services.api.whatsapp import status
from services.common.config import Settings
from tests.unit import api_world as w

SECRET = "test-app-secret"
PATH = "/webhooks/whatsapp"
APPROVAL_ID = str(uuid.uuid4())
APPROVAL = {
    "user_id": str(w.MEMBER),
    "template": "approval_request",
    "params": {"amount": "$76.00", "reply_by": "6:30 pm"},
    "buttons": [
        {"payload": f"ap:{APPROVAL_ID}:approved", "title": "Approve"},
        {"payload": f"ap:{APPROVAL_ID}:rejected", "title": "Reject"},
    ],
    "ref": {"kind": "approval", "id": APPROVAL_ID},
}
CHAT = {"user_id": str(w.MEMBER), "template": "chat_reply", "params": {"text": "hi"}, "buttons": []}


@pytest.fixture
def world(pg: str, monkeypatch: pytest.MonkeyPatch) -> str:
    w.patch_settings(monkeypatch)
    env = {
        "SERVICE_NAME": "migrate",
        "DATABASE_URL": "x",
        "LOG_LEVEL": "info",
        "OPENFGA_PRESHARED_KEY": "x",
        "OPENFGA_API_URL": "x",
        "WHATSAPP_APP_SECRET": SECRET,
    }
    settings = Settings.from_env(env)
    monkeypatch.setattr(whatsapp, "get_settings", lambda: settings)
    w.seed(pg)
    return pg


def _sent(world: str, body: dict[str, Any], graph_id: str) -> uuid.UUID:
    """A `whatsapp_out` the sender sent on the real path, with its Graph message id."""
    message_id, _ = w.sent_message(world, "system:runs", w.WHATSAPP, "whatsapp_out", body)
    w.query(
        world,
        "INSERT INTO whatsapp_sends (message_id, user_id, outcome, graph_message_id)"
        " VALUES (%s, %s, %s, %s) RETURNING 1",
        message_id,
        w.MEMBER,
        s.SENT,
        graph_id,
    )
    return message_id


def _post(world: str, statuses: list[Any]) -> None:
    value = {"messaging_product": "whatsapp", "statuses": statuses}
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "waba", "changes": [{"field": "messages", "value": value}]}],
    }
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    resp = TestClient(api.create_app(w.connect_to(world))).post(
        PATH, content=raw, headers={whatsapp.SIGNATURE_HEADER: sig}
    )
    assert resp.status_code == 200


def _record(world: str, statuses: list[dict[str, Any]]) -> None:
    async def go(conn: Any) -> None:
        await status.record(conn, statuses)

    w.run(world, go)


def _failed(graph_id: str, code: int) -> dict[str, Any]:
    return {
        "id": graph_id,
        "status": "failed",
        "timestamp": "1700000000",
        "recipient_id": "1",
        "errors": [{"code": code, "title": "Re-engagement message"}],
    }


@pytest.mark.db
def test_status_delivered_recorded(world: str) -> None:
    message_id = _sent(world, APPROVAL, "wamid.ok")
    _post(world, [{"id": "wamid.ok", "status": "delivered", "recipient_id": "1"}])
    [send] = w.query(world, "SELECT * FROM whatsapp_sends")
    assert (send["message_id"], send["status"]) == (message_id, "delivered")
    assert send["status_at"] is not None
    assert send["outcome"] == s.SENT  # the outcome is not changed
    _post(world, [{"id": "wamid.ok", "status": "read", "recipient_id": "1"}])
    [send] = w.query(world, "SELECT * FROM whatsapp_sends")
    assert send["status"] == "read"
    # An unknown status value is not recorded (validated in code).
    _post(world, [{"id": "wamid.ok", "status": "bogus", "recipient_id": "1"}])
    [send] = w.query(world, "SELECT * FROM whatsapp_sends")
    assert send["status"] == "read"
    assert w.query(world, "SELECT * FROM held_messages") == []


@pytest.mark.db
def test_status_131047_holds(world: str) -> None:
    held_id = _sent(world, APPROVAL, "wamid.late")
    chat_id = _sent(world, CHAT, "wamid.chat")
    other_id = _sent(world, APPROVAL | {"ref": {"kind": "approval", "id": "x"}}, "wamid.other")
    _post(world, [_failed("wamid.late", s.OUTSIDE_WINDOW_CODE)])
    _record(world, [_failed("wamid.late", s.OUTSIDE_WINDOW_CODE)])  # redelivery: one row
    _record(world, [_failed("wamid.chat", s.OUTSIDE_WINDOW_CODE)])  # model-written: never held
    _record(world, [_failed("wamid.other", 131026)])  # another error: not held

    [held] = w.query(world, "SELECT * FROM held_messages")
    assert (held["message_id"], held["user_id"]) == (held_id, w.MEMBER)
    assert (held["ref_kind"], held["ref_id"], held["status"]) == (
        "approval",
        APPROVAL_ID,
        "pending",
    )
    assert held["body"]["template"] == "approval_request"
    sends = {r["message_id"]: r for r in w.query(world, "SELECT * FROM whatsapp_sends")}
    assert {m: r["status"] for m, r in sends.items()} == {
        held_id: "failed",
        chat_id: "failed",
        other_id: "failed",
    }


@pytest.mark.db
def test_status_unknown_id_ignored(world: str) -> None:
    message_id = _sent(world, APPROVAL, "wamid.known")
    _post(
        world,
        [
            {"id": "wamid.unknown", "status": "delivered", "recipient_id": "1"},
            _failed("wamid.unknown2", s.OUTSIDE_WINDOW_CODE),
            {"status": "delivered"},
            "not a dict",
        ],
    )
    [send] = w.query(world, "SELECT * FROM whatsapp_sends")
    assert (send["message_id"], send["status"], send["status_at"]) == (message_id, None, None)
    assert w.query(world, "SELECT * FROM held_messages") == []
