"""US seed: launch roster, eval copies and fixtures (architecture item 55, agent-protocol P34).

`mock_stock_rows` is the one builder of `mock_stock` rows. The seed's stock writer and
`services.mocks.control.reset` both use it, so the two never drift (architecture §7,
amended 2026-11-02).

`seeded_limits` and `household_envs` read `households.yaml` the same way, so the reset's
setup keys restore the seeded values and only ever touch eval households (evals.md §4).

`mock_grant_rows` is the one builder of mock grants: the seed and the reset's re-run of the
seed's eval part (identity-authz I27, step 9) both use it.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "DATA_DIR",
    "MOCK_STOCK_ENVS",
    "MOCK_STOCK_QTY",
    "MockGrant",
    "eval_mock_grant_rows",
    "household_envs",
    "mock_grant_rows",
    "mock_stock_rows",
    "region_defaults",
    "seed_member_ids",
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


def region_defaults(data_dir: Path = DATA_DIR) -> dict[str, str]:
    """The seeded `us` region's `{region, zone_id, timezone, currency}` (the `defaults` of
    `households.yaml`), which a household created by onboarding takes (identity-authz §5)."""
    with (data_dir / "households.yaml").open() as f:
        defaults: dict[str, str] = yaml.safe_load(f)["defaults"]
    return {k: defaults[k] for k in ("region", "zone_id", "timezone", "currency")}


def seed_member_ids(env: str, data_dir: Path = DATA_DIR) -> dict[str, list[str]]:
    """`{household_id: [member_id]}` for the seeded households of `env`."""
    return {
        hh["household_id"]: [m["member_id"] for m in hh["members"]]
        for hh in _load_households(data_dir)
        if hh["env"] == env
    }


MockGrant = tuple[Any, str, str, Any, Any, str, list[str]]


def mock_grant_rows(hh: Mapping[str, Any]) -> list[MockGrant]:
    """`(grant_id, agent_id, agent_kind, user_id, consented_by, household_id, scopes)` of the
    one mock grant per people agent of the seeded household `hh`.

    The household agent's grant holder is the approver; a consumer agent's is its member,
    consented by the approver for a child. The seed and `services.mocks.control.reset` both
    use this builder, so the two never drift.
    """
    from services.common import ids
    from services.identity.grants import CLASS_SCOPES

    hid = hh["household_id"]
    approver = next(m for m in hh["members"] if m.get("approver"))
    admin_id = ids.seed_user_id(approver["member_id"])
    people = [(f"household:{hid}", "household", admin_id, admin_id)]
    for m in hh["members"]:
        user_id = ids.seed_user_id(m["member_id"])
        consented_by = admin_id if m["role"] == "child" else user_id
        people.append((f"consumer:{m['member_id']}", "consumer", user_id, consented_by))
    return [
        (
            ids.seed_grant_id(agent_id, user_id),
            agent_id,
            kind,
            user_id,
            consented_by,
            hid,
            list(CLASS_SCOPES[kind]),
        )
        for agent_id, kind, user_id, consented_by in people
    ]


def eval_mock_grant_rows(data_dir: Path = DATA_DIR) -> list[MockGrant]:
    """`mock_grant_rows` of every seeded eval household with members."""
    return [
        row
        for hh in _load_households(data_dir)
        if hh["env"] == "eval" and hh["members"]
        for row in mock_grant_rows(hh)
    ]
