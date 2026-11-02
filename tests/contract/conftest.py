"""Fixtures for the seller contract tests. The helpers are in `tests/contract/shop.py` and
`tests/contract/square.py`."""

import socket
from typing import Any

import httpx
import pytest

from tests.contract.shop import Shop, seed_shop


@pytest.fixture
def shop_db(pg: str, monkeypatch: pytest.MonkeyPatch) -> Shop:
    """The mock store and restaurant cards, items and stock in a fresh schema."""
    return seed_shop(pg, monkeypatch)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any real network call: httpx's real transports and Python TCP sockets.

    Postgres is reached through libpq, and Unix sockets stay allowed, so database tests
    still run. Replayed Square calls use `httpx.MockTransport`, which is not blocked.
    """

    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("network blocked in contract tests")

    async def blocked_async(*args: Any, **kwargs: Any) -> Any:
        blocked()

    connect = socket.socket.connect

    def unix_only(sock: socket.socket, address: Any) -> None:
        if sock.family != socket.AF_UNIX:
            blocked()
        connect(sock, address)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async)
    monkeypatch.setattr(socket.socket, "connect", unix_only)
