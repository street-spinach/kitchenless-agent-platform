"""One valid body per message type, for the protocol tests."""

from datetime import UTC, datetime

import pytest

ORDER = "11111111-aaaa-4aaa-8aaa-000000000001"
QUOTE = "22222222-bbbb-4bbb-8bbb-000000000002"
SHORTFALL = "33333333-cccc-4ccc-8ccc-000000000003"
REF = "44444444-dddd-4ddd-8ddd-000000000004"
OTHER = "55555555-eeee-4eee-8eee-000000000005"

T0 = datetime(2026, 10, 31, 17, 0, tzinfo=UTC).isoformat()
T1 = datetime(2026, 10, 31, 18, 30, tzinfo=UTC).isoformat()
WINDOW = {"start": T0, "end": T1}

QUOTE_LINES = [
    {
        "line": 1,
        "requested_item": "oat milk",
        "offered_sku": "SKU-1",
        "offered_name": "Oat milk 1L",
        "qty": 2,
        "unit_minor": 399,
        "substituted_for": None,
    },
    {
        "line": 2,
        "requested_item": "rye bread",
        "offered_sku": "SKU-2",
        "offered_name": "Wheat bread",
        "qty": 1,
        "unit_minor": 450,
        "substituted_for": "rye bread",
    },
    {
        "line": 3,
        "requested_item": "basil",
        "offered_sku": None,
        "offered_name": None,
        "qty": 1,
        "unit_minor": 0,
        "substituted_for": None,
    },
]

SHORTFALL_LINES = [
    {
        "line": 1,
        "sku": "SKU-1",
        "name": "Oat milk 1L",
        "qty_missing": 1,
        "line_total_minor": 798,
        "substitutes": [{"sku": "SKU-9", "name": "Soy milk 1L", "qty": 1, "unit_minor": 350}],
    }
]

SAMPLES: dict[str, dict] = {
    "user_message": {"inbound_id": OTHER},
    "start_weekly_order": {"kind": "grocery"},
    "start_dinner": {"kind": "dinner"},
    "constraints_request": {"order_id": ORDER, "user_id": OTHER},
    "constraints": {
        "order_id": ORDER,
        "allergens": ["tree_nuts", "milk"],
        "diets": ["vegetarian"],
        "dislikes": ["olives"],
    },
    "quote_request": {
        "order_id": ORDER,
        "kind": "grocery",
        "items": [{"item": "oat milk", "qty": 2, "unit": "l"}],
        "constraints": {"allergens": ["tree_nuts"], "diets": []},
        "currency": "USD",
        "zone_id": "us-oh-45202",
        "window": WINDOW,
        "reply_by": T0,
    },
    "offer": {
        "order_id": ORDER,
        "quote_id": QUOTE,
        "round": 0,
        "lines": QUOTE_LINES,
        "total_minor": 1248,
        "currency": "USD",
        "expires_at": T1,
        "final": False,
        "rationale": "SECRET-RATIONALE cheapest in stock",
    },
    "counter_offer": {
        "order_id": ORDER,
        "quote_id": QUOTE,
        "round": 1,
        "lines": QUOTE_LINES,
        "total_minor": 1100,
        "currency": "USD",
    },
    "accept": {"order_id": ORDER, "quote_id": QUOTE, "items_hash": "abc", "total_minor": 1248},
    "reject": {"order_id": ORDER, "quote_id": QUOTE, "reason": "price"},
    "cancel": {"order_id": ORDER, "quote_id": QUOTE, "reason": "buyer_cancelled"},
    "order_confirm": {
        "order_id": ORDER,
        "state": "placed",
        "lines": [{"line": 1, "sku": "SKU-1", "name": "Oat milk 1L", "qty": 2, "unit_minor": 399}],
        "total_minor": 798,
        "currency": "USD",
    },
    "delivery_quote_request": {
        "order_id": ORDER,
        "zone_id": "us-oh-45202",
        "window": WINDOW,
        "pickup_agent_id": "grocery:mock-us",
    },
    "delivery_quote": {
        "order_id": ORDER,
        "quote_id": QUOTE,
        "window": WINDOW,
        "price_minor": 599,
        "currency": "USD",
        "expires_at": T1,
        "rationale": "SECRET-RATIONALE fastest",
    },
    "delivery_request": {
        "order_id": ORDER,
        "quote_id": QUOTE,
        "window": WINDOW,
        "zone_id": "us-oh-45202",
        "address_ref": OTHER,
    },
    "delivery_booked": {
        "order_id": ORDER,
        "state": "courier_booked",
        "delivery_agent_id": "delivery:mock-us",
        "window": WINDOW,
    },
    "order_status": {"order_id": ORDER, "state": "in_delivery"},
    "approval_decision": {"approval_id": OTHER, "order_id": ORDER, "status": "approved"},
    "quote_round_closed": {"order_id": ORDER, "round": "quote"},
    "whatsapp_out": {
        "user_id": OTHER,
        "template": "shortfall",
        "params": {"text": "hello"},
        "buttons": [{"payload": f"sf:{SHORTFALL}:1:drop", "title": "Skip it"}],
    },
    "protocol_error": {"ref_message_id": REF, "code": "wrong_party", "detail": "not your quote"},
    "shortfall_detected": {"order_id": ORDER, "skus": ["SKU-1"], "event_id": OTHER},
    "shortfall_notice": {
        "order_id": ORDER,
        "shortfall_id": SHORTFALL,
        "lines": SHORTFALL_LINES,
        "reply_by": T1,
    },
    "shortfall_relay": {
        "order_id": ORDER,
        "shortfall_id": SHORTFALL,
        "lines": SHORTFALL_LINES,
        "reply_by": T1,
    },
    "shortfall_closed": {"shortfall_id": SHORTFALL, "order_id": ORDER, "reason": "timeout"},
    "shortfall_decision": {
        "order_id": ORDER,
        "shortfall_id": SHORTFALL,
        "picks": {1: "SKU-9", 2: "drop"},
    },
}


@pytest.fixture
def samples() -> dict[str, dict]:
    return {type_: dict(body) for type_, body in SAMPLES.items()}
