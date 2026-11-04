from pathlib import Path
from typing import Any

import pytest
import yaml

from evals import d4, run

D4_YAML = Path(d4.__file__).parent / "d4.yaml"
HEADER_CELLS = 9


def row(node: str = "respond_to_counter", agent_id: str = "grocery:mock-us", **cols: Any) -> dict:
    """A recorded `model.choose` row; `source` and `reason` go in the payload."""
    payload = {
        "node": node,
        "choice": "accept",
        "allowed": "one of accept, reject",
        "model_pick": "accept",
        "source": cols.pop("source", "model"),
        "reason": cols.pop("reason", None),
    }
    base = {"tokens_in": 10, "tokens_out": 5, "model": "muse-a", "latency_ms": 100}
    return {"kind": "decision", "agent_id": agent_id, "payload": payload, **base, **cols}


def fallback(reason: str, **cols: Any) -> dict:
    """A rule-default row whose model call gave no answer: no tokens, no latency."""
    empty = {"tokens_in": None, "tokens_out": None, "latency_ms": None}
    return row(source="rule_default", reason=reason, **{**empty, **cols})


def attempt(rows: list[dict], category: str = "decision", verdict: str = "pass") -> dict:
    return {"category": category, "verdict": verdict, "decisions": rows}


def cells(report: str, node: str) -> list[str]:
    line = next(ln for ln in report.splitlines() if ln.startswith(f"| {node} |"))
    found = [c.strip() for c in line.strip("|").split("|")]
    assert len(found) == HEADER_CELLS
    return found


def test_per_point_metrics() -> None:
    rows = [
        row(latency_ms=100, tokens_in=10, tokens_out=5),
        row(latency_ms=200, tokens_in=20, tokens_out=5),
        row(
            latency_ms=300,
            tokens_in=30,
            tokens_out=5,
            source="rule_default",
            reason="out_of_bounds",
        ),
        row(
            latency_ms=400,
            tokens_in=40,
            tokens_out=5,
            source="rule_default",
            reason="model_invalid_output",
        ),
        fallback("timeout"),
    ]
    found = cells(d4.report([attempt(rows)]), "respond_to_counter")
    # Sample, model used, out of bounds, invalid output, timeout, p50, p95, mean tokens.
    assert found[1:] == ["5", "40%", "20%", "20%", "20%", "200", "400", "30.0"]


def test_percentiles_are_nearest_rank_over_many_rows() -> None:
    rows = [row(latency_ms=ms) for ms in range(100, 0, -1)]
    found = cells(d4.report([attempt(rows)]), "respond_to_counter")
    assert found[6:8] == ["50", "95"]


def test_points_are_kept_apart_and_attempts_add_up() -> None:
    report = d4.report(
        [
            attempt([row("choose_window", "delivery:mock-us")]),
            attempt([row("choose_window", "delivery:mock-us"), row("rank_substitutes")]),
        ]
    )
    assert cells(report, "choose_window")[1] == "2"
    assert cells(report, "rank_substitutes")[1] == "1"


def test_point_with_no_rows_reports_sample_zero() -> None:
    report = d4.report([attempt([row("choose_window", "delivery:mock-us")])])
    assert cells(report, "respond_to_counter")[1:] == ["0"] + ["n/a"] * 7
    assert cells(d4.report([]), "choose_window")[1] == "0"


def test_only_timeouts_has_no_latency_or_tokens() -> None:
    found = cells(d4.report([attempt([fallback("timeout")])]), "respond_to_counter")
    assert found[1:] == ["1", "0%", "0%", "0%", "100%", "n/a", "n/a", "n/a"]


def test_pass_rates_count_only_pass_and_fail() -> None:
    report = d4.report(
        [
            attempt([], "decision", "pass"),
            attempt([], "decision", "fail"),
            attempt([], "decision", "skipped"),
            attempt([], "task", "fail"),
        ]
    )
    assert "Decision pass rate: 50%" in report
    # The D4 list holds no safety case, so a D4 run has no safety rate.
    assert "Safety pass rate: n/a" in report
    assert "Safety pass rate: 100%" in d4.report([attempt([], "safety", "pass")])


def test_report_names_the_model_and_picks_none() -> None:
    report = d4.report([attempt([row(model="muse-a"), fallback("timeout", model="muse-a")])])
    assert "Business model: muse-a\n" in report
    assert "Business model: n/a\n" in d4.report([])
    assert "recommend" not in report.lower()
    assert "winner" not in report.lower()


def test_decision_rows_keep_only_business_choices() -> None:
    choice = row()
    trace = [
        choice,
        # A household `model.complete` or code row, and a seller's code decision: no `source`.
        {"kind": "decision", "agent_id": "household:us-1", "payload": {"node": "negotiate"}},
        {"kind": "decision", "agent_id": "grocery:mock-us", "payload": {"node": "accept"}},
        # A household row with a `source`, and a row of another kind.
        row(agent_id="household:us-1"),
        {**row(), "kind": "tool_call"},
    ]
    assert d4.decision_rows(trace) == [choice]


def test_case_list_ids_exist_and_are_real_capable() -> None:
    listed = yaml.safe_load(D4_YAML.read_text())
    assert len(listed) == len(set(listed)) == 6
    cases = {c["id"]: c for c in run.load_cases(set(run.load_thresholds()["categories"]))}
    assert run.load_case_ids(D4_YAML, set(cases)) == listed
    for case_id in listed:
        assert "real" in run.model_modes(cases[case_id]), case_id


def test_cases_file_narrows_all() -> None:
    # `infra/eval-nightly.sh` runs `--all --mode real --trigger d4 --cases-file evals/d4.yaml`.
    cases = run.load_cases(set(run.load_thresholds()["categories"]))
    listed = yaml.safe_load(D4_YAML.read_text())
    selected = run.select(cases, all_cases=True, case_ids=listed, mode="real")
    assert sorted(c["id"] for c in selected) == sorted(listed)
    assert len(run.select(cases, all_cases=True, mode="real")) > len(listed)


def test_run_case_keeps_the_decision_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    choice = row()
    trace = [choice, {"kind": "outcome", "payload": {"reason": None}}]
    monkeypatch.setattr(run, "reset_and_seed_mocks", lambda case: None)
    monkeypatch.setitem(run.SCENARIOS, "hello", lambda case: run.uuid.uuid4())
    monkeypatch.setattr(run, "read_trace", lambda dsn, run_id: trace)
    case = {
        "id": "c",
        "category": "task",
        "run": "hello",
        "expect": [{"check": "outcome_recorded"}],
    }
    assert run.run_case(case, 1, "dsn", timeout=1).decisions == [choice]


def _main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> str:
    """`run.main` on one written case, returning what it printed."""
    case = {
        "id": "decision-a",
        "category": "decision",
        "tags": [],
        "run": "hello",
        "expect": [{"check": "outcome_recorded"}],
    }
    (tmp_path / "decision").mkdir()
    (tmp_path / "decision" / "decision-a.yaml").write_text(yaml.safe_dump(case))
    load = run.load_cases
    printed: list[str] = []

    def attempt_run(c: dict, repeat: int, dsn: str, timeout: float, mode: str = "stub") -> Any:
        return run.RunResult(c["id"], c["category"], repeat, passed=True, decisions=[row()])

    class NoHistory(run.History):
        def _do(self, what: str, sql: str, params: tuple[Any, ...]) -> None:
            return None

    monkeypatch.setattr(run, "load_cases", lambda categories: load(categories, tmp_path))
    monkeypatch.setattr(run, "write_results", lambda s, r, md: Path("r.json"))
    monkeypatch.setattr(run, "run_case", attempt_run)
    monkeypatch.setattr(run, "take_lock", lambda dsn, **kw: object())
    monkeypatch.setattr(run, "release_lock", lambda conn: None)
    monkeypatch.setattr(run, "History", NoHistory)
    monkeypatch.setattr(
        run, "print", lambda *a, **k: printed.append(" ".join(map(str, a))), raising=False
    )
    monkeypatch.setenv("GIT_SHA", "abc")
    assert run.main(argv) == 0
    return "\n".join(printed)


def test_summary_holds_the_report_only_for_d4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    printed = _main(tmp_path, monkeypatch, ["--all", "--trigger", "d4"])
    summary = printed.split(run.SUMMARY_BEGIN)[1].split(run.SUMMARY_END)[0]
    assert "## D4 report" in summary
    assert cells(summary, "respond_to_counter")[1] == "1"
    assert "Decision pass rate: 100%" in summary


def test_other_triggers_have_no_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "D4 report" not in _main(tmp_path, monkeypatch, ["--all", "--trigger", "nightly"])
