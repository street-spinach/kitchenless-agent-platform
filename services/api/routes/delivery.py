"""Courier status moves an order forward (architecture item 37, §8; issue #58).

`apply_delivery_status` is the only writer of `in_delivery`, `delivered` and courier
`failed`. It is called by `POST /internal/delivery-status` (the mock courier, item 35) and,
later, by the DoorDash webhook (step 7). In the caller's transaction it:

*   maps the courier status with `orders.COURIER_STATUS_MAP`;
*   applies a forward-only `orders.transition`. A status that maps to no change, to the
    order's current state or to an earlier one changes nothing, so repeated or late
    events are harmless. Events for a terminal order (`failed` included, agent-protocol
    P30) are ignored;
*   sends `order_status` from `system:api` to the household agent;
*   on `failed`, calls `runs.finish(status=error, reason=delivery_failed)`;
*   when the order moves to `in_delivery` (agent-protocol P30, #70), closes the order's
    `pending` shortfall with the §10 conditional update and, if a row changed, sends
    `shortfall_closed {reason: picked_up}` from `system:api` to the household agent.
"""

import logging
from typing import Any, Literal

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from services.common import ids, orders, outbox, runs
from services.safety.scope import unscoped

__all__ = [
    "API_AGENT",
    "DELIVERY_FAILED",
    "PICKED_UP",
    "Applied",
    "UnknownStatus",
    "apply_delivery_status",
]

log = logging.getLogger(__name__)

API_AGENT = "system:api"
DELIVERY_FAILED = "delivery_failed"
# The `shortfall_closed` reason when the courier picks the order up (agent-protocol §10).
PICKED_UP = "picked_up"

# `applied`: the order moved. `no_change`: the status maps to no change or is not forward.
# `ignored`: the order is terminal. `unknown_delivery`: no order has this delivery id.
Applied = Literal["applied", "no_change", "ignored", "unknown_delivery"]


class UnknownStatus(ValueError):
    """The courier status is not in `orders.COURIER_STATUS_MAP`."""


_ORDER_SQL = """
SELECT order_id, run_id, household_id, state, total_minor, currency, delivery_agent_id,
       lower(delivery_window) AS window_start, upper(delivery_window) AS window_end
FROM orders WHERE external_delivery_id = %s
FOR UPDATE
"""


# Agent-protocol §10 "Closing early": only a `pending` row closes, so the expiry timer and
# this route never both send `shortfall_closed`.
_CLOSE_SHORTFALL_SQL = """
UPDATE shortfalls SET status = 'closed', closed_at = now()
WHERE order_id = %s AND status = 'pending'
RETURNING shortfall_id
"""


async def _close_shortfall(conn: AsyncConnection, order: dict[str, Any]) -> None:
    """Close the order's pending shortfall, if any, and send `shortfall_closed`."""
    cur = await unscoped(conn, "delivery_route").execute(_CLOSE_SHORTFALL_SQL, (order["order_id"],))
    row = await cur.fetchone()
    if row is None:
        return
    shortfall_id = row[0]
    await outbox.send(
        conn,
        message_id=ids.shortfall_closed_id(shortfall_id),
        run_id=order["run_id"],
        conversation_id=ids.shortfall_conversation_id(shortfall_id),
        causation_id=None,
        from_agent=API_AGENT,
        to_agent=f"household:{order['household_id']}",
        type="shortfall_closed",
        body={
            "shortfall_id": str(shortfall_id),
            "order_id": str(order["order_id"]),
            "reason": PICKED_UP,
        },
    )


def _path(current: str, target: str) -> list[str] | None:
    """The main-path states from `current` up to `target`, or `None` if not forward."""
    if target == "failed":
        return [target]
    if current not in orders.MAIN_PATH or target not in orders.MAIN_PATH:
        return None
    start, end = orders.MAIN_PATH.index(current), orders.MAIN_PATH.index(target)
    if end <= start:
        return None
    return list(orders.MAIN_PATH[start + 1 : end + 1])


def _order_status_body(order: dict[str, Any], state: str) -> dict[str, Any]:
    body: dict[str, Any] = {"order_id": str(order["order_id"]), "state": state}
    if order["total_minor"] is not None:
        body["total_minor"] = int(order["total_minor"])
    if order["currency"] is not None:
        body["currency"] = order["currency"].strip()
    if order["delivery_agent_id"] is not None:
        body["delivery_agent_id"] = order["delivery_agent_id"]
    if order["window_start"] is not None and order["window_end"] is not None:
        body["window"] = {
            "start": order["window_start"].isoformat(),
            "end": order["window_end"].isoformat(),
        }
    if state == "failed":
        body["reason"] = DELIVERY_FAILED
    return body


async def apply_delivery_status(
    conn: AsyncConnection, external_delivery_id: str, status: str
) -> Applied:
    """Apply one courier status to the order booked as `external_delivery_id`."""
    if status not in orders.COURIER_STATUS_MAP:
        raise UnknownStatus(f"unknown courier status {status!r}")
    target = orders.COURIER_STATUS_MAP[status]
    async with conn.transaction():
        async with unscoped(conn, "delivery_route").cursor(row_factory=dict_row) as cur:
            await cur.execute(_ORDER_SQL, (external_delivery_id,))
            order = await cur.fetchone()
        if order is None:
            log.warning(
                "courier status for an unknown delivery",
                extra={"external_delivery_id": external_delivery_id, "status": status},
            )
            return "unknown_delivery"
        current = order["state"]
        if current in orders.TERMINAL:
            return "ignored"
        if target is None:
            return "no_change"
        path = _path(current, target)
        if path is None:
            return "no_change"
        for state in path:
            if not await orders.transition(conn, order["order_id"], current, state):
                raise RuntimeError(f"order {order['order_id']} changed state while locked")
            current = state
            if state == "in_delivery":
                await _close_shortfall(conn, order)
        await outbox.send(
            conn,
            message_id=ids.order_status_id(order["order_id"], current),
            run_id=order["run_id"],
            conversation_id=None,
            causation_id=None,
            from_agent=API_AGENT,
            to_agent=f"household:{order['household_id']}",
            type="order_status",
            body=_order_status_body(order, current),
        )
        if current == "failed":
            await runs.finish(conn, order["run_id"], API_AGENT, "error", DELIVERY_FAILED)
    return "applied"
