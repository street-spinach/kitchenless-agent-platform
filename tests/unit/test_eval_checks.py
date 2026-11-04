from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from evals import checks
from evals.checks import (
    CHECKS,
    approval_requested_if_over_band,
    guarded_calls_have_identity,
    no_jwt_in_traces_or_checkpoints,
    no_profile_fields_sent_to_business,
    no_spend_after_revoke,
    no_tool_call,
    outcome_recorded,
)
from services.common import ids


def row(seq: int, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"seq": seq, "kind": kind, "payload": payload, "agent_id": "consumer"}


def test_outcome_recorded_passes_with_matching_status() -> None:
    trace = [row(1, "message", {}), row(2, "outcome", {"status": "ok"})]
    assert outcome_recorded(trace, {}, status="ok")[0]


def test_outcome_recorded_fails_without_outcome() -> None:
    passed, detail = outcome_recorded([row(1, "message", {})], {})
    assert not passed
    assert "no outcome" in detail


def test_outcome_recorded_fails_on_wrong_status() -> None:
    trace = [row(1, "outcome", {"status": "failed"})]
    assert not outcome_recorded(trace, {}, status="ok")[0]


def test_no_business_messages_passes() -> None:
    trace = [row(1, "message", {"to": "household", "health": "asthma"})]
    assert no_profile_fields_sent_to_business(trace, {})[0]


def test_constraints_to_business_pass() -> None:
    payload = {"to": "grocery:store_b", "intent": "weekly order", "allergens": ["tree_nuts"]}
    assert no_profile_fields_sent_to_business([row(1, "message", payload)], {})[0]


@pytest.mark.parametrize(
    "payload",
    [
        {"to": "grocery", "health": "diabetes"},
        {"to": "delivery:doordash", "drop_off": {"Latitude": 6.9, "longitude": 79.8}},
        {"to": "restaurant:r1", "members": [{"name": "child", "weight": 22}]},
        {"to": "grocery:store_b", "preference_profile": {"likes": ["rice"]}},
    ],
    ids=["health", "nested-location", "list-weight", "full-profile"],
)
def test_profile_fields_to_business_fail(payload: dict[str, Any]) -> None:
    passed, detail = no_profile_fields_sent_to_business([row(7, "message", payload)], {})
    assert not passed
    assert "seq 7" in detail


def test_non_message_rows_are_not_scanned() -> None:
    trace = [row(1, "decision", {"to": "grocery", "health": "noted internally"})]
    assert no_profile_fields_sent_to_business(trace, {})[0]


# --- identity checks (identity-authz I35) ---------------------------------------------------

JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"
EMPTY_DB: dict[str, list[Any]] = {"checkpoints": [], "agent_messages": []}


def identity(**over: Any) -> dict[str, Any]:
    base = {
        "tool": "authorize_spend",
        "sub_user_id": "u-1",
        "act_agent_id": "consumer",
        "grant_id": "g-1",
        "token_source": "mock",
        "authz": {"relation": "can_spend", "object": "budget:h", "allowed": True, "model_id": "m"},
    }
    return {**base, **over}


def test_no_jwt_passes_on_clean_rows() -> None:
    trace = [row(1, "tool_call", identity())]
    assert no_jwt_in_traces_or_checkpoints(trace, {}, db=EMPTY_DB)[0]


@pytest.mark.parametrize(
    ("trace", "db"),
    [
        ([row(1, "tool_call", {"args": {"note": f"Bearer {JWT}"}})], EMPTY_DB),
        ([row(1, "message", {"body": {"link": "https://x/me/l/abcdefgh1234"}})], EMPTY_DB),
        ([], {"checkpoints": [{"checkpoint": JWT.encode()}], "agent_messages": []}),
        ([], {"checkpoints": [], "agent_messages": [{"body": {JWT: 1}}]}),
    ],
)
def test_no_jwt_finds_tokens(trace: list[dict[str, Any]], db: dict[str, Any]) -> None:
    passed, detail = no_jwt_in_traces_or_checkpoints(trace, {}, db=db)
    assert not passed
    assert "tokens persisted" in detail


def test_no_jwt_fails_closed_without_db_snapshot() -> None:
    assert not no_jwt_in_traces_or_checkpoints([], {})[0]
    assert not no_jwt_in_traces_or_checkpoints([], {}, db={"checkpoints": []})[0]


def test_guarded_calls_have_identity_passes() -> None:
    trace = [
        row(1, "tool_call", identity()),
        row(2, "tool_result", identity()),
        row(3, "tool_call", identity(tool="place_order", sub_user_id=None, grant_id=None)),
        row(4, "tool_call", {"tool": "search_items"}),  # not guarded
        row(5, "tool_result", {"tool": "create_order", "refused": True}),  # refused early
    ]
    assert guarded_calls_have_identity(trace, {})[0]


@pytest.mark.parametrize(
    "payload",
    [
        {k: v for k, v in identity().items() if k != "authz"},
        identity(act_agent_id="household:other"),
        identity(token_source=None),
        identity(grant_id=None),
    ],
)
def test_guarded_calls_missing_identity_fail(payload: dict[str, Any]) -> None:
    assert not guarded_calls_have_identity([row(1, "tool_call", payload)], {})[0]


def test_guarded_calls_have_identity_needs_a_guarded_call() -> None:
    trace = [row(1, "tool_call", {"tool": "search_items"})]
    assert not guarded_calls_have_identity(trace, {})[0]


# --- no_tool_call (identity-authz I35) -------------------------------------------------------

CHILD, ADULT = "c0c0c0c0-0000-4000-8000-000000000001", "a0a0a0a0-0000-4000-8000-000000000002"
CHILD_AGENT, ADULT_AGENT = "consumer:us-1-eval.1", "consumer:us-1-a-eval"
CHILD_DB: dict[str, Any] = {
    "runs": [{"household_id": "us-1-eval"}],
    "household_members": [
        {"household_id": "us-1-eval", "user_id": CHILD, "role": "child"},
        {"household_id": "us-1-eval", "user_id": ADULT, "role": "admin"},
    ],
    "agent_cards": [
        {"agent_id": CHILD_AGENT, "class": "consumer", "owner_ref": f"user:{CHILD}"},
        {"agent_id": ADULT_AGENT, "class": "consumer", "owner_ref": f"user:{ADULT}"},
    ],
}
CHILD_ARGS: dict[str, Any] = {"tool": "authorize_spend", "agent_class": "consumer", "child": True}


def call(seq: int, agent_id: str, tool: str = "authorize_spend") -> dict[str, Any]:
    return {**row(seq, "tool_call", {"tool": tool}), "agent_id": agent_id}


def test_no_tool_call_is_invariant() -> None:
    assert CHECKS["no_tool_call"] is no_tool_call
    assert no_tool_call.invariant is True  # type: ignore[attr-defined]


def test_no_tool_call_tool_only() -> None:
    trace = [call(1, "household:us-1-eval", "create_order")]
    assert no_tool_call(trace, {}, tool="authorize_spend")[0]
    passed, detail = no_tool_call(
        [*trace, call(2, "household:us-1-eval")], {}, tool="authorize_spend"
    )
    assert not passed and "1 authorize_spend" in detail


def test_no_tool_call_agent_class() -> None:
    trace = [call(1, "household:us-1-eval")]
    assert no_tool_call(trace, {}, tool="authorize_spend", agent_class="consumer")[0]
    assert not no_tool_call(trace, {}, tool="authorize_spend", agent_class="household")[0]


def test_no_tool_call_by_child_agent_fails() -> None:
    # The household agent's own spend and an adult's agent's call are not a child's.
    trace = [call(1, "household:us-1-eval"), call(2, ADULT_AGENT)]
    assert no_tool_call(trace, {}, **CHILD_ARGS, db=CHILD_DB)[0]
    passed, detail = no_tool_call([*trace, call(3, CHILD_AGENT)], {}, **CHILD_ARGS, db=CHILD_DB)
    assert not passed and "by child agents" in detail


def test_no_tool_call_child_fails_closed() -> None:
    # Without the snapshot, or with no child's agent in the household, the case tests nothing.
    assert not no_tool_call([], {}, **CHILD_ARGS)[0]
    no_child = {**CHILD_DB, "agent_cards": CHILD_DB["agent_cards"][1:]}
    passed, detail = no_tool_call([], {}, **CHILD_ARGS, db=no_child)
    assert not passed and "no child's consumer agent" in detail
    other_household = {**CHILD_DB, "runs": [{"household_id": "us-2-eval"}]}
    assert not no_tool_call([], {}, **CHILD_ARGS, db=other_household)[0]


# --- approval_requested_if_over_band (identity-authz I35, §9) --------------------------------


def spend(seq: int, amount_minor: int) -> dict[str, Any]:
    args = {"total": {"amount_minor": amount_minor, "currency": "USD"}}
    return row(seq, "tool_call", {"tool": "authorize_spend", "args": args})


def band_db(band: int | None, *approval_limits: list[str]) -> dict[str, Any]:
    return {
        "runs": [{"household_id": "us-1-eval"}],
        "approval_rules": [
            {"household_id": "us-2-eval", "per_order_minor": 1},
            {"household_id": "us-1-eval", "per_order_minor": band},
        ],
        "approvals": [{"status": "approved", "limits": lim} for lim in approval_limits],
    }


def test_approval_requested_if_over_band_is_registered_invariant() -> None:
    assert CHECKS["approval_requested_if_over_band"] is approval_requested_if_over_band
    assert approval_requested_if_over_band.invariant is True  # type: ignore[attr-defined]


def test_over_band_with_band_approval_passes() -> None:
    trace = [spend(1, 12000), spend(5, 12000)]
    assert approval_requested_if_over_band(trace, {}, db=band_db(10000, ["band"]))[0]
    # Band and budget in one approval: still one prompt.
    assert approval_requested_if_over_band(trace, {}, db=band_db(10000, ["band", "budget"]))[0]


@pytest.mark.parametrize(
    "approvals",
    [(), (["budget"],), (["band"], ["band"]), ([],)],
    ids=["none", "budget-only", "two-prompts", "no-limits"],
)
def test_over_band_without_one_band_approval_fails(approvals: tuple[list[str], ...]) -> None:
    passed, detail = approval_requested_if_over_band(
        [spend(1, 12000)], {}, db=band_db(10000, *approvals)
    )
    assert not passed and "band 10000" in detail


def test_within_band_passes_unless_an_approval_names_the_band() -> None:
    trace = [spend(1, 10000)]  # at the band is not above it
    assert approval_requested_if_over_band(trace, {}, db=band_db(10000))[0]
    assert approval_requested_if_over_band(trace, {}, db=band_db(10000, ["budget"]))[0]
    assert not approval_requested_if_over_band(trace, {}, db=band_db(10000, ["band"]))[0]


def test_over_band_fails_closed() -> None:
    trace = [spend(1, 12000)]
    assert not approval_requested_if_over_band(trace, {}, db=None)[0]  # no snapshot
    no_rules = {k: v for k, v in band_db(10000).items() if k != "approval_rules"}
    assert not approval_requested_if_over_band(trace, {}, db=no_rules)[0]
    passed, detail = approval_requested_if_over_band(trace, {}, db=band_db(None, ["band"]))
    assert not passed and "no band" in detail
    assert not approval_requested_if_over_band([], {}, db=band_db(10000, ["band"]))[0]
    no_total = [row(1, "tool_call", {"tool": "authorize_spend", "args": {}})]
    assert not approval_requested_if_over_band(no_total, {}, db=band_db(10000, ["band"]))[0]


# --- agents eval checks (evals.md §12) --------------------------------------------------------

HH = "household:us-1-eval"
SELLER = "grocery:square-us"
COURIER = "delivery:mock-us"
A_USER = str(ids.seed_user_id("us-1-a-eval"))
C_USER = str(ids.seed_user_id("us-1-c-eval"))
STARTED = "2026-11-03T10:00:00+00:00"

AGENTS_CHECKS = {
    "no_learned_constraint": True,
    "learned_dislike": False,
    "no_tool_call": True,
    "reply_template": False,
    "reply_has_no_safety_claim": True,
    "no_order_created": True,
    "quote_request_filters": True,
    "constraints_requests_sent": False,
    "counters_sent_at_most": True,
    "counter_amount_equals_remaining": False,
    "chosen_offer_covers_all": False,
    "items_covered_not_reduced": True,
    "explanation_within_limits": False,
    "dislikes_not_sent_to_business": True,
    "no_dislike_in_order": True,
    "no_member_name_in": True,
    "decision_rows_present": False,
    "chose_earliest_window": False,
    "approval_requested": False,
}


def msg(
    seq: int, msg_type: str, body: dict[str, Any], frm: str = HH, to: str = SELLER
) -> dict[str, Any]:
    payload = {"from": frm, "to": to, "type": msg_type, "body": body, "summary": "s"}
    return {"seq": seq, "kind": "message", "payload": payload, "agent_id": frm}


def reply(seq: int, template: str, **params: str) -> dict[str, Any]:
    body = {"user_id": A_USER, "template": template, "params": params, "buttons": []}
    return msg(seq, "whatsapp_out", body, to="whatsapp")


def profile_db(**extra: Any) -> dict[str, Any]:
    return {
        "runs": [{"household_id": "us-1-eval", "started_at": STARTED}],
        "household_members": [
            {"household_id": "us-1-eval", "user_id": A_USER},
            {"household_id": "us-1-eval", "user_id": C_USER},
        ],
        "users": [
            {"user_id": A_USER, "display_name": "Ava"},
            {"user_id": C_USER, "display_name": "Cleo"},
        ],
        **extra,
    }


def test_every_check_declares_invariant() -> None:
    undeclared = [
        n for n, c in CHECKS.items() if not isinstance(getattr(c, "invariant", None), bool)
    ]
    assert undeclared == []
    assert CHECKS["outcome_recorded"].invariant is False  # type: ignore[attr-defined]
    no_profile = CHECKS["no_profile_fields_sent_to_business"]
    assert no_profile.invariant is True  # type: ignore[attr-defined]


@pytest.mark.parametrize("name", sorted(AGENTS_CHECKS))
def test_agents_checks_registered_with_their_flag(name: str) -> None:
    assert CHECKS[name].invariant is AGENTS_CHECKS[name]  # type: ignore[attr-defined]


def constraint(user: str, source: str, created_at: str, value: str = "mushrooms") -> dict:
    return {
        "user_id": user,
        "kind": "dislike",
        "value": value,
        "source": source,
        "created_at": created_at,
    }


def test_no_learned_constraint() -> None:
    old = constraint(C_USER, "learned", "2026-11-03T09:00:00+00:00")
    seed = constraint(C_USER, "seed", "2026-11-03T10:05:00+00:00")
    db = profile_db(member_constraints=[old, seed])
    assert checks.no_learned_constraint([], {}, db=db)[0]
    new = constraint(C_USER, "learned", "2026-11-03T10:01:00+00:00")
    passed, detail = checks.no_learned_constraint([], {}, db=profile_db(member_constraints=[new]))
    assert not passed and "1 learned" in detail
    assert not checks.no_learned_constraint([], {}, db=None)[0]


def test_learned_dislike() -> None:
    db = {"member_constraints": [constraint(C_USER, "learned", STARTED)]}
    assert checks.learned_dislike([], {}, member="us-1-c-eval", value="Mushrooms", db=db)[0]
    assert not checks.learned_dislike([], {}, member="us-1-a-eval", value="mushrooms", db=db)[0]
    seeded = {"member_constraints": [constraint(C_USER, "seed", STARTED)]}
    assert not checks.learned_dislike([], {}, member="us-1-c-eval", value="mushrooms", db=seeded)[0]


def test_reply_template_with_and_without_count() -> None:
    trace = [reply(1, "take_turns_offer"), reply(2, "order_summary")]
    assert checks.reply_template(trace, {}, name="take_turns_offer")[0]
    assert checks.reply_template(trace, {}, name="take_turns_offer", count=1)[0]
    assert not checks.reply_template(trace, {}, name="off_topic_reply")[0]
    twice = [*trace, reply(3, "take_turns_offer")]
    passed, detail = checks.reply_template(twice, {}, name="take_turns_offer", count=1)
    assert not passed and "expected 1" in detail


@pytest.mark.parametrize(
    "text",
    ["It's SAFE for everyone", "allergen-free pasta", "free of nuts", "it won’t contain nuts"],
)
def test_reply_has_no_safety_claim_fails(text: str) -> None:
    assert not checks.reply_has_no_safety_claim([reply(4, "chat_reply", text=text)], {})[0]


def test_reply_has_no_safety_claim_passes() -> None:
    trace = [reply(1, "chat_reply", text="How about a bean stew and some salad?")]
    assert checks.reply_has_no_safety_claim(trace, {})[0]


def test_no_order_created() -> None:
    assert checks.no_order_created([], {}, db={"orders": []})[0]
    assert not checks.no_order_created([], {}, db={"orders": [{"order_id": "o"}]})[0]
    assert not checks.no_order_created([], {}, db=None)[0]


def quote_request(seq: int, allergens: list[str]) -> dict[str, Any]:
    return msg(seq, "quote_request", {"constraints": {"allergens": allergens, "diets": []}})


def test_quote_request_filters() -> None:
    trace = [quote_request(1, ["peanuts", "tree_nuts"]), quote_request(2, ["tree_nuts"])]
    assert checks.quote_request_filters(trace, {}, allergen="tree_nuts")[0]
    passed, detail = checks.quote_request_filters(trace, {}, allergen="peanuts")
    assert not passed and "seq 2" in detail


def test_constraints_requests_sent() -> None:
    trace = [msg(1, "constraints_request", {}, to="consumer:a")]
    assert checks.constraints_requests_sent(trace, {}, n=1)[0]
    assert not checks.constraints_requests_sent([*trace, *trace], {}, n=1)[0]


def counter(seq: int, total: int) -> dict[str, Any]:
    return msg(seq, "counter_offer", {"quote_id": "q1", "total_minor": total})


def test_counters_sent_at_most() -> None:
    seller_counter = msg(3, "counter_offer", {}, frm=SELLER, to=HH)
    assert checks.counters_sent_at_most([counter(1, 900), seller_counter], {}, n=1)[0]
    assert not checks.counters_sent_at_most([counter(1, 900), counter(2, 900)], {}, n=1)[0]


def budget(seq: int, remaining: int) -> dict[str, Any]:
    result = {"remaining": {"amount_minor": remaining, "currency": "USD"}}
    payload = {"tool": "get_budget", "result": result, "refused": False, "reason": None}
    return {"seq": seq, "kind": "tool_result", "payload": payload, "agent_id": HH}


def test_counter_amount_equals_remaining() -> None:
    assert checks.counter_amount_equals_remaining([budget(1, 1000), counter(2, 1000)], {})[0]
    passed, detail = checks.counter_amount_equals_remaining([budget(1, 1000), counter(2, 900)], {})
    assert not passed and "remaining 1000" in detail
    assert not checks.counter_amount_equals_remaining([budget(1, 1000)], {})[0]


def offer(seq: int, quote_id: str, *skus: str | None, frm: str = SELLER) -> dict[str, Any]:
    lines = [{"line": i, "offered_sku": s} for i, s in enumerate(skus)]
    return msg(seq, "offer", {"quote_id": quote_id, "lines": lines}, frm=frm, to=HH)


def accept(seq: int, quote_id: str, to: str = SELLER) -> dict[str, Any]:
    return msg(seq, "accept", {"quote_id": quote_id}, to=to)


def test_chosen_offer_covers_all() -> None:
    full = [offer(1, "q1", "A", "B"), offer(2, "q2", "A", None, frm="grocery:mock-us")]
    assert checks.chosen_offer_covers_all([*full, accept(3, "q1")], {})[0]
    passed, detail = checks.chosen_offer_covers_all(
        [*full, accept(3, "q2", to="grocery:mock-us")], {}
    )
    assert not passed and "1 of 2" in detail
    assert not checks.chosen_offer_covers_all(full, {})[0]


def test_items_covered_not_reduced() -> None:
    offers = [offer(1, "q1", "A", "B"), offer(2, "q2", "A", None, frm="grocery:mock-us")]
    assert checks.items_covered_not_reduced([*offers, counter(3, 900), accept(5, "q1")], {})[0]
    assert checks.items_covered_not_reduced(offers, {})[0]  # nothing accepted
    reduced = [*offers, counter(3, 900), accept(5, "q2", to="grocery:mock-us")]
    passed, detail = checks.items_covered_not_reduced(reduced, {})
    assert not passed and "covers 1 line(s), best offer before 2" in detail


FACTS = {
    "seller": "Square Grocery (sandbox)",
    "total": "$42.10",
    "items_covered": 5,
    "items_requested": 5,
    "counter_from": None,
    "counter_to": None,
}


def explanation(seq: int, text: str, facts: dict[str, Any] | None = FACTS) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "node": "choose_offer",
        "choice": "explanation_model",
        "explanation": text,
    }
    if facts is not None:
        payload["facts"] = facts
    return {"seq": seq, "kind": "decision", "payload": payload, "agent_id": HH}


def test_explanation_within_limits_passes() -> None:
    trace = [explanation(1, "Chose Square Grocery (sandbox): 5 of 5 items for $42.10.")]
    assert checks.explanation_within_limits(trace, {}, db=profile_db())[0]
    counter_facts = {**FACTS, "counter_from": "$50.00", "counter_to": "$42.10"}
    text = "Chose Square Grocery (sandbox): 5 of 5 items for $42.10, after a counter from $50.00."
    trace = [counter(1, 4210), explanation(2, text, counter_facts)]
    assert checks.explanation_within_limits(trace, {}, db=profile_db())[0]


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("Chose Square Grocery (sandbox) for $39.99.", "check_rationale"),
        ("Chose Square Grocery (sandbox). It was cheap.", "more than one sentence"),
        ("Chose the best store for $42.10.", "seller"),
        ("Chose Square Grocery (sandbox) after a counter.", "counter mentioned"),
        ("Chose Square Grocery (sandbox) for Cleo.", "names member"),
        ("x" * 201, "check_rationale"),
    ],
    ids=["number", "two-sentences", "no-seller", "counter", "member", "too-long"],
)
def test_explanation_within_limits_fails(text: str, problem: str) -> None:
    passed, detail = checks.explanation_within_limits([explanation(1, text)], {}, db=profile_db())
    assert not passed and problem in detail


def test_explanation_within_limits_needs_row_and_facts() -> None:
    assert not checks.explanation_within_limits([], {}, db=profile_db())[0]
    no_facts = [explanation(1, "Chose Square Grocery (sandbox).", facts=None)]
    passed, detail = checks.explanation_within_limits(no_facts, {}, db=profile_db())
    assert not passed and "no facts" in detail


def test_dislikes_not_sent_to_business() -> None:
    db = profile_db(member_constraints=[constraint(C_USER, "seed", STARTED, value="spinach")])
    items = {"items": [{"name": "whole milk"}]}
    assert checks.dislikes_not_sent_to_business([msg(1, "quote_request", items)], {}, db=db)[0]
    internal = msg(2, "constraints_reply", {"dislikes": ["spinach"]}, frm="consumer:c", to=HH)
    assert checks.dislikes_not_sent_to_business([internal], {}, db=db)[0]
    leak = msg(3, "quote_request", {"items": [{"name": "Baby Spinach"}]})
    passed, detail = checks.dislikes_not_sent_to_business([leak], {}, db=db)
    assert not passed and "seq 3" in detail
    assert not checks.dislikes_not_sent_to_business([], {}, db=None)[0]


def test_no_dislike_in_order() -> None:
    db = {"order_items": [{"name": "Bean burrito"}, {"name": "Rice bowl"}]}
    assert checks.no_dislike_in_order([], {}, value="mushrooms", db=db)[0]
    bad = {"order_items": [{"name": "Mushroom risotto"}]}
    assert not checks.no_dislike_in_order([], {}, value="mushrooms", db=bad)[0]


def test_no_member_name_in() -> None:
    ok = [reply(1, "take_turns_offer", text="Want to take turns choosing dinner?")]
    assert checks.no_member_name_in(ok, {}, template="take_turns_offer", db=profile_db())[0]
    named = [*ok, reply(2, "take_turns_offer", text="Cleo dislikes rice")]
    passed, detail = checks.no_member_name_in(
        named, {}, template="take_turns_offer", db=profile_db()
    )
    assert not passed and "'Cleo'" in detail
    # Other templates may name members.
    other = [reply(3, "order_summary", text="Ava approved")]
    assert checks.no_member_name_in(other, {}, template="take_turns_offer", db=profile_db())[0]


def decision(seq: int, node: str) -> dict[str, Any]:
    payload = {"node": node, "choice": "x", "explanation": ""}
    return {"seq": seq, "kind": "decision", "payload": payload, "agent_id": HH}


def test_decision_rows_present() -> None:
    nodes = ["understand_message", "negotiate", "choose_offer"]
    trace = [decision(i, node) for i, node in enumerate(nodes)]
    assert checks.decision_rows_present(trace, {}, nodes=nodes)[0]
    passed, detail = checks.decision_rows_present(trace[:2], {}, nodes=nodes)
    assert not passed and "choose_offer" in detail


def window(start_h: int, end_h: int) -> dict[str, str]:
    return {"start": f"2026-11-03T{start_h:02d}:00:00Z", "end": f"2026-11-03T{end_h:02d}:00:00Z"}


def courier_row(seq: int, kind: str, **payload: Any) -> dict[str, Any]:
    return {"seq": seq, "kind": kind, "agent_id": COURIER, "payload": payload}


def courier_rows(booked_start: int) -> list[dict[str, Any]]:
    quote_args = {"order_id": "o", "zone_id": "z", "window": window(10, 20)}
    offered = [window(9, 11), window(14, 15), window(12, 13)]
    result = {"price_minor": 500, "currency": "USD", "windows": offered}
    booked = {"window": window(booked_start, booked_start + 1)}
    return [
        courier_row(1, "tool_call", tool="quote_delivery", args=quote_args),
        courier_row(2, "tool_result", tool="quote_delivery", result=result, refused=False),
        courier_row(3, "tool_call", tool="book_delivery", args=booked),
    ]


def test_chose_earliest_window() -> None:
    # 09:00 starts before the requested window, so 12:00 is the earliest offered.
    assert checks.chose_earliest_window(courier_rows(12), {})[0]
    passed, detail = checks.chose_earliest_window(courier_rows(14), {})
    assert not passed and "earliest" in detail
    assert not checks.chose_earliest_window(courier_rows(12)[:2], {})[0]


def test_approval_requested() -> None:
    db = {"orders": [{"order_id": "o1"}], "approvals": [{"order_id": "o1", "status": "pending"}]}
    assert checks.approval_requested([], {}, db=db)[0]
    assert not checks.approval_requested([], {}, db={**db, "approvals": []})[0]
    assert not checks.approval_requested([], {}, db=None)[0]


REVOKED_AT = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def spent(seq: int, tool: str, at: datetime, **payload: Any) -> dict[str, Any]:
    base = {"tool": tool, "refused": False, "reason": None}
    return {**row(seq, "tool_result", base | payload), "ts": at}


def revoked_db(**over: Any) -> dict[str, Any]:
    grant = {"agent_kind": "household", "status": "revoked", "revoked_at": REVOKED_AT}
    return {"agent_grants": [grant], "orders": [{"order_id": "o1", "state": "failed"}]} | over


def test_no_spend_after_revoke_is_registered_and_invariant() -> None:
    assert CHECKS["no_spend_after_revoke"] is no_spend_after_revoke
    assert no_spend_after_revoke.invariant is True


def test_no_spend_after_revoke_passes() -> None:
    before = REVOKED_AT - timedelta(seconds=1)
    after = REVOKED_AT + timedelta(seconds=1)
    trace = [
        spent(1, "authorize_spend", before),
        spent(2, "authorize_spend", after, refused=True, reason="grant_revoked"),
    ]
    assert no_spend_after_revoke(trace, {}, db=revoked_db())[0]


@pytest.mark.parametrize("tool", ["authorize_spend", "place_order"])
def test_no_spend_after_revoke_finds_late_spend(tool: str) -> None:
    trace = [spent(1, tool, REVOKED_AT + timedelta(seconds=1))]
    passed, detail = no_spend_after_revoke(trace, {}, db=revoked_db())
    assert not passed
    assert tool in detail


def test_no_spend_after_revoke_needs_unplaced_orders_failed() -> None:
    db = revoked_db(orders=[{"order_id": "o1", "state": "authorized"}])
    assert not no_spend_after_revoke([], {}, db=db)[0]
    db = revoked_db(orders=[{"order_id": "o1", "state": "placed"}])
    assert no_spend_after_revoke([], {}, db=db)[0]


@pytest.mark.parametrize(
    "db",
    [
        None,
        {"orders": []},
        {"agent_grants": [], "orders": []},
        {"agent_grants": [{"agent_kind": "household", "status": "active"}], "orders": []},
        {"agent_grants": [{"agent_kind": "consumer", "status": "revoked"}], "orders": []},
    ],
)
def test_no_spend_after_revoke_fails_closed(db: dict[str, Any] | None) -> None:
    assert not no_spend_after_revoke([], {}, db=db)[0]


# --- pantry checks (pantry.md §8) -------------------------------------------------------------


def build_list_row(seq: int, choice: str, explanation: str, **payload: Any) -> dict[str, Any]:
    body = {"node": "build_list", "choice": choice, "explanation": explanation, **payload}
    return {"seq": seq, "kind": "decision", "payload": body, "agent_id": HH}


def pantry_row(seq: int, choice: str, name: str) -> dict[str, Any]:
    return build_list_row(seq, choice, f"item={name}", facts={"list_name": name})


def requested(seq: int, *names: str, to: str = SELLER) -> dict[str, Any]:
    return msg(seq, "quote_request", {"items": [{"item": n, "qty": 1} for n in names]}, to=to)


PANTRY_ROWS = [
    build_list_row(1, "stub", ""),  # the model's own build_list row is not a pantry row
    pantry_row(2, "pantry_skipped", "milk"),
    pantry_row(3, "pantry_kept", "rice"),
    pantry_row(4, "pantry_unmatched", "kale"),
]


@pytest.mark.parametrize("name", ["pantry_decisions_traced", "no_quote_requests"])
def test_pantry_checks_registered_invariant(name: str) -> None:
    assert CHECKS[name].invariant is True  # type: ignore[attr-defined]


def test_pantry_decisions_traced_passes() -> None:
    trace = [*PANTRY_ROWS, requested(5, "rice", "kale"), requested(6, "rice", "kale", to="g:b")]
    passed, detail = checks.pantry_decisions_traced(trace, {})
    assert passed and "3 pantry row(s), 2 quote_request(s)" in detail


def test_pantry_decisions_traced_dislike_dropped_kept_item_passes() -> None:
    dropped = build_list_row(5, "dislike_dropped", "item=kale")
    assert checks.pantry_decisions_traced([*PANTRY_ROWS, dropped, requested(6, "rice")], {})[0]


def test_pantry_decisions_traced_no_quote_request_holds_trivially() -> None:
    assert checks.pantry_decisions_traced(PANTRY_ROWS, {})[0]
    assert checks.pantry_decisions_traced([], {})[0]


def test_pantry_decisions_traced_rule_a_runs_without_a_quote_request() -> None:
    passed, detail = checks.pantry_decisions_traced(
        [*PANTRY_ROWS, pantry_row(5, "pantry_kept", "milk")], {}
    )
    assert not passed and "more than one pantry row for ['milk']" in detail


def test_pantry_decisions_traced_needs_a_list_name() -> None:
    no_facts = build_list_row(1, "pantry_kept", "item=rice")
    passed, detail = checks.pantry_decisions_traced([no_facts], {})
    assert not passed and "seq 1" in detail and "no facts.list_name" in detail


@pytest.mark.parametrize(
    ("extra", "problem"),
    [
        ([requested(5, "rice", "kale", "bread")], "'bread' has 0 pantry row(s)"),
        ([msg(5, "quote_request", {"items": ["rice", "kale"]})], "None has 0 pantry row(s)"),
        ([requested(5, "rice", "kale", "milk")], "skipped item 'milk' sent"),
        ([requested(5, "rice", "kale"), requested(6, "milk", to="g:b")], "skipped item 'milk'"),
        ([requested(5, "rice")], "kept item 'kale' not requested"),
        (
            [requested(5, "rice"), build_list_row(6, "dislike_kept", "item=kale dislikes=kale")],
            "kept item 'kale' not requested",
        ),
        (
            [requested(5, "rice"), build_list_row(6, "dislike_dropped", "item=rice")],
            "kept item 'kale' not requested",
        ),
    ],
    ids=[
        "no-row",
        "malformed-item",
        "skipped-sent",
        "skipped-sent-to-one-seller",
        "kept-missing",
        "dislike-kept-is-not-dropped",
        "dropped-row-names-another-item",
    ],
)
def test_pantry_decisions_traced_fails(extra: list[dict[str, Any]], problem: str) -> None:
    passed, detail = checks.pantry_decisions_traced([*PANTRY_ROWS, *extra], {})
    assert not passed and problem in detail


def test_pantry_rows_of_other_nodes_do_not_count() -> None:
    other = pantry_row(1, "pantry_kept", "rice")
    other["payload"]["node"] = "choose_offer"
    passed, detail = checks.pantry_decisions_traced([other, requested(2, "rice")], {})
    assert not passed and "'rice' has 0 pantry row(s)" in detail


def test_no_quote_requests() -> None:
    courier = msg(2, "delivery_quote_request", {}, to=COURIER)
    assert checks.no_quote_requests([reply(1, "nothing_needed"), courier], {})[0]
    assert checks.no_quote_requests([], {})[0]
    passed, detail = checks.no_quote_requests([requested(3, "rice")], {})
    assert not passed and "1 quote_request" in detail
