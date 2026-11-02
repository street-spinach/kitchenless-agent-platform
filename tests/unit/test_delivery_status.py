"""`apply_delivery_status` and `POST /internal/delivery-status` (architecture items 35, 37,
§8; agent-protocol P30; issue #58)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg.types.range import Range

from services.api.routes import delivery
from services.common import ids
from tests.unit import api_world as w

pytestmark = pytest.mark.db

HOUSEHOLD = "us-1"
HOUSEHOLD_AGENT = f"household:{HOUSEHOLD}"
COURIER = "delivery:mock-us"
SELLER = "grocery:mock-us"
EDI = str(uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"))
USER = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
START = datetime(2026, 11, 3, 17, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    w.patch_settings(monkeypatch)


def _seed(pg: str, state: str = "courier_booked") -> tuple[uuid.UUID, uuid.UUID]:
    """A running weekly run with one order in `state`, booked as EDI with COURIER."""
    address_ref = ids.seed_address_ref(HOUSEHOLD)
    run_id, order_id = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO households (household_id, name, region, zone_id, timezone, currency)"
            " VALUES (%s, 'Test', 'us', 'us-oh-45202', 'America/New_York', 'USD')",
            (HOUSEHOLD,),
        )
        conn.execute(
            "INSERT INTO household_addresses"
            " (address_ref, household_id, line1, city, region, postal_code, country)"
            " VALUES (%s, %s, '1 Main St', 'Cincinnati', 'OH', '45202', 'US')",
            (address_ref, HOUSEHOLD),
        )
        for agent_id, cls, owner in (
            (HOUSEHOLD_AGENT, "household", HOUSEHOLD_AGENT),
            (COURIER, "delivery", "org:mock-courier-us"),
            (SELLER, "grocery", "org:mock-store-us"),
        ):
            conn.execute(
                "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
                " VALUES (%s, %s, %s, 'us', 'agents')",
                (agent_id, cls, owner),
            )
        conn.execute(
            "INSERT INTO runs (run_id, household_id, kind, trigger, modes)"
            " VALUES (%s, %s, 'weekly', %s, '{}')",
            (run_id, HOUSEHOLD, Jsonb({"user_id": str(USER)})),
        )
        conn.execute(
            "INSERT INTO orders (order_id, run_id, household_id, kind, state, seller_agent_id,"
            " delivery_agent_id, total_minor, currency, address_ref, external_delivery_id,"
            " delivery_window)"
            " VALUES (%s, %s, %s, 'grocery', %s, %s, %s, 1207, 'USD', %s, %s, %s)",
            (
                order_id,
                run_id,
                HOUSEHOLD,
                state,
                SELLER,
                COURIER,
                address_ref,
                EDI,
                Range(START, START + timedelta(hours=1), "[)"),
            ),
        )
    return run_id, order_id


def _apply(pg: str, status: str, edi: str = EDI) -> str:
    return w.run(pg, lambda conn: delivery.apply_delivery_status(conn, edi, status))


def _state(pg: str, order_id: uuid.UUID) -> str:
    [row] = w.query(pg, "SELECT state FROM orders WHERE order_id = %s", order_id)
    return row["state"]


def _messages(pg: str, run_id: uuid.UUID) -> list[dict[str, Any]]:
    return w.query(
        pg,
        "SELECT * FROM agent_messages WHERE run_id = %s AND type = 'order_status'"
        " ORDER BY created_at, message_id",
        run_id,
    )


def _run(pg: str, run_id: uuid.UUID) -> dict[str, Any]:
    [row] = w.query(pg, "SELECT status, reason FROM runs WHERE run_id = %s", run_id)
    return row


def _outcomes(pg: str, run_id: uuid.UUID) -> list[dict[str, Any]]:
    return w.query(pg, "SELECT * FROM agent_traces WHERE run_id = %s AND kind = 'outcome'", run_id)


def test_forward_only(pg: str) -> None:
    run_id, order_id = _seed(pg)

    assert _apply(pg, "booked") == "no_change"
    assert _state(pg, order_id) == "courier_booked"
    assert _messages(pg, run_id) == []

    assert _apply(pg, "picked_up") == "applied"
    assert _state(pg, order_id) == "in_delivery"
    [msg] = _messages(pg, run_id)
    assert msg["message_id"] == ids.order_status_id(order_id, "in_delivery")
    assert (msg["from_agent"], msg["to_agent"]) == ("system:api", HOUSEHOLD_AGENT)
    assert msg["body"]["state"] == "in_delivery"
    assert msg["body"]["order_id"] == str(order_id)
    assert msg["body"]["delivery_agent_id"] == COURIER

    # A repeat, or a status that maps to an earlier state, changes nothing.
    assert _apply(pg, "picked_up") == "no_change"
    assert _apply(pg, "enroute_to_dropoff") == "no_change"
    assert _apply(pg, "booked") == "no_change"
    assert _state(pg, order_id) == "in_delivery"
    assert len(_messages(pg, run_id)) == 1

    assert _apply(pg, "delivered") == "applied"
    assert _state(pg, order_id) == "delivered"
    assert [m["body"]["state"] for m in _messages(pg, run_id)] == ["in_delivery", "delivered"]

    # Nothing moves a delivered order, not even `failed`.
    assert _apply(pg, "picked_up") == "ignored"
    assert _apply(pg, "failed") == "ignored"
    assert _state(pg, order_id) == "delivered"
    assert len(_messages(pg, run_id)) == 2
    # Only a courier failure ends the run here; `delivered` is the household agent's.
    assert _run(pg, run_id)["status"] == "running"


def test_delivered_without_pickup_walks_the_main_path(pg: str) -> None:
    run_id, order_id = _seed(pg)
    assert _apply(pg, "delivered") == "applied"
    assert _state(pg, order_id) == "delivered"
    [msg] = _messages(pg, run_id)
    assert msg["body"]["state"] == "delivered"


@pytest.mark.parametrize("start", ["courier_booked", "in_delivery"])
@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failed_ends_the_run(pg: str, start: str, status: str) -> None:
    run_id, order_id = _seed(pg, start)

    assert _apply(pg, status) == "applied"

    assert _state(pg, order_id) == "failed"
    assert _run(pg, run_id) == {"status": "error", "reason": "delivery_failed"}
    [outcome] = _outcomes(pg, run_id)
    assert outcome["agent_id"] == "system:api"
    assert outcome["payload"]["reason"] == "delivery_failed"
    [msg] = _messages(pg, run_id)
    assert msg["body"]["state"] == "failed"
    assert msg["body"]["reason"] == "delivery_failed"
    # No retry is offered for a failed delivery (architecture §9).
    assert w.query(pg, "SELECT * FROM agent_messages WHERE type = 'whatsapp_out'") == []

    # A repeated failure changes nothing and writes no second outcome.
    assert _apply(pg, status) == "ignored"
    assert len(_outcomes(pg, run_id)) == 1
    assert len(_messages(pg, run_id)) == 1


@pytest.mark.parametrize("status", ["booked", "picked_up", "delivered", "failed"])
def test_events_for_failed_order_ignored(pg: str, status: str) -> None:
    # The order failed elsewhere (`amend_order` left no line, agent-protocol P30).
    run_id, order_id = _seed(pg, "failed")

    assert _apply(pg, status) == "ignored"

    assert _state(pg, order_id) == "failed"
    assert _messages(pg, run_id) == []
    assert _run(pg, run_id)["status"] == "running"
    assert _outcomes(pg, run_id) == []


def test_unknown_status_refused(pg: str) -> None:
    _, order_id = _seed(pg)
    with pytest.raises(delivery.UnknownStatus):
        _apply(pg, "teleported")
    assert _state(pg, order_id) == "courier_booked"


def test_unknown_delivery_changes_nothing(pg: str) -> None:
    run_id, order_id = _seed(pg)
    assert _apply(pg, "picked_up", edi=str(uuid.uuid4())) == "unknown_delivery"
    assert _state(pg, order_id) == "courier_booked"
    assert _messages(pg, run_id) == []


def _post(pg: str, body: Any) -> Any:
    return w.client(pg).post("/internal/delivery-status", json=body)


def test_route_applies_the_mock_courier_post(pg: str) -> None:
    run_id, order_id = _seed(pg)
    body = {"external_delivery_id": EDI, "status": "picked_up", "event_id": f"{EDI}:picked_up"}

    resp = _post(pg, body)
    assert resp.status_code == 200
    assert resp.json() == {"result": "applied"}
    assert _state(pg, order_id) == "in_delivery"

    # The mock retries a post until it gets a 2xx; a repeat is still a 2xx.
    again = _post(pg, body)
    assert again.status_code == 200
    assert again.json() == {"result": "no_change"}
    assert len(_messages(pg, run_id)) == 1


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ([], 400),
        ({"external_delivery_id": EDI, "status": "picked_up"}, 400),
        ({"external_delivery_id": EDI, "status": "", "event_id": "e"}, 400),
        ({"external_delivery_id": EDI, "status": "teleported", "event_id": "e"}, 400),
        ({"external_delivery_id": str(uuid.uuid4()), "status": "picked_up", "event_id": "e"}, 404),
    ],
)
def test_route_refuses_bad_posts(pg: str, body: Any, code: int) -> None:
    _, order_id = _seed(pg)
    assert _post(pg, body).status_code == code
    assert _state(pg, order_id) == "courier_booked"
