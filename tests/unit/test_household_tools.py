"""The `/mcp/household` tools (architecture item 43, agent-protocol P25; issue #46)."""

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
from services.mcp.household import server as household
from services.mcp.meta import CallContext
from services.safety.guard import GuardRefused

pytestmark = pytest.mark.db

SECRET = "unit-test-mock-token-secret-32-bytes-long"
HOUSEHOLD = "us-1"
AGENT = f"household:{HOUSEHOLD}"
SELLER = "grocery:mock-us"
OTHER_SELLER = "grocery:other-us"
USER = ids.seed_user_id("us-1-a")
ITEMS = [
    {"line": 1, "sku": "milk-1l", "name": "Milk 1 l", "qty": 2, "unit_minor": 150},
    {"line": 2, "sku": "bread", "name": "Bread", "qty": 1, "unit_minor": 300},
]


@pytest.fixture(autouse=True)
def _auth(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    monkeypatch.setattr(tokens, "_secret", lambda: SECRET)
    checks: list[tuple[str, str, str]] = []

    def check(user: str, relation: str, object: str) -> tuple[bool, str]:
        checks.append((user, relation, object))
        return True, "model-1"

    monkeypatch.setattr(fga, "check", check)
    return checks


def _exec(pg: str, sql: str, *params: Any) -> None:
    with psycopg.connect(pg) as conn:
        conn.execute(sql, params)


def _query(pg: str, sql: str, *params: Any) -> list[dict[str, Any]]:
    with psycopg.connect(pg) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _seed(pg: str) -> uuid.UUID:
    """One household with a member, address, budget and approver, the cards and a run."""
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO households (household_id, name, region, zone_id, timezone, currency)"
            " VALUES (%s, 'Test', 'us', 'us-oh-45202', 'America/New_York', 'USD')",
            (HOUSEHOLD,),
        )
        conn.execute("INSERT INTO users (user_id, display_name) VALUES (%s, 'Asha')", (USER,))
        conn.execute(
            "INSERT INTO household_members (household_id, user_id, role) VALUES (%s, %s, 'admin')",
            (HOUSEHOLD, USER),
        )
        conn.execute(
            "INSERT INTO household_addresses"
            " (address_ref, household_id, line1, city, region, postal_code, country)"
            " VALUES (%s, %s, '1 Main St', 'Cincinnati', 'OH', '45202', 'US')",
            (ids.seed_address_ref(HOUSEHOLD), HOUSEHOLD),
        )
        conn.execute(
            "INSERT INTO budgets (household_id, period, amount_minor, currency)"
            " VALUES (%s, 'weekly', 100000, 'USD')",
            (HOUSEHOLD,),
        )
        conn.execute(
            "INSERT INTO approval_rules (household_id, approver_user_id) VALUES (%s, %s)",
            (HOUSEHOLD, USER),
        )
        for agent_id, cls, owner in (
            (AGENT, "household", AGENT),
            (SELLER, "grocery", "org:mock"),
            (OTHER_SELLER, "grocery", "org:other"),
        ):
            conn.execute(
                "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
                " VALUES (%s, %s, %s, 'us', 'agents')",
                (agent_id, cls, owner),
            )
        run_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO runs (run_id, household_id, kind, trigger, modes)"
            " VALUES (%s, %s, 'weekly', %s, '{}')",
            (run_id, HOUSEHOLD, Jsonb({"user_id": str(USER)})),
        )
    return run_id


def _order(pg: str, run_id: uuid.UUID, state: str) -> uuid.UUID:
    order_id = uuid.uuid4()
    _exec(
        pg,
        "INSERT INTO orders (order_id, run_id, household_id, kind, state, currency, address_ref)"
        " VALUES (%s, %s, %s, 'grocery', %s, 'USD', %s)",
        order_id,
        run_id,
        HOUSEHOLD,
        state,
        ids.seed_address_ref(HOUSEHOLD),
    )
    return order_id


def _quote(pg: str, run_id: uuid.UUID, order_id: uuid.UUID, seller: str, total: int) -> uuid.UUID:
    quote_id = uuid.uuid4()
    _exec(
        pg,
        "INSERT INTO quotes (quote_id, run_id, order_id, conversation_id, seller_agent_id,"
        " buyer_agent_id, items, total_minor, currency, expires_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, '[]', %s, 'USD', now() + interval '10 minutes')",
        quote_id,
        run_id,
        order_id,
        uuid.uuid4(),
        seller,
        AGENT,
        total,
    )
    return quote_id


def _ctx(run_id: uuid.UUID) -> CallContext:
    return CallContext(
        run_id=run_id,
        agent_id=AGENT,
        conversation_id=None,
        attempt=1,
        token=tokens.MockIssuer(secret=SECRET).issue(f"user:{USER}", AGENT, ["orders"]),
        env="live",
        modes={},
        on_behalf_of=AGENT,
    )


def _with_pool[T](pg: str, monkeypatch: pytest.MonkeyPatch, fn: Callable[[], Awaitable[T]]) -> T:
    async def go() -> T:
        async with AsyncConnectionPool(pg, min_size=1, max_size=3, open=False) as pool:
            monkeypatch.setattr(db, "get_pool", lambda: pool)
            return await fn()

    return asyncio.run(go())


def _authorize(
    pg: str,
    monkeypatch: pytest.MonkeyPatch,
    run_id: uuid.UUID,
    order_id: uuid.UUID,
    quote_id: uuid.UUID,
    total: int,
) -> dict[str, Any]:
    return _with_pool(
        pg,
        monkeypatch,
        lambda: household.authorize_spend(
            _ctx(run_id),
            order_id=order_id,
            quote_id=quote_id,
            items=ITEMS,
            total={"amount_minor": total, "currency": "USD"},
        ),
    )


def _results(pg: str, run_id: uuid.UUID, tool: str) -> list[dict[str, Any]]:
    rows = _query(
        pg,
        "SELECT payload FROM agent_traces WHERE run_id = %s AND kind = 'tool_result' ORDER BY seq",
        run_id,
    )
    return [r["payload"] for r in rows if r["payload"]["tool"] == tool]


def test_authorize_spend_key_scoped_by_quote(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed(pg)
    order_id = _order(pg, run_id, "negotiating")
    first = _quote(pg, run_id, order_id, SELLER, 600)

    result = _authorize(pg, monkeypatch, run_id, order_id, first, 600)
    assert result["quote_id"] == str(first)
    # A replay of the same call returns the stored result.
    assert _authorize(pg, monkeypatch, run_id, order_id, first, 600) == result

    # The offer expired: re-quote (authorized -> quoting -> negotiating) and authorize the
    # new quote at a different total. A key without the scope would refuse this call with
    # idempotency_conflict.
    for expected, new in (("authorized", "quoting"), ("quoting", "negotiating")):
        moved = _with_pool(
            pg,
            monkeypatch,
            lambda e=expected, n=new: household.set_order_state(
                _ctx(run_id), order_id=str(order_id), expected=e, new=n
            ),
        )
        assert moved["changed"]
    second = _quote(pg, run_id, order_id, SELLER, 580)
    result = _authorize(pg, monkeypatch, run_id, order_id, second, 580)
    assert result["quote_id"] == str(second)

    keys = _query(pg, "SELECT key, status FROM idempotency_keys ORDER BY key")
    assert sorted(k["key"] for k in keys) == sorted(
        ids.idempotency_key(order_id, "authorize", AGENT, scope=q) for q in (first, second)
    )
    assert {k["status"] for k in keys} == {"done"}
    [order] = _query(pg, "SELECT state, total_minor FROM orders WHERE order_id = %s", order_id)
    assert (order["state"], order["total_minor"]) == ("authorized", 580)


def test_authorize_spend_sets_seller(
    pg: str, monkeypatch: pytest.MonkeyPatch, _auth: list[tuple[str, str, str]]
) -> None:
    run_id = _seed(pg)
    order_id = _order(pg, run_id, "negotiating")
    other_order = _order(pg, run_id, "negotiating")
    foreign = _quote(pg, run_id, other_order, OTHER_SELLER, 600)

    # A quote of another order is refused, and nothing changes.
    with pytest.raises(GuardRefused) as e:
        _authorize(pg, monkeypatch, run_id, order_id, foreign, 600)
    assert e.value.reason == household.QUOTE_MISMATCH
    [order] = _query(pg, "SELECT state, seller_agent_id FROM orders WHERE order_id = %s", order_id)
    assert (order["state"], order["seller_agent_id"]) == ("negotiating", None)

    # A total other than the quote's is refused.
    quote_id = _quote(pg, run_id, order_id, SELLER, 600)
    with pytest.raises(GuardRefused) as e:
        _authorize(pg, monkeypatch, run_id, order_id, quote_id, 500)
    assert e.value.reason == household.QUOTE_MISMATCH

    result = _authorize(pg, monkeypatch, run_id, order_id, quote_id, 600)

    expected_hash = orders.items_hash(ITEMS)
    assert result == {
        "order_id": str(order_id),
        "quote_id": str(quote_id),
        "state": "authorized",
        "seller_agent_id": SELLER,
        "total_minor": 600,
        "currency": "USD",
        "items_hash": expected_hash,
    }
    [order] = _query(
        pg,
        "SELECT state, seller_agent_id, total_minor, items_hash FROM orders WHERE order_id = %s",
        order_id,
    )
    assert order == {
        "state": "authorized",
        "seller_agent_id": SELLER,
        "total_minor": 600,
        "items_hash": expected_hash,
    }
    lines = _query(
        pg, "SELECT line, sku FROM order_items WHERE order_id = %s ORDER BY line", order_id
    )
    assert [(r["line"], r["sku"]) for r in lines] == [(1, "milk-1l"), (2, "bread")]
    assert ("agent:" + AGENT, "can_spend", f"budget:{HOUSEHOLD}") in _auth
    refusals = [r["reason"] for r in _results(pg, run_id, "authorize_spend") if r["refused"]]
    assert refusals == [household.QUOTE_MISMATCH, household.QUOTE_MISMATCH]


def test_set_order_state_limited_states(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed(pg)
    order_id = _order(pg, run_id, "negotiating")

    def call(expected: str, new: str) -> dict[str, Any]:
        return _with_pool(
            pg,
            monkeypatch,
            lambda: household.set_order_state(
                _ctx(run_id), order_id=str(order_id), expected=expected, new=new
            ),
        )

    # States owned by other tools are refused, and the order is unchanged.
    for new in ("awaiting_approval", "authorized", "placed", "delivered", "expired", "failed"):
        with pytest.raises(GuardRefused) as e:
            call("negotiating", new)
        assert e.value.reason == household.STATE_NOT_ALLOWED
    # A target in the set but a pair not in orders.ALLOWED is refused too.
    with pytest.raises(GuardRefused) as e:
        call("delivered", "quoting")
    assert e.value.reason == household.STATE_NOT_ALLOWED
    [order] = _query(pg, "SELECT state FROM orders WHERE order_id = %s", order_id)
    assert order["state"] == "negotiating"

    # The update is conditional on `expected`.
    assert call("quoting", "cancelled") == {
        "order_id": str(order_id),
        "state": "negotiating",
        "changed": False,
    }
    assert call("negotiating", "cancelled") == {
        "order_id": str(order_id),
        "state": "cancelled",
        "changed": True,
    }

    refused = [r for r in _results(pg, run_id, "set_order_state") if r["refused"]]
    assert [r["reason"] for r in refused] == [household.STATE_NOT_ALLOWED] * 7
    assert household.SETTABLE_STATES == {"quoting", "negotiating", "cancelled"}
