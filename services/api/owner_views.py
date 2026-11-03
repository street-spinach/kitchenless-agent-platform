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
"""

from typing import Any

from psycopg import AsyncConnection

from services.agents.common.model import check_rationale
from services.common import ids
from services.safety import scope

__all__ = [
    "LIVE_QUOTE_STATUSES",
    "MAX_ROWS",
    "NOTICE_TEXT",
    "notices",
    "orders",
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
