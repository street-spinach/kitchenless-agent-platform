"""The real WhatsApp sender: Graph API requests and the 3-attempt rule (architecture item 39,
§9; issue #73). No test calls the real Graph API: every request goes to a mock transport."""

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest

from services.api.whatsapp import sender as s
from services.common.consumer import Inbound, StepFailed
from services.common.protocol.a2a import AgentMessageRow
from services.common.protocol.schemas import WhatsAppOut
from tests.unit import api_world as w

PHONE = "+15550100301"
TOKEN = "test-token"
PHONE_NUMBER_ID = "123456"
RETRY = {"user_id": str(uuid.uuid4()), "template": "retry_prompt", "params": {}, "buttons": []}


class Graph:
    """A mock Graph API that answers each request with the next of `responses`."""

    def __init__(self, *responses: int | tuple[int, dict[str, Any]] | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, tuple):
            return httpx.Response(result[0], json=result[1])
        return httpx.Response(result, json={})

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def sender(self, token: str = TOKEN, phone_number_id: str = PHONE_NUMBER_ID) -> s.GraphSender:
        return s.GraphSender(
            token, phone_number_id, transport=httpx.MockTransport(self.handle), sleep=self.sleep
        )


def test_approval_prompt_renders_amount_and_buttons() -> None:
    # #63: the approver's prompt names the amount and carries Approve and Reject.
    graph = Graph(200)
    approval = uuid.uuid4()
    buttons = [
        {"payload": f"ap:{approval}:approved", "title": "Approve"},
        {"payload": f"ap:{approval}:rejected", "title": "Reject"},
    ]
    params = {"amount": "$76.00", "reply_by": "6:30 pm"}
    body = {**RETRY, "template": "approval_request", "params": params, "buttons": buttons}
    asyncio.run(graph.sender()(PHONE, body))
    sent = json.loads(graph.requests[0].content)
    assert sent["interactive"]["body"]["text"] == (
        "Your agent wants to spend $76.00 and needs your approval. Reply by 6:30 pm."
    )
    titles = [b["reply"]["title"] for b in sent["interactive"]["action"]["buttons"]]
    assert titles == ["Approve", "Reject"]


@pytest.mark.parametrize(
    ("template", "params", "text"),
    [
        ("approval_expired", {}, "This approval has expired, so the order was cancelled."),
        ("approval_not_approver", {"approver": "Asha"}, "Only Asha can approve this."),
        ("approval_cancelled", {}, "Order cancelled: it wasn't approved."),
    ],
)
def test_approval_reply_texts(template: str, params: dict[str, str], text: str) -> None:
    # Architecture CUJs 3 and 7 fix these texts (#63).
    payload = s.graph_payload(PHONE, {**RETRY, "template": template, "params": params})
    assert payload is not None
    assert payload["text"]["body"] == text


@pytest.mark.parametrize(
    ("template", "params", "text"),
    [
        ("chat_reply", {"text": "Try a lentil soup."}, "Try a lentil soup."),
        ("order_summary", {"text": "Delivered."}, "Delivered."),
        (
            "no_offers",
            {},
            "No store could fill this order right now. Reply *retry* to try again.",
        ),
        (
            "off_topic_reply",
            {},
            "I can only help with food: meals, groceries and dinner orders.",
        ),
        (
            "dislike_saved",
            {"value": "olives"},
            "Saved: you don't like olives. I'll avoid it when I can. "
            "Allergies are set by your household admin.",
        ),
        ("dislike_removed", {"value": "olives"}, "Done: olives is off your dislikes."),
        (
            "dislike_not_removed",
            {},
            "I can only forget dislikes you told me. Your household admin can change the others.",
        ),
        (
            "dislike_not_saved",
            {"admin": "Asha"},
            "Allergies and diets are saved by Asha. Ask them to tell me.",
        ),
        (
            "take_turns_offer",
            {"values": "olives, mushrooms"},
            "Tonight's dinner had something one person doesn't like (olives, mushrooms). "
            "Want me to take turns on whose dislikes come first?",
        ),
        (
            "take_turns_offer_off",
            {"values": "olives"},
            "Tonight's dinner had something one person doesn't like (olives). Take turns is on.",
        ),
        ("take_turns_saved", {"state": "on"}, "Take turns is now on."),
        ("take_turns_saved", {"state": "off"}, "Take turns is now off."),
        (
            "take_turns_not_approver",
            {"approver": "Asha"},
            "Only Asha can change take turns.",
        ),
        (
            "nothing_needed",
            {"items": "milk, eggs"},
            "Nothing to order this week. Already at home: milk, eggs.",
        ),
    ],
)
def test_agents_template_texts(template: str, params: dict[str, str], text: str) -> None:
    # Agents §11 templates; each renders with its params.
    payload = s.graph_payload(PHONE, {**RETRY, "template": template, "params": params})
    assert payload is not None
    assert payload["text"]["body"] == text


@pytest.mark.parametrize(
    "template",
    [
        "chat_reply",
        "order_summary",
        "dislike_saved",
        "dislike_removed",
        "take_turns_offer",
        "take_turns_offer_off",
        "take_turns_saved",
        "take_turns_not_approver",
        "dislike_not_saved",
        "nothing_needed",
    ],
)
def test_agents_template_without_its_param_is_not_rendered(template: str) -> None:
    # A missing param drops the message (fails closed).
    assert s.graph_payload(PHONE, {**RETRY, "template": template, "params": {}}) is None


def test_take_turns_offer_renders_its_button() -> None:
    order = uuid.uuid4()
    buttons = [{"payload": f"tt:{order}:on", "title": "Take turns"}]
    params = {"values": "olives"}
    body = {**RETRY, "template": "take_turns_offer", "params": params, "buttons": buttons}
    payload = s.graph_payload(PHONE, body)
    assert payload is not None
    replies = payload["interactive"]["action"]["buttons"]
    assert replies == [{"type": "reply", "reply": {"id": f"tt:{order}:on", "title": "Take turns"}}]


def test_first_attempt_succeeds_sends_one_text_message() -> None:
    graph = Graph(200)
    asyncio.run(graph.sender()(PHONE, RETRY))
    [req] = graph.requests
    assert str(req.url) == f"{s.GRAPH_URL}/{PHONE_NUMBER_ID}/messages"
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(req.content) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "15550100301",
        "type": "text",
        "text": {"body": s.TEXTS["retry_prompt"]},
    }
    assert graph.sleeps == []


def test_failed_attempts_retried_then_succeed() -> None:
    graph = Graph(500, httpx.ConnectTimeout("slow"), 200)
    asyncio.run(graph.sender()(PHONE, RETRY))
    assert len(graph.requests) == 3
    assert graph.sleeps == list(s.RETRY_DELAYS_SECONDS)


@pytest.mark.parametrize(
    "responses",
    [(500, 502, 503), (400, 400, 400), (httpx.ReadTimeout("t"), httpx.ConnectError("c"), 429)],
)
def test_three_attempts_then_step_failed(responses: tuple[int | Exception, ...]) -> None:
    graph = Graph(*responses)
    with pytest.raises(StepFailed) as err:
        asyncio.run(graph.sender()(PHONE, RETRY))
    assert (err.value.where, err.value.reason) == (s.WHERE, s.SEND_FAILED)
    assert len(graph.requests) == s.ATTEMPTS == 3
    assert graph.sleeps == list(s.RETRY_DELAYS_SECONDS)


def test_attempts_fit_in_about_a_minute_inside_the_step_cap() -> None:
    from services.common.consumer import STEP_TIMEOUT_SECONDS

    worst = s.ATTEMPTS * s.REQUEST_TIMEOUT_SECONDS + sum(s.RETRY_DELAYS_SECONDS)
    assert 45 <= worst < STEP_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "body",
    [
        {**RETRY, "template": "approval_request"},
        {**RETRY, "buttons": [{"payload": f"p{i}", "title": "t"} for i in range(4)]},
    ],
)
def test_unrenderable_body_fails_without_calling_graph(body: dict[str, Any]) -> None:
    graph = Graph()
    with pytest.raises(StepFailed) as err:
        asyncio.run(graph.sender()(PHONE, body))
    assert err.value.reason == s.UNRENDERABLE
    assert graph.requests == []


def test_window_refusal_not_retried() -> None:
    # Experience §5 step 7: 131047 means the person is outside the window; a retry can't
    # change that, so it raises `GraphRefused` after one request, with no sleep.
    graph = Graph((400, {"error": {"code": s.OUTSIDE_WINDOW_CODE, "message": "outside"}}))
    with pytest.raises(s.GraphRefused) as err:
        asyncio.run(graph.sender()(PHONE, RETRY))
    assert err.value.code == s.OUTSIDE_WINDOW_CODE
    assert len(graph.requests) == 1
    assert graph.sleeps == []


def test_other_graph_error_codes_keep_three_attempts() -> None:
    error = (400, {"error": {"code": 131026, "message": "undeliverable"}})
    graph = Graph(error, error, error)
    with pytest.raises(StepFailed):
        asyncio.run(graph.sender()(PHONE, RETRY))
    assert len(graph.requests) == s.ATTEMPTS


def test_graph_sender_returns_message_id() -> None:
    graph = Graph((200, {"messages": [{"id": "wamid.ABC"}]}), 200)
    assert asyncio.run(graph.sender()(PHONE, RETRY)) == "wamid.ABC"
    assert asyncio.run(graph.sender()(PHONE, RETRY)) is None  # no id in the response


@pytest.mark.parametrize(("token", "phone_number_id"), [("", PHONE_NUMBER_ID), (TOKEN, "")])
def test_missing_credentials_fail_without_calling_graph(token: str, phone_number_id: str) -> None:
    graph = Graph()
    with pytest.raises(StepFailed):
        asyncio.run(graph.sender(token, phone_number_id)(PHONE, RETRY))
    assert graph.requests == []


def test_buttons_sent_as_reply_buttons() -> None:
    graph = Graph(200)
    body = {**RETRY, "buttons": [{"payload": "approve:1", "title": "Approve"}]}
    asyncio.run(graph.sender()(PHONE, body))
    sent = json.loads(graph.requests[0].content)
    assert sent["type"] == "interactive"
    assert sent["interactive"] == {
        "type": "button",
        "body": {"text": s.TEXTS["retry_prompt"]},
        "action": {
            "buttons": [{"type": "reply", "reply": {"id": "approve:1", "title": "Approve"}}]
        },
    }


# --- the sim path (EX-I3) -----------------------------------------------------------------


@pytest.fixture
def sim_world(pg: str, monkeypatch: pytest.MonkeyPatch) -> str:
    w.patch_settings(monkeypatch)
    w.seed(pg)
    return pg


def _sim_send(world: str, body: dict[str, Any]) -> Inbound:
    body = {"user_id": str(w.MEMBER), "params": {}, "buttons": [], **body}
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
    msg = Inbound(row=row, attempts=1, body=WhatsAppOut.model_validate(body))
    asyncio.run(s.Sender(w.connect_to(world))(msg))
    return msg


@pytest.mark.db
@pytest.mark.parametrize(
    "body",
    [
        {**RETRY, "buttons": [{"payload": f"p{i}", "title": "t"} for i in range(4)]},
        {**RETRY, "buttons": [{"payload": "p", "title": "x" * (s.MAX_TITLE_CHARS + 1)}]},
        {"template": "chat_reply", "params": {"text": "x" * (s.MAX_BODY_CHARS + 1)}},
        {"template": "approval_request"},  # a missing param
    ],
)
def test_sim_applies_limits(sim_world: str, body: dict[str, Any]) -> None:
    body = {k: v for k, v in body.items() if k != "user_id"}
    msg = _sim_send(sim_world, body)
    assert w.query(sim_world, "SELECT * FROM sim_outbound") == []
    [trace] = w.query(sim_world, "SELECT * FROM agent_traces WHERE run_id = %s", msg.row.run_id)
    assert (trace["kind"], trace["agent_id"]) == ("decision", w.WHATSAPP)
    assert trace["message_id"] == msg.row.message_id
    payload = trace["payload"]
    assert (payload["node"], payload["choice"], payload["reason"]) == (
        s.WINDOW_NODE,
        s.REFUSED,
        s.UNRENDERABLE,
    )
    assert payload["ref"] == str(msg.row.message_id)
    [send] = w.query(sim_world, "SELECT * FROM whatsapp_sends")
    assert (send["message_id"], send["outcome"], send["reason"]) == (
        msg.row.message_id,
        s.REFUSED,
        s.UNRENDERABLE,
    )


@pytest.mark.db
def test_sim_keeps_message_id_and_template(sim_world: str) -> None:
    buttons = [{"payload": "approve:1", "title": "x" * s.MAX_TITLE_CHARS}]
    msg = _sim_send(sim_world, {"template": "retry_prompt", "buttons": buttons})
    asyncio.run(s.Sender(w.connect_to(sim_world))(msg))  # a replayed step inserts nothing
    longest = "y" * s.MAX_BODY_CHARS
    _sim_send(sim_world, {"template": "chat_reply", "params": {"text": longest}})

    first, second = w.query(sim_world, "SELECT * FROM sim_outbound ORDER BY id")
    body = first["body"]
    assert body["message_id"] == str(msg.row.message_id)
    assert body["template"] == "retry_prompt"
    assert body["buttons"] == buttons
    assert body["graph"] == s.graph_payload(w.MEMBER_PHONE, msg.body.model_dump(mode="json"))
    assert body["graph"]["to"] == w.MEMBER_PHONE.removeprefix("+")
    assert body["graph"]["interactive"]["body"]["text"] == s.TEXTS["retry_prompt"]
    assert second["body"]["graph"]["text"]["body"] == longest
    # X9: each send writes one `sent` decision row; the replay writes nothing more.
    traces = w.query(sim_world, "SELECT * FROM agent_traces")
    assert [t["payload"]["choice"] for t in traces] == [s.SENT, s.SENT]

    resp = w.client(sim_world).get(f"/internal/sim/outbox?phone={w.MEMBER_PHONE}&after_id=0")
    assert [m["id"] for m in resp.json()["messages"]] == [first["id"], second["id"]]
