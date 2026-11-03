"""Identity texts in `api` (identity-authz §5, §12 and I26): the reply to a phone that is not
registered, the renew text, the revoke notice and the approval prompt."""

import asyncio
import uuid
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from services.api.ingest import CONSENT_RENEWAL, Content, ingest
from services.api.whatsapp import sender as s
from services.common.config import Settings
from services.common.consumer import Inbound
from services.common.protocol.a2a import AgentMessageRow
from services.common.protocol.schemas import WhatsAppOut
from services.identity import grants, keycloak_admin, links
from tests.unit import api_world as w
from tests.unit import identity_world as iw

pytestmark = pytest.mark.db

CHANNEL = "whatsapp"
HOUSEHOLD_AGENT = f"household:{w.HOUSEHOLD_ID}"
STORE = "Fresh Market"
SELLER = "grocery:fresh-market"
RENEW = [{"payload": "me:renew", "title": "Renew"}]
RENEW_REPLY = [{"type": "reply", "reply": {"id": "me:renew", "title": "Renew"}}]


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


def _grant_id(pg: str, agent_id: str) -> Any:
    [row] = w.query(pg, "SELECT grant_id FROM agent_grants WHERE agent_id = %s", agent_id)
    return row["grant_id"]


def test_renew_sent_with_ref(world: str) -> None:
    # Experience EX-I19: the renew text carries the Renew button and `ref = {grant, id}`,
    # and is written inside or outside the window; the sender decides (§5).
    _revoke_household_grant(world)
    grant_id = _grant_id(world, HOUSEHOLD_AGENT)

    # The admin writes, so they are inside the window: the renew text is sent to them.
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
    assert msg["body"]["buttons"] == RENEW
    assert msg["body"]["ref"] == {"kind": "grant", "id": str(grant_id)}
    # No link is issued or stored: the Renew tap asks for one (experience §7).
    assert msg["body"].get("link") is None
    # No household run started, and no user_message was sent.
    assert w.query(world, "SELECT 1 FROM runs WHERE household_id IS NOT NULL") == []

    _send(world, msg)
    [out] = w.query(world, "SELECT body FROM sim_outbound WHERE run_id = %s", run_id)
    interactive = out["body"]["graph"]["interactive"]
    assert interactive["body"]["text"] == (
        "Your agents' access has ended. Tap Renew and I'll send you a sign-in link."
    )
    assert interactive["action"]["buttons"] == RENEW_REPLY
    assert w.query(world, "SELECT 1 FROM consent_links") == []
    [send] = w.query(world, "SELECT outcome FROM whatsapp_sends")
    assert send["outcome"] == s.SENT

    # A member writes; the household's approver (the admin) was last seen 2 days ago, so is
    # outside the window: the renew text is still written, and the sender holds it as
    # `grant` (no Meta template is configured here). The run still ends with the reason.
    w.query(
        world,
        "UPDATE channel_identities SET last_inbound_at = now() - interval '2 days'"
        " WHERE phone_e164 = %s RETURNING 1",
        w.ADMIN_PHONE,
    )
    outside = _ingest(world, "wamid.r2", w.MEMBER_PHONE)
    [run] = w.query(world, "SELECT status, reason FROM runs WHERE run_id = %s", outside)
    assert (run["status"], run["reason"]) == ("completed", "grant_revoked")
    [msg] = w.query(world, "SELECT * FROM agent_messages WHERE run_id = %s", outside)
    assert msg["body"]["user_id"] == str(w.ADMIN)
    assert msg["body"]["ref"] == {"kind": "grant", "id": str(grant_id)}
    _send(world, msg)
    [held] = w.query(world, "SELECT ref_kind, ref_id FROM held_messages")
    assert (held["ref_kind"], held["ref_id"]) == ("grant", str(grant_id))
    assert w.query(world, "SELECT 1 FROM sim_outbound WHERE run_id = %s", outside) == []


def test_renew_without_grant_has_no_ref(world: str) -> None:
    # `grant_missing`: no grant to name, so no `ref`; outside the window with no template
    # the sender skips it rather than holding it (fails closed).
    w.query(world, "DELETE FROM agent_grants WHERE agent_id = %s RETURNING 1", HOUSEHOLD_AGENT)
    run_id = _ingest(world, "wamid.m1", w.ADMIN_PHONE)
    [run] = w.query(world, "SELECT reason FROM runs WHERE run_id = %s", run_id)
    assert run["reason"] == "grant_missing"
    [msg] = w.query(world, "SELECT * FROM agent_messages WHERE run_id = %s", run_id)
    assert msg["body"]["template"] == CONSENT_RENEWAL
    assert msg["body"].get("ref") is None
    w.query(
        world,
        "UPDATE channel_identities SET last_inbound_at = NULL WHERE phone_e164 = %s RETURNING 1",
        w.ADMIN_PHONE,
    )
    _send(world, msg)
    [send] = w.query(world, "SELECT outcome, reason FROM whatsapp_sends")
    assert (send["outcome"], send["reason"]) == (s.SKIPPED, s.OUTSIDE_WINDOW)
    assert w.query(world, "SELECT 1 FROM held_messages") == []


def _placed_order(pg: str) -> None:
    run_id, address = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO organizations (org_id, name, region) VALUES ('org-fresh', %s, 'us')",
            (STORE,),
        )
        conn.execute(
            "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
            " VALUES (%s, 'grocery', 'org:org-fresh', 'us', 'agents')",
            (SELLER,),
        )
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
            "INSERT INTO orders (order_id, run_id, household_id, kind, seller_agent_id, state,"
            " total_minor, currency, address_ref) VALUES"
            " (%s, %s, %s, 'grocery', %s, 'placed', 4250, 'USD', %s)",
            (uuid.uuid4(), run_id, w.HOUSEHOLD_ID, SELLER, address),
        )


def _revoke(pg: str, grant_id: Any) -> bool:
    async def go(conn: psycopg.AsyncConnection) -> bool:
        assert await grants.revoke(conn, grant_id, w.ADMIN, "user")
        await conn.commit()
        done = await grants.after_revoke(conn, grant_id)
        await conn.commit()
        return done

    return w.run(pg, go)


def _notice(pg: str, grant_id: Any) -> dict[str, Any]:
    [msg] = w.query(
        pg,
        "SELECT * FROM agent_messages WHERE body->>'template' = 'consent_revoked'"
        " AND body->'ref'->>'id' = %s",
        str(grant_id),
    )
    return msg


async def _fail(keycloak_sub: str, client_id: str) -> None:
    raise keycloak_admin.KeycloakError("boom")


def test_revoke_notice_has_renew_button(world: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Identity-authz §11 step 3, experience §6 (EX-I19): `after_revoke` sends
    # `consent_revoked` with `agent`, `placed_line`, the Renew button and `ref`.
    revoked: list[tuple[str, str]] = []

    async def revoke_consent(keycloak_sub: str, client_id: str) -> None:
        revoked.append((keycloak_sub, client_id))

    monkeypatch.setattr(keycloak_admin, "revoke_consent", revoke_consent)
    _placed_order(world)
    grant_id = _grant_id(world, HOUSEHOLD_AGENT)

    assert _revoke(world, grant_id) is True
    assert len(revoked) == 1
    msg = _notice(world, grant_id)
    assert (msg["from_agent"], msg["to_agent"]) == ("system:api", w.WHATSAPP)
    body = msg["body"]
    assert body["user_id"] == str(w.ADMIN)
    assert body["params"] == {
        "agent": "The household agent",
        "placed_line": f" Your order from {STORE} was already placed and will still arrive.",
    }
    assert body["buttons"] == RENEW
    assert body["ref"] == {"kind": "grant", "id": str(grant_id)}
    [run] = w.query(world, "SELECT * FROM runs WHERE run_id = %s", msg["run_id"])
    assert (run["household_id"], run["kind"], run["status"]) == (None, "chat", "completed")
    assert run["trigger"]["source"] == "code"

    _send(world, msg)
    [out] = w.query(world, "SELECT body FROM sim_outbound WHERE run_id = %s", msg["run_id"])
    interactive = out["body"]["graph"]["interactive"]
    assert interactive["body"]["text"] == (
        "The household agent stopped. Orders not yet placed won't go ahead. Your order from "
        f"{STORE} was already placed and will still arrive. Tap Renew and I'll send you a "
        "sign-in link."
    )
    assert interactive["action"]["buttons"] == RENEW_REPLY

    # A consumer agent's grant: "Your agent", no placed line. A Keycloak failure is audited
    # and the notice is still sent.
    monkeypatch.setattr(keycloak_admin, "revoke_consent", _fail)
    with psycopg.connect(world) as conn:
        sub = str(uuid.uuid4())
        conn.execute("UPDATE users SET keycloak_sub = %s WHERE user_id = %s", (sub, w.MEMBER))
        member_grant = iw.add_grant(
            conn, w.MEMBER_CONSUMER, iw.Person(w.MEMBER, sub), w.HOUSEHOLD_ID
        )
    assert _revoke(world, member_grant) is False
    assert w.query(world, "SELECT 1 FROM audit_events WHERE kind = 'keycloak_revoke_failed'") == [
        {"?column?": 1}
    ]
    msg = _notice(world, member_grant)
    assert msg["body"]["user_id"] == str(w.MEMBER)
    assert msg["body"]["params"] == {"agent": "Your agent", "placed_line": ""}
    _send(world, msg)
    [out] = w.query(world, "SELECT body FROM sim_outbound WHERE run_id = %s", msg["run_id"])
    assert out["body"]["graph"]["interactive"]["body"]["text"] == (
        "Your agent stopped. Orders not yet placed won't go ahead. "
        "Tap Renew and I'll send you a sign-in link."
    )


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
