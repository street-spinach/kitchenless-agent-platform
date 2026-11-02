"""Control panel routes in `api` (architecture item 39, agent-protocol §8 and P31; issue #74)."""

import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from starlette.testclient import TestClient

from services.api import main as api
from services.api.routes import panel
from services.common import ids
from services.common.config import Settings
from services.common.protocol.schemas import SCHEMAS
from services.mocks import control
from tests.unit import api_world as w

db = pytest.mark.db

STORE = "grocery:mock-us"
HOUSEHOLD = f"household:{w.HOUSEHOLD_ID}"
T0 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
ORDER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
QUOTE_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
CONVERSATION = uuid.UUID("33333333-3333-4333-8333-333333333333")

OFFER = {
    "order_id": str(ORDER_ID),
    "quote_id": str(QUOTE_ID),
    "round": 0,
    "lines": [
        {
            "line": 1,
            "requested_item": "oat milk",
            "offered_sku": "SKU-1",
            "offered_name": "Oat milk 1l",
            "qty": 2,
            "unit_minor": 350,
        }
    ],
    "total_minor": 700,
    "currency": "USD",
    "expires_at": (T0 + timedelta(minutes=5)).isoformat(),
    "final": False,
    "rationale": "Cheapest oat milk in stock",
}
# A people type (S8) whose body holds profile data.
CONSTRAINTS = {
    "order_id": str(ORDER_ID),
    "allergens": ["peanuts"],
    "diets": ["vegan"],
    "dislikes": ["cilantro"],
}


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    w.patch_settings(monkeypatch)
    settings = Settings.from_env(
        {"SERVICE_NAME": "migrate", "DATABASE_URL": "x", "LOG_LEVEL": "info"}
    )
    monkeypatch.setattr(control, "get_settings", lambda: settings)


@pytest.fixture
def world(pg: str) -> str:
    w.seed(pg)
    return pg


def _run(conn: psycopg.Connection) -> uuid.UUID:
    run_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO runs (run_id, household_id, kind, trigger, modes)"
        " VALUES (%s, %s, 'weekly', %s, %s)",
        (run_id, w.HOUSEHOLD_ID, Jsonb({"source": "test"}), Jsonb({})),
    )
    return run_id


def _message(
    conn: psycopg.Connection,
    run_id: uuid.UUID,
    *,
    type_: str,
    body: dict[str, Any],
    from_agent: str,
    to_agent: str,
    conversation_id: uuid.UUID | None = None,
    causation_id: uuid.UUID | None = None,
    **cols: Any,
) -> uuid.UUID:
    message_id = uuid.uuid4()
    names = ", ".join(cols)
    extra = "".join(", %s" for _ in cols)
    conn.execute(
        "INSERT INTO agent_messages (message_id, run_id, conversation_id, causation_id,"
        f" from_agent, to_agent, to_host, type, body{', ' + names if cols else ''})"
        f" VALUES (%s, %s, %s, %s, %s, %s, 'agents', %s, %s{extra})",
        (
            message_id,
            run_id,
            conversation_id,
            causation_id,
            from_agent,
            to_agent,
            type_,
            Jsonb(body),
            *cols.values(),
        ),
    )
    return message_id


def _trace(
    conn: psycopg.Connection,
    run_id: uuid.UUID,
    seq: int,
    kind: str,
    payload: dict[str, Any],
    *,
    conversation_id: uuid.UUID | None = None,
    message_id: uuid.UUID | None = None,
    agent_id: str = HOUSEHOLD,
) -> None:
    conn.execute(
        "INSERT INTO agent_traces (run_id, seq, ts, agent_id, kind, payload, conversation_id,"
        " message_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            run_id,
            seq,
            T0 + timedelta(seconds=seq),
            agent_id,
            kind,
            Jsonb(payload),
            conversation_id,
            message_id,
        ),
    )


def _message_trace(
    conn: psycopg.Connection,
    run_id: uuid.UUID,
    seq: int,
    message_id: uuid.UUID,
    *,
    type_: str,
    body: dict[str, Any],
    from_agent: str,
    to_agent: str,
    summary: str,
    conversation_id: uuid.UUID | None = None,
    causation_id: uuid.UUID | None = None,
) -> None:
    payload = {
        "from": from_agent,
        "to": to_agent,
        "type": type_,
        "body": body,
        "summary": summary,
        "causation_id": None if causation_id is None else str(causation_id),
    }
    _trace(
        conn,
        run_id,
        seq,
        "message",
        payload,
        conversation_id=conversation_id,
        message_id=message_id,
        agent_id=from_agent,
    )


# --- no database ----------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _no_db() -> AsyncIterator[Any]:
    raise AssertionError("this request must be refused before the database")
    yield  # pragma: no cover


def _client_no_db() -> TestClient:
    return TestClient(api.create_app(_no_db))


def test_detail_types_are_exactly_s1_to_s7() -> None:
    s8 = {
        "user_message",
        "start_weekly_order",
        "start_dinner",
        "constraints_request",
        "constraints",
        "approval_decision",
        "quote_round_closed",
        "whatsapp_out",
        "shortfall_detected",
        "shortfall_closed",
    }
    assert panel.DETAIL_TYPES == set(SCHEMAS) - s8
    assert "protocol_error" in panel.DETAIL_TYPES
    assert "shortfall_notice" in panel.DETAIL_TYPES


def test_panel_routes_are_mounted_in_api() -> None:
    paths = {getattr(r, "path", None) for r in api.create_app(_no_db).routes}
    assert {
        "/panel/reset",
        "/panel/events",
        "/panel/modes/{vendor}",
        "/panel/approvals/{approval_id}/expire",
        "/panel/runs/live",
        "/panel/messages/{message_id}",
    } <= paths


@pytest.mark.parametrize(
    "query", ["?run_id=not-a-uuid", "?limit=0", "?limit=x", f"?limit={panel.LIVE_LIMIT_MAX + 1}"]
)
def test_runs_live_refuses_bad_query(query: str) -> None:
    resp = _client_no_db().get(f"/panel/runs/live{query}")
    assert resp.status_code == 400


@pytest.mark.parametrize("params", [None, {}, {"order_id": "not-a-uuid", "skus": ["SKU-1"]}])
def test_shortfall_event_without_order_id_refused_before_db(params: Any) -> None:
    resp = _client_no_db().post(
        "/panel/events",
        json={"env": "eval", "kind": "shortfall", "target_agent_id": STORE, "params": params},
    )
    assert resp.status_code == 400


def test_message_detail_unknown_id_is_404() -> None:
    assert _client_no_db().get("/panel/messages/not-a-uuid").status_code == 404


# --- live view ------------------------------------------------------------------------------


@db
def test_runs_live_fields_and_lanes(world: str) -> None:
    with psycopg.connect(world) as conn:
        run_id = _run(conn)
        constraints = _message(
            conn,
            run_id,
            type_="constraints",
            body=CONSTRAINTS,
            from_agent=w.ADMIN_CONSUMER,
            to_agent=HOUSEHOLD,
        )
        offer = _message(
            conn,
            run_id,
            type_="offer",
            body=OFFER,
            from_agent=STORE,
            to_agent=HOUSEHOLD,
            conversation_id=CONVERSATION,
            causation_id=constraints,
        )
        _message_trace(
            conn,
            run_id,
            1,
            constraints,
            type_="constraints",
            body=CONSTRAINTS,
            from_agent=w.ADMIN_CONSUMER,
            to_agent=HOUSEHOLD,
            summary="Constraints received (2 codes)",
        )
        _message_trace(
            conn,
            run_id,
            2,
            offer,
            type_="offer",
            body=OFFER,
            from_agent=STORE,
            to_agent=HOUSEHOLD,
            summary="Offer $7.00: 1 of 1 items",
            conversation_id=CONVERSATION,
            causation_id=constraints,
        )
        # Not part of the live view.
        _trace(conn, run_id, 3, "decision", {"node": "n", "choice": "c", "explanation": "e"})
        # The error trace has no conversation of its own: it goes in its ref's lane.
        _trace(
            conn,
            run_id,
            4,
            "error",
            {
                "where": "protocol",
                "reason": "party",
                "attempt": 1,
                "ref_message_id": str(offer),
                "detail": "free text",
            },
        )

    resp = w.client(world).get(f"/panel/runs/live?run_id={run_id}")
    assert resp.status_code == 200
    [run] = resp.json()["runs"]
    assert run["run_id"] == str(run_id)
    lanes = {lane["lane"]: lane["rows"] for lane in run["lanes"]}
    assert set(lanes) == {panel.HOUSEHOLD_LANE, str(CONVERSATION)}

    [people] = lanes[panel.HOUSEHOLD_LANE]
    assert people["kind"] == "message"
    assert people["message_id"] == str(constraints)
    assert people["conversation_id"] is None
    assert people["causation_id"] is None
    assert people["summary"] == "Constraints received (2 codes)"

    offer_row, error_row = lanes[str(CONVERSATION)]
    assert datetime.fromisoformat(offer_row.pop("ts")) == T0 + timedelta(seconds=2)
    assert offer_row == {
        "kind": "message",
        "seq": 2,
        "message_id": str(offer),
        "conversation_id": str(CONVERSATION),
        "causation_id": str(constraints),
        "from": STORE,
        "to": HOUSEHOLD,
        "type": "offer",
        "summary": "Offer $7.00: 1 of 1 items",
    }
    assert error_row["kind"] == "error"
    assert error_row["ref_message_id"] == str(offer)
    assert (error_row["where"], error_row["reason"], error_row["attempt"]) == (
        "protocol",
        "party",
        1,
    )
    # Rows carry summaries only: no body, rationale, profile data or free text.
    for text in ("peanuts", "cilantro", "Cheapest oat milk", "free text", '"body"'):
        assert text not in resp.text


@db
def test_runs_live_limit_keeps_latest_rows(world: str) -> None:
    with psycopg.connect(world) as conn:
        run_id = _run(conn)
        for seq in (1, 2, 3):
            mid = _message(
                conn,
                run_id,
                type_="offer",
                body=OFFER,
                from_agent=STORE,
                to_agent=HOUSEHOLD,
                conversation_id=CONVERSATION,
            )
            _message_trace(
                conn,
                run_id,
                seq,
                mid,
                type_="offer",
                body=OFFER,
                from_agent=STORE,
                to_agent=HOUSEHOLD,
                summary=f"offer {seq}",
                conversation_id=CONVERSATION,
            )
    resp = w.client(world).get("/panel/runs/live?limit=2")
    assert resp.status_code == 200
    [run] = resp.json()["runs"]
    [lane] = run["lanes"]
    assert [r["seq"] for r in lane["rows"]] == [2, 3]


# --- message detail -------------------------------------------------------------------------


@db
def test_message_detail_shows_body_a2a_and_rationale_for_offer(world: str) -> None:
    processed = T0 + timedelta(seconds=9)
    with psycopg.connect(world) as conn:
        run_id = _run(conn)
        offer = _message(
            conn,
            run_id,
            type_="offer",
            body=OFFER,
            from_agent=STORE,
            to_agent=HOUSEHOLD,
            conversation_id=CONVERSATION,
            status="processed",
            attempts=2,
            last_error="boom",
            processed_at=processed,
        )
    resp = w.client(world).get(f"/panel/messages/{offer}")
    assert resp.status_code == 200
    out = resp.json()
    assert (out["attempts"], out["last_error"]) == (2, "boom")
    assert datetime.fromisoformat(out["processed_at"]) == processed
    assert out["status"] == "processed"
    assert out["summary"].startswith("Offer ")
    assert out["body"] == OFFER
    assert out["rationale"] == "Cheapest oat milk in stock"
    assert out["a2a"]["messageId"] == str(offer)
    assert out["a2a"]["contextId"] == str(CONVERSATION)
    assert {"text": "Cheapest oat milk in stock"} in out["a2a"]["parts"]


@db
def test_message_detail_hides_body_for_people_types(world: str) -> None:
    with psycopg.connect(world) as conn:
        run_id = _run(conn)
        constraints = _message(
            conn,
            run_id,
            type_="constraints",
            body=CONSTRAINTS,
            from_agent=w.ADMIN_CONSUMER,
            to_agent=HOUSEHOLD,
            attempts=1,
        )
    resp = w.client(world).get(f"/panel/messages/{constraints}")
    assert resp.status_code == 200
    out = resp.json()
    assert out["type"] == "constraints"
    assert out["summary"] == "Constraints received (2 codes)"
    assert out["attempts"] == 1
    assert out["last_error"] is None
    assert out["processed_at"] is None
    for key in ("body", "a2a", "rationale"):
        assert key not in out
    for text in ("peanuts", "vegan", "cilantro"):
        assert text not in resp.text


@db
def test_message_detail_unknown_message_is_404(world: str) -> None:
    assert w.client(world).get(f"/panel/messages/{uuid.uuid4()}").status_code == 404


# --- mock control ---------------------------------------------------------------------------


@db
def test_reset_refuses_live(world: str) -> None:
    resp = w.client(world).post("/panel/reset", json={"env": "live"})
    assert resp.status_code == 400


@db
def test_reset_eval(world: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(control, "mock_stock_rows", lambda env: [])
    resp = w.client(world).post("/panel/reset", json={"env": "eval"})
    assert resp.status_code == 200
    assert resp.json()["env"] == "eval"
    assert "runs" in resp.json()["deleted"]


@db
def test_events_writes_mock_event(world: str) -> None:
    resp = w.client(world).post(
        "/panel/events",
        json={"env": "eval", "kind": "out_of_stock", "target_agent_id": STORE, "params": {}},
    )
    assert resp.status_code == 200
    [row] = w.query(world, "SELECT * FROM mock_events WHERE id = %s", resp.json()["event_id"])
    assert (row["env"], row["kind"], row["target_agent_id"]) == ("eval", "out_of_stock", STORE)
    bad = w.client(world).post(
        "/panel/events", json={"env": "eval", "kind": "nope", "target_agent_id": STORE}
    )
    assert bad.status_code == 400


# --- shortfall event (agent-protocol §10 and P31, #70) --------------------------------------


def _order(pg: str, state: str) -> uuid.UUID:
    """A run with a `grocery` order from STORE in `state`, lines SKU-1 and SKU-2."""
    address_ref = uuid.uuid4()
    order_id = uuid.uuid4()
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
            " VALUES (%s, 'grocery', 'org:mock-store-us', 'us', 'agents')"
            " ON CONFLICT DO NOTHING",
            (STORE,),
        )
        conn.execute(
            "INSERT INTO household_addresses"
            " (address_ref, household_id, line1, city, region, postal_code, country)"
            " VALUES (%s, %s, '1 Main St', 'Cincinnati', 'OH', '45202', 'US')",
            (address_ref, w.HOUSEHOLD_ID),
        )
        run_id = _run(conn)
        conn.execute(
            "INSERT INTO orders (order_id, run_id, household_id, kind, state, seller_agent_id,"
            " total_minor, currency, address_ref)"
            " VALUES (%s, %s, %s, 'grocery', %s, %s, 1050, 'USD', %s)",
            (order_id, run_id, w.HOUSEHOLD_ID, state, STORE, address_ref),
        )
        for line, sku in ((1, "SKU-1"), (2, "SKU-2")):
            conn.execute(
                "INSERT INTO order_items (order_id, line, sku, name, qty, unit_minor)"
                " VALUES (%s, %s, %s, 'Item', 1, 525)",
                (order_id, line, sku),
            )
    return order_id


def _shortfall_event(
    pg: str, order_id: uuid.UUID, skus: list[str] | None = None, target: str = STORE
) -> Any:
    return w.client(pg).post(
        "/panel/events",
        json={
            "env": "eval",
            "kind": "shortfall",
            "target_agent_id": target,
            "params": {"order_id": str(order_id), "skus": skus or ["SKU-1"]},
        },
    )


def _detected(pg: str) -> list[dict[str, Any]]:
    return w.query(pg, "SELECT * FROM agent_messages WHERE type = 'shortfall_detected'")


@db
@pytest.mark.parametrize(
    "state", ["placed", "authorized", "in_delivery", "delivered", "failed", "cancelled"]
)
def test_shortfall_event_refused_unless_courier_booked(world: str, state: str) -> None:
    order_id = _order(world, state)
    resp = _shortfall_event(world, order_id)
    assert resp.status_code == 409
    assert w.query(world, "SELECT * FROM mock_events") == []
    assert _detected(world) == []


@db
def test_shortfall_event_sends_shortfall_detected_to_the_seller(world: str) -> None:
    order_id = _order(world, "courier_booked")
    resp = _shortfall_event(world, order_id, ["SKU-1", "SKU-2"])
    assert resp.status_code == 200
    event_id = resp.json()["event_id"]
    [event] = w.query(world, "SELECT * FROM mock_events WHERE id = %s", event_id)
    assert (event["env"], event["kind"], event["target_agent_id"]) == ("eval", "shortfall", STORE)
    assert event["params"] == {"order_id": str(order_id), "skus": ["SKU-1", "SKU-2"]}
    [msg] = _detected(world)
    source = ids.mock_event_id(event_id)
    assert msg["message_id"] == ids.message_id(source, 0)
    assert (msg["from_agent"], msg["to_agent"]) == ("system:api", STORE)
    assert msg["body"] == {
        "order_id": str(order_id),
        "skus": ["SKU-1", "SKU-2"],
        "event_id": str(source),
    }


@db
def test_second_shortfall_event_refused(world: str) -> None:
    order_id = _order(world, "courier_booked")
    assert _shortfall_event(world, order_id).status_code == 200

    # Before the household opens the `shortfalls` row: the first event already counts.
    assert _shortfall_event(world, order_id, ["SKU-2"]).status_code == 409

    # After the row exists.
    with psycopg.connect(world) as conn:
        conn.execute(
            "INSERT INTO shortfalls (shortfall_id, run_id, order_id, seller_agent_id, lines,"
            " due_at) SELECT %s, run_id, order_id, seller_agent_id, '[]', now()"
            " FROM orders WHERE order_id = %s",
            (ids.shortfall_id(order_id), order_id),
        )
        conn.execute("DELETE FROM mock_events")
    assert _shortfall_event(world, order_id, ["SKU-2"]).status_code == 409
    assert len(_detected(world)) == 1


@db
@pytest.mark.parametrize(
    ("skus", "target"),
    [
        (["SKU-9"], STORE),
        (["SKU-1", "SKU-2", "SKU-1"], STORE),
        (["SKU-1"], "grocery:other"),
    ],
)
def test_shortfall_event_refuses_bad_params(world: str, skus: list[str], target: str) -> None:
    order_id = _order(world, "courier_booked")
    assert _shortfall_event(world, order_id, skus, target).status_code == 400
    assert w.query(world, "SELECT * FROM mock_events") == []
    assert _detected(world) == []


@db
def test_put_mode_writes_adapter_modes(world: str) -> None:
    resp = w.client(world).put("/panel/modes/doordash", json={"mode": "mock"})
    assert resp.status_code == 200
    [row] = w.query(world, "SELECT * FROM adapter_modes WHERE vendor = 'doordash'")
    assert (row["mode"], row["set_by"]) == ("mock", "panel")
    assert w.client(world).put("/panel/modes/nope", json={"mode": "mock"}).status_code == 400


@db
def test_expire_approval_without_timer_is_404(world: str) -> None:
    resp = w.client(world).post(f"/panel/approvals/{uuid.uuid4()}/expire")
    assert resp.status_code == 404
