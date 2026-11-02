"""Catalog and menu search (architecture §5 "Search", item 45; issue #47)."""

import asyncio
from typing import Any

import psycopg
import pytest

from services.mocks import stores

pytestmark = pytest.mark.db

STORE = "grocery:mock-us"
ZONE = "us-oh-45202"
# The best text match carries peanuts, is not vegan, or costs too much; filters must drop
# them before ranking, so a limit of 1 still finds the item that passes.
ITEMS = [
    ("COOKIE-PEANUT", "Cookies cookies cookies", ["peanuts"], ["vegetarian", "vegan"], 300),
    ("COOKIE-BUTTER", "Butter cookies cookies", ["milk"], ["vegetarian"], 300),
    ("COOKIE-LUXE", "Luxe cookies cookies", [], ["vegetarian", "vegan"], 900),
    ("COOKIE-EMPTY", "Sold out cookies cookies", [], ["vegetarian", "vegan"], 300),
    ("COOKIE-OAT", "Oat cookies", [], ["vegetarian", "vegan"], 350),
    ("BANANAS", "Bananas", [], ["vegetarian", "vegan"], 129),
]


def _seed(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO agent_cards (agent_id, class, owner_ref, region, zone_ids, currency,"
            " host_process, vendor) VALUES (%s, 'grocery', 'org:mock', 'us', %s, 'USD',"
            " 'agents', 'mock')",
            (STORE, [ZONE]),
        )
        for sku, name, allergens, diets, price in ITEMS:
            conn.execute(
                "INSERT INTO catalog_items (agent_id, sku, name, allergens, diets, price_minor,"
                " currency) VALUES (%s, %s, %s, %s, %s, %s, 'USD')",
                (STORE, sku, name, allergens, diets, price),
            )
            qty = 0 if sku == "COOKIE-EMPTY" else 10
            conn.execute(
                "INSERT INTO mock_stock (env, agent_id, sku, qty) VALUES ('live', %s, %s, %s)",
                (STORE, sku, qty),
            )


def _search(pg: str, query: str, filters: dict[str, Any], limit: int) -> list[str]:
    async def go() -> list[dict[str, Any]]:
        async with await psycopg.AsyncConnection.connect(pg) as conn:
            return await stores.search(
                conn,
                table="catalog_items",
                env="live",
                agent_id=STORE,
                zone_ids=[ZONE],
                query=query,
                filters=filters,
                limit=limit,
            )

    return [item["sku"] for item in asyncio.run(go())]


def test_filters_applied_before_ranking(pg: str) -> None:
    _seed(pg)

    # Without filters the peanut cookies rank first; the sold-out item is never returned
    # and bananas do not match the query.
    ranked = _search(pg, "cookies", {}, 10)
    assert ranked[0] == "COOKIE-PEANUT"
    assert "COOKIE-EMPTY" not in ranked and "BANANAS" not in ranked

    filters = {
        "allergen_codes": ["peanuts", "milk"],
        "diet_codes": ["vegan"],
        "max_price_minor": 500,
        "zone_id": ZONE,
    }
    assert _search(pg, "cookies", filters, 1) == ["COOKIE-OAT"]
    # A zone the card does not cover returns nothing.
    assert _search(pg, "cookies", {**filters, "zone_id": "us-xx-00000"}, 10) == []
