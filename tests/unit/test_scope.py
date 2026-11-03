"""The household query helper and its static scan (identity-authz §8 "Scoping helper", I20).

`test_scoped_tables_only_via_helper` scans every `.py` file under `services/` and `evals/`
for SQL that names a household-owned table. A file that has some must be in `ALLOWLIST`:

*   `unscoped:<reason>`: a caller §8 classifies as having no household context. The file
    must route its queries through `unscoped(conn, "<reason>")`.
*   `not moved` was the entry of a caller not yet moved; since identity-authz step 12
    (I39, #127) it is refused, so every entry is `unscoped:<reason>` or `scoped`.
*   `scoped`: a file whose household SQL all goes through `scope.scoped`. Every string
    in it that names a household table must bind `%(household)s`, and it never calls
    `unscoped`.
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
SCOPED = "scoped"

ALLOWLIST: dict[str, str] = {
    # §8: callers without a household context, `unscoped(conn, reason)` from the start.
    "services/identity/grants.py": "unscoped:grants",
    "services/identity/audit.py": "unscoped:audit.record",
    "services/identity/fga.py": "unscoped:fga.reconcile",
    "services/identity/links.py": "unscoped:links",
    # Onboarding writes a new person's rows, before any household scope exists (I23).
    "services/identity/onboarding.py": "unscoped:onboarding",
    "services/seed/__main__.py": "unscoped:seed",
    "services/common/runs.py": "unscoped:runs",
    "services/agents/sweeper.py": "unscoped:sweeper",
    "services/api/ingest.py": "unscoped:ingest",
    "services/mocks/control.py": "unscoped:control.reset",
    # Every household query through `scope.scoped` with the session's scope (I24).
    "services/api/routes/me.py": SCOPED,
    # The eval runner reads one run's rows by run_id (agent-protocol P38), eval only.
    "evals/run.py": "unscoped:evals.run",
    # I39 step 12: the last callers moved off the "not moved" list. Shared helpers and
    # callers keyed by an order, run, message or person id, with no `HouseholdScope` at
    # hand, are `unscoped(conn, reason)`; the household MCP server binds every household
    # query to the run's household through `scope.scoped`.
    "services/agents/a2a/demo.py": "unscoped:a2a_demo",
    "services/agents/common/seller.py": "unscoped:seller_agent",
    "services/agents/consumer/graph.py": "unscoped:consumer_graph",
    "services/agents/delivery/handler.py": "unscoped:delivery_handler",
    "services/agents/household/graph.py": "unscoped:household_graph",
    "services/api/routes/delivery.py": "unscoped:delivery_route",
    "services/api/routes/panel.py": "unscoped:panel",
    "services/api/routes/runs.py": "unscoped:runs_route",
    "services/api/whatsapp/sender.py": "unscoped:whatsapp_sender",
    "services/api/whatsapp/status.py": "unscoped:whatsapp_status",
    "services/api/whatsapp/templates.py": "unscoped:whatsapp_templates",
    "services/common/orders.py": "unscoped:orders",
    "services/common/protocol/check.py": "unscoped:protocol_check",
    "services/mcp/delivery/server.py": "unscoped:delivery_server",
    "services/mcp/household/server.py": SCOPED,
    "services/mcp/seller.py": "unscoped:seller_server",
    "services/safety/addresses.py": "unscoped:addresses",
    "services/safety/limits.py": "unscoped:limits",
}

# EX-I46: the owner API's files. Their SQL on `ORG_TABLES` goes through `scope.org_scoped`.
ORG_FILES = ("services/api/routes/app.py", "services/api/owner_views.py")

# SQL built from a table-name variable cannot be checked, so it fails the scan unless
# listed here with the tables it can name (none of them household-owned, except the seed's
# and the protocol check's, which are allowlisted above).
TABLE_VARIABLE_ALLOWLIST: dict[str, str] = {
    "services/seed/__main__.py": "_upsert over every seeded table (unscoped:seed)",
    "services/common/protocol/check.py": "row lookups by table (unscoped:protocol_check)",
    "services/mcp/square_seller.py": "catalog_items or menu_items",
    "services/mocks/stores.py": "catalog_items or menu_items",
}

_ORG_SQL = re.compile(
    rf"\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+(?:ONLY\s+)?({'|'.join(scope.ORG_TABLES)})\b",
    re.IGNORECASE,
)
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


def household_sql(source: str, org_bound_ok: bool = False) -> list[str]:
    """The household tables named by SQL in `source`. With `org_bound_ok` (the owner API's
    files, EX-I46), SQL bound to `%(org)s` is left to `test_org_tables_only_via_helper`."""
    return sorted(
        {
            m.group(1).lower()
            for s in _strings(ast.parse(source))
            if not (org_bound_ok and scope._ORG_PLACEHOLDER in s)
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
    found = {
        p.as_posix(): household_sql(p.read_text(), p.as_posix() in ORG_FILES) for p in _py_files()
    }
    found = {path: tables for path, tables in found.items() if tables}
    unlisted = {path: tables for path, tables in found.items() if path not in ALLOWLIST}
    assert unlisted == {}, "household tables outside scope.py; use scope.scoped or unscoped"
    stale = sorted(path for path in ALLOWLIST if path not in found)
    assert stale == [], "allowlist entries with no household SQL left; remove them"
    for path, entry in ALLOWLIST.items():
        if entry.startswith("unscoped:"):
            reason = entry.removeprefix("unscoped:")
            assert reason in unscoped_reasons(Path(path).read_text()), (path, reason)
        elif entry == SCOPED:
            source = Path(path).read_text()
            assert not unscoped_reasons(source), path
            unbound = [
                s
                for s in _strings(ast.parse(source))
                if _HOUSEHOLD_SQL.search(s) and scope._HOUSEHOLD_PLACEHOLDER not in s
            ]
            assert unbound == [], (path, unbound)
        else:
            # Step 12 (#127): no `not moved` entry, or any other kind, is left.
            raise AssertionError((path, entry))


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


def org_sql(source: str) -> list[str]:
    """The string literals in `source` whose SQL names an `ORG_TABLES` table."""
    return [s for s in _strings(ast.parse(source)) if _ORG_SQL.search(s)]


def _calls(source: str, name: str) -> bool:
    return any(
        isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", None)) == name
        for node in ast.walk(ast.parse(source))
    )


def check_org_file(source: str) -> list[str]:
    """What is wrong with an owner API file's org-table SQL (empty when it is fine)."""
    found = org_sql(source)
    problems = [f"not bound to %(org)s: {s!r}" for s in found if scope._ORG_PLACEHOLDER not in s]
    if found and not _calls(source, "org_scoped"):
        problems.append("names an org table but never calls scope.org_scoped")
    if found and _calls(source, "unscoped"):
        problems.append("names an org table and calls unscoped")
    return problems


def test_org_tables_only_via_helper() -> None:
    """EX-I46: `ORG_TABLES` are read in the owner API's files only through `org_scoped`."""
    for path in ORG_FILES:
        if Path(path).exists():
            assert check_org_file(Path(path).read_text()) == [], path
    # The scan itself catches each way around the helper.
    bound = "SELECT 1 FROM quotes WHERE " + scope.ORG_SELLER_FILTER
    good = f"q = scope.org_scoped(conn, s)\nawait q.fetch({bound!r})\n"
    assert check_org_file(good) == []
    assert check_org_file('conn.execute("SELECT * FROM orders WHERE id = %s")\n')
    assert check_org_file(f"await conn.execute({bound!r})\n")
    assert check_org_file(f"q = unscoped(conn, 'x')\nscope.org_scoped(c, s)\nq({bound!r})\n")
    assert check_org_file('q = scope.org_scoped(c, s)\nq.fetch("SELECT * FROM agent_traces")\n')
    assert check_org_file('x = "SELECT agent_id FROM agent_cards"\n') == []


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


# --- the org helper (EX-I46) ------------------------------------------------------------------

ORG = scope.OrgScope("o-1")
SELLER = scope.ORG_SELLER_FILTER


def test_org_scoped_binds_the_org() -> None:
    conn = _Conn()
    q = scope.org_scoped(conn, ORG)  # type: ignore[arg-type]
    sql = f"SELECT id FROM quotes q WHERE q.{SELLER} AND q.status = %(s)s"
    asyncio.run(q.execute(sql, s="won"))
    assert conn.calls == [(sql, {"s": "won", "org": "o-1"})]
    # Whitespace in the filter may differ (multi-line SQL).
    split = "SELECT 1 FROM orders o\n WHERE o." + SELLER.replace(" FROM ", "\n    FROM ")
    asyncio.run(q.execute(split))
    # Tables with no seller column need only the org placeholder.
    asyncio.run(q.execute("SELECT mode FROM adapter_modes WHERE vendor = %(org)s"))
    assert [c[1] for c in conn.calls[1:]] == [{"org": "o-1"}, {"org": "o-1"}]


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        # Quotes or orders without the seller filter, even with %(org)s elsewhere.
        ("SELECT 1 FROM quotes WHERE %(org)s IS NOT NULL", {}),
        ("SELECT 1 FROM agent_traces t JOIN orders o ON true WHERE t.org = %(org)s", {}),
        # A different filter that only looks similar.
        (
            "SELECT 1 FROM orders WHERE seller_agent_id IN "
            "(SELECT agent_id FROM agent_cards WHERE owner_ref = %(org)s)",
            {},
        ),
        # The household binder's rules hold too.
        ("SELECT 1 FROM adapter_modes", {}),
        (f"SELECT 1 FROM quotes WHERE {SELLER} AND x = %s", {}),
        (f"SELECT 1 FROM quotes WHERE {SELLER}", {"org": "o-2"}),
        (f"SELECT 1 FROM quotes WHERE {SELLER} AND h = %(household)s", {"household": "h-1"}),
    ],
)
def test_org_scoped_requires_seller_filter(sql: str, params: dict[str, Any]) -> None:
    conn = _Conn()
    q = scope.org_scoped(conn, ORG)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        asyncio.run(q.execute(sql, **params))
    with pytest.raises(ValueError):
        asyncio.run(q.fetch(sql, **params))
    assert conn.calls == []


def test_org_scope_needs_an_org() -> None:
    with pytest.raises(TypeError):
        scope.org_scoped(_Conn(), "o-1")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        scope.org_scoped(_Conn(), scope.OrgScope(""))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        scope.org_scoped(_Conn(), scope.from_ctx(_Run("h-1")))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        scope.scoped(_Conn(), ORG)  # type: ignore[arg-type]
