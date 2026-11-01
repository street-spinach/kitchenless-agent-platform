"""`python -m services.seed [--env live|eval|all]` (architecture item 55, agent-protocol P34).

Builds the US launch roster, the eval copies and the fixtures from `data/*.yaml`. Every write
is an upsert, so running it twice gives the same rows. Business cards, catalogs, menus and
rules are shared by both `env` values and always written; `--env` picks which households and
which `mock_stock` env are written. Exits non-zero on any failure, so the deploy rolls back
(§9).

OpenFGA tuples are not written here: the model and its tuples belong to identity-authz §7
(architecture item 54, amended by I-A3), and the seed's FGA step (`fga.reconcile`) lands with
identity-authz I27.
"""

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import psycopg
import yaml
from psycopg.types.json import Jsonb

from services.common import ids

DATA_DIR = Path(__file__).parent / "data"
ENVS = ("live", "eval", "all")
CARD_VERSION_DEFAULT = "1.0.0"
MOCK_STOCK_QTY = 100
E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")
# Fictional numbers only (555-01xx). Real numbers come from the environment.
FICTIONAL_PHONE = re.compile(r"^\+1555010[0-9]{4}$")


class SeedError(RuntimeError):
    """The seed data or its environment is invalid."""


# --- Data loading and validation ---------------------------------------------------------


def _yaml(data_dir: Path, name: str) -> Any:
    with (data_dir / name).open() as f:
        return yaml.safe_load(f)


def _product_allergens(product: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    for ing in product["ingredients"]:
        found.update(ing["allergens"])
        for sub in ing["contains"]:
            found.update(sub["allergens"])
    return found


def load_data(data_dir: Path = DATA_DIR) -> dict[str, Any]:
    """Read every data file and check that they agree. Raises `SeedError` if not."""
    data = {
        "households": _yaml(data_dir, "households.yaml")["households"],
        "agents": _yaml(data_dir, "agents.yaml"),
        "rules": _yaml(data_dir, "rules.yaml"),
        "catalogs": _yaml(data_dir, "catalog_mock.yaml")["catalogs"],
        "shortfall_fixture": _yaml(data_dir, "catalog_mock.yaml")["shortfall_fixture"],
        "menus": _yaml(data_dir, "menu_mock.yaml")["menus"],
    }
    with (data_dir / "off_subset.json").open() as f:
        data["off"] = json.load(f)["products"]
    _validate(data)
    return data


def _validate(data: dict[str, Any]) -> None:
    products = {p["sku"]: p for p in data["off"]}
    # One allergen list per ingredient name: the graph shares ingredient nodes by name.
    ingredient_allergens: dict[str, list[str]] = {}
    for p in data["off"]:
        for ing in [*p["ingredients"], *(s for i in p["ingredients"] for s in i["contains"])]:
            seen = ingredient_allergens.setdefault(ing["name"], sorted(ing["allergens"]))
            if seen != sorted(ing["allergens"]):
                raise SeedError(f"ingredient {ing['name']!r} has two allergen lists")

    business = {c["agent_id"] for c in data["agents"]["business"]}
    for agent_id, items in [*data["catalogs"].items(), *data["menus"].items()]:
        if agent_id not in business:
            raise SeedError(f"items for unknown card {agent_id}")
        for item in items:
            product = products.get(item["sku"])
            if product is None:
                raise SeedError(f"{item['sku']} has no product in off_subset.json")
            if set(item["allergens"]) != _product_allergens(product):
                raise SeedError(f"{item['sku']} allergens differ from off_subset.json")
    for agent_id in data["rules"]:
        if agent_id not in business:
            raise SeedError(f"rules for unknown card {agent_id}")

    fx = data["shortfall_fixture"]
    catalog = {i["sku"]: i for i in data["catalogs"][fx["agent_id"]]}
    item = catalog[fx["sku"]]
    safe, unsafe = catalog[fx["safe_substitute"]], catalog[fx["unsafe_substitute"]]
    if not (safe["price_minor"] <= item["price_minor"] >= unsafe["price_minor"]):
        raise SeedError("shortfall substitutes must be priced at or below the item")
    if "tree_nuts" in safe["allergens"] or "tree_nuts" not in unsafe["allergens"]:
        raise SeedError("shortfall fixture needs one safe and one tree-nut substitute")

    for hh in data["households"]:
        for m in hh["members"]:
            phone = m.get("phone")
            if phone is not None and not FICTIONAL_PHONE.match(phone):
                raise SeedError(f"{m['member_id']}: repository phones must be 555-01xx")


def parse_us1_phones(raw: str | None) -> list[str] | None:
    """`SEED_US1_PHONES`: three comma-separated E.164 numbers, or empty for none."""
    if raw is None or not raw.strip():
        return None
    phones = [p.strip() for p in raw.split(",")]
    if len(phones) != 3 or not all(E164.match(p) for p in phones):
        raise SeedError("SEED_US1_PHONES must hold three comma-separated E.164 numbers")
    return phones


# --- Postgres -------------------------------------------------------------------------------


def _upsert(conn: psycopg.Connection, table: str, key: Sequence[str], row: dict[str, Any]) -> None:
    cols = list(row)
    updates = [c for c in cols if c not in key]
    action = (
        "DO UPDATE SET " + ", ".join(f"{c} = EXCLUDED.{c}" for c in updates)
        if updates
        else "DO NOTHING"
    )
    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
        f" ON CONFLICT ({', '.join(key)}) {action}",
        [Jsonb(v) if isinstance(v, dict) else v for v in row.values()],
    )


def _floor_minor(price_minor: int, pct: int) -> int:
    return -(-price_minor * pct // 100)  # rounded up, so the floor is never below pct


def _seed_business(
    conn: psycopg.Connection, data: dict[str, Any], kroger_location_id: str | None
) -> None:
    agents = data["agents"]
    for org in agents["organizations"]:
        _upsert(conn, "organizations", ["org_id"], {**org, "region": "us"})
    d = agents["business_defaults"]
    for card in agents["business"]:
        config: dict[str, Any] = {
            "description": card["description"],
            "card_version": card.get("card_version", d.get("card_version", CARD_VERSION_DEFAULT)),
        }
        if card.get("location_id_from_env"):
            config["location_id"] = kroger_location_id
        _upsert(
            conn,
            "agent_cards",
            ["agent_id"],
            {
                "agent_id": card["agent_id"],
                "class": ids.parse_agent_id(card["agent_id"])[0],
                "owner_ref": f"org:{card['owner']}",
                "region": d["region"],
                "zone_ids": d["zone_ids"],
                "currency": d["currency"],
                "host_process": "agents",
                "vendor": card["vendor"],
                "config": config,
                "enabled": True,
            },
        )
    for card in agents["system"]:
        _upsert(
            conn,
            "agent_cards",
            ["agent_id"],
            {
                "agent_id": card["agent_id"],
                "class": card["class"],
                "owner_ref": card["owner_ref"],
                "region": card["region"],
                "zone_ids": [],
                "currency": None,
                "host_process": card["host_process"],
                "vendor": None,
                "config": {},
                "enabled": True,
            },
        )

    currency = d["currency"]
    for table, groups in (("catalog_items", data["catalogs"]), ("menu_items", data["menus"])):
        for agent_id, items in groups.items():
            for item in items:
                _upsert(
                    conn,
                    table,
                    ["agent_id", "sku"],
                    {
                        "agent_id": agent_id,
                        "sku": item["sku"],
                        "name": item["name"],
                        "brand": item.get("brand"),
                        "ingredients": item.get("ingredients", ""),
                        "allergens": item["allergens"],
                        "diets": item["diets"],
                        "price_minor": item["price_minor"],
                        "currency": currency,
                        "in_stock": True,
                    },
                )

    for agent_id, rules in data["rules"].items():
        prices = _seeded_prices(data, agent_id)
        floors = {sku: _floor_minor(p, rules["floor_pct"]) for sku, p in sorted(prices.items())}
        _upsert(
            conn,
            "business_rules",
            ["agent_id", "kind"],
            {"agent_id": agent_id, "kind": "floors", "value": floors},
        )


def _seeded_prices(data: dict[str, Any], agent_id: str) -> dict[str, int]:
    items = data["catalogs"].get(agent_id, []) + data["menus"].get(agent_id, [])
    return {i["sku"]: i["price_minor"] for i in items}


def _seed_household(
    conn: psycopg.Connection, hh: dict[str, Any], us1_phones: list[str] | None
) -> None:
    hid = hh["household_id"]
    _upsert(
        conn,
        "households",
        ["household_id"],
        {
            k: hh[k]
            for k in ("household_id", "name", "env", "region", "zone_id", "timezone", "currency")
        },
    )
    addr = hh["address"]
    _upsert(
        conn,
        "household_addresses",
        ["address_ref"],
        {
            "address_ref": ids.seed_address_ref(hid),
            "household_id": hid,
            "line1": addr["line1"],
            "line2": addr.get("line2"),
            "city": addr["city"],
            "region": addr["region"],
            "postal_code": str(addr["postal_code"]),
            "country": addr["country"],
        },
    )
    if "weekly_budget_minor" in hh:
        _upsert(
            conn,
            "budgets",
            ["household_id", "period"],
            {
                "household_id": hid,
                "period": "weekly",
                "amount_minor": hh["weekly_budget_minor"],
                "currency": hh["currency"],
            },
        )
    members = hh["members"]
    if not members:
        return

    from_env = hh.get("phones_from_env")
    if from_env and us1_phones is None:
        print(f"seed: {from_env} is empty; {hid} has no phones", file=sys.stderr)

    for i, m in enumerate(members):
        user_id = ids.seed_user_id(m["member_id"])
        _upsert(
            conn,
            "users",
            ["user_id"],
            {"user_id": user_id, "display_name": m["member_id"]},
        )
        _upsert(
            conn,
            "household_members",
            ["household_id", "user_id"],
            {"household_id": hid, "user_id": user_id, "role": m["role"]},
        )
        for code in m.get("allergies", []):
            conn.execute(
                "INSERT INTO member_constraints (user_id, kind, value, source)"
                " SELECT %s, 'allergy', %s, 'seed' WHERE NOT EXISTS ("
                "  SELECT 1 FROM member_constraints"
                "  WHERE user_id = %s AND kind = 'allergy' AND value = %s)",
                (user_id, code, user_id, code),
            )
        if m.get("approver"):
            _upsert(
                conn,
                "approval_rules",
                ["household_id"],
                {"household_id": hid, "approver_user_id": user_id},
            )
        phone, mode = None, "sim"
        if from_env and us1_phones is not None:
            phone, mode = us1_phones[i], "real"
        elif m.get("phone") is not None:
            phone = m["phone"]
        if phone is not None:
            _upsert(
                conn,
                "channel_identities",
                ["phone_e164"],
                {"phone_e164": phone, "user_id": user_id, "channel_mode": mode},
            )
        _people_card(conn, f"consumer:{m['member_id']}", "consumer", f"user:{user_id}", hh)
    _people_card(conn, f"household:{hid}", "household", f"household:{hid}", hh)


def _people_card(
    conn: psycopg.Connection, agent_id: str, cls: str, owner_ref: str, hh: dict[str, Any]
) -> None:
    _upsert(
        conn,
        "agent_cards",
        ["agent_id"],
        {
            "agent_id": agent_id,
            "class": cls,
            "owner_ref": owner_ref,
            "region": hh["region"],
            "zone_ids": [hh["zone_id"]],
            "currency": hh["currency"],
            "host_process": "agents",
            "vendor": None,
            "config": {},
            "enabled": True,
        },
    )


def _seed_mock_stock(conn: psycopg.Connection, data: dict[str, Any], envs: Iterable[str]) -> None:
    for env in envs:
        for groups in (data["catalogs"], data["menus"]):
            for agent_id, items in groups.items():
                for item in items:
                    _upsert(
                        conn,
                        "mock_stock",
                        ["env", "agent_id", "sku"],
                        {
                            "env": env,
                            "agent_id": agent_id,
                            "sku": item["sku"],
                            "qty": MOCK_STOCK_QTY,
                        },
                    )


def seed(
    conn: psycopg.Connection,
    env: str = "all",
    *,
    us1_phones: list[str] | None = None,
    kroger_location_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    """Write the seed rows for `env` in one transaction on `conn`."""
    if env not in ENVS:
        raise SeedError(f"--env must be one of {ENVS}, got {env!r}")
    data = load_data() if data is None else data
    envs = ("live", "eval") if env == "all" else (env,)
    with conn.transaction():
        _seed_business(conn, data, kroger_location_id)
        for hh in data["households"]:
            if hh["env"] in envs:
                _seed_household(conn, hh, us1_phones)
        _seed_mock_stock(conn, data, envs)


# --- Neo4j ----------------------------------------------------------------------------------

_LOAD_GRAPH = """
UNWIND $products AS p
MERGE (prod:Product {sku: p.sku})
SET prod.name = p.name, prod.category = p.category
FOREACH (d IN p.diets | MERGE (dn:Diet {code: d}) MERGE (prod)-[:FITS_DIET]->(dn))
FOREACH (i IN p.ingredients |
  MERGE (ing:Ingredient {name: i.name})
  MERGE (prod)-[:CONTAINS]->(ing)
  FOREACH (a IN i.allergens | MERGE (al:Allergen {code: a}) MERGE (ing)-[:HAS_ALLERGEN]->(al))
  FOREACH (s IN i.contains |
    MERGE (si:Ingredient {name: s.name})
    MERGE (ing)-[:CONTAINS]->(si)
    FOREACH (a IN s.allergens |
      MERGE (al2:Allergen {code: a}) MERGE (si)-[:HAS_ALLERGEN]->(al2))))
"""


def load_graph(uri: str, user: str, password: str, products: list[dict[str, Any]]) -> None:
    """Rebuild the food graph from the Open Food Facts subset (architecture §7)."""
    from neo4j import GraphDatabase

    with GraphDatabase.driver(uri, auth=(user, password)) as driver:
        with driver.session() as session:

            def rebuild(tx: Any) -> None:
                tx.run("MATCH (n) DETACH DELETE n")
                tx.run(_LOAD_GRAPH, products=products)

            session.execute_write(rebuild)


# --- Entry point ----------------------------------------------------------------------------


async def _kroger_catalog_mode(dsn: str) -> str:
    from services.common.modes import resolve_modes

    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        return (await resolve_modes(conn))["kroger_catalog"]


def main(argv: Sequence[str] | None = None) -> int:
    from services.common.config import get_settings

    parser = argparse.ArgumentParser(prog="python -m services.seed")
    parser.add_argument("--env", choices=ENVS, default="all")
    args = parser.parse_args(argv)
    try:
        settings = get_settings()
        if not settings.database_url:
            raise SeedError("DATABASE_URL is not set")
        data = load_data()
        envs = ("live", "eval") if args.env == "all" else (args.env,)
        us1_phones = parse_us1_phones(settings.seed_us1_phones) if "live" in envs else None
        if asyncio.run(_kroger_catalog_mode(settings.database_url)) == "real":
            # The Kroger catalog sync needs the Kroger adapter (build step 7).
            raise SeedError("MODE_KROGER_CATALOG=real, but the Kroger catalog sync is not built")
        if not (settings.neo4j_uri and settings.neo4j_user and settings.neo4j_password):
            raise SeedError("NEO4J_URI, NEO4J_USER and NEO4J_PASSWORD are required")
        with psycopg.connect(settings.database_url) as conn:
            seed(
                conn,
                args.env,
                us1_phones=us1_phones,
                kroger_location_id=settings.kroger_location_id or None,
                data=data,
            )
        load_graph(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password, data["off"])
    except Exception as exc:  # noqa: BLE001 - any failure must exit non-zero (§9)
        print(f"seed failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"seed: done (--env {args.env})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
