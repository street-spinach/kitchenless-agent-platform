"""The message catalog (experience §6, EX-I1) and the approval reply-by time (EX-I2)."""

import string
import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from services.agents.consumer import graph as cg
from services.agents.household import graph as hg
from services.api.whatsapp import sender as s
from services.mcp.household.server import format_reply_by

PHONE = "+15550100301"
# WhatsApp limits (experience §5 step 2).
MAX_TITLE_CHARS = 20
MAX_BODY_CHARS = 1024

# Every §6 key that lands in X2. `unknown_number`, `consent_renewal` and `consent_revoked`
# come with identity-authz step 9 and X15; `nothing_needed` keeps pantry P5's wording.
CATALOG_KEYS = {
    "retry_prompt",
    "media_not_supported",
    "already_decided",
    "approval_request",
    "approval_expired",
    "approval_not_approver",
    "approval_cancelled",
    "chat_reply",
    "order_summary",
    "no_offers",
    "off_topic_reply",
    "dislike_saved",
    "dislike_removed",
    "dislike_not_removed",
    "dislike_not_saved",
    "take_turns_offer",
    "take_turns_offer_off",
    "take_turns_saved",
    "take_turns_not_approver",
    "shortfall_prompt",
    "shortfall_result",
    "setup_ask",
    "setup_restate",
    "setup_remove_confirm",
    "setup_saved",
    "setup_complete",
    "setup_discarded",
    "setup_not_admin",
    "setup_expired",
    "setup_not_understood",
    "setup_phone_taken",
    "join_link",
    "joined",
    "me_link",
    "held_cancelled_line",
    "reminder_delivery_today",
    "reminder_pantry_summary",
    "reminders_off",
    "reminders_on",
}

# The §6 button titles, and the code constants that send them today.
BUTTON_TITLES = [
    "Approve",
    "Reject",
    hg.TAKE_TURNS_TITLE,
    hg.STOP_TAKING_TURNS_TITLE,
    cg.SKIP_TITLE,
    "Yes, save",
    "No",
    "Yes, remove",
    "Keep",
    "Renew",
    "Stop these",
    "Open pantry",
]

REFUSALS = {
    "approval_not_approver": "Only Asha can approve this.",
    "already_decided": "That's already been decided.",
    "approval_expired": "This approval has expired, so the order was cancelled.",
    "take_turns_not_approver": "Only Asha can change take turns.",
    "setup_not_admin": "Only Asha can change this.",
    "setup_expired": "That expired. Send it again and I'll restate it.",
    "setup_not_understood": (
        "I didn't catch that. Try one person at a time, for example: Kavi is allergic to peanuts."
    ),
}


def _fields(key: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(s.TEXTS[key]) if name}


def _body(key: str, params: dict[str, str], buttons: list[dict[str, str]] | None = None) -> dict:
    return {"user_id": str(uuid.uuid4()), "template": key, "params": params, "buttons": buttons}


def test_every_key_renders() -> None:
    assert CATALOG_KEYS <= set(s.TEXTS)
    for key in s.TEXTS:
        params = {name: "x" for name in _fields(key)}
        payload = s.graph_payload(PHONE, _body(key, params))
        assert payload is not None, key
        assert len(payload["text"]["body"]) <= MAX_BODY_CHARS, key
        # A missing param is never sent (fails closed).
        if params:
            assert s.graph_payload(PHONE, _body(key, {})) is None, key


def test_new_wording_with_params() -> None:
    cases = {
        "approval_request": (
            {"amount": "$76.00", "reply_by": "Sat 6:30 pm"},
            "Your agent wants to spend $76.00 and needs your approval. Reply by Sat 6:30 pm.",
        ),
        "setup_restate": (
            {"lines": "Kavi: peanut allergy", "unmatched": ""},
            "I'll save: Kavi: peanut allergy. Nothing else is saved.",
        ),
        "join_link": (
            {"name": "Sam", "phone": "+15550100302", "link": "https://x/j"},
            "Send this to Sam. It works only from +15550100302: https://x/j",
        ),
        "held_cancelled_line": (
            {"amount": "$76.00"},
            "While you were away, the order for $76.00 was cancelled because it wasn't "
            "approved in time.",
        ),
        "reminders_off": ({}, "Reminders are off. Write *reminders on* to turn them back on."),
    }
    for key, (params, text) in cases.items():
        payload = s.graph_payload(PHONE, _body(key, params))
        assert payload is not None and payload["text"]["body"] == text, key


def test_button_limits() -> None:
    for title in BUTTON_TITLES:
        assert 0 < len(title) <= MAX_TITLE_CHARS, title
    three = [{"payload": f"p{i}", "title": t} for i, t in enumerate(BUTTON_TITLES[:3])]
    assert s.graph_payload(PHONE, _body("setup_saved", {}, three)) is not None
    four = [*three, {"payload": "p4", "title": "Keep"}]
    assert s.graph_payload(PHONE, _body("setup_saved", {}, four)) is None
    assert s.MAX_BUTTONS == 3


def test_every_refusal_has_text() -> None:
    for key, text in REFUSALS.items():
        params = {name: "Asha" for name in _fields(key)}
        payload = s.graph_payload(PHONE, _body(key, params))
        assert payload is not None and payload["text"]["body"] == text, key


def test_shortfall_keys_present() -> None:
    for key in ("shortfall_prompt", "shortfall_result"):
        assert s.TEXTS[key] == "{text}"
        payload = s.graph_payload(PHONE, _body(key, {"text": "Milk is out."}))
        assert payload is not None and payload["text"]["body"] == "Milk is out."


NY = ZoneInfo("America/New_York")


@pytest.mark.parametrize(
    ("due", "now", "text"),
    [
        # Saturday 2026-11-07 18:30 in New York (UTC-5).
        (
            datetime(2026, 11, 7, 23, 30, tzinfo=UTC),
            datetime(2026, 11, 7, 15, tzinfo=UTC),
            "6:30 pm",
        ),
        (
            datetime(2026, 11, 7, 23, 30, tzinfo=UTC),
            datetime(2026, 11, 6, 15, tzinfo=UTC),
            "Sat 6:30 pm",
        ),
        # Midnight and noon, and a UTC day that is still yesterday in New York.
        (
            datetime(2026, 11, 7, 5, 0, tzinfo=UTC),
            datetime(2026, 11, 7, 4, tzinfo=UTC),
            "Sat 12:00 am",
        ),
        (
            datetime(2026, 11, 7, 17, 5, tzinfo=UTC),
            datetime(2026, 11, 7, 16, tzinfo=UTC),
            "12:05 pm",
        ),
    ],
)
def test_reply_by_in_household_time(due: datetime, now: datetime, text: str) -> None:
    assert format_reply_by(due, now, NY) == text
