"""Square restaurant wire contract (adapters §11, file 29): `SquareRestaurantAdapter`
against the recorded Square responses in `tests/contract/fixtures/`, with the network
blocked. The cases are in `tests/contract/square_wire_cases.py`. Both Square cards share
one sandbox account and differ by location id (build step 0), so they replay the same
recordings.
"""

import pytest

from services.mcp.restaurant.adapters.square import SquareRestaurantAdapter
from tests.contract import square_wire_cases as cases
from tests.contract.shop import SQUARE_KITCHEN, Shop

pytestmark = pytest.mark.usefixtures("no_network")

CLS, AGENT = SquareRestaurantAdapter, SQUARE_KITCHEN


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
