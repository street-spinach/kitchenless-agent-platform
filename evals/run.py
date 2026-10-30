"""Eval runner (ENGINEERING_PROCESS.md section 12.6).

For each selected case and repeat: reset and seed mocks, start the scenario, wait for an
`outcome` trace event, then run the case's checks over the trace. Writes
evals/results/<timestamp>.json and .md and exits non-zero if a gated category falls below its
threshold or the suite exceeds its token budget.

Config (env): ORCHESTRATOR_URL, DATABASE_URL, EVAL_TIMEOUT_SECONDS.
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from evals.checks import CHECKS
from services.agents.common.trace import read_trace

EVALS_DIR = Path(__file__).parent
CASES_DIR = EVALS_DIR / "cases"
RESULTS_DIR = EVALS_DIR / "results"
THRESHOLDS_PATH = EVALS_DIR / "thresholds.yaml"

# Defaults match infra/.env.example and the ports published on 127.0.0.1, for runs on the host.
DEFAULT_ORCHESTRATOR_URL = "http://localhost:8000"
DEFAULT_DATABASE_URL = "postgresql://spinach:change-me-local@localhost:5432/spinach"
REQUIRED_CASE_FIELDS = ("id", "category", "tags", "run", "expect")


class CaseError(ValueError):
    pass


@dataclass
class RunResult:
    case_id: str
    category: str
    repeat: int
    passed: bool
    run_id: str | None = None
    seconds: float = 0.0
    tokens: int = 0
    checks: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


# --- cases and thresholds -------------------------------------------------------------------


def load_thresholds(path: Path = THRESHOLDS_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def validate_case(case: Any, source: str, categories: set[str]) -> dict[str, Any]:
    if not isinstance(case, dict):
        raise CaseError(f"{source}: case must be a mapping")
    missing = [f for f in REQUIRED_CASE_FIELDS if f not in case]
    if missing:
        raise CaseError(f"{source}: missing fields {missing}")
    if case["category"] not in categories:
        raise CaseError(f"{source}: unknown category {case['category']!r}")
    if Path(source).parent.name != case["category"]:
        raise CaseError(f"{source}: category {case['category']!r} does not match its directory")
    if case["run"] not in SCENARIOS:
        raise CaseError(f"{source}: unknown scenario {case['run']!r}")
    if not isinstance(case["expect"], list) or not case["expect"]:
        raise CaseError(f"{source}: expect must be a non-empty list")
    for item in case["expect"]:
        if not isinstance(item, dict) or item.get("check") not in CHECKS:
            raise CaseError(f"{source}: unknown check {item!r}")
    return case


def load_cases(categories: set[str], cases_dir: Path = CASES_DIR) -> list[dict[str, Any]]:
    cases, seen = [], set()
    for path in sorted(cases_dir.glob("*/*.yaml")):
        case = validate_case(yaml.safe_load(path.read_text()), str(path), categories)
        if case["id"] in seen:
            raise CaseError(f"{path}: duplicate case id {case['id']!r}")
        seen.add(case["id"])
        cases.append(case)
    return cases


def select(
    cases: list[dict[str, Any]],
    *,
    tag: str | None = None,
    category: str | None = None,
    case_id: str | None = None,
    all_cases: bool = False,
) -> list[dict[str, Any]]:
    if all_cases:
        return list(cases)
    return [
        c
        for c in cases
        if (tag is None or tag in c["tags"])
        and (category is None or c["category"] == category)
        and (case_id is None or c["id"] == case_id)
    ]


# --- scenarios ------------------------------------------------------------------------------


def reset_and_seed_mocks(case: dict[str, Any]) -> None:
    # The mock control panel has no reset or seed API yet (docs/designs/adapters.md will define
    # it). Cases with setup or conditions can't be honoured until then, so refuse them.
    if case.get("setup") or case.get("conditions"):
        raise CaseError(f"{case['id']}: setup and conditions need the control panel API")


def start_hello(case: dict[str, Any]) -> uuid.UUID:
    url = os.environ.get("ORCHESTRATOR_URL", DEFAULT_ORCHESTRATOR_URL) + "/hello/run"
    body = json.dumps(case.get("input", {})).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return uuid.UUID(json.loads(resp.read())["run_id"])


SCENARIOS: dict[str, Callable[[dict[str, Any]], uuid.UUID]] = {"hello": start_hello}


def wait_for_outcome(dsn: str, run_id: uuid.UUID, timeout: float) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while True:
        trace = read_trace(dsn, run_id)
        if any(row["kind"] == "outcome" for row in trace):
            return trace
        if time.monotonic() >= deadline:
            raise TimeoutError(f"no outcome event for run {run_id} after {timeout:.0f}s")
        time.sleep(0.5)


def run_checks(trace: list[dict[str, Any]], case: dict[str, Any]) -> list[dict[str, Any]]:
    results = []
    for item in case["expect"]:
        try:
            passed, detail = CHECKS[item["check"]](trace, case, **(item.get("args") or {}))
        except Exception as err:
            passed, detail = False, f"check raised {type(err).__name__}: {err}"
        results.append({"check": item["check"], "passed": passed, "detail": detail})
    return results


def run_case(case: dict[str, Any], repeat: int, dsn: str, timeout: float) -> RunResult:
    result = RunResult(case["id"], case["category"], repeat, passed=False)
    started = time.monotonic()
    try:
        reset_and_seed_mocks(case)
        run_id = SCENARIOS[case["run"]](case)
        result.run_id = str(run_id)
        trace = wait_for_outcome(dsn, run_id, timeout)
        result.tokens = sum((r["tokens_in"] or 0) + (r["tokens_out"] or 0) for r in trace)
        result.checks = run_checks(trace, case)
        result.passed = all(c["passed"] for c in result.checks)
    except Exception as err:
        result.error = f"{type(err).__name__}: {err}"
    result.seconds = round(time.monotonic() - started, 2)
    return result


# --- reporting ------------------------------------------------------------------------------


def summarize(results: list[RunResult], thresholds: dict[str, Any]) -> dict[str, Any]:
    categories = {}
    for name in sorted({r.category for r in results}):
        runs = [r for r in results if r.category == name]
        rule = thresholds["categories"][name]
        rate = sum(r.passed for r in runs) / len(runs)
        categories[name] = {
            "runs": len(runs),
            "passed": sum(r.passed for r in runs),
            "pass_rate": round(rate, 4),
            "min_pass_rate": rule["min_pass_rate"],
            "gate": rule["gate"],
            "ok": rate >= rule["min_pass_rate"],
        }
    budget = thresholds["budget"]
    tokens = sum(r.tokens for r in results)
    slow = sorted({r.case_id for r in results if r.seconds > budget["warn_seconds_per_scenario"]})
    gate_failures = [n for n, c in categories.items() if c["gate"] and not c["ok"]]
    over_budget = tokens > budget["max_tokens_per_suite"]
    return {
        "categories": categories,
        "tokens": tokens,
        "max_tokens_per_suite": budget["max_tokens_per_suite"],
        "over_budget": over_budget,
        "slow_cases": slow,
        "gate_failures": gate_failures,
        "passed": not gate_failures and not over_budget,
    }


def to_markdown(summary: dict[str, Any], results: list[RunResult]) -> str:
    lines = [
        f"# Eval results: {'PASS' if summary['passed'] else 'FAIL'}",
        "",
        "| Category | Passed | Pass rate | Threshold | Gate | Status |",
        "|---|---|---|---|---|---|",
    ]
    for name, c in summary["categories"].items():
        status = "ok" if c["ok"] else ("FAIL" if c["gate"] else "below (not gating)")
        lines.append(
            f"| {name} | {c['passed']}/{c['runs']} | {c['pass_rate']:.0%} | "
            f"{c['min_pass_rate']:.0%} | {'yes' if c['gate'] else 'no'} | {status} |"
        )
    lines += ["", f"Tokens: {summary['tokens']} / {summary['max_tokens_per_suite']}"]
    if summary["slow_cases"]:
        lines.append(f"Slow cases (warn): {', '.join(summary['slow_cases'])}")
    failed = [r for r in results if not r.passed]
    if failed:
        lines += ["", "## Failed runs", ""]
        for r in failed:
            why = r.error or "; ".join(c["detail"] for c in r.checks if not c["passed"])
            lines.append(f"- `{r.case_id}` repeat {r.repeat} (run {r.run_id}): {why}")
    return "\n".join(lines) + "\n"


def write_results(summary: dict[str, Any], results: list[RunResult], markdown: str) -> Path:
    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    payload = {"summary": summary, "runs": [asdict(r) for r in results]}
    (RESULTS_DIR / f"{stamp}.json").write_text(json.dumps(payload, indent=2))
    (RESULTS_DIR / f"{stamp}.md").write_text(markdown)
    return RESULTS_DIR / f"{stamp}.json"


# --- entry point ----------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m evals.run")
    parser.add_argument("--tag")
    parser.add_argument("--category")
    parser.add_argument("--case", dest="case_id")
    parser.add_argument("--all", dest="all_cases", action="store_true")
    args = parser.parse_args(argv)
    if not (args.all_cases or args.tag or args.category or args.case_id):
        parser.error("choose cases with --tag, --category, --case or --all")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    thresholds = load_thresholds()
    try:
        cases = load_cases(set(thresholds["categories"]))
    except CaseError as err:
        print(f"Invalid eval case: {err}", file=sys.stderr)
        return 2
    selected = select(
        cases,
        tag=args.tag,
        category=args.category,
        case_id=args.case_id,
        all_cases=args.all_cases,
    )
    if not selected:
        print("No eval cases matched the selection.", file=sys.stderr)
        return 2

    dsn = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    timeout = float(os.environ.get("EVAL_TIMEOUT_SECONDS", "300"))
    results = [
        run_case(case, repeat, dsn, timeout)
        for case in selected
        for repeat in range(1, thresholds["categories"][case["category"]]["repeats"] + 1)
    ]
    summary = summarize(results, thresholds)
    markdown = to_markdown(summary, results)
    path = write_results(summary, results, markdown)
    print(markdown)
    print(f"Results: {path}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
