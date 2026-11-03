from typing import Any

import pytest

from evals.checks import (
    CHECKS,
    guarded_calls_have_identity,
    no_jwt_in_traces_or_checkpoints,
    no_profile_fields_sent_to_business,
    no_tool_call,
    outcome_recorded,
)


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
