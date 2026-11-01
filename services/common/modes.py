"""Adapter mode resolution (architecture §5 "Mode resolution", item 5).

`resolve_modes` picks `real` or `mock` for each vendor key, once, when a run starts.
Precedence, highest first:

1. `MODE_FORCE_MOCK=1` (set in CI) makes every vendor mock.
2. A control-panel override row in `adapter_modes`.
3. `MODE_<VENDOR>` from the container environment.
4. `MODE`, which defaults to `mock`.

`kroger_ordering` is always mock.
"""

from typing import Literal, cast

from psycopg import AsyncConnection

from services.common.config import Settings, get_settings

__all__ = ["ALWAYS_MOCK", "VENDORS", "Mode", "resolve_modes"]

Mode = Literal["real", "mock"]

VENDORS = frozenset(
    {"kroger_catalog", "kroger_ordering", "square_retail", "square_restaurant", "doordash"}
)
ALWAYS_MOCK = frozenset({"kroger_ordering"})

_MODES = frozenset({"real", "mock"})


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
