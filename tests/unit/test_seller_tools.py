"""The grocery `place_order` tool and its guard limits (architecture §5, item 43; issue #47)."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from services.common import db, ids, orders
from services.identity import fga, tokens
from services.mcp import seller
from services.mcp.grocery import server as grocery
from services.mcp.meta import CallContext
from services.safety.guard import GuardRefused

pytestmark = pytest.mark.db

SECRET = "unit-test-mock-token-secret-32-bytes-long"
HOUSEHOLD = "us-1"
ZONE = "us-oh-45202"
SELLER = "grocery:mock-us"
OTHER_SELLER = "grocery:other-us"
COURIER = "delivery:mock-us"
FAR_COURIER = "delivery:far-us"
ORG = "org:mock-grocery-us"
ITEMS = [
    {"line": 1, "sku": "MOCK-MILK-WHOLE", "name": "Whole milk", "qty": 2, "unit_minor": 429},
    {"line": 2, "sku": "MOCK-EGGS-12", "name": "Eggs", "qty": 1, "unit_minor": 349},
]
STOCK = 10


@pytest.fixture(autouse=True)
def _auth(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    monkeypatch.setattr(tokens, "_secret", lambda: SECRET)
    checks: list[tuple[str, str, str]] = []

    def check(user: str, relation: str, object: str) -> bool:
        checks.append((user, relation, object))
        return True

    monkeypatch.setattr(fga, "check", check)
    return checks


def _query(pg: str, sql: str, *params: Any) -> list[dict[str, Any]]:
    with psycopg.connect(pg) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _seed(pg: str) -> uuid.UUID:
    """A household in ZONE, the seller and courier cards, mock stock and a run."""
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO households (household_id, name, region, zone_id, timezone, currency)"
            " VALUES (%s, 'Test', 'us', %s, 'America/New_York', 'USD')",
            (HOUSEHOLD, ZONE),
        )
        conn.execute(
            "INSERT INTO household_addresses"
            " (address_ref, household_id, line1, city, region, postal_code, country)"
            " VALUES (%s, %s, '1 Main St', 'Cincinnati', 'OH', '45202', 'US')",
            (ids.seed_address_ref(HOUSEHOLD), HOUSEHOLD),
        )
        for agent_id, cls, owner, zones in (
            (SELLER, "grocery", ORG, [ZONE]),
            (OTHER_SELLER, "grocery", "org:other", [ZONE]),
            (COURIER, "delivery", "org:mock-courier-us", [ZONE]),
            (FAR_COURIER, "delivery", "org:far", ["us-xx-00000"]),
        ):
            conn.execute(
                "INSERT INTO agent_cards (agent_id, class, owner_ref, region, zone_ids,"
                " currency, host_process, vendor) VALUES (%s, %s, %s, 'us', %s, 'USD',"
                " 'agents', 'mock')",
                (agent_id, cls, owner, zones),
            )
        for item in ITEMS:
            conn.execute(
                "INSERT INTO mock_stock (env, agent_id, sku, qty) VALUES ('live', %s, %s, %s)",
                (SELLER, item["sku"], STOCK),
            )
        run_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO runs (run_id, household_id, kind, trigger, modes)"
            " VALUES (%s, %s, 'weekly', %s, '{}')",
            (run_id, HOUSEHOLD, Jsonb({})),
        )
    return run_id


def _authorized_order(pg: str, run_id: uuid.UUID, seller_id: str) -> uuid.UUID:
    order_id = uuid.uuid4()
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO orders (order_id, run_id, household_id, kind, state, seller_agent_id,"
            " total_minor, currency, items_hash, address_ref)"
            " VALUES (%s, %s, %s, 'grocery', 'authorized', %s, %s, 'USD', %s, %s)",
            (
                order_id,
                run_id,
                HOUSEHOLD,
                seller_id,
                sum(i["qty"] * i["unit_minor"] for i in ITEMS),
                orders.items_hash(ITEMS),
                ids.seed_address_ref(HOUSEHOLD),
            ),
        )
        for i in ITEMS:
            conn.execute(
                "INSERT INTO order_items (order_id, line, sku, name, qty, unit_minor)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (order_id, i["line"], i["sku"], i["name"], i["qty"], i["unit_minor"]),
            )
    return order_id


def _ctx(run_id: uuid.UUID) -> CallContext:
    return CallContext(
        run_id=run_id,
        agent_id=SELLER,
        conversation_id=None,
        attempt=1,
        token=tokens.MockIssuer(secret=SECRET).issue(ORG, SELLER, ["orders"]),
        env="live",
        modes={},
        on_behalf_of=ORG,
    )


def _with_pool[T](pg: str, monkeypatch: pytest.MonkeyPatch, fn: Callable[[], Awaitable[T]]) -> T:
    async def go() -> T:
        async with AsyncConnectionPool(pg, min_size=1, max_size=3, open=False) as pool:
            monkeypatch.setattr(db, "get_pool", lambda: pool)
            return await fn()

    return asyncio.run(go())


def _place(
    pg: str,
    monkeypatch: pytest.MonkeyPatch,
    run_id: uuid.UUID,
    order_id: uuid.UUID,
    courier: str = COURIER,
    items_hash: str | None = None,
) -> dict[str, Any]:
    quote_id = ids.quote_id(order_id)
    return _with_pool(
        pg,
        monkeypatch,
        lambda: grocery.place_order(
            _ctx(run_id),
            order_id=order_id,
            quote_id=quote_id,
            items_hash=items_hash or orders.items_hash(ITEMS),
            delivery_agent_id=courier,
        ),
    )


def _stock(pg: str) -> list[int]:
    rows = _query(pg, "SELECT qty FROM mock_stock WHERE agent_id = %s ORDER BY sku", SELLER)
    return [r["qty"] for r in rows]


def test_place_order_limits_then_places_once(
    pg: str, monkeypatch: pytest.MonkeyPatch, _auth: list[tuple[str, str, str]]
) -> None:
    run_id = _seed(pg)
    order_id = _authorized_order(pg, run_id, SELLER)

    # The courier must cover the household's zone, and the items must be the authorized ones.
    for kwargs, reason in (
        ({"courier": FAR_COURIER}, "out_of_zone"),
        ({"courier": "delivery:nobody"}, seller.INVALID_COURIER),
        ({"courier": OTHER_SELLER}, seller.INVALID_COURIER),
        ({"items_hash": "0" * 64}, seller.ITEMS_CHANGED),
    ):
        with pytest.raises(GuardRefused) as e:
            _place(pg, monkeypatch, run_id, order_id, **kwargs)
        assert e.value.reason == reason
    [order] = _query(
        pg, "SELECT state, delivery_agent_id FROM orders WHERE order_id = %s", order_id
    )
    assert (order["state"], order["delivery_agent_id"]) == ("authorized", None)
    assert _stock(pg) == [STOCK, STOCK]
    assert _query(pg, "SELECT * FROM mock_orders") == []

    result = _place(pg, monkeypatch, run_id, order_id)

    key = ids.idempotency_key(order_id, "place_order", SELLER)
    assert result == {
        "order_id": str(order_id),
        "quote_id": str(ids.quote_id(order_id)),
        "state": "placed",
        "external_order_id": ids.vendor_key(key),
        "delivery_agent_id": COURIER,
        "total_minor": 2 * 429 + 349,
        "currency": "USD",
    }
    [order] = _query(
        pg,
        "SELECT state, delivery_agent_id, external_order_id FROM orders WHERE order_id = %s",
        order_id,
    )
    assert order == {
        "state": "placed",
        "delivery_agent_id": COURIER,
        "external_order_id": ids.vendor_key(key),
    }
    # MOCK-EGGS-12 sorts before MOCK-MILK-WHOLE.
    assert _stock(pg) == [STOCK - 1, STOCK - 2]
    assert ("agent:" + SELLER, "member_of", ORG) in _auth

    # A replay returns the stored result and places nothing more.
    assert _place(pg, monkeypatch, run_id, order_id) == result
    assert _stock(pg) == [STOCK - 1, STOCK - 2]
    assert len(_query(pg, "SELECT * FROM mock_orders")) == 1


def test_place_order_refuses_another_sellers_order(
    pg: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(pg)
    order_id = _authorized_order(pg, run_id, OTHER_SELLER)

    with pytest.raises(GuardRefused) as e:
        _place(pg, monkeypatch, run_id, order_id)
    assert e.value.reason == seller.AUTHZ_DENIED
    [order] = _query(pg, "SELECT state FROM orders WHERE order_id = %s", order_id)
    assert order["state"] == "authorized"
