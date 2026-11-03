"""Identity texts in `api` (identity-authz §5, §12 and I26): the reply to a phone that is not
registered, the renew text and the approval prompt."""

import asyncio
from typing import Any

import psycopg
import pytest

from services.api.ingest import CONSENT_RENEWAL, Content, ingest
from services.api.whatsapp import sender as s
from services.common.config import Settings
from services.common.consumer import Inbound
from services.common.protocol.a2a import AgentMessageRow
from services.common.protocol.schemas import WhatsAppOut
from services.identity import links
from tests.unit import api_world as w

pytestmark = pytest.mark.db

CHANNEL = "whatsapp"
HOUSEHOLD_AGENT = f"household:{w.HOUSEHOLD_ID}"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    w.patch_settings(monkeypatch)
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


@pytest.fixture
def world(pg: str) -> str:
    w.seed(pg)
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO approval_rules (household_id, approver_user_id) VALUES (%s, %s)",
            (w.HOUSEHOLD_ID, w.ADMIN),
        )
    return pg


def _ingest(pg: str, external_id: str, phone: str, replies: list[str] | None = None) -> Any:
    async def reply(to: str) -> None:
        assert replies is not None, "no unknown_number reply expected"
        replies.append(to)

    return w.run(
        pg,
        lambda conn: ingest(
            conn,
            CHANNEL,
            external_id,
            phone,
            Content("text", text="Weekly shop"),
            reply_unknown=reply,
        ),
    )


def _revoke_household_grant(pg: str) -> None:
    w.query(
        pg,
        "UPDATE agent_grants SET status = 'revoked', revoke_reason = 'user'"
        " WHERE agent_id = %s RETURNING 1",
        HOUSEHOLD_AGENT,
    )


def _inbound(row: dict[str, Any]) -> Inbound:
    msg = AgentMessageRow(
        message_id=row["message_id"],
        run_id=row["run_id"],
        conversation_id=None,
        causation_id=None,
        from_agent=row["from_agent"],
        to_agent=row["to_agent"],
        type=row["type"],
        body=row["body"],
    )
    return Inbound(row=msg, attempts=1, body=WhatsAppOut.model_validate(row["body"]))


def _send(pg: str, row: dict[str, Any]) -> None:
    asyncio.run(s.Sender(w.connect_to(pg))(_inbound(row)))


def test_unknown_phone_reply(world: str) -> None:
    replies: list[str] = []
    assert _ingest(world, "wamid.u1", w.UNKNOWN_PHONE, replies) is None
    assert replies == [w.UNKNOWN_PHONE]
    # No run and no outbox row: the reply goes straight to the Graph API.
    assert w.query(world, "SELECT 1 FROM runs") == []
    assert w.query(world, "SELECT 1 FROM agent_messages") == []
    # The inbound row is kept, so a redelivery gets no second reply.
    assert _ingest(world, "wamid.u1", w.UNKNOWN_PHONE, replies) is None
    assert replies == [w.UNKNOWN_PHONE]
    # A known phone never gets it.
    _ingest(world, "wamid.k1", w.MEMBER_PHONE)
    payload = s.graph_payload(
        w.UNKNOWN_PHONE, {"template": "unknown_number", "params": {}, "buttons": []}
    )
    assert payload is not None
    assert payload["text"]["body"] == "This number isn't registered yet."


def test_renew_text_inside_window_only(world: str) -> None:
    _revoke_household_grant(world)

    # The admin writes, so they are inside the window: the renew text goes to them.
    run_id = _ingest(world, "wamid.r1", w.ADMIN_PHONE)
    [run] = w.query(world, "SELECT * FROM runs WHERE run_id = %s", run_id)
    assert (run["household_id"], run["status"], run["reason"]) == (
        None,
        "completed",
        "grant_revoked",
    )
    assert run["trigger"]["source"] == "code"
    [msg] = w.query(world, "SELECT * FROM agent_messages WHERE run_id = %s", run_id)
    assert (msg["type"], msg["from_agent"], msg["to_agent"]) == (
        "whatsapp_out",
        "system:api",
        w.WHATSAPP,
    )
    assert msg["body"]["user_id"] == str(w.ADMIN)
    assert msg["body"]["template"] == CONSENT_RENEWAL
    assert msg["body"]["link"] == {"household_id": w.HOUSEHOLD_ID, "agent_ids": [HOUSEHOLD_AGENT]}
    # The link token is never stored in the message (identity-authz I-G8).
    assert "/me/l/" not in str(msg["body"])
    # No household run started, and no user_message was sent.
    assert w.query(world, "SELECT 1 FROM runs WHERE household_id IS NOT NULL") == []

    # The sender issues a fresh consent link at send time.
    _send(world, msg)
    [out] = w.query(world, "SELECT body FROM sim_outbound WHERE run_id = %s", run_id)
    text = out["body"]["graph"]["text"]["body"]
    assert text.startswith("Your agent's permission has ended. Tap to renew: https://")
    assert "/me/l/" in text
    [link] = w.query(world, "SELECT * FROM consent_links")
    assert (link["purpose"], link["target_user_id"], link["sent_to_user_id"]) == (
        "consent",
        w.ADMIN,
        w.ADMIN,
    )
    assert (link["household_id"], link["agent_ids"]) == (w.HOUSEHOLD_ID, [HOUSEHOLD_AGENT])
    [send] = w.query(world, "SELECT outcome FROM whatsapp_sends")
    assert send["outcome"] == s.SENT

    # A member writes; the household's approver (the admin) was last seen 2 days ago, so is
    # outside the window: nothing is sent, and the run still ends with the reason.
    w.query(
        world,
        "UPDATE channel_identities SET last_inbound_at = now() - interval '2 days'"
        " WHERE phone_e164 = %s RETURNING 1",
        w.ADMIN_PHONE,
    )
    outside = _ingest(world, "wamid.r2", w.MEMBER_PHONE)
    [run] = w.query(world, "SELECT status, reason FROM runs WHERE run_id = %s", outside)
    assert (run["status"], run["reason"]) == ("completed", "grant_revoked")
    assert w.query(world, "SELECT 1 FROM agent_messages WHERE run_id = %s", outside) == []


def test_renew_text_with_link_skipped_by_sender_outside_window(world: str) -> None:
    # The window can close between ingest and the send: a link text is then skipped, never
    # held or sent as a template, and no link is issued.
    body = {
        "user_id": str(w.ADMIN),
        "template": CONSENT_RENEWAL,
        "params": {},
        "buttons": [],
        "link": {"household_id": w.HOUSEHOLD_ID, "agent_ids": [HOUSEHOLD_AGENT]},
    }
    message_id, run_id = w.sent_message(world, "system:api", w.WHATSAPP, "whatsapp_out", body)
    w.query(
        world,
        "UPDATE channel_identities SET last_inbound_at = NULL WHERE phone_e164 = %s RETURNING 1",
        w.ADMIN_PHONE,
    )
    [row] = w.query(world, "SELECT * FROM agent_messages WHERE message_id = %s", message_id)
    _send(world, row)
    [send] = w.query(world, "SELECT outcome, reason FROM whatsapp_sends")
    assert (send["outcome"], send["reason"]) == (s.SKIPPED, s.OUTSIDE_WINDOW)
    assert w.query(world, "SELECT 1 FROM consent_links") == []
    assert w.query(world, "SELECT 1 FROM held_messages") == []
    assert w.query(world, "SELECT 1 FROM sim_outbound WHERE run_id = %s", run_id) == []


def test_approval_prompt_has_amount_and_reply_by() -> None:
    # Identity-authz §12 amendment (experience EX-A15, EX-A8): the prompt is neutral, with
    # the amount and the reply-by time, whichever limit triggered it.
    body = {
        "template": "approval_request",
        "params": {"amount": "$120.00", "reply_by": "6:30 pm"},
        "buttons": [],
    }
    payload = s.graph_payload("+15550100201", body)
    assert payload is not None
    text = payload["text"]["body"]
    assert text == "Your agent wants to spend $120.00 and needs your approval. Reply by 6:30 pm."
    assert "limit" not in text and "budget" not in text
    assert s.graph_payload("+15550100201", {**body, "params": {"amount": "$120.00"}}) is None


def test_unknown_number_reply_failure_does_not_raise(caplog: pytest.LogCaptureFixture) -> None:
    # The default reply is best effort: with no WhatsApp credentials nothing is sent, and
    # nothing is raised into ingest, whose inbound row is already committed.
    from services.api import ingest as ingest_mod

    assert asyncio.run(ingest_mod.send_unknown_number(w.UNKNOWN_PHONE)) is None
    assert "unknown_number reply not sent" in caplog.text
