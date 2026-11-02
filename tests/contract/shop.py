"""Helpers for the seller contract tests (architecture item 69, mock mode).

`seed_shop` (the `shop_db` fixture in `conftest.py`) seeds the mock store and mock
restaurant cards, their items and `live` and `eval` stock into a fresh schema, and returns
a `Shop` helper that runs coroutines with `db.get_pool()` pointing at that schema.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from services.common import db
from services.common.directory import AgentCard

ZONE = "us-oh-45202"
STORE = "grocery:mock-us"
RESTAURANT = "restaurant:mock-us"
STOCK = 100

ITEMS: dict[str, tuple[str, list[tuple[str, str, list[str], list[str], int]]]] = {
    STORE: (
        "catalog_items",
        [
            ("MOCK-MILK-WHOLE", "Whole milk 1 gal", ["milk"], ["vegetarian"], 429),
            ("MOCK-EGGS-12", "Large eggs 12 ct", ["eggs"], ["vegetarian"], 349),
            ("MOCK-BANANAS-2LB", "Bananas 2 lb", [], ["vegetarian", "vegan"], 129),
        ],
    ),
    RESTAURANT: (
        "menu_items",
        [
            ("MOCK-DIN-CHICKEN-RICE", "Grilled chicken and rice bowl", [], ["high_protein"], 1299),
            ("MOCK-DIN-VEG-PASTA", "Vegetable penne", ["gluten"], ["vegetarian", "vegan"], 1149),
        ],
    ),
}


def card(agent_id: str) -> AgentCard:
    cls = agent_id.partition(":")[0]
    return AgentCard(
        agent_id=agent_id,
        agent_class=cls,
        owner_ref=f"org:mock-{cls}-us",
        region="us",
        zone_ids=(ZONE,),
        currency="USD",
        host_process="agents",
        vendor="mock",
    )


@dataclass
class Shop:
    dsn: str
    monkeypatch: pytest.MonkeyPatch

    def run[T](self, fn: Callable[[], Awaitable[T]]) -> T:
        async def go() -> T:
            async with AsyncConnectionPool(self.dsn, min_size=1, max_size=3, open=False) as pool:
                self.monkeypatch.setattr(db, "get_pool", lambda: pool)
                return await fn()

        return asyncio.run(go())

    def query(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        with psycopg.connect(self.dsn) as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def stock(self, env: str, agent_id: str, sku: str) -> int:
        [row] = self.query(
            "SELECT qty FROM mock_stock WHERE env = %s AND agent_id = %s AND sku = %s",
            env,
            agent_id,
            sku,
        )
        return int(row["qty"])

    def event(self, env: str, kind: str, target: str, params: dict[str, Any] | None = None) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute(
                "INSERT INTO mock_events (env, kind, target_agent_id, params)"
                " VALUES (%s, %s, %s, %s)",
                (env, kind, target, Jsonb(params or {})),
            )


def seed_shop(pg: str, monkeypatch: pytest.MonkeyPatch) -> Shop:
    with psycopg.connect(pg) as conn:
        for agent_id, (table, items) in ITEMS.items():
            c = card(agent_id)
            conn.execute(
                "INSERT INTO agent_cards (agent_id, class, owner_ref, region, zone_ids,"
                " currency, host_process, vendor) VALUES (%s, %s, %s, 'us', %s, 'USD',"
                " 'agents', 'mock')",
                (agent_id, c.agent_class, c.owner_ref, list(c.zone_ids)),
            )
            for sku, name, allergens, diets, price in items:
                conn.execute(
                    f"INSERT INTO {table} (agent_id, sku, name, ingredients, allergens, diets,"
                    " price_minor, currency) VALUES (%s, %s, %s, %s, %s, %s, %s, 'USD')",
                    (agent_id, sku, name, name.lower(), allergens, diets, price),
                )
                for env in ("live", "eval"):
                    conn.execute(
                        "INSERT INTO mock_stock (env, agent_id, sku, qty) VALUES (%s, %s, %s, %s)",
                        (env, agent_id, sku, STOCK),
                    )
    return Shop(pg, monkeypatch)
