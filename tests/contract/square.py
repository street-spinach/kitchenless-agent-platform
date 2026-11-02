"""Square on the replay transport, for the contract and wire tests (adapters §11).

*   `load(name)` reads `tests/contract/fixtures/square_<name>.json`, recorded by the owner
    with `scripts/record_square.py` (build step 0). Nothing here writes recordings.
*   `Replay(*names)` is an `httpx.MockTransport` handler that answers each request with the
    next recording's response, checks the method and path, and keeps every request body it
    received in `sent`.
*   `use_square(monkeypatch, replay)` points the Square adapters at sandbox Settings and at
    a client on the replay transport. Under `REAL=1` (owner only, by hand) it uses the
    `SQUARE_*` variables of the owner's terminal and the real sandbox instead.
"""

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from services.common.config import Settings
from services.mcp import square_client, square_seller
from services.mcp.square_client import SquareClient

FIXTURES = Path(__file__).resolve().parent / "fixtures"
# The location id placeholder the recorder writes in place of the real one.
LOCATION_ID = "LOCATION_ID"
TOKEN = "replay-token"
REAL = os.environ.get("REAL") == "1"

_BASE_ENV = {"SERVICE_NAME": "migrate", "DATABASE_URL": "x", "LOG_LEVEL": "info"}
_SQUARE_VARS = (
    "SQUARE_ENV",
    "SQUARE_RETAIL_TOKEN",
    "SQUARE_RESTAURANT_TOKEN",
    "SQUARE_RETAIL_LOCATION_ID",
    "SQUARE_RESTAURANT_LOCATION_ID",
)


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"square_{name}.json").read_text())


def recorded_files() -> list[Path]:
    return sorted(FIXTURES.glob("square_*.json"))


# The order of the recordings: its id, idempotency key and lines (as our order lines).
_CREATE = load("create_order")["request"]["body"]
RECORDED_KEY: str = _CREATE["idempotency_key"]
RECORDED_ORDER_ID: str = _CREATE["order"]["reference_id"]
RECORDED_LINES = [
    {
        "sku": item["note"],
        "qty": int(item["quantity"]),
        "unit_minor": item["base_price_money"]["amount"],
    }
    for item in _CREATE["order"]["line_items"]
]
RECORDED_SQUARE_ORDER_ID: str = load("create_order")["response"]["body"]["order"]["id"]


class Replay:
    """Answers requests with recorded responses, in order."""

    def __init__(self, *names: str) -> None:
        self.records = [load(name) for name in names]
        self.sent: list[dict[str, Any]] = []
        self.transport = httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        assert len(self.sent) < len(self.records), "request with no recording left"
        record = self.records[len(self.sent)]
        assert request.method == record["request"]["method"]
        assert request.url.path == "/v2" + record["request"]["path"]
        self.sent.append(json.loads(request.content))
        response = record["response"]
        return httpx.Response(response["status"], json=response["body"])


def settings(**overrides: str) -> Settings:
    """Sandbox Settings for both Square cards; `overrides` by variable name."""
    env = {
        "SQUARE_ENV": "sandbox",
        "SQUARE_RETAIL_TOKEN": TOKEN,
        "SQUARE_RESTAURANT_TOKEN": TOKEN,
        "SQUARE_RETAIL_LOCATION_ID": LOCATION_ID,
        "SQUARE_RESTAURANT_LOCATION_ID": LOCATION_ID,
        **overrides,
    }
    return Settings.from_env({**_BASE_ENV, **env})


def use_square(monkeypatch: pytest.MonkeyPatch, replay: Replay | None, **overrides: str) -> None:
    """Point the Square adapters at `replay` (or, under `REAL=1`, the live sandbox)."""
    if REAL and replay is None:
        live = {name: os.environ.get(name, "") for name in _SQUARE_VARS}
        monkeypatch.setattr(square_seller, "get_settings", lambda: settings(**live))
        # A new client per call: each `Shop.run` has its own event loop.
        monkeypatch.setattr(square_client, "client_for", lambda token: SquareClient(token))
        return
    configured = settings(**overrides)
    monkeypatch.setattr(square_seller, "get_settings", lambda: configured)
    transport = replay.transport if replay is not None else _no_calls()
    monkeypatch.setattr(
        square_client, "client_for", lambda token: SquareClient(token, transport=transport)
    )


def _no_calls() -> httpx.MockTransport:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected Square call: {request.method} {request.url.path}")

    return httpx.MockTransport(refuse)
