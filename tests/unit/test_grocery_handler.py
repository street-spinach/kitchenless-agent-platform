"""The grocery handler under the negotiation state machine (agent-protocol §7, P23; issue
#56) and the shortfall (agent-protocol §10, P23; issue #69).

Each test runs real steps through the consumer (claim, `protocol.check`, handler, final
transaction) against Postgres, with stub choices and a fake seller MCP server.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from services.agents.common.seller import PgSellerStore, SellerHandler
from services.common import consumer as c
from services.common import ids, orders, runs
from services.common.trace import TraceStore
from tests.unit import model_fakes as mf
from tests.unit import seller_fakes as sf

pytestmark = pytest.mark.db

GROCERY = sf.GROCERY
HOUSEHOLD = sf.HOUSEHOLD
EXTERNAL = "external:a2a-demo"
COURIERS = ("delivery:fast-us", "delivery:mock-us")
SWEEPER = "system:sweeper"
MILK = "MOCK-MILK-WHOLE"


class World:
    """One run with an order, the seller, two couriers and the A2A demo card."""

    def __init__(
        self,
        pg: str,
        *,
        kind: str = "weekly",
        overrides: dict[str, Any] | None = None,
        answers: dict[str, Any] | None = None,
    ) -> None:
        self.pg = pg
        self.run_id = uuid.uuid4()
        self.order_id = uuid.uuid4()
        self.buyer = EXTERNAL if kind == "a2a_demo" else HOUSEHOLD
        self.household = "a2a-demo" if kind == "a2a_demo" else "us-1"
        self.seller = sf.FakeSeller()
        self.stubs = sf.RecordingStubs(answers)
        self.traces = mf.FakeTraces()
        address = uuid.uuid4()
        trigger: dict[str, Any] = {"source": "test"}
        if overrides:
            trigger["overrides"] = overrides
        with psycopg.connect(pg) as conn:
            conn.execute(
                "INSERT INTO households (household_id, name, env, region, zone_id, timezone,"
                " currency) VALUES (%s, %s, 'eval', 'us', %s, 'America/New_York', 'USD')",
                (self.household, self.household, sf.ZONE),
            )
            cards = [
                (HOUSEHOLD, "household", "household:us-1", "agents"),
                (GROCERY, "grocery", "org:mock", "agents"),
                (EXTERNAL, "external", "system", "external"),
                *((cid, "delivery", "org:courier", "agents") for cid in COURIERS),
            ]
            for agent_id, cls, owner, host in cards:
                conn.execute(
                    "INSERT INTO agent_cards (agent_id, class, owner_ref, region, zone_ids,"
                    " currency, host_process) VALUES (%s, %s, %s, 'us', %s, 'USD', %s)",
                    (agent_id, cls, owner, [sf.ZONE], host),
                )
            conn.execute(
                "INSERT INTO business_rules (agent_id, kind, value) VALUES (%s, 'floors', %s)",
                (GROCERY, Jsonb(sf.FLOORS)),
            )
            conn.execute(
                "INSERT INTO household_addresses (address_ref, household_id, line1, city,"
                " region, postal_code, country) VALUES (%s, %s, '1 Main', 'Cincinnati',"
                " 'OH', '45202', 'US')",
                (address, self.household),
            )
            conn.execute(
                "INSERT INTO runs (run_id, household_id, kind, trigger, modes)"
                " VALUES (%s, %s, %s, %s, '{}')",
                (self.run_id, self.household, kind, Jsonb(trigger)),
            )
            conn.execute(
                "INSERT INTO orders (order_id, run_id, household_id, kind, seller_agent_id,"
                " state, currency, address_ref) VALUES (%s, %s, %s, 'grocery', %s,"
                " 'negotiating', 'USD', %s)",
                (self.order_id, self.run_id, self.household, GROCERY, address),
            )
        self._n = 0

    # Database helpers --------------------------------------------------------------------

    def query(self, q: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with psycopg.connect(self.pg) as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(q, params)
            return cur.fetchall()

    def execute(self, q: str, params: tuple[Any, ...] = ()) -> None:
        with psycopg.connect(self.pg) as conn:
            conn.execute(q, params)

    def quote(self) -> dict[str, Any]:
        """The seller's latest quote."""
        [row] = self.query(
            "SELECT * FROM quotes WHERE seller_agent_id = %s ORDER BY created_at DESC LIMIT 1",
            (GROCERY,),
        )
        return row

    def sent(self, type: str, cause: uuid.UUID | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM agent_messages WHERE type = %s AND from_agent = %s"
        params: tuple[Any, ...] = (type, GROCERY)
        if cause is not None:
            q += " AND causation_id = %s"
            params += (cause,)
        return self.query(q + " ORDER BY message_id", params)

    def message(self, message_id: uuid.UUID) -> dict[str, Any]:
        [row] = self.query("SELECT * FROM agent_messages WHERE message_id = %s", (message_id,))
        return row

    def expire(self) -> None:
        self.execute(
            "UPDATE quotes SET expires_at = now() - interval '1 second' WHERE quote_id = %s",
            (self.quote()["quote_id"],),
        )

    # Steps -------------------------------------------------------------------------------

    def insert(
        self,
        type: str,
        body: dict[str, Any],
        *,
        from_agent: str | None = None,
        conversation_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        self._n += 1
        message_id = uuid.uuid4()
        self.execute(
            "INSERT INTO agent_messages (message_id, run_id, conversation_id, from_agent,"
            " to_agent, to_host, type, body, created_at) VALUES (%s, %s, %s, %s, %s, 'agents',"
            " %s, %s, now() - make_interval(secs => %s))",
            (
                message_id,
                self.run_id,
                conversation_id,
                from_agent or self.buyer,
                GROCERY,
                type,
                Jsonb({"schema_version": 1, **body}),
                1000 - self._n,
            ),
        )
        return message_id

    def run_steps(self) -> None:
        async def go() -> None:
            pool = AsyncConnectionPool(self.pg, min_size=1, max_size=5, open=False)
            await pool.open()
            try:
                handler = SellerHandler(
                    "grocery",
                    model=sf.chooser(self.stubs, self.traces),
                    traces=TraceStore(pool),
                    store=PgSellerStore(pool),
                    call_tool=self.seller,
                    offer_ttl_seconds=600,
                    shortfall_reply_seconds=900,
                )

                async def handle(msg: c.Inbound) -> c.StepResult:
                    return await handler(msg, sf.CARD)

                consumer = c.Consumer(pool, "agents", handle, finish_run=runs.finish)
                claimed = await consumer.claim()
                await asyncio.gather(*(consumer.start(rec) for rec in claimed))
            finally:
                await pool.close()

        asyncio.run(go())

    def step(self, type: str, body: dict[str, Any], **kw: Any) -> uuid.UUID:
        message_id = self.insert(type, body, **kw)
        self.run_steps()
        return message_id

    def request(self, items: list[tuple[str, int]] | None = None, **extra: Any) -> uuid.UUID:
        body = sf.request_body(self.order_id, items or [("whole milk", 10)], **extra)
        self.conversation_id = uuid.uuid4()
        return self.step("quote_request", body, conversation_id=self.conversation_id)

    def decision_body(self, **extra: Any) -> dict[str, Any]:
        q = self.quote()
        return {"order_id": str(self.order_id), "quote_id": str(q["quote_id"]), **extra}

    def counter(self, total: int) -> uuid.UUID:
        q = self.quote()
        body = self.decision_body(
            round=q["round"], lines=q["items"]["lines"], total_minor=total, currency="USD"
        )
        return self.step("counter_offer", body, conversation_id=q["conversation_id"])

    def items_hash(self) -> str:
        """The hash the household's `authorize_spend` computes from the offered lines."""
        lines = [
            {"sku": line["offered_sku"], "qty": line["qty"], "unit_minor": line["unit_minor"]}
            for line in self.quote()["items"]["lines"]
            if line["offered_sku"]
        ]
        return orders.items_hash(lines)

    def authorize(self) -> None:
        self.execute(
            "UPDATE orders SET state = 'authorized', items_hash = %s, total_minor = %s"
            " WHERE order_id = %s",
            (self.items_hash(), self.quote()["total_minor"], self.order_id),
        )

    def accept(self, *, items_hash: str | None = None, total: int | None = None) -> uuid.UUID:
        q = self.quote()
        body = self.decision_body(
            items_hash=items_hash or self.items_hash(),
            total_minor=q["total_minor"] if total is None else total,
        )
        return self.step("accept", body, conversation_id=q["conversation_id"])

    def close_round(self) -> uuid.UUID:
        body = {"order_id": str(self.order_id), "round": "delivery"}
        return self.step("quote_round_closed", body, from_agent=SWEEPER)


def _floor(quote: dict[str, Any]) -> int:
    return sum(line["qty"] * sf.FLOORS[line["offered_sku"]] for line in quote["items"]["lines"])


def _decision_rows(w: World, node: str) -> list[dict[str, Any]]:
    return [r for r in w.traces.of("decision") if r["node"] == node]


# Counters ---------------------------------------------------------------------------------


def test_counter_agreed_sets_countered_and_final_offer(pg: str) -> None:
    w = World(pg)
    w.request()
    first = w.quote()
    buyer = first["total_minor"] - 10
    assert buyer >= _floor(first)
    mid = w.counter(buyer)
    q = w.quote()
    assert (q["status"], q["round"], q["total_minor"]) == ("countered", 1, buyer)
    assert q["expires_at"] > first["expires_at"] - timedelta(seconds=1)
    [offer] = w.sent("offer", cause=mid)
    assert w.message(mid)["status"] == "processed"
    assert offer["body"]["final"] is True and offer["body"]["round"] == 1
    assert offer["body"]["total_minor"] == buyer
    assert datetime.fromisoformat(offer["body"]["expires_at"]) == q["expires_at"]


def test_second_counter_allows_only_agree_or_reject(pg: str) -> None:
    w = World(pg, answers={"respond_to_counter": {"action": "counter", "arg": 4100}})
    w.request()
    total = w.quote()["total_minor"]
    w.counter(total - 20)  # round 0: the model's counter at 4100 is allowed
    assert (w.quote()["status"], w.quote()["round"]) == ("countered", 1)
    assert w.quote()["total_minor"] == 4100
    mid = w.counter(4090)  # round 1: counter is no longer allowed; the default agrees
    first, second = _decision_rows(w, "respond_to_counter")
    assert set(first["allowed"]["actions"]) == {"reject", "agree", "counter"}
    assert set(second["allowed"]["actions"]) == {"reject", "agree"}
    assert second["source"] == "rule_default" and second["reason"] == "out_of_bounds"
    q = w.quote()
    assert (q["status"], q["round"], q["total_minor"]) == ("countered", 2, 4090)
    [offer] = w.sent("offer", cause=mid)
    assert offer["body"]["final"] is True


def test_counter_on_expired_quote_rejected(pg: str) -> None:
    w = World(pg)
    w.request()
    w.expire()
    total = w.quote()["total_minor"]
    first = w.counter(total - 10)
    assert w.quote()["status"] == "expired"
    [reject] = w.sent("reject", cause=first)
    assert reject["body"]["reason"] == "expired"
    second = w.counter(total - 10)
    [again] = w.sent("reject", cause=second)
    assert again["body"]["reason"] == "expired"
    assert w.quote()["status"] == "expired"


# Accept -----------------------------------------------------------------------------------


def test_accept_sets_accepted_once(pg: str) -> None:
    w = World(pg)
    w.request()
    w.authorize()
    mid = w.accept()
    assert w.message(mid)["status"] == "processed"
    assert w.quote()["status"] == "accepted"
    requests = w.sent("delivery_quote_request", cause=mid)
    assert sorted(r["to_agent"] for r in requests) == sorted(COURIERS)
    assert {r["message_id"] for r in requests} == {ids.message_id(mid, n) for n in (0, 1)}
    [timer] = w.query("SELECT * FROM timers WHERE run_id = %s", (w.run_id,))
    assert timer["kind"] == "quote_round"
    assert timer["ref"] == {"order_id": str(w.order_id), "round": "delivery", "to": GROCERY}


def test_accept_after_expiry_rejects_expired(pg: str) -> None:
    w = World(pg)
    w.request()
    w.authorize()
    w.expire()
    mid = w.accept()
    assert w.quote()["status"] == "expired"
    [reject] = w.sent("reject", cause=mid)
    assert reject["body"]["reason"] == "expired"
    assert w.sent("delivery_quote_request") == []
    assert w.query("SELECT 1 FROM timers WHERE run_id = %s", (w.run_id,)) == []


def test_redelivered_accept_resends_same_ids(pg: str) -> None:
    w = World(pg, overrides={c.CRASH_BEFORE_FINISH: "accept"})
    w.request()
    w.authorize()
    mid = w.accept()  # the first attempt crashes after the handler, before the commit
    row = w.message(mid)
    assert row["status"] == "pending" and row["last_error"].startswith("InjectedFailure")
    assert w.quote()["status"] == "open"
    assert w.sent("delivery_quote_request") == []
    w.execute("UPDATE agent_messages SET available_at = now() WHERE message_id = %s", (mid,))
    w.run_steps()
    assert w.message(mid)["status"] == "processed"
    assert w.quote()["status"] == "accepted"
    requests = w.sent("delivery_quote_request", cause=mid)
    assert {r["message_id"] for r in requests} == {ids.message_id(mid, n) for n in (0, 1)}
    assert len(w.query("SELECT 1 FROM timers WHERE run_id = %s", (w.run_id,))) == 1


def test_second_accept_with_new_id_ignored(pg: str) -> None:
    w = World(pg)
    w.request()
    w.authorize()
    w.accept()
    second = w.accept()
    assert w.message(second)["status"] == "processed"
    assert w.query("SELECT 1 FROM agent_messages WHERE causation_id = %s", (second,)) == []
    assert len(w.query("SELECT 1 FROM timers WHERE run_id = %s", (w.run_id,))) == 1
    [trace] = w.query(
        "SELECT payload FROM agent_traces WHERE run_id = %s AND kind = 'decision'"
        " AND payload->>'node' = 'accept'",
        (w.run_id,),
    )
    assert trace["payload"]["choice"] == "duplicate_accept"


def test_accept_items_hash_mismatch_rejected(pg: str) -> None:
    w = World(pg)
    w.request()
    w.authorize()
    mid = w.accept(items_hash="0" * 64)
    [reject] = w.sent("reject", cause=mid)
    assert reject["body"]["reason"] == "items_changed"
    assert w.quote()["status"] == "open"
    assert w.sent("delivery_quote_request") == []


def test_accept_total_mismatch_rejected_price(pg: str) -> None:
    w = World(pg)
    w.request()
    w.authorize()
    mid = w.accept(total=w.quote()["total_minor"] - 1)
    [reject] = w.sent("reject", cause=mid)
    assert reject["body"]["reason"] == "price"
    assert w.quote()["status"] == "open"


# Cancel, reject and the courier round ----------------------------------------------------


def test_cancel_after_placed_rejected(pg: str) -> None:
    w = World(pg)
    w.request()
    w.authorize()
    w.accept()
    w.execute("UPDATE orders SET state = 'placed' WHERE order_id = %s", (w.order_id,))
    mid = w.step(
        "cancel",
        w.decision_body(reason="buyer_cancelled"),
        conversation_id=w.quote()["conversation_id"],
    )
    [reject] = w.sent("reject", cause=mid)
    assert reject["body"]["reason"] == "already_placed"
    assert w.quote()["status"] == "accepted"


def test_cancel_after_approval_expired_closes_quote(pg: str) -> None:
    # CUJ 3 (#63): the sweeper set the order `expired`; the household's `cancel` closes the
    # open quote instead of answering `already_placed`.
    w = World(pg)
    w.request()
    w.execute("UPDATE orders SET state = 'expired' WHERE order_id = %s", (w.order_id,))
    mid = w.step(
        "cancel",
        w.decision_body(reason="buyer_cancelled"),
        conversation_id=w.quote()["conversation_id"],
    )
    assert w.sent("reject", cause=mid) == []
    q = w.quote()
    assert (q["status"], q["closed_by"]) == ("rejected", "buyer")


def test_no_courier_rejects_accepted_quote(pg: str) -> None:
    w = World(pg)
    w.request()
    w.authorize()
    w.accept()
    mid = w.close_round()
    q = w.quote()
    assert (q["status"], q["closed_by"]) == ("rejected", "seller")
    [reject] = w.sent("reject", cause=mid)
    assert reject["body"]["reason"] == "no_courier"
    assert reject["to_agent"] == HOUSEHOLD
    assert reject["conversation_id"] == q["conversation_id"]
    assert w.seller.of("place_order") == []


def test_closed_by_recorded(pg: str) -> None:
    w = World(pg, answers={"respond_to_counter": {"action": "reject"}})
    w.request()
    w.step("reject", w.decision_body(reason="other"), conversation_id=w.quote()["conversation_id"])
    assert (w.quote()["status"], w.quote()["closed_by"]) == ("rejected", "buyer")
    # A second negotiation in the run: the seller refuses the counter.
    w.request()
    mid = w.counter(w.quote()["total_minor"] - 10)
    assert (w.quote()["status"], w.quote()["closed_by"]) == ("rejected", "seller")
    [reject] = w.sent("reject", cause=mid)
    assert reject["body"]["reason"] == "price"


# quote_request ---------------------------------------------------------------------------


def test_offer_expires_at_equals_quote_row(pg: str) -> None:
    w = World(pg)
    before = datetime.now(UTC)
    mid = w.request()
    q = w.quote()
    [offer] = w.sent("offer", cause=mid)
    assert datetime.fromisoformat(offer["body"]["expires_at"]) == q["expires_at"]
    assert before + timedelta(seconds=600) <= q["expires_at"]
    assert q["expires_at"] <= datetime.now(UTC) + timedelta(seconds=600)
    assert q["quote_id"] == ids.quote_id(mid)
    assert (q["status"], q["round"], q["buyer_agent_id"]) == ("open", 0, HOUSEHOLD)
    assert offer["body"]["final"] is False
    assert offer["conversation_id"] == w.conversation_id


def test_quote_request_saves_constraints(pg: str) -> None:
    w = World(pg)
    w.request(constraints={"allergens": ["tree_nuts", "peanuts"], "diets": ["vegetarian"]})
    assert w.quote()["constraints"] == {
        "allergens": ["peanuts", "tree_nuts"],
        "diets": ["vegetarian"],
    }


def test_a2a_rejected_quote_finishes_run_cancelled(pg: str) -> None:
    w = World(pg, kind="a2a_demo")
    w.request()
    [offer] = w.sent("offer")
    assert offer["to_agent"] == EXTERNAL and offer["status"] == "processed"
    w.step(
        "reject",
        w.decision_body(reason="other"),
        conversation_id=w.quote()["conversation_id"],
    )
    q = w.quote()
    assert (q["status"], q["closed_by"]) == ("rejected", "buyer")
    [run] = w.query("SELECT status, reason FROM runs WHERE run_id = %s", (w.run_id,))
    assert (run["status"], run["reason"]) == ("cancelled", "a2a_demo_closed")


# The shortfall (agent-protocol §10, P23; #69) ---------------------------------------------

API = "system:api"
MILK_2PCT = "MOCK-MILK-2PCT"
ALMOND = "MOCK-MILK-ALMOND"
EGGS = "MOCK-EGGS-12"


def _courier_booked(w: World, items: list[tuple[str, int]], allergens: list[str]) -> None:
    """The order placed from the accepted quote, its lines stored, the courier booked."""
    w.request(items, constraints={"allergens": allergens, "diets": []})
    w.authorize()
    w.accept()
    assert w.quote()["status"] == "accepted"
    w.execute("UPDATE orders SET state = 'courier_booked' WHERE order_id = %s", (w.order_id,))
    for line in w.quote()["items"]["lines"]:
        w.execute(
            "INSERT INTO order_items (order_id, line, sku, name, qty, unit_minor)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (
                w.order_id,
                line["line"],
                line["offered_sku"],
                line["offered_name"],
                line["qty"],
                line["unit_minor"],
            ),
        )


def _detect(w: World, skus: list[str]) -> uuid.UUID:
    body = {"order_id": str(w.order_id), "skus": skus, "event_id": str(uuid.uuid4())}
    return w.step("shortfall_detected", body, from_agent=API)


def test_shortfall_substitutes_filtered_and_within_line_total(pg: str) -> None:
    # The order line is 2 x 2% milk (399). The broader "milk" search finds whole milk
    # (429, above the line's unit price) and almond milk (379, tree nuts).
    w = World(pg)
    _courier_booked(w, [("reduced fat milk", 2)], ["tree_nuts"])
    before = datetime.now(UTC)
    mid = _detect(w, [MILK_2PCT])

    [notice] = w.sent("shortfall_notice", cause=mid)
    shortfall_id = ids.shortfall_id(w.order_id)
    assert notice["to_agent"] == HOUSEHOLD
    assert notice["conversation_id"] == ids.shortfall_conversation_id(shortfall_id)
    body = notice["body"]
    assert body["shortfall_id"] == str(shortfall_id)
    [line] = body["lines"]
    assert (line["line"], line["sku"], line["qty_missing"], line["line_total_minor"]) == (
        0,
        MILK_2PCT,
        2,
        2 * 399,
    )
    # Almond milk is filtered by the household's codes, whole milk by the line total: the
    # line is offered as drop only.
    assert line["substitutes"] == []
    reply_by = datetime.fromisoformat(body["reply_by"])
    assert before + timedelta(seconds=900) <= reply_by
    assert reply_by <= datetime.now(UTC) + timedelta(seconds=900)
    # The search carries the codes saved on the accepted quote, never a profile.
    search = w.seller.of("search_items")[-1]
    assert search["args"]["query"] == "milk"
    assert search["args"]["filters"]["allergen_codes"] == ["tree_nuts"]
    ranks = [inputs for point, inputs in w.stubs.seen if point == "rank_substitutes"]
    assert ranks[-1]["message"]["type"] == "shortfall_detected"


def test_shortfall_substitute_offered_within_line_total(pg: str) -> None:
    # Without the tree-nut code almond milk is offered; whole milk is still too dear. The
    # run override `SHORTFALL_REPLY_SECONDS` sets `reply_by`.
    w = World(pg, overrides={"SHORTFALL_REPLY_SECONDS": 3})
    _courier_booked(w, [("reduced fat milk", 2)], [])
    before = datetime.now(UTC)
    mid = _detect(w, [MILK_2PCT])
    [notice] = w.sent("shortfall_notice", cause=mid)
    [line] = notice["body"]["lines"]
    assert line["substitutes"] == [
        {"sku": ALMOND, "name": "Almond milk 64 oz", "qty": 2, "unit_minor": 379}
    ]
    for sub in line["substitutes"]:
        assert sub["qty"] * sub["unit_minor"] <= line["line_total_minor"]
    reply_by = datetime.fromisoformat(notice["body"]["reply_by"])
    assert before + timedelta(seconds=3) <= reply_by <= datetime.now(UTC) + timedelta(seconds=3)


def _closed_shortfall(w: World, picks: dict[str, str]) -> uuid.UUID:
    shortfall_id = ids.shortfall_id(w.order_id)
    substitute = {"sku": ALMOND, "name": "Almond milk 64 oz", "qty": 2, "unit_minor": 379}
    notice = [
        {
            "line": 0,
            "sku": MILK_2PCT,
            "name": "Reduced fat 2% milk 1 gal",
            "qty_missing": 2,
            "line_total_minor": 798,
            "substitutes": [substitute],
        }
    ]
    w.execute(
        "INSERT INTO shortfalls (shortfall_id, run_id, order_id, seller_agent_id, lines, picks,"
        " status, due_at, closed_at) VALUES (%s, %s, %s, %s, %s, %s, 'closed', now(), now())",
        (shortfall_id, w.run_id, w.order_id, GROCERY, Jsonb(notice), Jsonb(picks)),
    )
    return shortfall_id


def _decide(w: World, shortfall_id: uuid.UUID, picks: dict[str, str]) -> uuid.UUID:
    body = {"order_id": str(w.order_id), "shortfall_id": str(shortfall_id), "picks": picks}
    return w.step(
        "shortfall_decision",
        body,
        from_agent=HOUSEHOLD,
        conversation_id=ids.shortfall_conversation_id(shortfall_id),
    )


def test_shortfall_decision_amends_once(pg: str) -> None:
    w = World(pg)
    _courier_booked(w, [("reduced fat milk", 2), ("large eggs", 1)], [])
    picks = {"0": ALMOND}
    shortfall_id = _closed_shortfall(w, picks)
    amended_lines = [
        {
            "line": 0,
            "sku": ALMOND,
            "name": "Almond milk 64 oz",
            "qty": 2,
            "unit_minor": 379,
            "substituted_for": MILK_2PCT,
        },
        {
            "line": 1,
            "sku": EGGS,
            "name": "Large eggs 12 ct",
            "qty": 1,
            "unit_minor": 349,
            "substituted_for": None,
        },
    ]
    w.seller.amend_result = {
        "order_id": str(w.order_id),
        "shortfall_id": str(shortfall_id),
        "state": "courier_booked",
        "picks": picks,
        "refused": [],
        "lines": amended_lines,
        "total_minor": 2 * 379 + 349,
        "currency": "USD",
    }
    mid = _decide(w, shortfall_id, picks)

    # One `amend_order` call carries every pick.
    [call] = w.seller.of("amend_order")
    assert call["args"] == {
        "order_id": str(w.order_id),
        "shortfall_id": str(shortfall_id),
        "picks": picks,
    }
    [confirm] = w.sent("order_confirm", cause=mid)
    assert confirm["to_agent"] == HOUSEHOLD
    assert confirm["conversation_id"] == ids.shortfall_conversation_id(shortfall_id)
    assert confirm["body"]["state"] == "courier_booked"
    assert confirm["body"]["lines"] == amended_lines
    assert confirm["body"]["total_minor"] == 2 * 379 + 349
    assert "reason" not in confirm["body"]


def test_shortfall_every_line_dropped_confirms_failed(pg: str) -> None:
    w = World(pg)
    _courier_booked(w, [("reduced fat milk", 2)], [])
    shortfall_id = _closed_shortfall(w, {})
    w.seller.amend_result = {"state": "failed", "lines": [], "total_minor": 0, "currency": "USD"}
    mid = _decide(w, shortfall_id, {"0": "drop"})
    assert len(w.seller.of("amend_order")) == 1
    [failed] = w.sent("order_confirm", cause=mid)
    assert (failed["body"]["state"], failed["body"]["reason"]) == ("failed", "nothing_to_deliver")
    assert failed["body"]["lines"] == []


def test_shortfall_decision_refused_changes_nothing(pg: str) -> None:
    w = World(pg)
    _courier_booked(w, [("reduced fat milk", 2)], [])
    shortfall_id = _closed_shortfall(w, {"0": ALMOND})
    w.seller.refuse_amend = "amendment_not_authorized"
    mid = _decide(w, shortfall_id, {"0": ALMOND})
    assert len(w.seller.of("amend_order")) == 1
    assert w.sent("order_confirm", cause=mid) == []
    [trace] = w.query(
        "SELECT payload FROM agent_traces WHERE run_id = %s AND kind = 'decision'"
        " AND payload->>'node' = 'shortfall_decision'",
        (w.run_id,),
    )
    assert trace["payload"]["choice"] == "amend_refused_amendment_not_authorized"
