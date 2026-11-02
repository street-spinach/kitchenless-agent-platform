"""The US seed (architecture item 55, agent-protocol P34; issue #51)."""

import asyncio
import re

import psycopg
import pytest

import services.seed as seed_pkg
from services.common import directory, ids, modes, pantry, vendors
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
    seed_mod.seed(conn, "all", us1_phones=US1_PHONES)


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
        courier_rules = conn.execute(
            "SELECT agent_id, kind, value FROM business_rules"
            " WHERE agent_id LIKE 'delivery:%' ORDER BY agent_id, kind"
        ).fetchall()

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
    # The Kroger card seeds from the mock catalog: no location id (adapters §5, file 20).
    assert kroger is not None and "location_id" not in kroger[0]
    # Courier cards get a `delivery` rule and no floors (item 55, amended for #48).
    rule = {"fee_minor": 599, "slot_minutes": 60, "slot_capacity": 2}
    assert courier_rules == [
        ("delivery:doordash-us", "delivery", rule),
        ("delivery:mock-us", "delivery", rule),
    ]


def test_no_real_phone_in_repo_data() -> None:
    e164 = re.compile(r"\+[\d\s().-]{8,}\d")
    found = []
    for path in sorted(seed_mod.DATA_DIR.iterdir()):
        for match in e164.findall(path.read_text()):
            phone = "+" + re.sub(r"\D", "", match)
            found.append(phone)
            assert seed_mod.FICTIONAL_PHONE.match(phone), f"{path.name}: {match!r}"
    assert found  # the scan sees the simulated numbers, so it would see a real one too


def test_seed_fails_on_unmapped_vendor() -> None:
    data = seed_mod.load_data()
    card = next(c for c in data["agents"]["business"] if c["agent_id"] == "delivery:mock-us")
    card["vendor"] = "uber"  # no `vendors.card_vendor_key` entry

    with pytest.raises(seed_mod.SeedError, match="delivery:mock-us"):
        seed_mod._validate(data)

    # `mock` needs no entry, and every seeded vendor has one.
    card["vendor"] = vendors.MOCK_VENDOR
    seed_mod._validate(data)


def test_display_names_carry_simulated() -> None:
    agents = seed_mod.load_data()["agents"]
    names = {o["org_id"]: o["name"] for o in agents["organizations"]}
    simulated = []
    for card in agents["business"]:
        area = ids.parse_agent_id(card["agent_id"])[0]
        if card["vendor"] != vendors.MOCK_VENDOR:
            stub = directory.AgentCard(
                agent_id=card["agent_id"],
                agent_class=area,
                owner_ref=f"org:{card['owner']}",
                region="us",
                zone_ids=(),
                currency=None,
                host_process="agents",
                vendor=card["vendor"],
            )
            key = vendors.card_vendor_key(area, stub, seed_mod._KEY_CHECK_TOOL[area])
            if key not in modes.ALWAYS_MOCK:
                continue
        simulated.append(card["agent_id"])
        assert names[card["owner"]].endswith("(simulated)"), card["agent_id"]

    # Kroger and DoorDash have no real class; the three mock cards are always simulated.
    assert sorted(simulated) == [
        "delivery:doordash-us", "delivery:mock-us", "grocery:kroger-us",
        "grocery:mock-us", "restaurant:mock-us",
    ]  # fmt: skip


def test_catalog_pantry_items_in_vocabulary() -> None:
    data = seed_mod.load_data()
    vocab = pantry.load_vocabulary()
    links = {i["sku"]: i.get("pantry_item") for items in data["catalogs"].values() for i in items}
    # Pantry design P3: every catalog SKU names its vocabulary item.
    assert links == {
        "MOCK-MILK-WHOLE": "milk", "MOCK-MILK-2PCT": "milk", "SQ-MILK-WHOLE": "milk",
        "MOCK-MILK-ALMOND": "almond milk",
        "MOCK-EGGS-12": "eggs", "SQ-EGGS-12": "eggs",
        "MOCK-BREAD-WHEAT": "bread", "SQ-BREAD-WHITE": "bread",
        "MOCK-BANANAS-2LB": "bananas", "SQ-BANANAS-2LB": "bananas",
        "MOCK-APPLES-3LB": "apples",
        "MOCK-CHICKEN-BREAST": "chicken breast",
        "MOCK-RICE-2LB": "rice",
        "MOCK-GRANOLA-NUT": "granola",
        "SQ-PASTA-1LB": "pasta",
        "SQ-ALMONDS-1LB": "almonds",
    }  # fmt: skip
    assert set(links.values()) <= set(vocab.names)

    # The seed refuses a link to a name outside the vocabulary.
    data["catalogs"]["grocery:mock-us"][0]["pantry_item"] = "oat milk"
    with pytest.raises(seed_mod.SeedError, match="oat milk"):
        seed_mod._validate(data)


def test_vocabulary_rejects_duplicate_alias() -> None:
    data = seed_mod.load_data()
    entries = data["pantry_items"]
    eggs = next(e for e in entries if e["name"] == "eggs")

    eggs["aliases"] = [*eggs["aliases"], "whole milk"]  # already an alias of milk
    with pytest.raises(seed_mod.SeedError, match="whole milk"):
        seed_mod._validate(data)

    eggs["aliases"] = ["egg", "milk"]  # another item's name
    with pytest.raises(seed_mod.SeedError, match="milk"):
        seed_mod._validate(data)

    data["pantry_items"] = [*entries, {"name": "eggs", "aliases": [], "window_days": 7}]
    eggs["aliases"] = ["egg"]
    with pytest.raises(seed_mod.SeedError, match="eggs"):
        seed_mod._validate(data)


@pytest.mark.db
def test_seed_writes_catalog_pantry_item(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        seed_mod.seed(conn, "eval")
        rows = dict(conn.execute("SELECT sku, pantry_item FROM catalog_items").fetchall())
    assert rows["MOCK-MILK-2PCT"] == "milk"
    assert rows["MOCK-MILK-ALMOND"] == "almond milk"
    assert rows["SQ-PASTA-1LB"] == "pasta"
    assert None not in rows.values()


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


# --- seeded_limits and the eval allergy fixture (evals.md §4, E-A9, file 9a; issue #224) ---


def _members(env: str) -> dict[str, list[str]]:
    return {
        m["member_id"]: sorted(m.get("allergies", []))
        for hh in seed_mod.load_data()["households"]
        if hh["env"] == env
        for m in hh["members"]
    }


def test_seeded_limits_returns_the_yaml_values() -> None:
    limits = seed_pkg.seeded_limits()
    assert limits == {
        hh["household_id"]: {
            "weekly_budget_minor": hh.get("weekly_budget_minor"),
            "per_order_minor": hh.get("per_order_minor"),
        }
        for hh in seed_mod.load_data()["households"]
    }
    assert limits["us-1-eval"] == {"weekly_budget_minor": 15000, "per_order_minor": None}
    assert limits["us-2-eval"]["weekly_budget_minor"] == 15000
    assert limits["a2a-demo"] == {"weekly_budget_minor": None, "per_order_minor": None}
    assert seed_pkg.household_envs()["us-1-eval"] == "eval"
    assert seed_pkg.household_envs()["us-1"] == "live"


def test_eval_member_b_has_peanuts_and_live_members_unchanged() -> None:
    assert _members("eval")["us-1-b-eval"] == ["peanuts"]
    assert _members("live") == {
        "us-1-a": [],
        "us-1-b": [],
        "us-1-c": ["tree_nuts"],
        "us-2-a": [],
        "us-2-b": [],
    }


@pytest.mark.db
def test_seed_writes_eval_peanut_allergy_only(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        _seed(conn)
        rows = conn.execute(
            "SELECT user_id, value FROM member_constraints WHERE kind = 'allergy'"
        ).fetchall()
    by_user: dict[str, set[str]] = {}
    for user_id, value in rows:
        by_user.setdefault(user_id, set()).add(value)
    assert by_user.get(ids.seed_user_id("us-1-b-eval")) == {"peanuts"}
    assert ids.seed_user_id("us-1-b") not in by_user
    assert "peanuts" not in set().union(
        *(v for u, v in by_user.items() if u not in {ids.seed_user_id("us-1-b-eval")})
    )
