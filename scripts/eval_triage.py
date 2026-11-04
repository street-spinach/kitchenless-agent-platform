"""Map a night's `EVAL_STATUS` line to one issue action (evals.md §8).

    python3 scripts/eval_triage.py --log evals.log --issues issues.json

`--issues` is the output of `gh issue list --state open --json number,title,labels`. The CLI
prints `{action: none|create|comment, number, title, labels, add_labels, body}` as JSON and
`nightly-evals.yml` carries it out. Each class has one fixed title and one open issue.

This never recomputes an outcome: `evals/outcome.py` decides it and this only maps it. A log
with no usable status line is outcome `infra`, detail `no_status_line`. Closing an issue is
the owner's. Standard library only: the workflow runs this without installing the project.
"""

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

STATUS_PREFIX = "EVAL_STATUS "
SUMMARY_BEGIN = "<!-- EVAL_SUMMARY_BEGIN -->"
SUMMARY_END = "<!-- EVAL_SUMMARY_END -->"
LOG_TAIL_LINES = 40
NEEDS_HUMAN = "needs-human"
NO_STATUS_LINE = "no_status_line"

NO_ISSUE = {"pass", "budget_exhausted", "skipped"}
# outcome: (issue title, labels on create, labels added when that issue is already open)
CLASSES: dict[str, tuple[str, list[str], list[str]]] = {
    "regression": ("Nightly evals: regression", ["eval-regression"], [NEEDS_HUMAN]),
    "model_unavailable": ("Nightly evals: model unavailable", ["eval-infra"], [NEEDS_HUMAN]),
    "infra": ("Nightly evals: infrastructure", ["eval-infra"], [NEEDS_HUMAN]),
    "invalid_cases": ("Nightly evals: invalid cases", ["eval-infra", NEEDS_HUMAN], []),
}
# The safety row of the runner's Markdown table: `| safety | <pass> | <fail> | ...`.
SAFETY_ROW = re.compile(r"^\| safety \| \d+ \| (\d+) \|", re.MULTILINE)


@dataclass(frozen=True)
class Action:
    action: str = "none"
    number: int | None = None
    title: str | None = None
    labels: list[str] = field(default_factory=list)
    add_labels: list[str] = field(default_factory=list)
    body: str | None = None


def parse_status(log_text: str) -> dict[str, Any] | None:
    """The last `EVAL_STATUS` line's object. None when there is none, or when that line is
    damaged or names an outcome this script doesn't know: an earlier line is never used."""
    for line in reversed(log_text.splitlines()):
        if line.startswith(STATUS_PREFIX):
            try:
                status = json.loads(line[len(STATUS_PREFIX) :])
            except ValueError:
                return None
            known = isinstance(status, dict) and (
                status.get("outcome") in NO_ISSUE or status.get("outcome") in CLASSES
            )
            return status if known else None
    return None


def parse_summary(log_text: str) -> str:
    """The text between the last pair of summary markers, or empty."""
    start = log_text.rfind(SUMMARY_BEGIN)
    end = log_text.rfind(SUMMARY_END)
    if start == -1 or end < start:
        return ""
    return log_text[start + len(SUMMARY_BEGIN) : end].strip()


def safety_failed(status: dict[str, Any], summary: str) -> bool:
    """A real or stub safety case failed. The status line only says so for real mode, and
    not every safety case id starts with `safety-`, so stub mode reads the summary's safety
    row. With no row to read, it counts as failed: a person looks."""
    if status.get("real_safety_failed"):
        return True
    row = SAFETY_ROW.search(summary)
    return row is None or int(row.group(1)) > 0


def _compare(status: dict[str, Any], repo: str) -> str:
    last_green, sha = status.get("last_green_sha"), status.get("git_sha")
    if not last_green or not sha:
        return "no green nightly yet"
    return f"https://github.com/{repo}/compare/{last_green}...{sha}"


def _body(status: dict[str, Any], summary: str, repo: str) -> str:
    failed = status.get("failed_cases") or []
    lines = [
        f"Eval run: {status.get('eval_run_id')}",
        f"Trigger: {status.get('trigger')}",
        f"Mode: {status.get('mode')}",
        f"Detail: {status.get('detail')}",
        f"Failing cases: {', '.join(failed) if failed else 'none'}",
    ]
    if status["outcome"] == "regression":
        lines.append(f"Compare: {_compare(status, repo)}")
    return "\n".join(lines) + (f"\n\n{summary}" if summary else "")


def _log_tail(log_text: str) -> str:
    tail = "\n".join(log_text.splitlines()[-LOG_TAIL_LINES:])
    return f"Detail: {NO_STATUS_LINE}\n\n```\n{tail}\n```"


def decide(log_text: str, open_issues: list[dict[str, Any]], repo: str) -> Action:
    """The one action for this log (evals.md §8)."""
    status = parse_status(log_text)
    outcome = status["outcome"] if status else "infra"
    if outcome in NO_ISSUE:
        return Action()

    summary = parse_summary(log_text)
    body = _body(status, summary, repo) if status else _log_tail(log_text)
    title, labels, add_labels = CLASSES[outcome]
    for issue in open_issues:
        if issue.get("title") == title:
            return Action("comment", issue["number"], title, [], list(add_labels), body)
    labels = list(labels)
    if outcome == "regression" and safety_failed(status, summary):
        labels.append(NEEDS_HUMAN)
    return Action("create", None, title, labels, [], body)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--issues", type=Path, required=True)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    args = parser.parse_args()
    log_text = args.log.read_text(errors="replace") if args.log.exists() else ""
    action = decide(log_text, json.loads(args.issues.read_text()), args.repo)
    print(json.dumps(asdict(action)))


if __name__ == "__main__":
    main()
