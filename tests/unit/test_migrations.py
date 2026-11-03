"""Migrations 0001-0015 apply in order to an empty database (issues #25, #26, #27, #112, #196,
#226, #254, #278)."""

import re
import shutil
import uuid
from datetime import date
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from migrations.apply import MIGRATIONS_DIR, apply

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
    "agent_clients",
    "agent_grants",
    "consent_links",
    "audit_events",
    "fga_models",
    "household_rules",
    "eval_runs",
    "eval_results",
    "whatsapp_sends",
    "held_messages",
    "reminders",
    "onboarding_proposals",
    "join_codes",
    "meal_reactions",
    "experience_weekly_asks",
    "experience_weekly_guards",
}
# Views carry no rows of their own, so no created_at.
VIEWS = {"experience_weekly_asks", "experience_weekly_guards"}
# Tables whose own time column records creation (architecture, Migrations preamble).
OWN_TIME_COLUMN = {
    "agent_traces",
    "runs",
    "inbound_messages",
    "approvals",
    "pantry_events",
    "audit_events",
    "fga_models",
    "eval_runs",
}


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
    assert done[9:11] == ["0010_identity.sql", "0011_approval_band.sql"]
    assert done[11:13] == ["0012_agents.sql", "0013_eval_history.sql"]
    assert done[13:14] == ["0014_pantry.sql"]
    assert done[14:15] == ["0015_experience.sql"]
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
    assert TABLES - OWN_TIME_COLUMN - VIEWS <= {r[0] for r in rows}


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


def _household_with_user(conn: psycopg.Connection) -> uuid.UUID:
    conn.execute(
        "INSERT INTO households (household_id, name, region, zone_id, timezone, currency)"
        " VALUES ('hh:1', 'H', 'us', 'z1', 'UTC', 'USD')"
    )
    user_id = uuid.uuid4()
    conn.execute("INSERT INTO users (user_id, display_name) VALUES (%s, 'A')", (user_id,))
    return user_id


GRANT_INSERT = (
    "INSERT INTO agent_grants (grant_id, agent_id, agent_kind, user_id, consented_by,"
    " household_id, scopes, refresh_ciphertext, key_version, expires_at, status)"
    " VALUES (%s, %s, %s, %s, %s, 'hh:1', '{}', '\\x00', 1, now() + interval '1 day', %s)"
)


def test_one_active_household_grant(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        asha = _household_with_user(conn)
        ben = uuid.uuid4()
        conn.execute("INSERT INTO users (user_id, display_name) VALUES (%s, 'B')", (ben,))
        _card(conn, "household:1", "household", "agents")
        conn.execute(
            GRANT_INSERT, (uuid.uuid4(), "household:1", "household", asha, asha, "revoked")
        )
        conn.execute(GRANT_INSERT, (uuid.uuid4(), "household:1", "household", asha, asha, "active"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                GRANT_INSERT, (uuid.uuid4(), "household:1", "household", ben, ben, "active")
            )


def test_one_active_grant_per_agent_and_user(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        asha = _household_with_user(conn)
        ben = uuid.uuid4()
        conn.execute("INSERT INTO users (user_id, display_name) VALUES (%s, 'B')", (ben,))
        _card(conn, "consumer:1", "consumer", "agents")
        conn.execute(GRANT_INSERT, (uuid.uuid4(), "consumer:1", "consumer", asha, asha, "active"))
        conn.execute(GRANT_INSERT, (uuid.uuid4(), "consumer:1", "consumer", ben, ben, "active"))
        conn.execute(GRANT_INSERT, (uuid.uuid4(), "consumer:1", "consumer", asha, asha, "expired"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                GRANT_INSERT, (uuid.uuid4(), "consumer:1", "consumer", asha, asha, "active")
            )


def test_agent_clients_need_no_card(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO agent_clients (agent_id, client_id, kind)"
            " VALUES ('system:api', 'system-api', 'service')"
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO agent_clients (agent_id, client_id, kind)"
                " VALUES ('x:1', 'x-1', 'robot')"
            )


@pytest.mark.parametrize(
    ("kind", "ok"),
    [("keycloak_revoke_failed", True), ("consent_granted", True), ("token_minted", False)],
)
def test_audit_events_kind(pg: str, kind: str, ok: bool) -> None:
    with psycopg.connect(pg) as conn:
        insert = "INSERT INTO audit_events (kind) VALUES (%s)"
        if ok:
            conn.execute(insert, (kind,))
            assert conn.execute("SELECT detail FROM audit_events").fetchone() == ({},)
        else:
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(insert, (kind,))


def test_approval_band_columns(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        user_id = _household_with_user(conn)
        conn.execute(
            "INSERT INTO approval_rules (household_id, approver_user_id) VALUES ('hh:1', %s)",
            (user_id,),
        )
        assert conn.execute("SELECT per_order_minor FROM approval_rules").fetchone() == (None,)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE approval_rules SET per_order_minor = -1")
    with psycopg.connect(pg) as conn:
        cols = conn.execute(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = 'approvals'"
            " AND column_name = 'limits'"
        ).fetchone()
    assert cols[:2] == ("ARRAY", "NO")
    assert cols[2].startswith("'{}'")


CONSTRAINT_INSERT = (
    "INSERT INTO member_constraints (user_id, kind, value, source) VALUES (%s, %s, %s, %s)"
)


def test_learned_rows_are_dislikes_only(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        user_id = _household_with_user(conn)
        with pytest.raises(psycopg.errors.CheckViolation) as err:
            conn.execute(CONSTRAINT_INSERT, (user_id, "allergy", "milk", "learned"))
        assert err.value.diag.constraint_name == "member_constraints_learned_dislike_only"
    with psycopg.connect(pg) as conn:
        user_id = uuid.uuid4()
        conn.execute("INSERT INTO users (user_id, display_name) VALUES (%s, 'B')", (user_id,))
        conn.execute(CONSTRAINT_INSERT, (user_id, "dislike", "olives", "learned"))


def test_learned_dislike_unique(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        user_id = _household_with_user(conn)
        conn.execute(CONSTRAINT_INSERT, (user_id, "dislike", "olives", "seed"))
        conn.execute(CONSTRAINT_INSERT, (user_id, "dislike", "olives", "learned"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(CONSTRAINT_INSERT, (user_id, "dislike", "olives", "learned"))


def test_household_rules_defaults(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        _household_with_user(conn)
        conn.execute("INSERT INTO household_rules (household_id) VALUES ('hh:1')")
        row = conn.execute(
            "SELECT take_turns, turn_index, last_turn_order_id, set_by FROM household_rules"
        ).fetchone()
    assert row == (False, 0, None, None)


EVAL_RUN_INSERT = (
    "INSERT INTO eval_runs (eval_run_id, trigger, git_sha, model_mode, models, selection,"
    " thresholds) VALUES (%s, %s, 'abc123', 'stub', '{}', '{case-1}', '{}')"
)
EVAL_RESULT_INSERT = (
    "INSERT INTO eval_results (eval_run_id, case_id, repeat, category, case_hash, model_mode,"
    " verdict, error_kind, seconds, checks)"
    " VALUES (%s, 'case-1', %s, 'safety', 'h', 'stub', 'pass', 'none', 1.5, '{}')"
)


@pytest.mark.parametrize(
    ("trigger", "ok"),
    [("nightly", True), ("manual", True), ("d4", True), ("rehearsal", True), ("ci", False)],
)
def test_eval_runs_trigger_check(pg: str, trigger: str, ok: bool) -> None:
    with psycopg.connect(pg) as conn:
        if ok:
            conn.execute(EVAL_RUN_INSERT, (uuid.uuid4(), trigger))
            row = conn.execute(
                "SELECT tokens, outcome, reason_counts, error_kind_counts, finished_at"
                " FROM eval_runs"
            ).fetchone()
            assert row == (0, None, {}, {}, None)
        else:
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(EVAL_RUN_INSERT, (uuid.uuid4(), trigger))
    with psycopg.connect(pg) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(EVAL_RUN_INSERT.replace("'stub'", "'replay'"), (uuid.uuid4(), "manual"))


def test_eval_results_cascade_with_eval_run(pg: str) -> None:
    keep, drop = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(pg) as conn:
        for eval_run_id in (keep, drop):
            conn.execute(EVAL_RUN_INSERT, (eval_run_id, "manual"))
            conn.execute(EVAL_RESULT_INSERT, (eval_run_id, 0))
            conn.execute(EVAL_RESULT_INSERT, (eval_run_id, 1))
    with psycopg.connect(pg) as conn, pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(EVAL_RESULT_INSERT, (keep, 1))
    with psycopg.connect(pg) as conn:
        row = conn.execute(
            "SELECT agent_run_ids, tokens, models_used, reason FROM eval_results LIMIT 1"
        ).fetchone()
        assert row == ([], 0, [], None)
        conn.execute("DELETE FROM eval_runs WHERE eval_run_id = %s", (drop,))
        rows = conn.execute("SELECT eval_run_id FROM eval_results").fetchall()
        indexes = conn.execute(
            "SELECT indexdef FROM pg_indexes"
            " WHERE schemaname = current_schema() AND tablename = 'eval_results'"
        ).fetchall()
    assert {r[0] for r in rows} == {keep}
    assert len(rows) == 2
    assert any("(case_id, model_mode, created_at DESC)" in r[0] for r in indexes)


PANTRY_SQL = MIGRATIONS_DIR / "0014_pantry.sql"


def test_0014_additive() -> None:
    words = set(re.findall(r"[A-Z]+", PANTRY_SQL.read_text().upper()))
    assert not words & {"DROP", "RENAME", "CHECK"}


def test_0014_pantry_columns_and_index(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        cols = conn.execute(
            "SELECT table_name, column_name, data_type, is_nullable, column_default"
            " FROM information_schema.columns WHERE table_schema = current_schema()"
            " AND column_name IN ('pantry_item', 'kind', 'run_id')"
            " AND table_name IN ('pantry_events', 'catalog_items')"
        ).fetchall()
        index = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema()"
            " AND indexname = 'pantry_events_item_latest'"
        ).fetchone()
    assert sorted(cols) == [
        ("catalog_items", "pantry_item", "text", "YES", None),
        ("pantry_events", "kind", "text", "NO", "'add'::text"),
        ("pantry_events", "pantry_item", "text", "YES", None),
        ("pantry_events", "run_id", "uuid", "YES", None),
    ]
    assert index is not None
    assert "(household_id, pantry_item, event_id DESC)" in index[0]


def test_0014_applies_over_existing_rows(empty_schema_dsn: str, tmp_path: Path) -> None:
    for sql in MIGRATIONS_DIR.glob("*.sql"):
        if sql.name < PANTRY_SQL.name:
            shutil.copy(sql, tmp_path / sql.name)
    apply(empty_schema_dsn, tmp_path)
    with psycopg.connect(empty_schema_dsn) as conn:
        run_id = _run(conn)
        order_id = _order(conn, run_id)
        conn.execute(
            "INSERT INTO pantry_events (household_id, item, delta, unit, source, order_id)"
            " VALUES ('hh:1', 'sku-milk', 1, 'each', 'delivery', %s)",
            (order_id,),
        )
        conn.execute(
            "INSERT INTO catalog_items (agent_id, sku, name, ingredients, price_minor, currency)"
            " VALUES ('grocery:1', 'sku-milk', 'Milk', 'milk', 199, 'USD')"
        )
    shutil.copy(PANTRY_SQL, tmp_path / PANTRY_SQL.name)  # 0014 alone, not the later ones
    assert apply(empty_schema_dsn, tmp_path) == ["0014_pantry.sql"]
    with psycopg.connect(empty_schema_dsn) as conn:
        event = conn.execute("SELECT item, pantry_item, kind, run_id FROM pantry_events").fetchone()
        sku = conn.execute("SELECT pantry_item FROM catalog_items").fetchone()
        # Old code that inserts without kind still writes an 'add'; any kind is accepted.
        conn.execute(
            "INSERT INTO pantry_events (household_id, item, delta, unit, source)"
            " VALUES ('hh:1', 'sku-eggs', 1, 'each', 'delivery')"
        )
        conn.execute(
            "INSERT INTO pantry_events (household_id, item, pantry_item, kind, delta, unit,"
            " source, run_id) VALUES ('hh:1', 'eggs', 'eggs', 'receipt', 0, 'each', 'x', %s)",
            (run_id,),
        )
        kinds = conn.execute("SELECT kind FROM pantry_events ORDER BY event_id").fetchall()
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO pantry_events (household_id, item, delta, unit, source, run_id)"
                " VALUES ('hh:1', 'x', 0, 'each', 'x', %s)",
                (uuid.uuid4(),),
            )
    assert event == ("sku-milk", None, "add", None)
    assert sku == (None,)
    assert kinds == [("add",), ("add",), ("receipt",)]


EXPERIENCE_SQL = MIGRATIONS_DIR / "0015_experience.sql"


def test_0015_additive(empty_schema_dsn: str, tmp_path: Path) -> None:
    text = EXPERIENCE_SQL.read_text().upper()
    words = set(re.findall(r"[A-Z]+", text))
    assert not words & {"DROP", "RENAME", "CHECK"}
    assert not re.search(r"ALTER\s+TYPE", text)
    for sql in MIGRATIONS_DIR.glob("*.sql"):
        if sql.name < EXPERIENCE_SQL.name:
            shutil.copy(sql, tmp_path / sql.name)
    apply(empty_schema_dsn, tmp_path)
    assert apply(empty_schema_dsn) == ["0015_experience.sql"]
    assert apply(empty_schema_dsn) == []


def test_0015_indexes(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        defs = [
            r[0]
            for r in conn.execute(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema()"
            ).fetchall()
        ]
    for expected in (
        "held_messages USING btree (user_id, status, held_id)",
        "reminders USING btree (household_id, kind)",
        "onboarding_proposals USING btree (household_id, status)",
        "join_codes USING btree (household_id) WHERE (used_at IS NULL)",
        "whatsapp_sends USING btree (user_id, created_at)",
    ):
        assert any(expected in d for d in defs), expected


def _send(
    conn: psycopg.Connection,
    run_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: str,
    outcome: str = "sent",
    status: str | None = None,
) -> None:
    message_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO agent_messages (message_id, run_id, from_agent, to_agent, to_host, type,"
        " body) VALUES (%s, %s, 'consumer:1', 'channel:whatsapp', 'api', 'out', %s)",
        (message_id, run_id, Jsonb({"buttons": [{"payload": payload}]})),
    )
    conn.execute(
        "INSERT INTO whatsapp_sends (message_id, user_id, outcome, status, created_at)"
        " VALUES (%s, %s, %s, %s, '2026-11-04 12:00+00')",
        (message_id, user_id, outcome, status),
    )


def test_0015_weekly_views(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        user_id = _household_with_user(conn)
        conn.execute(
            "INSERT INTO household_members (household_id, user_id, role)"
            " VALUES ('hh:1', %s, 'admin')",
            (user_id,),
        )
        run_id = _run(conn, "chat")  # household_id = NULL: the send still counts
        _send(conn, run_id, user_id, "ap:1")
        _send(conn, run_id, user_id, "ob:1", outcome="template")
        _send(conn, run_id, user_id, "tt:1", status="failed")
        _send(conn, run_id, user_id, "me:page")
        _send(conn, run_id, user_id, "sf:1", outcome="held")
        _send(conn, run_id, user_id, "sf:2", outcome="skipped")
        asks = conn.execute(
            "SELECT household_id, user_id, week, asks FROM experience_weekly_asks"
        ).fetchall()
        guards = conn.execute(
            "SELECT household_id, week, sends_not_delivered, approvals_expired"
            " FROM experience_weekly_guards"
        ).fetchall()
    week = date(2026, 11, 2)
    assert asks == [("hh:1", user_id, week, 2)]
    assert guards == [("hh:1", week, 1, 0)]
