"""Shortfall amendment limits and tools (agent-protocol §10, P25-P28; #67, #68).

`authorize_amendment` and `check_amend_order` are tested against an in-memory connection,
like `test_limits.py`. The shortfall tools (household `open_shortfall`,
`choose_shortfall_option`, `authorize_amendment`; seller `amend_order`) are *db* tests.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from services.common import db, ids, orders
from services.identity import fga, tokens
from services.mcp.grocery import server as grocery
from services.mcp.household import server as household
from services.mcp.knowledge.adapters.neo4j_knowledge import AllergenCheck, AllergenHit
from services.mcp.meta import AdapterFailure, CallContext
from services.safety import limits
from services.safety.guard import GuardRefused

ORDER_ID = uuid.UUID("8a4f6c1e-0000-4000-8000-000000000067")
SHORTFALL_ID = ids.shortfall_id(ORDER_ID)
SELLER = "grocery:fresh-mart"
HOUSEHOLD_AGENT = "household:us-1"
MEMBERS = {"ana": ["tree_nuts"], "ben": []}

# Line 1 (oat milk, 499) offers a safe and a tree-nut substitute; line 2 (bread, 300)
# offers one substitute that costs more than the line it replaces.
LINES: list[dict[str, Any]] = [
    {
        "line": 1,
        "sku": "oat-milk-1l",
        "name": "Oat milk 1 l",
        "qty_missing": 1,
        "line_total_minor": 499,
        "substitutes": [
            {"sku": "soy-milk-1l", "name": "Soy milk 1 l", "qty": 1, "unit_minor": 449},
            {"sku": "almond-milk-1l", "name": "Almond milk 1 l", "qty": 1, "unit_minor": 479},
        ],
    },
    {
        "line": 2,
        "sku": "bread-white",
        "name": "White bread",
        "qty_missing": 2,
        "line_total_minor": 300,
        "substitutes": [
            {"sku": "bread-sourdough", "name": "Sourdough", "qty": 2, "unit_minor": 175},
        ],
    },
]


@dataclass
class _Cursor:
    row: tuple[Any, ...] | None

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self.row


@dataclass
class FakeConn:
    """Answers the amendment limit queries from in-memory rows."""

    status: str = "closed"
    state: str = "courier_booked"
    seller: str = SELLER
    stored: dict[str, Any] = field(default_factory=dict)  # idempotency key -> result
    queries: list[str] = field(default_factory=list)

    async def execute(self, query: str, params: Sequence[Any]) -> _Cursor:
        self.queries.append(query)
        if "JOIN orders" in query:
            shortfall_id, order_id = params
            if (shortfall_id, order_id) != (SHORTFALL_ID, ORDER_ID):
                return _Cursor(None)
            return _Cursor((LINES, self.status, self.state))
        if "FROM shortfalls" in query:
            found = tuple(params) == (SHORTFALL_ID, ORDER_ID, self.seller)
            return _Cursor((1,) if found else None)
        if "FROM idempotency_keys" in query:
            (key,) = params
            return _Cursor((self.stored[key],) if key in self.stored else None)
        raise AssertionError(f"unexpected query {query}")


@dataclass
class FakeKnowledge:
    result: AllergenCheck = field(default_factory=AllergenCheck)
    error: Exception | None = None
    calls: list[tuple[list[str], list[str]]] = field(default_factory=list)

    async def check_allergens(
        self, skus: Sequence[str], allergen_codes: Sequence[str]
    ) -> AllergenCheck:
        self.calls.append((list(skus), list(allergen_codes)))
        if self.error is not None:
            raise self.error
        return self.result


def _authorize(
    picks: dict[int, str], knowledge: FakeKnowledge, conn: FakeConn | None = None
) -> dict[str, Any]:
    return asyncio.run(
        limits.authorize_amendment(
            conn or FakeConn(),  # type: ignore[arg-type]
            order_id=ORDER_ID,
            shortfall_id=SHORTFALL_ID,
            picks=picks,
            member_allergens=MEMBERS,
            knowledge=knowledge,
        )
    )


def _amend(conn: FakeConn, picks: dict[Any, str], seller: str = SELLER) -> None:
    asyncio.run(
        limits.check_amend_order(
            conn,  # type: ignore[arg-type]
            order_id=ORDER_ID,
            shortfall_id=SHORTFALL_ID,
            seller_agent_id=seller,
            household_agent_id=HOUSEHOLD_AGENT,
            picks=picks,
        )
    )


def test_authorize_amendment_refuses_allergen() -> None:
    knowledge = FakeKnowledge(
        AllergenCheck(hits=[AllergenHit(sku="almond-milk-1l", allergen_code="tree_nuts")])
    )
    result = _authorize({1: "almond-milk-1l", 2: "drop"}, knowledge)
    assert result == {"picks": {"1": "drop", "2": "drop"}, "refused": [1]}
    assert knowledge.calls == [(["almond-milk-1l"], ["tree_nuts"])]

    safe = _authorize({1: "soy-milk-1l"}, FakeKnowledge())
    assert safe == {"picks": {"1": "soy-milk-1l"}, "refused": []}

    unknown = _authorize(
        {1: "soy-milk-1l"}, FakeKnowledge(AllergenCheck(unknown_skus=["soy-milk-1l"]))
    )
    assert unknown == {"picks": {"1": "drop"}, "refused": [1]}


def test_authorize_amendment_refuses_substitute_above_line_total() -> None:
    # 2 × 175 = 350 > 300: refused even though it is safe for every member.
    knowledge = FakeKnowledge()
    result = _authorize({1: "soy-milk-1l", 2: "bread-sourdough"}, knowledge)
    assert result == {"picks": {"1": "soy-milk-1l", "2": "drop"}, "refused": [2]}
    assert knowledge.calls == [(["soy-milk-1l"], ["tree_nuts"])]


@pytest.mark.parametrize(
    "error", [AdapterFailure("knowledge_unavailable", retryable=True), TimeoutError()]
)
def test_authorize_amendment_fails_closed_without_graph(error: Exception) -> None:
    result = _authorize({1: "soy-milk-1l", 2: "drop"}, FakeKnowledge(error=error))
    assert result == {"picks": {"1": "drop", "2": "drop"}, "refused": [1]}


def test_authorize_amendment_state_rules() -> None:
    # In delivery: substitutes become drops, drops are still allowed.
    in_delivery = FakeConn(state="in_delivery")
    result = _authorize({1: "soy-milk-1l", 2: "drop"}, FakeKnowledge(), in_delivery)
    assert result == {"picks": {"1": "drop", "2": "drop"}, "refused": [1]}
    for state in ("delivered", "failed", "cancelled"):
        with pytest.raises(limits.WrongOrderState):
            _authorize({2: "drop"}, FakeKnowledge(), FakeConn(state=state))


def test_authorize_amendment_refuses_bad_input() -> None:
    with pytest.raises(limits.ShortfallPending):
        _authorize({2: "drop"}, FakeKnowledge(), FakeConn(status="pending"))
    with pytest.raises(limits.PickNotInNotice):
        _authorize({3: "drop"}, FakeKnowledge())
    with pytest.raises(limits.PickNotInNotice):
        _authorize({1: "bread-sourdough"}, FakeKnowledge())
    with pytest.raises(limits.ShortfallNotFound):
        asyncio.run(
            limits.authorize_amendment(
                FakeConn(),  # type: ignore[arg-type]
                order_id=uuid.uuid4(),
                shortfall_id=SHORTFALL_ID,
                picks={2: "drop"},
                member_allergens=MEMBERS,
                knowledge=FakeKnowledge(),
            )
        )


def test_amend_order_picks_must_match_authorization() -> None:
    key = ids.idempotency_key(ORDER_ID, "authorize_amendment", HOUSEHOLD_AGENT, scope=SHORTFALL_ID)
    authorized = _authorize(
        {1: "almond-milk-1l", 2: "bread-sourdough"},
        FakeKnowledge(
            AllergenCheck(hits=[AllergenHit(sku="almond-milk-1l", allergen_code="tree_nuts")])
        ),
    )
    conn = FakeConn(stored={key: authorized})

    _amend(conn, {1: "drop", 2: "drop"})  # the authorized picks, int keys
    _amend(conn, {"1": "drop", "2": "drop"})  # the same picks, as stored

    with pytest.raises(limits.AmendmentNotAuthorized):
        _amend(conn, {1: "almond-milk-1l", 2: "drop"})  # the refused substitute
    with pytest.raises(limits.AmendmentNotAuthorized):
        _amend(conn, {1: "drop"})  # a line missing
    with pytest.raises(limits.AmendmentNotAuthorized):
        _amend(FakeConn(), {1: "drop", 2: "drop"})  # never authorized
    with pytest.raises(limits.ShortfallNotFound):
        _amend(conn, {1: "drop", 2: "drop"}, seller="grocery:other")


def test_amendment_limit_exceptions_are_guard_refused() -> None:
    for exc in (
        limits.ShortfallNotFound(),
        limits.ShortfallPending(),
        limits.PickNotInNotice(1),
        limits.WrongOrderState("delivered"),
        limits.AmendmentNotAuthorized(),
    ):
        assert isinstance(exc, GuardRefused)


# --- the shortfall tools (db) ---------------------------------------------------------

SECRET = "unit-test-mock-token-secret-32-bytes-long"
HH = "us-1"
HH_AGENT = f"household:{HH}"
STORE = "grocery:mock-us"
ORG = "org:mock-grocery-us"
API = "system:api"
ZONE = "us-oh-45202"
ASHA = ids.seed_user_id("us-1-a")
BEN = ids.seed_user_id("us-1-b")
STRANGER = ids.seed_user_id("us-2-a")
EXTERNAL_ORDER = "mock-order-1"
ITEMS = [
    {"line": 1, "sku": "MOCK-MILK-WHOLE", "name": "Whole milk", "qty": 2, "unit_minor": 429},
    {"line": 2, "sku": "MOCK-EGGS-12", "name": "Eggs", "qty": 1, "unit_minor": 349},
    {"line": 3, "sku": "MOCK-BREAD", "name": "Bread", "qty": 1, "unit_minor": 300},
]
# Lines 1 and 2 are short; each offers one substitute at or below its line total.
NOTICE = [
    {
        "line": 1,
        "sku": "MOCK-MILK-WHOLE",
        "name": "Whole milk",
        "qty_missing": 2,
        "line_total_minor": 858,
        "substitutes": [
            {"sku": "MOCK-MILK-OAT", "name": "Oat milk", "qty": 2, "unit_minor": 399},
        ],
    },
    {
        "line": 2,
        "sku": "MOCK-EGGS-12",
        "name": "Eggs",
        "qty_missing": 1,
        "line_total_minor": 349,
        "substitutes": [
            {"sku": "MOCK-EGGS-6", "name": "Eggs (6)", "qty": 1, "unit_minor": 199},
        ],
    },
]


@pytest.fixture
def _auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tokens, "_secret", lambda: SECRET)
    monkeypatch.setattr(fga, "check", lambda user, relation, object: True)


def _query(pg: str, sql: str, *params: Any) -> list[dict[str, Any]]:
    with psycopg.connect(pg) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _exec(pg: str, sql: str, *params: Any) -> None:
    with psycopg.connect(pg) as conn:
        conn.execute(sql, params)


def _seed(pg: str) -> tuple[uuid.UUID, uuid.UUID]:
    """A household with two members, a stranger, the cards, a run and a booked order."""
    run_id, order_id = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO households (household_id, name, region, zone_id, timezone, currency)"
            " VALUES (%s, 'Test', 'us', %s, 'America/New_York', 'USD')",
            (HH, ZONE),
        )
        for user, name in ((ASHA, "Asha"), (BEN, "Ben"), (STRANGER, "Sam")):
            conn.execute("INSERT INTO users (user_id, display_name) VALUES (%s, %s)", (user, name))
        for user, role in ((ASHA, "admin"), (BEN, "member")):
            conn.execute(
                "INSERT INTO household_members (household_id, user_id, role) VALUES (%s, %s, %s)",
                (HH, user, role),
            )
        conn.execute(
            "INSERT INTO household_addresses"
            " (address_ref, household_id, line1, city, region, postal_code, country)"
            " VALUES (%s, %s, '1 Main St', 'Cincinnati', 'OH', '45202', 'US')",
            (ids.seed_address_ref(HH), HH),
        )
        for agent_id, cls, owner in ((HH_AGENT, "household", HH_AGENT), (STORE, "grocery", ORG)):
            conn.execute(
                "INSERT INTO agent_cards (agent_id, class, owner_ref, region, zone_ids,"
                " currency, host_process, vendor) VALUES (%s, %s, %s, 'us', %s, 'USD',"
                " 'agents', 'mock')",
                (agent_id, cls, owner, [ZONE]),
            )
        conn.execute(
            "INSERT INTO runs (run_id, household_id, kind, trigger, modes)"
            " VALUES (%s, %s, 'weekly', %s, '{}')",
            (run_id, HH, Jsonb({})),
        )
        conn.execute(
            "INSERT INTO orders (order_id, run_id, household_id, kind, state, seller_agent_id,"
            " external_order_id, total_minor, currency, items_hash, address_ref)"
            " VALUES (%s, %s, %s, 'grocery', 'courier_booked', %s, %s, %s, 'USD', %s, %s)",
            (
                order_id,
                run_id,
                HH,
                STORE,
                EXTERNAL_ORDER,
                sum(i["qty"] * i["unit_minor"] for i in ITEMS),
                orders.items_hash(ITEMS),
                ids.seed_address_ref(HH),
            ),
        )
        for i in ITEMS:
            conn.execute(
                "INSERT INTO order_items (order_id, line, sku, name, qty, unit_minor)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (order_id, i["line"], i["sku"], i["name"], i["qty"], i["unit_minor"]),
            )
        body = {
            "order_id": str(order_id),
            "lines": [
                {"sku": i["sku"], "qty": i["qty"], "unit_minor": i["unit_minor"]} for i in ITEMS
            ],
            "total_minor": sum(i["qty"] * i["unit_minor"] for i in ITEMS),
            "currency": "USD",
        }
        conn.execute(
            "INSERT INTO mock_orders (external_order_id, env, agent_id, idempotency_key, body,"
            " state) VALUES (%s, 'live', %s, %s, %s, 'placed')",
            (EXTERNAL_ORDER, STORE, EXTERNAL_ORDER, Jsonb(body)),
        )
    return run_id, order_id


def _ctx(run_id: uuid.UUID, agent_id: str, sub: str) -> CallContext:
    return CallContext(
        run_id=run_id,
        agent_id=agent_id,
        conversation_id=None,
        attempt=1,
        token=tokens.MockIssuer(secret=SECRET).issue(sub, agent_id, ["orders"]),
        env="live",
        modes={},
        on_behalf_of=None if agent_id == API else sub,
    )


def _with_pool[T](pg: str, monkeypatch: pytest.MonkeyPatch, fn: Callable[[], Awaitable[T]]) -> T:
    async def go() -> T:
        async with AsyncConnectionPool(pg, min_size=1, max_size=3, open=False) as pool:
            monkeypatch.setattr(db, "get_pool", lambda: pool)
            return await fn()

    return asyncio.run(go())


@dataclass
class World:
    pg: str
    monkeypatch: pytest.MonkeyPatch
    run_id: uuid.UUID
    order_id: uuid.UUID

    @property
    def shortfall_id(self) -> uuid.UUID:
        return ids.shortfall_id(self.order_id)

    def run[T](self, fn: Callable[[], Awaitable[T]]) -> T:
        return _with_pool(self.pg, self.monkeypatch, fn)

    def open(self) -> dict[str, Any]:
        return self.run(
            lambda: household.open_shortfall(
                _ctx(self.run_id, HH_AGENT, f"user:{ASHA}"),
                shortfall_id=str(self.shortfall_id),
                order_id=str(self.order_id),
                seller=STORE,
                lines=NOTICE,
                due_at=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            )
        )

    def choose(self, line: int, pick: str, user: uuid.UUID, agent_id: str = API) -> dict[str, Any]:
        return self.run(
            lambda: household.choose_shortfall_option(
                _ctx(self.run_id, agent_id, "svc.api"),
                shortfall_id=self.shortfall_id,
                line=line,
                pick=pick,
                user_id=user,
            )
        )

    def row(self) -> dict[str, Any]:
        (row,) = _query(
            self.pg,
            "SELECT picks, status, closed_at FROM shortfalls WHERE shortfall_id = %s",
            self.shortfall_id,
        )
        return row

    def closed_messages(self) -> list[dict[str, Any]]:
        return _query(
            self.pg,
            "SELECT message_id, from_agent, to_agent, conversation_id, body FROM agent_messages"
            " WHERE run_id = %s AND type = 'shortfall_closed'",
            self.run_id,
        )


@pytest.fixture
def world(pg: str, monkeypatch: pytest.MonkeyPatch, _auth: None) -> World:
    run_id, order_id = _seed(pg)
    w = World(pg, monkeypatch, run_id, order_id)
    assert w.open()["opened"] is True
    return w


@pytest.mark.db
def test_open_shortfall_once_per_order(world: World) -> None:
    replay = world.open()
    assert (replay["opened"], replay["status"]) == (False, "pending")
    assert world.row()["picks"] == {}
    sf = world.run(
        lambda: household.get_shortfall(
            _ctx(world.run_id, HH_AGENT, f"user:{ASHA}"), shortfall_id=str(world.shortfall_id)
        )
    )
    assert (sf["seller_agent_id"], sf["status"], sf["lines"]) == (STORE, "pending", NOTICE)
    with pytest.raises(GuardRefused) as e:
        world.run(
            lambda: household.open_shortfall(
                _ctx(world.run_id, HH_AGENT, f"user:{ASHA}"),
                shortfall_id=str(uuid.uuid4()),
                order_id=str(world.order_id),
                seller=STORE,
                lines=NOTICE,
                due_at=datetime.now(UTC).isoformat(),
            )
        )
    assert e.value.reason == household.INVALID_SHORTFALL


@pytest.mark.db
def test_first_reply_per_line_wins(world: World) -> None:
    first = world.choose(1, "MOCK-MILK-OAT", ASHA)
    assert (first["outcome"], first["status"]) == ("chosen", "pending")

    second = world.choose(1, "drop", BEN)
    assert second["outcome"] == "already_decided"
    assert world.row()["picks"] == {"1": "MOCK-MILK-OAT"}


@pytest.mark.db
def test_last_pick_closes_and_sends_shortfall_closed(world: World) -> None:
    world.choose(1, "MOCK-MILK-OAT", ASHA)
    assert world.closed_messages() == []

    last = world.choose(2, "drop", BEN)
    assert (last["outcome"], last["status"]) == ("chosen", "closed")
    row = world.row()
    assert row["picks"] == {"1": "MOCK-MILK-OAT", "2": "drop"}
    assert row["status"] == "closed" and row["closed_at"] is not None

    (msg,) = world.closed_messages()
    assert msg["message_id"] == ids.shortfall_closed_id(world.shortfall_id)
    assert (msg["from_agent"], msg["to_agent"]) == (API, HH_AGENT)
    assert msg["conversation_id"] == ids.shortfall_conversation_id(world.shortfall_id)
    assert msg["body"] == {
        "shortfall_id": str(world.shortfall_id),
        "order_id": str(world.order_id),
        "reason": "all_picked",
    }

    # A late tap changes nothing and sends nothing.
    assert world.choose(2, "MOCK-EGGS-6", ASHA)["outcome"] == "already_decided"
    assert len(world.closed_messages()) == 1


@pytest.mark.db
def test_non_member_reply_refused(world: World) -> None:
    with pytest.raises(GuardRefused) as e:
        world.choose(1, "MOCK-MILK-OAT", STRANGER)
    assert e.value.reason == household.AUTHZ_DENIED
    # Only `api`, with its service token, may record a pick.
    with pytest.raises(GuardRefused) as e:
        world.choose(1, "MOCK-MILK-OAT", ASHA, agent_id=HH_AGENT)
    assert e.value.reason == household.AUTHZ_DENIED
    assert world.row()["picks"] == {}


@pytest.mark.db
def test_pick_not_in_notice_refused(world: World) -> None:
    for line, pick in ((1, "MOCK-EGGS-6"), (1, "MOCK-CAVIAR"), (3, "drop"), (9, "drop")):
        with pytest.raises(GuardRefused) as e:
            world.choose(line, pick, ASHA)
        assert e.value.reason == "pick_not_in_notice"
    assert world.row()["picks"] == {}


def _authorize_and_amend(world: World, state_before_amend: str) -> dict[str, Any]:
    world.choose(1, "MOCK-MILK-OAT", ASHA)
    world.choose(2, "drop", BEN)
    authorized = world.run(
        lambda: household.authorize_amendment(
            _ctx(world.run_id, HH_AGENT, f"user:{ASHA}"),
            order_id=world.order_id,
            shortfall_id=world.shortfall_id,
            picks={1: "MOCK-MILK-OAT", 2: "drop"},
        )
    )
    assert (authorized["picks"], authorized["refused"]) == ({"1": "MOCK-MILK-OAT", "2": "drop"}, [])
    if state_before_amend != "courier_booked":
        _exec(
            world.pg,
            "UPDATE orders SET state = %s WHERE order_id = %s",
            state_before_amend,
            world.order_id,
        )
    return world.run(
        lambda: grocery.amend_order(
            _ctx(world.run_id, STORE, ORG),
            order_id=world.order_id,
            shortfall_id=world.shortfall_id,
            picks=authorized["picks"],
        )
    )


def _order_lines(world: World) -> list[tuple[int, str, str | None]]:
    rows = _query(
        world.pg,
        "SELECT line, sku, substituted_for FROM order_items WHERE order_id = %s ORDER BY line",
        world.order_id,
    )
    return [(r["line"], r["sku"], r["substituted_for"]) for r in rows]


@pytest.mark.db
def test_amend_order_applies_substitute(world: World) -> None:
    result = _authorize_and_amend(world, "courier_booked")
    assert (result["picks"], result["refused"]) == ({"1": "MOCK-MILK-OAT", "2": "drop"}, [])
    assert (result["substituted"], result["dropped"]) == ([1], [2])
    assert result["total_minor"] == 2 * 399 + 300
    assert _order_lines(world) == [(1, "MOCK-MILK-OAT", "MOCK-MILK-WHOLE"), (3, "MOCK-BREAD", None)]
    (order,) = _query(
        world.pg, "SELECT total_minor FROM orders WHERE order_id = %s", world.order_id
    )
    assert order["total_minor"] == 2 * 399 + 300
    assert world.row()["status"] == "applied"
    (mock,) = _query(
        world.pg, "SELECT body FROM mock_orders WHERE external_order_id = %s", EXTERNAL_ORDER
    )
    assert [line["sku"] for line in mock["body"]["lines"]] == ["MOCK-MILK-OAT", "MOCK-BREAD"]
    assert mock["body"]["total_minor"] == 2 * 399 + 300


@pytest.mark.db
def test_amend_order_in_delivery_turns_substitutes_into_drops(world: World) -> None:
    # Authorized while `courier_booked`; the courier picked up before the seller amended.
    result = _authorize_and_amend(world, "in_delivery")
    assert (result["picks"], result["refused"]) == ({"1": "drop", "2": "drop"}, [1])
    assert (result["substituted"], result["dropped"]) == ([], [1, 2])
    assert (result["state"], result["total_minor"]) == ("in_delivery", 300)
    assert _order_lines(world) == [(3, "MOCK-BREAD", None)]
    (order,) = _query(
        world.pg,
        "SELECT state, total_minor, items_hash FROM orders WHERE order_id = %s",
        world.order_id,
    )
    assert (order["state"], order["total_minor"]) == ("in_delivery", 300)
    assert order["items_hash"] == orders.items_hash([ITEMS[2]])
    assert world.row()["status"] == "applied"
    (mock,) = _query(
        world.pg, "SELECT body FROM mock_orders WHERE external_order_id = %s", EXTERNAL_ORDER
    )
    assert [line["sku"] for line in mock["body"]["lines"]] == ["MOCK-BREAD"]
