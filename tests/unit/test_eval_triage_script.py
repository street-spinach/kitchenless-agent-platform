"""scripts/eval_triage.py and the workflow that carries out its action (evals §8, §10, file 27)."""

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from evals import run
from scripts import eval_triage
from scripts.eval_triage import Action, decide

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github/workflows/nightly-evals.yml"
REPO_NAME = "street-spinach/kitchenless-agent-platform"

REGRESSION = "Nightly evals: regression"
MODEL_DOWN = "Nightly evals: model unavailable"
INFRA = "Nightly evals: infrastructure"
INVALID = "Nightly evals: invalid cases"


def summary_table(safety_fail: int = 0) -> str:
    """The real runner's Markdown, so a change to its table breaks this test."""
    row = {
        "pass": 3,
        "fail": safety_fail,
        "skipped": 0,
        "pass_rate": 1.0,
        "min_pass_rate": 1.0,
        "gate": True,
        "ok": safety_fail == 0,
    }
    summary = {
        "outcome": "regression",
        "outcome_detail": None,
        "categories": {"safety": row, "task": row | {"fail": 2}},
        "gate_failures": [],
        "tokens": 0,
        "max_tokens_per_suite": 10,
        "slow_cases": [],
    }
    return run.to_markdown(summary, [])


def log(outcome: str, summary: str = "the summary", **fields: Any) -> str:
    status = {
        "outcome": outcome,
        "detail": None,
        "exit_code": 0,
        "eval_run_id": "run-1",
        "mode": "stub",
        "trigger": "nightly",
        "git_sha": "new",
        "last_green_sha": None,
        "failed_cases": [],
        "real_safety_failed": False,
    } | fields
    return (
        f"noise\n{eval_triage.SUMMARY_BEGIN}\n{summary}\n{eval_triage.SUMMARY_END}\n"
        f"EVAL_STATUS {json.dumps(status)}\nmake: *** [eval-nightly] Error 1\n"
    )


def open_issue(title: str, number: int = 7) -> dict[str, Any]:
    return {"number": number, "title": title, "labels": []}


@pytest.mark.parametrize("outcome", ["pass", "budget_exhausted", "skipped"])
def test_quiet_outcomes_file_nothing(outcome: str) -> None:
    assert decide(log(outcome), [open_issue(REGRESSION)], REPO_NAME) == Action()


@pytest.mark.parametrize(
    ("outcome", "title", "labels"),
    [
        ("regression", REGRESSION, ["eval-regression"]),
        ("model_unavailable", MODEL_DOWN, ["eval-infra"]),
        ("infra", INFRA, ["eval-infra"]),
        ("invalid_cases", INVALID, ["eval-infra", "needs-human"]),
    ],
)
def test_each_class_creates_its_issue(outcome: str, title: str, labels: list[str]) -> None:
    action = decide(log(outcome, summary_table()), [], REPO_NAME)
    assert (action.action, action.title, action.labels) == ("create", title, labels)
    assert action.number is None


@pytest.mark.parametrize(
    ("outcome", "title", "add_labels"),
    [
        ("regression", REGRESSION, ["needs-human"]),
        ("model_unavailable", MODEL_DOWN, ["needs-human"]),
        ("infra", INFRA, ["needs-human"]),
        ("invalid_cases", INVALID, []),
    ],
)
def test_open_issue_of_the_class_gets_a_comment(
    outcome: str, title: str, add_labels: list[str]
) -> None:
    issues = [open_issue("Something else", 3), open_issue(title, 7)]
    action = decide(log(outcome), issues, REPO_NAME)
    assert (action.action, action.number, action.add_labels) == ("comment", 7, add_labels)


def test_open_issue_of_another_class_does_not_count() -> None:
    assert decide(log("infra"), [open_issue(REGRESSION)], REPO_NAME).action == "create"


def test_no_status_line_is_infra_with_the_log_tail() -> None:
    text = "\n".join(f"line {i}" for i in range(100))
    action = decide(text, [], REPO_NAME)
    assert (action.action, action.title, action.labels) == ("create", INFRA, ["eval-infra"])
    assert "no_status_line" in action.body
    assert "line 60" in action.body and "line 99" in action.body
    assert "line 59" not in action.body


def test_empty_log_is_infra() -> None:
    assert decide("", [], REPO_NAME).title == INFRA


@pytest.mark.parametrize(
    "line", ['EVAL_STATUS {"outcome": ', 'EVAL_STATUS {"outcome": "new"}', "EVAL_STATUS []"]
)
def test_unusable_last_status_line_is_infra(line: str) -> None:
    """An earlier `pass` never stands in for a damaged last line."""
    assert decide(log("pass") + line + "\n", [], REPO_NAME).title == INFRA


def test_last_status_line_wins() -> None:
    assert decide(log("regression") + log("pass"), [], REPO_NAME).action == "none"


def test_exit_code_is_never_used_to_recompute() -> None:
    assert decide(log("pass", exit_code=1), [], REPO_NAME).action == "none"


def test_real_safety_failure_needs_a_human_at_once() -> None:
    text = log("regression", summary_table(), mode="real", real_safety_failed=True)
    assert decide(text, [], REPO_NAME).labels == ["eval-regression", "needs-human"]


def test_stub_safety_failure_needs_a_human_at_once() -> None:
    """Read from the safety row: `hello-no-profile-fields-to-business` is a safety case."""
    text = log("regression", summary_table(safety_fail=1))
    assert decide(text, [], REPO_NAME).labels == ["eval-regression", "needs-human"]


def test_other_category_failure_stays_without_needs_human() -> None:
    text = log("regression", summary_table(), failed_cases=["task-a"])
    assert decide(text, [], REPO_NAME).labels == ["eval-regression"]


def test_regression_with_no_summary_row_needs_a_human() -> None:
    assert decide(log("regression", "cut off"), [], REPO_NAME).labels == [
        "eval-regression",
        "needs-human",
    ]


def test_body_holds_the_run_and_the_summary() -> None:
    text = log("infra", "the table", detail="mode_switch_failed", failed_cases=["task-a"])
    body = decide(text, [], REPO_NAME).body
    for part in ("run-1", "nightly", "stub", "mode_switch_failed", "task-a", "the table"):
        assert part in body
    assert eval_triage.SUMMARY_BEGIN not in body


def test_compare_link_uses_last_green_sha() -> None:
    body = decide(log("regression", last_green_sha="old"), [], REPO_NAME).body
    assert f"https://github.com/{REPO_NAME}/compare/old...new" in body


def test_compare_says_no_green_nightly() -> None:
    assert "no green nightly yet" in decide(log("regression"), [], REPO_NAME).body


def test_only_regression_gets_a_compare_line() -> None:
    assert "Compare" not in decide(log("infra", last_green_sha="old"), [], REPO_NAME).body


def test_markers_match_the_runner() -> None:
    assert eval_triage.SUMMARY_BEGIN == run.SUMMARY_BEGIN
    assert eval_triage.SUMMARY_END == run.SUMMARY_END
    assert eval_triage.STATUS_PREFIX == run.STATUS_PREFIX


def test_cli_prints_the_action(tmp_path: Path) -> None:
    (tmp_path / "evals.log").write_text(log("infra"))
    (tmp_path / "issues.json").write_text(json.dumps([open_issue(INFRA, 9)]))
    done = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts/eval_triage.py"),
            "--log",
            str(tmp_path / "evals.log"),
            "--issues",
            str(tmp_path / "issues.json"),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    action = json.loads(done.stdout)
    assert set(action) == {"action", "number", "title", "labels", "add_labels", "body"}
    assert (action["action"], action["number"]) == ("comment", 9)


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text())


def _steps() -> dict[str, dict[str, Any]]:
    return {s.get("name", s.get("uses")): s for s in _workflow()["jobs"]["evals"]["steps"]}


def test_workflow_triggers_and_group_are_kept() -> None:
    workflow = _workflow()
    on = workflow[True]  # YAML 1.1 reads the `on` key as a boolean
    assert on["schedule"] == [{"cron": "30 20 * * *"}]
    assert on["workflow_dispatch"]["inputs"]["business_model"]["required"] is False
    assert workflow["concurrency"]["group"] == "deploy"


def test_workflow_ignores_the_ssh_exit_code() -> None:
    step = _steps()["Run the nightly evals on the VM"]
    assert "make eval-nightly BUSINESS_MODEL=" in step["run"]
    assert step["run"].rstrip().endswith("tee evals.log || true")
    assert step["continue-on-error"] is True


def test_a_stopped_vm_reaches_triage() -> None:
    assert "known_hosts || true" in _steps()["Configure SSH"]["run"]


def test_workflow_never_puts_the_input_in_a_script() -> None:
    """`${{ inputs... }}` in `run` would be shell injection; it only goes through `env`."""
    for step in _steps().values():
        assert "inputs." not in step.get("run", "")


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("", True),
        ("meta-models/muse-spark-open", True),
        ("a'; rm -rf /; '", False),
        ("$(id)", False),
        ("two words", False),
    ],
)
def test_business_model_input_check(value: str, ok: bool) -> None:
    script = _steps()["Check the business model input"]["run"]
    done = subprocess.run(
        ["bash", "-c", script],
        env={"BUSINESS_MODEL": value, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (done.returncode == 0) is ok


def test_workflow_steps_are_in_design_order() -> None:
    names = list(_steps())
    order = ["Run the nightly evals on the VM", "List open issues", "Triage", "Job summary"]
    assert [n for n in names if n in order] == order
    assert "File regression issue" not in names
    assert re.search(r"--limit 200 > issues\.json", _steps()["List open issues"]["run"])


def test_every_workflow_script_parses() -> None:
    for name, step in _steps().items():
        if "run" in step:
            done = subprocess.run(
                ["bash", "-n"], input=step["run"], capture_output=True, text=True, timeout=30
            )
            assert done.returncode == 0, (name, done.stderr)


def test_setup_labels_creates_eval_infra() -> None:
    text = (REPO / "scripts/setup_labels.sh").read_text()
    assert re.search(
        r'^create eval-infra\s+\w{6} "Nightly eval infrastructure or provider failure"$',
        text,
        re.MULTILINE,
    )
