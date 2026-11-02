"""The real WhatsApp sender: Graph API requests and the 3-attempt rule (architecture item 39,
§9; issue #73). No test calls the real Graph API: every request goes to a mock transport."""

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest

from services.api.whatsapp import sender as s
from services.common.consumer import StepFailed

PHONE = "+15550100301"
TOKEN = "test-token"
PHONE_NUMBER_ID = "123456"
RETRY = {"user_id": str(uuid.uuid4()), "template": "retry_prompt", "params": {}, "buttons": []}


class Graph:
    """A mock Graph API that answers each request with the next of `responses`."""

    def __init__(self, *responses: int | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return httpx.Response(result, json={})

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def sender(self, token: str = TOKEN, phone_number_id: str = PHONE_NUMBER_ID) -> s.GraphSender:
        return s.GraphSender(
            token, phone_number_id, transport=httpx.MockTransport(self.handle), sleep=self.sleep
        )


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
