"""US seed: launch roster, eval copies and fixtures (architecture item 55, agent-protocol P34).

`mock_stock_rows` is the one builder of `mock_stock` rows. The seed's stock writer and
`services.mocks.control.reset` both use it, so the two never drift (architecture §7,
amended 2026-11-02).

`seeded_limits` and `household_envs` read `households.yaml` the same way, so the reset's
setup keys restore the seeded values and only ever touch eval households (evals.md §4).
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "DATA_DIR",
    "MOCK_STOCK_ENVS",
    "MOCK_STOCK_QTY",
    "household_envs",
    "mock_stock_rows",
    "seeded_limits",
]

DATA_DIR = Path(__file__).parent / "data"
MOCK_STOCK_ENVS = ("live", "eval")
MOCK_STOCK_QTY = 100


def _load_items(data_dir: Path) -> dict[str, Any]:
    with (data_dir / "catalog_mock.yaml").open() as f:
        catalogs = yaml.safe_load(f)["catalogs"]
    with (data_dir / "menu_mock.yaml").open() as f:
        menus = yaml.safe_load(f)["menus"]
    return {"catalogs": catalogs, "menus": menus}


def mock_stock_rows(
    env: str, data: Mapping[str, Any] | None = None
) -> list[tuple[str, str, str, int]]:
    """`(env, agent_id, sku, qty)` for every seeded catalog and menu item.

    Reads the seed data files unless `data` (the seed's loaded data) is given. Raises
    `ValueError` for an unknown `env`.
    """
    if env not in MOCK_STOCK_ENVS:
        raise ValueError(f"env must be one of {MOCK_STOCK_ENVS}, got {env!r}")
    items = _load_items(DATA_DIR) if data is None else data
    return [
        (env, agent_id, item["sku"], MOCK_STOCK_QTY)
        for groups in (items["catalogs"], items["menus"])
        for agent_id, group in groups.items()
        for item in group
    ]


def _load_households(data_dir: Path) -> list[dict[str, Any]]:
    with (data_dir / "households.yaml").open() as f:
        households: list[dict[str, Any]] = yaml.safe_load(f)["households"]
    return households


def seeded_limits(data_dir: Path = DATA_DIR) -> dict[str, dict[str, int | None]]:
    """`{household_id: {weekly_budget_minor, per_order_minor}}` as the seed writes them.

    `None` means the seed writes no value: no `budgets` row, or a NULL
    `approval_rules.per_order_minor` (evals.md §4, file 9a).
    """
    return {
        hh["household_id"]: {
            "weekly_budget_minor": hh.get("weekly_budget_minor"),
            "per_order_minor": hh.get("per_order_minor"),
        }
        for hh in _load_households(data_dir)
    }


def household_envs(data_dir: Path = DATA_DIR) -> dict[str, str]:
    """`{household_id: env}` for every seeded household."""
    return {hh["household_id"]: hh["env"] for hh in _load_households(data_dir)}
