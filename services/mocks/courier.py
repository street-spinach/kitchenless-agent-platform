"""Mock courier backend and progression loop (architecture item 46, amended 2026-11-02, #48).

Everything works over the `mock_deliveries` rows of one courier card and one `env`.

*   **Rules.** The card's `business_rules` kind `delivery` = `{fee_minor, slot_minutes,
    slot_capacity}` is read on every quote and booking. A card without a valid rule fails
    with `AdapterFailure("adapter_not_configured", retryable=False)`.
*   **Quote.** `price_minor` is `fee_minor` in the card's currency. `windows` are the whole
    `slot_minutes` slots inside the requested window, back to back from its start, without
    the slots that already overlap `slot_capacity` `booked` rows of the same `env`, card and
    zone. The list is in start order and may be empty.
*   **Booking.** Idempotent on the key; `external_delivery_id` is the key (the vendor key).
    A booking joins the `batch_id` of the earliest booked overlapping row of the same `env`,
    card and zone, and keeps its own window; otherwise its `batch_id` is
    `ids.mock_batch_id(external_delivery_id)`.
*   **Progression.** `progress_forever()` moves each row `booked → picked_up → delivered` at
    `next_state_at`. Each step lasts `MOCK_COURIER_STEP_SECONDS` plus the largest
    `params.delay_seconds` of the active `courier_delayed` events for the card and `env`.
    Each change is first posted to `api` `/internal/delivery-status`; the row changes only
    after a 2xx, otherwise the same post is retried on the next tick.

The delivery address is passed to `book_delivery` like a real courier's, but the mock never
stores, logs or returns it.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx
from psycopg import AsyncConnection
from psycopg.types.range import Range

from services.common import db, ids
from services.common.config import get_settings
from services.common.modes import NOT_CONFIGURED
from services.mcp.meta import AdapterFailure
from services.mocks.stores import INVALID_REQUEST, active_events

__all__ = [
    "NEXT_STATE",
    "STATUS_PATH",
    "TICK_SECONDS",
    "MockCourier",
    "Post",
    "post_status",
    "progress_forever",
    "tick",
]

log = logging.getLogger(__name__)

RULE_KIND = "delivery"
NEXT_STATE = {"booked": "picked_up", "picked_up": "delivered"}
STATUS_PATH = "/internal/delivery-status"
TICK_SECONDS = 1.0
POST_TIMEOUT_SECONDS = 5.0

# Posts one status body to `api` and returns the HTTP status code.
Post = Callable[[dict[str, Any]], Awaitable[int]]


class WindowLike(Protocol):
    """The protocol `Window` (`services/common/protocol/schemas.py`)."""

    @property
    def start(self) -> datetime: ...
    @property
    def end(self) -> datetime: ...


def _aware(window: WindowLike) -> tuple[datetime, datetime]:
    start, end = window.start, window.end
    if any(t.tzinfo is None or t.utcoffset() is None for t in (start, end)):
        raise AdapterFailure(INVALID_REQUEST, retryable=False)
    return start, end


def _window(start: datetime, end: datetime) -> dict[str, str]:
    return {"start": start.astimezone(UTC).isoformat(), "end": end.astimezone(UTC).isoformat()}


def _positive_int(value: Any, *, zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if zero else 1):
        raise AdapterFailure(NOT_CONFIGURED, retryable=False)
    return value


async def delivery_rule(conn: AsyncConnection, agent_id: str) -> tuple[int, int, int]:
    """`(fee_minor, slot_minutes, slot_capacity)` of the card's `delivery` rule."""
    cur = await conn.execute(
        "SELECT value FROM business_rules WHERE agent_id = %s AND kind = %s",
        (agent_id, RULE_KIND),
    )
    row = await cur.fetchone()
    value = row[0] if row is not None else None
    if not isinstance(value, Mapping):
        raise AdapterFailure(NOT_CONFIGURED, retryable=False)
    return (
        _positive_int(value.get("fee_minor"), zero=True),
        _positive_int(value.get("slot_minutes")),
        _positive_int(value.get("slot_capacity")),
    )


async def delay_seconds(conn: AsyncConnection, env: str, agent_id: str) -> int:
    """The largest `delay_seconds` of the active `courier_delayed` events, or 0."""
    delays = [
        params.get("delay_seconds")
        for kind, params in await active_events(conn, env, agent_id)
        if kind == "courier_delayed" and isinstance(params, Mapping)
    ]
    valid = [d for d in delays if isinstance(d, int) and not isinstance(d, bool) and d > 0]
    return max(valid, default=0)


async def step(conn: AsyncConnection, env: str, agent_id: str) -> timedelta:
    """How long the next progression step of this card lasts in `env`."""
    base = get_settings().mock_courier_step_seconds
    return timedelta(seconds=base + await delay_seconds(conn, env, agent_id))


def _overlaps(booked: Range[datetime], start: datetime, end: datetime) -> bool:
    lower, upper = booked.lower, booked.upper
    return (upper is None or start < upper) and (lower is None or lower < end)


class MockCourier:
    """The mock backend of one courier card in one `env`."""

    def __init__(self, *, env: str, agent_id: str, currency: str | None) -> None:
        self.env = env
        self.agent_id = agent_id
        self.currency = currency

    async def quote_delivery(self, zone_id: str, window: WindowLike) -> dict[str, Any]:
        """`{price_minor, currency, windows}` for `zone_id` inside `window`."""
        start, end = _aware(window)
        async with db.get_pool().connection() as conn:
            fee_minor, slot_minutes, capacity = await delivery_rule(conn, self.agent_id)
            cur = await conn.execute(
                'SELECT "window" FROM mock_deliveries'
                " WHERE env = %s AND agent_id = %s AND zone_id = %s AND state = 'booked'"
                ' AND "window" && tstzrange(%s, %s)',
                (self.env, self.agent_id, zone_id, start, end),
            )
            booked = [r[0] for r in await cur.fetchall()]
        slot = timedelta(minutes=slot_minutes)
        windows = []
        slot_start = start
        while slot_start + slot <= end:
            slot_end = slot_start + slot
            taken = sum(1 for b in booked if _overlaps(b, slot_start, slot_end))
            if taken < capacity:
                windows.append(_window(slot_start, slot_end))
            slot_start = slot_end
        return {"price_minor": fee_minor, "currency": self.currency, "windows": windows}

    async def book_delivery(
        self,
        *,
        idempotency_key: str,
        order_id: str,
        zone_id: str,
        window: WindowLike,
        address: Any,
    ) -> dict[str, Any]:
        """Book once per `idempotency_key` (the vendor key, also `external_delivery_id`).

        `address` is what a real courier needs; the mock does not keep it.
        """
        start, end = _aware(window)
        if not start < end:
            raise AdapterFailure(INVALID_REQUEST, retryable=False)
        edi = idempotency_key
        booked = Range(start, end, "[)")
        async with db.get_pool().connection() as conn:
            await delivery_rule(conn, self.agent_id)
            async with conn.transaction():
                # One booking at a time per card, env and zone, so two bookings never miss
                # each other's batch.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"mock_courier|{self.env}|{self.agent_id}|{zone_id}",),
                )
                cur = await conn.execute(
                    "SELECT batch_id FROM mock_deliveries"
                    " WHERE env = %s AND agent_id = %s AND zone_id = %s AND state = 'booked'"
                    ' AND "window" && %s AND batch_id IS NOT NULL'
                    " ORDER BY created_at, external_delivery_id LIMIT 1",
                    (self.env, self.agent_id, zone_id, booked),
                )
                row = await cur.fetchone()
                batch_id = row[0] if row is not None else ids.mock_batch_id(edi)
                next_state_at = datetime.now(UTC) + await step(conn, self.env, self.agent_id)
                cur = await conn.execute(
                    "INSERT INTO mock_deliveries (external_delivery_id, env, agent_id,"
                    ' idempotency_key, zone_id, "window", batch_id, state, next_state_at)'
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, 'booked', %s)"
                    " ON CONFLICT DO NOTHING RETURNING external_delivery_id",
                    (edi, self.env, self.agent_id, edi, zone_id, booked, batch_id, next_state_at),
                )
                inserted = await cur.fetchone() is not None
            if not inserted:
                return await self._stored(conn, edi)
        return {"external_delivery_id": edi, "state": "booked", "window": _window(start, end)}

    async def get_delivery_status(self, external_delivery_id: str) -> dict[str, Any]:
        """`{external_delivery_id, state, window}` of one of this card's deliveries."""
        async with db.get_pool().connection() as conn:
            return await self._stored(conn, external_delivery_id)

    async def _stored(self, conn: AsyncConnection, external_delivery_id: str) -> dict[str, Any]:
        cur = await conn.execute(
            'SELECT state, "window" FROM mock_deliveries'
            " WHERE external_delivery_id = %s AND env = %s AND agent_id = %s",
            (external_delivery_id, self.env, self.agent_id),
        )
        row = await cur.fetchone()
        if row is None:
            # Unknown, or another card's or env's: a vendor would refuse it.
            raise AdapterFailure(INVALID_REQUEST, retryable=False)
        state, window = row
        return {
            "external_delivery_id": external_delivery_id,
            "state": state,
            "window": _window(window.lower, window.upper),
        }


# --- progression ----------------------------------------------------------------------


async def post_status(body: dict[str, Any]) -> int:
    """POST `body` to `api` `/internal/delivery-status`; return the HTTP status code."""
    url = get_settings().api_url.rstrip("/") + STATUS_PATH
    async with httpx.AsyncClient(timeout=POST_TIMEOUT_SECONDS) as client:
        response = await client.post(url, json=body)
    return response.status_code


async def tick(post: Post, now: datetime | None = None) -> int:
    """Advance every due delivery by one state; return how many moved.

    A row moves only after `post` returns a 2xx for its new state. Any other result, or
    an exception, is logged and the row stays due, so the next tick posts it again.
    """
    now = now or datetime.now(UTC)
    pool = db.get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT external_delivery_id, env, agent_id, state FROM mock_deliveries"
            " WHERE state = ANY(%s) AND next_state_at <= %s"
            " ORDER BY next_state_at, external_delivery_id",
            (list(NEXT_STATE), now),
        )
        due = await cur.fetchall()
    moved = 0
    for edi, env, agent_id, state in due:
        new = NEXT_STATE[state]
        body = {"external_delivery_id": edi, "status": new, "event_id": f"{edi}:{new}"}
        try:
            status = await post(body)
        except Exception:
            log.warning("mock courier status post failed", extra={"event_id": body["event_id"]})
            continue
        if not 200 <= status < 300:
            log.warning(
                "mock courier status post refused",
                extra={"event_id": body["event_id"], "status_code": status},
            )
            continue
        async with pool.connection() as conn:
            next_at = None if new not in NEXT_STATE else now + await step(conn, env, agent_id)
            cur = await conn.execute(
                "UPDATE mock_deliveries SET state = %s, next_state_at = %s"
                " WHERE external_delivery_id = %s AND state = %s",
                (new, next_at, edi, state),
            )
            moved += cur.rowcount
    return moved


async def progress_forever(post: Post = post_status, interval: float = TICK_SECONDS) -> None:
    """Run `tick` every `interval` seconds until cancelled. A failed tick is logged."""
    while True:
        try:
            await tick(post)
        except Exception:
            log.exception("mock courier tick failed")
        await asyncio.sleep(interval)
