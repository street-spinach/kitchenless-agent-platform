"""The `/mcp/knowledge` server and `Neo4jKnowledge` adapter (architecture items 43, 44;
issue #49)."""

import asyncio
import json
import uuid
from collections.abc import Sequence
from typing import Any

import psycopg
import pytest
from neo4j import EagerResult
from neo4j.exceptions import CypherSyntaxError, ServiceUnavailable
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from services.common import db
from services.mcp.knowledge import server as knowledge_server
from services.mcp.knowledge.adapters.neo4j_knowledge import (
    KNOWLEDGE_UNAVAILABLE,
    AllergenCheck,
    AllergenHit,
    Neo4jKnowledge,
    PathStep,
    Substitute,
)
from services.mcp.meta import AdapterFailure, CallContext, tool_errors


class FakeDriver:
    """Stands in for `neo4j.AsyncDriver`: returns canned records or raises."""

    def __init__(self, records: Sequence[dict[str, Any]] = (), error: Exception | None = None):
        self.records = list(records)
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute_query(self, query: str, params: dict[str, Any], **_: Any) -> EagerResult:
        self.calls.append((query, params))
        if self.error is not None:
            raise self.error
        return EagerResult(self.records, None, [])  # type: ignore[arg-type]


def _adapter(driver: FakeDriver) -> Neo4jKnowledge:
    return Neo4jKnowledge(driver)  # type: ignore[arg-type]


def test_allergen_hit() -> None:
    driver = FakeDriver(
        [
            {"sku": "granola-almond", "known": True, "codes": ["tree_nuts", "gluten"]},
            {"sku": "milk-1l", "known": True, "codes": []},
        ]
    )
    check = asyncio.run(
        _adapter(driver).check_allergens(["granola-almond", "milk-1l"], ["tree_nuts", "gluten"])
    )
    assert check == AllergenCheck(
        hits=[AllergenHit("granola-almond", "gluten"), AllergenHit("granola-almond", "tree_nuts")],
        unknown_skus=[],
    )
    _, params = driver.calls[0]
    assert params == {"skus": ["granola-almond", "milk-1l"], "codes": ["tree_nuts", "gluten"]}


def test_no_allergen_hit() -> None:
    driver = FakeDriver([{"sku": "milk-1l", "known": True, "codes": []}])
    check = asyncio.run(_adapter(driver).check_allergens(["milk-1l"], ["tree_nuts"]))
    assert check == AllergenCheck(hits=[], unknown_skus=[])


def test_sku_missing_from_graph_is_unknown_not_clear() -> None:
    driver = FakeDriver([{"sku": "mystery-1", "known": False, "codes": []}])
    check = asyncio.run(_adapter(driver).check_allergens(["mystery-1"], ["tree_nuts"]))
    assert check.hits == []
    assert check.unknown_skus == ["mystery-1"]


def test_no_skus_skips_the_graph() -> None:
    driver = FakeDriver()
    assert asyncio.run(_adapter(driver).check_allergens([], ["tree_nuts"])) == AllergenCheck()
    assert driver.calls == []


@pytest.mark.parametrize(
    "method,args",
    [
        ("check_allergens", (["milk-1l"], ["tree_nuts"])),
        ("find_substitutes", ("milk-1l", {"allergen_codes": ["tree_nuts"]})),
        ("explain_path", ("milk-1l", "tree_nuts")),
    ],
)
def test_neo4j_down_raises(method: str, args: tuple[Any, ...]) -> None:
    driver = FakeDriver(error=ServiceUnavailable("connection refused"))
    with pytest.raises(AdapterFailure) as e:
        asyncio.run(getattr(_adapter(driver), method)(*args))
    assert e.value.reason == KNOWLEDGE_UNAVAILABLE
    assert e.value.retryable is True


def test_query_error_raises_not_retryable() -> None:
    driver = FakeDriver(error=CypherSyntaxError("bad query"))
    with pytest.raises(AdapterFailure) as e:
        asyncio.run(_adapter(driver).check_allergens(["milk-1l"], ["tree_nuts"]))
    assert (e.value.reason, e.value.retryable) == (KNOWLEDGE_UNAVAILABLE, False)


def test_find_substitutes_passes_constraints() -> None:
    driver = FakeDriver([{"sku": "oat-milk-1l", "name": "Oat milk 1 l"}])
    subs = asyncio.run(
        _adapter(driver).find_substitutes(
            "milk-1l", {"allergen_codes": ["milk"], "diet_codes": ["vegan"]}
        )
    )
    assert subs == [Substitute("oat-milk-1l", "Oat milk 1 l")]
    _, params = driver.calls[0]
    assert params == {"sku": "milk-1l", "allergen_codes": ["milk"], "diet_codes": ["vegan"]}


def test_explain_path() -> None:
    steps = [
        {"kind": "product", "name": "Almond granola"},
        {"kind": "ingredient", "name": "almonds"},
        {"kind": "allergen", "name": "tree_nuts"},
    ]
    driver = FakeDriver([{"steps": steps}])
    explanation = asyncio.run(_adapter(driver).explain_path("granola-almond", "tree_nuts"))
    assert explanation.path == [
        PathStep("product", "Almond granola"),
        PathStep("ingredient", "almonds"),
        PathStep("allergen", "tree_nuts"),
    ]
    assert asyncio.run(_adapter(FakeDriver()).explain_path("milk-1l", "tree_nuts")).path == []


def test_server_registers_the_three_tools() -> None:
    tools = asyncio.run(knowledge_server.server.list_tools())
    assert {t.name for t in tools} == {"check_allergens", "find_substitutes", "explain_path"}


@pytest.mark.db
def test_neo4j_down_reaches_client_as_unavailable(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = uuid.uuid4()
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, kind, status, trigger, modes)"
            " VALUES (%s, 'chat', 'running', '{}', '{}')",
            (run_id,),
        )
    ctx = CallContext(
        run_id=run_id,
        agent_id="household:us-1",
        conversation_id=None,
        attempt=1,
        token="t",
        env="live",
        modes={},
        on_behalf_of="household:us-1",
    )
    down = _adapter(FakeDriver(error=ServiceUnavailable("connection refused")))
    monkeypatch.setattr(knowledge_server, "_knowledge", down)

    async def go() -> Any:
        async with AsyncConnectionPool(pg, min_size=1, max_size=3, open=False) as pool:
            monkeypatch.setattr(db, "get_pool", lambda: pool)
            async with tool_errors(ctx, "knowledge", "check_allergens") as err:
                return await knowledge_server.check_allergens(
                    ctx, skus=["milk-1l"], allergen_codes=["tree_nuts"]
                )
            return err.result

    result = asyncio.run(go())
    assert result.is_error
    assert json.loads(result.content[0].text) == {
        "kind": "unavailable",
        "reason": KNOWLEDGE_UNAVAILABLE,
        "retryable": True,
    }
    with psycopg.connect(pg) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT kind, payload FROM agent_traces WHERE run_id = %s ORDER BY seq", (run_id,)
        )
        rows = cur.fetchall()
    assert [r["kind"] for r in rows] == ["tool_call", "error"]
    assert rows[1]["payload"]["where"] == "knowledge.check_allergens"
