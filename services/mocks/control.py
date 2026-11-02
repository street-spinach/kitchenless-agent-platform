"""Mock control functions behind `/panel/*` (architecture item 47 and §7, agent-protocol P33).

*   `reset(conn, env, setup=None, household_id="us-1-eval")`: deletes the rows of `eval`
    runs and the `eval` mock state in one transaction, children first, then re-inserts
    `services.seed.mock_stock_rows("eval")` (§7 reset scope, amended 2026-11-02). It then
    undoes every `SETUP_KEYS` entry for all eval households and applies `setup` to
    `household_id` (evals.md §4, E-A8). It refuses any other `env`.
*   `inject_event(...)`: writes a `mock_events` row and returns its id. A `shortfall`
    event needs `params = {order_id, skus}`, with 1 to `SHORTFALL_MAX_LINES` SKUs, each a
    line of the order (agent-protocol §10). It is refused when the order's run pinned the
    seller's vendor key to `real` (adapters §8).
*   `set_mode(...)`: writes the control-panel override in `adapter_modes`.
*   `expire_approval(...)`: makes the approval's pending `approval_expiry` timer due now,
    so the sweeper expires it.

The route checks that belong to `/panel/events` (the order is `courier_booked` and has no
shortfall, then `shortfall_detected`) are agent-protocol P31 and live in the route.
"""

import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from services.common import directory, dislikes, ids, pantry, vendors
from services.common.config import get_settings
from services.common.modes import ALWAYS_MOCK, VENDORS
from services.seed import household_envs, mock_stock_rows, seeded_limits

__all__ = [
    "DEFAULT_EVAL_HOUSEHOLD",
    "EVENT_KINDS",
    "SETUP_KEYS",
    "ControlRefused",
    "SetupKey",
    "expire_approval",
    "inject_event",
    "reset",
    "set_mode",
]

ENVS = frozenset({"live", "eval"})
EVENT_KINDS = frozenset(
    {"out_of_stock", "kitchen_overloaded", "courier_delayed", "adapter_down", "shortfall"}
)
MODES = frozenset({"real", "mock"})


class ControlRefused(ValueError):
    """The control request is invalid or not allowed. Nothing was written."""


# --- reset ----------------------------------------------------------------------------------

_EVAL_RUNS_SQL = """
SELECT r.run_id FROM runs r JOIN households h ON h.household_id = r.household_id
WHERE h.env = 'eval'
"""

# Children first (§7, amended 2026-11-02). %(runs)s is the eval run ids.
_RESET_SQL: tuple[tuple[str, str], ...] = (
    ("agent_messages", "DELETE FROM agent_messages WHERE run_id = ANY(%(runs)s)"),
    ("idempotency_keys", "DELETE FROM idempotency_keys WHERE run_id = ANY(%(runs)s)"),
    ("timers", "DELETE FROM timers WHERE run_id = ANY(%(runs)s)"),
    ("approvals", "DELETE FROM approvals WHERE run_id = ANY(%(runs)s)"),
    ("quotes", "DELETE FROM quotes WHERE run_id = ANY(%(runs)s)"),
    ("shortfalls", "DELETE FROM shortfalls WHERE run_id = ANY(%(runs)s)"),
    (
        "order_items",
        "DELETE FROM order_items WHERE order_id IN"
        " (SELECT order_id FROM orders WHERE run_id = ANY(%(runs)s))",
    ),
    (
        "pantry_events",
        "DELETE FROM pantry_events WHERE household_id IN"
        " (SELECT household_id FROM households WHERE env = 'eval')",
    ),
    ("orders", "DELETE FROM orders WHERE run_id = ANY(%(runs)s)"),
    # Learned dislikes and take-turns rules of eval households (agents §9, §10; AG-A12).
    (
        "member_constraints",
        "DELETE FROM member_constraints WHERE source = 'learned' AND user_id IN"
        " (SELECT hm.user_id FROM household_members hm JOIN households h"
        "  ON h.household_id = hm.household_id WHERE h.env = 'eval')",
    ),
    (
        "household_rules",
        "DELETE FROM household_rules WHERE household_id IN"
        " (SELECT household_id FROM households WHERE env = 'eval')",
    ),
    ("runs", "DELETE FROM runs WHERE run_id = ANY(%(runs)s)"),
    (
        "sim_outbound",
        "DELETE FROM sim_outbound WHERE run_id = ANY(%(runs)s) OR phone_e164 IN"
        " (SELECT ci.phone_e164 FROM channel_identities ci"
        "  JOIN household_members hm ON hm.user_id = ci.user_id"
        "  JOIN households h ON h.household_id = hm.household_id"
        "  WHERE h.env = 'eval')",
    ),
    ("mock_stock", "DELETE FROM mock_stock WHERE env = 'eval'"),
    ("mock_orders", "DELETE FROM mock_orders WHERE env = 'eval'"),
    ("mock_deliveries", "DELETE FROM mock_deliveries WHERE env = 'eval'"),
    ("mock_events", "DELETE FROM mock_events WHERE env = 'eval'"),
)


# --- setup keys (evals.md §4, E-A8) ----------------------------------------------------------

DEFAULT_EVAL_HOUSEHOLD = "us-1-eval"


@dataclass(frozen=True)
class SetupKey:
    """One eval setup key: its value type, how it is applied to one eval household, and how
    the next reset undoes it for an eval household from `services.seed.seeded_limits()`."""

    type: type
    apply: Callable[[AsyncConnection, str, Any], Awaitable[None]]
    undo: Callable[[AsyncConnection, str, Mapping[str, int | None]], Awaitable[None]]
    # Checks the value's shape before anything is written; returns it normalized or raises
    # `ControlRefused`. `None` means the type check alone is enough.
    check: Callable[[str, Any], Any] | None = None


# Every statement is limited to eval households in SQL as well as in Python.
_EVAL_ONLY = "household_id IN (SELECT household_id FROM households WHERE env = 'eval')"


async def _apply_budget(conn: AsyncConnection, household_id: str, value: int) -> None:
    cur = await conn.execute(
        "UPDATE budgets SET amount_minor = %s"
        f" WHERE household_id = %s AND period = 'weekly' AND {_EVAL_ONLY}",
        (value, household_id),
    )
    if cur.rowcount != 1:
        raise ControlRefused(f"budget_minor: {household_id} has no weekly budget")


async def _undo_budget(
    conn: AsyncConnection, household_id: str, seeded: Mapping[str, int | None]
) -> None:
    value = seeded["weekly_budget_minor"]
    if value is None:  # the seed writes no budget row, and apply never creates one
        return
    await conn.execute(
        "UPDATE budgets SET amount_minor = %s"
        f" WHERE household_id = %s AND period = 'weekly' AND {_EVAL_ONLY}",
        (value, household_id),
    )


async def _apply_per_order(conn: AsyncConnection, household_id: str, value: int) -> None:
    cur = await conn.execute(
        f"UPDATE approval_rules SET per_order_minor = %s WHERE household_id = %s AND {_EVAL_ONLY}",
        (value, household_id),
    )
    if cur.rowcount != 1:
        raise ControlRefused(f"per_order_minor: {household_id} has no approval rules")


async def _undo_per_order(
    conn: AsyncConnection, household_id: str, seeded: Mapping[str, int | None]
) -> None:
    await conn.execute(
        f"UPDATE approval_rules SET per_order_minor = %s WHERE household_id = %s AND {_EVAL_ONLY}",
        (seeded["per_order_minor"], household_id),
    )


def _check_dislikes(name: str, value: Mapping[Any, Any]) -> dict[str, list[str]]:
    """`{member id: [value]}`, each value normalized by `dislikes.check_value`."""
    checked: dict[str, list[str]] = {}
    for member_id, values in value.items():
        if not isinstance(member_id, str) or not member_id or not isinstance(values, list):
            raise ControlRefused(f"setup key {name!r} must map member ids to lists of values")
        normalized = [dislikes.check_value(v) for v in values]
        if any(v is None for v in normalized):
            raise ControlRefused(f"setup key {name!r}: invalid dislike value for {member_id}")
        checked[member_id] = [v for v in normalized if v is not None]
    return checked


async def _apply_dislikes(
    conn: AsyncConnection, household_id: str, value: Mapping[str, list[str]]
) -> None:
    for member_id, values in value.items():
        user_id = ids.seed_user_id(member_id)
        cur = await conn.execute(
            f"SELECT 1 FROM household_members WHERE user_id = %s AND household_id = %s"
            f" AND {_EVAL_ONLY}",
            (user_id, household_id),
        )
        if await cur.fetchone() is None:
            raise ControlRefused(f"dislikes: {member_id} is not a member of {household_id}")
        for v in values:
            await conn.execute(
                "INSERT INTO member_constraints (user_id, kind, value, source)"
                " VALUES (%s, 'dislike', %s, 'learned')"
                " ON CONFLICT (user_id, value) WHERE source = 'learned' DO NOTHING",
                (user_id, v),
            )


async def _undo_dislikes(
    conn: AsyncConnection, household_id: str, seeded: Mapping[str, int | None]
) -> None:
    # The reset's deletes already removed these (AG-A12); this keeps the key self-contained.
    await conn.execute(
        "DELETE FROM member_constraints WHERE source = 'learned' AND user_id IN"
        f" (SELECT user_id FROM household_members WHERE household_id = %s AND {_EVAL_ONLY})",
        (household_id,),
    )


async def _apply_take_turns(conn: AsyncConnection, household_id: str, value: bool) -> None:
    cur = await conn.execute(
        "INSERT INTO household_rules (household_id, take_turns)"
        f" SELECT household_id, %s FROM households WHERE household_id = %s AND {_EVAL_ONLY}"
        " ON CONFLICT (household_id) DO UPDATE"
        " SET take_turns = EXCLUDED.take_turns, updated_at = now()",
        (value, household_id),
    )
    if cur.rowcount != 1:
        raise ControlRefused(f"take_turns: {household_id} is not an eval household")


async def _undo_take_turns(
    conn: AsyncConnection, household_id: str, seeded: Mapping[str, int | None]
) -> None:
    # The reset's deletes already removed these (AG-A12); this keeps the key self-contained.
    await conn.execute(
        f"DELETE FROM household_rules WHERE household_id = %s AND {_EVAL_ONLY}",
        (household_id,),
    )


_PANTRY_ENTRY_KEYS = frozenset({"kind", "age_hours"})


def _check_pantry(name: str, value: Mapping[Any, Any]) -> dict[str, list[dict[str, Any]]]:
    """`{vocabulary name: [{kind, age_hours}]}` (pantry design §8). Each list is non-empty,
    each `kind` is in `pantry.KINDS`, each `age_hours` is a finite number >= 0 and does not
    increase along the list, so the last entry is both the latest and the newest event."""
    vocab = pantry.load_vocabulary()
    checked: dict[str, list[dict[str, Any]]] = {}
    for item, entries in value.items():
        if not isinstance(item, str) or item not in vocab.window_days:
            raise ControlRefused(f"setup key {name!r}: {item!r} is not a pantry vocabulary name")
        if not isinstance(entries, list) or not entries:
            raise ControlRefused(f"setup key {name!r}: {item} needs a non-empty list of events")
        previous: float | None = None
        rows: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, Mapping) or set(entry) != _PANTRY_ENTRY_KEYS:
                raise ControlRefused(
                    f"setup key {name!r}: {item} events must have exactly kind and age_hours"
                )
            kind, age = entry["kind"], entry["age_hours"]
            if not isinstance(kind, str) or kind not in pantry.KINDS:
                raise ControlRefused(f"setup key {name!r}: {item} has an unknown kind {kind!r}")
            if (
                isinstance(age, bool)
                or not isinstance(age, int | float)
                or not math.isfinite(age)
                or age < 0
            ):
                raise ControlRefused(f"setup key {name!r}: {item} age_hours must be a number >= 0")
            if previous is not None and age > previous:
                raise ControlRefused(f"setup key {name!r}: {item} age_hours must not increase")
            previous = age
            rows.append({"kind": kind, "age_hours": float(age)})
        checked[item] = rows
    return checked


async def _apply_pantry(
    conn: AsyncConnection, household_id: str, value: Mapping[str, list[dict[str, Any]]]
) -> None:
    checked = _check_pantry("pantry", value)
    for item, entries in checked.items():
        for entry in entries:
            cur = await conn.execute(
                "INSERT INTO pantry_events"
                " (household_id, item, pantry_item, kind, delta, unit, source, ts)"
                " SELECT household_id, %s, %s, %s, 0, 'each', 'eval_setup',"
                " now() - make_interval(secs => %s)"
                " FROM households WHERE household_id = %s AND env = 'eval'",
                (item, item, entry["kind"], entry["age_hours"] * 3600, household_id),
            )
            if cur.rowcount != 1:
                raise ControlRefused(f"pantry: {household_id} is not an eval household")


async def _undo_pantry(
    conn: AsyncConnection, household_id: str, seeded: Mapping[str, int | None]
) -> None:
    # The reset's deletes already removed every eval household's pantry_events (pantry §8).
    return


SETUP_KEYS: Mapping[str, SetupKey] = {
    "budget_minor": SetupKey(int, _apply_budget, _undo_budget),
    "per_order_minor": SetupKey(int, _apply_per_order, _undo_per_order),
    "dislikes": SetupKey(dict, _apply_dislikes, _undo_dislikes, _check_dislikes),
    "take_turns": SetupKey(bool, _apply_take_turns, _undo_take_turns),
    "pantry": SetupKey(dict, _apply_pantry, _undo_pantry, _check_pantry),
}


def _validate_setup(setup: Mapping[str, Any] | None, household_id: str) -> dict[str, Any]:
    if household_envs().get(household_id) != "eval":
        raise ControlRefused(f"household must be a seeded eval household, got {household_id!r}")
    if setup is None:
        return {}
    if not isinstance(setup, Mapping):
        raise ControlRefused("setup must be an object")
    checked: dict[str, Any] = {}
    for name, value in setup.items():
        key = SETUP_KEYS.get(name)
        if key is None:
            raise ControlRefused(f"unknown setup key {name!r}")
        # bool is an int subclass; a setup value of true is never an amount.
        if (isinstance(value, bool) and key.type is not bool) or not isinstance(value, key.type):
            raise ControlRefused(f"setup key {name!r} must be {key.type.__name__}")
        if key.type is int and value < 0:
            raise ControlRefused(f"setup key {name!r} must not be negative")
        checked[name] = key.check(name, value) if key.check is not None else value
    return checked


async def reset(
    conn: AsyncConnection,
    env: str,
    setup: Mapping[str, Any] | None = None,
    household_id: str = DEFAULT_EVAL_HOUSEHOLD,
) -> dict[str, int]:
    """Delete `eval` run rows and `eval` mock state, re-seed `eval` mock stock, undo every
    setup key for the eval households, then apply `setup` to `household_id`.

    One transaction. Returns the number of rows deleted per table. Raises `ControlRefused`
    unless `env == "eval"`, for a `household_id` that is not a seeded eval household, and
    for an unknown setup key or a value of the wrong type; nothing is written then.
    """
    if env != "eval":
        raise ControlRefused(f"reset is allowed for env 'eval' only, got {env!r}")
    applied = _validate_setup(setup, household_id)
    stock = mock_stock_rows("eval")
    limits = seeded_limits()
    eval_households = sorted(h for h, e in household_envs().items() if e == "eval")
    deleted: dict[str, int] = {}
    async with conn.transaction():
        cur = await conn.execute(_EVAL_RUNS_SQL)
        params = {"runs": [row[0] for row in await cur.fetchall()]}
        for table, sql in _RESET_SQL:
            cur = await conn.execute(sql, params)
            deleted[table] = cur.rowcount
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO mock_stock (env, agent_id, sku, qty) VALUES (%s, %s, %s, %s)",
                stock,
            )
        for key in SETUP_KEYS.values():
            for hid in eval_households:
                await key.undo(conn, hid, limits[hid])
        for name, value in applied.items():
            await SETUP_KEYS[name].apply(conn, household_id, value)
    return deleted


# --- inject_event ---------------------------------------------------------------------------


async def _validate_shortfall(conn: AsyncConnection, params: Mapping[str, Any]) -> dict[str, Any]:
    if set(params) != {"order_id", "skus"}:
        raise ControlRefused("shortfall params must be exactly {order_id, skus}")
    try:
        order_id = UUID(str(params["order_id"]))
    except ValueError:
        raise ControlRefused("shortfall params.order_id must be a uuid") from None
    skus = params["skus"]
    max_lines = get_settings().shortfall_max_lines
    if (
        not isinstance(skus, list)
        or not all(isinstance(s, str) and s for s in skus)
        or not 1 <= len(skus) <= max_lines
        or len(set(skus)) != len(skus)
    ):
        raise ControlRefused(f"shortfall params.skus must be 1 to {max_lines} distinct SKUs")
    cur = await conn.execute("SELECT sku FROM order_items WHERE order_id = %s", (order_id,))
    lines = {row[0] for row in await cur.fetchall()}
    missing = [s for s in skus if s not in lines]
    if missing:
        raise ControlRefused(f"shortfall SKUs are not lines of order {order_id}: {missing}")
    await _require_mock_seller(conn, order_id)
    return {"order_id": str(order_id), "skus": skus}


SHORTFALL_NEEDS_MOCK = "shortfall events need a mock-mode order"


async def _require_mock_seller(conn: AsyncConnection, order_id: UUID) -> None:
    """Refuse unless the order's run pinned the seller's vendor key to `mock` (adapters §8).

    A seller with no vendor key (a `mock` card, or a vendor with no entry, which can reach
    no real adapter) passes; an order with no run or seller, or an unknown seller card, is
    refused.
    """
    cur = await conn.execute(
        "SELECT o.seller_agent_id, r.modes FROM orders o JOIN runs r ON r.run_id = o.run_id"
        " WHERE o.order_id = %s",
        (order_id,),
    )
    row = await cur.fetchone()
    if row is None or row[0] is None:
        raise ControlRefused(f"order {order_id} has no seller")
    seller_agent_id, run_modes = row
    from services.mcp.meta import AdapterFailure

    try:
        card = await directory.card(conn, seller_agent_id)
    except directory.UnknownAgent:
        raise ControlRefused(SHORTFALL_NEEDS_MOCK) from None
    try:
        key = vendors.card_vendor_key(card.agent_class, card, "amend_order")
    except AdapterFailure:
        return
    if key is not None and (run_modes or {}).get(key) == "real":
        raise ControlRefused(SHORTFALL_NEEDS_MOCK)


async def inject_event(
    conn: AsyncConnection,
    *,
    env: str,
    kind: str,
    target_agent_id: str,
    params: Mapping[str, Any] | None = None,
) -> int:
    """Write an active `mock_events` row and return its id (agent-protocol P33).

    Raises `ControlRefused` for an unknown `env` or `kind`, invalid `shortfall` params, or a
    `shortfall` on an order whose run pinned the seller's vendor key to `real`.
    """
    if env not in ENVS:
        raise ControlRefused(f"env must be one of {sorted(ENVS)}, got {env!r}")
    if kind not in EVENT_KINDS:
        raise ControlRefused(f"kind must be one of {sorted(EVENT_KINDS)}, got {kind!r}")
    if not target_agent_id:
        raise ControlRefused("target_agent_id is required")
    body: dict[str, Any] = dict(params or {})
    if kind == "shortfall":
        body = await _validate_shortfall(conn, body)
    cur = await conn.execute(
        "INSERT INTO mock_events (env, kind, target_agent_id, params)"
        " VALUES (%s, %s, %s, %s) RETURNING id",
        (env, kind, target_agent_id, Jsonb(body)),
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


# --- set_mode and expire_approval -----------------------------------------------------------


async def set_mode(conn: AsyncConnection, vendor: str, mode: str, *, set_by: str = "panel") -> None:
    """Upsert the `adapter_modes` override for `vendor`.

    Raises `ControlRefused` for an unknown vendor or mode, or `real` for an always-mock
    vendor.
    """
    if vendor not in VENDORS:
        raise ControlRefused(f"vendor must be one of {sorted(VENDORS)}, got {vendor!r}")
    if mode not in MODES:
        raise ControlRefused(f"mode must be 'real' or 'mock', got {mode!r}")
    if vendor in ALWAYS_MOCK and mode != "mock":
        raise ControlRefused(f"{vendor} is always mock")
    await conn.execute(
        "INSERT INTO adapter_modes (vendor, mode, set_by) VALUES (%s, %s, %s)"
        " ON CONFLICT (vendor) DO UPDATE SET mode = EXCLUDED.mode,"
        " set_by = EXCLUDED.set_by, set_at = now()",
        (vendor, mode, set_by),
    )


async def expire_approval(conn: AsyncConnection, approval_id: UUID) -> None:
    """Set the approval's pending `approval_expiry` timer due now (architecture item 39).

    Raises `LookupError` if the approval has no pending expiry timer.
    """
    cur = await conn.execute(
        "UPDATE timers SET due_at = now()"
        " WHERE kind = 'approval_expiry' AND status = 'pending' AND ref->>'approval_id' = %s",
        (str(approval_id),),
    )
    if cur.rowcount == 0:
        raise LookupError(f"approval {approval_id} has no pending expiry timer")
