import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

from evals import run
from evals.run import CaseError, RunResult

THRESHOLDS = {
    "categories": {
        "safety": {"gate": True, "min_pass_rate": 1.0, "repeats": 3},
        "task": {"gate": True, "min_pass_rate": 0.9, "repeats": 2},
        "explanation": {"gate": False, "min_pass_rate": 0.7, "repeats": 1},
    },
    "budget": {"max_tokens_per_suite": 100, "warn_seconds_per_scenario": 120},
}
CATEGORIES = set(THRESHOLDS["categories"])


def case(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "c1",
        "category": "task",
        "tags": ["fast"],
        "run": "hello",
        "expect": [{"check": "outcome_recorded"}],
    }
    return {**base, **overrides}


def write_case(root: Path, category: str, data: dict[str, Any]) -> None:
    (root / category).mkdir(parents=True, exist_ok=True)
    (root / category / f"{data['id']}.yaml").write_text(yaml.safe_dump(data))


def result(category: str, passed: bool, tokens: int = 0, seconds: float = 1.0) -> RunResult:
    return RunResult("c", category, 1, passed, tokens=tokens, seconds=seconds)


def test_repo_cases_and_thresholds_are_valid() -> None:
    thresholds = run.load_thresholds()
    cases = run.load_cases(set(thresholds["categories"]))
    fast = run.select(cases, tag="fast")
    assert {c["category"] for c in fast} >= {"safety", "task"}
    assert all(c in fast for c in cases if c["category"] == "safety")


@pytest.mark.parametrize(
    "bad,message",
    [
        ({"run": None}, "missing fields"),
        ({"category": "vibes"}, "unknown category"),
        ({"run": "weekly_grocery_order"}, "unknown scenario"),
        ({"expect": [{"check": "made_up"}]}, "unknown check"),
        ({"expect": []}, "non-empty"),
    ],
)
def test_invalid_cases_are_rejected(bad: dict[str, Any], message: str) -> None:
    data = case(**bad)
    if bad == {"run": None}:
        del data["run"]
    with pytest.raises(CaseError, match=message):
        run.validate_case(data, f"cases/{data['category']}/c1.yaml", CATEGORIES)


def test_category_must_match_directory(tmp_path: Path) -> None:
    write_case(tmp_path, "safety", case(category="task"))
    with pytest.raises(CaseError, match="does not match"):
        run.load_cases(CATEGORIES, tmp_path)


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    write_case(tmp_path, "task", case())
    write_case(tmp_path, "safety", case(category="safety"))
    with pytest.raises(CaseError, match="duplicate"):
        run.load_cases(CATEGORIES, tmp_path)


def test_select_filters() -> None:
    cases = [case(id="a"), case(id="b", tags=[], category="safety")]
    assert [c["id"] for c in run.select(cases, tag="fast")] == ["a"]
    assert [c["id"] for c in run.select(cases, category="safety")] == ["b"]
    assert [c["id"] for c in run.select(cases, case_id="b")] == ["b"]
    assert [c["id"] for c in run.select(cases, all_cases=True)] == ["a", "b"]


def test_gated_category_below_threshold_fails() -> None:
    summary = run.summarize(
        [result("safety", True), result("safety", True), result("safety", False)], THRESHOLDS
    )
    assert summary["categories"]["safety"]["pass_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert summary["gate_failures"] == ["safety"]
    assert not summary["passed"]


def test_non_gating_category_does_not_fail_suite() -> None:
    summary = run.summarize([result("task", True), result("explanation", False)], THRESHOLDS)
    assert summary["categories"]["explanation"]["ok"] is False
    assert summary["passed"]


def test_token_budget_fails_suite() -> None:
    summary = run.summarize([result("task", True, tokens=101)], THRESHOLDS)
    assert summary["over_budget"]
    assert not summary["passed"]


def test_slow_cases_warn_only() -> None:
    summary = run.summarize([result("task", True, seconds=121)], THRESHOLDS)
    assert summary["slow_cases"] == ["c"]
    assert summary["passed"]


def test_run_case_with_passing_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = uuid.uuid4()
    monkeypatch.setitem(run.SCENARIOS, "hello", lambda c: run_id)
    trace = [
        {"seq": 1, "kind": "message", "payload": {}, "tokens_in": 5, "tokens_out": None},
        {
            "seq": 2,
            "kind": "outcome",
            "payload": {"status": "ok"},
            "tokens_in": None,
            "tokens_out": 3,
        },
    ]
    monkeypatch.setattr(run, "read_trace", lambda dsn, rid: trace)
    res = run.run_case(case(), 1, "dsn", timeout=1)
    assert res.passed
    assert res.run_id == str(run_id)
    assert res.tokens == 8


def test_run_case_times_out_without_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(run.SCENARIOS, "hello", lambda c: uuid.uuid4())
    monkeypatch.setattr(run, "read_trace", lambda dsn, rid: [])
    res = run.run_case(case(), 1, "dsn", timeout=0)
    assert not res.passed
    assert res.error and res.error.startswith("TimeoutError")


def test_run_case_refuses_setup_without_control_panel() -> None:
    res = run.run_case(case(setup={"household": "family_of_three"}), 1, "dsn", timeout=0)
    assert not res.passed
    assert "control panel" in (res.error or "")


def test_check_exception_counts_as_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(trace: Any, case: Any) -> tuple[bool, str]:
        raise KeyError("payload")

    monkeypatch.setitem(run.CHECKS, "outcome_recorded", boom)
    [check] = run.run_checks([], case())
    assert not check["passed"]
    assert "KeyError" in check["detail"]


def test_requires_a_selection() -> None:
    with pytest.raises(SystemExit):
        run.parse_args([])
