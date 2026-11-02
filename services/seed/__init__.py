"""US seed: launch roster, eval copies and fixtures (architecture item 55, agent-protocol P34).

`mock_stock_rows` is the one builder of `mock_stock` rows. The seed's stock writer and
`services.mocks.control.reset` both use it, so the two never drift (architecture §7,
amended 2026-11-02).
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

__all__ = ["DATA_DIR", "MOCK_STOCK_ENVS", "MOCK_STOCK_QTY", "mock_stock_rows"]

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
