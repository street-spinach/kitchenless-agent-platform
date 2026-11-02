"""Adapter mode resolution (architecture §5 "Mode resolution", item 5).

`resolve_modes` picks `real` or `mock` for each vendor key, once, when a run starts.
Precedence, highest first:

1. `MODE_FORCE_MOCK=1` (set in CI) makes every vendor mock.
2. A control-panel override row in `adapter_modes`.
3. `MODE_<VENDOR>` from the container environment, when set and not blank.
4. `MODE`, which defaults to `mock`.

Every key in `ALWAYS_MOCK`, the vendor keys with no real adapter class in `_ADAPTERS`, is
`mock` before any of these is read (adapters §3).

`adapter_for(vendor, run_modes)` returns the adapter class for the vendor's mode in the run.
A key in `VENDORS` that is missing from a non-empty `run_modes` is `mock`; an empty
`run_modes` raises. A `real` vendor whose real adapter is not built raises
`AdapterFailure("adapter_not_configured", retryable=False)`. It never falls back to the mock
(item 5, amended 2026-11-02, #47; Alternative R).

`pin_for_env(modes, env)` pins every key to `mock` for an `eval` household's run: the mock
tables are split by `env`, the Square sandbox is not.
"""

import importlib
from collections.abc import Mapping
from typing import Any, Literal, cast

from psycopg import AsyncConnection

from services.common.config import Settings, get_settings

__all__ = [
    "ALWAYS_MOCK",
    "NOT_CONFIGURED",
    "VENDORS",
    "Mode",
    "adapter_for",
    "pin_for_env",
    "resolve_modes",
]

Mode = Literal["real", "mock"]

VENDORS = frozenset(
    {"kroger_catalog", "kroger_ordering", "square_retail", "square_restaurant", "doordash"}
)
_MODES = frozenset({"real", "mock"})

NOT_CONFIGURED = "adapter_not_configured"

_MOCK_STORE = "services.mcp.grocery.adapters.mock_store:MockStoreAdapter"
_MOCK_RESTAURANT = "services.mcp.restaurant.adapters.mock_restaurant:MockRestaurantAdapter"
_MOCK_COURIER = "services.mcp.delivery.adapters.mock_courier:MockCourierAdapter"
# The Square classes (adapters §2 and file 26). Their modules land in adapters build step 6;
# until then `adapter_for` raises `adapter_not_configured` for them.
_SQUARE_RETAIL = "services.mcp.grocery.adapters.square:SquareRetailAdapter"
_SQUARE_RESTAURANT = "services.mcp.restaurant.adapters.square:SquareRestaurantAdapter"
# Adapter classes by vendor and mode, as `module:Class`, imported on use so this module
# does not depend on the MCP servers. Kroger and DoorDash have no real class (adapters §12).
_ADAPTERS: dict[tuple[str, Mode], str] = {
    ("kroger_catalog", "mock"): _MOCK_STORE,
    ("kroger_ordering", "mock"): _MOCK_STORE,
    ("square_retail", "mock"): _MOCK_STORE,
    ("square_retail", "real"): _SQUARE_RETAIL,
    ("square_restaurant", "mock"): _MOCK_RESTAURANT,
    ("square_restaurant", "real"): _SQUARE_RESTAURANT,
    ("doordash", "mock"): _MOCK_COURIER,
}

# Vendor keys with no real adapter class: always `mock` (adapters §3).
ALWAYS_MOCK = VENDORS - {key for (key, mode) in _ADAPTERS if mode == "real"}


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
        # Empty or blank means "use MODE" (.env.example): Compose passes unset ones as "".
        # A non-blank value is checked as given, so an invalid one still raises, even for
        # an always-mock key.
        env = getattr(settings, f"mode_{vendor}")
        vendor_env = _check(f"MODE_{vendor.upper()}", env) if env and env.strip() else None
        if vendor in ALWAYS_MOCK:
            modes[vendor] = "mock"
        elif vendor in overrides:
            modes[vendor] = _check(f"adapter_modes[{vendor}]", overrides[vendor])
        elif vendor_env is not None:
            modes[vendor] = vendor_env
        else:
            modes[vendor] = default
    return modes


def pin_for_env(modes: dict[str, Mode], env: str) -> dict[str, Mode]:
    """Every key `mock` for an `eval` household's run; otherwise `modes` unchanged."""
    if env == "eval":
        return {key: "mock" for key in modes}
    return modes


def adapter_for(vendor: str, run_modes: Mapping[str, str]) -> type[Any]:
    """The adapter class for `vendor` in the run's mode (`runs.modes`).

    A vendor in `VENDORS` that is missing from a non-empty `run_modes` uses the mock.
    Raises `AdapterFailure("adapter_not_configured", retryable=False)` when `run_modes` is
    empty, the vendor is unknown, or it has no adapter class for its mode. A `real` vendor
    with no real adapter is never given the mock.
    """
    from services.mcp.meta import AdapterFailure

    if not run_modes or vendor not in VENDORS:
        raise AdapterFailure(NOT_CONFIGURED, retryable=False)
    mode = run_modes.get(vendor, "mock")
    path = _ADAPTERS.get((vendor, cast(Mode, mode)))
    if path is None:
        raise AdapterFailure(NOT_CONFIGURED, retryable=False)
    module, _, name = path.partition(":")
    try:
        loaded = importlib.import_module(module)
    except ModuleNotFoundError as e:
        # A registered real class whose module is not built yet (adapters build step 6).
        if e.name != module:
            raise
        raise AdapterFailure(NOT_CONFIGURED, retryable=False) from None
    return cast(type[Any], getattr(loaded, name))
