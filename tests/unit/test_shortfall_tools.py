"""Shortfall amendment limits (agent-protocol §10 "Applying" and "Amending", P28; #67).

`authorize_amendment` and `check_amend_order` are tested against an in-memory connection,
like `test_limits.py`. The tool wiring and the db tests for the shortfall tools are #68.
"""

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from services.common import ids
from services.mcp.knowledge.adapters.neo4j_knowledge import AllergenCheck, AllergenHit
from services.mcp.meta import AdapterFailure
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
