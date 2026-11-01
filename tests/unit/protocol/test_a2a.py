"""A2A pure functions: cards, messages, task state (agent-protocol §2, §7, P5)."""

import itertools
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from services.common import ids
from services.common.protocol import a2a, render_summary
from services.common.protocol.a2a import (
    A2AContext,
    AgentCardRow,
    AgentMessageRow,
    QuoteRow,
    from_a2a_message,
    task_state,
    to_a2a_card,
    to_a2a_message,
)
from tests.unit.protocol.conftest import ORDER, QUOTE, SAMPLES, WINDOW

NOW = datetime(2026, 10, 31, 17, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
EARLIER = NOW - timedelta(minutes=5)
RUN = UUID("66666666-ffff-4fff-8fff-000000000006")
CONV = UUID("77777777-aaaa-4aaa-8aaa-000000000007")
BUYER = "household:us-1"
SELLER = "grocery:square-us"

QUOTE_ROW = QuoteRow(
    quote_id=UUID(QUOTE),
    run_id=RUN,
    order_id=UUID(ORDER),
    conversation_id=CONV,
    seller_agent_id=SELLER,
    buyer_agent_id=BUYER,
    status="open",
    expires_at=LATER,
)


def _quote(status: str, expires_at: datetime = LATER, closed_by: str | None = None) -> QuoteRow:
    return replace(QUOTE_ROW, status=status, expires_at=expires_at, closed_by=closed_by)


# One parameter per row of the §7 "Quotes as A2A tasks" table.
TASK_STATE_ROWS = [
    pytest.param(None, "pending", "TASK_STATE_SUBMITTED", id="submitted"),
    pytest.param(None, "processing", "TASK_STATE_WORKING", id="working"),
    pytest.param(_quote("open"), "processed", "TASK_STATE_INPUT_REQUIRED", id="input-open"),
    pytest.param(
        _quote("countered"), "processed", "TASK_STATE_INPUT_REQUIRED", id="input-countered"
    ),
    pytest.param(
        _quote("rejected", closed_by="seller"), "processed", "TASK_STATE_REJECTED", id="rejected"
    ),
    pytest.param(
        _quote("rejected", closed_by="buyer"), "processed", "TASK_STATE_CANCELED", id="canceled"
    ),
    pytest.param(_quote("expired"), "processed", "TASK_STATE_FAILED", id="failed-expired"),
    pytest.param(_quote("open", EARLIER), "processed", "TASK_STATE_FAILED", id="failed-open-past"),
    pytest.param(
        _quote("countered", EARLIER), "processed", "TASK_STATE_FAILED", id="failed-countered-past"
    ),
    pytest.param(None, "failed", "TASK_STATE_FAILED", id="failed-request"),
]


@pytest.mark.parametrize(("quote", "request_status", "expected"), TASK_STATE_ROWS)
def test_task_state_table(quote: QuoteRow | None, request_status: str, expected: str) -> None:
    assert task_state(quote, request_status, NOW) == expected


def test_task_state_never_completed() -> None:
    allowed = {
        "TASK_STATE_SUBMITTED",
        "TASK_STATE_WORKING",
        "TASK_STATE_INPUT_REQUIRED",
        "TASK_STATE_REJECTED",
        "TASK_STATE_CANCELED",
        "TASK_STATE_FAILED",
    }
    request_statuses = ["pending", "processing", "processed", "failed"]
    quotes: list[QuoteRow | None] = [None]
    for status, expires_at, closed_by in itertools.product(
        ["open", "countered", "accepted", "rejected", "expired"],
        [EARLIER, NOW, LATER],
        [None, "buyer", "seller"],
    ):
        quotes.append(_quote(status, expires_at, closed_by))
    seen = set()
    for quote, request_status in itertools.product(quotes, request_statuses):
        try:
            state = task_state(quote, request_status, NOW)
        except ValueError:
            continue
        seen.add(state)
        assert state in allowed
    assert "TASK_STATE_COMPLETED" not in seen
    assert seen == allowed
    # An accepted quote can never be read as a task state (an outside task is never accepted).
    with pytest.raises(ValueError):
        task_state(_quote("accepted"), "processed", NOW)


def _row(step: int, from_agent: str, to_agent: str, type_: str) -> AgentMessageRow:
    return AgentMessageRow(
        message_id=ids.message_id(RUN, step),
        run_id=RUN,
        conversation_id=CONV,
        causation_id=None if step == 0 else ids.message_id(RUN, step - 1),
        from_agent=from_agent,
        to_agent=to_agent,
        type=type_,
        body=dict(SAMPLES[type_]),
    )


def test_roles_follow_task_opener() -> None:
    # The worked negotiation of §7 (P-CUJ 1).
    steps = [
        (BUYER, SELLER, "quote_request", "ROLE_USER"),
        (SELLER, BUYER, "offer", "ROLE_AGENT"),
        (BUYER, SELLER, "counter_offer", "ROLE_USER"),
        (SELLER, BUYER, "offer", "ROLE_AGENT"),
        (BUYER, SELLER, "accept", "ROLE_USER"),
        (SELLER, BUYER, "order_confirm", "ROLE_AGENT"),
    ]
    for step, (from_agent, to_agent, type_, role) in enumerate(steps):
        message = to_a2a_message(_row(step, from_agent, to_agent, type_), QUOTE_ROW)
        assert message["role"] == role, type_
        assert message["taskId"] == QUOTE
        assert message["contextId"] == str(CONV)
    # Before the seller inserts the quotes row, the opening request is still the user's.
    opening = to_a2a_message(_row(0, BUYER, SELLER, "quote_request"), None)
    assert opening["role"] == "ROLE_USER"
    # The courier exchange is its own task: the store opened it, the courier owns it.
    store = "grocery:mock-us"
    courier_quote = replace(QUOTE_ROW, buyer_agent_id=store, seller_agent_id="delivery:mock-us")
    request = to_a2a_message(
        _row(0, store, "delivery:mock-us", "delivery_quote_request"), courier_quote
    )
    reply = to_a2a_message(_row(1, "delivery:mock-us", store, "delivery_quote"), courier_quote)
    assert (request["role"], reply["role"]) == ("ROLE_USER", "ROLE_AGENT")


def test_rationale_is_text_part_not_data() -> None:
    row = _row(1, SELLER, BUYER, "offer")
    message = to_a2a_message(row, QUOTE_ROW)
    data_parts = [p for p in message["parts"] if "data" in p]
    text_parts = [p for p in message["parts"] if "text" in p]
    assert len(data_parts) == 1
    assert data_parts[0]["data"]["type"] == "offer"
    assert "rationale" not in data_parts[0]["data"]["body"]
    assert "SECRET-RATIONALE" not in repr(data_parts[0])
    assert text_parts == [{"text": SAMPLES["offer"]["rationale"]}]
    assert "SECRET-RATIONALE" not in message["metadata"]["summary"]
    assert message["metadata"] == {
        "run_id": str(RUN),
        "from": SELLER,
        "to": BUYER,
        "causation_id": str(row.causation_id),
        "summary": message["metadata"]["summary"],
    }
    # No rationale: no text part at all.
    plain = to_a2a_message(_row(2, BUYER, SELLER, "counter_offer"), QUOTE_ROW)
    assert [p for p in plain["parts"] if "text" in p] == []


SENDER = "external:a2a-demo"
NEW_CTX = A2AContext(
    run_id=RUN,
    order_id=UUID(ORDER),
    conversation_id=CONV,
    zone_id="us-oh-45202",
    currency="USD",
    window=WINDOW,
    reply_by=WINDOW["start"],
)


def _params(*parts: dict, role: str = "ROLE_USER", message_id: str = "client-1") -> dict:
    return {"message": {"messageId": message_id, "role": role, "parts": list(parts)}}


def _quote_request_part(**overrides: object) -> dict:
    body = {
        "items": [{"item": "oat milk", "qty": 2, "unit": "l"}],
        "constraints": {"allergens": ["tree_nuts"], "diets": []},
    }
    body.update(overrides)
    return {"data": {"type": "quote_request", "body": body}}


def test_from_a2a_requires_one_data_part() -> None:
    part = _quote_request_part()
    with pytest.raises(ValueError):
        from_a2a_message(_params(), SENDER, SELLER, NEW_CTX)
    with pytest.raises(ValueError):
        from_a2a_message(_params({"text": "hi"}), SENDER, SELLER, NEW_CTX)
    with pytest.raises(ValueError):
        from_a2a_message(_params(part, part), SENDER, SELLER, NEW_CTX)
    with pytest.raises(ValueError):
        from_a2a_message(_params(part, role="ROLE_AGENT"), SENDER, SELLER, NEW_CTX)
    # Text parts are ignored; one data part is enough.
    args = from_a2a_message(_params({"text": "free text"}, part), SENDER, SELLER, NEW_CTX)
    assert args.type == "quote_request"


def test_from_a2a_ignores_client_order_fields() -> None:
    hostile = _quote_request_part(
        order_id="99999999-9999-4999-8999-999999999999",
        zone_id="elsewhere",
        currency="EUR",
        window={"start": "2030-01-01T00:00:00+00:00", "end": "2030-01-01T01:00:00+00:00"},
        reply_by="2030-01-01T00:00:00+00:00",
        kind="dinner",
    )
    args = from_a2a_message(_params(hostile), SENDER, SELLER, NEW_CTX)
    assert args.body["order_id"] == ORDER
    assert args.body["zone_id"] == "us-oh-45202"
    assert args.body["currency"] == "USD"
    assert args.body["window"] == WINDOW
    assert args.body["reply_by"] == WINDOW["start"]
    assert args.body["kind"] == "grocery"  # from the target card's class
    assert (args.run_id, args.conversation_id, args.causation_id) == (RUN, CONV, None)
    assert (args.from_agent, args.to_agent) == (SENDER, SELLER)
    # The body is a valid quote_request once the endpoint has filled its fields.
    render_summary(args.type, args.body)

    # counter_offer: order and quote ids come from the quote row named by taskId.
    quote_ctx = A2AContext(
        run_id=RUN, order_id=UUID(ORDER), conversation_id=CONV, quote_id=UUID(QUOTE)
    )
    counter = dict(SAMPLES["counter_offer"])
    counter["order_id"] = "99999999-9999-4999-8999-999999999999"
    counter["quote_id"] = "88888888-8888-4888-8888-888888888888"
    args = from_a2a_message(
        _params({"data": {"type": "counter_offer", "body": counter}}), SENDER, SELLER, quote_ctx
    )
    assert args.body["order_id"] == ORDER
    assert args.body["quote_id"] == QUOTE


def test_card_has_required_fields() -> None:
    card = AgentCardRow(
        agent_id="grocery:mock-us",
        agent_class="grocery",
        owner_ref="org:mock-grocer",
        org_name="Mock Grocer",
        config={"description": "Mock grocery store", "card_version": "1.0.0"},
    )
    a2a_card = to_a2a_card(card, "http://agents:8003")
    url = "http://agents:8003/a2a/grocery:mock-us"
    assert a2a_card["name"] == "grocery:mock-us"
    assert a2a_card["description"] == "Mock grocery store"
    assert a2a_card["version"] == "1.0.0"
    assert a2a_card["supportedInterfaces"] == [
        {"url": url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    ]
    assert a2a_card["provider"] == {"organization": "Mock Grocer", "url": url}
    assert a2a_card["capabilities"] == {
        "streaming": False,
        "pushNotifications": False,
        "extensions": [
            {
                "uri": "urn:kitchenless:on-behalf-of:v1",
                "params": {"on_behalf_of": "org:mock-grocer"},
            }
        ],
    }
    assert a2a_card["defaultInputModes"] == ["application/json"]
    assert a2a_card["defaultOutputModes"] == ["application/json", "text/plain"]
    # Q4: the proto's security fields, not the older "security" spelling.
    assert a2a_card["securitySchemes"] == {
        "bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}
    }
    assert a2a_card["securityRequirements"] == [{"schemes": {"bearer": {"list": []}}}]
    assert "security" not in a2a_card
    [skill] = a2a_card["skills"]
    assert skill["id"] == "grocery_quote"
    assert {"id", "name", "description", "tags"} <= skill.keys()
    assert set(skill["tags"]) == {"quote_request", "counter_offer", "reject"}

    restaurant = replace(card, agent_id="restaurant:mock-us", agent_class="restaurant")
    assert to_a2a_card(restaurant, "http://agents:8003")["skills"][0]["id"] == "dinner_quote"
    assert to_a2a_card(replace(card, config={}), "http://agents:8003")["version"] == "1.0.0"
    for agent_class in ("household", "consumer", "delivery", "external"):
        with pytest.raises(ValueError):
            to_a2a_card(replace(card, agent_class=agent_class), "http://agents:8003")

    # Q2 and Q3 constants, exactly as resolved.
    assert a2a.A2A_PROTOCOL_VERSION == "1.0"
    assert a2a.JSONRPC_ERRORS == {
        "TaskNotFoundError": (-32001, "TASK_NOT_FOUND"),
        "TaskNotCancelableError": (-32002, "TASK_NOT_CANCELABLE"),
        "UnsupportedOperationError": (-32004, "UNSUPPORTED_OPERATION"),
        "VersionNotSupportedError": (-32009, "VERSION_NOT_SUPPORTED"),
        "JSONParseError": (-32700, None),
        "InvalidRequestError": (-32600, None),
        "MethodNotFoundError": (-32601, None),
        "InvalidParamsError": (-32602, None),
        "InternalError": (-32603, None),
    }
    assert a2a.error_data("TASK_NOT_FOUND") == [
        {
            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
            "reason": "TASK_NOT_FOUND",
            "domain": "a2a-protocol.org",
        }
    ]
    assert a2a.error_data("WRONG_PARTY", "kitchenless")[0]["domain"] == "kitchenless"


def test_a2a_message_id_deterministic() -> None:
    params = _params(_quote_request_part(), message_id="client-42")
    first = from_a2a_message(params, SENDER, SELLER, NEW_CTX)
    second = from_a2a_message(params, SENDER, SELLER, NEW_CTX)
    expected = ids.a2a_message_id(SENDER, "client-42")
    assert first.message_id == second.message_id == expected
    assert first.trace_extra == second.trace_extra
    assert a2a.NS_A2A == ids.NS_A2A
    other = from_a2a_message(
        _params(_quote_request_part(), message_id="client-43"), SENDER, SELLER, NEW_CTX
    )
    assert other.message_id != expected
    # A different data part with the same messageId keeps the id but changes the hash (§11).
    changed = from_a2a_message(
        _params(
            _quote_request_part(items=[{"item": "rye", "qty": 1, "unit": "ea"}]),
            message_id="client-42",
        ),
        SENDER,
        SELLER,
        NEW_CTX,
    )
    assert changed.message_id == expected
    assert changed.trace_extra != first.trace_extra
    # The stored form keeps its own id when read back as A2A.
    row = _row(0, BUYER, SELLER, "quote_request")
    assert to_a2a_message(row, None)["messageId"] == str(row.message_id)
    assert to_a2a_message(row, None) == to_a2a_message(row, None)
