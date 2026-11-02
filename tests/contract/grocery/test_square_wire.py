"""Square retail wire contract (adapters §11, file 29): `SquareRetailAdapter` against the
recorded Square responses in `tests/contract/fixtures/`, with the network blocked. The cases
are in `tests/contract/square_wire_cases.py`.
"""

import asyncio

import pytest

from services.mcp.grocery.adapters.square import SquareRetailAdapter
from services.mcp.square_client import SquareClient
from tests.contract import square
from tests.contract import square_wire_cases as cases
from tests.contract.shop import SQUARE_STORE, Shop

pytestmark = pytest.mark.usefixtures("no_network")

CLS, AGENT = SquareRetailAdapter, SQUARE_STORE


@pytest.mark.db
def test_create_order(shop_db: Shop, monkeypatch: pytest.MonkeyPatch) -> None:
    cases.create_order(shop_db, monkeypatch, CLS, AGENT)


@pytest.mark.db
def test_replay_same_key_same_order(shop_db: Shop, monkeypatch: pytest.MonkeyPatch) -> None:
    cases.replay_same_key(shop_db, monkeypatch, CLS, AGENT)


@pytest.mark.db
def test_total_mismatch(shop_db: Shop, monkeypatch: pytest.MonkeyPatch) -> None:
    cases.total_mismatch(shop_db, monkeypatch, CLS, AGENT)


@pytest.mark.db
def test_5xx_is_retryable(shop_db: Shop, monkeypatch: pytest.MonkeyPatch) -> None:
    cases.server_error(shop_db, monkeypatch, CLS, AGENT)


@pytest.mark.db
def test_429_is_retryable(shop_db: Shop, monkeypatch: pytest.MonkeyPatch) -> None:
    cases.rate_limited(shop_db, monkeypatch, CLS, AGENT)


@pytest.mark.db
def test_key_reused_different_body(shop_db: Shop, monkeypatch: pytest.MonkeyPatch) -> None:
    cases.key_reused_different_body(shop_db, monkeypatch, CLS, AGENT)


@pytest.mark.db
def test_unauthorized(shop_db: Shop, monkeypatch: pytest.MonkeyPatch) -> None:
    cases.unauthorized(shop_db, monkeypatch, CLS, AGENT)


@pytest.mark.db
def test_bad_location(shop_db: Shop, monkeypatch: pytest.MonkeyPatch) -> None:
    cases.bad_location(shop_db, monkeypatch, CLS, AGENT)


@pytest.mark.db
def test_currency_mismatch(shop_db: Shop, monkeypatch: pytest.MonkeyPatch) -> None:
    cases.currency_mismatch(shop_db, monkeypatch, CLS, AGENT)


@pytest.mark.db
@pytest.mark.parametrize("overrides", cases.NOT_CONFIGURED_SETTINGS)
def test_not_configured_refuses_before_any_call(
    shop_db: Shop, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, str]
) -> None:
    cases.not_configured(shop_db, monkeypatch, CLS, AGENT, overrides)


@pytest.mark.db
def test_recorded_requests_have_no_household_fields(
    shop_db: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases.recorded_requests_have_no_household_fields(shop_db, monkeypatch, CLS, AGENT)


def test_network_is_blocked() -> None:
    cases.network_is_blocked()


def test_calculate_order_matches_our_prices() -> None:
    # Build step 0: no taxes on ad-hoc lines, so `quote` prices from our tables and the
    # adapters never call CalculateOrder. The recording's total equals our line prices.
    replay = square.Replay("calculate_order")
    client = SquareClient(square.TOKEN, transport=replay.transport)
    lines = [
        {
            "sku": item["note"],
            "name": item["name"],
            "qty": int(item["quantity"]),
            "unit_minor": item["base_price_money"]["amount"],
        }
        for item in square.load("calculate_order")["request"]["body"]["order"]["line_items"]
    ]

    order = asyncio.run(
        client.calculate_order(location_id=square.LOCATION_ID, lines=lines, currency="USD")
    )

    # The recorder also sent a fulfilment and reference id; the lines are what Square prices.
    recorded = square.load("calculate_order")["request"]["body"]["order"]
    [sent] = replay.sent
    assert sent["order"]["line_items"] == recorded["line_items"]
    assert sent["order"]["location_id"] == recorded["location_id"]
    assert order["total_money"] == {
        "amount": sum(line["qty"] * line["unit_minor"] for line in lines),
        "currency": "USD",
    }
    assert order["total_tax_money"]["amount"] == 0
