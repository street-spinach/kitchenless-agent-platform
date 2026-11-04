"""`services/api/story.py` (showcase.md §4): each section from seeded rows, in the shapes the
agents really write (a counter overwrites the quote's total, a loser's quote stays `open`,
`quotes` also holds couriers' quotes, `delivery_booked` is sent twice)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.types.json import Jsonb

from services.api import story as story_module
from services.api.story import Story, build_story, without_spending
from services.safety import scope
from tests.unit import api_world as aw
from tests.unit import experience_world as ew
from tests.unit import identity_world as w
from tests.unit.test_me_household import _named
from tests.unit.test_me_routes import (
    HOUSEHOLD,
    HOUSEHOLD_AGENT,
    FakeKeycloak,
    World,
    make_kc,
    make_world,
)

db = pytest.mark.db

FRESH = "grocery:fresh-market-us"
CORNER = "grocery:corner-us"
LATE = "grocery:late-us"
PICKY = "grocery:picky-us"
SILENT = "grocery:silent-us"
SWIFT = "delivery:swift-us"
SLOW = "delivery:slow-us"
SELLERS = {
    FRESH: "Fresh Market",
    CORNER: "Corner Shop",
    LATE: "Late Foods",
    PICKY: "Picky Grocer",
    SILENT: "Silent Stores",
    SWIFT: "Swift Couriers",
    SLOW: "Slow Couriers",
}
RATIONALE = "Our best price for this basket."
CONSTRAINTS = {"allergens": ["peanuts", "tree_nuts"], "diets": ["high_protein", "vegetarian"]}
LINE = {
    "line": 0,
    "requested_item": "rice",
    "offered_sku": "sku-1",
    "offered_name": "Rice",
    "qty": 1,
    "unit_minor": 100,
}
MISSING = {**LINE, "line": 1, "offered_sku": None, "offered_name": None}


@dataclass
class Seeded:
    order_id: UUID
    run_id: UUID
    at: datetime
    tick: int = 0


@pytest.fixture
def kc(monkeypatch: pytest.MonkeyPatch) -> FakeKeycloak:
    return make_kc(monkeypatch)


@pytest.fixture
def world(pg: str, kc: FakeKeycloak) -> World:
    made = make_world(pg, kc)
    _named(pg, made)
    return made


def message(
    conn: psycopg.Connection,
    seeded: Seeded,
    kind: str,
    sender: str,
    receiver: str,
    **body: Any,
) -> None:
    seeded.tick += 1
    conn.execute(
        "INSERT INTO agent_messages (message_id, run_id, conversation_id, from_agent, to_agent,"
        " to_host, type, body, created_at) VALUES (%s, %s, %s, %s, %s, 'agents', %s, %s, %s)",
        (
            uuid.uuid4(),
            seeded.run_id,
            uuid.uuid4(),
            sender,
            receiver,
            kind,
            Jsonb({"order_id": str(seeded.order_id), **body}),
            seeded.at + timedelta(seconds=seeded.tick),
        ),
    )


def quote(
    conn: psycopg.Connection,
    seeded: Seeded,
    seller: str,
    total: int,
    status: str,
    *,
    lines: list[dict[str, Any]] | None = None,
    buyer: str = HOUSEHOLD_AGENT,
    constraints: dict[str, Any] | None = None,
) -> None:
    seeded.tick += 1
    conn.execute(
        "INSERT INTO quotes (quote_id, run_id, order_id, conversation_id, seller_agent_id,"
        " buyer_agent_id, items, total_minor, currency, expires_at, status, constraints,"
        " created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'USD', %s, %s, %s, %s)",
        (
            uuid.uuid4(),
            seeded.run_id,
            seeded.order_id,
            uuid.uuid4(),
            seller,
            buyer,
            Jsonb({"lines": lines or [LINE]}),
            total,
            seeded.at + timedelta(hours=1),
            status,
            Jsonb(CONSTRAINTS if constraints is None else constraints),
            seeded.at + timedelta(seconds=seeded.tick),
        ),
    )


def trace(
    conn: psycopg.Connection,
    seeded: Seeded,
    kind: str,
    payload: dict[str, Any],
    *,
    agent: str = HOUSEHOLD_AGENT,
    tokens: tuple[int, int] | None = None,
    model: str | None = None,
) -> None:
    seeded.tick += 1
    conn.execute(
        "INSERT INTO agent_traces (run_id, seq, ts, agent_id, kind, payload, tokens_in,"
        " tokens_out, model) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            seeded.run_id,
            seeded.tick,
            seeded.at + timedelta(seconds=seeded.tick),
            agent,
            kind,
            Jsonb(payload),
            tokens[0] if tokens else None,
            tokens[1] if tokens else None,
            model,
        ),
    )


def spend_check(
    conn: psycopg.Connection, seeded: Seeded, refused: bool, reason: str | None
) -> None:
    """A `run_guarded` row for `household.authorize_spend`."""
    trace(
        conn,
        seeded,
        "tool_result",
        {
            "server": "household",
            "tool": "authorize_spend",
            "result": None,
            "refused": refused,
            "reason": reason,
            "act_agent_id": HOUSEHOLD_AGENT,
        },
    )


def bare_order(pg: str, state: str, at: datetime | None = None) -> Seeded:
    at = at or datetime.now(UTC) - timedelta(hours=1)
    with psycopg.connect(pg) as conn:
        order_id, run_id = ew.order(conn, HOUSEHOLD, state, at)
    return Seeded(order_id, run_id, at)


def seed_order(pg: str, world: World, starter: w.Person | None = None) -> Seeded:
    """The whole story of a delivered-to-plan weekly order, started by `starter` (the member
    by default): five stores asked, one countered, two couriers, one approval."""
    seeded = bare_order(pg, "courier_booked")
    window = {"start": "2026-11-05T22:00:00+00:00", "end": "2026-11-05T23:00:00+00:00"}
    with psycopg.connect(pg) as conn:
        for agent, name in SELLERS.items():
            org = agent.split(":")[1]
            conn.execute(
                "INSERT INTO organizations (org_id, name, region) VALUES (%s, %s, 'us')",
                (org, name),
            )
            w.add_agent(conn, agent, f"org:{org}")
        conn.execute(
            "INSERT INTO budgets (household_id, period, amount_minor, currency)"
            " VALUES (%s, 'weekly', 20000, 'USD')",
            (HOUSEHOLD,),
        )
        conn.execute(
            "UPDATE orders SET seller_agent_id = %s, delivery_agent_id = %s, total_minor = 18240,"
            " delivery_window = tstzrange(%s, %s) WHERE order_id = %s",
            (FRESH, SWIFT, window["start"], window["end"], seeded.order_id),
        )
        conn.execute(
            "UPDATE runs SET trigger = %s WHERE run_id = %s",
            (Jsonb({"user_id": str((starter or world.member).user_id)}), seeded.run_id),
        )
        for line, name in ((1, "Rice"), (2, "Beans")):
            conn.execute(
                "INSERT INTO order_items (order_id, line, sku, name, qty, unit_minor)"
                " VALUES (%s, %s, %s, %s, 1, 100)",
                (seeded.order_id, line, f"sku-{line}", name),
            )
        for seller in (FRESH, CORNER, LATE, PICKY, SILENT):
            message(
                conn,
                seeded,
                "quote_request",
                HOUSEHOLD_AGENT,
                seller,
                constraints=CONSTRAINTS,
            )
        # The counter overwrote Fresh Market's total; Corner Shop lost and stays `open`.
        quote(conn, seeded, FRESH, 18240, "accepted")
        quote(conn, seeded, CORNER, 19500, "open", lines=[LINE, MISSING])
        quote(conn, seeded, LATE, 21000, "expired")
        quote(conn, seeded, PICKY, 20500, "rejected")
        # A courier's quote: its buyer is the store.
        quote(conn, seeded, SWIFT, 500, "accepted", buyer=FRESH, constraints={})
        message(
            conn,
            seeded,
            "offer",
            FRESH,
            HOUSEHOLD_AGENT,
            round=0,
            total_minor=19190,
            currency="USD",
            rationale=RATIONALE,
        )
        message(
            conn,
            seeded,
            "offer",
            CORNER,
            HOUSEHOLD_AGENT,
            round=0,
            total_minor=19500,
            currency="USD",
        )
        message(conn, seeded, "counter_offer", HOUSEHOLD_AGENT, FRESH, total_minor=18240)
        message(
            conn, seeded, "offer", FRESH, HOUSEHOLD_AGENT, round=1, total_minor=18240, final=True
        )
        message(conn, seeded, "accept", HOUSEHOLD_AGENT, FRESH, total_minor=18240)
        for courier, fee in ((SLOW, 300), (SWIFT, 500)):
            message(
                conn,
                seeded,
                "delivery_quote",
                courier,
                FRESH,
                window=window,
                price_minor=fee,
                currency="USD",
            )
        message(conn, seeded, "delivery_booked", SWIFT, FRESH, delivery_agent_id=SWIFT)
        message(conn, seeded, "delivery_booked", SWIFT, HOUSEHOLD_AGENT, delivery_agent_id=SWIFT)
        conn.execute(
            "INSERT INTO approvals (approval_id, run_id, household_id, order_id, amount_minor,"
            " currency, status, requested_at, decided_at, decided_by)"
            " VALUES (%s, %s, %s, %s, 18240, 'USD', 'approved', %s, %s, %s)",
            (
                uuid.uuid4(),
                seeded.run_id,
                HOUSEHOLD,
                seeded.order_id,
                seeded.at,
                seeded.at + timedelta(minutes=2),
                world.admin.user_id,
            ),
        )
        trace(conn, seeded, "message", {"type": "offer"}, agent=FRESH)
        trace(conn, seeded, "message", {"type": "accept"})
        trace(
            conn,
            seeded,
            "decision",
            {"node": "choose_offer", "source": "model"},
            tokens=(100, 20),
            model="spark",
        )
        trace(conn, seeded, "decision", {"node": "price", "source": "rule_default"}, agent=FRESH)
        trace(conn, seeded, "decision", {"node": "negotiate"})
        spend_check(conn, seeded, True, "needs_approval")
        spend_check(conn, seeded, False, None)
    return seeded


def build(pg: str, order_id: UUID, household: str = HOUSEHOLD) -> Story:
    home = scope.HouseholdScope(household, "session")
    return aw.run(pg, lambda conn: build_story(scope.scoped(conn, home), order_id))


def check(built: Story, name: str) -> Any:
    return next(c for c in built.checks if c.name == name)


@db
def test_summary(world: World) -> None:
    summary = build(world.pg, seed_order(world.pg, world).order_id).summary
    assert (summary.title, summary.seller, summary.state) == (
        "Groceries",
        "Fresh Market",
        "courier_booked",
    )
    assert (summary.total, summary.budget) == ("$182.40", "$200.00")
    assert (summary.bar_value, summary.bar_max) == (18240, 20000)
    assert summary.window is not None and "Nov" in summary.window


@db
def test_who_we_asked(world: World) -> None:
    rows = build(world.pg, seed_order(world.pg, world).order_id).sellers
    assert [(r.name, r.outcome) for r in rows] == [
        ("Fresh Market", "Won"),
        ("Corner Shop", "Not chosen"),
        ("Late Foods", "No reply in time"),
        ("Picky Grocer", "Not chosen"),
        ("Silent Stores", "No offer"),
    ]
    fresh, corner, _, _, silent = rows
    # The first offer is the message's: the counter overwrote the quote's total.
    assert (fresh.first_offer, fresh.final_offer) == ("$191.90", "$182.40")
    assert (corner.first_offer, corner.final_offer, corner.unavailable) == ("$195.00", "$195.00", 1)
    assert (silent.first_offer, silent.final_offer) == (None, None)


@db
def test_open_quote_waits_while_the_order_is_being_decided(world: World) -> None:
    seeded = bare_order(world.pg, "negotiating")
    with psycopg.connect(world.pg) as conn:
        conn.execute(
            "INSERT INTO organizations (org_id, name, region) VALUES ('corner-us', 'Corner', 'us')"
        )
        w.add_agent(conn, CORNER, "org:corner-us")
        quote(conn, seeded, CORNER, 19500, "open")
    assert [r.outcome for r in build(world.pg, seeded.order_id).sellers] == ["Waiting"]


@db
def test_negotiation(world: World) -> None:
    turns = build(world.pg, seed_order(world.pg, world).order_id).turns
    assert [(t.who, t.action, t.amount) for t in turns] == [
        ("Fresh Market", "offered", "$191.90"),
        ("Corner Shop", "offered", "$195.00"),
        ("Your household agent", "countered", "$182.40"),
        ("Fresh Market", "came back with", "$182.40"),
        ("Your household agent", "accepted", "$182.40"),
    ]
    assert turns[0].rationale == RATIONALE
    assert [t.dot for t in turns] == ["ok", "ok", "attention", "ok", "ok"]


@db
def test_checks_in_code(world: World) -> None:
    built = build(world.pg, seed_order(world.pg, world).order_id)
    assert [c.name for c in built.checks] == ["Allergens", "Diet", "Budget", "Approval"]
    allergens, diet, budget, approval = built.checks
    # Codes as words, the union the order was quoted with.
    assert allergens.detail == "peanuts, tree nuts · 2 lines checked"
    assert allergens.result == "Passed"
    assert (diet.detail, diet.result) == ("high protein, vegetarian", "Passed")
    assert (budget.detail, budget.result) == ("$182.40 of $200.00 this week", "Passed")
    assert approval.result == "Approved"
    assert approval.detail.startswith("Approved by your household's approver, ")


@db
def test_allergen_refusal_shows(world: World) -> None:
    seeded = bare_order(world.pg, "failed")
    with psycopg.connect(world.pg) as conn:
        spend_check(conn, seeded, True, "allergen_conflict")
    allergens = check(build(world.pg, seeded.order_id), "Allergens")
    assert (allergens.result, allergens.result_pill) == ("Refused", "refused")


@db
def test_refused_offer_stays_visible_after_a_later_pass(world: World) -> None:
    seeded = bare_order(world.pg, "authorized")
    with psycopg.connect(world.pg) as conn:
        spend_check(conn, seeded, True, "allergen_conflict")
        spend_check(conn, seeded, False, None)
    allergens = check(build(world.pg, seeded.order_id), "Allergens")
    assert allergens.result == "Passed"
    assert "1 offer refused" in allergens.detail


@db
@pytest.mark.parametrize("reason", ["authz_denied", "quote_mismatch"])
def test_refusal_before_the_allergen_check_is_not_a_pass(world: World, reason: str) -> None:
    seeded = bare_order(world.pg, "negotiating")
    with psycopg.connect(world.pg) as conn:
        spend_check(conn, seeded, True, reason)
    assert check(build(world.pg, seeded.order_id), "Allergens").result == "Not run yet"


@db
def test_nothing_is_claimed_before_the_checks_ran(world: World) -> None:
    seeded = bare_order(world.pg, "quoting")
    with psycopg.connect(world.pg) as conn:
        message(
            conn,
            seeded,
            "quote_request",
            HOUSEHOLD_AGENT,
            FRESH,
            constraints=CONSTRAINTS,
        )
    built = build(world.pg, seeded.order_id)
    assert check(built, "Allergens").result == "Not run yet"
    assert check(built, "Diet").result == "Not run yet"
    assert check(built, "Approval").result == "Not run yet"
    assert built.summary.bar_max is None


@db
def test_diet_not_confirmed_when_the_quote_lacks_the_code(world: World) -> None:
    seeded = seed_order(world.pg, world)
    with psycopg.connect(world.pg) as conn:
        conn.execute(
            "UPDATE quotes SET constraints = %s WHERE order_id = %s AND seller_agent_id = %s",
            (Jsonb({"allergens": [], "diets": ["vegetarian"]}), seeded.order_id, FRESH),
        )
    diet = check(build(world.pg, seeded.order_id), "Diet")
    assert (diet.result, diet.result_pill) == ("Not confirmed", "attention")


@db
def test_budget_over_and_approval_not_needed(world: World) -> None:
    seeded = seed_order(world.pg, world)
    with psycopg.connect(world.pg) as conn:
        conn.execute("DELETE FROM approvals WHERE order_id = %s", (seeded.order_id,))
        conn.execute(
            "UPDATE orders SET total_minor = 25000 WHERE order_id = %s", (seeded.order_id,)
        )
    built = build(world.pg, seeded.order_id)
    assert check(built, "Budget").result == "Over budget"
    assert check(built, "Approval").result == "Not needed"
    # The bar stops at the budget.
    assert (built.summary.bar_value, built.summary.bar_max) == (20000, 20000)


@db
def test_delivery(world: World) -> None:
    couriers = build(world.pg, seed_order(world.pg, world).order_id).couriers
    assert [(c.name, c.fee, c.chosen) for c in couriers] == [
        ("Slow Couriers", "$3.00", False),
        ("Swift Couriers", "$5.00", True),
    ]
    assert all(c.window and "Nov" in c.window for c in couriers)


@db
def test_behind_the_scenes(world: World) -> None:
    seeded = seed_order(world.pg, world)
    behind = build(world.pg, seeded.order_id).behind
    assert (behind.agents, behind.messages) == (2, 2)
    assert (behind.model_decisions, behind.rule_decisions) == (1, 2)
    assert behind.tokens == 120
    assert behind.elapsed == "6 s"


@db
def test_no_member_names_or_agent_ids(world: World) -> None:
    text = repr(build(world.pg, seed_order(world.pg, world).order_id))
    for name in ("Maya", "Sam", "Kavi"):
        assert name not in text
    for agent in (HOUSEHOLD_AGENT, FRESH, SWIFT):
        assert agent not in text


@db
def test_unnamed_seller_shows_no_agent_id(world: World) -> None:
    seeded = bare_order(world.pg, "quoting")
    with psycopg.connect(world.pg) as conn:
        w.add_agent(conn, "grocery:no-org-us", "org:missing")
        message(conn, seeded, "quote_request", HOUSEHOLD_AGENT, "grocery:no-org-us")
    assert [r.name for r in build(world.pg, seeded.order_id).sellers] == ["A store"]


@db
def test_other_households_order_is_refused(world: World) -> None:
    seeded = seed_order(world.pg, world)
    with pytest.raises(LookupError):
        build(world.pg, seeded.order_id, "h-other")


@db
def test_messages_of_another_order_in_the_run_are_left_out(world: World) -> None:
    seeded = bare_order(world.pg, "quoting")
    with psycopg.connect(world.pg) as conn:
        other = Seeded(uuid.uuid4(), seeded.run_id, seeded.at)
        message(conn, other, "offer", FRESH, HOUSEHOLD_AGENT, round=0, total_minor=100)
    assert build(world.pg, seeded.order_id).turns == []


@db
def test_build_writes_nothing(world: World) -> None:
    """SG6: no new trace rows."""
    seeded = seed_order(world.pg, world)
    tables = ("agent_traces", "agent_messages", "quotes", "orders", "approvals")

    def counts() -> list[int]:
        return [aw.query(world.pg, f"SELECT count(*) AS n FROM {t}")[0]["n"] for t in tables]

    before = counts()
    build(world.pg, seeded.order_id)
    assert counts() == before


@db
def test_without_spending(world: World) -> None:
    hidden = without_spending(build(world.pg, seed_order(world.pg, world).order_id))
    text = repr(hidden)
    assert "$" not in text
    assert RATIONALE not in text
    assert [c.name for c in hidden.checks] == ["Allergens", "Diet"]
    assert hidden.spending_hidden
    # The shape of the story stays: who was asked, who won, who delivers.
    assert [r.outcome for r in hidden.sellers][0] == "Won"
    assert len(hidden.turns) == 5
    assert hidden.couriers[1].chosen


def test_elapsed_words() -> None:
    assert story_module._elapsed(14.9) == "14 s"
    assert story_module._elapsed(134) == "2 min 14 s"
