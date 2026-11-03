"""Attempt verdicts and the eval run outcome (evals.md §5). Pure: no I/O, no clock.

`evals/run.py` builds each attempt, asks `verdict` for its verdict and error kind, then asks
`outcome` for the eval run's one outcome. `scripts/eval_triage.py` never recomputes an
outcome; it reads the runner's.

Results JSON v1 (`evals/results/<stamp>.json`). This table is the contract.

| Field | Type | Meaning |
|---|---|---|
| `schema` | int | Always `1` |
| `eval_run_id` | str (uuid) | `ids.new_eval_run_id()`, one per eval run |
| `trigger` | str or null | `nightly`, `manual`, `d4`, `rehearsal`; null for the PR check |
| `git_sha` | str or null | `GIT_SHA` from the environment |
| `model_mode` | str | `stub` or `real` |
| `models` | {people, business} | Model names from `agents` `/health`; null until it reports them |
| `started_at`, `finished_at` | str | ISO 8601 UTC |
| `outcome` | str | One of `OUTCOMES` |
| `outcome_detail` | str or null | Why: the failing categories, or the infra or budget detail |
| `exit_code` | int | `EXIT_CODES[outcome]` |
| `selection` | [str] | Case ids, in run order |
| `categories` | {name: {...}} | Per category, as below |
| `tokens` | int | Tokens used by every attempt |
| `max_tokens_per_suite` | int | From `thresholds.yaml` |
| `reason_counts` | {reason: n} | Outcome reasons of the attempts' agent runs |
| `error_kind_counts` | {kind: n} | One count per attempt, by `ERROR_KINDS` |
| `history_write_errors` | int | Failed history writes (only with `--trigger`) |
| `attempts` | [attempt] | One per case and repeat, as below |

Each category:

| Field | Type | Meaning |
|---|---|---|
| `pass`, `fail`, `skipped` | int | Attempts by verdict |
| `pass_rate` | float or null | `pass / (pass + fail)`; null when every attempt was skipped |
| `min_pass_rate`, `gate` | float, bool | From `thresholds.yaml` |
| `ok` | bool or null | `pass_rate >= min_pass_rate`; null when there is no pass rate |

Each attempt:

| Field | Type | Meaning |
|---|---|---|
| `case_id`, `repeat` | str, int | |
| `agent_run_ids` | [str] | The agent runs it started; empty when none started |
| `verdict` | str | `pass`, `fail` or `skipped` |
| `error_kind` | str | One of `ERROR_KINDS` |
| `reason` | str or null | The agent run's `outcome` reason |
| `seconds` | float | Wall time of the attempt |
| `tokens` | int | `tokens_in + tokens_out` over the run's trace rows |
| `checks` | [{check, args, invariant, passed, detail}] | One per `expect` entry |
| `models_used` | [str] | Distinct `model` values of the run's trace rows |
"""

from collections.abc import Iterable, Mapping
from typing import Any

PASS, FAIL, SKIPPED = "pass", "fail", "skipped"
VERDICTS = (PASS, FAIL, SKIPPED)

NONE = "none"
CHECK_FAILED = "check_failed"
MODEL_UNAVAILABLE = "model_unavailable"
TOKEN_BUDGET = "token_budget"
TIMEOUT = "timeout"
INFRA = "infra"
BUDGET_SKIPPED = "budget_skipped"
ERROR_KINDS = (NONE, CHECK_FAILED, MODEL_UNAVAILABLE, TOKEN_BUDGET, TIMEOUT, INFRA, BUDGET_SKIPPED)
# What stopped an attempt before its checks could judge a finished run (`stopped`).
STOPS = (TIMEOUT, INFRA, BUDGET_SKIPPED)
# Outcome reasons that make a real-mode attempt `skipped` when only completion checks failed.
PROVIDER_REASONS = (MODEL_UNAVAILABLE, TOKEN_BUDGET)
# An `error` trace row's `where` names a model call with this prefix (model.py).
MODEL_WHERE_PREFIX = "model:"

INVALID_CASES = "invalid_cases"
REGRESSION = "regression"
BUDGET_EXHAUSTED = "budget_exhausted"
OUTCOMES = (INVALID_CASES, REGRESSION, INFRA, MODEL_UNAVAILABLE, BUDGET_EXHAUSTED, PASS)
EXIT_CODES = {
    INVALID_CASES: 2,
    REGRESSION: 1,
    INFRA: 3,
    MODEL_UNAVAILABLE: 3,
    BUDGET_EXHAUSTED: 0,
    PASS: 0,
}
SAFETY = "safety"
# `outcome_detail` when the suite's tokens passed `max_tokens_per_suite`.
OVER_BUDGET = "over_budget"


def is_invariant(check: Mapping[str, Any]) -> bool:
    """A check result with no `invariant` declaration counts as invariant (evals.md §5)."""
    return check.get("invariant", True) is not False


def model_error(trace: Iterable[Mapping[str, Any]]) -> bool:
    """Whether an `error` trace row's `where` names a model call (architecture §10)."""
    return any(
        row.get("kind") == "error"
        and str((row.get("payload") or {}).get("where") or "").startswith(MODEL_WHERE_PREFIX)
        for row in trace
    )


def verdict(attempt: Mapping[str, Any]) -> tuple[str, str]:
    """`(verdict, error_kind)` for one attempt (evals.md §5 table).

    `attempt` keys: `mode` (`stub` or `real`), `stopped` (null or one of `STOPS`), `reason`
    (the outcome reason or null), `checks` (results with `passed` and `invariant`) and
    `model_error` (see `model_error`). A failed invariant check is always `fail`.
    """
    stopped = attempt.get("stopped")
    if stopped == BUDGET_SKIPPED:
        return SKIPPED, BUDGET_SKIPPED
    if stopped == INFRA:
        return SKIPPED, INFRA
    if stopped is not None and stopped != TIMEOUT:
        raise ValueError(f"unknown stop {stopped!r}")
    real = attempt.get("mode") == "real"
    failed = [c for c in attempt.get("checks") or () if not c.get("passed")]
    invariant_failed = any(is_invariant(c) for c in failed)
    if stopped == TIMEOUT:
        if real and attempt.get("model_error") and not invariant_failed:
            return SKIPPED, TIMEOUT
        return FAIL, TIMEOUT
    if not failed:
        return PASS, NONE
    reason = attempt.get("reason")
    if real and not invariant_failed and reason in PROVIDER_REASONS:
        return SKIPPED, reason
    return FAIL, CHECK_FAILED


def categories(
    attempts: Iterable[Mapping[str, Any]], thresholds: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """Per category of the attempts: counts, pass rate and whether it meets its threshold.
    Skipped attempts count in neither side of the rate; an all-skipped category has none."""
    by_name: dict[str, list[str]] = {}
    for a in attempts:
        by_name.setdefault(a["category"], []).append(a["verdict"])
    result = {}
    for name in sorted(by_name):
        verdicts = by_name[name]
        rule = thresholds["categories"][name]
        passed, failed = verdicts.count(PASS), verdicts.count(FAIL)
        rate = round(passed / (passed + failed), 4) if passed + failed else None
        result[name] = {
            "pass": passed,
            "fail": failed,
            "skipped": verdicts.count(SKIPPED),
            "pass_rate": rate,
            "min_pass_rate": rule["min_pass_rate"],
            "gate": rule["gate"],
            "ok": None if rate is None else rate >= rule["min_pass_rate"],
        }
    return result


def gate_failures(
    attempts: Iterable[Mapping[str, Any]], thresholds: Mapping[str, Any], mode: str
) -> list[str]:
    """The categories that make a regression (evals.md §5, "What gates"). In stub mode, each
    gated category below its threshold; in real mode, only safety, on any failed attempt."""
    attempts = list(attempts)
    if mode == "real":
        failed = any(a["category"] == SAFETY and a["verdict"] == FAIL for a in attempts)
        return [SAFETY] if failed else []
    return [
        name
        for name, c in categories(attempts, thresholds).items()
        if c["gate"] and c["ok"] is False
    ]


def outcome(
    attempts: Iterable[Mapping[str, Any]],
    thresholds: Mapping[str, Any],
    mode: str,
    *,
    invalid: str | None = None,
    run_error: str | None = None,
) -> tuple[str, str | None, int]:
    """`(outcome, detail, exit_code)` for an eval run: the first rule of evals.md §5 that
    applies. `attempts` carry `category`, `verdict`, `error_kind` and `tokens`. `invalid` is
    the validation error, if any; `run_error` an eval-run-level infra detail (`no_git_sha`,
    `mode_mismatch`, the lock wait)."""
    attempts = list(attempts)

    def done(name: str, detail: str | None) -> tuple[str, str | None, int]:
        return name, detail, EXIT_CODES[name]

    if invalid is not None:
        return done(INVALID_CASES, invalid)
    failures = gate_failures(attempts, thresholds, mode)
    if failures:
        return done(REGRESSION, ",".join(failures))
    if run_error is not None:
        return done(INFRA, run_error)
    kinds = [a["error_kind"] for a in attempts]
    if kinds.count(INFRA) * 2 > len(kinds):
        return done(INFRA, "infra_attempts")
    if mode == "real":
        provider = [
            a
            for a in attempts
            if a["error_kind"] == MODEL_UNAVAILABLE
            or (a["error_kind"] == TIMEOUT and a["verdict"] == SKIPPED)
        ]
        if len(provider) * 2 > len(attempts):
            return done(MODEL_UNAVAILABLE, MODEL_UNAVAILABLE)
    if BUDGET_SKIPPED in kinds:
        return done(BUDGET_EXHAUSTED, BUDGET_SKIPPED)
    tokens = sum(int(a.get("tokens") or 0) for a in attempts)
    if tokens > thresholds["budget"]["max_tokens_per_suite"]:
        return done(BUDGET_EXHAUSTED, OVER_BUDGET)
    return done(PASS, None)
