"""The `/mcp/household` tools (architecture item 43, agent-protocol P25; issue #46)."""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from services.common import db, ids, orders, pantry
from services.identity import fga
from services.mcp.household import server as household
from services.mcp.meta import CallContext
from services.safety.guard import GuardRefused
from tests.unit import identity_world as w

pytestmark = pytest.mark.db

HOUSEHOLD = "us-1"
AGENT = f"household:{HOUSEHOLD}"
SELLER = "grocery:mock-us"
OTHER_SELLER = "grocery:other-us"
USER = ids.seed_user_id("us-1-a")
# USER's Keycloak user id: the `sub` of the household and consumer agents' tokens.
USER_SUB = "4ec40af4-f3ed-45f8-aece-861a4bca8cdb"
CONSUMER = "consumer:us-1-a"
ITEMS = [
    {"line": 1, "sku": "milk-1l", "name": "Milk 1 l", "qty": 2, "unit_minor": 150},
    {"line": 2, "sku": "bread", "name": "Bread", "qty": 1, "unit_minor": 300},
]


@pytest.fixture(autouse=True)
def _auth(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
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
        _identity(conn)
        run_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO runs (run_id, household_id, kind, trigger, modes)"
            " VALUES (%s, %s, 'weekly', %s, '{}')",
            (run_id, HOUSEHOLD, Jsonb({"user_id": str(USER)})),
        )
    return run_id


def _identity(conn: psycopg.Connection) -> None:
    """The identity rows the guard checks (identity-authz §8 step 1): every caller's
    `agent_clients` row, and USER's active grants for the household and consumer agents."""
    for agent_id, owner in (
        (AGENT, AGENT),
        (CONSUMER, f"user:{USER}"),
        (SELLER, "org:mock"),
        (OTHER_SELLER, "org:other"),
    ):
        w.add_agent(conn, agent_id, owner)
    # `system:api` has no card (identity-authz §3).
    conn.execute(
        "INSERT INTO agent_clients (agent_id, client_id, kind) VALUES (%s, %s, 'service')",
        (household.API_AGENT, w.API_CLIENT),
    )
    holder = w.Person(USER, USER_SUB)
    for agent_id in (AGENT, CONSUMER):
        w.add_grant(conn, agent_id, holder, HOUSEHOLD)


def _order(pg: str, run_id: uuid.UUID, state: str, kind: str = "grocery") -> uuid.UUID:
    order_id = uuid.uuid4()
    _exec(
        pg,
        "INSERT INTO orders (order_id, run_id, household_id, kind, state, currency, address_ref)"
        " VALUES (%s, %s, %s, %s, %s, 'USD', %s)",
        order_id,
        run_id,
        HOUSEHOLD,
        kind,
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


def _ctx(run_id: uuid.UUID, env: str = "live") -> CallContext:
    return CallContext(
        run_id=run_id,
        agent_id=AGENT,
        conversation_id=None,
        attempt=1,
        token=w.people_token(AGENT, USER_SUB),
        env=env,
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
    assert (fga.agent_object(AGENT), "can_spend", f"budget:{HOUSEHOLD}") in _auth
    refusals = [r["reason"] for r in _results(pg, run_id, "authorize_spend") if r["refused"]]
    assert refusals == [household.QUOTE_MISMATCH, household.QUOTE_MISMATCH]


def test_approval_prompt_has_ref(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Experience EX-I17: the approver's prompt names the approval it is about.
    run_id = _seed(pg)
    overrides = Jsonb({household.APPROVAL_TTL_OVERRIDE: 600})
    _exec(pg, "UPDATE runs SET trigger = trigger || jsonb_build_object('overrides', %s)", overrides)
    _exec(
        pg,
        "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
        " VALUES (%s, 'channel', 'system', 'us', 'api')",
        household.WHATSAPP,
    )
    order_id = _order(pg, run_id, "negotiating")
    result = _with_pool(
        pg,
        monkeypatch,
        lambda: household.request_approval(
            _ctx(run_id), order_id=order_id, amount={"amount_minor": 7600, "currency": "USD"}
        ),
    )
    approval_id = result["approval_id"]
    [prompt] = _query(pg, "SELECT message_id, body FROM agent_messages WHERE type = 'whatsapp_out'")
    assert prompt["message_id"] == ids.approval_prompt_id(uuid.UUID(approval_id))
    assert prompt["body"]["ref"] == {"kind": "approval", "id": approval_id}


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


# --- agents tools (agents design §9, §10; file 13) ------------------------------------

OTHER_USER = ids.seed_user_id("us-1-b")


def _agent_ctx(run_id: uuid.UUID, agent: str, token: str) -> CallContext:
    return CallContext(
        run_id=run_id,
        agent_id=agent,
        conversation_id=None,
        attempt=1,
        token=token,
        env="live",
        modes={},
        on_behalf_of=agent,
    )


def _consumer_ctx(run_id: uuid.UUID) -> CallContext:
    return _agent_ctx(run_id, CONSUMER, w.people_token(CONSUMER, USER_SUB, w.CONSUMER_SCOPES))


def _api_ctx(run_id: uuid.UUID) -> CallContext:
    return _agent_ctx(run_id, household.API_AGENT, w.service_token(household.API_AGENT))


def _add_member(pg: str, user_id: uuid.UUID, name: str, role: str = "member") -> None:
    _exec(pg, "INSERT INTO users (user_id, display_name) VALUES (%s, %s)", user_id, name)
    _exec(
        pg,
        "INSERT INTO household_members (household_id, user_id, role) VALUES (%s, %s, %s)",
        HOUSEHOLD,
        user_id,
        role,
    )


def _constraint(pg: str, user_id: uuid.UUID, kind: str, value: str, source: str = "seed") -> None:
    _exec(
        pg,
        "INSERT INTO member_constraints (user_id, kind, value, source) VALUES (%s, %s, %s, %s)",
        user_id,
        kind,
        value,
        source,
    )


def _dislike(
    pg: str, monkeypatch: pytest.MonkeyPatch, run_id: uuid.UUID, value: str, action: str
) -> dict[str, Any]:
    return _with_pool(
        pg,
        monkeypatch,
        lambda: household.record_dislike(
            _consumer_ctx(run_id), user_id=USER, value=value, action=action
        ),
    )


def _dislike_rows(pg: str) -> list[tuple[str, str]]:
    rows = _query(
        pg,
        "SELECT value, source FROM member_constraints WHERE user_id = %s AND kind = 'dislike'"
        " ORDER BY value, source",
        USER,
    )
    return [(r["value"], r["source"]) for r in rows]


def test_record_dislike_add_remove(
    pg: str, monkeypatch: pytest.MonkeyPatch, _auth: list[tuple[str, str, str]]
) -> None:
    run_id = _seed(pg)

    outcomes = [
        _dislike(pg, monkeypatch, run_id, " Mushrooms ", "add")["outcome"],
        _dislike(pg, monkeypatch, run_id, "mushrooms", "add")["outcome"],
    ]
    assert outcomes == ["added", "already_present"]
    assert _dislike_rows(pg) == [("mushrooms", "learned")]

    outcomes = [
        _dislike(pg, monkeypatch, run_id, "mushrooms", "remove")["outcome"],
        _dislike(pg, monkeypatch, run_id, "mushrooms", "remove")["outcome"],
    ]
    assert outcomes == ["removed", "not_learned"]
    assert _dislike_rows(pg) == []
    assert (fga.agent_object(CONSUMER), "acts_for", f"user:{USER}") in _auth
    # The guard traced every call.
    assert len(_results(pg, run_id, "record_dislike")) == 4


def test_record_dislike_seeded_row_present(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed(pg)
    _constraint(pg, USER, "dislike", "olives")

    assert _dislike(pg, monkeypatch, run_id, "Olives", "add")["outcome"] == "already_present"
    assert _dislike_rows(pg) == [("olives", "seed")]
    # A seeded row is never deleted.
    assert _dislike(pg, monkeypatch, run_id, "olives", "remove")["outcome"] == "not_learned"
    assert _dislike_rows(pg) == [("olives", "seed")]


def test_record_dislike_other_user_denied(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed(pg)
    _add_member(pg, OTHER_USER, "Ben")
    allowed = (fga.agent_object(CONSUMER), "acts_for", f"user:{USER}")

    def check(user: str, relation: str, object: str) -> tuple[bool, str]:
        return (user, relation, object) == allowed, "model-1"

    monkeypatch.setattr(fga, "check", check)

    with pytest.raises(GuardRefused) as e:
        _with_pool(
            pg,
            monkeypatch,
            lambda: household.record_dislike(
                _consumer_ctx(run_id), user_id=OTHER_USER, value="okra", action="add"
            ),
        )
    assert e.value.reason == household.AUTHZ_DENIED
    assert _query(pg, "SELECT 1 FROM member_constraints WHERE user_id = %s", OTHER_USER) == []


def test_record_dislike_bad_shape(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed(pg)

    for value in ("5 dollars", ""):
        with pytest.raises(GuardRefused) as e:
            _dislike(pg, monkeypatch, run_id, value, "add")
        assert e.value.reason == household.INVALID_VALUE
    # An action outside add and remove fails closed the same way.
    with pytest.raises(GuardRefused) as e:
        _dislike(pg, monkeypatch, run_id, "okra", "replace")
    assert e.value.reason == household.INVALID_VALUE
    assert _dislike_rows(pg) == []
    refused = [r["reason"] for r in _results(pg, run_id, "record_dislike") if r["refused"]]
    assert refused == [household.INVALID_VALUE] * 3


def test_get_household_allergens_union(
    pg: str, monkeypatch: pytest.MonkeyPatch, _auth: list[tuple[str, str, str]]
) -> None:
    run_id = _seed(pg)
    _add_member(pg, OTHER_USER, "Ben")
    outsider = uuid.uuid4()
    _exec(pg, "INSERT INTO users (user_id, display_name) VALUES (%s, 'Out')", outsider)
    _constraint(pg, USER, "allergy", "peanuts")
    _constraint(pg, USER, "allergy", "milk")
    _constraint(pg, OTHER_USER, "allergy", "sesame")
    _constraint(pg, OTHER_USER, "allergy", "milk")
    _constraint(pg, OTHER_USER, "diet", "vegetarian")
    _constraint(pg, OTHER_USER, "dislike", "okra")
    _constraint(pg, outsider, "allergy", "fish")

    result = _with_pool(pg, monkeypatch, lambda: household.get_household_allergens(_ctx(run_id)))

    assert result == {"allergens": ["milk", "peanuts", "sesame"]}
    assert (fga.agent_object(AGENT), "acts_for", f"household:{HOUSEHOLD}") in _auth


def _join_code(pg: str, code: str, allergies: list[str], diets: list[str], days: int = 7) -> None:
    _exec(
        pg,
        "INSERT INTO join_codes (code, household_id, phone_e164, display_name, pending,"
        " created_by, expires_at)"
        " VALUES (%s, %s, '+15550100777', 'Sam', %s, %s, now() + make_interval(days => %s))",
        code,
        HOUSEHOLD,
        Jsonb({"allergies": allergies, "diets": diets}),
        USER,
        days,
    )


def _member_allergens(pg: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    async def read() -> dict[str, list[str]]:
        async with db.get_pool().connection() as conn:
            return await household._member_allergens(conn, HOUSEHOLD)

    return _with_pool(pg, monkeypatch, read)


def test_open_join_code_allergens_merged(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed(pg)
    _constraint(pg, USER, "allergy", "milk")
    _join_code(pg, "K7Q2M3XD", ["peanuts"], ["vegetarian"])
    _join_code(pg, "AAAAAAAA", ["fish"], [], days=-1)  # expired: no longer merged

    result = _with_pool(pg, monkeypatch, lambda: household.get_household_allergens(_ctx(run_id)))
    constraints = _with_pool(
        pg, monkeypatch, lambda: household.get_constraints(_ctx(run_id), user_id=USER)
    )

    assert result == {"allergens": ["milk", "peanuts"]}
    assert _member_allergens(pg, monkeypatch) == {
        str(USER): ["milk"],
        "join:K7Q2M3XD": ["peanuts"],
    }
    assert {
        "user_id": "join:K7Q2M3XD",
        "allergies": ["peanuts"],
        "diets": ["vegetarian"],
    } in constraints["members"]
    assert len(constraints["members"]) == 2


def test_used_code_not_merged_twice(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed(pg)
    _add_member(pg, OTHER_USER, "Sam")
    _join_code(pg, "K7Q2M3XD", ["peanuts"], [])
    _constraint(pg, OTHER_USER, "allergy", "peanuts", "join")
    _exec(pg, "UPDATE join_codes SET used_at = now(), used_by = %s", OTHER_USER)

    result = _with_pool(pg, monkeypatch, lambda: household.get_household_allergens(_ctx(run_id)))

    assert result == {"allergens": ["peanuts"]}
    assert _member_allergens(pg, monkeypatch) == {str(OTHER_USER): ["peanuts"]}


def test_get_household_rules_default(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed(pg)

    def rules() -> dict[str, Any]:
        return _with_pool(pg, monkeypatch, lambda: household.get_household_rules(_ctx(run_id)))

    assert rules() == {"take_turns": False, "turn_index": 0, "approver_user_id": str(USER)}
    _exec(
        pg,
        "INSERT INTO household_rules (household_id, take_turns, turn_index) VALUES (%s, true, 3)",
        HOUSEHOLD,
    )
    assert rules() == {"take_turns": True, "turn_index": 3, "approver_user_id": str(USER)}


def _advance(
    pg: str, monkeypatch: pytest.MonkeyPatch, run_id: uuid.UUID, order_id: uuid.UUID
) -> dict[str, Any]:
    return _with_pool(
        pg, monkeypatch, lambda: household.advance_turn(_ctx(run_id), order_id=order_id)
    )


def _turn(pg: str) -> dict[str, Any]:
    [row] = _query(
        pg,
        "SELECT turn_index, last_turn_order_id FROM household_rules WHERE household_id = %s",
        HOUSEHOLD,
    )
    return row


def test_advance_turn_once_per_order(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed(pg)
    first = _order(pg, run_id, "delivered", "dinner")
    second = _order(pg, run_id, "delivered", "dinner")

    # With no rule row, or take-turns off, nothing advances.
    assert _advance(pg, monkeypatch, run_id, first) == {"advanced": False}
    _exec(pg, "INSERT INTO household_rules (household_id) VALUES (%s)", HOUSEHOLD)
    assert _advance(pg, monkeypatch, run_id, first) == {"advanced": False}
    assert _turn(pg) == {"turn_index": 0, "last_turn_order_id": None}

    _exec(pg, "UPDATE household_rules SET take_turns = true WHERE household_id = %s", HOUSEHOLD)
    assert _advance(pg, monkeypatch, run_id, first) == {"advanced": True}
    assert _advance(pg, monkeypatch, run_id, first) == {"advanced": False}
    assert _turn(pg) == {"turn_index": 1, "last_turn_order_id": first}

    assert _advance(pg, monkeypatch, run_id, second) == {"advanced": True}
    assert _turn(pg) == {"turn_index": 2, "last_turn_order_id": second}


def test_advance_turn_wrong_order(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed(pg)
    _exec(
        pg,
        "INSERT INTO household_rules (household_id, take_turns) VALUES (%s, true)",
        HOUSEHOLD,
    )
    wrong = [
        _order(pg, run_id, "delivered", "grocery"),
        _order(pg, run_id, "placed", "dinner"),
        uuid.uuid4(),
    ]

    for order_id in wrong:
        with pytest.raises(GuardRefused) as e:
            _advance(pg, monkeypatch, run_id, order_id)
        assert e.value.reason == household.WRONG_ORDER_STATE
    assert _turn(pg) == {"turn_index": 0, "last_turn_order_id": None}


def _set_take_turns(
    pg: str,
    monkeypatch: pytest.MonkeyPatch,
    ctx: CallContext,
    order_id: uuid.UUID,
    on: bool,
    user_id: uuid.UUID,
) -> dict[str, Any]:
    return _with_pool(
        pg,
        monkeypatch,
        lambda: household.set_take_turns(ctx, order_id=order_id, on=on, user_id=user_id),
    )


def test_set_take_turns_approver_only(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed(pg)
    _add_member(pg, OTHER_USER, "Ben")
    order_id = _order(pg, run_id, "delivered", "dinner")

    cases = [
        (_api_ctx(run_id), order_id, OTHER_USER, "wrong_approver"),
        # A household agent's delegated token on a `svc.api` tool: §8 step 1b refuses it.
        (_ctx(run_id), order_id, USER, "scope_missing"),
        (_api_ctx(run_id), uuid.uuid4(), USER, household.AUTHZ_DENIED),
    ]
    for ctx, order, user, reason in cases:
        with pytest.raises(GuardRefused) as e:
            _set_take_turns(pg, monkeypatch, ctx, order, True, user)
        assert e.value.reason == reason
    assert _query(pg, "SELECT 1 FROM household_rules") == []


def test_set_take_turns_changed_flag(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed(pg)
    order_id = _order(pg, run_id, "delivered", "dinner")

    def tap(on: bool) -> dict[str, Any]:
        return _set_take_turns(pg, monkeypatch, _api_ctx(run_id), order_id, on, USER)

    assert tap(False) == {"changed": False, "take_turns": False}
    assert tap(True) == {"changed": True, "take_turns": True}
    assert tap(True) == {"changed": False, "take_turns": True}
    [row] = _query(
        pg, "SELECT take_turns, set_by FROM household_rules WHERE household_id = %s", HOUSEHOLD
    )
    assert row == {"take_turns": True, "set_by": USER}
    assert tap(False) == {"changed": True, "take_turns": False}
    assert len(_results(pg, run_id, "set_take_turns")) == 4


# --- Pantry (pantry design P4, PT-A2) ---

ESTIMATE_KEYS = {
    "pantry_item",
    "latest_event_id",
    "kind",
    "age_hours",
    "window_hours",
    "present",
    "trusted",
    "confidence",
}


def _catalog(pg: str, agent_id: str, sku: str, pantry_item: str | None) -> None:
    _exec(
        pg,
        "INSERT INTO catalog_items (agent_id, sku, name, price_minor, currency, pantry_item)"
        " VALUES (%s, %s, %s, 100, 'USD', %s)",
        agent_id,
        sku,
        sku,
        pantry_item,
    )


def _delivered(pg: str, run_id: uuid.UUID, lines: list[tuple[str, int]]) -> uuid.UUID:
    order_id = _order(pg, run_id, "delivered")
    _exec(pg, "UPDATE orders SET seller_agent_id = %s WHERE order_id = %s", SELLER, order_id)
    for n, (sku, qty) in enumerate(lines, start=1):
        _exec(
            pg,
            "INSERT INTO order_items (order_id, line, sku, name, qty, unit_minor)"
            " VALUES (%s, %s, %s, %s, %s, 100)",
            order_id,
            n,
            sku,
            sku,
            qty,
        )
    return order_id


def _record_delivery(
    pg: str, monkeypatch: pytest.MonkeyPatch, run_id: uuid.UUID, order_id: uuid.UUID
) -> dict[str, Any]:
    return _with_pool(
        pg, monkeypatch, lambda: household.record_delivery(_ctx(run_id), order_id=order_id)
    )


def _event(
    pg: str,
    item: str,
    pantry_item: str | None,
    kind: str = "add",
    delta: int = 0,
    age_hours: float = 1,
    source: str = "eval_setup",
) -> int:
    with psycopg.connect(pg) as conn:
        cur = conn.execute(
            "INSERT INTO pantry_events (household_id, item, pantry_item, kind, delta, unit,"
            " source, ts) VALUES (%s, %s, %s, %s, %s, 'each', %s,"
            " now() - make_interval(secs => %s)) RETURNING event_id",
            (HOUSEHOLD, item, pantry_item, kind, delta, source, age_hours * 3600),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _get_pantry(
    pg: str, monkeypatch: pytest.MonkeyPatch, run_id: uuid.UUID, env: str = "eval"
) -> dict[str, Any]:
    return _with_pool(pg, monkeypatch, lambda: household.get_pantry(_ctx(run_id, env)))


def test_record_delivery_writes_pantry_item(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed(pg)
    _catalog(pg, SELLER, "MOCK-MILK-2PCT", "milk")
    # The same SKU at another seller maps elsewhere: the join is on the order's seller.
    _catalog(pg, OTHER_SELLER, "MOCK-MILK-2PCT", "eggs")
    order_id = _delivered(pg, run_id, [("MOCK-MILK-2PCT", 2), ("MOCK-MILK-2PCT", 1)])

    assert _record_delivery(pg, monkeypatch, run_id, order_id) == {
        "order_id": str(order_id),
        "recorded": 1,
    }
    rows = _query(
        pg,
        "SELECT household_id, item, pantry_item, kind, delta, unit, source, order_id, run_id"
        " FROM pantry_events",
    )
    assert rows == [
        {
            "household_id": HOUSEHOLD,
            "item": "MOCK-MILK-2PCT",
            "pantry_item": "milk",
            "kind": "add",
            "delta": 3,
            "unit": "each",
            "source": "delivery",
            "order_id": order_id,
            "run_id": run_id,
        }
    ]
    # Once per (order_id, item).
    assert _record_delivery(pg, monkeypatch, run_id, order_id)["recorded"] == 0


def test_record_delivery_unmapped_sku_null_pantry_item(
    pg: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(pg)
    _catalog(pg, SELLER, "MOCK-SOAP", None)
    order_id = _delivered(pg, run_id, [("MOCK-SOAP", 1), ("NOT-IN-CATALOG", 1)])

    assert _record_delivery(pg, monkeypatch, run_id, order_id)["recorded"] == 2
    rows = _query(pg, "SELECT item, pantry_item, kind FROM pantry_events ORDER BY item")
    assert rows == [
        {"item": "MOCK-SOAP", "pantry_item": None, "kind": "add"},
        {"item": "NOT-IN-CATALOG", "pantry_item": None, "kind": "add"},
    ]


def test_get_pantry_items_unchanged_by_new_rows(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed(pg)
    _event(pg, "MOCK-MILK-2PCT", "milk", delta=2, source="delivery")
    _event(pg, "legacy-bread", None, delta=1, source="delivery")
    before = _get_pantry(pg, monkeypatch, run_id)["items"]
    assert before == [
        {"item": "MOCK-MILK-2PCT", "qty": 2, "unit": "each"},
        {"item": "legacy-bread", "qty": 1, "unit": "each"},
    ]

    # Setup-key-style rows: delta 0, item = pantry_item, kinds verify and out (PG8).
    _event(pg, "milk", "milk", kind="verify")
    _event(pg, "eggs", "eggs", kind="out")
    _event(pg, "rice", "rice", kind="add")
    for env in ("eval", "live"):
        assert _get_pantry(pg, monkeypatch, run_id, env)["items"] == before


def test_get_pantry_estimates_latest_event_wins(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed(pg)
    _event(pg, "milk", "milk", kind="add", age_hours=1)
    # The later event wins even though its ts is older.
    out = _event(pg, "milk", "milk", kind="out", age_hours=2)
    eggs = _event(pg, "eggs", "eggs", kind="add", age_hours=5)
    rice = _event(pg, "rice", "rice", kind="verify", age_hours=400)

    result = _get_pantry(pg, monkeypatch, run_id)
    assert result["enabled"] is True
    by_item = {e["pantry_item"]: e for e in result["estimates"]}
    assert set(by_item) == {"eggs", "milk", "rice"}
    assert by_item["milk"]["latest_event_id"] == out
    assert (by_item["milk"]["kind"], by_item["milk"]["present"]) == ("out", False)
    assert (by_item["milk"]["trusted"], by_item["milk"]["confidence"]) == (False, 0.0)
    assert by_item["eggs"]["latest_event_id"] == eggs
    assert (by_item["eggs"]["present"], by_item["eggs"]["trusted"]) == (True, True)
    assert by_item["eggs"]["window_hours"] == 7 * 24
    assert by_item["rice"]["latest_event_id"] == rice
    assert (by_item["rice"]["present"], by_item["rice"]["trusted"]) == (True, False)


def test_get_pantry_ignores_legacy_rows(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed(pg)
    _event(pg, "milk", None, delta=2, source="delivery")
    # A name the vocabulary doesn't know is left out too.
    _event(pg, "caviar", "caviar")

    result = _get_pantry(pg, monkeypatch, run_id)
    assert result["enabled"] is True
    assert result["estimates"] == []
    assert result["items"] == [{"item": "milk", "qty": 2, "unit": "each"}]


def test_get_pantry_off_for_live_household(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed(pg)
    _event(pg, "milk", "milk", kind="add", age_hours=1)

    result = _get_pantry(pg, monkeypatch, run_id, env="live")
    assert result["enabled"] is False
    assert result["estimates"] == []
    # The same row is estimated for an eval household (PG6: the gate is env alone).
    assert len(_get_pantry(pg, monkeypatch, run_id)["estimates"]) == 1


def test_get_pantry_result_shape(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed(pg)
    _event(pg, "MOCK-EGGS-12", "eggs", delta=1, age_hours=1.234567, source="delivery")

    result = _get_pantry(pg, monkeypatch, run_id)
    assert set(result) == {"household_id", "items", "enabled", "as_of", "estimates"}
    assert set(result["items"][0]) == {"item", "qty", "unit"}
    as_of = datetime.fromisoformat(result["as_of"])
    assert as_of.utcoffset() == timedelta(0)
    [est] = result["estimates"]
    assert set(est) == ESTIMATE_KEYS
    assert isinstance(est["age_hours"], float)
    assert est["age_hours"] == round(est["age_hours"], 2)
    assert 1.2 <= est["age_hours"] <= 1.3
    # The result goes through json.dumps with no default.
    assert json.loads(json.dumps(result)) == result


def test_get_pantry_vocabulary_failure_raises(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _seed(pg)

    def broken() -> pantry.Vocabulary:
        raise ValueError("bad vocabulary")

    monkeypatch.setattr(pantry, "load_vocabulary", broken)
    with pytest.raises(ValueError, match="bad vocabulary"):
        _get_pantry(pg, monkeypatch, run_id)
    # Not enabled: the vocabulary is not read, and estimates stay empty.
    assert _get_pantry(pg, monkeypatch, run_id, env="live")["estimates"] == []
