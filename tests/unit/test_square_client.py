"""`services/mcp/square_client.py` (adapters §7, file 31): requests, the error table, the
10 s deadline and the `AdapterFailure` fields. Every call goes through `httpx.MockTransport`.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from services.mcp import square_client
from services.mcp.meta import AdapterFailure
from services.mcp.square_client import SquareClient, client_for

TOKEN = "EAAAl-secret-sandbox-token"
LINES = [
    {"sku": "MOCK-OAT-MILK", "name": "Oat milk 1L", "qty": 2, "unit_minor": 399},
    {"sku": "MOCK-SOURDOUGH", "name": "Sourdough loaf", "qty": 1, "unit_minor": 550},
]
ORDER = {"id": "SQ-ORDER-1", "state": "OPEN", "total_money": {"amount": 1348, "currency": "USD"}}

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


def _client(handler: Handler) -> SquareClient:
    return SquareClient(TOKEN, transport=httpx.MockTransport(handler))


def _respond(status: int, body: Any) -> Handler:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return handler


def _create(client: SquareClient) -> dict[str, Any]:
    return asyncio.run(
        client.create_order(
            idempotency_key="vendor-key-1",
            location_id="LOC-1",
            reference_id="order-1",
            lines=LINES,
            currency="USD",
        )
    )


def _failure(client: SquareClient) -> AdapterFailure:
    with pytest.raises(AdapterFailure) as e:
        _create(client)
    return e.value


def test_create_order_request() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"order": ORDER})

    assert _create(_client(handler)) == ORDER

    [request] = seen
    assert request.method == "POST"
    assert str(request.url) == "https://connect.squareupsandbox.com/v2/orders"
    assert request.headers["Square-Version"] == square_client.SQUARE_VERSION == "2025-01-23"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(request.content) == {
        "idempotency_key": "vendor-key-1",
        "order": {
            "location_id": "LOC-1",
            "reference_id": "order-1",
            "line_items": [
                {
                    "name": "Oat milk 1L",
                    "quantity": "2",
                    "base_price_money": {"amount": 399, "currency": "USD"},
                    "note": "MOCK-OAT-MILK",
                },
                {
                    "name": "Sourdough loaf",
                    "quantity": "1",
                    "base_price_money": {"amount": 550, "currency": "USD"},
                    "note": "MOCK-SOURDOUGH",
                },
            ],
            "fulfillments": [
                {
                    "type": "PICKUP",
                    "state": "PROPOSED",
                    "pickup_details": {
                        "recipient": {"display_name": "order-1"},
                        "schedule_type": "ASAP",
                    },
                }
            ],
        },
    }


def test_calculate_order_request() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"order": ORDER})

    result = asyncio.run(
        _client(handler).calculate_order(location_id="LOC-1", lines=LINES, currency="USD")
    )

    assert result == ORDER
    [request] = seen
    assert request.url.path == "/v2/orders/calculate"
    body = json.loads(request.content)
    assert set(body) == {"order"} and body["order"]["location_id"] == "LOC-1"
    assert [i["note"] for i in body["order"]["line_items"]] == ["MOCK-OAT-MILK", "MOCK-SOURDOUGH"]


def _errors(code: str, category: str = "INVALID_REQUEST_ERROR") -> dict[str, Any]:
    return {"errors": [{"category": category, "code": code, "detail": "x"}]}


@pytest.mark.parametrize(
    ("status", "body", "reason", "retryable", "code"),
    [
        (500, _errors("INTERNAL_SERVER_ERROR", "API_ERROR"), "adapter_5xx", True, None),
        (503, _errors("SERVICE_UNAVAILABLE", "API_ERROR"), "adapter_5xx", True, None),
        (429, _errors("RATE_LIMITED", "RATE_LIMIT_ERROR"), "adapter_rate_limited", True, None),
        (
            401,
            _errors("UNAUTHORIZED", "AUTHENTICATION_ERROR"),
            "adapter_not_configured",
            False,
            None,
        ),
        (403, _errors("FORBIDDEN", "AUTHENTICATION_ERROR"), "adapter_not_configured", False, None),
        (400, _errors("IDEMPOTENCY_KEY_REUSED"), "vendor_key_reused", False, None),
        (400, _errors("INVALID_VALUE"), "adapter_4xx", False, None),
        (404, _errors("NOT_FOUND"), "adapter_4xx", False, None),
        (422, {}, "adapter_4xx", False, "none"),
    ],
)
def test_error_table(
    status: int, body: dict[str, Any], reason: str, retryable: bool, code: str | None
) -> None:
    failure = _failure(_client(_respond(status, body)))

    assert (failure.reason, failure.retryable) == (reason, retryable)
    assert failure.vendor == "square"
    assert failure.http_status == status
    expected_code = None if code == "none" else body["errors"][0]["code"]
    assert failure.vendor_code == expected_code
    assert TOKEN not in str(failure) and TOKEN not in repr(failure.args)


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("connection refused"),
        httpx.ConnectTimeout("connect timeout"),
        httpx.ReadTimeout("read timeout"),
    ],
)
def test_timeout_or_connection_error_is_retryable(error: Exception) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise error

    failure = _failure(_client(handler))

    assert (failure.reason, failure.retryable) == ("adapter_timeout", True)
    assert failure.vendor == "square"
    assert (failure.http_status, failure.vendor_code) == (None, None)


def test_deadline_raises_adapter_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    assert square_client.CALL_DEADLINE_SECONDS == 10
    assert square_client.CONNECT_TIMEOUT_SECONDS == 3
    client = _client(_respond(200, {"order": ORDER}))
    assert client._http.timeout.connect == 3
    # A transport that sleeps past the deadline (shortened so the test is quick).
    monkeypatch.setattr(square_client, "CALL_DEADLINE_SECONDS", 0.05)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={"order": ORDER})

    failure = _failure(_client(slow))

    assert (failure.reason, failure.retryable, failure.vendor) == (
        "adapter_timeout",
        True,
        "square",
    )


@pytest.mark.parametrize("body", [{"errors": []}, {"order": "nope"}, None])
def test_success_without_an_order_is_a_server_fault(body: Any) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if body is None:
            return httpx.Response(200, content=b"not json")
        return httpx.Response(200, json=body)

    failure = _failure(_client(handler))

    assert (failure.reason, failure.retryable, failure.http_status) == ("adapter_5xx", True, 200)


def test_never_logs_bodies_or_headers(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    _create(_client(_respond(200, {"order": ORDER})))
    _failure(_client(_respond(400, _errors("INVALID_VALUE"))))

    for secret in (TOKEN, "Oat milk", "MOCK-OAT-MILK", "LOC-1", "SQ-ORDER-1"):
        assert secret not in caplog.text


def test_client_for_caches_per_token() -> None:
    assert client_for("token-a") is client_for("token-a")
    assert client_for("token-a") is not client_for("token-b")
    # A test transport gets its own client, never the cached one.
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    assert client_for("token-a", transport) is not client_for("token-a")
