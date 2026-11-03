"""Reminders (experience.md §10, EX-I41): derived each minute from current state.

*   `due_reminders(conn, now)`: the reminders due now and not yet handled, by the §10 rules:
    *   `delivery_today`: an order `courier_booked` or `in_delivery` whose delivery window
        starts today in household time, after 08:00 household time; to every member of the
        household (children are skipped below); `ref_id` is the order id.
    *   `pantry_summary`: `pantry.is_enabled(env)`, Saturday after 10:00 household time; to
        the approver; `ref_id` is `<household_id>:<ISO week>`.
*   `apply_skips(conn, due, now)`: records a `skipped` row for a person who opted out
    (`reminders_off_at`), is a child, or already got a reminder today (household time).
    Quiet hours (21:00-08:00) record nothing: the reminder is not yet due. Returns the rest.
*   `send(conn, due)`: one `reminders` row (`sent`), a `household_id = NULL` run with
    `trigger.source = "reminder"`, and one `whatsapp_out` with `message_id =
    ids.reminder_message_id(reminder_id)` and `ref = {reminder, <reminder_id>}`. A reminder
    is never held: outside the window with no Meta template the sender records it
    `skipped`, reason `no_template` (`templates.holdable` is false for reminder keys).
*   `reminder_pass(conn, now)`: all three, once; the sweeper calls it every 60 s (EX-I42).

`UNIQUE (kind, ref_id, user_id)` on `reminders` makes every reminder handled once.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from services.common import ids, outbox, pantry, runs
from services.common.trace import TraceStore
from services.safety.scope import unscoped

__all__ = [
    "DELIVERY_TODAY",
    "PANTRY_SUMMARY",
    "REMINDER_SECONDS",
    "Due",
    "apply_skips",
    "due_reminders",
    "reminder_pass",
    "send",
]

log = logging.getLogger(__name__)

REMINDER_SECONDS = 60.0
SWEEPER = "system:sweeper"
WHATSAPP = "channel:whatsapp"
DELIVERY_TODAY = "delivery_today"
PANTRY_SUMMARY = "pantry_summary"
TEMPLATES = {DELIVERY_TODAY: "reminder_delivery_today", PANTRY_SUMMARY: "reminder_pantry_summary"}
STOP_BUTTON = {"payload": ids.REMINDER_STOP, "title": "Stop these"}
PANTRY_BUTTON = {"payload": ids.page_button("pantry"), "title": "Open pantry"}
DELIVERY_FROM_HOUR = 8
PANTRY_FROM_HOUR = 10
QUIET_FROM_HOUR = 21  # quiet hours are 21:00-08:00 household time
QUIET_TO_HOUR = 8
SATURDAY = 6  # isodow

# Skip reasons (fixed codes, experience §10).
OPTED_OUT = "opted_out"
CHILD = "child"
DAILY_CAP = "daily_cap"
SENT = "sent"
SKIPPED = "skipped"

_TRACES = TraceStore(None)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Due:
    kind: str
    ref_id: str
    user_id: UUID
    household_id: str


_DELIVERY_SQL = """
SELECT o.order_id::text AS ref_id, hm.user_id, h.household_id
  FROM orders o
  JOIN households h ON h.household_id = o.household_id
  JOIN household_members hm ON hm.household_id = h.household_id
 WHERE o.state IN ('courier_booked', 'in_delivery')
   AND o.delivery_window IS NOT NULL
   AND (lower(o.delivery_window) AT TIME ZONE h.timezone)::date
       = (%(now)s::timestamptz AT TIME ZONE h.timezone)::date
   AND extract(hour FROM %(now)s::timestamptz AT TIME ZONE h.timezone) >= %(from_hour)s
   AND NOT EXISTS (SELECT 1 FROM reminders r WHERE r.kind = 'delivery_today'
                   AND r.ref_id = o.order_id::text AND r.user_id = hm.user_id)
 ORDER BY o.order_id, hm.user_id
"""

_PANTRY_SQL = """
SELECT h.household_id, h.env, ar.approver_user_id AS user_id,
       to_char(%(now)s::timestamptz AT TIME ZONE h.timezone, 'IYYY-"W"IW') AS week
  FROM households h
  JOIN approval_rules ar ON ar.household_id = h.household_id
 WHERE ar.approver_user_id IS NOT NULL
   AND extract(isodow FROM %(now)s::timestamptz AT TIME ZONE h.timezone) = %(saturday)s
   AND extract(hour FROM %(now)s::timestamptz AT TIME ZONE h.timezone) >= %(from_hour)s
   AND NOT EXISTS (
     SELECT 1 FROM reminders r WHERE r.kind = 'pantry_summary' AND r.user_id = ar.approver_user_id
       AND r.ref_id = h.household_id || ':'
           || to_char(%(now)s::timestamptz AT TIME ZONE h.timezone, 'IYYY-"W"IW'))
 ORDER BY h.household_id
"""


async def due_reminders(conn: AsyncConnection, now: datetime) -> list[Due]:
    """The reminders due at `now` that have no `reminders` row yet."""
    q = unscoped(conn, "reminder_pass")
    due: list[Due] = []
    async with q.cursor(row_factory=dict_row) as cur:
        await cur.execute(_DELIVERY_SQL, {"now": now, "from_hour": DELIVERY_FROM_HOUR})
        for row in await cur.fetchall():
            due.append(Due(DELIVERY_TODAY, row["ref_id"], row["user_id"], row["household_id"]))
        await cur.execute(
            _PANTRY_SQL, {"now": now, "saturday": SATURDAY, "from_hour": PANTRY_FROM_HOUR}
        )
        for row in await cur.fetchall():
            # EX-A17: the one rule for whether the pantry is on.
            if pantry.is_enabled(row["env"]):
                ref_id = f"{row['household_id']}:{row['week']}"
                due.append(Due(PANTRY_SUMMARY, ref_id, row["user_id"], row["household_id"]))
    return due


_PERSON_SQL = """
SELECT hm.role, h.timezone,
       EXISTS (SELECT 1 FROM channel_identities ci
                WHERE ci.user_id = hm.user_id AND ci.reminders_off_at IS NOT NULL) AS opted_out,
       EXISTS (SELECT 1 FROM reminders r
                WHERE r.user_id = hm.user_id AND r.status = 'sent'
                  AND (r.created_at AT TIME ZONE h.timezone)::date
                      = (%(now)s::timestamptz AT TIME ZONE h.timezone)::date) AS sent_today
  FROM household_members hm JOIN households h ON h.household_id = hm.household_id
 WHERE hm.user_id = %(user_id)s AND hm.household_id = %(household_id)s
"""

_RECORD_SQL = """
INSERT INTO reminders (household_id, user_id, kind, ref_id, status, reason, created_at)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (kind, ref_id, user_id) DO NOTHING
RETURNING reminder_id
"""


def _quiet(local: datetime) -> bool:
    return local.hour >= QUIET_FROM_HOUR or local.hour < QUIET_TO_HOUR


async def _record(
    conn: AsyncConnection, due: Due, status: str, reason: str | None, now: datetime
) -> int | None:
    cur = await unscoped(conn, "reminder_pass").execute(
        _RECORD_SQL,
        (due.household_id, due.user_id, due.kind, due.ref_id, status, reason, now),
    )
    row = await cur.fetchone()
    return None if row is None else int(row[0])


async def apply_skips(conn: AsyncConnection, due: list[Due], now: datetime) -> list[Due]:
    """Record the skipped ones; return those to send now, at most one per person."""
    out: list[Due] = []
    sending: set[UUID] = set()
    for d in due:
        async with unscoped(conn, "reminder_pass").cursor(row_factory=dict_row) as cur:
            await cur.execute(
                _PERSON_SQL, {"now": now, "user_id": d.user_id, "household_id": d.household_id}
            )
            person = await cur.fetchone()
        if person is None:
            continue  # no longer a member: nothing to send
        if person["opted_out"]:
            await _record(conn, d, SKIPPED, OPTED_OUT, now)
        elif person["role"] == "child":
            await _record(conn, d, SKIPPED, CHILD, now)
        elif person["sent_today"] or d.user_id in sending:
            await _record(conn, d, SKIPPED, DAILY_CAP, now)
        elif _quiet(now.astimezone(ZoneInfo(person["timezone"]))):
            continue  # not yet due: nothing is recorded
        else:
            sending.add(d.user_id)
            out.append(d)
    return out


_STORE_SQL = """
SELECT org.name AS store, lower(o.delivery_window) AS starts, upper(o.delivery_window) AS ends,
       h.timezone
  FROM orders o
  JOIN households h ON h.household_id = o.household_id
  JOIN agent_cards c ON c.agent_id = o.seller_agent_id
  JOIN organizations org ON c.owner_ref = 'org:' || org.org_id
 WHERE o.order_id = %s AND o.household_id = %s
"""

_LATEST_PANTRY_SQL = """
SELECT DISTINCT ON (pantry_item) pantry_item, event_id, kind, ts
  FROM pantry_events
 WHERE household_id = %s AND pantry_item IS NOT NULL
 ORDER BY pantry_item, event_id DESC
"""


def _clock(at: datetime) -> str:
    hour = at.hour % 12 or 12
    return f"{hour}:{at.minute:02d} {'am' if at.hour < 12 else 'pm'}"


def _names(items: list[str]) -> str:
    return ", ".join(sorted(items)) if items else "nothing"


async def _params(conn: AsyncConnection, due: Due, now: datetime) -> dict[str, str] | None:
    """The text's parameters, built by code from facts; `None` if they can't be built."""
    q = unscoped(conn, "reminder_pass")
    if due.kind == DELIVERY_TODAY:
        async with q.cursor(row_factory=dict_row) as cur:
            await cur.execute(_STORE_SQL, (UUID(due.ref_id), due.household_id))
            row = await cur.fetchone()
        if row is None or row["starts"] is None or row["ends"] is None:
            return None
        tz = ZoneInfo(row["timezone"])
        window = f"{_clock(row['starts'].astimezone(tz))}-{_clock(row['ends'].astimezone(tz))}"
        return {"store": row["store"], "window": window}
    vocab = pantry.load_vocabulary()
    async with q.cursor(row_factory=dict_row) as cur:
        await cur.execute(_LATEST_PANTRY_SQL, (due.household_id,))
        latest = await cur.fetchall()
    at_home: list[str] = []
    unsure: list[str] = []
    for event in latest:
        days = vocab.window_days.get(event["pantry_item"])
        if days is None:
            continue
        est = pantry.estimate(event, days, now)
        if est.trusted:
            at_home.append(est.pantry_item)
        elif est.present:
            unsure.append(est.pantry_item)
    return {
        "at_home": _names(at_home),
        "unsure": _names(unsure),
        "at_home_count": str(len(at_home)),
        "unsure_count": str(len(unsure)),
    }


async def send(conn: AsyncConnection, due: Due, now: datetime) -> UUID | None:
    """Record and send one reminder in one transaction; return its message id, or `None`
    when it was already handled or its facts are gone."""
    async with conn.transaction():
        params = await _params(conn, due, now)
        if params is None:
            return None
        reminder_id = await _record(conn, due, SENT, None, now)
        if reminder_id is None:
            return None  # another pass handled it
        message_id = ids.reminder_message_id(reminder_id)
        await unscoped(conn, "reminder_pass").execute(
            "UPDATE reminders SET message_id = %s WHERE reminder_id = %s",
            (message_id, reminder_id),
        )
        run_id = await runs.start(
            conn,
            household_id=None,
            kind="chat",
            trigger={"source": "reminder", "user_id": str(due.user_id)},
        )
        buttons = [STOP_BUTTON] if due.kind == DELIVERY_TODAY else [PANTRY_BUTTON, STOP_BUTTON]
        body: dict[str, Any] = {
            "user_id": str(due.user_id),
            "template": TEMPLATES[due.kind],
            "params": params,
            "buttons": buttons,
            "ref": {"kind": "reminder", "id": str(reminder_id)},
        }
        # §15: one `decision` row, node `reminder`, by `system:sweeper`.
        await _TRACES.write(
            run_id,
            SWEEPER,
            "decision",
            {
                "node": "reminder",
                "choice": due.kind,
                "explanation": "",
                "reminder_id": reminder_id,
                "ref_id": due.ref_id,
            },
            on_behalf_of=None,
            conn=conn,
        )
        await outbox.send(
            conn,
            message_id=message_id,
            run_id=run_id,
            conversation_id=None,
            causation_id=None,
            from_agent=SWEEPER,
            to_agent=WHATSAPP,
            type="whatsapp_out",
            body=body,
        )
        await runs.finish(conn, run_id, SWEEPER, "completed", "reminder_sent")
    return message_id


async def reminder_pass(conn: AsyncConnection, now: datetime) -> int:
    """One pass: what is due, the skips, then each send. Returns how many were sent. A
    reminder whose send raises is rolled back and tried again on the next pass."""
    async with conn.transaction():
        to_send = await apply_skips(conn, await due_reminders(conn, now), now)
    sent = 0
    for due in to_send:
        try:
            if await send(conn, due, now) is not None:
                sent += 1
        except Exception:
            log.exception("reminder %s %s for %s failed", due.kind, due.ref_id, due.user_id)
    return sent
