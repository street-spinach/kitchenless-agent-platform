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
*   on `failed`, calls `runs.finish(status=error, reason=delivery_failed)`.

Closing a pending shortfall on `in_delivery` (agent-protocol P30) belongs to #70.
"""

import logging
from typing import Any, Literal

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from services.common import ids, orders, outbox, runs

__all__ = ["API_AGENT", "DELIVERY_FAILED", "Applied", "UnknownStatus", "apply_delivery_status"]

log = logging.getLogger(__name__)

API_AGENT = "system:api"
DELIVERY_FAILED = "delivery_failed"

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
        async with conn.cursor(row_factory=dict_row) as cur:
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
