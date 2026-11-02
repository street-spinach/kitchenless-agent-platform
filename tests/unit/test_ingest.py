"""`services/api/ingest.py` (architecture item 33; issue #40)."""

import uuid

import pytest

from services.api.ingest import API_AGENT, MEDIA_NOT_SUPPORTED, Content, ingest
from services.common import ids, runs
from tests.unit import api_world as w

pytestmark = pytest.mark.db

CHANNEL = "whatsapp"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    w.patch_settings(monkeypatch)


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
