"""Mock control: reset scope, shortfall events (architecture item 47 and §7 reset scope,
amended 2026-11-02; agent-protocol P33; issue #50)."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from services.common import ids
from services.common.config import Settings
from services.mocks import control
from services.seed import MOCK_STOCK_QTY, mock_stock_rows, seeded_limits
from services.seed import __main__ as seed_mod

LIVE = "us-1"
EVAL = "us-1-eval"
GROCERY = "grocery:mock-us"
US1_PHONES = ["+15550100201", "+15550100202", "+15550100203"]
LIVE_PHONE = "+15550100001"  # us-2-a
EVAL_PHONE = "+15550100101"  # us-1-a-eval
# a2a-demo has no members, so the seed writes no household card for it.
BUYERS = {LIVE: f"household:{LIVE}", EVAL: f"household:{EVAL}", "a2a-demo": "external:a2a-demo"}

# Tables the eval reset deletes from, and the ones it must never touch.
RUN_TABLES = (
    "runs",
    "orders",
    "order_items",
    "quotes",
    "approvals",
    "timers",
    "agent_messages",
    "idempotency_keys",
    "shortfalls",
    "pantry_events",
)
MOCK_TABLES = ("mock_orders", "mock_deliveries", "mock_events")


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    env = {
        "SERVICE_NAME": "migrate",
        "DATABASE_URL": "x",
        "LOG_LEVEL": "info",
        "OPENFGA_PRESHARED_KEY": "x",
        "OPENFGA_API_URL": "x",
    }
    settings = Settings.from_env(env)
    monkeypatch.setattr(control, "get_settings", lambda: settings)
    return settings


def _run[T](pg: str, fn: Callable[[psycopg.AsyncConnection], Awaitable[T]]) -> T:
    async def go() -> T:
        async with await psycopg.AsyncConnection.connect(pg) as conn:
            return await fn(conn)

    return asyncio.run(go())


def _count(conn: psycopg.Connection, sql: str, *params: Any) -> int:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return int(row[0])


def _add_run(
    conn: psycopg.Connection, household_id: str | None, *, shortfall: bool = True
) -> dict[str, Any]:
    """One run with a row in every per-run table; returns its ids."""
    run_id, order_id = uuid.uuid4(), uuid.uuid4()
    kind = "weekly" if household_id else "a2a_demo"
    conn.execute(
        "INSERT INTO runs (run_id, household_id, kind, trigger, modes) VALUES (%s, %s, %s, %s, %s)",
        (run_id, household_id, kind, Jsonb({"source": "test"}), Jsonb({})),
    )
    conn.execute(
        "INSERT INTO agent_messages (message_id, run_id, from_agent, to_agent, to_host, type,"
        " body) VALUES (%s, %s, 'system:api', %s, 'agents', 'test', '{}')",
        (uuid.uuid4(), run_id, GROCERY),
    )
    conn.execute(
        "INSERT INTO agent_traces (run_id, seq, agent_id, kind, payload)"
        " VALUES (%s, 0, 'system:api', 'outcome', '{}')",
        (run_id,),
    )
    conn.execute(
        "INSERT INTO inbound_messages (channel, external_id, run_id) VALUES ('whatsapp', %s, %s)",
        (f"wamid.{run_id}", run_id),
    )
    if household_id is None:
        return {"run_id": run_id}

    conn.execute(
        "INSERT INTO orders (order_id, run_id, household_id, kind, seller_agent_id, state,"
        " currency, address_ref) VALUES (%s, %s, %s, 'grocery', %s, 'courier_booked', 'USD', %s)",
        (order_id, run_id, household_id, GROCERY, ids.seed_address_ref(household_id)),
    )
    for line, sku in enumerate(("MOCK-MILK-WHOLE", "MOCK-EGGS-12", "MOCK-RICE-2LB"), start=1):
        conn.execute(
            "INSERT INTO order_items (order_id, line, sku, name, qty, unit_minor)"
            " VALUES (%s, %s, %s, %s, 1, 100)",
            (order_id, line, sku, sku),
        )
    conn.execute(
        "INSERT INTO quotes (quote_id, run_id, order_id, conversation_id, seller_agent_id,"
        " buyer_agent_id, items, total_minor, currency, expires_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, '[]', 300, 'USD', now())",
        (uuid.uuid4(), run_id, order_id, uuid.uuid4(), GROCERY, BUYERS[household_id]),
    )
    approval_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO approvals (approval_id, run_id, household_id, order_id, amount_minor,"
        " currency) VALUES (%s, %s, %s, %s, 300, 'USD')",
        (approval_id, run_id, household_id, order_id),
    )
    cur = conn.execute(
        "INSERT INTO timers (kind, run_id, due_at, ref) VALUES ('approval_expiry', %s, %s, %s)"
        " RETURNING timer_id",
        (
            run_id,
            datetime.now(UTC) + timedelta(hours=1),
            Jsonb({"approval_id": str(approval_id), "order_id": str(order_id)}),
        ),
    )
    row = cur.fetchone()
    assert row is not None
    conn.execute(
        "INSERT INTO idempotency_keys (key, run_id, request_hash, status)"
        " VALUES (%s, %s, 'h', 'done')",
        (ids.idempotency_key(order_id, "place_order", GROCERY), run_id),
    )
    if shortfall:
        conn.execute(
            "INSERT INTO shortfalls (shortfall_id, run_id, order_id, seller_agent_id, lines,"
            " due_at) VALUES (%s, %s, %s, %s, '[]', now())",
            (uuid.uuid4(), run_id, order_id, GROCERY),
        )
    conn.execute(
        "INSERT INTO pantry_events (household_id, item, delta, unit, source, order_id)"
        " VALUES (%s, 'milk', 1, 'gal', 'order', %s)",
        (household_id, order_id),
    )
    conn.execute(
        "INSERT INTO sim_outbound (phone_e164, run_id, body) VALUES ('+15550109999', %s, '{}')",
        (run_id,),
    )
    return {
        "run_id": run_id,
        "order_id": order_id,
        "approval_id": approval_id,
        "timer_id": row[0],
    }


def _add_mock_rows(conn: psycopg.Connection, env: str) -> None:
    conn.execute(
        "INSERT INTO mock_orders (external_order_id, env, agent_id, idempotency_key, body,"
        " state) VALUES (%s, %s, %s, %s, '{}', 'placed')",
        (f"mo-{env}", env, GROCERY, f"key-mo-{env}"),
    )
    conn.execute(
        "INSERT INTO mock_deliveries (external_delivery_id, env, agent_id, idempotency_key,"
        ' zone_id, "window", state) VALUES (%s, %s, %s, %s, %s, tstzrange(now(), now() +'
        " interval '1 hour'), 'booked')",
        (f"md-{env}", env, "delivery:mock-us", f"key-md-{env}", "us-oh-45202"),
    )
    conn.execute(
        "INSERT INTO mock_events (env, kind, target_agent_id, params)"
        " VALUES (%s, 'out_of_stock', %s, %s)",
        (env, GROCERY, Jsonb({"sku": "MOCK-EGGS-12"})),
    )
    conn.execute(
        "UPDATE mock_stock SET qty = 5 WHERE env = %s AND agent_id = %s AND sku = 'MOCK-EGGS-12'",
        (env, GROCERY),
    )


@pytest.fixture
def world(pg: str) -> dict[str, Any]:
    with psycopg.connect(pg) as conn:
        seed_mod.seed(conn, "all", us1_phones=US1_PHONES)
        live = _add_run(conn, LIVE)
        eval_run = _add_run(conn, EVAL)
        demo = _add_run(conn, "a2a-demo", shortfall=False)
        orphan = _add_run(conn, None)
        for env in ("live", "eval"):
            _add_mock_rows(conn, env)
        for phone in (LIVE_PHONE, EVAL_PHONE):
            conn.execute("INSERT INTO sim_outbound (phone_e164, body) VALUES (%s, '{}')", (phone,))
    return {"pg": pg, "live": live, "eval": eval_run, "demo": demo, "orphan": orphan}


def _run_rows(conn: psycopg.Connection, run_id: uuid.UUID) -> dict[str, int]:
    counts = {}
    for table in RUN_TABLES:
        if table in ("order_items", "pantry_events"):
            sql = f"SELECT count(*) FROM {table} WHERE order_id IN"  # noqa: S608
            sql += " (SELECT order_id FROM orders WHERE run_id = %s)"
        else:
            sql = f"SELECT count(*) FROM {table} WHERE run_id = %s"  # noqa: S608
        counts[table] = _count(conn, sql, run_id)
    return counts


def _reset(pg: str, env: str) -> dict[str, int]:
    return _run(pg, lambda conn: control.reset(conn, env))


@pytest.mark.db
def test_reset_eval_leaves_live_rows(world: dict[str, Any]) -> None:
    pg = world["pg"]
    with psycopg.connect(pg) as conn:
        live_before = _run_rows(conn, world["live"]["run_id"])
        assert all(live_before.values())
        assert all(_run_rows(conn, world["eval"]["run_id"]).values())

    _reset(pg, "eval")

    with psycopg.connect(pg) as conn:
        # Every row of the eval runs, a2a-demo included, is gone.
        for key in ("eval", "demo"):
            assert not any(_run_rows(conn, world[key]["run_id"]).values()), key
        # Live runs, and runs with no household, are untouched.
        assert _run_rows(conn, world["live"]["run_id"]) == live_before
        orphan = world["orphan"]["run_id"]
        assert _count(conn, "SELECT count(*) FROM runs WHERE run_id = %s", orphan) == 1
        assert _count(conn, "SELECT count(*) FROM agent_messages WHERE run_id = %s", orphan) == 1
        # Traces and inbound messages are kept for every run.
        assert _count(conn, "SELECT count(*) FROM agent_traces") == 4
        assert _count(conn, "SELECT count(*) FROM inbound_messages") == 4

        # sim_outbound: eval run rows and eval phones go; live run rows and phones stay.
        phones = {r[0] for r in conn.execute("SELECT phone_e164 FROM sim_outbound").fetchall()}
        assert phones == {LIVE_PHONE, "+15550109999"}
        assert (
            _count(
                conn,
                "SELECT count(*) FROM sim_outbound WHERE run_id = %s",
                world["live"]["run_id"],
            )
            == 1
        )

        for table in MOCK_TABLES:
            envs = [r[0] for r in conn.execute(f"SELECT env FROM {table}").fetchall()]  # noqa: S608
            assert envs == ["live"], table

        # Eval stock is re-seeded from the seed data; live stock keeps its changes.
        stock = conn.execute(
            "SELECT env, agent_id, sku, qty FROM mock_stock WHERE env = 'eval'"
        ).fetchall()
        assert sorted(stock) == sorted(mock_stock_rows("eval"))
        live_qty = _count(
            conn,
            "SELECT qty FROM mock_stock WHERE env = 'live' AND agent_id = %s"
            " AND sku = 'MOCK-EGGS-12'",
            GROCERY,
        )
        assert live_qty == 5

        # Seeded eval households and their members are kept.
        assert _count(conn, "SELECT count(*) FROM households WHERE env = 'eval'") == 3


@pytest.mark.db
def test_reset_live_refused(world: dict[str, Any]) -> None:
    pg = world["pg"]
    with psycopg.connect(pg) as conn:
        before = {k: _run_rows(conn, world[k]["run_id"]) for k in ("live", "eval")}

    for env in ("live", "all", ""):
        with pytest.raises(control.ControlRefused):
            _reset(pg, env)

    with psycopg.connect(pg) as conn:
        assert {k: _run_rows(conn, world[k]["run_id"]) for k in ("live", "eval")} == before
        assert _count(conn, "SELECT count(*) FROM mock_events") == 2


@pytest.mark.db
def test_reset_eval_after_shortfall_succeeds(world: dict[str, Any]) -> None:
    pg = world["pg"]
    eval_run = world["eval"]["run_id"]
    with psycopg.connect(pg) as conn:
        assert _count(conn, "SELECT count(*) FROM shortfalls WHERE run_id = %s", eval_run) == 1

    deleted = _reset(pg, "eval")

    assert deleted["shortfalls"] == 1
    with psycopg.connect(pg) as conn:
        assert _count(conn, "SELECT count(*) FROM shortfalls WHERE run_id = %s", eval_run) == 0
        assert _count(conn, "SELECT count(*) FROM runs WHERE run_id = %s", eval_run) == 0
        live_run = world["live"]["run_id"]
        assert _count(conn, "SELECT count(*) FROM shortfalls WHERE run_id = %s", live_run) == 1

    # A second reset on an already clean eval state also succeeds.
    assert _reset(pg, "eval")["runs"] == 0


def _inject(pg: str, **kwargs: Any) -> int:
    return _run(pg, lambda conn: control.inject_event(conn, **kwargs))


@pytest.mark.db
def test_inject_event_validates_shortfall_params(world: dict[str, Any]) -> None:
    pg = world["pg"]
    order_id = str(world["eval"]["order_id"])
    with psycopg.connect(pg) as conn:
        conn.execute("DELETE FROM mock_events")

    bad: list[Any] = [
        None,
        {},
        {"order_id": order_id},
        {"skus": ["MOCK-MILK-WHOLE"]},
        {"order_id": "not-a-uuid", "skus": ["MOCK-MILK-WHOLE"]},
        {"order_id": order_id, "skus": []},
        {"order_id": order_id, "skus": "MOCK-MILK-WHOLE"},
        {"order_id": order_id, "skus": ["MOCK-MILK-WHOLE", "MOCK-MILK-WHOLE"]},
        {"order_id": order_id, "skus": ["MOCK-MILK-WHOLE", "MOCK-EGGS-12", "MOCK-RICE-2LB", "X"]},
        {"order_id": order_id, "skus": ["MOCK-BANANAS-2LB"]},
        {"order_id": str(uuid.uuid4()), "skus": ["MOCK-MILK-WHOLE"]},
        {"order_id": order_id, "skus": ["MOCK-MILK-WHOLE"], "extra": 1},
    ]
    for params in bad:
        with pytest.raises(control.ControlRefused):
            _inject(pg, env="eval", kind="shortfall", target_agent_id=GROCERY, params=params)
    with pytest.raises(control.ControlRefused):
        _inject(pg, env="eval", kind="bogus", target_agent_id=GROCERY, params={})
    with pytest.raises(control.ControlRefused):
        _inject(pg, env="prod", kind="out_of_stock", target_agent_id=GROCERY, params={})

    with psycopg.connect(pg) as conn:
        assert _count(conn, "SELECT count(*) FROM mock_events") == 0

    skus = ["MOCK-MILK-WHOLE", "MOCK-EGGS-12", "MOCK-RICE-2LB"]
    event_id = _inject(
        pg,
        env="eval",
        kind="shortfall",
        target_agent_id=GROCERY,
        params={"order_id": order_id, "skus": skus},
    )

    with psycopg.connect(pg) as conn:
        row = conn.execute(
            "SELECT env, kind, target_agent_id, params, active FROM mock_events WHERE id = %s",
            (event_id,),
        ).fetchone()
    assert row == ("eval", "shortfall", GROCERY, {"order_id": order_id, "skus": skus}, True)


def _square_order(pg: str, order_id: uuid.UUID, square_retail: str) -> None:
    """Make the order a `grocery:square-us` order whose run pinned `square_retail`."""
    with psycopg.connect(pg) as conn:
        conn.execute(
            "UPDATE orders SET seller_agent_id = 'grocery:square-us' WHERE order_id = %s",
            (order_id,),
        )
        conn.execute(
            "UPDATE runs SET modes = %s WHERE run_id = (SELECT run_id FROM orders"
            " WHERE order_id = %s)",
            (Jsonb({"square_retail": square_retail, "doordash": "mock"}), order_id),
        )


@pytest.mark.db
def test_shortfall_refused_on_real_mode_order(world: dict[str, Any]) -> None:
    # Adapters §8: a shortfall amends the order, and a real order cannot be amended.
    pg = world["pg"]
    order_id = world["live"]["order_id"]
    _square_order(pg, order_id, "real")
    with psycopg.connect(pg) as conn:
        conn.execute("DELETE FROM mock_events")

    with pytest.raises(control.ControlRefused, match="shortfall events need a mock-mode order"):
        _inject(
            pg,
            env="live",
            kind="shortfall",
            target_agent_id="grocery:square-us",
            params={"order_id": str(order_id), "skus": ["MOCK-MILK-WHOLE"]},
        )

    with psycopg.connect(pg) as conn:
        assert _count(conn, "SELECT count(*) FROM mock_events") == 0


@pytest.mark.db
def test_shortfall_allowed_on_mock_mode_order(world: dict[str, Any]) -> None:
    pg = world["pg"]
    order_id = world["live"]["order_id"]
    _square_order(pg, order_id, "mock")

    event_id = _inject(
        pg,
        env="live",
        kind="shortfall",
        target_agent_id="grocery:square-us",
        params={"order_id": str(order_id), "skus": ["MOCK-MILK-WHOLE"]},
    )

    with psycopg.connect(pg) as conn:
        row = conn.execute("SELECT kind FROM mock_events WHERE id = %s", (event_id,)).fetchone()
    assert row == ("shortfall",)


def test_mock_stock_rows_cover_seeded_items() -> None:
    data = seed_mod.load_data()
    expected = {
        (agent_id, item["sku"])
        for groups in (data["catalogs"], data["menus"])
        for agent_id, items in groups.items()
        for item in items
    }
    for env in ("live", "eval"):
        rows = mock_stock_rows(env)
        assert rows == mock_stock_rows(env, data)
        assert {(a, s) for _, a, s, _ in rows} == expected
        assert {(e, q) for e, _, _, q in rows} == {(env, MOCK_STOCK_QTY)}
    with pytest.raises(ValueError):
        mock_stock_rows("all")


@pytest.mark.db
def test_reset_eval_deletes_learned_dislikes_and_household_rules(world: dict[str, Any]) -> None:
    """Agents §9, §10 (AG-A12): eval households' learned dislikes and `household_rules`
    rows are deleted; a live household's, and seeded rows, are kept."""
    pg = world["pg"]
    eval_user = ids.seed_user_id("us-1-a-eval")
    live_user = ids.seed_user_id("us-1-a")
    with psycopg.connect(pg) as conn:
        for user in (eval_user, live_user):
            conn.execute(
                "INSERT INTO member_constraints (user_id, kind, value, source)"
                " VALUES (%s, 'dislike', 'okra', 'learned')",
                (user,),
            )
        for household in (EVAL, LIVE):
            conn.execute(
                "INSERT INTO household_rules (household_id, take_turns, turn_index)"
                " VALUES (%s, true, 2)",
                (household,),
            )
        seeded = _count(conn, "SELECT count(*) FROM member_constraints WHERE source <> 'learned'")

    deleted = _reset(pg, "eval")

    assert deleted["member_constraints"] == 1
    assert deleted["household_rules"] == 1
    with psycopg.connect(pg) as conn:
        learned = conn.execute(
            "SELECT user_id FROM member_constraints WHERE source = 'learned'"
        ).fetchall()
        assert [r[0] for r in learned] == [live_user]
        rules = conn.execute("SELECT household_id FROM household_rules").fetchall()
        assert [r[0] for r in rules] == [LIVE]
        after = _count(conn, "SELECT count(*) FROM member_constraints WHERE source <> 'learned'")
        assert after == seeded


# --- setup keys (evals.md §4, E-A8; issue #224) ---------------------------------------------

_LIVE_LIMITS_SQL = (
    "SELECT b.household_id, b.amount_minor, r.per_order_minor FROM budgets b"
    " JOIN households h USING (household_id)"
    " LEFT JOIN approval_rules r USING (household_id)"
    " WHERE h.env = 'live' ORDER BY 1"
)


def _budget(conn: psycopg.Connection, household_id: str) -> int:
    return _count(
        conn,
        "SELECT amount_minor FROM budgets WHERE household_id = %s AND period = 'weekly'",
        household_id,
    )


def _per_order(conn: psycopg.Connection, household_id: str) -> int | None:
    row = conn.execute(
        "SELECT per_order_minor FROM approval_rules WHERE household_id = %s", (household_id,)
    ).fetchone()
    assert row is not None
    return row[0]


def _reset_setup(pg: str, setup: Any, household_id: str = EVAL) -> dict[str, int]:
    return _run(pg, lambda conn: control.reset(conn, "eval", setup, household_id))


@pytest.mark.db
def test_budget_setup_applied_then_restored(world: dict[str, Any]) -> None:
    pg = world["pg"]
    limits = seeded_limits()
    seeded = limits[EVAL]["weekly_budget_minor"]
    assert seeded is not None

    _reset_setup(pg, {"budget_minor": 2500})
    with psycopg.connect(pg) as conn:
        assert _budget(conn, EVAL) == 2500
        assert _budget(conn, "us-2-eval") == limits["us-2-eval"]["weekly_budget_minor"]

    _reset(pg, "eval")
    with psycopg.connect(pg) as conn:
        assert _budget(conn, EVAL) == seeded


@pytest.mark.db
def test_per_order_setup_restored(world: dict[str, Any]) -> None:
    pg = world["pg"]
    limits = seeded_limits()

    _reset_setup(pg, {"per_order_minor": 4000}, "us-2-eval")
    with psycopg.connect(pg) as conn:
        assert _per_order(conn, "us-2-eval") == 4000
        assert _per_order(conn, EVAL) == limits[EVAL]["per_order_minor"]

    _reset(pg, "eval")
    with psycopg.connect(pg) as conn:
        assert _per_order(conn, "us-2-eval") == limits["us-2-eval"]["per_order_minor"]


@pytest.mark.db
def test_setup_never_touches_live_households(world: dict[str, Any]) -> None:
    pg = world["pg"]
    with psycopg.connect(pg) as conn:
        # Live limits that differ from the seed: no reset may apply or restore them.
        conn.execute("UPDATE budgets SET amount_minor = 999 WHERE household_id = %s", (LIVE,))
        conn.execute(
            "UPDATE approval_rules SET per_order_minor = 777 WHERE household_id = %s", (LIVE,)
        )
        before = conn.execute(_LIVE_LIMITS_SQL).fetchall()

    setup = {"budget_minor": 1, "per_order_minor": 1}
    _reset_setup(pg, setup)
    for household in (LIVE, "us-2", "a2a-demo", "nope"):
        with pytest.raises(control.ControlRefused):
            _reset_setup(pg, setup, household)
    _reset(pg, "eval")

    with psycopg.connect(pg) as conn:
        after = conn.execute(_LIVE_LIMITS_SQL).fetchall()
    assert after == before
    assert (LIVE, 999, 777) in after


@pytest.mark.db
def test_bad_setup_refused_and_writes_nothing(world: dict[str, Any]) -> None:
    pg = world["pg"]
    eval_run = world["eval"]["run_id"]
    bad: list[Any] = [
        {"bogus": 1},
        {"budget_minor": "2500"},
        {"budget_minor": True},
        {"budget_minor": 25.0},
        {"per_order_minor": None},
        {"per_order_minor": -1},
        ["budget_minor"],
    ]
    for setup in bad:
        with pytest.raises(control.ControlRefused):
            _reset_setup(pg, setup)
    with psycopg.connect(pg) as conn:
        assert _count(conn, "SELECT count(*) FROM runs WHERE run_id = %s", eval_run) == 1


def _learned(conn: psycopg.Connection) -> list[tuple[uuid.UUID, str]]:
    rows = conn.execute(
        "SELECT user_id, value FROM member_constraints WHERE source = 'learned' ORDER BY 1, 2"
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


@pytest.mark.db
def test_dislikes_setup_removed_by_next_reset(world: dict[str, Any]) -> None:
    """Evals §4 (step 3b): `dislikes` inserts learned dislike rows for the eval household's
    members; the next reset deletes them (agents AG-A12). Seeded rows and a live
    household's learned rows are kept."""
    pg = world["pg"]
    member_b, member_c = ids.seed_user_id("us-1-b-eval"), ids.seed_user_id("us-1-c-eval")
    live_user = ids.seed_user_id("us-1-a")
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO member_constraints (user_id, kind, value, source)"
            " VALUES (%s, 'dislike', 'okra', 'learned')",
            (live_user,),
        )
        seeded = _count(conn, "SELECT count(*) FROM member_constraints WHERE source <> 'learned'")

    setup = {"dislikes": {"us-1-b-eval": ["Rice", "rice"], "us-1-c-eval": ["spinach"]}}
    _reset_setup(pg, setup)
    with psycopg.connect(pg) as conn:
        kinds = conn.execute(
            "SELECT DISTINCT kind FROM member_constraints WHERE source = 'learned'"
        ).fetchall()
        assert kinds == [("dislike",)]
        assert _learned(conn) == sorted(
            [(live_user, "okra"), (member_b, "rice"), (member_c, "spinach")]
        )

    _reset(pg, "eval")
    with psycopg.connect(pg) as conn:
        assert _learned(conn) == [(live_user, "okra")]
        after = _count(conn, "SELECT count(*) FROM member_constraints WHERE source <> 'learned'")
        assert after == seeded

    # Members of another household, live members and bad values are refused; nothing written.
    bad: list[Any] = [
        {"dislikes": {"us-2-a-eval": ["rice"]}},
        {"dislikes": {"us-1-a": ["rice"]}},
        {"dislikes": {"nobody": ["rice"]}},
        {"dislikes": {"us-1-a-eval": ["rice; drop"]}},
        {"dislikes": {"us-1-a-eval": "rice"}},
        {"dislikes": {"us-1-a-eval": [1]}},
        {"dislikes": ["rice"]},
    ]
    for bad_setup in bad:
        with pytest.raises(control.ControlRefused):
            _reset_setup(pg, bad_setup)
    with psycopg.connect(pg) as conn:
        assert _learned(conn) == [(live_user, "okra")]


@pytest.mark.db
def test_take_turns_setup_removed(world: dict[str, Any]) -> None:
    """Evals §4 (step 3b): `take_turns` writes the eval household's `household_rules` row;
    the next reset deletes it (agents AG-A12). A live household's row is kept."""
    pg = world["pg"]
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO household_rules (household_id, take_turns) VALUES (%s, true)", (LIVE,)
        )

    def rules() -> list[tuple[str, bool]]:
        with psycopg.connect(pg) as conn:
            rows = conn.execute(
                "SELECT household_id, take_turns FROM household_rules ORDER BY 1"
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    _reset_setup(pg, {"take_turns": True})
    assert rules() == sorted([(LIVE, True), (EVAL, True)])

    _reset_setup(pg, {"take_turns": False}, "us-2-eval")
    assert rules() == sorted([(LIVE, True), ("us-2-eval", False)])

    _reset(pg, "eval")
    assert rules() == [(LIVE, True)]

    for value in (1, 0, "true", None):
        with pytest.raises(control.ControlRefused):
            _reset_setup(pg, {"take_turns": value})
    assert rules() == [(LIVE, True)]


_PANTRY_SQL = (
    "SELECT household_id, item, pantry_item, kind, delta, unit, source, order_id, run_id,"
    " round(extract(epoch FROM now() - ts) / 3600)::int"
    " FROM pantry_events WHERE source = 'eval_setup' ORDER BY event_id"
)


def _pantry_rows(conn: psycopg.Connection) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in conn.execute(_PANTRY_SQL).fetchall()]


@pytest.mark.db
def test_pantry_setup_key(world: dict[str, Any]) -> None:
    """Pantry §8, PG9: one `pantry_events` row per entry, in list order, for the case's
    household only, with `delta 0`, `unit 'each'`, `source 'eval_setup'` and a back-dated
    `ts`. Other households (eval or live) get no rows."""
    pg = world["pg"]
    setup = {
        "pantry": {
            "milk": [{"kind": "add", "age_hours": 30}, {"kind": "verify", "age_hours": 2}],
            "eggs": [{"kind": "out", "age_hours": 0}],
            "rice": [{"kind": "add", "age_hours": 5.5}, {"kind": "add", "age_hours": 5.5}],
        }
    }
    hid = "us-2-eval"
    _reset_setup(pg, setup, hid)
    with psycopg.connect(pg) as conn:
        rows = _pantry_rows(conn)
        ages = conn.execute(
            "SELECT extract(epoch FROM now() - ts) / 3600 FROM pantry_events"
            " WHERE source = 'eval_setup' ORDER BY event_id"
        ).fetchall()
        # Other eval households have no rows; live rows from the fixture are not setup rows.
        others = _count(
            conn,
            "SELECT count(*) FROM pantry_events WHERE household_id <> %s"
            " AND (source = 'eval_setup' OR household_id IN"
            " (SELECT household_id FROM households WHERE env = 'eval'))",
            hid,
        )
    assert rows == [
        (hid, "milk", "milk", "add", 0, "each", "eval_setup", None, None, 30),
        (hid, "milk", "milk", "verify", 0, "each", "eval_setup", None, None, 2),
        (hid, "eggs", "eggs", "out", 0, "each", "eval_setup", None, None, 0),
        (hid, "rice", "rice", "add", 0, "each", "eval_setup", None, None, 6),
        (hid, "rice", "rice", "add", 0, "each", "eval_setup", None, None, 6),
    ]
    assert [round(float(a[0]), 1) for a in ages][3:] == [5.5, 5.5]
    assert others == 0


@pytest.mark.db
def test_pantry_setup_refusals(world: dict[str, Any]) -> None:
    """Pantry §8: each `ControlRefused` case names the item and writes nothing."""
    pg = world["pg"]
    ok = {"kind": "add", "age_hours": 1}
    older_last = [{"kind": "add", "age_hours": 1}, {"kind": "add", "age_hours": 2}]
    bad: list[tuple[Any, str]] = [
        ({"caviar": [ok]}, "caviar"),  # not in the vocabulary
        ({"whole milk": [ok]}, "whole milk"),  # an alias is not a vocabulary name
        ({"milk": [{"kind": "gone", "age_hours": 1}]}, "milk"),  # kind not in KINDS
        ({"eggs": [{"kind": "add", "age_hours": -1}]}, "eggs"),  # below 0
        ({"eggs": [{"kind": "add", "age_hours": "1"}]}, "eggs"),  # not a number
        ({"eggs": [{"kind": "add", "age_hours": True}]}, "eggs"),  # bool is not a number
        ({"eggs": [{"kind": "add", "age_hours": float("nan")}]}, "eggs"),  # not finite
        ({"bread": older_last}, "bread"),  # age_hours increases
        ({"rice": [{"kind": "add", "age_hours": 1, "qty": 2}]}, "rice"),  # extra key
        ({"rice": [{"kind": "add"}]}, "rice"),  # missing key
        ({"pasta": []}, "pasta"),  # empty list
        ({"pasta": ok}, "pasta"),  # not a list
    ]
    for value, item in bad:
        with pytest.raises(control.ControlRefused, match=item):
            _reset_setup(pg, {"pantry": value})
    with pytest.raises(control.ControlRefused):
        _reset_setup(pg, {"pantry": [["milk", ok]]})
    with psycopg.connect(pg) as conn:
        assert _pantry_rows(conn) == []


@pytest.mark.db
def test_pantry_setup_undone_by_reset(world: dict[str, Any]) -> None:
    """Pantry PG9: the next reset removes the setup rows, keeps live pantry rows, and leaves
    the eval households with no pantry events."""
    pg = world["pg"]
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO pantry_events (household_id, item, delta, unit, source)"
            " VALUES (%s, 'live-sku', 1, 'each', 'delivery')",
            (LIVE,),
        )
    live_sql = "SELECT count(*) FROM pantry_events WHERE household_id = %s"
    with psycopg.connect(pg) as conn:
        live_before = _count(conn, live_sql, LIVE)
    assert live_before >= 1
    eval_sql = (
        "SELECT count(*) FROM pantry_events WHERE household_id IN"
        " (SELECT household_id FROM households WHERE env = 'eval')"
    )

    _reset_setup(pg, {"pantry": {"bananas": [{"kind": "add", "age_hours": 1}]}})
    with psycopg.connect(pg) as conn:
        assert len(_pantry_rows(conn)) == 1
        assert _count(conn, live_sql, EVAL) == 1

    _reset(pg, "eval")
    with psycopg.connect(pg) as conn:
        assert _pantry_rows(conn) == []
        assert _count(conn, eval_sql) == 0
        assert _count(conn, live_sql, LIVE) == live_before


@pytest.mark.db
def test_reset_restores_window(world: dict[str, Any]) -> None:
    pg = world["pg"]
    old = datetime.now(UTC) - timedelta(days=3)
    with psycopg.connect(pg) as conn:
        conn.execute("UPDATE channel_identities SET last_inbound_at = NULL")
        conn.execute(
            "UPDATE channel_identities SET last_inbound_at = %s WHERE phone_e164 = %s",
            (old, LIVE_PHONE),
        )

    _reset(pg, "eval")

    eval_phones_sql = (
        "SELECT ci.phone_e164, ci.last_inbound_at FROM channel_identities ci"
        " JOIN household_members hm ON hm.user_id = ci.user_id"
        " JOIN households h ON h.household_id = hm.household_id WHERE h.env = %s"
    )
    with psycopg.connect(pg) as conn:
        eval_rows = conn.execute(eval_phones_sql, ("eval",)).fetchall()
        live_rows = dict(conn.execute(eval_phones_sql, ("live",)).fetchall())
        now = conn.execute("SELECT now()").fetchone()
    assert now is not None
    assert EVAL_PHONE in {phone for phone, _ in eval_rows}
    for phone, at in eval_rows:
        assert at is not None and at > now[0] - timedelta(minutes=1), phone
    # Live phones are never touched by the eval reset.
    assert live_rows[LIVE_PHONE] == old
    assert all(at is None for phone, at in live_rows.items() if phone != LIVE_PHONE)
