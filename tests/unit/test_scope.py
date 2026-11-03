"""The household query helper and its static scan (identity-authz §8 "Scoping helper", I20).

`test_scoped_tables_only_via_helper` scans every `.py` file under `services/` and `evals/`
for SQL that names a household-owned table. A file that has some must be in `ALLOWLIST`:

*   `unscoped:<reason>`: a caller §8 classifies as having no household context. The file
    must route its queries through `unscoped(conn, "<reason>")`.
*   `not moved`: a caller not yet moved to `scope.scoped` (identity-authz I39, step 12).
    This list only shrinks; by step 12 it holds no `not moved` entries.
"""

import ast
import asyncio
import re
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import AsyncConnection

from services.safety import guard, scope
from services.safety.guard import GuardRefused
from tests.unit import identity_world as w

ROOTS = ("services", "evals")
NOT_MOVED = "not moved"

ALLOWLIST: dict[str, str] = {
    # §8: callers without a household context, `unscoped(conn, reason)` from the start.
    "services/identity/grants.py": "unscoped:grants",
    "services/identity/audit.py": "unscoped:audit.record",
    "services/identity/fga.py": "unscoped:fga.reconcile",
    "services/seed/__main__.py": "unscoped:seed",
    "services/common/runs.py": "unscoped:runs",
    "services/agents/sweeper.py": "unscoped:sweeper",
    "services/api/ingest.py": "unscoped:ingest",
    "services/mocks/control.py": "unscoped:control.reset",
    # Not yet moved to `scope.scoped` (I39, step 12).
    "services/agents/a2a/demo.py": NOT_MOVED,
    "services/agents/common/seller.py": NOT_MOVED,
    "services/agents/consumer/graph.py": NOT_MOVED,
    "services/agents/delivery/handler.py": NOT_MOVED,
    "services/agents/household/graph.py": NOT_MOVED,
    "services/api/routes/delivery.py": NOT_MOVED,
    "services/api/routes/panel.py": NOT_MOVED,
    "services/api/routes/runs.py": NOT_MOVED,
    "services/api/whatsapp/sender.py": NOT_MOVED,
    "services/api/whatsapp/status.py": NOT_MOVED,
    "services/api/whatsapp/templates.py": NOT_MOVED,
    "services/common/orders.py": NOT_MOVED,
    "services/common/protocol/check.py": NOT_MOVED,
    "services/mcp/delivery/server.py": NOT_MOVED,
    "services/mcp/household/server.py": NOT_MOVED,
    "services/mcp/seller.py": NOT_MOVED,
    "services/safety/addresses.py": NOT_MOVED,
    "services/safety/limits.py": NOT_MOVED,
}

# SQL built from a table-name variable cannot be checked, so it fails the scan unless
# listed here with the tables it can name (none of them household-owned, except the seed's
# and the protocol check's, which are allowlisted above).
TABLE_VARIABLE_ALLOWLIST: dict[str, str] = {
    "services/seed/__main__.py": "_upsert over every seeded table (unscoped:seed)",
    "services/common/protocol/check.py": "row lookups by table (not moved)",
    "services/mcp/square_seller.py": "catalog_items or menu_items",
    "services/mocks/stores.py": "catalog_items or menu_items",
}

_TABLES = "|".join(sorted(scope.HOUSEHOLD_TABLES, key=len, reverse=True))
_HOUSEHOLD_SQL = re.compile(
    rf"\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+(?:ONLY\s+)?({_TABLES})\b", re.IGNORECASE
)
# A SQL keyword followed by a format placeholder: the table comes from a variable.
_TABLE_VARIABLE = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+\{")
# SQL in this codebase is written with upper-case verbs; message texts are not SQL.
_SQL_VERB = re.compile(r"\b(?:SELECT|INSERT|UPDATE|DELETE|WITH)\b")


def _py_files() -> list[Path]:
    files = [p for root in ROOTS for p in Path(root).rglob("*.py")]
    return sorted(p for p in files if p.as_posix() != "services/safety/scope.py")


def _strings(tree: ast.AST) -> list[str]:
    """Every string literal, and the literal parts of f-strings and `sql.SQL(...)` text."""
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            out.append(
                "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
            )
    return out


def household_sql(source: str) -> list[str]:
    """The household tables named by SQL in `source`."""
    return sorted(
        {
            m.group(1).lower()
            for s in _strings(ast.parse(source))
            for m in _HOUSEHOLD_SQL.finditer(s)
        }
    )


def table_variables(source: str) -> list[str]:
    """String literals in `source` whose SQL takes its table from a variable."""
    return [
        s for s in _strings(ast.parse(source)) if _SQL_VERB.search(s) and _TABLE_VARIABLE.search(s)
    ]


def unscoped_reasons(source: str) -> set[str]:
    """The literal reasons passed to `unscoped(conn, "<reason>")` in `source`."""
    reasons: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", getattr(node.func, "attr", None)) == "unscoped"
            and len(node.args) == 2
            and isinstance(node.args[1], ast.Constant)
        ):
            reasons.add(node.args[1].value)
    return reasons


def test_scoped_tables_only_via_helper() -> None:
    found = {p.as_posix(): household_sql(p.read_text()) for p in _py_files()}
    found = {path: tables for path, tables in found.items() if tables}
    unlisted = {path: tables for path, tables in found.items() if path not in ALLOWLIST}
    assert unlisted == {}, "household tables outside scope.py; use scope.scoped or unscoped"
    stale = sorted(path for path in ALLOWLIST if path not in found)
    assert stale == [], "allowlist entries with no household SQL left; remove them"
    for path, entry in ALLOWLIST.items():
        if entry.startswith("unscoped:"):
            reason = entry.removeprefix("unscoped:")
            assert reason in unscoped_reasons(Path(path).read_text()), (path, reason)
        else:
            assert entry == NOT_MOVED, (path, entry)


def test_unscoped_reasons_are_allowlisted() -> None:
    """Every `unscoped(conn, reason)` in the code names its file's allowlisted reason."""
    for path in _py_files():
        reasons = unscoped_reasons(path.read_text())
        if reasons:
            assert ALLOWLIST.get(path.as_posix()) in {f"unscoped:{r}" for r in reasons}, path
            assert len(reasons) == 1, (path, reasons)


def test_table_name_variables_refused() -> None:
    sample = 'table = "orders"\nq = f"SELECT * FROM {table} WHERE household_id = %s"\n'
    assert table_variables(sample) == ["SELECT * FROM {} WHERE household_id = %s"]
    assert table_variables('q = f"SELECT {cols} FROM orders"\n') == []
    assert household_sql('q = f"SELECT {cols} FROM orders o JOIN approvals a ON true"\n') == [
        "approvals",
        "orders",
    ]
    found = sorted(p.as_posix() for p in _py_files() if table_variables(p.read_text()))
    assert found == sorted(TABLE_VARIABLE_ALLOWLIST)


# --- the helper ------------------------------------------------------------------------------


class _Conn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def execute(self, sql: str, params: Any = None) -> Any:
        self.calls.append((sql, params))
        return None


class _Run:
    def __init__(self, household_id: str | None) -> None:
        self.household_id = household_id


def test_scoped_binds_the_household() -> None:
    conn = _Conn()
    q = scope.scoped(conn, scope.from_ctx(_Run("h-1")))  # type: ignore[arg-type]
    asyncio.run(
        q.execute("SELECT 1 FROM orders WHERE household_id = %(household)s AND x = %(x)s", x=2)
    )
    assert conn.calls == [
        (
            "SELECT 1 FROM orders WHERE household_id = %(household)s AND x = %(x)s",
            {"x": 2, "household": "h-1"},
        )
    ]


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        ("SELECT 1 FROM orders", {}),  # no household placeholder
        ("SELECT 1 FROM orders WHERE household_id = %(household)s AND x = %s", {}),
        ("SELECT 1 FROM orders WHERE household_id = %(household)s", {"household": "h-2"}),
    ],
)
def test_scoped_refuses_unbound_sql(sql: str, params: dict[str, Any]) -> None:
    q = scope.scoped(_Conn(), scope.from_ctx(_Run("h-1")))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        asyncio.run(q.execute(sql, **params))


def test_scope_needs_a_household() -> None:
    with pytest.raises(scope.NoHousehold):
        scope.from_ctx(_Run(None))
    with pytest.raises(scope.NoHousehold):
        scope.from_session(_Run(""))
    assert scope.from_session(_Run("h-1")) == scope.HouseholdScope("h-1", "session")
    with pytest.raises(TypeError):
        scope.scoped(_Conn(), "h-1")  # type: ignore[arg-type]


def test_unscoped_needs_a_reason() -> None:
    with pytest.raises(ValueError):
        scope.unscoped(_Conn(), " ")


# --- the forged household argument (I-CUJ 9) --------------------------------------------------


@pytest.mark.db
def test_forged_household_id_refused(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A guarded call naming another household is refused `authz_denied` before OpenFGA,
    the limits or the adapter (identity-authz §8 step 2)."""
    with psycopg.connect(pg) as conn:
        world = w.add_world(conn, "h-1")
        w.add_world(conn, "h-2")
        run_id = w.add_run(conn, "h-1")
    token = w.people_token(world.household_agent, world.admin.keycloak_sub)
    ctx = w.MutableCtx(run_id, world.household_agent, token)
    reached: list[str] = []

    def check(*args: Any) -> tuple[bool, str]:
        reached.append("fga")
        return True, "model-1"

    async def call() -> dict[str, Any]:
        reached.append("adapter")
        return {}

    async def go(household_id: str) -> Any:
        async with await AsyncConnection.connect(pg) as _:
            pass
        from psycopg_pool import AsyncConnectionPool

        async with AsyncConnectionPool(pg, min_size=1, max_size=2, open=False) as pool:
            monkeypatch.setattr(guard.db, "get_pool", lambda: pool)
            return await guard.run_guarded(
                ctx,
                tool="household.create_order",
                relation=("acts_for", "household:h-1"),
                limits=None,
                key=None,
                request={"order_id": str(uuid.uuid4()), "household_id": household_id},
                call=call,
                apply=None,
            )

    monkeypatch.setattr(guard.fga, "check", check)
    with pytest.raises(GuardRefused) as e:
        asyncio.run(go("h-2"))
    assert e.value.reason == "authz_denied"
    assert reached == []
    assert asyncio.run(go("h-1")) == {}
    assert reached == ["fga", "adapter"]
