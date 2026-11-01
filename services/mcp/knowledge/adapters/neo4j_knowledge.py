"""`Neo4jKnowledge`: allergens, substitutes and explanations from the food graph.

Architecture item 44 ("Knowledge: `Neo4jKnowledge`"), §5 knowledge row. The graph is
seeded from the Open Food Facts extract (item 55). Result types are provisional (§13: the
adapters design owns them) and stay minimal: ids and codes, never raw driver records.

Graph shape the queries read (provisional, until the adapters design fixes it):

    (:Product {sku, name, category})-[:CONTAINS]->(:Ingredient {name})
    (:Ingredient)-[:CONTAINS]->(:Ingredient)              sub-ingredients
    (:Product|Ingredient)-[:HAS_ALLERGEN]->(:Allergen {code})
    (:Product)-[:FITS_DIET]->(:Diet {code})

If the graph is unreachable every method raises `AdapterFailure`, so allergen checks fail
closed (§9 "Neo4j down").
"""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from neo4j import AsyncDriver, AsyncGraphDatabase, RoutingControl
from neo4j.exceptions import DriverError, Neo4jError, ServiceUnavailable, SessionExpired

from services.mcp.meta import AdapterFailure

__all__ = [
    "KNOWLEDGE_UNAVAILABLE",
    "AllergenCheck",
    "AllergenHit",
    "Explanation",
    "Knowledge",
    "Neo4jKnowledge",
    "PathStep",
    "Substitute",
]

KNOWLEDGE_UNAVAILABLE = "knowledge_unavailable"
# §9: adapter calls time out after 15 s.
TIMEOUT_SECONDS = 15.0
# How deep CONTAINS chains are followed from a product to an allergen.
MAX_DEPTH = 4
# `rank_substitutes` picks from at most 3 candidates per missing item (§4).
MAX_SUBSTITUTES = 3


@dataclass(frozen=True)
class AllergenHit:
    sku: str
    allergen_code: str


@dataclass(frozen=True)
class AllergenCheck:
    """`hits` lists every (sku, code) found. `unknown_skus` are not in the graph, so they
    could not be checked; callers must not treat them as allergen-free."""

    hits: list[AllergenHit] = field(default_factory=list)
    unknown_skus: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Substitute:
    sku: str
    name: str


@dataclass(frozen=True)
class PathStep:
    kind: str  # "product", "ingredient" or "allergen"
    name: str


@dataclass(frozen=True)
class Explanation:
    """The graph path from a product to an allergen; empty when there is none."""

    sku: str
    allergen_code: str
    path: list[PathStep] = field(default_factory=list)


class Knowledge(Protocol):
    async def check_allergens(
        self, skus: Sequence[str], allergen_codes: Sequence[str]
    ) -> AllergenCheck: ...

    async def find_substitutes(
        self, sku: str, constraints: Mapping[str, Sequence[str]]
    ) -> list[Substitute]: ...

    async def explain_path(self, sku: str, allergen_code: str) -> Explanation: ...


_CHECK = f"""
UNWIND $skus AS sku
OPTIONAL MATCH (p:Product {{sku: sku}})
OPTIONAL MATCH (p)-[:CONTAINS*0..{MAX_DEPTH}]->()-[:HAS_ALLERGEN]->(a:Allergen)
WHERE a.code IN $codes
RETURN sku, p IS NOT NULL AS known, collect(DISTINCT a.code) AS codes
"""

_SUBSTITUTES = f"""
MATCH (p:Product {{sku: $sku}})
MATCH (s:Product {{category: p.category}})
WHERE s.sku <> p.sku
  AND NOT EXISTS {{
    MATCH (s)-[:CONTAINS*0..{MAX_DEPTH}]->()-[:HAS_ALLERGEN]->(a:Allergen)
    WHERE a.code IN $allergen_codes
  }}
  AND ALL(d IN $diet_codes WHERE EXISTS {{ MATCH (s)-[:FITS_DIET]->(:Diet {{code: d}}) }})
RETURN s.sku AS sku, s.name AS name
ORDER BY s.name, s.sku
LIMIT {MAX_SUBSTITUTES}
"""

_EXPLAIN = f"""
MATCH (p:Product {{sku: $sku}}), (a:Allergen {{code: $code}})
MATCH path = shortestPath((p)-[:CONTAINS|HAS_ALLERGEN*1..{MAX_DEPTH + 1}]->(a))
RETURN [n IN nodes(path) | {{
  kind: CASE WHEN n:Product THEN 'product' WHEN n:Allergen THEN 'allergen'
             ELSE 'ingredient' END,
  name: coalesce(n.name, n.code, n.sku)
}}] AS steps
LIMIT 1
"""


class Neo4jKnowledge:
    """The `Knowledge` adapter over the Neo4j food graph."""

    def __init__(self, driver: AsyncDriver) -> None:
        self._driver = driver

    @classmethod
    def connect(cls, uri: str, user: str, password: str) -> "Neo4jKnowledge":
        return cls(AsyncGraphDatabase.driver(uri, auth=(user, password)))

    async def close(self) -> None:
        await self._driver.close()

    async def _read(self, query: str, **params: Any) -> list[Any]:
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                result = await self._driver.execute_query(
                    query, params, routing_=RoutingControl.READ
                )
        except (TimeoutError, ServiceUnavailable, SessionExpired) as e:
            raise AdapterFailure(KNOWLEDGE_UNAVAILABLE, retryable=True) from e
        except (DriverError, Neo4jError, OSError) as e:
            raise AdapterFailure(KNOWLEDGE_UNAVAILABLE, retryable=False) from e
        return list(result.records)

    async def check_allergens(
        self, skus: Sequence[str], allergen_codes: Sequence[str]
    ) -> AllergenCheck:
        if not skus:
            return AllergenCheck()
        records = await self._read(_CHECK, skus=list(skus), codes=list(allergen_codes))
        hits: list[AllergenHit] = []
        unknown: list[str] = []
        for r in records:
            if not r["known"]:
                unknown.append(r["sku"])
            hits.extend(AllergenHit(r["sku"], code) for code in sorted(r["codes"]))
        return AllergenCheck(hits=hits, unknown_skus=unknown)

    async def find_substitutes(
        self, sku: str, constraints: Mapping[str, Sequence[str]]
    ) -> list[Substitute]:
        records = await self._read(
            _SUBSTITUTES,
            sku=sku,
            allergen_codes=list(constraints.get("allergen_codes", [])),
            diet_codes=list(constraints.get("diet_codes", [])),
        )
        return [Substitute(r["sku"], r["name"]) for r in records]

    async def explain_path(self, sku: str, allergen_code: str) -> Explanation:
        records = await self._read(_EXPLAIN, sku=sku, code=allergen_code)
        steps = [PathStep(s["kind"], s["name"]) for s in records[0]["steps"]] if records else []
        return Explanation(sku=sku, allergen_code=allergen_code, path=steps)
