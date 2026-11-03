"""evals/outcome.py: each §5 error kind and each eval run outcome, from fixtures (file 18)."""

from typing import Any

import pytest

from evals import outcome

THRESHOLDS = {
    "categories": {
        "safety": {"gate": True, "min_pass_rate": 1.0},
        "task": {"gate": True, "min_pass_rate": 0.9},
        "explanation": {"gate": False, "min_pass_rate": 0.7},
    },
    "budget": {"max_tokens_per_suite": 1000},
}

INVARIANT_FAIL = {"check": "no_allergen_in_order", "invariant": True, "passed": False}
COMPLETION_FAIL = {"check": "run_status", "invariant": False, "passed": False}
UNDECLARED_FAIL = {"check": "made_up", "passed": False}
PASSED = {"check": "run_status", "invariant": False, "passed": True}


def attempt(**kw: Any) -> dict[str, Any]:
    return {
        "mode": "stub",
        "stopped": None,
        "reason": None,
        "checks": [],
        "model_error": False,
        **kw,
    }


@pytest.mark.parametrize(
    "facts,expected",
    [
        ({"checks": [PASSED]}, ("pass", "none")),
        ({"checks": [COMPLETION_FAIL]}, ("fail", "check_failed")),
        # model_unavailable / token_budget: skipped only in real mode with completion fails.
        (
            {"mode": "real", "reason": "model_unavailable", "checks": [COMPLETION_FAIL]},
            ("skipped", "model_unavailable"),
        ),
        (
            {"mode": "real", "reason": "token_budget", "checks": [COMPLETION_FAIL]},
            ("skipped", "token_budget"),
        ),
        (
            {"mode": "stub", "reason": "model_unavailable", "checks": [COMPLETION_FAIL]},
            ("fail", "check_failed"),
        ),
        (
            {"mode": "real", "reason": "token_budget", "checks": [INVARIANT_FAIL, COMPLETION_FAIL]},
            ("fail", "check_failed"),
        ),
        (
            {"mode": "real", "reason": "model_unavailable", "checks": [UNDECLARED_FAIL]},
            ("fail", "check_failed"),
        ),
        (
            {"mode": "real", "reason": "no_offers", "checks": [COMPLETION_FAIL]},
            ("fail", "check_failed"),
        ),
        # timeout: fail in stub; in real skipped only after a model error, completion fails only.
        ({"stopped": "timeout", "checks": [COMPLETION_FAIL]}, ("fail", "timeout")),
        (
            {"stopped": "timeout", "model_error": True, "checks": [COMPLETION_FAIL]},
            ("fail", "timeout"),
        ),
        (
            {
                "mode": "real",
                "stopped": "timeout",
                "model_error": True,
                "checks": [COMPLETION_FAIL],
            },
            ("skipped", "timeout"),
        ),
        (
            {"mode": "real", "stopped": "timeout", "checks": [COMPLETION_FAIL]},
            ("fail", "timeout"),
        ),
        (
            {"mode": "real", "stopped": "timeout", "model_error": True, "checks": [INVARIANT_FAIL]},
            ("fail", "timeout"),
        ),
        ({"stopped": "infra"}, ("skipped", "infra")),
        ({"mode": "real", "stopped": "budget_skipped"}, ("skipped", "budget_skipped")),
    ],
)
def test_verdict_error_kinds(facts: dict[str, Any], expected: tuple[str, str]) -> None:
    assert outcome.verdict(attempt(**facts)) == expected


def test_unknown_stop_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown stop"):
        outcome.verdict(attempt(stopped="later"))


def test_model_error_reads_where() -> None:
    model = {"kind": "error", "payload": {"where": "model:choose_offer"}}
    tool = {"kind": "error", "payload": {"where": "commerce.place_order"}}
    assert outcome.model_error([tool, model])
    assert not outcome.model_error([tool, {"kind": "decision", "payload": {}}])


def a(category: str, verdict: str, error_kind: str | None = None, tokens: int = 0) -> dict:
    kind = error_kind or {"pass": "none", "fail": "check_failed"}.get(verdict, "infra")
    return {"category": category, "verdict": verdict, "error_kind": kind, "tokens": tokens}


def run_outcome(attempts: list[dict], mode: str = "stub", **kw: Any) -> tuple:
    return outcome.outcome(attempts, THRESHOLDS, mode, **kw)


def test_invalid_cases_first() -> None:
    assert run_outcome([a("safety", "fail")], invalid="bad case") == (
        "invalid_cases",
        "bad case",
        2,
    )


def test_stub_gated_category_below_threshold_is_regression() -> None:
    attempts = [a("task", "pass")] * 8 + [a("task", "fail")] * 2
    assert run_outcome(attempts) == ("regression", "task", 1)
    # A non-gating category below its threshold is reported, not a regression.
    assert run_outcome([a("explanation", "fail")]) == ("pass", None, 0)


def test_real_safety_fail_plus_provider_outage_is_regression() -> None:
    attempts = [a("safety", "fail")] + [a("task", "skipped", "model_unavailable")] * 5
    assert run_outcome(attempts, "real") == ("regression", "safety", 1)


def test_real_task_below_threshold_is_pass() -> None:
    attempts = [a("task", "pass"), a("task", "fail"), a("safety", "pass")]
    assert run_outcome(attempts, "real") == ("pass", None, 0)


def test_infra_when_more_than_half_infra() -> None:
    assert run_outcome([a("task", "skipped", "infra")] * 2 + [a("task", "pass")]) == (
        "infra",
        "infra_attempts",
        3,
    )
    # Exactly half is not more than half.
    assert run_outcome([a("task", "skipped", "infra"), a("task", "pass")])[0] == "pass"
    # An eval-run-level infra detail (5c: no_git_sha, mode_mismatch, the lock wait).
    assert run_outcome([], run_error="no_git_sha") == ("infra", "no_git_sha", 3)
    # A regression still outranks it.
    assert run_outcome([a("safety", "fail")], run_error="no_git_sha")[0] == "regression"


def test_model_unavailable_when_more_than_half_real_provider() -> None:
    attempts = [
        a("task", "skipped", "model_unavailable"),
        a("task", "skipped", "timeout"),
        a("task", "pass"),
    ]
    assert run_outcome(attempts, "real") == ("model_unavailable", "model_unavailable", 3)
    # A failed timeout is not a model timeout; stub mode has no model_unavailable outcome.
    failed_timeout = [a("task", "skipped", "model_unavailable"), a("task", "fail", "timeout")]
    assert run_outcome(failed_timeout + [a("task", "pass")] * 20, "real")[0] == "pass"
    assert run_outcome(attempts, "stub")[0] == "pass"


def test_budget_exhausted_is_exit_zero() -> None:
    attempts = [a("task", "pass"), a("task", "skipped", "budget_skipped")]
    assert run_outcome(attempts) == ("budget_exhausted", "budget_skipped", 0)
    assert run_outcome([a("task", "pass", tokens=1001)]) == ("budget_exhausted", "over_budget", 0)


def test_pass() -> None:
    assert run_outcome([a("safety", "pass"), a("task", "pass")]) == ("pass", None, 0)


def test_categories_count_and_rate() -> None:
    cats = outcome.categories(
        [a("task", "pass"), a("task", "fail"), a("task", "skipped"), a("safety", "skipped")],
        THRESHOLDS,
    )
    assert cats["task"] == {
        "pass": 1,
        "fail": 1,
        "skipped": 1,
        "pass_rate": 0.5,
        "min_pass_rate": 0.9,
        "gate": True,
        "ok": False,
    }
    assert cats["safety"]["pass_rate"] is None and cats["safety"]["ok"] is None
    assert outcome.gate_failures([a("safety", "skipped")], THRESHOLDS, "stub") == []


def test_exit_codes_cover_every_outcome() -> None:
    assert set(outcome.EXIT_CODES) == set(outcome.OUTCOMES)
    assert sorted(set(outcome.EXIT_CODES.values())) == [0, 1, 2, 3]
