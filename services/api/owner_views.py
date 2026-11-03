"""The owner app's read views (experience.md §11, EX-I47): orders and notices.

Every query goes through `scope.org_scoped`, so it binds `%(org)s` and, on `quotes` or
`orders`, keeps only rows whose seller agent belongs to the organisation. The responses
name no household: an order is shown by `ids.org_order_ref`, never by its id, household,
person or address.

*   `orders(conn, org)`: live orders (a seller of the org is set and the state is not
    final) and open negotiations (the org's quotes still `open` or `countered` and not yet
    expired), newest first. `rationale` is the latest checked rationale from the org's own
    `offer` for that order.
*   `summary(conn, org)`: today's (UTC) quotes, quotes won (`accepted`) and orders.
*   `notices(conn, org, limit)`: one fixed sentence per kind in `NOTICE_TEXT`, newest
    first.
*   `insights(conn, org, now)`: this ISO week's busiest delivery hour, most-asked
    constraint code and quotes won, from live households only (`env = 'live'`). A cell
    covering fewer than `INSIGHTS_MIN_HOUSEHOLDS` distinct households is hidden. Text is
    built by code. `sample_insights(now)` is the fixed, labelled sample.
*   `connection(conn, org)`: the org's vendor key, its resolved mode and the panel
    override's `set_at`. No health probe is made.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from psycopg import AsyncConnection

from services.agents.common.model import check_rationale
from services.common import directory, ids, modes, vendors
from services.common.protocol.schemas import AllergenCode, DietCode
from services.safety import scope

__all__ = [
    "INSIGHTS_MIN_HOUSEHOLDS",
    "LIVE_QUOTE_STATUSES",
    "MAX_ROWS",
    "NOTICE_TEXT",
    "connection",
    "insights",
    "notices",
    "orders",
    "sample_insights",
    "summary",
]

MAX_ROWS = 50
FINAL_ORDER_STATES = ("delivered", "cancelled", "expired", "failed")
LIVE_QUOTE_STATUSES = ("open", "countered")
NOTICE_TEXT = {
    "counter_refused_floor": "A buyer's counter-offer was below your discount limit, so it "
    "was not accepted.",
    "offer_expired": "An offer expired before the buyer accepted it.",
    "order_cancelled": "An order was cancelled or expired before delivery.",
}

# Plain literals (no interpolation), so the scope scan sees `%(org)s` in each one.
_ORDERS_SQL = """
SELECT * FROM (
  SELECT 'order' AS kind, o.order_id, o.state, o.total_minor AS amount_minor, o.currency,
         o.updated_at,
         (SELECT t.payload -> 'body' FROM agent_traces t
            WHERE t.run_id = o.run_id AND t.kind = 'message'
              AND t.payload ->> 'type' = 'offer'
              AND t.payload -> 'body' ->> 'order_id' = o.order_id::text
              AND t.agent_id IN (SELECT agent_id FROM agent_cards
                                  WHERE owner_ref = 'org:' || %(org)s)
            ORDER BY t.seq DESC LIMIT 1) AS offer
    FROM orders o
   WHERE o.seller_agent_id IN (SELECT agent_id FROM agent_cards
                                 WHERE owner_ref = 'org:' || %(org)s)
     AND o.state <> ALL(%(final)s)
  UNION ALL
  SELECT 'quote' AS kind, q.order_id, q.status AS state, q.total_minor AS amount_minor,
         q.currency, q.created_at AS updated_at,
         (SELECT t.payload -> 'body' FROM agent_traces t
            WHERE t.run_id = q.run_id AND t.kind = 'message'
              AND t.payload ->> 'type' = 'offer'
              AND t.payload -> 'body' ->> 'order_id' = q.order_id::text
              AND t.agent_id IN (SELECT agent_id FROM agent_cards
                                  WHERE owner_ref = 'org:' || %(org)s)
            ORDER BY t.seq DESC LIMIT 1) AS offer
    FROM quotes q
   WHERE q.seller_agent_id IN (SELECT agent_id FROM agent_cards
                                 WHERE owner_ref = 'org:' || %(org)s)
     AND q.status = ANY(%(live)s) AND q.expires_at > now()
) live
ORDER BY updated_at DESC
LIMIT %(limit)s
"""

_SUMMARY_SQL = """
SELECT
  (SELECT count(*) FROM quotes q
    WHERE q.seller_agent_id IN (SELECT agent_id FROM agent_cards
                                  WHERE owner_ref = 'org:' || %(org)s)
      AND q.created_at >= %(day)s),
  (SELECT count(*) FROM quotes q
    WHERE q.seller_agent_id IN (SELECT agent_id FROM agent_cards
                                  WHERE owner_ref = 'org:' || %(org)s)
      AND q.created_at >= %(day)s AND q.status = 'accepted'),
  (SELECT count(*) FROM orders o
    WHERE o.seller_agent_id IN (SELECT agent_id FROM agent_cards
                                  WHERE owner_ref = 'org:' || %(org)s)
      AND o.created_at >= %(day)s)
"""

_NOTICES_SQL = """
SELECT kind, noticed_at FROM (
  SELECT 'counter_refused_floor' AS kind, t.ts AS noticed_at
    FROM agent_traces t
   WHERE t.kind = 'decision' AND t.payload ->> 'node' = 'owner_floor'
     AND t.agent_id IN (SELECT agent_id FROM agent_cards
                         WHERE owner_ref = 'org:' || %(org)s)
  UNION ALL
  SELECT 'offer_expired', q.expires_at
    FROM quotes q
   WHERE q.seller_agent_id IN (SELECT agent_id FROM agent_cards
                                 WHERE owner_ref = 'org:' || %(org)s)
     AND q.status = 'expired'
  UNION ALL
  SELECT 'order_cancelled', o.updated_at
    FROM orders o
   WHERE o.seller_agent_id IN (SELECT agent_id FROM agent_cards
                                 WHERE owner_ref = 'org:' || %(org)s)
     AND o.state IN ('cancelled', 'expired')
) n
ORDER BY noticed_at DESC
LIMIT %(limit)s
"""


def _rationale(offer: Any) -> str | None:
    """The offer's rationale, checked again against its own body (fail closed)."""
    if not isinstance(offer, dict):
        return None
    return check_rationale(
        offer.get("rationale"), {k: v for k, v in offer.items() if k != "rationale"}
    )


async def orders(conn: AsyncConnection, org: scope.OrgScope) -> list[dict[str, Any]]:
    q = scope.org_scoped(conn, org)
    rows = await q.fetch(
        _ORDERS_SQL, final=list(FINAL_ORDER_STATES), live=list(LIVE_QUOTE_STATUSES), limit=MAX_ROWS
    )
    return [
        {
            "ref": ids.org_order_ref(org.org_id, order_id),
            "kind": kind,
            "state": state,
            "amount_minor": amount,
            "currency": currency,
            "updated_at": updated_at.isoformat(),
            "rationale": _rationale(offer),
        }
        for kind, order_id, state, amount, currency, updated_at, offer in rows
    ]


async def summary(conn: AsyncConnection, org: scope.OrgScope, day: Any) -> dict[str, int]:
    """Counts since `day` (the start of today, UTC, from the route)."""
    q = scope.org_scoped(conn, org)
    (row,) = await q.fetch(_SUMMARY_SQL, day=day)
    quotes_today, won_today, orders_today = row
    return {
        "quotes_today": int(quotes_today),
        "won_today": int(won_today),
        "orders_today": int(orders_today),
    }


async def notices(conn: AsyncConnection, org: scope.OrgScope, limit: int) -> list[dict[str, Any]]:
    q = scope.org_scoped(conn, org)
    rows = await q.fetch(_NOTICES_SQL, limit=limit)
    return [{"kind": kind, "text": NOTICE_TEXT[kind], "at": at.isoformat()} for kind, at in rows]


# --- insights (EX-OD12) ------------------------------------------------------------------------

INSIGHTS_MIN_HOUSEHOLDS = 5
SAMPLE_FILE = Path(__file__).with_name("insights_sample.yaml")
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
LABELS = {"busiest_hour": "Busiest", "top_constraint": "Most asked", "quotes_won": "Quotes won"}
_ALLERGENS = sorted(a.value for a in AllergenCode)
_DIETS = sorted(d.value for d in DietCode)

_HOURS_SQL = """
SELECT extract(isodow FROM lower(o.delivery_window) AT TIME ZONE h.timezone)::int AS dow,
       extract(hour FROM lower(o.delivery_window) AT TIME ZONE h.timezone)::int AS hour,
       count(*) AS n, count(DISTINCT o.household_id) AS households
  FROM orders o
  JOIN households h ON h.household_id = o.household_id
 WHERE o.seller_agent_id IN (SELECT agent_id FROM agent_cards
                              WHERE owner_ref = 'org:' || %(org)s)
   AND h.env = 'live' AND o.delivery_window IS NOT NULL
   AND lower(o.delivery_window) >= %(start)s AND lower(o.delivery_window) < %(end)s
 GROUP BY 1, 2
 ORDER BY n DESC, households DESC, dow, hour
 LIMIT 1
"""

_TAGS_SQL = """
SELECT tag.kind, tag.code, count(DISTINCT o.household_id) AS households
  FROM quotes q
  JOIN orders o ON o.order_id = q.order_id
  JOIN households h ON h.household_id = o.household_id
  CROSS JOIN LATERAL (
    SELECT 'allergen' AS kind, a AS code
      FROM jsonb_array_elements_text(CASE WHEN jsonb_typeof(q.constraints -> 'allergens')
                                     = 'array' THEN q.constraints -> 'allergens'
                                     ELSE '[]'::jsonb END) a
     WHERE a = ANY(%(allergens)s)
    UNION ALL
    SELECT 'diet', d
      FROM jsonb_array_elements_text(CASE WHEN jsonb_typeof(q.constraints -> 'diets')
                                     = 'array' THEN q.constraints -> 'diets'
                                     ELSE '[]'::jsonb END) d
     WHERE d = ANY(%(diets)s)
  ) tag
 WHERE q.seller_agent_id IN (SELECT agent_id FROM agent_cards
                              WHERE owner_ref = 'org:' || %(org)s)
   AND h.env = 'live' AND q.created_at >= %(start)s AND q.created_at < %(end)s
 GROUP BY 1, 2
 ORDER BY households DESC, tag.kind, tag.code
 LIMIT 1
"""

_WON_SQL = """
SELECT count(*) FILTER (WHERE q.status = 'accepted') AS won, count(*) AS total,
       count(DISTINCT o.household_id) AS households
  FROM quotes q
  JOIN orders o ON o.order_id = q.order_id
  JOIN households h ON h.household_id = o.household_id
 WHERE q.seller_agent_id IN (SELECT agent_id FROM agent_cards
                              WHERE owner_ref = 'org:' || %(org)s)
   AND h.env = 'live' AND q.created_at >= %(start)s AND q.created_at < %(end)s
"""


def iso_week(now: datetime) -> tuple[str, datetime, datetime]:
    """The ISO week of `now` (UTC): its label and its [start, end) bounds."""
    year, week, weekday = now.isocalendar()
    start = (now - timedelta(days=weekday - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return f"{year}-W{week:02d}", start, start + timedelta(days=7)


def _cell(metric: str, households: int, value: str) -> dict[str, Any]:
    if households < INSIGHTS_MIN_HOUSEHOLDS:
        return {"metric": metric, "hidden": True}
    return {"metric": metric, "label": LABELS[metric], "value": value}


def _hour_text(dow: int, hour: int) -> str:
    return f"{DAYS[dow - 1]} {hour:02d}:00–{(hour + 1) % 24:02d}:00"


def _tag_text(kind: str, code: str) -> str:
    words = code.replace("_", " ")
    return f"no {words}" if kind == "allergen" else words


async def insights(conn: AsyncConnection, org: scope.OrgScope, now: datetime) -> dict[str, Any]:
    week, start, end = iso_week(now)
    q = scope.org_scoped(conn, org)
    hours = await q.fetch(_HOURS_SQL, start=start, end=end)
    tags = await q.fetch(_TAGS_SQL, start=start, end=end, allergens=_ALLERGENS, diets=_DIETS)
    ((won, total, won_households),) = await q.fetch(_WON_SQL, start=start, end=end)
    cells = [
        _cell("busiest_hour", hours[0][3], _hour_text(hours[0][0], hours[0][1]))
        if hours
        else _cell("busiest_hour", 0, ""),
        _cell("top_constraint", tags[0][2], _tag_text(tags[0][0], tags[0][1]))
        if tags
        else _cell("top_constraint", 0, ""),
        _cell("quotes_won", int(won_households), f"{int(won)} of {int(total)}"),
    ]
    return {"week": week, "sample": False, "cells": cells}


def sample_insights(now: datetime) -> dict[str, Any]:
    """The fixed sample (`INSIGHTS_SAMPLE=1`), always marked `sample: true`."""
    data = yaml.safe_load(SAMPLE_FILE.read_text())
    cells = [
        {"metric": c["metric"], "label": LABELS[c["metric"]], "value": str(c["value"])}
        for c in data["cells"]
    ]
    return {"week": iso_week(now)[0], "sample": True, "cells": cells}


# --- connection ----------------------------------------------------------------------------

_CARDS_SQL = """
SELECT agent_id FROM agent_cards WHERE owner_ref = 'org:' || %(org)s ORDER BY agent_id LIMIT 1
"""

_SET_AT_SQL = """
SELECT set_at FROM adapter_modes
 WHERE vendor = %(vendor)s
   AND EXISTS (SELECT 1 FROM agent_cards WHERE owner_ref = 'org:' || %(org)s)
"""

_FIRST_TOOL = {"delivery": "quote_delivery"}


async def connection(conn: AsyncConnection, org: scope.OrgScope) -> dict[str, Any] | None:
    """`{vendor, mode, set_at}` for the org's card, or None when it has none. A `mock` card,
    or a vendor with no key, is `mock` (fail closed: never shown as real)."""
    q = scope.org_scoped(conn, org)
    rows = await q.fetch(_CARDS_SQL)
    if not rows:
        return None
    from services.mcp.meta import AdapterFailure  # lazily, as `vendors` does

    card = await directory.card(conn, rows[0][0])
    area = card.agent_class
    try:
        key = vendors.card_vendor_key(area, card, _FIRST_TOOL.get(area, "quote"))
    except AdapterFailure:
        key = None
    if key is None:
        return {"vendor": card.vendor or vendors.MOCK_VENDOR, "mode": "mock", "set_at": None}
    mode = (await modes.resolve_modes(conn))[key]
    set_at = await q.fetch(_SET_AT_SQL, vendor=key)
    return {
        "vendor": key,
        "mode": mode,
        "set_at": set_at[0][0].isoformat() if set_at else None,
    }
