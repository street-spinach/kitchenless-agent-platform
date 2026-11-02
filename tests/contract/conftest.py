"""Fixtures for the seller contract tests. The helpers are in `tests/contract/shop.py`."""

import pytest

from tests.contract.shop import Shop, seed_shop


@pytest.fixture
def shop_db(pg: str, monkeypatch: pytest.MonkeyPatch) -> Shop:
    """The mock store and restaurant cards, items and stock in a fresh schema."""
    return seed_shop(pg, monkeypatch)
