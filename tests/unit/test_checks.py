"""The experience eval checks and coverage entries (experience §14, EX-I52)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from evals import select as case_order
from evals.checks import (
    CHECKS,
    member_constraints_added,
    no_item_with_allergen,
    no_sim_outbound,
    restatement_names_unsaved,
)

PHONE = "+15550100001"
OTHER_PHONE = "+15550100002"
STARTED = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
OUTSIDE_WINDOW = STARTED - timedelta(hours=30)


def _sim_db(*rows: dict[str, Any], last_inbound_at: datetime | None = OUTSIDE_WINDOW) -> dict:
    return {
        "runs": [{"started_at": STARTED}],
        "channel_identities": [{"phone_e164": PHONE, "last_inbound_at": last_inbound_at}],
        "sim_outbound": list(rows),
    }


def _sim_row(template: str, at: datetime, phone: str = PHONE) -> dict[str, Any]:
    return {"phone_e164": phone, "body": {"template": template}, "created_at": at}


def _restate(seq: int, unmatched: Any) -> dict[str, Any]:
    body = {"template": "setup_restate", "params": {"lines": "Sam", "unmatched": unmatched}}
    return {"seq": seq, "kind": "message", "payload": {"type": "whatsapp_out", "body": body}}


def _not_saved(*words: str) -> str:
    quoted = ", ".join(f"'{w}'" for w in words)
    return f"Not saved: {quoted} (I can record: peanuts, tree nuts, vegan). "


def _order_db(*skus: str) -> dict[str, Any]:
    return {
        "orders": [{"order_id": "o1", "seller_agent_id": "grocery:store_a"}],
        "order_items": [{"order_id": "o1", "sku": sku} for sku in skus],
        "catalog_items": [
            {"agent_id": "grocery:store_a", "sku": "rice", "allergens": []},
            {"agent_id": "grocery:store_a", "sku": "satay", "allergens": ["peanuts"]},
        ],
        "menu_items": [{"agent_id": "grocery:store_a", "sku": "curry", "allergens": None}],
    }


def test_experience_checks() -> None:
    """Each §14 check passes on a good run, fails on a bad one and is registered."""
    assert no_sim_outbound([], {}, phone=PHONE, db=_sim_db())[0]
    sent = _sim_db(_sim_row("approval_request", STARTED + MINUTE))
    assert not no_sim_outbound([], {}, phone=PHONE, db=sent)[0]

    seeded = {"kind": "allergy", "value": "eggs", "created_at": STARTED - MINUTE}
    constraints = {"runs": [{"started_at": STARTED}], "member_constraints": [seeded]}
    assert member_constraints_added([], {}, n=0, db=constraints)[0]
    added = {"kind": "allergy", "value": "peanuts", "created_at": STARTED + MINUTE}
    constraints["member_constraints"].append(added)
    assert not member_constraints_added([], {}, n=0, db=constraints)[0]
    assert member_constraints_added([], {}, n=1, db=constraints)[0]

    assert restatement_names_unsaved([_restate(1, _not_saved("kiwi"))], {}, word="kiwi")[0]
    assert not restatement_names_unsaved([_restate(1, _not_saved("mango"))], {}, word="kiwi")[0]

    assert no_item_with_allergen([], {}, allergen="peanuts", db=_order_db("rice", "curry"))[0]
    assert not no_item_with_allergen([], {}, allergen="peanuts", db=_order_db("rice", "satay"))[0]

    for name in (
        "no_sim_outbound",
        "member_constraints_added",
        "restatement_names_unsaved",
        "no_item_with_allergen",
    ):
        assert CHECKS[name].invariant is True  # type: ignore[attr-defined]


def test_experience_sim_outbound_only_counts_the_phone_and_template() -> None:
    db = _sim_db(
        _sim_row("approval_request", STARTED + MINUTE, OTHER_PHONE),
        _sim_row("held_cancelled_line", STARTED + MINUTE),
    )
    assert not no_sim_outbound([], {}, phone=PHONE, db=db)[0]
    assert no_sim_outbound([], {}, phone=PHONE, template="approval_request", db=db)[0]
    assert not no_sim_outbound([], {}, phone=PHONE, template="held_cancelled_line", db=db)[0]


def test_experience_sim_outbound_until_write() -> None:
    wrote = STARTED + 2 * MINUTE
    after = _sim_row("approval_request", wrote + MINUTE)
    before = _sim_row("approval_request", wrote - MINUTE)
    assert no_sim_outbound(
        [], {}, phone=PHONE, until_write=True, db=_sim_db(after, last_inbound_at=wrote)
    )[0]
    assert not no_sim_outbound(
        [], {}, phone=PHONE, until_write=True, db=_sim_db(before, after, last_inbound_at=wrote)
    )[0]
    # Without `until_write`, the write excuses nothing.
    assert not no_sim_outbound([], {}, phone=PHONE, db=_sim_db(after, last_inbound_at=wrote))[0]


@pytest.mark.parametrize("last_inbound_at", [OUTSIDE_WINDOW, None])
def test_experience_sim_outbound_no_write_in_the_run(last_inbound_at: datetime | None) -> None:
    """A write from before the run, or none at all, excuses nothing."""
    db = _sim_db(_sim_row("approval_request", STARTED + MINUTE), last_inbound_at=last_inbound_at)
    assert not no_sim_outbound([], {}, phone=PHONE, until_write=True, db=db)[0]


@pytest.mark.parametrize(
    ("db", "detail"),
    [
        (None, "no db snapshot"),
        ({"runs": [{"started_at": STARTED}]}, "lacks"),
        ({"runs": [], "channel_identities": [], "sim_outbound": []}, "no run row"),
    ],
)
def test_experience_sim_outbound_fails_closed_without_data(db: Any, detail: str) -> None:
    passed, got = no_sim_outbound([], {}, phone=PHONE, db=db)
    assert not passed
    assert detail in got


def test_experience_sim_outbound_unknown_phone_fails_closed() -> None:
    passed, detail = no_sim_outbound([], {}, phone=OTHER_PHONE, db=_sim_db())
    assert not passed
    assert "no channel identity" in detail


def test_experience_constraints_fail_closed_without_a_run() -> None:
    assert not member_constraints_added([], {}, n=0, db=None)[0]
    assert not member_constraints_added([], {}, n=0, db={"runs": [], "member_constraints": []})[0]


def test_experience_restatement_needs_the_quoted_word() -> None:
    """A recordable code in the "I can record" list is not a word named as not saved."""
    trace = [_restate(1, _not_saved("kiwi"))]
    assert not restatement_names_unsaved(trace, {}, word="peanuts")[0]
    assert restatement_names_unsaved(trace, {}, word="KIWI")[0]


def test_experience_restatement_counts_a_cross_check_word() -> None:
    unsaved = "I also saw 'shellfish' but didn't save it. Say 'add shellfish for Sam' if so."
    assert restatement_names_unsaved([_restate(1, unsaved)], {}, word="shellfish")[0]


def test_experience_restatement_reads_the_last_one_and_only_the_not_saved_part() -> None:
    assert not restatement_names_unsaved(
        [_restate(1, _not_saved("kiwi")), _restate(2, "")], {}, word="kiwi"
    )[0]
    named_in_lines = _restate(1, "")
    named_in_lines["payload"]["body"]["params"]["lines"] = "'kiwi'"
    assert not restatement_names_unsaved([named_in_lines], {}, word="kiwi")[0]


def test_experience_restatement_fails_without_one() -> None:
    passed, detail = restatement_names_unsaved([], {}, word="kiwi")
    assert not passed
    assert "no setup_restate" in detail


def test_experience_allergen_fails_closed() -> None:
    assert not no_item_with_allergen([], {}, allergen="peanuts", db=_order_db("unlisted"))[0]
    assert not no_item_with_allergen([], {}, allergen="peanuts", db=None)[0]
    # A misspelt code could never match a catalog tag, so the case would test nothing.
    passed, detail = no_item_with_allergen([], {}, allergen="peanut", db=_order_db("rice"))
    assert not passed
    assert "not an allergen code" in detail


def test_experience_coverage_entries() -> None:
    coverage = case_order.load_coverage()
    goals = coverage["goals"]["experience"]
    assert set(goals) == {"XG2", "XG3", "XG7", "XG8", "XG11", "XG12"}
    for goal, cases in goals.items():
        assert cases, f"{goal} maps to no case"
        for case_id in cases:
            assert case_order.source_design(case_id, coverage["prefixes"]) == "experience"
    for case_id in ("task-setup-by-chat", "task-held-release"):
        assert case_order.source_design(case_id, coverage["prefixes"]) == "experience"
