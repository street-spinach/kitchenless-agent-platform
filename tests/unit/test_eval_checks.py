from typing import Any

import pytest

from evals.checks import no_profile_fields_sent_to_business, outcome_recorded


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
