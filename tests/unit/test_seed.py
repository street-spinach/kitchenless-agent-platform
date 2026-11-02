"""The US seed (architecture item 55, agent-protocol P34; issue #51)."""

import asyncio
import re

import psycopg
import pytest

from services.common import directory, ids
from services.seed import __main__ as seed_mod

# Seeded tables, with the columns that vary between runs left out.
TABLES = {
    "organizations": "org_id",
    "users": "user_id",
    "households": "household_id",
    "household_members": "household_id, user_id",
    "member_constraints": "id",
    "budgets": "household_id, period",
    "approval_rules": "household_id",
    "household_addresses": "address_ref",
    "channel_identities": "phone_e164",
    "agent_cards": "agent_id",
    "business_rules": "agent_id, kind",
    "catalog_items": "agent_id, sku",
    "menu_items": "agent_id, sku",
    "mock_stock": "env, agent_id, sku",
}
VARYING = {"created_at", "updated_at", "search"}
# Fictional, so the test never needs a real number.
US1_PHONES = ["+15550109901", "+15550109902", "+15550109903"]


def _snapshot(conn: psycopg.Connection) -> dict[str, list[tuple]]:
    snap = {}
    for table, order in TABLES.items():
        cols = [
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = %s AND table_schema = current_schema()"
                " ORDER BY ordinal_position",
                (table,),
            )
            if r[0] not in VARYING
        ]
        snap[table] = conn.execute(
            f"SELECT {', '.join(cols)} FROM {table} ORDER BY {order}"
        ).fetchall()
    return snap


def _seed(conn: psycopg.Connection) -> None:
    seed_mod.seed(conn, "all", us1_phones=US1_PHONES, kroger_location_id="01400943")


@pytest.mark.db
def test_seed_twice_same_rows(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        _seed(conn)
        first = _snapshot(conn)
        _seed(conn)
        second = _snapshot(conn)

    assert first == second
    assert all(first[t] for t in TABLES), [t for t in TABLES if not first[t]]

    with psycopg.connect(pg) as conn:
        cards = {r[0] for r in conn.execute("SELECT agent_id FROM agent_cards")}
        households = {r[0] for r in conn.execute("SELECT household_id FROM households")}
        real = conn.execute(
            "SELECT phone_e164 FROM channel_identities WHERE channel_mode = 'real'"
            " ORDER BY phone_e164"
        ).fetchall()
        child_allergy = conn.execute(
            "SELECT value FROM member_constraints WHERE user_id = %s",
            (ids.seed_user_id("us-1-c-eval"),),
        ).fetchall()
        floors = conn.execute(
            "SELECT value FROM business_rules WHERE agent_id = 'grocery:mock-us'"
            " AND kind = 'floors'"
        ).fetchone()
        kroger = conn.execute(
            "SELECT config FROM agent_cards WHERE agent_id = 'grocery:kroger-us'"
        ).fetchone()

    # The launch roster (§3): 14 live agents plus 7 eval copies, and the A2A caller.
    assert cards == {
        "household:us-1", "household:us-2",
        "consumer:us-1-a", "consumer:us-1-b", "consumer:us-1-c",
        "consumer:us-2-a", "consumer:us-2-b",
        "grocery:kroger-us", "grocery:square-us", "grocery:mock-us",
        "restaurant:square-us", "restaurant:mock-us",
        "delivery:doordash-us", "delivery:mock-us", "channel:whatsapp",
        "household:us-1-eval", "household:us-2-eval",
        "consumer:us-1-a-eval", "consumer:us-1-b-eval", "consumer:us-1-c-eval",
        "consumer:us-2-a-eval", "consumer:us-2-b-eval",
        "external:a2a-demo",
    }  # fmt: skip
    assert households == {"us-1", "us-2", "us-1-eval", "us-2-eval", "a2a-demo"}
    assert [r[0] for r in real] == US1_PHONES
    assert child_allergy == [("tree_nuts",)]
    # 95% of list, rounded up: 429 -> 408 (407.55).
    assert floors is not None and floors[0]["MOCK-MILK-WHOLE"] == 408
    assert kroger is not None and kroger[0]["location_id"] == "01400943"


def test_no_real_phone_in_repo_data() -> None:
    e164 = re.compile(r"\+[\d\s().-]{8,}\d")
    found = []
    for path in sorted(seed_mod.DATA_DIR.iterdir()):
        for match in e164.findall(path.read_text()):
            phone = "+" + re.sub(r"\D", "", match)
            found.append(phone)
            assert seed_mod.FICTIONAL_PHONE.match(phone), f"{path.name}: {match!r}"
    assert found  # the scan sees the simulated numbers, so it would see a real one too


@pytest.mark.db
def test_a2a_demo_card_seeded(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        seed_mod.seed(conn, "eval")
        card = conn.execute(
            "SELECT class, host_process, owner_ref FROM agent_cards"
            " WHERE agent_id = 'external:a2a-demo'"
        ).fetchone()
        household = conn.execute(
            "SELECT env, zone_id, currency FROM households WHERE household_id = 'a2a-demo'"
        ).fetchone()
        addresses = conn.execute(
            "SELECT address_ref FROM household_addresses WHERE household_id = 'a2a-demo'"
        ).fetchall()
        members = conn.execute(
            "SELECT count(*) FROM household_members WHERE household_id = 'a2a-demo'"
        ).fetchone()
        business = conn.execute(
            "SELECT agent_id, config FROM agent_cards"
            " WHERE class IN ('grocery', 'restaurant', 'delivery')"
        ).fetchall()
        prices = dict(
            conn.execute(
                "SELECT sku, price_minor FROM catalog_items WHERE agent_id = 'grocery:mock-us'"
            ).fetchall()
        )
        allergens = dict(
            conn.execute(
                "SELECT sku, allergens FROM catalog_items WHERE agent_id = 'grocery:mock-us'"
            ).fetchall()
        )

    assert card == ("external", "external", "system")
    assert household == ("eval", "us-oh-45202", "USD")
    assert addresses == [(ids.seed_address_ref("a2a-demo"),)]
    assert members == (0,)
    assert len(business) == 7
    for agent_id, config in business:
        assert config["description"], agent_id
        assert config["card_version"] == "1.0.0", agent_id

    # Shortfall fixture: one safe and one tree-nut substitute, both at or below the item.
    fx = seed_mod.load_data()["shortfall_fixture"]
    item, safe, unsafe = fx["sku"], fx["safe_substitute"], fx["unsafe_substitute"]
    assert prices[safe] <= prices[item] and prices[unsafe] <= prices[item]
    assert "tree_nuts" not in allergens[safe] and "tree_nuts" in allergens[unsafe]

    async def find() -> list[str]:
        async with await psycopg.AsyncConnection.connect(pg) as aconn:
            found = []
            for cls in ("grocery", "restaurant", "delivery", "external"):
                cards = await directory.find(aconn, cls, "us", "us-oh-45202", "USD")
                found += [c.agent_id for c in cards]
            return found

    assert "external:a2a-demo" not in asyncio.run(find())
