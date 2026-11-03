"""The weekly grocery order completes end to end in mock mode (architecture G1, item 69).

Run on the live stack via `make scenario` (testrunner), with `MODEL_MODE=stub` and mock
adapters, after `make seed`.
"""

import json
import os
import time
import urllib.request
import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row

API = os.environ.get("SCENARIO_API_URL", "http://api:8000")
HOUSEHOLD = "us-1-eval"
# G1: within 300 s in mock mode with the stub model.
TIMEOUT_SECONDS = 300
OPEN_STATUSES = {"running", "awaiting_approval"}


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _get(path: str) -> dict[str, Any]:
    with urllib.request.urlopen(API + path, timeout=10) as resp:
        return json.loads(resp.read())


def _wait_for_outcome(run_id: uuid.UUID) -> dict[str, Any]:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while True:
        run = _get(f"/runs/{run_id}")
        if run["status"] not in OPEN_STATUSES:
            return run
        assert time.monotonic() < deadline, f"run {run_id} still {run['status']} after 300 s"
        time.sleep(2)


def test_weekly_order_completes_in_mock_mode() -> None:
    # Start from a clean eval household, as every eval case does (evals/run.py). The VM's
    # database keeps the pantry an earlier deploy's run delivered, and with a full pantry the
    # weekly order correctly skips the shop (`nothing_needed`, pantry §5), so the run would
    # not test the order path at all.
    _post("/panel/reset", {"env": "eval", "household": HOUSEHOLD})
    started = _post(
        "/runs",
        {
            "scenario": "weekly_grocery_order",
            "household_id": HOUSEHOLD,
            "input": "Weekly shop please",
        },
    )
    run_id = uuid.UUID(started["run_id"])

    run = _wait_for_outcome(run_id)
    assert run["status"] == "completed", run

    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        outcomes = conn.execute(
            "SELECT payload FROM agent_traces WHERE run_id = %s AND kind = 'outcome'",
            (run_id,),
        ).fetchall()
        orders = conn.execute(
            "SELECT order_id, household_id, state, external_order_id, external_delivery_id"
            " FROM orders WHERE run_id = %s",
            (run_id,),
        ).fetchall()

        # Exactly one outcome row, and it says completed (G1).
        assert [o["payload"]["status"] for o in outcomes] == ["completed"]

        # One order, for this household, placed and delivered.
        assert len(orders) == 1, orders
        order = orders[0]
        assert order["household_id"] == HOUSEHOLD
        assert order["state"] == "delivered"
        assert order["external_order_id"]

        # One courier booking for the order.
        assert order["external_delivery_id"]
        bookings = conn.execute(
            "SELECT state FROM mock_deliveries WHERE external_delivery_id = %s",
            (order["external_delivery_id"],),
        ).fetchall()
        assert [b["state"] for b in bookings] == ["delivered"]

        # One pantry update per order line.
        lines = conn.execute(
            "SELECT line, name FROM order_items WHERE order_id = %s ORDER BY line",
            (order["order_id"],),
        ).fetchall()
        pantry = conn.execute(
            "SELECT item, delta FROM pantry_events WHERE order_id = %s",
            (order["order_id"],),
        ).fetchall()
        assert lines
        assert len(pantry) == len(lines), (lines, pantry)
        assert all(p["delta"] > 0 for p in pantry)
