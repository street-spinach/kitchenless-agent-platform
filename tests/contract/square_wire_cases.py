"""The Square wire cases (adapters §11, file 29), shared by
`tests/contract/{grocery,restaurant}/test_square_wire.py`.

Each case replays recordings from `tests/contract/fixtures/square_*.json` through
`httpx.MockTransport` (`tests/contract/square.py`), with the network blocked. Every case
takes the area's Square adapter class and card id.
"""

import asyncio
import json
from typing import Any

import httpx
import pytest
import yaml

from services.mcp import square_client
from services.mcp.meta import AdapterFailure
from tests.contract import square
from tests.contract.shop import OAT_MILK, Shop, card

_HOUSEHOLDS = square.FIXTURES.parents[2] / "services" / "seed" / "data" / "households.yaml"


def _adapter(adapter_cls: type[Any], agent_id: str, currency: str = "USD") -> Any:
    return adapter_cls(card(agent_id, currency), "live")


def _place(
    shop: Shop,
    adapter: Any,
    *,
    lines: list[dict[str, Any]] | None = None,
    currency: str = "USD",
) -> Any:
    return shop.run(
        lambda: adapter.place_order(
            idempotency_key=square.RECORDED_KEY,
            order_id=square.RECORDED_ORDER_ID,
            lines=square.RECORDED_LINES if lines is None else lines,
            currency=currency,
        )
    )


def _failure(shop: Shop, adapter: Any, **kwargs: Any) -> AdapterFailure:
    with pytest.raises(AdapterFailure) as e:
        _place(shop, adapter, **kwargs)
    return e.value


def _fields(failure: AdapterFailure) -> tuple[Any, ...]:
    return (
        failure.reason,
        failure.retryable,
        failure.vendor,
        failure.http_status,
        failure.vendor_code,
    )


def create_order(shop: Shop, mp: pytest.MonkeyPatch, cls: type[Any], agent_id: str) -> None:
    replay = square.Replay("create_order")
    square.use_square(mp, replay)

    result = _place(shop, _adapter(cls, agent_id))

    # Every field we send matches the recording: ad-hoc lines, reference id, pickup.
    assert replay.sent == [square.load("create_order")["request"]["body"]]
    assert result == {
        "external_order_id": square.RECORDED_SQUARE_ORDER_ID,
        "state": "placed",
        "total_minor": 1348,
        "currency": "USD",
    }


def replay_same_key(shop: Shop, mp: pytest.MonkeyPatch, cls: type[Any], agent_id: str) -> None:
    replay = square.Replay("create_order", "create_order_replay")
    square.use_square(mp, replay)
    adapter = _adapter(cls, agent_id)

    first = _place(shop, adapter)
    again = _place(shop, adapter)

    # A retry sends the same body under the same key, and Square returns the same order.
    assert replay.sent[0] == replay.sent[1]
    assert again == first
    assert first["external_order_id"] == square.RECORDED_SQUARE_ORDER_ID


def total_mismatch(shop: Shop, mp: pytest.MonkeyPatch, cls: type[Any], agent_id: str) -> None:
    # The authorised price of one line differs from what Square totals (1348).
    lines = [dict(line) for line in square.RECORDED_LINES]
    lines[0]["unit_minor"] += 1
    square.use_square(mp, square.Replay("create_order"))

    failure = _failure(shop, _adapter(cls, agent_id), lines=lines)

    assert _fields(failure) == ("total_mismatch", False, "square", None, None)


def server_error(shop: Shop, mp: pytest.MonkeyPatch, cls: type[Any], agent_id: str) -> None:
    square.use_square(mp, square.Replay("5xx"))

    failure = _failure(shop, _adapter(cls, agent_id))

    assert _fields(failure) == ("adapter_5xx", True, "square", 503, "SERVICE_UNAVAILABLE")


def rate_limited(shop: Shop, mp: pytest.MonkeyPatch, cls: type[Any], agent_id: str) -> None:
    square.use_square(mp, square.Replay("429"))

    failure = _failure(shop, _adapter(cls, agent_id))

    assert _fields(failure) == ("adapter_rate_limited", True, "square", 429, "RATE_LIMITED")


def key_reused_different_body(
    shop: Shop, mp: pytest.MonkeyPatch, cls: type[Any], agent_id: str
) -> None:
    # Build step 0: Square answers HTTP 200 with the original order (2 oat milks) when the
    # key is reused with a different body (3 oat milks). The adapter must notice.
    replay = square.Replay("key_reused_different_body")
    square.use_square(mp, replay)
    lines = [dict(line) for line in square.RECORDED_LINES]
    assert lines[0]["sku"] == OAT_MILK
    lines[0]["qty"] = 3

    failure = _failure(shop, _adapter(cls, agent_id), lines=lines)

    assert replay.sent == [square.load("key_reused_different_body")["request"]["body"]]
    assert _fields(failure) == ("vendor_key_reused", False, "square", None, None)


def unauthorized(shop: Shop, mp: pytest.MonkeyPatch, cls: type[Any], agent_id: str) -> None:
    square.use_square(mp, square.Replay("unauthorized"))

    failure = _failure(shop, _adapter(cls, agent_id))

    assert _fields(failure) == ("adapter_not_configured", False, "square", 401, "UNAUTHORIZED")


def bad_location(shop: Shop, mp: pytest.MonkeyPatch, cls: type[Any], agent_id: str) -> None:
    replay = square.Replay("bad_location")
    location = {
        "SQUARE_RETAIL_LOCATION_ID": "NOTALOCATION",
        "SQUARE_RESTAURANT_LOCATION_ID": "NOTALOCATION",
    }
    square.use_square(mp, replay, **location)

    failure = _failure(shop, _adapter(cls, agent_id))

    # The recorder used a fresh key for this case; the order body is the same.
    [sent] = replay.sent
    assert sent["order"] == square.load("bad_location")["request"]["body"]["order"]
    assert _fields(failure) == ("adapter_not_configured", False, "square", 403, "FORBIDDEN")


def currency_mismatch(shop: Shop, mp: pytest.MonkeyPatch, cls: type[Any], agent_id: str) -> None:
    # The order's currency differs from the card's: refused before any call.
    square.use_square(mp, None)
    failure = _failure(shop, _adapter(cls, agent_id), currency="EUR")
    assert _fields(failure) == ("currency_mismatch", False, "square", None, None)

    # Square answers in another currency (USD) than the card's (CAD).
    square.use_square(mp, square.Replay("create_order"))
    failure = _failure(shop, _adapter(cls, agent_id, "CAD"), currency="CAD")
    assert _fields(failure) == ("currency_mismatch", False, "square", None, None)


NOT_CONFIGURED_SETTINGS = [
    {"SQUARE_ENV": "production"},
    {"SQUARE_ENV": ""},
    {"SQUARE_RETAIL_TOKEN": "", "SQUARE_RESTAURANT_TOKEN": ""},
    {"SQUARE_RETAIL_LOCATION_ID": "", "SQUARE_RESTAURANT_LOCATION_ID": ""},
]


def not_configured(
    shop: Shop, mp: pytest.MonkeyPatch, cls: type[Any], agent_id: str, overrides: dict[str, str]
) -> None:
    # Outside the sandbox, or with no token or location id: refused before any call.
    square.use_square(mp, None, **overrides)

    failure = _failure(shop, _adapter(cls, agent_id))

    assert _fields(failure) == ("adapter_not_configured", False, "square", None, None)


def _household_values() -> set[str]:
    """The seed households' names, phones and address lines, cities and postcodes."""
    data = yaml.safe_load(_HOUSEHOLDS.read_text())
    values: set[str] = set()
    for household in data["households"]:
        values.add(household["name"])
        address = household.get("address") or {}
        values.update(str(address[k]) for k in ("line1", "city", "postal_code") if k in address)
        for member in household.get("members") or []:
            if member.get("phone"):
                values.add(member["phone"])
    return values


def recorded_requests_have_no_household_fields(
    shop: Shop, mp: pytest.MonkeyPatch, cls: type[Any], agent_id: str
) -> None:
    values = _household_values()
    assert {"US household 1", "100 Main Street", "+15550100001"} <= values
    # Every recorded request, and every request this adapter sends.
    replay = square.Replay("create_order")
    square.use_square(mp, replay)
    _place(shop, _adapter(cls, agent_id))
    bodies = [json.loads(p.read_text())["request"]["body"] for p in square.recorded_files()]
    assert len(bodies) == 8
    for body in bodies + replay.sent:
        text = json.dumps(body)
        leaked = sorted(v for v in values if v in text)
        assert leaked == [], leaked


def network_is_blocked() -> None:
    client = square_client.SquareClient("no-token")

    async def call() -> None:
        await client._http.post("/orders", json={})

    with pytest.raises(RuntimeError, match="network blocked"):
        asyncio.run(call())
    # A real `httpx` call outside our client is blocked too.
    with pytest.raises(RuntimeError, match="network blocked"):
        httpx.get("https://connect.squareupsandbox.com/v2/locations")
