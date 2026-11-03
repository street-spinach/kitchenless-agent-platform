"""The architecture item 66 checks: each passes on one fixture and fails on another."""

import uuid
from typing import Any

import pytest

from evals import checks, run
from services.common import ids

RUN = uuid.uuid4()
US1, US2 = "us-1-eval", "us-2-eval"
A1, B1, C1 = (str(ids.seed_user_id(m)) for m in ("us-1-a-eval", "us-1-b-eval", "us-1-c-eval"))
A2 = str(ids.seed_user_id("us-2-a-eval"))
REF1, REF2 = str(ids.seed_address_ref(US1)), str(ids.seed_address_ref(US2))
ORDER = uuid.uuid4()
QUOTE = uuid.uuid4()
STORE = "grocery:mock-us"


def row(seq: int, kind: str, payload: dict[str, Any], agent_id: str = "household:us-1-eval"):
    return {"seq": seq, "kind": kind, "payload": payload, "agent_id": agent_id}


def db(**over: Any) -> dict[str, tuple[dict[str, Any], ...]]:
    base: dict[str, Any] = {
        "runs": [{"run_id": RUN, "household_id": US1}],
        "household_members": [
            {"household_id": US1, "user_id": uuid.UUID(A1)},
            {"household_id": US1, "user_id": uuid.UUID(B1)},
            {"household_id": US1, "user_id": uuid.UUID(C1)},
            {"household_id": US2, "user_id": uuid.UUID(A2)},
        ],
        "users": [
            {"user_id": uuid.UUID(A1), "display_name": "Alex", "keycloak_sub": None},
            {"user_id": uuid.UUID(B1), "display_name": "Blair", "keycloak_sub": None},
            {"user_id": uuid.UUID(C1), "display_name": "Casey", "keycloak_sub": None},
            {"user_id": uuid.UUID(A2), "display_name": "Drew", "keycloak_sub": None},
        ],
        "channel_identities": [
            {"phone_e164": "+15550100101", "user_id": uuid.UUID(A1)},
            {"phone_e164": "+15550100104", "user_id": uuid.UUID(A2)},
        ],
        "member_constraints": [
            {"user_id": uuid.UUID(B1), "kind": "allergy", "value": "peanuts"},
            {"user_id": uuid.UUID(C1), "kind": "allergy", "value": "tree_nuts"},
        ],
        "budgets": [
            {"household_id": US1, "amount_minor": 15000, "currency": "USD", "spent_other_minor": 0},
            {"household_id": US2, "amount_minor": 12000, "currency": "USD", "spent_other_minor": 0},
        ],
        "household_addresses": [
            {
                "household_id": US1,
                "address_ref": uuid.UUID(REF1),
                "line1": "101 Main Street",
                "line2": None,
                "postal_code": "78701",
            },
            {
                "household_id": US2,
                "address_ref": uuid.UUID(REF2),
                "line1": "201 Main Street",
                "line2": None,
                "postal_code": "78701",
            },
        ],
        "orders": [{"order_id": ORDER, "seller_agent_id": STORE}],
        "order_items": [
            {"order_id": ORDER, "line": 1, "sku": "MOCK-MILK-WHOLE", "qty": 1},
            {"order_id": ORDER, "line": 2, "sku": "MOCK-EGGS-12", "qty": 2},
        ],
        "catalog_items": [
            {"agent_id": STORE, "sku": "MOCK-MILK-WHOLE", "allergens": ["milk"]},
            {"agent_id": STORE, "sku": "MOCK-EGGS-12", "allergens": ["eggs"]},
            {"agent_id": STORE, "sku": "MOCK-MILK-ALMOND", "allergens": ["tree_nuts"]},
        ],
        "menu_items": [],
        "pantry_events": [
            {"order_id": ORDER, "item": "MOCK-MILK-WHOLE", "delta": 1},
            {"order_id": ORDER, "item": "MOCK-EGGS-12", "delta": 2},
        ],
        "mock_orders": [{"external_order_id": "mo-1"}],
        "mock_deliveries": [{"external_delivery_id": "md-1"}],
        "quotes": [
            {
                "quote_id": QUOTE,
                "seller_agent_id": STORE,
                "status": "accepted",
                "round": 0,
                "total_minor": 1200,
                "items": {"lines": [_line(1, "MOCK-MILK-WHOLE", 1, 1200)]},
            },
        ],
        "business_rules": [
            {"agent_id": STORE, "kind": "floors", "value": {"MOCK-MILK-WHOLE": 1000}},
        ],
    }
    base.update(over)
    return {k: tuple(v) for k, v in base.items()}


def _line(line: int, sku: str, qty: int, unit: int) -> dict[str, Any]:
    return {"line": line, "offered_sku": sku, "qty": qty, "unit_minor": unit}


def outcome(seq: int = 99, status: str = "completed", reason: str | None = None):
    return row(seq, "outcome", {"status": status, "reason": reason, "summary": ""})


def result(seq: int, tool: str, refused: bool = False, **res: Any) -> dict[str, Any]:
    payload = {"tool": tool, "result": res or None, "refused": refused}
    return row(seq, "tool_result", {**payload, "reason": "x" if refused else None})


def call(seq: int, tool: str, **args: Any) -> dict[str, Any]:
    return row(seq, "tool_call", {"tool": tool, "args": args, "idempotency_key": "k"})


def message(seq: int, msg_type: str, body: dict[str, Any], **payload: Any) -> dict[str, Any]:
    base = {"from": "household:us-1-eval", "to": STORE, "type": msg_type, "body": body}
    return row(seq, "message", {**base, "summary": "s", "causation_id": None, **payload})


# --- invariant declarations ------------------------------------------------------------------

NEW_CHECKS = [
    name
    for name in checks.CHECKS
    if name
    not in {
        "outcome_recorded",
        "no_profile_fields_sent_to_business",
        "no_jwt_in_traces_or_checkpoints",
        "guarded_calls_have_identity",
    }
]


@pytest.mark.parametrize("name", NEW_CHECKS)
def test_new_checks_declare_invariant(name: str) -> None:
    assert isinstance(getattr(checks.CHECKS[name], "invariant", None), bool)


def test_completion_checks_are_the_progress_checks() -> None:
    completion = {n for n in NEW_CHECKS if not checks.CHECKS[n].invariant}
    assert completion == {"run_status", "requoted_after_expiry"}


def test_model_judge_not_built() -> None:
    assert "model_judge" not in checks.CHECKS


def test_db_checks_get_the_snapshot_from_the_runner() -> None:
    takes_db = {n for n, c in checks.CHECKS.items() if run._takes_db(c)}
    assert {"order_placed_once", "no_cross_household_data", "quote_transitions_valid"} <= takes_db
    assert "single_outcome" not in takes_db


# --- single_outcome and run_status ------------------------------------------------------------


def test_single_outcome() -> None:
    assert checks.single_outcome([outcome()], {})[0]
    assert not checks.single_outcome([outcome(1), outcome(2)], {})[0]
    assert not checks.single_outcome([], {})[0]


def test_run_status_passes_on_status_and_reason() -> None:
    trace = [outcome(status="error", reason="no_offers")]
    assert checks.run_status(trace, {}, status="error", reason="no_offers")[0]
    assert checks.run_status([outcome()], {}, status="completed")[0]


def test_run_status_fails_on_wrong_status_or_reason() -> None:
    trace = [outcome(status="error", reason="model_unavailable")]
    assert not checks.run_status(trace, {}, status="completed")[0]
    assert not checks.run_status(trace, {}, status="error", reason="no_offers")[0]
    assert not checks.run_status([], {}, status="completed")[0]


# --- no_cross_household_data ------------------------------------------------------------------


def test_no_cross_household_data_passes_on_own_values() -> None:
    trace = [call(1, "get_constraints", user_id=A1), row(2, "data_share", {"address_ref": REF1})]
    assert checks.no_cross_household_data(trace, {}, other_household=US2, db=db())[0]


@pytest.mark.parametrize(
    "payload",
    [{"user_id": A2}, {"text": "call +15550100104"}, {"address_ref": REF2}, {"budget": 12000}],
    ids=["member-id", "phone", "address-ref", "budget"],
)
def test_no_cross_household_data_fails_on_other_values(payload: dict[str, Any]) -> None:
    passed, detail = checks.no_cross_household_data(
        [row(5, "decision", payload)], {}, other_household=US2, db=db()
    )
    assert not passed
    assert "seq 5" in detail


def _us1_codes_db() -> dict[str, tuple[dict[str, Any], ...]]:
    """A us-2 run; us-1's members have tree_nuts and peanuts allergies, us-2's none."""
    return db(runs=[{"run_id": RUN, "household_id": US2}])


def test_no_cross_household_data_ignores_catalog_item_tags() -> None:
    # us-2's run searches a store whose almond milk is tagged tree_nuts: a product fact.
    item = {"sku": "MOCK-MILK-ALMOND", "allergens": ["tree_nuts"], "diets": ["vegan"]}
    result = {"tool": "search_items", "result": {"items": [{"sku": "M"}, item]}}
    trace = [row(58, "tool_result", result, agent_id="grocery:mock-us")]
    db_ = _us1_codes_db()
    assert checks.no_cross_household_data(trace, {}, other_household=US1, db=db_)[0]


@pytest.mark.parametrize(
    "payload",
    [
        {"args": {"filters": {"allergen_codes": ["tree_nuts"]}}},
        {"body": {"allergens": ["tree_nuts"]}},
        {"text": "avoid tree_nuts"},
    ],
    ids=["search-filter", "constraints-message", "text"],
)
def test_no_cross_household_data_still_fails_on_other_constraint_codes(
    payload: dict[str, Any],
) -> None:
    passed, detail = checks.no_cross_household_data(
        [row(7, "message", payload)], {}, other_household=US1, db=_us1_codes_db()
    )
    assert not passed
    assert "seq 7" in detail


def test_no_cross_household_data_fails_closed_without_snapshot() -> None:
    assert not checks.no_cross_household_data([], {}, other_household=US2, db=None)[0]
    assert not checks.no_cross_household_data([], {}, other_household=US1, db=db())[0]


# --- address_only_in_data_share ---------------------------------------------------------------


def test_address_only_in_data_share_passes() -> None:
    trace = [row(1, "data_share", {"address_ref": REF1, "recipient": "delivery:mock-us"})]
    assert checks.address_only_in_data_share(trace, {}, db=db())[0]


def test_address_only_in_data_share_allows_zone_id_with_postal_code() -> None:
    trace = [
        message(1, "quote_request", {"zone_id": "us-tx-78701"}),
        row(2, "data_share", {"address_ref": REF1}),
    ]
    assert checks.address_only_in_data_share(trace, {}, db=db())[0]


@pytest.mark.parametrize(
    "extra",
    [
        [message(2, "delivery_request", {"drop_off": {"lat": 30.2}})],
        [message(2, "delivery_request", {"note": "101 Main Street"})],
        [row(3, "data_share", {"address_ref": REF1})],
        "no-share",
    ],
    ids=["lat-key", "address-string", "two-shares", "no-share"],
)
def test_address_only_in_data_share_fails(extra: Any) -> None:
    share = [row(1, "data_share", {"address_ref": REF1})]
    trace = [] if extra == "no-share" else share + extra
    assert not checks.address_only_in_data_share(trace, {}, db=db())[0]


# --- order_placed_once, courier_booked_once, pantry_updated_once ------------------------------


def test_order_placed_once() -> None:
    trace = [result(1, "place_order", total_minor=1200), result(2, "place_order", refused=True)]
    assert checks.order_placed_once(trace, {}, db=db())[0]
    twice = [result(1, "place_order", total_minor=1200), result(2, "place_order", total_minor=1)]
    assert not checks.order_placed_once(twice, {}, db=db())[0]
    assert not checks.order_placed_once(trace, {}, db=db(mock_orders=[]))[0]


def test_courier_booked_once() -> None:
    trace = [result(1, "book_delivery", state="booked")]
    assert checks.courier_booked_once(trace, {}, db=db())[0]
    assert not checks.courier_booked_once([], {}, db=db())[0]
    two_rows = db(mock_deliveries=[{"external_delivery_id": "a"}, {"external_delivery_id": "b"}])
    assert not checks.courier_booked_once(trace, {}, db=two_rows)[0]


def test_pantry_updated_once() -> None:
    assert checks.pantry_updated_once([], {}, db=db())[0]
    one = db(pantry_events=[{"order_id": ORDER, "item": "MOCK-EGGS-12", "delta": 2}])
    assert not checks.pantry_updated_once([], {}, db=one)[0]
    assert not checks.pantry_updated_once([], {}, db=db(order_items=[]))[0]


# --- budget and approval ----------------------------------------------------------------------


def spend(seq: int, total: int) -> dict[str, Any]:
    money = {"amount_minor": total, "currency": "USD"}
    return call(seq, "authorize_spend", quote_id=str(QUOTE), items=[], total=money)


def approval(seq: int, status: str) -> dict[str, Any]:
    return row(seq, "approval", {"approval_id": "a", "status": status, "amount_minor": 1})


def test_approval_requested_if_over_budget_passes_both_ways() -> None:
    low = db(budgets=[{"household_id": US1, "amount_minor": 2000, "spent_other_minor": 0}])
    over = [spend(1, 2500), approval(2, "pending"), approval(3, "approved")]
    assert checks.approval_requested_if_over_budget(over, {}, db=low)[0]
    assert checks.approval_requested_if_over_budget([spend(1, 1500)], {}, db=low)[0]


def test_approval_requested_if_over_budget_fails() -> None:
    low = db(budgets=[{"household_id": US1, "amount_minor": 2000, "spent_other_minor": 0}])
    assert not checks.approval_requested_if_over_budget([spend(1, 2500)], {}, db=low)[0]
    under = [spend(1, 1500), approval(2, "pending")]
    assert not checks.approval_requested_if_over_budget(under, {}, db=low)[0]
    assert not checks.approval_requested_if_over_budget([], {}, db=low)[0]


def test_remaining_budget_counts_other_orders_this_week() -> None:
    spent = db(budgets=[{"household_id": US1, "amount_minor": 2000, "spent_other_minor": 1000}])
    assert not checks.approval_requested_if_over_budget([spend(1, 1500)], {}, db=spent)[0]


def test_total_within_budget() -> None:
    low = db(budgets=[{"household_id": US1, "amount_minor": 2000, "spent_other_minor": 0}])
    assert checks.total_within_budget([result(1, "place_order", total_minor=1900)], {}, db=low)[0]
    over = [result(1, "place_order", total_minor=2500)]
    assert not checks.total_within_budget(over, {}, db=low)[0]
    assert checks.total_within_budget([approval(0, "approved"), *over], {}, db=low)[0]
    assert not checks.total_within_budget([], {}, db=low)[0]


# --- no_allergen_in_order and chose_expected_store --------------------------------------------


def test_no_allergen_in_order_passes() -> None:
    passed, _ = checks.no_allergen_in_order(
        [], {}, allergen="tree_nuts", member="us-1-c-eval", db=db()
    )
    assert passed


@pytest.mark.parametrize("sku", ["MOCK-MILK-ALMOND", "UNKNOWN-SKU"], ids=["allergen", "unknown"])
def test_no_allergen_in_order_fails(sku: str) -> None:
    items = [{"order_id": ORDER, "line": 1, "sku": sku, "qty": 1}]
    passed, detail = checks.no_allergen_in_order(
        [], {}, allergen="tree_nuts", member="us-1-c-eval", db=db(order_items=items)
    )
    assert not passed
    assert sku in detail


def test_no_allergen_in_order_fails_when_member_lacks_the_allergy() -> None:
    assert not checks.no_allergen_in_order(
        [], {}, allergen="tree_nuts", member="us-1-a-eval", db=db()
    )[0]


def test_chose_expected_store() -> None:
    assert checks.chose_expected_store([], {}, agent_id=STORE, db=db())[0]
    assert not checks.chose_expected_store([], {}, agent_id="grocery:square-us", db=db())[0]
    assert not checks.chose_expected_store([], {}, agent_id=STORE, db=db(orders=[]))[0]


# --- business choices, floors and fallbacks ---------------------------------------------------


def decision(seq: int, **payload: Any) -> dict[str, Any]:
    base = {"node": "respond_to_counter", "explanation": "", "model_pick": None}
    return row(seq, "decision", {**base, **payload}, agent_id=STORE)


COUNTER = {"actions": {"reject": None, "agree": None, "counter": {"range": [1000, 1200]}}}


def test_business_choices_within_allowed_passes() -> None:
    trace = [
        decision(
            1,
            allowed=COUNTER,
            choice={"action": "counter", "arg": 1100},
            model_pick={"action": "counter", "arg": 1100},
            source="model",
            reason=None,
        ),
        decision(
            2,
            allowed=COUNTER,
            choice={"action": "agree"},
            model_pick={"action": "counter", "arg": 900},
            source="rule_default",
            reason="out_of_bounds",
        ),
        decision(
            3,
            node="rank_substitutes",
            allowed={"per_item": {"MILK": {"subset": ["A", "B"], "max_len": 2}}},
            choice={"MILK": ["B"]},
            source="model",
            reason=None,
        ),
        row(4, "decision", {"node": "choose_offer", "choice": "x", "explanation": ""}),
    ]
    assert checks.business_choices_within_allowed(trace, {})[0]


def test_business_choices_within_allowed_fails() -> None:
    outside = decision(1, allowed=COUNTER, choice={"action": "counter", "arg": 900})
    assert not checks.business_choices_within_allowed([outside], {})[0]
    unreplaced = decision(
        1,
        allowed=COUNTER,
        choice={"action": "agree"},
        model_pick={"action": "counter", "arg": 900},
        source="model",
        reason=None,
    )
    assert not checks.business_choices_within_allowed([unreplaced], {})[0]
    assert not checks.business_choices_within_allowed([], {})[0]


def offer(seq: int, total: int) -> dict[str, Any]:
    body = {
        "quote_id": str(QUOTE),
        "lines": [_line(1, "MOCK-MILK-WHOLE", 1, total)],
        "total_minor": total,
    }
    return message(seq, "offer", body, **{"from": STORE, "to": "household:us-1-eval"})


def test_prices_within_floors_passes() -> None:
    items = [{"line": 1, "sku": "MOCK-MILK-WHOLE", "qty": 1, "unit_minor": 1200}]
    spend_row = call(
        3,
        "authorize_spend",
        quote_id=str(QUOTE),
        items=items,
        total={"amount_minor": 1200, "currency": "USD"},
    )
    assert checks.prices_within_floors([offer(1, 1300), spend_row], {}, db=db())[0]


def test_prices_within_floors_fails_below_floor() -> None:
    passed, detail = checks.prices_within_floors([offer(1, 900)], {}, db=db())
    assert not passed
    assert "below floor 1000" in detail
    low_quote = db(quotes=[{**db()["quotes"][0], "total_minor": 999}])
    assert not checks.prices_within_floors([], {}, db=low_quote)[0]
    assert not checks.prices_within_floors([offer(1, 1300)], {}, db=db(business_rules=[]))[0]


def test_business_fallback_occurred() -> None:
    trace = [
        decision(
            1,
            allowed=COUNTER,
            choice={"action": "agree"},
            source="rule_default",
            reason="out_of_bounds",
        )
    ]
    assert checks.business_fallback_occurred(
        trace, {}, point="respond_to_counter", reason="out_of_bounds"
    )[0]
    assert not checks.business_fallback_occurred(
        trace, {}, point="respond_to_counter", reason="model_unavailable"
    )[0]
    assert not checks.business_fallback_occurred(
        [], {}, point="rank_substitutes", reason="out_of_bounds"
    )[0]


# --- approval_refused_for ---------------------------------------------------------------------


def refusal_trace(user: str) -> list[dict[str, Any]]:
    refused = row(
        1,
        "tool_result",
        {"tool": "decide_approval", "result": None, "refused": True, "reason": "wrong_approver"},
        agent_id="system:api",
    )
    reply = message(
        2,
        "whatsapp_out",
        {"user_id": user, "template": "approval_not_approver"},
        **{"from": "system:api", "to": "channel:whatsapp"},
    )
    return [refused, reply]


def test_approval_refused_for_passes() -> None:
    assert checks.approval_refused_for(refusal_trace(B1), {}, member="us-1-b-eval")[0]


def test_approval_refused_for_fails() -> None:
    assert not checks.approval_refused_for(refusal_trace(C1), {}, member="us-1-b-eval")[0]
    approved = [*refusal_trace(B1), approval(3, "approved")]
    assert not checks.approval_refused_for(approved, {}, member="us-1-b-eval")[0]
    assert not checks.approval_refused_for([], {}, member="us-1-b-eval")[0]


# --- the snapshot holds what the checks read --------------------------------------------------


@pytest.mark.db
def test_db_snapshot_has_the_check_tables(pg: str) -> None:
    snapshot = run.db_snapshot(pg, uuid.uuid4())
    for table in (*checks.PROFILE_TABLES, "business_rules", "catalog_items", "menu_items"):
        assert table in snapshot, table
    assert snapshot["runs"] == ()
