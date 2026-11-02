"""Guard limits (architecture item 51, amended 2026-11-02; agent-protocol P28; issue #44)."""

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.common import orders
from services.common.directory import AgentCard
from services.common.money import Money
from services.common.protocol.schemas import Window
from services.mcp.knowledge.adapters.neo4j_knowledge import AllergenCheck, AllergenHit
from services.safety import limits
from services.safety.guard import GuardRefused

HOUSEHOLD = "us-1"
ORDER_ID = uuid.UUID("8a4f6c1e-0000-4000-8000-000000000044")
APPROVER = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


@dataclass
class _Cursor:
    row: tuple[Any, ...] | None

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self.row


@dataclass
class FakeConn:
    """Answers the limit queries from in-memory rows."""

    approvals: list[dict[str, Any]] = field(default_factory=list)
    approver: uuid.UUID | None = APPROVER
    order_currency: str | None = "USD"
    run_kind: str | None = "weekly"
    queries: list[str] = field(default_factory=list)

    async def execute(self, query: str, params: Sequence[Any]) -> _Cursor:
        self.queries.append(query)
        if "FROM approvals" in query:
            order_id, household_id, amount, currency = params
            covered = any(
                a["order_id"] == order_id
                and a["household_id"] == household_id
                and a["status"] == "approved"
                and a["amount_minor"] >= amount
                and a["currency"] == currency
                for a in self.approvals
            )
            return _Cursor((1,) if covered else None)
        if "FROM approval_rules" in query:
            return _Cursor(None if self.approver is None else (self.approver,))
        if "JOIN runs" in query:
            return _Cursor(None if self.run_kind is None else (self.run_kind,))
        if "SELECT currency FROM orders" in query:
            return _Cursor(None if self.order_currency is None else (self.order_currency,))
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


@pytest.fixture
def remaining(monkeypatch: pytest.MonkeyPatch) -> list[Money]:
    """The remaining weekly budget `check_budget` sees; defaults to $100.00."""
    value = [Money(10_000, "USD")]

    async def fake(conn: Any, household_id: str, *, now: datetime | None = None) -> Money:
        assert household_id == HOUSEHOLD
        return value[0]

    monkeypatch.setattr(orders, "remaining_weekly_budget", fake)
    return value


def _approval(amount_minor: int, status: str = "approved") -> dict[str, Any]:
    return {
        "order_id": ORDER_ID,
        "household_id": HOUSEHOLD,
        "status": status,
        "amount_minor": amount_minor,
        "currency": "USD",
    }


def _budget(conn: FakeConn, total: Money) -> None:
    asyncio.run(limits.check_budget(conn, HOUSEHOLD, ORDER_ID, total))  # type: ignore[arg-type]


def _card(zone_ids: tuple[str, ...]) -> AgentCard:
    return AgentCard(
        agent_id="delivery:mock-us",
        agent_class="delivery",
        owner_ref="org:mock",
        region="us",
        zone_ids=zone_ids,
        currency="USD",
        host_process="agents",
        vendor="mock",
    )


def test_over_remaining_budget_needs_approval(remaining: list[Money]) -> None:
    conn = FakeConn(approvals=[_approval(9_000), _approval(20_000, status="pending")])
    _budget(conn, Money(10_000, "USD"))  # at the remaining budget: no approval needed
    with pytest.raises(limits.NeedsApproval) as e:
        _budget(conn, Money(10_001, "USD"))
    assert e.value.reason == "needs_approval"
    assert isinstance(e.value, GuardRefused)


def test_approved_order_passes(remaining: list[Money]) -> None:
    conn = FakeConn(approvals=[_approval(15_000)])
    _budget(conn, Money(15_000, "USD"))
    other = FakeConn(approvals=[{**_approval(15_000), "order_id": uuid.uuid4()}])
    with pytest.raises(limits.NeedsApproval):
        _budget(other, Money(15_000, "USD"))


def test_requote_within_approved_total_passes(remaining: list[Money]) -> None:
    conn = FakeConn(approvals=[_approval(15_000)])
    _budget(conn, Money(14_200, "USD"))  # a re-quote below the approved total
    with pytest.raises(limits.NeedsApproval):
        _budget(conn, Money(15_001, "USD"))  # a re-quote above it needs a new approval


def test_wrong_approver_refused() -> None:
    conn = FakeConn()
    asyncio.run(limits.check_approver(conn, HOUSEHOLD, APPROVER))  # type: ignore[arg-type]
    asyncio.run(limits.check_approver(conn, HOUSEHOLD, str(APPROVER)))  # type: ignore[arg-type]
    with pytest.raises(limits.WrongApprover) as e:
        asyncio.run(limits.check_approver(conn, HOUSEHOLD, uuid.uuid4()))  # type: ignore[arg-type]
    assert e.value.reason == "wrong_approver"
    with pytest.raises(limits.WrongApprover):
        asyncio.run(limits.check_approver(FakeConn(approver=None), HOUSEHOLD, APPROVER))  # type: ignore[arg-type]


ITEMS = [{"sku": "sku-bread"}, {"sku": "sku-milk"}, {"sku": "sku-bread"}]
ALLERGIES = {"u1": ["milk"], "u2": ["peanuts", "milk"], "u3": []}


def test_allergen_item_rejected() -> None:
    knowledge = FakeKnowledge(
        AllergenCheck(hits=[AllergenHit("sku-milk", "milk")], unknown_skus=["sku-x"])
    )
    with pytest.raises(limits.AllergenConflict) as e:
        asyncio.run(limits.check_allergens(ITEMS, ALLERGIES, knowledge))
    assert (e.value.reason, e.value.sku, e.value.code) == ("allergen_conflict", "sku-milk", "milk")
    assert knowledge.calls == [(["sku-bread", "sku-milk"], ["milk", "peanuts"])]

    clean = FakeKnowledge()
    asyncio.run(limits.check_allergens(ITEMS, ALLERGIES, clean))
    # No member has allergen codes: the knowledge graph is not called.
    unused = FakeKnowledge(error=RuntimeError("must not be called"))
    asyncio.run(limits.check_allergens(ITEMS, {"u1": [], "u3": []}, unused))
    assert unused.calls == []


def test_unknown_sku_fails_closed() -> None:
    knowledge = FakeKnowledge(AllergenCheck(unknown_skus=["sku-milk"]))
    with pytest.raises(limits.AllergenUnknown) as e:
        asyncio.run(limits.check_allergens(ITEMS, ALLERGIES, knowledge))
    assert (e.value.reason, e.value.sku) == ("allergen_unknown", "sku-milk")


@pytest.mark.parametrize("error", [ConnectionError("down"), TimeoutError(), RuntimeError("x")])
def test_knowledge_down_fails_closed(error: Exception) -> None:
    knowledge = FakeKnowledge(error=error)
    with pytest.raises(limits.KnowledgeUnavailable) as e:
        asyncio.run(limits.check_allergens(ITEMS, ALLERGIES, knowledge))
    assert e.value.reason == "knowledge_unavailable"


def test_out_of_zone_rejected() -> None:
    limits.check_coverage(_card(("z1", "z2")), "z2")
    with pytest.raises(limits.OutOfZone) as e:
        limits.check_coverage(_card(("z1", "z2")), "z3")
    assert e.value.reason == "out_of_zone"
    with pytest.raises(limits.OutOfZone):
        limits.check_coverage(_card(()), "z1")


def test_window_must_end_in_future() -> None:
    hour = timedelta(hours=1)
    limits.check_window(Window(start=NOW + hour, end=NOW + 2 * hour), NOW)
    limits.check_window(Window(start=NOW - hour, end=NOW + hour), NOW)  # already open
    refused = [
        Window(start=NOW - 2 * hour, end=NOW - hour),  # ended
        Window(start=NOW - hour, end=NOW),  # ends now
        Window(start=NOW + 2 * hour, end=NOW + hour),  # start after end
        Window(start=NOW + hour, end=NOW + hour),  # empty
    ]
    for window in refused:
        with pytest.raises(limits.OutsideWindow) as e:
            limits.check_window(window, NOW)
        assert e.value.reason == "outside_window"
    with pytest.raises(limits.OutsideWindow):
        limits.check_window(Window(start=NOW + hour, end=NOW + 2 * hour), NOW.replace(tzinfo=None))


def _authorize(conn: FakeConn, total: Money, knowledge: FakeKnowledge | None = None) -> None:
    asyncio.run(
        limits.authorize_spend(
            conn,  # type: ignore[arg-type]
            household_id=HOUSEHOLD,
            order_id=ORDER_ID,
            items=ITEMS,
            total=total,
            member_allergens=ALLERGIES,
            knowledge=knowledge or FakeKnowledge(),
        )
    )


def test_authorize_spend_refuses_a2a_demo_order(remaining: list[Money]) -> None:
    _authorize(FakeConn(), Money(4_200, "USD"))
    with pytest.raises(limits.A2ADemoOrder) as e:
        _authorize(FakeConn(run_kind="a2a_demo"), Money(4_200, "USD"))
    assert e.value.reason == "a2a_demo_order"
    with pytest.raises(limits.A2ADemoOrder):
        _authorize(FakeConn(run_kind=None), Money(4_200, "USD"))


def test_authorize_spend_currency_mismatch(remaining: list[Money]) -> None:
    with pytest.raises(limits.CurrencyMismatch) as e:
        _authorize(FakeConn(order_currency="LKR"), Money(4_200, "USD"))
    assert e.value.reason == "currency_mismatch"
    # A total in another currency than the budget is refused, never compared.
    with pytest.raises(limits.CurrencyMismatch):
        _authorize(FakeConn(order_currency="EUR"), Money(4_200, "EUR"))


def test_authorize_spend_checks_allergens_first(remaining: list[Money]) -> None:
    conn = FakeConn(run_kind="a2a_demo")
    knowledge = FakeKnowledge(AllergenCheck(hits=[AllergenHit("sku-milk", "milk")]))
    with pytest.raises(limits.AllergenConflict):
        _authorize(conn, Money(4_200, "USD"), knowledge)
    assert conn.queries == []


def test_every_limit_exception_is_guard_refused() -> None:
    names = [n for n in limits.__all__ if n[0].isupper() and n != "WindowLike"]
    assert names
    for name in names:
        assert issubclass(getattr(limits, name), GuardRefused), name
