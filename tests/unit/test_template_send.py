"""Meta templates outside the WhatsApp window (experience §5 step 4, step 7, EX-I13).

No test calls the real Graph API: real phones use a fake `send_real` or a mock transport."""

import asyncio
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb

from services.api.whatsapp import sender as s
from services.api.whatsapp import templates
from services.common.config import Settings
from services.common.consumer import Inbound
from services.common.protocol.a2a import AgentMessageRow
from services.common.protocol.schemas import AllergenCode, DietCode, WhatsAppOut
from services.mcp.household.server import format_reply_by
from tests.unit import api_world as w
from tests.unit.test_config import full_env

TZ = ZoneInfo("America/New_York")
ORG_ID = "fresh-market"
STORE = "Fresh Market"
SELLER = "grocery:fresh-market"
APPROVAL_ID = uuid.uuid4()
ORDER_ID = uuid.uuid4()
NAMES = ("Kavi", "Sam", "Priya")


def _configured() -> Settings:
    env = {**full_env("api"), **{t.setting.upper(): t.name for t in templates.TEMPLATES.values()}}
    return Settings.from_env(env)


def _seed_order_world(pg: str) -> dict[str, int]:
    """A store, a delivered order of 2 lines, a pending approval with its timer, the
    member's two reminders, and sensitive data: allergy and diet codes and member names."""
    run_id = uuid.uuid4()
    address = uuid.uuid4()
    with psycopg.connect(pg) as conn:
        for user_id, name in zip((w.MEMBER, w.ADMIN, w.LIVE_ADMIN), NAMES, strict=True):
            conn.execute("UPDATE users SET display_name = %s WHERE user_id = %s", (name, user_id))
            for code in (AllergenCode.PEANUTS, AllergenCode.SESAME):
                conn.execute(
                    "INSERT INTO member_constraints (user_id, kind, value)"
                    " VALUES (%s, 'allergy', %s)",
                    (user_id, code.value),
                )
            conn.execute(
                "INSERT INTO member_constraints (user_id, kind, value) VALUES (%s, 'diet', %s)",
                (user_id, DietCode.HALAL.value),
            )
        conn.execute(
            "INSERT INTO organizations (org_id, name, region) VALUES (%s, %s, 'us')",
            (ORG_ID, STORE),
        )
        conn.execute(
            "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
            " VALUES (%s, 'grocery', %s, 'us', 'agents')",
            (SELLER, f"org:{ORG_ID}"),
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
            " total_minor, currency, address_ref, delivery_window) VALUES"
            " (%s, %s, %s, 'grocery', %s, 'delivered', 4250, 'USD', %s,"
            "  tstzrange((current_date + time '17:00') AT TIME ZONE 'America/New_York',"
            "            (current_date + time '19:00') AT TIME ZONE 'America/New_York'))",
            (ORDER_ID, run_id, w.HOUSEHOLD_ID, SELLER, address),
        )
        for line, (sku, name) in enumerate((("milk-1l", "Milk"), ("eggs-12", "Eggs")), 1):
            conn.execute(
                "INSERT INTO order_items (order_id, line, sku, name, qty, unit_minor)"
                " VALUES (%s, %s, %s, %s, 1, 2125)",
                (ORDER_ID, line, sku, name),
            )
        conn.execute(
            "INSERT INTO approvals (approval_id, run_id, household_id, order_id, amount_minor,"
            " currency) VALUES (%s, %s, %s, %s, 7600, 'USD')",
            (APPROVAL_ID, run_id, w.HOUSEHOLD_ID, ORDER_ID),
        )
        conn.execute(
            "INSERT INTO timers (kind, run_id, due_at, ref)"
            " VALUES ('approval_expiry', %s, now() + interval '2 hours', %s)",
            (run_id, Jsonb({"approval_id": str(APPROVAL_ID), "order_id": str(ORDER_ID)})),
        )
        reminders = {}
        for kind, ref_id in (("delivery_today", str(ORDER_ID)), ("pantry_summary", "us-2:W45")):
            row = conn.execute(
                "INSERT INTO reminders (household_id, user_id, kind, ref_id, status)"
                " VALUES (%s, %s, %s, %s, 'sent') RETURNING reminder_id",
                (w.HOUSEHOLD_ID, w.MEMBER, kind, ref_id),
            ).fetchone()
            assert row is not None
            reminders[kind] = row[0]
    return reminders


@pytest.fixture
def world(pg: str, monkeypatch: pytest.MonkeyPatch) -> str:
    w.patch_settings(monkeypatch)
    w.seed(pg)
    return pg


@pytest.fixture
def reminders(world: str) -> dict[str, int]:
    return _seed_order_world(world)


def _approval(approval_id: uuid.UUID = APPROVAL_ID) -> dict[str, Any]:
    return {
        "template": "approval_request",
        "params": {"amount": "$76.00", "reply_by": "6:30 pm"},
        "buttons": [
            {"payload": f"ap:{approval_id}:approved", "title": "Approve"},
            {"payload": f"ap:{approval_id}:rejected", "title": "Reject"},
        ],
        "ref": {"kind": "approval", "id": str(approval_id)},
    }


def _bodies(reminders: dict[str, int]) -> dict[str, dict[str, Any]]:
    """One `whatsapp_out` body per template key. In-window params carry names and codes
    where the in-window text may; the template parameters must not."""
    tt = f"tt:{ORDER_ID}:on"
    return {
        "approval_request": _approval(),
        "take_turns_offer": {
            "template": "take_turns_offer",
            "params": {"values": "peanuts, Kavi's sesame"},
            "buttons": [{"payload": tt, "title": "Take turns"}],
            "ref": {"kind": "order", "id": str(ORDER_ID)},
        },
        "order_summary": {
            "template": "order_summary",
            "params": {"text": "Kavi: your order was delivered (no peanuts, halal)."},
            "ref": {"kind": "order", "id": str(ORDER_ID)},
        },
        "consent_renewal": {
            "template": "consent_renewal",
            "buttons": [{"payload": "me:renew", "title": "Renew"}],
            "ref": {"kind": "grant", "id": "g1"},
        },
        "consent_revoked": {
            "template": "consent_revoked",
            "params": {"agent": "The household agent", "placed_line": ""},
            "buttons": [{"payload": "me:renew", "title": "Renew"}],
            "ref": {"kind": "grant", "id": "g1"},
        },
        "reminder_delivery_today": {
            "template": "reminder_delivery_today",
            "params": {"store": STORE, "window": "5-7 pm"},
            "buttons": [{"payload": "rs:stop", "title": "Stop these"}],
            "ref": {"kind": "reminder", "id": str(reminders["delivery_today"])},
        },
        "reminder_pantry_summary": {
            "template": "reminder_pantry_summary",
            "params": {
                "at_home": "milk, eggs",
                "unsure": "sesame oil",
                "at_home_count": "5",
                "unsure_count": "2",
            },
            "buttons": [
                {"payload": "me:pantry", "title": "Open pantry"},
                {"payload": "rs:stop", "title": "Stop these"},
            ],
            "ref": {"kind": "reminder", "id": str(reminders["pantry_summary"])},
        },
    }


def _full(body: dict[str, Any], user_id: uuid.UUID = w.MEMBER) -> dict[str, Any]:
    return {"user_id": str(user_id), "params": {}, "buttons": [], **body}


def _out(world: str, body: dict[str, Any], user_id: uuid.UUID = w.MEMBER) -> Inbound:
    body = _full(body, user_id)
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


def _send(
    world: str,
    msg: Inbound,
    send_real: s.SendReal = s.not_built,
    settings: Settings | None = None,
) -> None:
    sender = s.Sender(w.connect_to(world), send_real=send_real, settings=settings)
    asyncio.run(sender(msg))


def _outside(world: str, phone: str) -> None:
    w.query(
        world,
        "UPDATE channel_identities SET last_inbound_at = now() - interval '30 hours'"
        " WHERE phone_e164 = %s RETURNING 1",
        phone,
    )


def _params(world: str, key: str, body: dict[str, Any]) -> list[str] | None:
    return w.run(world, lambda conn: templates.params(conn, key, _full(body)))


class Graph:
    """A mock Graph API that answers each request with the next of `responses`."""

    def __init__(self, *responses: tuple[int, dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.sleeps: list[float] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        status, data = self.responses.pop(0)
        return httpx.Response(status, json=data)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def sender(self) -> s.GraphSender:
        return s.GraphSender(
            "token", "123", transport=httpx.MockTransport(self.handle), sleep=self.sleep
        )


@pytest.mark.db
def test_template_body_shape(world: str, reminders: dict[str, int]) -> None:
    _outside(world, w.REAL_PHONE)
    graph = Graph((200, {"messages": [{"id": "wamid.TPL"}]}))
    msg = _out(world, _approval(), w.LIVE_ADMIN)
    _send(world, msg, graph.sender(), _configured())
    [request] = graph.requests
    [due] = w.query(world, "SELECT due_at FROM timers WHERE kind = 'approval_expiry'")
    reply_by = format_reply_by(due["due_at"], datetime.now(UTC), TZ)
    assert request == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": w.REAL_PHONE.removeprefix("+"),
        "type": "template",
        "template": {
            "name": "approval_needed_v1",
            "language": {"code": "en_US"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": "$76.00"},
                        {"type": "text", "text": reply_by},
                    ],
                },
                {
                    "type": "button",
                    "sub_type": "quick_reply",
                    "index": "0",
                    "parameters": [{"type": "payload", "payload": f"ap:{APPROVAL_ID}:approved"}],
                },
                {
                    "type": "button",
                    "sub_type": "quick_reply",
                    "index": "1",
                    "parameters": [{"type": "payload", "payload": f"ap:{APPROVAL_ID}:rejected"}],
                },
            ],
        },
    }
    [send] = w.query(world, "SELECT * FROM whatsapp_sends")
    assert (send["outcome"], send["reason"], send["graph_message_id"]) == (
        s.TEMPLATE,
        None,
        "wamid.TPL",
    )
    [decision] = w.query(
        world,
        "SELECT payload FROM agent_traces WHERE run_id = %s AND kind = 'decision'",
        msg.row.run_id,
    )
    assert (decision["payload"]["node"], decision["payload"]["choice"]) == (
        s.WINDOW_NODE,
        s.TEMPLATE,
    )
    assert w.query(world, "SELECT * FROM held_messages") == []


@pytest.mark.db
def test_template_on_sim_phone(world: str, reminders: dict[str, int]) -> None:
    # The sim path applies the same step 4: the template request lands under `graph`.
    _outside(world, w.MEMBER_PHONE)
    body = _bodies(reminders)["order_summary"]
    msg = _out(world, body)
    _send(world, msg, settings=_configured())
    [out] = w.query(world, "SELECT body FROM sim_outbound")
    assert out["body"]["message_id"] == str(msg.row.message_id)
    assert out["body"]["template"] == "order_summary"
    assert out["body"]["graph"] == s.template_payload(
        w.MEMBER_PHONE, templates.request("order_delivered_v1", [STORE, "2", "$42.50"], [])
    )
    [send] = w.query(world, "SELECT * FROM whatsapp_sends")
    assert send["outcome"] == s.TEMPLATE


@pytest.mark.db
def test_params_in_order(world: str, reminders: dict[str, int]) -> None:
    [due] = w.query(world, "SELECT due_at FROM timers WHERE kind = 'approval_expiry'")
    expected = {
        "approval_request": ["$76.00", format_reply_by(due["due_at"], datetime.now(UTC), TZ)],
        "take_turns_offer": [],
        "order_summary": [STORE, "2", "$42.50"],
        "consent_renewal": [],
        "consent_revoked": ["The household agent"],
        "reminder_delivery_today": [STORE, "5:00 pm-7:00 pm"],
        "reminder_pantry_summary": ["5", "2"],
    }
    bodies = _bodies(reminders)
    assert set(expected) == set(templates.TEMPLATES) == set(bodies)
    for key, values in expected.items():
        assert _params(world, key, bodies[key]) == values, key
        # One value per `{{n}}`, numbered 1..n in the submitted body.
        slots = [int(n) for n in re.findall(r"\{\{(\d+)\}\}", templates.TEMPLATES[key].body)]
        assert slots == list(range(1, len(values) + 1)), key
    rendered = templates.TEMPLATES["order_summary"].body
    for n, value in enumerate(expected["order_summary"], 1):
        rendered = rendered.replace(f"{{{{{n}}}}}", value)
    assert rendered == "Your order from Fresh Market was delivered: 2 items, $42.50."


@pytest.mark.db
@pytest.mark.parametrize("settings", [None, "empty", "other_keys"])
def test_unconfigured_means_hold(
    world: str, reminders: dict[str, int], settings: str | None
) -> None:
    # No approved name for the key (no settings, empty `WA_TPL_*`, or only other keys'
    # names): step 4 finds no template and the approval is held (step 5).
    env = full_env("api")
    if settings == "other_keys":
        env |= {
            t.setting.upper(): t.name
            for t in templates.TEMPLATES.values()
            if t.key != "approval_request"
        }
    configured = None if settings is None else Settings.from_env(env)
    _outside(world, w.MEMBER_PHONE)
    msg = _out(world, _approval())
    _send(world, msg, settings=configured)
    assert w.query(world, "SELECT * FROM sim_outbound") == []
    [held] = w.query(world, "SELECT * FROM held_messages")
    assert held["message_id"] == msg.row.message_id
    [send] = w.query(world, "SELECT * FROM whatsapp_sends")
    assert send["outcome"] == s.HELD


@pytest.mark.db
@pytest.mark.parametrize(
    "change",
    [
        "UPDATE approvals SET status = 'approved'",
        "UPDATE timers SET status = 'done'",
        "UPDATE timers SET due_at = now() - interval '1 minute'",
    ],
)
def test_unbuildable_params_mean_hold(world: str, reminders: dict[str, int], change: str) -> None:
    # A decided or expired approval has no template parameters: it is held, and its release
    # check drops it later (fails closed).
    w.query(world, change + " RETURNING 1")
    _outside(world, w.MEMBER_PHONE)
    _send(world, _out(world, _approval()), settings=_configured())
    assert w.query(world, "SELECT * FROM sim_outbound") == []
    [send] = w.query(world, "SELECT * FROM whatsapp_sends")
    assert send["outcome"] == s.HELD


@pytest.mark.db
def test_params_have_no_sensitive_codes(world: str, reminders: dict[str, int]) -> None:
    names = {r["display_name"].lower() for r in w.query(world, "SELECT display_name FROM users")}
    codes = {c.value for c in AllergenCode} | {c.value for c in DietCode}
    stored = {r["value"] for r in w.query(world, "SELECT value FROM member_constraints")}
    assert names >= {n.lower() for n in NAMES} and stored <= codes and stored
    forbidden = names | codes | {c.replace("_", " ") for c in codes}
    for key, body in _bodies(reminders).items():
        values = _params(world, key, body)
        assert values is not None, key
        for value in values:
            words = set(re.findall(r"[a-z_]+", value.lower()))
            assert not words & forbidden, (key, value)
            assert not any(f in value.lower() for f in forbidden if " " in f), (key, value)
    # A `consent_revoked` subject other than the two fixed phrases is never a parameter.
    for agent in ("Kavi", "peanuts", ""):
        body = {**_bodies(reminders)["consent_revoked"], "params": {"agent": agent}}
        assert _params(world, "consent_revoked", body) is None


@pytest.mark.db
def test_template_refusal_not_retried(world: str, reminders: dict[str, int]) -> None:
    # §5 step 7: 132000-132999 (template refused or paused) ends `refused`, after one
    # request, with no sleep and no `StepFailed`.
    _outside(world, w.REAL_PHONE)
    graph = Graph((400, {"error": {"code": 132001, "message": "template does not exist"}}))
    msg = _out(world, _approval(), w.LIVE_ADMIN)
    _send(world, msg, graph.sender(), _configured())
    assert len(graph.requests) == 1
    assert graph.requests[0]["type"] == "template"
    assert graph.sleeps == []
    [send] = w.query(world, "SELECT * FROM whatsapp_sends")
    assert (send["outcome"], send["reason"]) == (s.REFUSED, s.TEMPLATE_REFUSED)
    assert w.query(world, "SELECT * FROM held_messages") == []
    _send(world, msg, graph.sender(), _configured())  # a replayed step sends nothing
    assert len(graph.requests) == 1


@pytest.mark.db
def test_131047_then_template(world: str, reminders: dict[str, int]) -> None:
    # Inside by our records, but Graph says outside (131047): the free-form send is not
    # retried, and step 4 sends the template.
    calls: list[dict[str, Any]] = []

    async def graph(phone: str, body: dict[str, Any]) -> str | None:
        calls.append(body)
        if "meta_template" not in body:
            raise s.GraphRefused(s.OUTSIDE_WINDOW_CODE)
        return "wamid.T"

    msg = _out(world, _approval(), w.LIVE_ADMIN)
    _send(world, msg, graph, _configured())
    assert len(calls) == 2
    assert "meta_template" not in calls[0]
    assert calls[1]["meta_template"]["name"] == "approval_needed_v1"
    [send] = w.query(world, "SELECT * FROM whatsapp_sends")
    assert (send["outcome"], send["graph_message_id"]) == (s.TEMPLATE, "wamid.T")
    assert w.query(world, "SELECT * FROM held_messages") == []
