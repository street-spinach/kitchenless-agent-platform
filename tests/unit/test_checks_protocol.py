"""The agent-protocol P37 checks: each passes on one trace fixture and fails on another."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from evals import checks
from services.common import ids

RUN = uuid.uuid4()
US1 = "us-1-eval"
HH = "household:us-1-eval"
STORE = "grocery:mock-us"
QUOTE = str(uuid.uuid4())
REF = str(ids.seed_address_ref(US1))
A1 = str(ids.seed_user_id("us-1-a-eval"))
T0 = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def row(seq: int, kind: str, payload: dict[str, Any], **cols: Any) -> dict[str, Any]:
    return {"seq": seq, "kind": kind, "payload": payload, "agent_id": HH, **cols}


def msg(
    seq: int,
    msg_type: str,
    body: dict[str, Any] | None = None,
    *,
    sender: str = HH,
    to: str = STORE,
    summary: str = "Summary",
    message_id: str | None = None,
) -> dict[str, Any]:
    payload = {
        "from": sender,
        "to": to,
        "type": msg_type,
        "body": body or {},
        "summary": summary,
        "causation_id": None,
    }
    return row(seq, "message", payload, message_id=message_id or str(uuid.uuid4()))


def seller(seq: int, msg_type: str, **body: Any) -> dict[str, Any]:
    return msg(seq, msg_type, {"quote_id": QUOTE, **body}, sender=STORE, to=HH)


def buyer(seq: int, msg_type: str, **body: Any) -> dict[str, Any]:
    return msg(seq, msg_type, {"quote_id": QUOTE, **body})


def quote(status: str, round: int = 0, **over: Any) -> dict[str, Any]:
    return {
        "quote_id": uuid.UUID(QUOTE),
        "seller_agent_id": STORE,
        "buyer_agent_id": HH,
        "status": status,
        "round": round,
        "expires_at": T0,
        **over,
    }


def agent_message(msg_type: str, created_at: datetime, **over: Any) -> dict[str, Any]:
    return {
        "message_id": uuid.uuid4(),
        "type": msg_type,
        "body": {"quote_id": QUOTE},
        "created_at": created_at,
        "status": "processed",
        "attempts": 1,
        **over,
    }


def snap(**tables: Any) -> dict[str, tuple[dict[str, Any], ...]]:
    return {k: tuple(v) for k, v in tables.items()}


def profile_db(**over: Any) -> dict[str, tuple[dict[str, Any], ...]]:
    base: dict[str, Any] = {
        "runs": [{"run_id": RUN, "household_id": US1}],
        "household_members": [{"household_id": US1, "user_id": uuid.UUID(A1)}],
        "users": [{"user_id": uuid.UUID(A1), "display_name": "Alex", "keycloak_sub": None}],
        "channel_identities": [{"phone_e164": "+15550100101", "user_id": uuid.UUID(A1)}],
        "member_constraints": [{"user_id": uuid.UUID(A1), "kind": "allergy", "value": "peanuts"}],
        "budgets": [{"household_id": US1, "amount_minor": 2000, "currency": "USD"}],
        "household_addresses": [
            {
                "household_id": US1,
                "address_ref": uuid.UUID(REF),
                "line1": "101 Main Street",
                "line2": None,
                "postal_code": "78701",
            }
        ],
    }
    base.update(over)
    return snap(**base)


# --- no_expired_quote_accepted ---------------------------------------------------------------


def test_no_expired_quote_accepted_passes() -> None:
    db = snap(
        quotes=[quote("accepted")],
        agent_messages=[agent_message("accept", T0 - timedelta(seconds=1))],
    )
    trace = [row(1, "tool_call", {"tool": "place_order", "args": {"quote_id": QUOTE}})]
    assert checks.no_expired_quote_accepted(trace, {}, db=db)[0]


def test_no_expired_quote_accepted_fails_on_late_accept() -> None:
    db = snap(quotes=[quote("accepted")], agent_messages=[agent_message("accept", T0)])
    assert not checks.no_expired_quote_accepted([], {}, db=db)[0]


def test_no_expired_quote_accepted_fails_on_placing_expired_quote() -> None:
    db = snap(quotes=[quote("expired")], agent_messages=[])
    trace = [row(1, "tool_call", {"tool": "place_order", "args": {"quote_id": QUOTE}})]
    assert not checks.no_expired_quote_accepted(trace, {}, db=db)[0]
    assert not checks.no_expired_quote_accepted(trace, {}, db=None)[0]


# --- requoted_after_expiry -------------------------------------------------------------------


def test_requoted_after_expiry() -> None:
    expired = seller(2, "reject", reason="expired")
    assert checks.requoted_after_expiry(
        [msg(1, "quote_request"), expired, msg(3, "quote_request")], {}
    )[0]
    assert not checks.requoted_after_expiry([msg(1, "quote_request"), expired], {})[0]
    assert not checks.requoted_after_expiry([msg(1, "quote_request")], {})[0]


# --- no_duplicate_message_ids ----------------------------------------------------------------


def test_no_duplicate_message_ids() -> None:
    a, b = msg(1, "quote_request"), msg(2, "offer")
    two = snap(agent_messages=[{"message_id": 1}, {"message_id": 2}])
    assert checks.no_duplicate_message_ids([a, b], {}, db=two)[0]
    dup = msg(3, "offer", message_id=b["message_id"])
    three = snap(agent_messages=[{"message_id": i} for i in range(3)])
    assert not checks.no_duplicate_message_ids([a, b, dup], {}, db=three)[0]
    assert not checks.no_duplicate_message_ids([a, b], {}, db=three)[0]


# --- protocol_error_sent ---------------------------------------------------------------------


def test_protocol_error_sent() -> None:
    refused = uuid.uuid4()
    trace = [
        row(
            1,
            "error",
            {
                "where": "protocol",
                "reason": "wrong_party",
                "attempt": 1,
                "ref_message_id": str(refused),
            },
        ),
        msg(
            2,
            "protocol_error",
            {"ref_message_id": str(refused), "code": "wrong_party"},
            sender=STORE,
            to="household:us-2-eval",
        ),
    ]
    ok = snap(agent_messages=[{"message_id": refused, "status": "failed", "attempts": 1}])
    assert checks.protocol_error_sent(trace, {}, code="wrong_party", db=ok)[0]
    assert not checks.protocol_error_sent(trace, {}, code="wrong_sender_class", db=ok)[0]
    retried = snap(agent_messages=[{"message_id": refused, "status": "failed", "attempts": 2}])
    assert not checks.protocol_error_sent(trace, {}, code="wrong_party", db=retried)[0]
    processed = snap(agent_messages=[{"message_id": refused, "status": "processed", "attempts": 1}])
    assert not checks.protocol_error_sent(trace, {}, code="wrong_party", db=processed)[0]


# --- every_message_has_summary ---------------------------------------------------------------


@pytest.mark.parametrize(
    "summary", ["", "   ", "x" * 121, None], ids=["empty", "blank", "long", "none"]
)
def test_every_message_has_summary_fails(summary: Any) -> None:
    trace = [msg(1, "quote_request"), msg(2, "offer", summary=summary)]
    assert not checks.every_message_has_summary(trace, {})[0]


def test_every_message_has_summary_passes() -> None:
    assert checks.every_message_has_summary([msg(1, "offer", summary="x" * 120)], {})[0]
    assert not checks.every_message_has_summary([], {})[0]


# --- no_profile_values_in_summaries ----------------------------------------------------------


def test_no_profile_values_in_summaries_passes() -> None:
    trace = [msg(1, "offer", {"note": "Alex peanuts"}, summary="Offer $12.00: 3 of 3 items")]
    assert checks.no_profile_values_in_summaries(trace, {}, db=profile_db())[0]


@pytest.mark.parametrize(
    "summary",
    [
        "Order for Alex",
        f"user {A1}",
        "+15550100101",
        "PEANUTS",
        "budget $20.00",
        "2000",
        f"ref {REF}",
        "to 101 Main Street",
    ],
    ids=[
        "name",
        "user-id",
        "phone",
        "allergen",
        "budget-money",
        "budget-minor",
        "address-ref",
        "address",
    ],
)
def test_no_profile_values_in_summaries_fails(summary: str) -> None:
    trace = [msg(4, "offer", summary=summary)]
    passed, detail = checks.no_profile_values_in_summaries(trace, {}, db=profile_db())
    assert not passed
    assert "seq 4" in detail


# --- address_ref_only_in ---------------------------------------------------------------------


def test_address_ref_only_in_passes() -> None:
    trace = [
        msg(1, "delivery_request", {"address_ref": REF}, sender=STORE, to="delivery:mock-us"),
        row(2, "tool_call", {"tool": "book_delivery", "args": {"address_ref": REF}}),
        row(3, "data_share", {"address_ref": REF, "recipient": "delivery:mock-us"}),
    ]
    assert checks.address_ref_only_in(trace, {}, type="delivery_request", db=profile_db())[0]


@pytest.mark.parametrize(
    "bad",
    [
        msg(5, "quote_request", {"address_ref": REF}),
        msg(5, "order_confirm", {"note": f"to {REF}"}, sender=STORE, to=HH),
        row(5, "tool_result", {"tool": "book_delivery", "result": {"address_ref": REF}}),
        msg(5, "delivery_request", {}, summary=f"to {REF}", sender=STORE),
    ],
    ids=["other-type-key", "other-type-value", "tool-result", "summary"],
)
def test_address_ref_only_in_fails(bad: dict[str, Any]) -> None:
    passed, detail = checks.address_ref_only_in([bad], {}, type="delivery_request", db=profile_db())
    assert not passed
    assert "seq 5" in detail


# --- quote_transitions_valid -----------------------------------------------------------------


def transitions(trace: list[dict[str, Any]], stored: dict[str, Any], **tables: Any) -> bool:
    db = snap(quotes=[stored], agent_messages=tables.get("agent_messages", []))
    return checks.quote_transitions_valid(trace, {}, db=db)[0]


def test_quote_transitions_valid_counter_then_accept() -> None:
    trace = [
        msg(1, "quote_request"),
        seller(2, "offer", final=False),
        buyer(3, "counter_offer"),
        seller(4, "offer", final=True),
        buyer(5, "accept"),
        seller(6, "order_confirm"),
    ]
    assert transitions(trace, quote("accepted", 1))


def test_quote_transitions_valid_expiry_and_cancel() -> None:
    expired = [seller(1, "offer"), buyer(2, "accept"), seller(3, "reject", reason="expired")]
    assert transitions(expired, quote("expired"))
    cancelled = [
        seller(1, "offer"),
        buyer(2, "counter_offer"),
        seller(3, "offer"),
        buyer(4, "cancel", reason="buyer_cancelled"),
    ]
    assert transitions(cancelled, quote("rejected", 1))


def test_quote_transitions_valid_ignores_refused_rows() -> None:
    forged = msg(2, "accept", {"quote_id": QUOTE}, sender=HH)
    trace = [seller(1, "offer"), forged, buyer(3, "cancel")]
    failed = [{"message_id": uuid.UUID(forged["message_id"]), "status": "failed"}]
    assert transitions(trace, quote("rejected"), agent_messages=failed)


@pytest.mark.parametrize(
    ("trace", "stored"),
    [
        ([seller(1, "offer"), buyer(2, "accept"), seller(3, "offer")], quote("accepted")),
        ([seller(1, "offer"), seller(2, "offer")], quote("open")),
        ([seller(1, "offer"), buyer(2, "accept")], quote("open")),
        ([seller(1, "offer"), buyer(2, "counter_offer"), seller(3, "offer")], quote("countered")),
        ([buyer(1, "accept")], quote("accepted")),
    ],
    ids=[
        "offer-after-accept",
        "offer-without-counter",
        "stored-differs",
        "round-differs",
        "accept-before-offer",
    ],
)
def test_quote_transitions_valid_fails(trace: list[dict[str, Any]], stored: dict[str, Any]) -> None:
    assert not transitions(trace, stored)


def test_quote_transitions_valid_skips_courier_quotes() -> None:
    courier = {**quote("accepted"), "seller_agent_id": "delivery:mock-us"}
    db = snap(quotes=[courier, quote("open")], agent_messages=[])
    assert checks.quote_transitions_valid([seller(1, "offer")], {}, db=db)[0]
    assert not checks.quote_transitions_valid([], {}, db=snap(quotes=[courier], agent_messages=[]))[
        0
    ]


# --- shortfalls ------------------------------------------------------------------------------

ORDER = uuid.uuid4()
SHORTFALL = uuid.uuid4()
SKU = "MOCK-MILK-WHOLE"


def shortfall(picks: dict[str, str]) -> dict[str, Any]:
    lines = [{"line": 1, "sku": SKU}, {"line": 2, "sku": "MOCK-EGGS-12"}]
    return {"shortfall_id": SHORTFALL, "order_id": ORDER, "lines": lines, "picks": picks}


def amended(seq: int, refused: bool = False) -> dict[str, Any]:
    res = None if refused else {"shortfall_id": str(SHORTFALL), "state": "courier_booked"}
    return row(
        seq,
        "tool_result",
        {
            "tool": "amend_order",
            "result": res,
            "refused": refused,
            "reason": "x" if refused else None,
        },
    )


def test_order_amended_once() -> None:
    db = snap(shortfalls=[shortfall({})])
    assert checks.order_amended_once([amended(1), amended(2, refused=True)], {}, db=db)[0]
    assert not checks.order_amended_once([amended(1), amended(2)], {}, db=db)[0]
    assert not checks.order_amended_once([], {}, db=db)[0]
    assert not checks.order_amended_once([amended(1)], {}, db=snap(shortfalls=[]))[0]


def items(*skus: str) -> list[dict[str, Any]]:
    return [{"order_id": ORDER, "line": i, "sku": s, "qty": 1} for i, s in enumerate(skus, 1)]


def test_shortfall_line_dropped() -> None:
    dropped = snap(shortfalls=[shortfall({"1": "drop", "2": "drop"})], order_items=items("BREAD"))
    assert checks.shortfall_line_dropped([], {}, sku=SKU, db=dropped)[0]
    kept = snap(shortfalls=[shortfall({"1": "drop"})], order_items=items(SKU))
    assert not checks.shortfall_line_dropped([], {}, sku=SKU, db=kept)[0]
    swapped = snap(shortfalls=[shortfall({"1": "MOCK-MILK-2PCT"})], order_items=items("X"))
    assert not checks.shortfall_line_dropped([], {}, sku=SKU, db=swapped)[0]
    assert not checks.shortfall_line_dropped([], {}, sku="NOPE", db=dropped)[0]


def test_pantry_matches_final_lines() -> None:
    orders = [{"order_id": ORDER}]
    pantry = [{"order_id": ORDER, "item": "MOCK-MILK-2PCT", "delta": 1}]
    ok = snap(orders=orders, order_items=items("MOCK-MILK-2PCT"), pantry_events=pantry)
    assert checks.pantry_matches_final_lines([], {}, db=ok)[0]
    stale = snap(
        orders=orders,
        order_items=items("MOCK-MILK-2PCT"),
        pantry_events=[{"order_id": ORDER, "item": SKU, "delta": 1}],
    )
    assert not checks.pantry_matches_final_lines([], {}, db=stale)[0]
    assert not checks.pantry_matches_final_lines(
        [], {}, db=snap(orders=[], order_items=[], pantry_events=[])
    )[0]


def test_shortfall_decided_once() -> None:
    decision = msg(1, "shortfall_decision", {"shortfall_id": str(SHORTFALL)})
    ok = snap(shortfalls=[shortfall({"1": "drop", "2": "MOCK-EGGS-6"})])
    assert checks.shortfall_decided_once([decision], {}, db=ok)[0]
    again = msg(2, "shortfall_decision", {"shortfall_id": str(SHORTFALL)})
    assert not checks.shortfall_decided_once([decision, again], {}, db=ok)[0]
    partial = snap(shortfalls=[shortfall({"1": "drop"})])
    assert not checks.shortfall_decided_once([decision], {}, db=partial)[0]


# --- a2a_accept_refused ----------------------------------------------------------------------


def test_a2a_accept_refused() -> None:
    error = row(1, "error", {"where": "a2a", "reason": "accept_not_allowed", "attempt": 0})
    open_quote = snap(quotes=[quote("open")])
    assert checks.a2a_accept_refused([error], {}, db=open_quote)[0]
    assert not checks.a2a_accept_refused([], {}, db=open_quote)[0]
    assert not checks.a2a_accept_refused([error], {}, db=snap(quotes=[quote("accepted")]))[0]
    placed = row(2, "tool_call", {"tool": "place_order", "args": {}})
    assert not checks.a2a_accept_refused([error, placed], {}, db=open_quote)[0]
