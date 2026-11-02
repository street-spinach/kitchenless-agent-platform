"""The Square Orders API client shared by the Square adapters (adapters §7, file 24).

Raw `httpx`, not the Square SDK (the `mcp` container's memory cap; Alternative AD15).

*   Base URL `https://connect.squareupsandbox.com/v2`; `Square-Version` is pinned to
    `SQUARE_VERSION`, the version the recordings in `tests/contract/fixtures/` use.
*   The connect timeout is 3 s, and each call runs inside `asyncio.timeout(10)`: httpx
    timeouts apply to each phase separately, so the outer deadline is what keeps one
    attempt below the MCP client's 15 s timeout.
*   Every failure is an `AdapterFailure` with `vendor="square"` and, when known,
    `http_status` and `vendor_code` (the §7 error table, `_raise_for`).
*   Nothing here logs. Request and response bodies and headers (the token) are never
    logged or put into an exception message.

`client_for(token)` returns one cached client per token; a test passes an
`httpx.MockTransport` as `transport` and gets its own, uncached client.
"""

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from services.mcp.meta import AdapterFailure

__all__ = [
    "BASE_URL",
    "CALL_DEADLINE_SECONDS",
    "CONNECT_TIMEOUT_SECONDS",
    "SQUARE_VERSION",
    "VENDOR",
    "SquareClient",
    "client_for",
]

SQUARE_VERSION = "2025-01-23"
BASE_URL = "https://connect.squareupsandbox.com/v2"
VENDOR = "square"
CONNECT_TIMEOUT_SECONDS = 3.0
CALL_DEADLINE_SECONDS = 10.0

# AdapterFailure reasons (§7 table).
TIMEOUT = "adapter_timeout"
SERVER_ERROR = "adapter_5xx"
RATE_LIMITED = "adapter_rate_limited"
NOT_CONFIGURED = "adapter_not_configured"
KEY_REUSED = "vendor_key_reused"
CLIENT_ERROR = "adapter_4xx"

# Square's error code for an idempotency key reused with a different request.
_KEY_REUSED_CODE = "IDEMPOTENCY_KEY_REUSED"


def _fail(
    reason: str, retryable: bool, *, http_status: int | None = None, code: str | None = None
) -> AdapterFailure:
    return AdapterFailure(
        reason, retryable, vendor=VENDOR, http_status=http_status, vendor_code=code
    )


def _vendor_code(response: httpx.Response) -> str | None:
    """The first Square error code in the body, or `None`."""
    try:
        errors = response.json().get("errors") or []
        code = errors[0].get("code")
    except (ValueError, AttributeError, IndexError, TypeError):
        return None
    return code if isinstance(code, str) else None


def _raise_for(response: httpx.Response) -> None:
    """Raise the §7 `AdapterFailure` for a non-2xx response; return for a 2xx."""
    status = response.status_code
    if status < 300:
        return
    code = _vendor_code(response)
    if status >= 500:
        raise _fail(SERVER_ERROR, True, http_status=status, code=code)
    if status == 429:
        raise _fail(RATE_LIMITED, True, http_status=status, code=code)
    if status in (401, 403):
        raise _fail(NOT_CONFIGURED, False, http_status=status, code=code)
    if code == _KEY_REUSED_CODE:
        raise _fail(KEY_REUSED, False, http_status=status, code=code)
    raise _fail(CLIENT_ERROR, False, http_status=status, code=code)


def _line_items(lines: Sequence[Mapping[str, Any]], currency: str) -> list[dict[str, Any]]:
    """Ad-hoc line items: our name, a whole quantity as a string, our price, our SKU."""
    return [
        {
            "name": str(line["name"]),
            "quantity": str(int(line["qty"])),
            "base_price_money": {"amount": int(line["unit_minor"]), "currency": currency},
            "note": str(line["sku"]),
        }
        for line in lines
    ]


class SquareClient:
    """One `httpx.AsyncClient` for one Square access token."""

    def __init__(self, token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Square-Version": SQUARE_VERSION,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(CALL_DEADLINE_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
            transport=transport,
        )

    async def _post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        try:
            async with asyncio.timeout(CALL_DEADLINE_SECONDS):
                response = await self._http.post(path, json=body)
        except (TimeoutError, httpx.TransportError):
            raise _fail(TIMEOUT, True) from None
        _raise_for(response)
        try:
            data = response.json()
        except ValueError:
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("order"), dict):
            # A 2xx without an order: treated as a server fault. A retry sends the same
            # body under the same key, so Square still makes at most one order.
            raise _fail(SERVER_ERROR, True, http_status=response.status_code)
        return data["order"]

    async def create_order(
        self,
        *,
        idempotency_key: str,
        location_id: str,
        reference_id: str,
        lines: Sequence[Mapping[str, Any]],
        currency: str,
    ) -> dict[str, Any]:
        """`POST /orders`: an unpaid `OPEN` order with one `PICKUP` fulfilment.

        `lines` hold `sku`, `name`, `qty` and `unit_minor`. The fulfilment's recipient
        display name is `reference_id` (our order id): no household name, phone or address
        is sent. Returns Square's `order` object.
        """
        body = {
            "idempotency_key": idempotency_key,
            "order": {
                "location_id": location_id,
                "reference_id": reference_id,
                "line_items": _line_items(lines, currency),
                "fulfillments": [
                    {
                        "type": "PICKUP",
                        "state": "PROPOSED",
                        "pickup_details": {
                            "recipient": {"display_name": reference_id},
                            "schedule_type": "ASAP",
                        },
                    }
                ],
            },
        }
        return await self._post("/orders", body)

    async def calculate_order(
        self, *, location_id: str, lines: Sequence[Mapping[str, Any]], currency: str
    ) -> dict[str, Any]:
        """`POST /orders/calculate` for the same ad-hoc lines. Returns Square's `order`.

        Not used by the adapters: build step 0 found no taxes on ad-hoc lines, so `quote`
        prices from our tables (`docs/spikes/vendors.md` run 2).
        """
        body = {"order": {"location_id": location_id, "line_items": _line_items(lines, currency)}}
        return await self._post("/orders/calculate", body)


_clients: dict[str, SquareClient] = {}


def client_for(token: str, transport: httpx.AsyncBaseTransport | None = None) -> SquareClient:
    """The cached client for `token`; with a `transport` (tests), a new uncached client."""
    if transport is not None:
        return SquareClient(token, transport=transport)
    client = _clients.get(token)
    if client is None:
        client = _clients[token] = SquareClient(token)
    return client
