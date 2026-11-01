"""Adapter mode resolution (architecture §5 and item 5; issue #29)."""

import asyncio
from typing import Any

import psycopg
import pytest

from services.common import modes
from services.common.config import Settings
from services.common.modes import VENDORS


def _use_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    base = {"SERVICE_NAME": "migrate", "DATABASE_URL": "x", "LOG_LEVEL": "info"}
    settings = Settings.from_env({**base, **env})
    monkeypatch.setattr(modes, "get_settings", lambda: settings)


class _Cursor:
    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[tuple[str, str]]:
        return self._rows


class _FakeConn:
    """Stands in for a connection whose `adapter_modes` table holds `rows`."""

    def __init__(self, rows: list[tuple[str, str]] | None = None) -> None:
        self._rows = rows or []

    async def execute(self, query: str, *args: Any) -> _Cursor:
        assert "adapter_modes" in query
        return _Cursor(self._rows)


def _resolve(conn: Any) -> dict[str, str]:
    return asyncio.run(modes.resolve_modes(conn))


def test_force_mock_overrides_all(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_env(
        monkeypatch,
        MODE_FORCE_MOCK="1",
        MODE="real",
        MODE_SQUARE_RETAIL="real",
        MODE_DOORDASH="real",
    )
    panel = [("kroger_catalog", "real"), ("square_restaurant", "real")]

    assert _resolve(_FakeConn(panel)) == {vendor: "mock" for vendor in VENDORS}


@pytest.mark.db
def test_panel_override_beats_env(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_env(monkeypatch, MODE="mock", MODE_SQUARE_RETAIL="mock", MODE_DOORDASH="real")
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO adapter_modes (vendor, mode, set_by) VALUES"
            " ('square_retail', 'real', 'panel'), ('doordash', 'mock', 'panel')"
        )

    async def go() -> dict[str, str]:
        async with await psycopg.AsyncConnection.connect(pg) as conn:
            return await modes.resolve_modes(conn)

    resolved = asyncio.run(go())

    assert resolved["square_retail"] == "real"
    assert resolved["doordash"] == "mock"
    assert resolved["kroger_catalog"] == "mock"


def test_vendor_env_beats_global_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_env(monkeypatch, MODE="real", MODE_DOORDASH="mock")

    resolved = _resolve(_FakeConn())

    assert resolved["doordash"] == "mock"
    assert resolved["square_retail"] == "real"
    assert resolved["kroger_catalog"] == "real"


def test_kroger_ordering_always_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_env(monkeypatch, MODE="real", MODE_KROGER_ORDERING="real")

    resolved = _resolve(_FakeConn([("kroger_ordering", "real")]))

    assert resolved["kroger_ordering"] == "mock"
    assert resolved["kroger_catalog"] == "real"
    assert set(resolved) == VENDORS
