"""One approval, two triggers (identity-authz §9, I20, I30; build step 12, #127).

`check_approval_needed` refuses `needs_approval` when the total is above the household's
band (`approval_rules.per_order_minor`), above the remaining weekly budget, or both, unless
an `approved` approval covers the total. `request_approval` stores the limits that were
hit in `approvals.limits`, in one approval per order.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from services.common import db, ids
from services.common.money import Money
from services.identity import fga
from services.mcp.household import server as household
from services.mcp.meta import CallContext
from services.safety import limits
from services.safety.guard import GuardRefused
from tests.unit import identity_world as w

pytestmark = pytest.mark.db

HOUSEHOLD = "us-1"
AGENT = f"household:{HOUSEHOLD}"
SELLER = "grocery:mock-us"
USER = ids.seed_user_id("us-1-a")
USER_SUB = "4ec40af4-f3ed-45f8-aece-861a4bca8cdb"
ITEMS = [{"line": 1, "sku": "milk-1l", "name": "Milk 1 l", "qty": 1, "unit_minor": 12000}]


@pytest.fixture(autouse=True)
def _auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fga, "check", lambda user, relation, object: (True, "model-1"))


def _exec(pg: str, sql: str, *params: Any) -> None:
    with psycopg.connect(pg) as conn:
        conn.execute(sql, params)


def _query(pg: str, sql: str, *params: Any) -> list[dict[str, Any]]:
    with psycopg.connect(pg) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _seed(pg: str, *, band: int | None, budget: int | None) -> uuid.UUID:
    """One household with its approver, `band` and weekly `budget` (none when `None`), the
    cards, the household agent's grant and a run."""
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO households (household_id, name, region, zone_id, timezone, currency)"
            " VALUES (%s, 'Test', 'us', 'us-oh-45202', 'America/New_York', 'USD')",
            (HOUSEHOLD,),
        )
        conn.execute(
            "INSERT INTO users (user_id, display_name, keycloak_sub) VALUES (%s, 'Asha', %s)",
            (USER, USER_SUB),
        )
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
        if budget is not None:
            conn.execute(
                "INSERT INTO budgets (household_id, period, amount_minor, currency)"
                " VALUES (%s, 'weekly', %s, 'USD')",
                (HOUSEHOLD, budget),
            )
        conn.execute(
            "INSERT INTO approval_rules (household_id, approver_user_id, per_order_minor)"
            " VALUES (%s, %s, %s)",
            (HOUSEHOLD, USER, band),
        )
        for agent_id, cls, owner, process in (
            (AGENT, "household", AGENT, "agents"),
            (SELLER, "grocery", "org:mock", "agents"),
            (household.WHATSAPP, "channel", "system", "api"),
        ):
            conn.execute(
                "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
                " VALUES (%s, %s, %s, 'us', %s)",
                (agent_id, cls, owner, process),
            )
        w.add_agent(conn, AGENT, AGENT)
        w.add_grant(conn, AGENT, w.Person(USER, USER_SUB), HOUSEHOLD)
        run_id = uuid.uuid4()
        overrides = {household.APPROVAL_TTL_OVERRIDE: 600}
        conn.execute(
            "INSERT INTO runs (run_id, household_id, kind, trigger, modes)"
            " VALUES (%s, %s, 'weekly', %s, '{}')",
            (run_id, HOUSEHOLD, Jsonb({"user_id": str(USER), "overrides": overrides})),
        )
    return run_id


def _order(pg: str, run_id: uuid.UUID) -> uuid.UUID:
    order_id = uuid.uuid4()
    _exec(
        pg,
        "INSERT INTO orders (order_id, run_id, household_id, kind, state, currency, address_ref)"
        " VALUES (%s, %s, %s, 'grocery', 'negotiating', 'USD', %s)",
        order_id,
        run_id,
        HOUSEHOLD,
        ids.seed_address_ref(HOUSEHOLD),
    )
    return order_id


def _quote(pg: str, run_id: uuid.UUID, order_id: uuid.UUID, total: int) -> uuid.UUID:
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
        SELLER,
        AGENT,
        total,
    )
    return quote_id


def _approved(pg: str, run_id: uuid.UUID, order_id: uuid.UUID, amount: int) -> None:
    _exec(
        pg,
        "INSERT INTO approvals (approval_id, run_id, household_id, order_id, amount_minor,"
        " currency, status, limits) VALUES (%s, %s, %s, %s, %s, 'USD', 'approved', %s)",
        ids.approval_id(order_id),
        run_id,
        HOUSEHOLD,
        order_id,
        amount,
        ["band"],
    )


def _ctx(run_id: uuid.UUID) -> CallContext:
    return CallContext(
        run_id=run_id,
        agent_id=AGENT,
        conversation_id=None,
        attempt=1,
        token=w.people_token(AGENT, USER_SUB),
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


def _needed(pg: str, order_id: uuid.UUID, total: int) -> list[str] | None:
    """`check_approval_needed`'s refusal: the limits it names, or `None` when it passes."""

    async def go() -> list[str] | None:
        async with await psycopg.AsyncConnection.connect(pg, autocommit=True) as conn:
            try:
                await limits.check_approval_needed(conn, HOUSEHOLD, order_id, Money(total, "USD"))
            except limits.NeedsApproval as e:
                assert e.reason == "needs_approval"
                return e.detail["limits"]
            return None

    return asyncio.run(go())


def _authorize(
    pg: str, monkeypatch: pytest.MonkeyPatch, run_id: uuid.UUID, order_id: uuid.UUID, total: int
) -> dict[str, Any]:
    quote_id = _quote(pg, run_id, order_id, total)
    items = [{**ITEMS[0], "unit_minor": total}]
    return _with_pool(
        pg,
        monkeypatch,
        lambda: household.authorize_spend(
            _ctx(run_id),
            order_id=order_id,
            quote_id=quote_id,
            items=items,
            total={"amount_minor": total, "currency": "USD"},
        ),
    )


def _request_approval(
    pg: str, monkeypatch: pytest.MonkeyPatch, run_id: uuid.UUID, order_id: uuid.UUID, total: int
) -> dict[str, Any]:
    return _with_pool(
        pg,
        monkeypatch,
        lambda: household.request_approval(
            _ctx(run_id), order_id=order_id, amount={"amount_minor": total, "currency": "USD"}
        ),
    )


def test_over_band_needs_approval(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # I-CUJ 5: a $100 band, a $120 offer inside a $1,000 weekly budget.
    run_id = _seed(pg, band=10_000, budget=100_000)
    order_id = _order(pg, run_id)
    assert _needed(pg, order_id, 12_000) == ["band"]
    assert _needed(pg, order_id, 10_000) is None  # at the band is not above it

    with pytest.raises(GuardRefused) as e:
        _authorize(pg, monkeypatch, run_id, order_id, 12_000)
    assert e.value.reason == "needs_approval"
    [order] = _query(pg, "SELECT state FROM orders WHERE order_id = %s", order_id)
    assert order["state"] == "negotiating"

    # The agent asks once; the approval names the band, and the approver is asked.
    result = _request_approval(pg, monkeypatch, run_id, order_id, 12_000)
    [approval] = _query(pg, "SELECT approval_id, status, limits, amount_minor FROM approvals")
    assert str(approval["approval_id"]) == result["approval_id"]
    assert (approval["status"], approval["limits"], approval["amount_minor"]) == (
        "pending",
        ["band"],
        12_000,
    )


def test_band_and_budget_one_prompt(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Over the $100 band and the $110 budget: one approval naming both, one prompt.
    run_id = _seed(pg, band=10_000, budget=11_000)
    order_id = _order(pg, run_id)
    assert _needed(pg, order_id, 12_000) == ["band", "budget"]

    first = _request_approval(pg, monkeypatch, run_id, order_id, 12_000)
    # A retried request is the same approval (its idempotency key), not a second prompt.
    assert _request_approval(pg, monkeypatch, run_id, order_id, 12_000) == first
    [approval] = _query(pg, "SELECT limits FROM approvals WHERE order_id = %s", order_id)
    assert approval["limits"] == ["band", "budget"]
    prompts = _query(pg, "SELECT body FROM agent_messages WHERE type = 'whatsapp_out'")
    assert len(prompts) == 1
    assert prompts[0]["body"]["ref"] == {"kind": "approval", "id": first["approval_id"]}


def test_requote_below_approved_total_passes(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed(pg, band=10_000, budget=100_000)
    order_id = _order(pg, run_id)
    _approved(pg, run_id, order_id, 12_000)
    assert _needed(pg, order_id, 12_000) is None
    assert _needed(pg, order_id, 11_500) is None  # a re-quote at or below stays covered
    assert _needed(pg, order_id, 12_001) == ["band"]  # above the approved total is not

    result = _authorize(pg, monkeypatch, run_id, order_id, 11_500)
    assert (result["state"], result["total_minor"]) == ("authorized", 11_500)


def test_no_band_no_band_limit(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # `per_order_minor` null: only the budget can send the order to approval.
    run_id = _seed(pg, band=None, budget=50_000)
    order_id = _order(pg, run_id)
    assert _needed(pg, order_id, 50_000) is None
    assert _needed(pg, order_id, 50_001) == ["budget"]
    result = _authorize(pg, monkeypatch, run_id, order_id, 45_000)
    assert result["state"] == "authorized"


def test_no_budget_spend_fails_closed_request_still_records_band(
    pg: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No weekly budget: the spend check cannot tell the budget limit, so it fails closed;
    # `request_approval` still asks, and records only the limit it can tell (the band).
    run_id = _seed(pg, band=10_000, budget=None)
    order_id = _order(pg, run_id)
    with pytest.raises(LookupError):
        _needed(pg, order_id, 12_000)
    _request_approval(pg, monkeypatch, run_id, order_id, 12_000)
    [approval] = _query(pg, "SELECT status, limits FROM approvals WHERE order_id = %s", order_id)
    assert (approval["status"], approval["limits"]) == ("pending", ["band"])
