"""Adapter mode resolution (architecture §5 "Mode resolution", item 5).

`resolve_modes` picks `real` or `mock` for each vendor key, once, when a run starts.
Precedence, highest first:

1. `MODE_FORCE_MOCK=1` (set in CI) makes every vendor mock.
2. A control-panel override row in `adapter_modes`.
3. `MODE_<VENDOR>` from the container environment.
4. `MODE`, which defaults to `mock`.

`kroger_ordering` is always mock.

`adapter_for(vendor, run_modes)` returns the adapter class for the vendor's mode in the run.
Until a vendor's real adapter exists (build step 7), `real` raises
`AdapterFailure("adapter_not_configured", retryable=False)`. It never falls back to the mock
(item 5, amended 2026-11-02, #47; Alternative R).
"""

import importlib
from collections.abc import Mapping
from typing import Any, Literal, cast

from psycopg import AsyncConnection

from services.common.config import Settings, get_settings

__all__ = ["ALWAYS_MOCK", "NOT_CONFIGURED", "VENDORS", "Mode", "adapter_for", "resolve_modes"]

Mode = Literal["real", "mock"]

VENDORS = frozenset(
    {"kroger_catalog", "kroger_ordering", "square_retail", "square_restaurant", "doordash"}
)
ALWAYS_MOCK = frozenset({"kroger_ordering"})

_MODES = frozenset({"real", "mock"})

NOT_CONFIGURED = "adapter_not_configured"

_MOCK_STORE = "services.mcp.grocery.adapters.mock_store:MockStoreAdapter"
_MOCK_RESTAURANT = "services.mcp.restaurant.adapters.mock_restaurant:MockRestaurantAdapter"
_MOCK_COURIER = "services.mcp.delivery.adapters.mock_courier:MockCourierAdapter"
# Adapter classes by vendor and mode, as `module:Class`, imported on use so this module
# does not depend on the MCP servers. The real adapters are added in build step 7; until
# then those entries are missing.
_ADAPTERS: dict[tuple[str, Mode], str] = {
    ("kroger_catalog", "mock"): _MOCK_STORE,
    ("kroger_ordering", "mock"): _MOCK_STORE,
    ("square_retail", "mock"): _MOCK_STORE,
    ("square_restaurant", "mock"): _MOCK_RESTAURANT,
    ("doordash", "mock"): _MOCK_COURIER,
}


def _check(name: str, value: str) -> Mode:
    if value not in _MODES:
        raise ValueError(f"{name} must be 'real' or 'mock', got {value!r}")
    return cast(Mode, value)


async def _panel_overrides(conn: AsyncConnection) -> dict[str, str]:
    cur = await conn.execute("SELECT vendor, mode FROM adapter_modes")
    return {vendor: mode for vendor, mode in await cur.fetchall()}


async def resolve_modes(conn: AsyncConnection) -> dict[str, Mode]:
    """One mode per vendor key in `VENDORS`, by the precedence in the module docstring."""
    settings: Settings = get_settings()
    if settings.mode_force_mock:
        return {vendor: "mock" for vendor in VENDORS}
    default = _check("MODE", settings.mode)
    overrides = await _panel_overrides(conn)
    modes: dict[str, Mode] = {}
    for vendor in VENDORS:
        if vendor in ALWAYS_MOCK:
            modes[vendor] = "mock"
        elif vendor in overrides:
            modes[vendor] = _check(f"adapter_modes[{vendor}]", overrides[vendor])
        elif (env := getattr(settings, f"mode_{vendor}")) is not None:
            modes[vendor] = _check(f"MODE_{vendor.upper()}", env)
        else:
            modes[vendor] = default
    return modes


def adapter_for(vendor: str, run_modes: Mapping[str, str]) -> type[Any]:
    """The adapter class for `vendor` in the run's mode (`runs.modes`).

    Raises `AdapterFailure("adapter_not_configured", retryable=False)` when the vendor is
    unknown, has no mode in `run_modes`, or has no adapter class for that mode. A `real`
    vendor with no real adapter is never given the mock.
    """
    from services.mcp.meta import AdapterFailure

    mode = run_modes.get(vendor)
    path = _ADAPTERS.get((vendor, cast(Mode, mode))) if vendor in VENDORS else None
    if path is None:
        raise AdapterFailure(NOT_CONFIGURED, retryable=False)
    module, _, name = path.partition(":")
    return cast(type[Any], getattr(importlib.import_module(module), name))
