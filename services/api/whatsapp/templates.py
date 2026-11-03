"""Meta templates for sends outside the WhatsApp window (experience §5).

The map: each text key that may be sent as a Meta template, its submitted Meta name, its
body exactly as submitted (`{{n}}` for parameters), and the setting that holds its approved
name (`WA_TPL_<KEY>`; empty means not approved). `params` builds a template's parameters by
code (EX-I13): amounts, times in the household's time zone, a store's `organizations.name`,
counts and fixed phrases. None is an allergy or diet code or a member's name (XG10).
`request` builds the Graph `template` object.

Also here: the holdable kinds (§5 "Holdable kinds"), which text keys each `ref.kind` may
hold, and the model-written keys, which are never held and never sent as a template.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from services.common.config import Settings
from services.common.money import Money
from services.common.money import format as money_format
from services.mcp.household.server import format_reply_by

__all__ = [
    "HOLDABLE",
    "LANGUAGE",
    "MODEL_KEYS",
    "REMINDER_KEYS",
    "REVOKED_AGENTS",
    "TEMPLATES",
    "MetaTemplate",
    "holdable",
    "meta_name",
    "params",
    "request",
]

# Every template is submitted in this language (runbook).
LANGUAGE = "en_US"


@dataclass(frozen=True)
class MetaTemplate:
    """One row of the §5 map."""

    key: str  # the text key in `sender.TEXTS`
    name: str  # the name submitted to Meta (runbook)
    setting: str  # the `Settings` field holding the approved name
    body: str  # the body submitted to Meta, `{{n}}` for parameters (§5)


TEMPLATES: dict[str, MetaTemplate] = {
    t.key: t
    for t in (
        MetaTemplate(
            "approval_request",
            "approval_needed_v1",
            "wa_tpl_approval_request",
            "Your agent wants to spend {{1}} and needs your approval. Reply by {{2}}.",
        ),
        MetaTemplate(
            "take_turns_offer",
            "take_turns_offer_v1",
            "wa_tpl_take_turns_offer",
            "Tonight's dinner had something one person doesn't like. "
            "Want me to take turns on whose dislikes come first?",
        ),
        MetaTemplate(
            "order_summary",
            "order_delivered_v1",
            "wa_tpl_order_summary",
            "Your order from {{1}} was delivered: {{2}} items, {{3}}.",
        ),
        MetaTemplate(
            "consent_renewal",
            "consent_renewal_v1",
            "wa_tpl_consent_renewal",
            "Your agents' access has ended. Tap Renew and I'll send you a sign-in link.",
        ),
        MetaTemplate(
            "consent_revoked",
            "consent_revoked_v1",
            "wa_tpl_consent_revoked",
            "{{1}} stopped. Orders not yet placed won't go ahead. "
            "Tap Renew and I'll send you a sign-in link.",
        ),
        MetaTemplate(
            "reminder_delivery_today",
            "delivery_today_v1",
            "wa_tpl_reminder_delivery_today",
            "Groceries from {{1}} arrive today, {{2}}. Nothing needed from you.",
        ),
        MetaTemplate(
            "reminder_pantry_summary",
            "pantry_summary_v1",
            "wa_tpl_reminder_pantry_summary",
            "Pantry check: {{1}} items at home by our records, {{2}} we're not sure about.",
        ),
    )
}

# `ref.kind` → the text keys it may hold (§5). Checked together, so a key sent with the
# wrong kind is not held (fails closed).
HOLDABLE: dict[str, frozenset[str]] = {
    "approval": frozenset({"approval_request"}),
    "order": frozenset(
        {"order_summary", "nothing_needed", "take_turns_offer", "take_turns_offer_off"}
    ),
    "grant": frozenset({"consent_renewal", "consent_revoked"}),
}

# Model-written text (§5 step 6): never held, never a Meta template.
MODEL_KEYS: frozenset[str] = frozenset({"chat_reply", "shortfall_prompt", "shortfall_result"})

# Reminders skipped outside the window get reason `no_template` (§5 step 6).
REMINDER_KEYS: frozenset[str] = frozenset({"reminder_delivery_today", "reminder_pantry_summary"})


def meta_name(key: str, settings: Settings) -> str | None:
    """The approved Meta template name for `key`, or `None` if it has none (not in the map,
    model-written, or its setting is empty)."""
    template = TEMPLATES.get(key)
    if template is None or key in MODEL_KEYS:
        return None
    return getattr(settings, template.setting) or None


def holdable(key: str, ref_kind: str | None) -> bool:
    """Whether a message with text key `key` and `ref.kind` may be held."""
    if ref_kind is None or key in MODEL_KEYS:
        return False
    return key in HOLDABLE.get(ref_kind, frozenset())


# --- parameters (EX-I13) ----------------------------------------------------------------

# The two subjects `consent_revoked` may name (§5): fixed phrases, never a member's name.
REVOKED_AGENTS: frozenset[str] = frozenset({"Your agent", "The household agent"})

# A pending approval, its pending `approval_expiry` timer not yet due, and the household's
# time zone. A decided or expired approval gets no template.
_APPROVAL_SQL = """
SELECT a.amount_minor, a.currency, t.due_at, now() AS now, h.timezone
FROM approvals a
JOIN households h ON h.household_id = a.household_id
JOIN timers t ON t.kind = 'approval_expiry' AND t.status = 'pending'
             AND t.ref->>'approval_id' = a.approval_id::text AND t.due_at > now()
WHERE a.approval_id = %s AND a.status = 'pending'
ORDER BY t.due_at DESC LIMIT 1
"""

# A delivered order, its seller's organization and the count of its final lines (§5).
_ORDER_SQL = """
SELECT org.name AS store, o.total_minor, o.currency,
       (SELECT count(*) FROM order_items oi WHERE oi.order_id = o.order_id) AS items
FROM orders o
JOIN agent_cards c ON c.agent_id = o.seller_agent_id
JOIN organizations org ON c.owner_ref = 'org:' || org.org_id
WHERE o.order_id = %s AND o.state = 'delivered' AND o.total_minor IS NOT NULL
"""

# The reminder the message is for, addressed to this person (experience §10).
_REMINDER_SQL = """
SELECT r.kind, r.ref_id, r.household_id, h.timezone
FROM reminders r JOIN households h ON h.household_id = r.household_id
WHERE r.reminder_id = %s AND r.user_id = %s
"""

# A `delivery_today` reminder's order in the same household: its store and window.
_DELIVERY_SQL = """
SELECT org.name AS store, lower(o.delivery_window) AS starts, upper(o.delivery_window) AS ends
FROM orders o
JOIN agent_cards c ON c.agent_id = o.seller_agent_id
JOIN organizations org ON c.owner_ref = 'org:' || org.org_id
WHERE o.order_id = %s AND o.household_id = %s AND o.delivery_window IS NOT NULL
"""


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value is not None else None
    except ValueError:
        return None


def _ref(body: dict[str, Any], kind: str) -> Any:
    ref = body.get("ref") or {}
    return ref.get("id") if ref.get("kind") == kind else None


def _clock(at: datetime) -> str:
    hour = at.hour % 12 or 12
    return f"{hour}:{at.minute:02d} {'am' if at.hour < 12 else 'pm'}"


def _count(value: Any) -> str | None:
    return str(int(value)) if isinstance(value, str) and value.isdigit() else None


async def _one(conn: AsyncConnection, sql: str, args: Sequence[Any]) -> dict[str, Any] | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, args)
        return await cur.fetchone()


async def params(conn: AsyncConnection, key: str, body: dict[str, Any]) -> list[str] | None:
    """The parameters of `key`'s Meta template for this `whatsapp_out` body, in `{{n}}`
    order, or `None` if they cannot be built (no template for the key, a missing or wrong
    `ref`, a decided approval, an order not delivered, a missing count). `None` means no
    template is sent (fails closed)."""
    if key not in TEMPLATES or key in MODEL_KEYS:
        return None
    if key in ("take_turns_offer", "consent_renewal"):
        return []
    given = body.get("params") or {}
    if key == "consent_revoked":
        agent = given.get("agent")
        return [agent] if agent in REVOKED_AGENTS else None
    if key == "approval_request":
        approval_id = _uuid(_ref(body, "approval"))
        row = await _one(conn, _APPROVAL_SQL, (approval_id,)) if approval_id else None
        if row is None:
            return None
        amount = money_format(Money(int(row["amount_minor"]), row["currency"]), "en")
        return [amount, format_reply_by(row["due_at"], row["now"], ZoneInfo(row["timezone"]))]
    if key == "order_summary":
        order_id = _uuid(_ref(body, "order"))
        row = await _one(conn, _ORDER_SQL, (order_id,)) if order_id else None
        if row is None:
            return None
        total = money_format(Money(int(row["total_minor"]), row["currency"]), "en")
        return [row["store"], str(row["items"]), total]
    # The reminders: `ref = {reminder, <reminder_id>}` (EX-I41).
    reminder_id = str(_ref(body, "reminder") or "")
    if not reminder_id.isdigit():
        return None
    reminder = await _one(conn, _REMINDER_SQL, (int(reminder_id), body["user_id"]))
    if reminder is None:
        return None
    if key == "reminder_delivery_today":
        order_id = _uuid(reminder["ref_id"]) if reminder["kind"] == "delivery_today" else None
        row = (
            await _one(conn, _DELIVERY_SQL, (order_id, reminder["household_id"]))
            if order_id
            else None
        )
        if row is None or row["starts"] is None or row["ends"] is None:
            return None
        tz = ZoneInfo(reminder["timezone"])
        window = f"{_clock(row['starts'].astimezone(tz))}-{_clock(row['ends'].astimezone(tz))}"
        return [row["store"], window]
    # reminder_pantry_summary: the two counts the reminder pass puts in the body.
    at_home, unsure = _count(given.get("at_home_count")), _count(given.get("unsure_count"))
    if reminder["kind"] != "pantry_summary" or at_home is None or unsure is None:
        return None
    return [at_home, unsure]


def request(name: str, values: Sequence[str], buttons: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The Graph `template` object: the approved `name`, `en_US`, a body component with
    `values` as text parameters (left out when there are none), and one quick-reply button
    component per in-window button, with the same payload (§5)."""
    components: list[dict[str, Any]] = []
    if values:
        components.append(
            {"type": "body", "parameters": [{"type": "text", "text": v} for v in values]}
        )
    for index, button in enumerate(buttons):
        components.append(
            {
                "type": "button",
                "sub_type": "quick_reply",
                "index": str(index),
                "parameters": [{"type": "payload", "payload": button["payload"]}],
            }
        )
    return {"name": name, "language": {"code": LANGUAGE}, "components": components}
