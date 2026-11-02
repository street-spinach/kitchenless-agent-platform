"""Adapter mode resolution (architecture §5 and item 5; issue #29; adapters §3 and §4,
issue #165)."""

import asyncio
import dataclasses
import re
from pathlib import Path
from typing import Any

import psycopg
import pytest

from services.common import modes
from services.common.config import Settings
from services.common.modes import ALWAYS_MOCK, VENDORS
from services.mcp.delivery.adapters.mock_courier import MockCourierAdapter
from services.mcp.grocery.adapters.mock_store import MockStoreAdapter
from services.mcp.meta import AdapterFailure
from services.mcp.restaurant.adapters.mock_restaurant import MockRestaurantAdapter


def _use_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    base = {
        "SERVICE_NAME": "migrate",
        "DATABASE_URL": "x",
        "LOG_LEVEL": "info",
        "OPENFGA_PRESHARED_KEY": "x",
        "OPENFGA_API_URL": "x",
    }
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
    # No real Kroger class (adapters §3): always mock, whatever MODE says.
    assert resolved["kroger_catalog"] == "mock"


def test_empty_vendor_env_uses_global_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    # Compose passes an unset MODE_<VENDOR> as "" (infra/docker-compose.yml); the deploy
    # seed failed with "MODE_SQUARE_RESTAURANT must be 'real' or 'mock', got ''".
    empty = {f"MODE_{v.upper()}": "" for v in VENDORS}
    _use_env(monkeypatch, MODE="mock", **empty)

    resolved = _resolve(_FakeConn())

    assert resolved == {v: "mock" for v in VENDORS}


def test_empty_vendor_mode_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_env(monkeypatch, MODE="mock", MODE_DOORDASH="")

    resolved = _resolve(_FakeConn())

    assert resolved["doordash"] == "mock"


def test_blank_vendor_mode_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Issue #155: a whitespace-only MODE_<VENDOR> raised instead of falling through to MODE.
    _use_env(monkeypatch, MODE="mock", MODE_DOORDASH="  ")

    resolved = _resolve(_FakeConn())

    assert resolved["doordash"] == "mock"


def test_invalid_vendor_mode_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_env(monkeypatch, MODE="mock", MODE_DOORDASH="fake")

    with pytest.raises(ValueError, match="MODE_DOORDASH"):
        _resolve(_FakeConn())


def test_kroger_ordering_always_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_env(monkeypatch, MODE="real", MODE_KROGER_ORDERING="real")

    resolved = _resolve(_FakeConn([("kroger_ordering", "real")]))

    assert resolved["kroger_ordering"] == "mock"
    assert resolved["kroger_catalog"] == "mock"
    assert resolved["doordash"] == "mock"
    assert set(resolved) == VENDORS


def test_adapter_for_real_without_adapter_not_configured() -> None:
    mock = {vendor: "mock" for vendor in VENDORS}
    assert modes.adapter_for("square_retail", mock) is MockStoreAdapter
    assert modes.adapter_for("kroger_catalog", mock) is MockStoreAdapter
    assert modes.adapter_for("kroger_ordering", mock) is MockStoreAdapter
    assert modes.adapter_for("square_restaurant", mock) is MockRestaurantAdapter
    assert modes.adapter_for("doordash", mock) is MockCourierAdapter

    # A key with no real class: `real` raises and never falls back to mock.
    for vendor in ALWAYS_MOCK:
        with pytest.raises(AdapterFailure) as e:
            modes.adapter_for(vendor, {**mock, vendor: "real"})
        assert (e.value.reason, e.value.retryable) == ("adapter_not_configured", False)
    # A key missing from a non-empty run's modes is the mock (adapters §3).
    assert modes.adapter_for("square_retail", {"doordash": "mock"}) is MockStoreAdapter
    # An unknown vendor, or a run with no modes at all, fails closed.
    for vendor, run_modes in (("acme", {"acme": "mock"}), ("square_retail", {})):
        with pytest.raises(AdapterFailure) as e:
            modes.adapter_for(vendor, run_modes)
        assert e.value.reason == "adapter_not_configured"


def test_always_mock_is_keys_without_a_real_class() -> None:
    assert {"kroger_catalog", "kroger_ordering", "doordash"} == ALWAYS_MOCK


def test_always_mock_ignores_mode_real(monkeypatch: pytest.MonkeyPatch) -> None:
    real = {f"MODE_{v.upper()}": "real" for v in VENDORS}
    _use_env(monkeypatch, MODE="real", **real)

    resolved = _resolve(_FakeConn())

    for vendor in ("doordash", "kroger_catalog", "kroger_ordering"):
        assert resolved[vendor] == "mock"
    assert resolved["square_retail"] == "real"
    assert resolved["square_restaurant"] == "real"


def test_always_mock_ignores_panel_real(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_env(monkeypatch, MODE="mock")

    resolved = _resolve(_FakeConn([("doordash", "real")]))

    assert resolved["doordash"] == "mock"


def test_missing_key_in_run_modes_is_mock() -> None:
    # A run pinned before a deploy that added a key keeps working, with that key simulated.
    run_modes = {"square_retail": "mock"}
    assert modes.adapter_for("doordash", run_modes) is MockCourierAdapter
    assert modes.adapter_for("square_restaurant", run_modes) is MockRestaurantAdapter
    assert modes.adapter_for("kroger_catalog", run_modes) is MockStoreAdapter


def test_empty_run_modes_raises() -> None:
    for vendor in VENDORS:
        with pytest.raises(AdapterFailure) as e:
            modes.adapter_for(vendor, {})
        assert (e.value.reason, e.value.retryable) == ("adapter_not_configured", False)


def test_square_real_is_not_configured_until_its_class_exists() -> None:
    # The Square classes land in adapters build step 6; until then `real` fails closed.
    mock = {vendor: "mock" for vendor in VENDORS}
    for vendor in ("square_retail", "square_restaurant"):
        with pytest.raises(AdapterFailure) as e:
            modes.adapter_for(vendor, {**mock, vendor: "real"})
        assert (e.value.reason, e.value.retryable) == ("adapter_not_configured", False)


def test_eval_env_pins_mock() -> None:
    resolved: dict[str, modes.Mode] = dict.fromkeys(VENDORS, "real")

    assert modes.pin_for_env(resolved, "eval") == dict.fromkeys(VENDORS, "mock")
    assert modes.pin_for_env(resolved, "live") == resolved


_INFRA = Path(__file__).resolve().parents[2] / "infra"


def test_every_vendor_has_setting_compose_and_env_example() -> None:
    fields = {f.name for f in dataclasses.fields(Settings)}
    compose = (_INFRA / "docker-compose.yml").read_text()
    env_example = (_INFRA / ".env.example").read_text()
    for vendor in VENDORS:
        name = f"MODE_{vendor.upper()}"
        assert f"mode_{vendor}" in fields, vendor
        assert re.search(rf"^\s*{name}:", compose, re.MULTILINE), name
        assert re.search(rf"^{name}=", env_example, re.MULTILINE), name
