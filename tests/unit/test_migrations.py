"""Migrations 0001-0009 apply in order to an empty database (issues #25, #26, #27)."""

import uuid

import psycopg
import pytest

from migrations.apply import apply

pytestmark = pytest.mark.db

TABLES = {
    "agent_traces",
    "users",
    "organizations",
    "households",
    "household_members",
    "member_constraints",
    "budgets",
    "approval_rules",
    "household_addresses",
    "channel_identities",
    "agent_cards",
    "business_rules",
    "runs",
    "agent_messages",
    "inbound_messages",
    "orders",
    "order_items",
    "quotes",
    "approvals",
    "timers",
    "pantry_events",
    "shortfalls",
    "idempotency_keys",
    "adapter_modes",
    "catalog_items",
    "menu_items",
    "mock_stock",
    "mock_orders",
    "mock_deliveries",
    "mock_events",
    "sim_outbound",
}
# Tables whose own time column records creation (architecture, Migrations preamble).
OWN_TIME_COLUMN = {"agent_traces", "runs", "inbound_messages", "approvals", "pantry_events"}


def _card(conn: psycopg.Connection, agent_id: str, cls: str, host: str) -> None:
    conn.execute(
        "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
        " VALUES (%s, %s, 'system', 'us', %s)",
        (agent_id, cls, host),
    )


def test_applies_in_order_on_empty_database(empty_schema_dsn: str) -> None:
    done = apply(empty_schema_dsn)

    assert done[:9] == [
        "0001_agent_traces.sql",
        "0002_core_entities.sql",
        "0003_agent_directory.sql",
        "0004_runs_and_messages.sql",
        "0005_orders_quotes_approvals_timers.sql",
        "0006_idempotency_and_modes.sql",
        "0007_catalog.sql",
        "0008_mock_backends.sql",
        "0009_trace_additions.sql",
    ]
    assert apply(empty_schema_dsn) == []
    with psycopg.connect(empty_schema_dsn) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
        ).fetchall()
    assert TABLES <= {r[0] for r in rows}


def test_every_new_table_has_created_at(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND column_name = 'created_at'"
        ).fetchall()
    assert TABLES - OWN_TIME_COLUMN <= {r[0] for r in rows}


def test_agent_cards_accept_external(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        _card(conn, "external:a2a-demo", "external", "external")
        _card(conn, "channel:whatsapp", "channel", "api")
        row = conn.execute(
            "SELECT zone_ids, config, enabled FROM agent_cards WHERE agent_id = 'external:a2a-demo'"
        ).fetchone()
    assert row == ([], {}, True)


@pytest.mark.parametrize(("cls", "host"), [("courier", "agents"), ("grocery", "mcp")])
def test_agent_cards_reject_unknown_class_or_host(pg: str, cls: str, host: str) -> None:
    with psycopg.connect(pg) as conn, pytest.raises(psycopg.errors.CheckViolation):
        _card(conn, "x:1", cls, host)


@pytest.mark.parametrize(
    ("kind", "value", "ok"),
    [
        ("allergy", "tree_nuts", True),
        ("allergy", "nuts", False),
        ("diet", "vegetarian", True),
        ("diet", "paleo", False),
        ("dislike", "olives", True),
        ("taste", "sweet", False),
    ],
)
def test_member_constraints_use_fixed_codes(pg: str, kind: str, value: str, ok: bool) -> None:
    user_id = uuid.uuid4()
    with psycopg.connect(pg) as conn:
        conn.execute("INSERT INTO users (user_id, display_name) VALUES (%s, 'A')", (user_id,))
        insert = "INSERT INTO member_constraints (user_id, kind, value) VALUES (%s, %s, %s)"
        if ok:
            conn.execute(insert, (user_id, kind, value))
        else:
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(insert, (user_id, kind, value))


def _run(conn: psycopg.Connection, kind: str = "weekly") -> uuid.UUID:
    run_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO runs (run_id, kind, trigger, modes) VALUES (%s, %s, '{}', '{}')",
        (run_id, kind),
    )
    return run_id


def _order(conn: psycopg.Connection, run_id: uuid.UUID) -> uuid.UUID:
    conn.execute(
        "INSERT INTO households (household_id, name, region, zone_id, timezone, currency)"
        " VALUES ('hh:1', 'H', 'us', 'z1', 'UTC', 'USD')"
    )
    address_ref = uuid.uuid4()
    conn.execute(
        "INSERT INTO household_addresses"
        " (address_ref, household_id, line1, city, region, postal_code, country)"
        " VALUES (%s, 'hh:1', '1 Main', 'C', 'R', '00000', 'US')",
        (address_ref,),
    )
    order_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO orders (order_id, run_id, household_id, kind, currency, address_ref)"
        " VALUES (%s, %s, 'hh:1', 'grocery', 'USD', %s)",
        (order_id, run_id, address_ref),
    )
    _card(conn, "grocery:1", "grocery", "agents")
    _card(conn, "household:1", "household", "agents")
    return order_id


def _quote(conn: psycopg.Connection, closed_by: str | None) -> tuple:
    run_id = _run(conn)
    order_id = _order(conn, run_id)
    quote_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO quotes (quote_id, run_id, order_id, conversation_id, seller_agent_id,"
        " buyer_agent_id, items, total_minor, currency, expires_at, closed_by)"
        " VALUES (%s, %s, %s, %s, 'grocery:1', 'household:1', '[]', 100, 'USD', now(), %s)",
        (quote_id, run_id, order_id, uuid.uuid4(), closed_by),
    )
    return conn.execute(
        "SELECT constraints, closed_by, round, status FROM quotes WHERE quote_id = %s",
        (quote_id,),
    ).fetchone()


SHORTFALL_INSERT = (
    "INSERT INTO shortfalls (shortfall_id, run_id, order_id, seller_agent_id, lines, due_at,"
    " status) VALUES (%s, %s, %s, 'grocery:1', '[]', now(), %s)"
)


@pytest.mark.parametrize(("kind", "ok"), [("a2a_demo", True), ("hello", True), ("demo", False)])
def test_runs_kind(pg: str, kind: str, ok: bool) -> None:
    with psycopg.connect(pg) as conn:
        if ok:
            _run(conn, kind)
        else:
            with pytest.raises(psycopg.errors.CheckViolation):
                _run(conn, kind)


def test_agent_messages_causation_id_and_conversation_index(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        cols = conn.execute(
            "SELECT column_name, is_nullable FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = 'agent_messages'"
        ).fetchall()
        indexes = conn.execute(
            "SELECT indexdef FROM pg_indexes"
            " WHERE schemaname = current_schema() AND tablename = 'agent_messages'"
        ).fetchall()
    assert ("causation_id", "YES") in cols
    assert any("(conversation_id, created_at)" in r[0] for r in indexes)


def test_no_body_hash_column(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        row = conn.execute(
            "SELECT table_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND column_name = 'body_hash'"
        ).fetchone()
    assert row is None


@pytest.mark.parametrize(
    ("kind", "ok"), [("shortfall_expiry", True), ("quote_round", True), ("reminder", False)]
)
def test_timers_kind(pg: str, kind: str, ok: bool) -> None:
    with psycopg.connect(pg) as conn:
        run_id = _run(conn)
        insert = "INSERT INTO timers (kind, run_id, due_at) VALUES (%s, %s, now())"
        if ok:
            conn.execute(insert, (kind, run_id))
        else:
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(insert, (kind, run_id))


@pytest.mark.parametrize("closed_by", [None, "buyer", "seller"])
def test_quotes_constraints_and_closed_by(pg: str, closed_by: str | None) -> None:
    with psycopg.connect(pg) as conn:
        row = _quote(conn, closed_by)
    assert row == ({}, closed_by, 0, "open")


def test_quotes_reject_unknown_closed_by(pg: str) -> None:
    with psycopg.connect(pg) as conn, pytest.raises(psycopg.errors.CheckViolation):
        _quote(conn, "courier")


def test_shortfalls_defaults_and_one_per_order(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        run_id = _run(conn)
        order_id = _order(conn, run_id)
        conn.execute(
            "INSERT INTO shortfalls (shortfall_id, run_id, order_id, seller_agent_id, lines,"
            " due_at) VALUES (%s, %s, %s, 'grocery:1', '[]', now())",
            (uuid.uuid4(), run_id, order_id),
        )
        row = conn.execute("SELECT picks, status, closed_at FROM shortfalls").fetchone()
        assert row == ({}, "pending", None)
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(SHORTFALL_INSERT, (uuid.uuid4(), run_id, order_id, "pending"))


@pytest.mark.parametrize(("status", "ok"), [("closed", True), ("applied", True), ("done", False)])
def test_shortfalls_status(pg: str, status: str, ok: bool) -> None:
    with psycopg.connect(pg) as conn:
        run_id = _run(conn)
        order_id = _order(conn, run_id)
        args = (uuid.uuid4(), run_id, order_id, status)
        if ok:
            conn.execute(SHORTFALL_INSERT, args)
        else:
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(SHORTFALL_INSERT, args)


@pytest.mark.parametrize(("status", "ok"), [("in_progress", True), ("done", True), ("x", False)])
def test_idempotency_keys_status(pg: str, status: str, ok: bool) -> None:
    with psycopg.connect(pg) as conn:
        run_id = _run(conn)
        insert = (
            "INSERT INTO idempotency_keys (key, run_id, request_hash, status)"
            " VALUES ('k', %s, 'h', %s)"
        )
        if ok:
            conn.execute(insert, (run_id, status))
            assert conn.execute("SELECT result FROM idempotency_keys").fetchone() == (None,)
        else:
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(insert, (run_id, status))


@pytest.mark.parametrize(("mode", "ok"), [("real", True), ("mock", True), ("replay", False)])
def test_adapter_modes_mode(pg: str, mode: str, ok: bool) -> None:
    with psycopg.connect(pg) as conn:
        insert = "INSERT INTO adapter_modes (vendor, mode, set_by) VALUES ('instacart', %s, 'ops')"
        if ok:
            conn.execute(insert, (mode,))
        else:
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(insert, (mode,))


@pytest.mark.parametrize("table", ["catalog_items", "menu_items"])
def test_catalog_search_is_generated_and_indexed(pg: str, table: str) -> None:
    with psycopg.connect(pg) as conn:
        _card(conn, "grocery:1", "grocery", "agents")
        conn.execute(
            f"INSERT INTO {table} (agent_id, sku, name, brand, ingredients, price_minor,"  # noqa: S608
            " currency) VALUES ('grocery:1', 's1', 'Peanut Butter', NULL, 'peanuts salt', 399,"
            " 'USD')"
        )
        row = conn.execute(
            f"SELECT allergens, diets, nutrition, in_stock FROM {table}"  # noqa: S608
            " WHERE search @@ to_tsquery('simple', 'peanuts')"
        ).fetchone()
        indexes = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema()"
            " AND tablename = %s",
            (table,),
        ).fetchall()
    assert row == ([], [], {}, True)
    assert sum("USING gin" in r[0] for r in indexes) == 2


def test_catalog_has_no_vector_columns(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        row = conn.execute(
            "SELECT table_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND udt_name = 'vector'"
        ).fetchone()
    assert row is None


@pytest.mark.parametrize(
    ("kind", "ok"),
    [
        ("shortfall", True),
        ("out_of_stock", True),
        ("kitchen_overloaded", True),
        ("courier_delayed", True),
        ("adapter_down", True),
        ("flood", False),
    ],
)
def test_mock_events_kind(pg: str, kind: str, ok: bool) -> None:
    with psycopg.connect(pg) as conn:
        insert = (
            "INSERT INTO mock_events (env, kind, target_agent_id) VALUES ('eval', %s, 'grocery:1')"
        )
        if ok:
            conn.execute(insert, (kind,))
            assert conn.execute("SELECT params, active FROM mock_events").fetchone() == ({}, True)
        else:
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(insert, (kind,))


def test_mock_stock_env(pg: str) -> None:
    with psycopg.connect(pg) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO mock_stock (env, agent_id, sku, qty) VALUES ('prod', 'grocery:1', 's', 1)"
        )


@pytest.mark.parametrize(("state", "ok"), [("booked", True), ("delivered", True), ("lost", False)])
def test_mock_deliveries_state_and_unique_key(pg: str, state: str, ok: bool) -> None:
    insert = (
        "INSERT INTO mock_deliveries (external_delivery_id, env, agent_id, idempotency_key,"
        " zone_id, \"window\", state) VALUES (%s, 'eval', 'delivery:1', 'k', 'z1',"
        " tstzrange(now(), now() + interval '1 hour'), %s)"
    )
    with psycopg.connect(pg) as conn:
        if not ok:
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(insert, ("d1", state))
            return
        conn.execute(insert, ("d1", state))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(insert, ("d2", state))


def test_mock_orders_idempotency_key_unique(pg: str) -> None:
    insert = (
        "INSERT INTO mock_orders (external_order_id, env, agent_id, idempotency_key, body, state)"
        " VALUES (%s, 'eval', 'grocery:1', 'k', '{}', 'placed')"
    )
    with psycopg.connect(pg) as conn:
        conn.execute(insert, ("o1",))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(insert, ("o2",))


def test_sim_outbound_index(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        indexes = conn.execute(
            "SELECT indexdef FROM pg_indexes"
            " WHERE schemaname = current_schema() AND tablename = 'sim_outbound'"
        ).fetchall()
    assert any("(phone_e164, id)" in r[0] for r in indexes)


def test_agent_traces_additions(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        cols = conn.execute(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = 'agent_traces'"
        ).fetchall()
        indexes = conn.execute(
            "SELECT indexdef FROM pg_indexes"
            " WHERE schemaname = current_schema() AND tablename = 'agent_traces'"
        ).fetchall()
        comment = conn.execute(
            "SELECT col_description('agent_traces'::regclass, ordinal_position::int)"
            " FROM information_schema.columns WHERE table_schema = current_schema()"
            " AND table_name = 'agent_traces' AND column_name = 'kind'"
        ).fetchone()
    assert {
        ("conversation_id", "uuid", "YES"),
        ("message_id", "uuid", "YES"),
        ("model", "text", "YES"),
        ("latency_ms", "integer", "YES"),
    } <= set(cols)
    defs = [r[0] for r in indexes]
    assert any(d.endswith("(ts)") for d in defs)
    assert any(d.endswith("(agent_id, ts)") for d in defs)
    assert all(k in comment[0] for k in ("run_start", "error", "data_share"))
