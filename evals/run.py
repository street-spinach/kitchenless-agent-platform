"""Eval runner (ENGINEERING_PROCESS.md section 12.6).

For each selected case and repeat: reset and seed mocks, start the scenario, drive the open
run (`approve_as`, `inject`, `shortfall`, `a2a_steps`; architecture item 65, agent-protocol
P38), wait for an `outcome` trace event, then run the case's checks over the trace (and, for
checks that take it, a read-only `db` snapshot of the run's rows). Each attempt's verdict and
the eval run's outcome come from `evals/outcome.py` (evals.md §5). Writes
evals/results/<timestamp>.json (JSON v1, the field table in `evals/outcome.py`) and .md, prints
the Markdown summary between
`SUMMARY_BEGIN` and `SUMMARY_END`, and ends standard output with one `EVAL_STATUS` line. Exit
codes: 0 pass or budget exhausted, 1 regression, 2 invalid cases, 3 infra or model unavailable.

Cases follow the format of evals.md §4 and are validated before any attempt (exit 2). Flags
(evals.md §5): `--tag`, `--category`, `--case`, `--cases-file`, `--all`, `--repeats`,
`--mode stub|real` (default `stub`; only cases whose `model_modes` include it run, each its
category's `repeats[mode]` times), `--trigger` and `--list` (every case's id, category, tags,
model modes and source design from `evals/coverage.yaml`; runs nothing).

Order (evals.md §7, `evals/select.py`): stub mode runs cases in id order; real mode runs the
cases least recently attempted in real mode with their current `case_hash` first, read from
`eval_results`. Each case runs all its repeats before the next.

Budget, deadline, lock and history (evals.md §5, §7; build step 5c):

*   Before each attempt the runner checks that the remaining tokens (`max_tokens_per_suite`
    minus the tokens used) are at least `RUN_TOKEN_CEILING` and the remaining time (until
    `EVAL_DEADLINE_SECONDS` after the runner started) is at least `EVAL_TIMEOUT_SECONDS`.
    Otherwise the attempt is `budget_skipped`.
*   With `--trigger`: `GIT_SHA` must be set (else outcome `infra`, `no_git_sha`); the runner
    takes the eval lock (`pg_try_advisory_lock(hashtext('evals:eval'))` every 5 s for up to
    10 minutes, else `infra`, `lock_timeout`) and holds it on its own connection for the
    whole eval run; it writes the `eval_runs` row, one `eval_results` row per attempt and the
    outcome at the end (`History`). A failed write never changes a verdict.
*   With `--mode real`: `$AGENTS_URL/health` must report `model_mode: real` (else `infra`,
    `mode_mismatch`).

Config (env): ORCHESTRATOR_URL, DATABASE_URL, AGENTS_URL, A2A_DEMO_TOKEN, EVAL_TIMEOUT_SECONDS,
EVAL_DEADLINE_SECONDS, RUN_TOKEN_CEILING, GIT_SHA.
"""

import argparse
import asyncio
import hashlib
import inspect
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import psycopg
import yaml
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from evals import outcome
from evals import select as case_order
from evals.checks import CHECKS
from services.agents.a2a.server import SENDER as A2A_SENDER
from services.agents.a2a.server import VERSION_HEADER as A2A_VERSION_HEADER
from services.agents.common.trace import read_trace
from services.common import directory, ids, orders
from services.common.protocol import render_summary
from services.common.protocol.a2a import (
    A2A_PROTOCOL_VERSION,
    ROLE_USER,
    TASK_STATE_SUBMITTED,
    TASK_STATE_WORKING,
)
from services.common.trace import TraceStore
from services.mocks.control import SETUP_KEYS
from services.safety.scope import Unscoped, unscoped

EVALS_DIR = Path(__file__).parent
CASES_DIR = EVALS_DIR / "cases"
RESULTS_DIR = EVALS_DIR / "results"
THRESHOLDS_PATH = EVALS_DIR / "thresholds.yaml"

# Defaults match infra/.env.example and the ports published on 127.0.0.1, for runs on the host.
DEFAULT_ORCHESTRATOR_URL = "http://localhost:8000"
DEFAULT_DATABASE_URL = "postgresql://spinach:change-me-local@localhost:5432/spinach"
REQUIRED_CASE_FIELDS = ("id", "category", "tags", "run", "expect")
# evals.md §4: the runner rejects a case file with any other field.
CASE_FIELDS = frozenset(
    {
        *REQUIRED_CASE_FIELDS,
        "model_modes",
        "household",
        "households",
        "input",
        "setup",
        "conditions",
        "overrides",
        "approve_as",
        "inject",
        "shortfall",
        "a2a_steps",
        "revoke",
    }
)
MODEL_MODES = ("stub", "real")
TRIGGERS = ("nightly", "manual", "d4", "rehearsal")
# evals.md Conventions, "Stub-only case": a real model never takes these paths.
STUB_OVERRIDES = frozenset(
    {
        "STUB_FAIL_NODE",
        "STUB_OUT_OF_BOUNDS",
        "STUB_DUPLICATE_MESSAGE",
        "STUB_ACCEPT_DELAY_ROUNDS",
        "STUB_SHORTFALL_UNSAFE_SUBSTITUTE",
        "STUB_COUNTER_REJECT",
    }
)
MEMBER_MESSAGE = "member_message"

EVAL_ENV = "eval"
# evals.md §4: the default eval household, and the pair's default households.
HOUSEHOLD = "us-1-eval"
PAIR = "weekly_grocery_order_pair"
PAIR_HOUSEHOLDS = ("us-1-eval", "us-2-eval")
# `POST /runs` needs the member's message text; a case with no `input.text` sends these
# (architecture §12 case shape; the stub classifier reads "weekly"/"shop" and "dinner").
DEFAULT_TEXT = {
    "weekly_grocery_order": "Do the weekly shop",
    PAIR: "Do the weekly shop",
    "busy_night_dinner": "Get us dinner tonight",
}
# Shortfall reply buttons are `sf:<shortfall_id>:<line>:<sku | drop>` (agent-protocol §10).
SHORTFALL_BUTTON = "sf"
# Agent-protocol P38: the `a2a_demo` scenario, against the seller of CUJ 7. The endpoint fills
# every other field of the request (§11 step 4).
A2A_DEMO = "a2a_demo"
A2A_AGENT = "grocery:mock-us"
A2A_REQUEST = {
    "items": [
        {"item": "milk", "qty": 1, "unit": "each"},
        {"item": "eggs", "qty": 1, "unit": "each"},
    ],
    "constraints": {"allergens": [], "diets": []},
}
A2A_OPS = ("send", "get", "cancel")
A2A_STEP_FIELDS = frozenset({"op", "type", "message", "error", "total_percent"})

# evals.md §5, "Standard output".
SUMMARY_BEGIN = "<!-- EVAL_SUMMARY_BEGIN -->"
SUMMARY_END = "<!-- EVAL_SUMMARY_END -->"
STATUS_PREFIX = "EVAL_STATUS "
RESULTS_SCHEMA = 1
# A runner error after the agent run started is a failed invariant check, so it can never
# become a skip (see the PR's Decisions).
RUNNER_ERROR_CHECK = "runner_error"

# evals.md §5 and §7 (build step 5c).
DEFAULT_AGENTS_URL = "http://localhost:8003"
# Matches infra/.env.example, for runs on the host, as DEFAULT_DATABASE_URL does.
DEFAULT_A2A_TOKEN = "change-me-local"
DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_DEADLINE_SECONDS = 3000.0
LOCK_KEY = "evals:eval"
LOCK_POLL_SECONDS = 5.0
LOCK_WAIT_SECONDS = 600.0
# `outcome_detail` of an eval run that ended `infra` before any attempt.
NO_GIT_SHA = "no_git_sha"
MODE_MISMATCH = "mode_mismatch"
LOCK_TIMEOUT = "lock_timeout"

log = logging.getLogger("evals.run")


class CaseError(ValueError):
    pass


@dataclass
class RunResult:
    """One attempt. `verdict` and `error_kind` come from `outcome.verdict`; `passed` is
    `verdict == pass`. Built with only `passed`, it is a plain pass or a check failure."""

    case_id: str
    category: str
    repeat: int
    passed: bool
    run_id: str | None = None
    seconds: float = 0.0
    tokens: int = 0
    reason: str | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    mode: str = "stub"
    stopped: str | None = None
    model_error: bool = False
    models_used: list[str] = field(default_factory=list)
    verdict: str | None = None
    error_kind: str | None = None

    def __post_init__(self) -> None:
        if self.verdict is None:
            self.verdict = outcome.PASS if self.passed else outcome.FAIL
            self.error_kind = outcome.NONE if self.passed else outcome.CHECK_FAILED

    def judge(self) -> None:
        """Set `verdict`, `error_kind` and `passed` from the attempt's facts."""
        self.verdict, self.error_kind = outcome.verdict(
            {
                "mode": self.mode,
                "stopped": self.stopped,
                "reason": self.reason,
                "checks": self.checks,
                "model_error": self.model_error,
            }
        )
        self.passed = self.verdict == outcome.PASS


# --- cases and thresholds -------------------------------------------------------------------


def load_thresholds(path: Path = THRESHOLDS_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def model_modes(case: dict[str, Any]) -> list[str]:
    """The case's model modes; `[stub, real]` when it names none (evals.md §4)."""
    return list(case.get("model_modes") or MODEL_MODES)


def check_args(check: Callable[..., Any]) -> set[str]:
    """The argument names a case may give `check`: its parameters after `trace` and `case`,
    except `db`, which the runner supplies."""
    names = list(inspect.signature(check).parameters)[2:]
    return {n for n in names if n != "db"}


def _validate_modes(case: dict[str, Any], source: str) -> None:
    modes = case.get("model_modes", list(MODEL_MODES))
    if (
        not isinstance(modes, list)
        or not modes
        or len(set(modes)) != len(modes)
        or not set(modes) <= set(MODEL_MODES)
    ):
        raise CaseError(f"{source}: model_modes must be [stub], [real] or [stub, real]")
    overrides = case.get("overrides") or {}
    if not isinstance(overrides, dict):
        raise CaseError(f"{source}: overrides must be a mapping")
    stub_only = sorted(STUB_OVERRIDES & set(overrides))
    if stub_only and modes != ["stub"]:
        raise CaseError(f"{source}: stub overrides {stub_only} need model_modes: [stub]")
    if case["category"] == "safety" and "stub" in modes and "fast" not in case["tags"]:
        raise CaseError(f"{source}: a stub-capable safety case must carry the fast tag")


def _validate_inputs(case: dict[str, Any], source: str) -> None:
    run_name = case["run"]
    # `hello` keeps its `input` as the body of `POST /hello/run` (see the PR's Decisions).
    if run_name == MEMBER_MESSAGE:
        given = case.get("input")
        if not (
            isinstance(given, dict)
            and set(given) == {"member", "text"}
            and all(isinstance(given[k], str) and given[k] for k in given)
        ):
            raise CaseError(f"{source}: member_message needs input: {{member, text}}")
    elif "input" in case and run_name != "hello":
        raise CaseError(f"{source}: input is only for member_message")
    if "households" in case:
        households = case["households"]
        if run_name != PAIR:
            raise CaseError(f"{source}: households is only for {PAIR}")
        if (
            not isinstance(households, list)
            or len(households) != 2
            or len(set(households)) != 2
            or not all(isinstance(h, str) and h for h in households)
        ):
            raise CaseError(f"{source}: households must be two different household ids")
    if "household" in case and not (isinstance(case["household"], str) and case["household"]):
        raise CaseError(f"{source}: household must be a household id")


def _validate_setup(case: dict[str, Any], source: str) -> None:
    setup = case.get("setup") or {}
    if not isinstance(setup, dict):
        raise CaseError(f"{source}: setup must be a mapping")
    for name, value in setup.items():
        key = SETUP_KEYS.get(name)
        if key is None:
            raise CaseError(f"{source}: unknown setup key {name!r}")
        # bool is an int subclass; a setup value of true is never an amount (as control.py).
        if (isinstance(value, bool) and key.type is not bool) or not isinstance(value, key.type):
            raise CaseError(f"{source}: setup key {name!r} must be {key.type.__name__}")


def _validate_a2a(case: dict[str, Any], source: str) -> None:
    """`a2a_steps` (agent-protocol P38): `{op: send, type, message?, error?, total_percent?}`,
    `{op: get}` or `{op: cancel}`, opening with the `quote_request` that starts the run."""
    if case["run"] != A2A_DEMO:
        if "a2a_steps" in case:
            raise CaseError(f"{source}: a2a_steps is only for {A2A_DEMO}")
        return
    if "overrides" in case:
        raise CaseError(f"{source}: {A2A_DEMO} takes no overrides: the endpoint starts the run")
    steps = case.get("a2a_steps")
    if not isinstance(steps, list) or not steps:
        raise CaseError(f"{source}: {A2A_DEMO} needs a non-empty a2a_steps list")
    for step in steps:
        if (
            not isinstance(step, dict)
            or step.get("op") not in A2A_OPS
            or set(step) - A2A_STEP_FIELDS
            or (step["op"] == "send") != isinstance(step.get("type"), str)
        ):
            raise CaseError(f"{source}: bad a2a step {step!r}")
    if steps[0].get("type") != "quote_request" or "error" in steps[0]:
        raise CaseError(f"{source}: a2a_steps must open with send quote_request")


def _validate_expect(case: dict[str, Any], source: str) -> None:
    if not isinstance(case["expect"], list) or not case["expect"]:
        raise CaseError(f"{source}: expect must be a non-empty list")
    for item in case["expect"]:
        if not isinstance(item, dict) or item.get("check") not in CHECKS:
            raise CaseError(f"{source}: unknown check {item!r}")
        if set(item) - {"check", "args"}:
            raise CaseError(f"{source}: expect entries take only check and args, got {item!r}")
        args = item.get("args") or {}
        if not isinstance(args, dict):
            raise CaseError(f"{source}: args of {item['check']} must be a mapping")
        unknown = sorted(set(args) - check_args(CHECKS[item["check"]]))
        if unknown:
            raise CaseError(f"{source}: unknown args {unknown} for check {item['check']}")


def validate_case(case: Any, source: str, categories: set[str]) -> dict[str, Any]:
    """The case format and validation rules of evals.md §4; any failure is exit 2."""
    if not isinstance(case, dict):
        raise CaseError(f"{source}: case must be a mapping")
    missing = [f for f in REQUIRED_CASE_FIELDS if f not in case]
    if missing:
        raise CaseError(f"{source}: missing fields {missing}")
    unknown = sorted(set(case) - CASE_FIELDS)
    if unknown:
        raise CaseError(f"{source}: unknown fields {unknown}")
    if Path(source).stem != case["id"]:
        raise CaseError(f"{source}: id {case['id']!r} does not match its file name")
    if case["category"] not in categories:
        raise CaseError(f"{source}: unknown category {case['category']!r}")
    if Path(source).parent.name != case["category"]:
        raise CaseError(f"{source}: category {case['category']!r} does not match its directory")
    if not isinstance(case["tags"], list) or not all(isinstance(t, str) for t in case["tags"]):
        raise CaseError(f"{source}: tags must be a list")
    _validate_inputs(case, source)
    if case["run"] not in SCENARIOS:
        raise CaseError(f"{source}: unknown scenario {case['run']!r}")
    _validate_modes(case, source)
    _validate_setup(case, source)
    _validate_a2a(case, source)
    _validate_expect(case, source)
    return case


# sha256 of each loaded case's file, by case id (evals.md §6 `case_hash`), set by `load_cases`.
CASE_HASHES: dict[str, str] = {}


def load_cases(categories: set[str], cases_dir: Path = CASES_DIR) -> list[dict[str, Any]]:
    cases, seen, hashes = [], set(), {}
    for path in sorted(cases_dir.glob("*/*.yaml")):
        raw = path.read_bytes()
        case = validate_case(yaml.safe_load(raw), str(path), categories)
        if case["id"] in seen:
            raise CaseError(f"{path}: duplicate case id {case['id']!r}")
        seen.add(case["id"])
        hashes[case["id"]] = hashlib.sha256(raw).hexdigest()
        cases.append(case)
    CASE_HASHES.update(hashes)
    return cases


def select(
    cases: list[dict[str, Any]],
    *,
    tag: str | None = None,
    category: str | None = None,
    case_id: str | None = None,
    all_cases: bool = False,
    case_ids: list[str] | None = None,
    mode: str | None = None,
) -> list[dict[str, Any]]:
    """The cases chosen by the flags; with `mode`, only cases whose `model_modes` include it
    (evals.md §4)."""
    if mode is not None:
        cases = [c for c in cases if mode in model_modes(c)]
    if all_cases:
        return list(cases)
    return [
        c
        for c in cases
        if (tag is None or tag in c["tags"])
        and (category is None or c["category"] == category)
        and (case_id is None or c["id"] == case_id)
        and (case_ids is None or c["id"] in case_ids)
    ]


def load_case_ids(path: Path, known: set[str]) -> list[str]:
    """`--cases-file`: a YAML list of case ids. Refuses an id that is not a case."""
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as err:
        raise CaseError(f"{path}: cannot read the cases file: {err}") from err
    if not isinstance(data, list) or not data or not all(isinstance(i, str) for i in data):
        raise CaseError(f"{path}: the cases file must be a non-empty list of case ids")
    unknown = sorted(set(data) - known)
    if unknown:
        raise CaseError(f"{path}: unknown case ids {unknown}")
    return data


def case_hash(case: dict[str, Any]) -> str:
    """sha256 of the case's file as `load_cases` read it (evals.md §6), so a changed case
    starts a new trend. A case not loaded from a file hashes its canonical JSON instead."""
    known = CASE_HASHES.get(case["id"])
    if known is not None:
        return known
    return hashlib.sha256(json.dumps(case, sort_keys=True, default=str).encode()).hexdigest()


# --- scenarios ------------------------------------------------------------------------------


def _api_url() -> str:
    return os.environ.get("ORCHESTRATOR_URL", DEFAULT_ORCHESTRATOR_URL)


def post_json(path: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST `body` to `api`; an HTTP error status raises."""
    req = urllib.request.Request(
        _api_url() + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def reset_household(case: dict[str, Any]) -> str:
    """The household the reset applies `setup` to: the case's `household`, or for a pair the
    first of its `households`, whose run the checks read."""
    if case["run"] == PAIR:
        return list(case.get("households") or PAIR_HOUSEHOLDS)[0]
    return case.get("household", HOUSEHOLD)


def reset_and_seed_mocks(case: dict[str, Any]) -> None:
    """`POST /panel/reset {env: eval}` with the case's `setup`, then each of its `conditions`
    through `POST /panel/events` (architecture item 65). `hello` touches no mock or eval
    household state, so its cases are not reset."""
    if case["run"] == "hello":
        if case.get("setup") or case.get("conditions"):
            raise CaseError(f"{case['id']}: hello cases take no setup or conditions")
        return
    reset: dict[str, Any] = {"env": EVAL_ENV, "household": reset_household(case)}
    if case.get("setup"):
        reset["setup"] = case["setup"]
    post_json("/panel/reset", reset)
    for condition in case.get("conditions") or []:
        post_json("/panel/events", {"env": EVAL_ENV, **condition})


def start_hello(case: dict[str, Any]) -> uuid.UUID:
    url = os.environ.get("ORCHESTRATOR_URL", DEFAULT_ORCHESTRATOR_URL) + "/hello/run"
    body = json.dumps(case.get("input", {})).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return uuid.UUID(json.loads(resp.read())["run_id"])


def _input_text(case: dict[str, Any], scenario: str) -> str:
    text = (case.get("input") or {}).get("text")
    return text if isinstance(text, str) and text.strip() else DEFAULT_TEXT[scenario]


def _start_run(scenario: str) -> Callable[[dict[str, Any]], uuid.UUID]:
    """`POST /runs` for one eval household (architecture item 36)."""

    def start(case: dict[str, Any]) -> uuid.UUID:
        body: dict[str, Any] = {
            "scenario": scenario,
            "household_id": case.get("household", HOUSEHOLD),
            "input": _input_text(case, scenario),
        }
        if case.get("overrides"):
            body["overrides"] = case["overrides"]
        return uuid.UUID(post_json("/runs", body)["run_id"])

    return start


def start_pair(case: dict[str, Any]) -> uuid.UUID:
    """`POST /runs` for two eval households. The checks read the first household's run."""
    body: dict[str, Any] = {
        "scenario": PAIR,
        "household_ids": case.get("households", list(PAIR_HOUSEHOLDS)),
        "input": _input_text(case, PAIR),
    }
    if case.get("overrides"):
        body["overrides"] = case["overrides"]
    return uuid.UUID(post_json("/runs", body)["run_id"])


def a2a_rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """One JSON-RPC call to `<AGENTS_URL>/a2a/<A2A_AGENT>`; an HTTP error status raises."""
    base = os.environ.get("AGENTS_URL", DEFAULT_AGENTS_URL).rstrip("/")
    token = os.environ.get("A2A_DEMO_TOKEN") or DEFAULT_A2A_TOKEN
    payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
    req = urllib.request.Request(
        f"{base}/a2a/{A2A_AGENT}",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            A2A_VERSION_HEADER: A2A_PROTOCOL_VERSION,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


class A2AClient:
    """One attempt's A2A client, following the case's `a2a_steps` (agent-protocol P38). Message
    ids are new each attempt, or a repeat would count as a duplicate of the last attempt's
    `SendMessage`; steps with the same `message` name share one id."""

    def __init__(self, steps: list[dict[str, Any]]) -> None:
        self.pending = list(steps)
        self.attempt = uuid.uuid4().hex
        self.task: dict[str, Any] | None = None
        self.last_message_id = ""

    def _offer(self) -> dict[str, Any]:
        artifacts = (self.task or {}).get("artifacts") or []
        if not artifacts:
            raise CaseError("a2a counter_offer: the task has no offer")
        return artifacts[-1]["parts"][0]["data"]["body"]

    def _body(self, step: dict[str, Any]) -> dict[str, Any]:
        if step["type"] == "quote_request":
            return A2A_REQUEST
        if step["type"] == "counter_offer":
            offer = self._offer()
            return {
                "round": offer["round"],
                "lines": offer["lines"],
                "total_minor": offer["total_minor"] * step.get("total_percent", 100) // 100,
                "currency": offer["currency"],
            }
        return {}

    def _message(self, step: dict[str, Any]) -> dict[str, Any]:
        self.last_message_id = f"{self.attempt}:{step.get('message') or uuid.uuid4().hex}"
        message: dict[str, Any] = {
            "messageId": self.last_message_id,
            "role": ROLE_USER,
            "parts": [{"data": {"type": step["type"], "body": self._body(step)}}],
        }
        if step["type"] != "quote_request" and self.task is not None:
            message["taskId"] = self.task["id"]
            message["contextId"] = self.task["contextId"]
        return message

    def _call(
        self, step: dict[str, Any], method: str, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        """The call's result, or `None` for the JSON-RPC error the step expects. Any other
        answer raises: an `accept` that is not refused must fail the attempt."""
        reply = a2a_rpc(method, params)
        error = reply.get("error")
        if (error or {}).get("code") != step.get("error"):
            raise CaseError(f"a2a {method}: got {error!r}, expected error {step.get('error')!r}")
        return None if error else reply["result"]

    def advance(self) -> bool:
        """Run the next step; returns whether every step is done. A `get` repeats until the
        task leaves `SUBMITTED` and `WORKING`."""
        step = self.pending[0]
        if step["op"] == "send":
            result = self._call(step, "SendMessage", {"message": self._message(step)})
            task = result["task"] if result else None
        elif self.task is None:
            raise CaseError(f"a2a {step['op']}: no task yet")
        elif step["op"] == "get":
            task = self._call(step, "GetTask", {"id": self.task["id"]})
        else:
            task = self._call(step, "CancelTask", {"id": self.task["id"]})
        if task is not None:
            self.task = task
        waiting = task is not None and task["status"]["state"] in (
            TASK_STATE_SUBMITTED,
            TASK_STATE_WORKING,
        )
        if step["op"] == "get" and waiting:
            return False
        self.pending.pop(0)
        return not self.pending


# Clients of the attempts started by `start_a2a`, until `run_steps` takes them.
A2A_CLIENTS: dict[uuid.UUID, A2AClient] = {}


def start_a2a(case: dict[str, Any]) -> uuid.UUID:
    """Send the opening `quote_request`, which starts the run; `run_steps` drives the rest.
    The run's id is derived from the request's message id (agent-protocol §11 step 3)."""
    client = A2AClient(case["a2a_steps"])
    client.advance()
    run_id = ids.a2a_run_id(ids.a2a_message_id(A2A_SENDER, client.last_message_id))
    A2A_CLIENTS[run_id] = client
    return run_id


SCENARIOS: dict[str, Callable[[dict[str, Any]], uuid.UUID]] = {
    "hello": start_hello,
    "weekly_grocery_order": _start_run("weekly_grocery_order"),
    PAIR: start_pair,
    "busy_night_dinner": _start_run("busy_night_dinner"),
    A2A_DEMO: start_a2a,
}


# --- runner steps while the run is open (approve_as, inject, shortfall, a2a_steps) -----------

# A step is polled with the run's current trace until it returns True (done).
Step = Callable[[list[dict[str, Any]]], bool]

_MEMBER_PHONE_SQL = """
SELECT phone_e164 FROM channel_identities
WHERE user_id = %s AND channel_mode = 'sim' ORDER BY created_at, phone_e164 LIMIT 1
"""
_RUN_BUTTONS_SQL = """
SELECT b->>'payload' AS payload
FROM sim_outbound, jsonb_array_elements(COALESCE(body->'buttons', '[]'::jsonb)) AS b
WHERE run_id = %s ORDER BY id
"""
_RUN_ORDER_SQL = """
SELECT order_id, state, seller_agent_id FROM orders
WHERE run_id = %s ORDER BY created_at DESC, order_id LIMIT 1
"""


def _db(conn: psycopg.Connection) -> Unscoped:
    """The runner reads a run's rows by `run_id`, across households, with no household
    context of its own (identity-authz §8)."""
    return unscoped(conn, "evals.run")


def member_phone(dsn: str, member_id: str) -> str:
    """The simulated phone of seed member `member_id`; refuses a member without one."""
    with psycopg.connect(dsn) as conn:
        row = _db(conn).execute(_MEMBER_PHONE_SQL, (ids.seed_user_id(member_id),)).fetchone()
    if row is None:
        raise CaseError(f"member {member_id!r} has no simulated phone")
    return row[0]


def run_buttons(dsn: str, run_id: uuid.UUID) -> list[str]:
    """Every button payload in the run's simulated outbox, oldest first."""
    with psycopg.connect(dsn) as conn:
        return [r[0] for r in _db(conn).execute(_RUN_BUTTONS_SQL, (run_id,)).fetchall() if r[0]]


def run_order(dsn: str, run_id: uuid.UUID) -> dict[str, Any] | None:
    with psycopg.connect(dsn) as conn, _db(conn).cursor(row_factory=dict_row) as cur:
        cur.execute(_RUN_ORDER_SQL, (run_id,))
        return cur.fetchone()


def tap(dsn: str, member_id: str, payload: str) -> None:
    """Press a button as `member_id` through the simulated WhatsApp channel."""
    post_json(
        "/internal/sim/whatsapp",
        {
            "phone": member_phone(dsn, member_id),
            "button": payload,
            "external_id": str(uuid.uuid4()),
        },
    )


def approve_step(dsn: str, run_id: uuid.UUID, member_id: str) -> Step:
    """`approve_as`: once the run's simulated outbox shows an approval request, press its
    Approve button as `member_id` (architecture item 65)."""

    def step(trace: list[dict[str, Any]]) -> bool:
        for payload in run_buttons(dsn, run_id):
            parsed = ids.parse_approval_button(payload)
            if parsed is not None and parsed[1] == "approved":
                tap(dsn, member_id, payload)
                return True
        return False

    return step


def shortfall_step(dsn: str, run_id: uuid.UUID, spec: dict[str, Any]) -> Step:
    """`shortfall: {skus, replies: [{as, line, pick}]}`: when the run's order is
    `courier_booked`, raise the shortfall panel event; then, as each reply's line shows in
    the run's outbox, tap `sf:<shortfall_id>:<line>:<pick>` as that member (P38)."""
    pending = list(spec.get("replies") or [])
    raised = False

    def step(trace: list[dict[str, Any]]) -> bool:
        nonlocal raised
        if not raised:
            order = run_order(dsn, run_id)
            if order is None or order["state"] != "courier_booked":
                return False
            post_json(
                "/panel/events",
                {
                    "env": EVAL_ENV,
                    "kind": "shortfall",
                    "target_agent_id": order["seller_agent_id"],
                    "params": {"order_id": str(order["order_id"]), "skus": spec["skus"]},
                },
            )
            raised = True
        shown = {}  # line -> shortfall_id, from the buttons sent so far
        for payload in run_buttons(dsn, run_id):
            parts = payload.split(":", 3)
            if len(parts) == 4 and parts[0] == SHORTFALL_BUTTON and parts[2].isdecimal():
                shown.setdefault(int(parts[2]), parts[1])
        for reply in list(pending):
            line = int(reply["line"])
            if line in shown:
                tap(dsn, reply["as"], f"{SHORTFALL_BUTTON}:{shown[line]}:{line}:{reply['pick']}")
                pending.remove(reply)
        return not pending

    return step


_INJECT_SQL = """
INSERT INTO agent_messages
  (message_id, run_id, conversation_id, causation_id, from_agent, to_agent, to_host, type,
   body, status)
VALUES
  (%(message_id)s, %(run_id)s, %(conversation_id)s, %(causation_id)s, %(from_agent)s,
   %(to_agent)s, %(to_host)s, %(type)s, %(body)s, 'pending')
ON CONFLICT (message_id) DO NOTHING
RETURNING message_id
"""


async def _inject(
    conn: psycopg.AsyncConnection,
    *,
    run_id: uuid.UUID,
    after: dict[str, Any],
    message: dict[str, Any],
    body: dict[str, Any],
) -> bool:
    """Write the forged row and its `message` trace as `outbox.send` does, but without
    `check_static` (agent-protocol P38, eval only). The summary still needs a body that
    fits the type's schema. Returns whether a row was inserted."""
    msg_type, from_agent, to_agent = message["type"], message["from"], message["to"]
    summary = render_summary(msg_type, body)
    message_id = ids.eval_inject_message_id(after["message_id"])
    to_host = (await directory.card(conn, to_agent)).host_process
    sender = await directory.card(conn, from_agent)
    async with conn.transaction():
        cur = await conn.execute(
            _INJECT_SQL,
            {
                "message_id": message_id,
                "run_id": run_id,
                "conversation_id": after.get("conversation_id"),
                "causation_id": after["message_id"],
                "from_agent": from_agent,
                "to_agent": to_agent,
                "to_host": to_host,
                "type": msg_type,
                "body": Jsonb(body),
            },
        )
        if await cur.fetchone() is None:
            return False
        payload = {
            "from": from_agent,
            "to": to_agent,
            "type": msg_type,
            "body": body,
            "summary": summary,
            "causation_id": str(after["message_id"]),
            "injected": True,
        }
        await TraceStore(None).write(  # type: ignore[arg-type]
            run_id,
            from_agent,
            "message",
            payload,
            on_behalf_of=None if sender.owner_ref == "system" else sender.owner_ref,
            conversation_id=after.get("conversation_id"),
            message_id=message_id,
            conn=conn,
        )
        await conn.execute("SELECT pg_notify(%s, '')", (f"a2a_{to_host}",))
    return True


def inject_message(
    dsn: str,
    run_id: uuid.UUID,
    after: dict[str, Any],
    message: dict[str, Any],
    body: dict[str, Any],
) -> bool:
    async def go() -> bool:
        async with await psycopg.AsyncConnection.connect(dsn) as conn:
            return await _inject(conn, run_id=run_id, after=after, message=message, body=body)

    return asyncio.run(go())


def _first_message(trace: list[dict[str, Any]], msg_type: str) -> dict[str, Any] | None:
    return next(
        (r for r in trace if r["kind"] == "message" and r["payload"].get("type") == msg_type),
        None,
    )


def inject_step(dsn: str, run_id: uuid.UUID, spec: dict[str, Any]) -> Step:
    """`inject: {after_type, message: {type, from, to, body_from}}`: after the run's first
    message of `after_type`, write a forged `type` message whose body is a copy of the run's
    first message of `body_from` (default `after_type`)."""
    message = spec["message"]

    def step(trace: list[dict[str, Any]]) -> bool:
        after = _first_message(trace, spec["after_type"])
        source = _first_message(trace, message.get("body_from") or spec["after_type"])
        if after is None or source is None:
            return False
        if after.get("message_id") is None:
            raise CaseError(f"the {spec['after_type']} trace row has no message_id")
        inject_message(dsn, run_id, after, message, dict(source["payload"]["body"]))
        return True

    return step


def a2a_step(client: A2AClient) -> Step:
    """`a2a_steps`: one client step per poll, so `drive` sees an outcome before the next."""

    def step(trace: list[dict[str, Any]]) -> bool:
        return client.advance()

    return step


def run_steps(case: dict[str, Any], dsn: str, run_id: uuid.UUID) -> list[Step]:
    steps = []
    client = A2A_CLIENTS.pop(run_id, None)
    if client is not None and client.pending:
        steps.append(a2a_step(client))
    if case.get("approve_as"):
        steps.append(approve_step(dsn, run_id, case["approve_as"]))
    if case.get("inject"):
        steps.append(inject_step(dsn, run_id, case["inject"]))
    if case.get("shortfall"):
        steps.append(shortfall_step(dsn, run_id, case["shortfall"]))
    return steps


def drive(dsn: str, run_id: uuid.UUID, steps: list[Step], deadline: float) -> None:
    """Poll the open run's steps until each is done, the run has an outcome, or `deadline`."""
    while steps and time.monotonic() < deadline:
        trace = read_trace(dsn, run_id)
        if any(row["kind"] == "outcome" for row in trace):
            return
        steps = [step for step in steps if not step(trace)]
        if steps:
            time.sleep(0.5)


# --- the read-only db snapshot (agent-protocol P38) -----------------------------------------

_ORDERS = "SELECT order_id FROM orders WHERE run_id = %(run)s"
_EVAL_HH = "SELECT household_id FROM households WHERE env = 'eval'"
_EVAL_USERS = f"SELECT user_id FROM household_members WHERE household_id IN ({_EVAL_HH})"
SNAPSHOT_SQL: dict[str, str] = {
    "agent_messages": "SELECT * FROM agent_messages WHERE run_id = %(run)s",
    "checkpoints": "SELECT * FROM checkpoints WHERE thread_id LIKE %(thread)s",
    "checkpoint_blobs": "SELECT * FROM checkpoint_blobs WHERE thread_id LIKE %(thread)s",
    "checkpoint_writes": "SELECT * FROM checkpoint_writes WHERE thread_id LIKE %(thread)s",
    "orders": "SELECT * FROM orders WHERE run_id = %(run)s",
    "order_items": f"SELECT * FROM order_items WHERE order_id IN ({_ORDERS}) ORDER BY line",
    "quotes": "SELECT * FROM quotes WHERE run_id = %(run)s",
    "approvals": "SELECT * FROM approvals WHERE run_id = %(run)s",
    # Identity-authz I35 `approval_requested_if_over_band`: the eval households' bands.
    "approval_rules": f"SELECT * FROM approval_rules WHERE household_id IN ({_EVAL_HH})",
    "shortfalls": "SELECT * FROM shortfalls WHERE run_id = %(run)s",
    "pantry_events": f"SELECT * FROM pantry_events WHERE order_id IN ({_ORDERS})",
    "mock_orders": "SELECT * FROM mock_orders WHERE external_order_id IN"
    " (SELECT external_order_id FROM orders WHERE run_id = %(run)s)",
    "mock_deliveries": "SELECT * FROM mock_deliveries WHERE external_delivery_id IN"
    " (SELECT external_delivery_id FROM orders WHERE run_id = %(run)s)",
    "sim_outbound": "SELECT * FROM sim_outbound WHERE run_id = %(run)s ORDER BY id",
    # The checks of architecture item 66 and agent-protocol P37: the run row, the eval
    # households' profile rows, and the sellers' floors and catalog allergens.
    "runs": "SELECT * FROM runs WHERE run_id = %(run)s",
    "household_members": f"SELECT * FROM household_members WHERE household_id IN ({_EVAL_HH})",
    "users": "SELECT user_id, display_name, keycloak_sub FROM users"
    f" WHERE user_id IN ({_EVAL_USERS})",
    "channel_identities": f"SELECT * FROM channel_identities WHERE user_id IN ({_EVAL_USERS})",
    "member_constraints": f"SELECT * FROM member_constraints WHERE user_id IN ({_EVAL_USERS})",
    "household_addresses": f"SELECT * FROM household_addresses WHERE household_id IN ({_EVAL_HH})",
    # Identity-authz I35 `no_tool_call`: which person each eval consumer agent acts for.
    "agent_cards": "SELECT agent_id, class, owner_ref FROM agent_cards WHERE class = 'consumer'"
    f" AND owner_ref IN (SELECT 'user:' || user_id::text FROM ({_EVAL_USERS}) eval_users)",
    # `spent_other_minor`: this week's placed-or-later orders of other runs, so the remaining
    # budget is `amount_minor - spent_other_minor` (orders.remaining_weekly_budget).
    "budgets": "SELECT b.*, COALESCE((SELECT sum(o.total_minor) FROM orders o"
    " WHERE o.household_id = b.household_id AND o.run_id <> %(run)s"
    " AND o.state = ANY(%(placed)s) AND o.created_at >= (date_trunc('week', now() AT TIME ZONE"
    " h.timezone) AT TIME ZONE h.timezone)), 0)::bigint AS spent_other_minor"
    " FROM budgets b JOIN households h USING (household_id)"
    f" WHERE b.period = 'weekly' AND b.household_id IN ({_EVAL_HH})",
    "business_rules": "SELECT * FROM business_rules WHERE kind = 'floors' AND agent_id IN"
    " (SELECT seller_agent_id FROM quotes WHERE run_id = %(run)s)",
    "catalog_items": "SELECT agent_id, sku, allergens, diets FROM catalog_items WHERE agent_id IN"
    " (SELECT seller_agent_id FROM orders WHERE run_id = %(run)s)",
    "menu_items": "SELECT agent_id, sku, allergens, diets FROM menu_items WHERE agent_id IN"
    " (SELECT seller_agent_id FROM orders WHERE run_id = %(run)s)",
}


def db_snapshot(dsn: str, run_id: uuid.UUID) -> Mapping[str, tuple[dict[str, Any], ...]]:
    """The run's rows, read in one read-only transaction. A table that does not exist is left
    out, so a check that needs it fails closed."""
    params = {"run": run_id, "thread": f"%:{run_id}", "placed": list(orders.PLACED_OR_LATER)}
    tables: dict[str, tuple[dict[str, Any], ...]] = {}
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.read_only = True
        with conn.transaction():
            for table, sql in SNAPSHOT_SQL.items():
                try:
                    with conn.transaction():
                        tables[table] = tuple(_db(conn).execute(sql, params).fetchall())
                except psycopg.errors.UndefinedTable:
                    continue
    return MappingProxyType(tables)


def _takes_db(check: Callable[..., Any]) -> bool:
    try:
        return "db" in inspect.signature(check).parameters
    except (TypeError, ValueError):
        return False


def wait_for_outcome(dsn: str, run_id: uuid.UUID, timeout: float) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while True:
        trace = read_trace(dsn, run_id)
        if any(row["kind"] == "outcome" for row in trace):
            return trace
        if time.monotonic() >= deadline:
            raise TimeoutError(f"no outcome event for run {run_id} after {timeout:.0f}s")
        time.sleep(0.5)


def needs_db(case: dict[str, Any]) -> bool:
    return any(_takes_db(CHECKS[item["check"]]) for item in case["expect"])


def run_checks(
    trace: list[dict[str, Any]],
    case: dict[str, Any],
    db: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run the case's checks; a check that takes `db` gets the read-only snapshot. Each result
    carries the check's `invariant` declaration; an undeclared check is invariant (§5)."""
    results = []
    for item in case["expect"]:
        check = CHECKS[item["check"]]
        given = dict(item.get("args") or {})
        args = dict(given)
        if _takes_db(check):
            args["db"] = db
        try:
            passed, detail = check(trace, case, **args)
        except Exception as err:
            passed, detail = False, f"check raised {type(err).__name__}: {err}"
        results.append(
            {
                "check": item["check"],
                "args": given,
                "invariant": getattr(check, "invariant", True) is not False,
                "passed": bool(passed),
                "detail": detail,
            }
        )
    return results


def is_infra(err: BaseException, stage: str) -> bool:
    """Whether an error before the agent run started is infrastructure (evals.md §5): the
    reset failed, `api` answered 5xx, or the connection failed."""
    if isinstance(err, urllib.error.HTTPError):
        return stage == "reset" or err.code >= 500
    return isinstance(err, urllib.error.URLError | ConnectionError | TimeoutError)


def run_case(
    case: dict[str, Any], repeat: int, dsn: str, timeout: float, mode: str = "stub"
) -> RunResult:
    """One attempt. On a timeout the checks still run over the partial trace, so a failed
    invariant check is `fail` whatever stopped the run (evals.md §5)."""
    result = RunResult(case["id"], case["category"], repeat, passed=False, mode=mode)
    started = time.monotonic()
    stage = "reset"
    try:
        reset_and_seed_mocks(case)
        stage = "start"
        run_id = SCENARIOS[case["run"]](case)
        stage = "run"
        result.run_id = str(run_id)
        deadline = time.monotonic() + timeout
        drive(dsn, run_id, run_steps(case, dsn, run_id), deadline)
        try:
            trace = wait_for_outcome(dsn, run_id, max(0.0, deadline - time.monotonic()))
        except TimeoutError as err:
            result.stopped, result.error = outcome.TIMEOUT, f"TimeoutError: {err}"
            trace = read_trace(dsn, run_id)
        result.tokens = sum((r.get("tokens_in") or 0) + (r.get("tokens_out") or 0) for r in trace)
        outcomes = [r for r in trace if r["kind"] == "outcome"]
        result.reason = outcomes[-1]["payload"].get("reason") if outcomes else None
        result.model_error = outcome.model_error(trace)
        result.models_used = sorted({r["model"] for r in trace if r.get("model")})
        db = db_snapshot(dsn, run_id) if needs_db(case) else None
        result.checks = run_checks(trace, case, db)
    except Exception as err:
        result.error = f"{type(err).__name__}: {err}"
        if stage != "run" and is_infra(err, stage):
            result.stopped = outcome.INFRA
        else:
            result.checks.append(
                {
                    "check": RUNNER_ERROR_CHECK,
                    "args": {},
                    "invariant": True,
                    "passed": False,
                    "detail": result.error,
                }
            )
    result.judge()
    result.seconds = round(time.monotonic() - started, 2)
    return result


# --- reporting ------------------------------------------------------------------------------


def _attempt_facts(r: RunResult) -> dict[str, Any]:
    return {
        "category": r.category,
        "verdict": r.verdict,
        "error_kind": r.error_kind,
        "tokens": r.tokens,
    }


def summarize(
    results: list[RunResult], thresholds: dict[str, Any], mode: str = "stub"
) -> dict[str, Any]:
    """Category rates, counts and the eval run outcome, all through `evals/outcome.py`."""
    attempts = [_attempt_facts(r) for r in results]
    budget = thresholds["budget"]
    tokens = sum(r.tokens for r in results)
    slow = sorted({r.case_id for r in results if r.seconds > budget["warn_seconds_per_scenario"]})
    reason_counts = Counter(r.reason for r in results if r.reason is not None)
    kinds = Counter(r.error_kind for r in results)
    name, detail, exit_code = outcome.outcome(attempts, thresholds, mode)
    return {
        "categories": outcome.categories(attempts, thresholds),
        "tokens": tokens,
        "max_tokens_per_suite": budget["max_tokens_per_suite"],
        "reason_counts": dict(sorted(reason_counts.items())),
        "error_kind_counts": dict(sorted(kinds.items())),
        "over_budget": tokens > budget["max_tokens_per_suite"],
        "slow_cases": slow,
        "gate_failures": outcome.gate_failures(attempts, thresholds, mode),
        "outcome": name,
        "outcome_detail": detail,
        "exit_code": exit_code,
        "passed": name == outcome.PASS,
    }


def _rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def to_markdown(summary: dict[str, Any], results: list[RunResult]) -> str:
    detail = f" ({summary['outcome_detail']})" if summary.get("outcome_detail") else ""
    lines = [
        f"# Eval results: {summary['outcome']}{detail}",
        "",
        "| Category | Pass | Fail | Skipped | Pass rate | Threshold | Gate | Status |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, c in summary["categories"].items():
        if c["ok"] is None:
            status = "no rate (all skipped)"
        elif c["ok"]:
            status = "ok"
        else:
            status = "FAIL" if name in summary["gate_failures"] else "below (not gating)"
        lines.append(
            f"| {name} | {c['pass']} | {c['fail']} | {c['skipped']} | {_rate(c['pass_rate'])} | "
            f"{c['min_pass_rate']:.0%} | {'yes' if c['gate'] else 'no'} | {status} |"
        )
    lines += ["", f"Tokens: {summary['tokens']} / {summary['max_tokens_per_suite']}"]
    if summary["slow_cases"]:
        lines.append(f"Slow cases (warn): {', '.join(summary['slow_cases'])}")
    sections = (("Failed attempts", outcome.FAIL), ("Skipped attempts", outcome.SKIPPED))
    for title, verdict in sections:
        shown = [r for r in results if r.verdict == verdict]
        if shown:
            lines += ["", f"## {title}", ""]
            for r in shown:
                why = r.error or "; ".join(str(c["detail"]) for c in r.checks if not c["passed"])
                lines.append(
                    f"- `{r.case_id}` repeat {r.repeat} (run {r.run_id}, {r.error_kind}): {why}"
                )
    return "\n".join(lines) + "\n"


def results_json(summary: dict[str, Any], results: list[RunResult]) -> dict[str, Any]:
    """The results JSON v1 (the field table in `evals/outcome.py`)."""
    return {
        "schema": RESULTS_SCHEMA,
        "eval_run_id": summary.get("eval_run_id"),
        "trigger": summary.get("trigger"),
        "git_sha": summary.get("git_sha"),
        "model_mode": summary.get("model_mode", "stub"),
        "models": summary.get("models") or {"people": None, "business": None},
        "started_at": summary.get("started_at"),
        "finished_at": summary.get("finished_at"),
        "outcome": summary["outcome"],
        "outcome_detail": summary["outcome_detail"],
        "exit_code": summary["exit_code"],
        "selection": summary.get("selection") or list(dict.fromkeys(r.case_id for r in results)),
        "categories": summary["categories"],
        "tokens": summary["tokens"],
        "max_tokens_per_suite": summary["max_tokens_per_suite"],
        "reason_counts": summary["reason_counts"],
        "error_kind_counts": summary["error_kind_counts"],
        "history_write_errors": summary.get("history_write_errors", 0),
        "attempts": [
            {
                "case_id": r.case_id,
                "repeat": r.repeat,
                "agent_run_ids": [r.run_id] if r.run_id else [],
                "verdict": r.verdict,
                "error_kind": r.error_kind,
                "reason": r.reason,
                "seconds": r.seconds,
                "tokens": r.tokens,
                "checks": [
                    {
                        "check": c["check"],
                        "args": c.get("args") or {},
                        "invariant": outcome.is_invariant(c),
                        "passed": c["passed"],
                        "detail": c.get("detail"),
                    }
                    for c in r.checks
                ],
                "models_used": list(r.models_used),
            }
            for r in results
        ],
    }


def write_results(summary: dict[str, Any], results: list[RunResult], markdown: str) -> Path:
    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    payload = results_json(summary, results)
    (RESULTS_DIR / f"{stamp}.json").write_text(json.dumps(payload, indent=2, default=str))
    (RESULTS_DIR / f"{stamp}.md").write_text(markdown)
    return RESULTS_DIR / f"{stamp}.json"


def status_line(summary: dict[str, Any], results: list[RunResult]) -> str:
    """The last line of standard output (evals.md §5). `last_green_sha` is read from the
    history with `--trigger` (`History.last_green_sha`), else null."""
    mode = summary.get("model_mode", "stub")
    failed = sorted({r.case_id for r in results if r.verdict == outcome.FAIL})
    real_safety_failed = mode == "real" and any(
        r.category == outcome.SAFETY and r.verdict == outcome.FAIL for r in results
    )
    status = {
        "outcome": summary["outcome"],
        "detail": summary["outcome_detail"],
        "exit_code": summary["exit_code"],
        "eval_run_id": summary.get("eval_run_id"),
        "mode": mode,
        "trigger": summary.get("trigger"),
        "git_sha": summary.get("git_sha"),
        "last_green_sha": summary.get("last_green_sha"),
        "failed_cases": failed,
        "real_safety_failed": real_safety_failed,
    }
    return STATUS_PREFIX + json.dumps(status)


def print_report(markdown: str, status: str) -> None:
    """The summary between the markers, then the `EVAL_STATUS` line, last."""
    print(SUMMARY_BEGIN)
    print(markdown.rstrip("\n"))
    print(SUMMARY_END)
    print(status, flush=True)


# --- budget, /health, the eval lock and history (evals.md §5, §7; build step 5c) -------------


@dataclass(frozen=True)
class Budget:
    """The two checks before each attempt (evals.md §7). `started` is the runner's start on
    the monotonic clock."""

    max_tokens: int
    run_ceiling: int
    timeout: float
    deadline: float
    started: float

    def affords(self, tokens_used: int, now: float) -> bool:
        tokens_left = self.max_tokens - tokens_used
        time_left = self.deadline - (now - self.started)
        return tokens_left >= self.run_ceiling and time_left >= self.timeout


def run_token_ceiling(max_tokens: int) -> int:
    """`RUN_TOKEN_CEILING`, or, when it is unset, `max_tokens_per_suite` itself, so a real
    attempt starts only on an untouched suite budget (see the PR's Decisions)."""
    raw = (os.environ.get("RUN_TOKEN_CEILING") or "").strip()
    return int(raw) if raw else max_tokens


def budget_skipped(case: dict[str, Any], repeat: int, mode: str) -> RunResult:
    """An attempt not started because the budget or the deadline could not afford it."""
    result = RunResult(
        case["id"],
        case["category"],
        repeat,
        passed=False,
        mode=mode,
        stopped=outcome.BUDGET_SKIPPED,
    )
    result.judge()
    return result


def agents_health() -> dict[str, Any]:
    url = os.environ.get("AGENTS_URL", DEFAULT_AGENTS_URL).rstrip("/") + "/health"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read())


def check_real_mode() -> tuple[dict[str, Any] | None, str | None]:
    """`({people, business}, None)` when `agents` `/health` reports `model_mode: real`;
    otherwise `(None, MODE_MISMATCH)`. An unreachable `/health` reports no mode, so it is a
    mismatch too (fail closed)."""
    try:
        health = agents_health()
    except Exception as err:
        print(f"agents /health unreachable: {type(err).__name__}: {err}", file=sys.stderr)
        return None, MODE_MISMATCH
    if health.get("model_mode") != "real":
        print(f"agents /health reports model_mode {health.get('model_mode')!r}", file=sys.stderr)
        return None, MODE_MISMATCH
    return {"people": health.get("model_name"), "business": health.get("model_business_name")}, None


def take_lock(
    dsn: str,
    *,
    wait: float = LOCK_WAIT_SECONDS,
    poll: float = LOCK_POLL_SECONDS,
) -> psycopg.Connection | None:
    """Take the eval lock on a connection of its own, trying every `poll` seconds for up to
    `wait` seconds. Returns the connection holding it (close it to release), or None. A
    failed connection counts as a try that did not get the lock."""
    give_up = time.monotonic() + wait
    while True:
        conn = None
        try:
            conn = psycopg.connect(dsn, autocommit=True)
            row = conn.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (LOCK_KEY,)).fetchone()
            if row is not None and row[0]:
                return conn
        except psycopg.Error as err:
            log.warning("eval lock: %s", err)
        if conn is not None:
            conn.close()
        if time.monotonic() + poll > give_up:
            return None
        time.sleep(poll)


def release_lock(conn: psycopg.Connection | None) -> None:
    if conn is None:
        return
    try:
        conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (LOCK_KEY,))
    except psycopg.Error as err:
        log.warning("eval lock release: %s", err)
    finally:
        conn.close()


_LAST_GREEN_SQL = """
SELECT git_sha FROM eval_runs
WHERE model_mode = %s AND trigger = %s AND outcome = 'pass'
ORDER BY started_at DESC, finished_at DESC NULLS LAST LIMIT 1
"""
_INSERT_RUN_SQL = """
INSERT INTO eval_runs (eval_run_id, trigger, git_sha, model_mode, models, selection, thresholds)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""
_INSERT_RESULT_SQL = """
INSERT INTO eval_results
  (eval_run_id, case_id, repeat, category, case_hash, model_mode, agent_run_ids, verdict,
   error_kind, reason, seconds, tokens, models_used, checks)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
_FINISH_RUN_SQL = """
UPDATE eval_runs SET outcome = %s, outcome_detail = %s, tokens = %s, reason_counts = %s,
  error_kind_counts = %s, finished_at = now()
WHERE eval_run_id = %s
"""


class History:
    """The eval history writes (evals.md §5, §6), only with `--trigger`. Each write uses its
    own connection and transaction. A failed write is logged and counted in `errors`; it
    never raises. When the tables don't exist, it logs once and writes nothing more."""

    def __init__(self, dsn: str, eval_run_id: str) -> None:
        self.dsn = dsn
        self.eval_run_id = eval_run_id
        self.errors = 0
        self.missing = False

    def _do(self, what: str, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]] | None:
        if self.missing:
            return None
        try:
            with psycopg.connect(self.dsn) as conn:
                cur = conn.execute(sql, params)
                return cur.fetchall() if cur.description else []
        except psycopg.errors.UndefinedTable as err:
            self.missing = True
            log.warning("eval history tables missing; no history is written: %s", err)
        except Exception as err:
            self.errors += 1
            log.warning("eval history %s failed: %s", what, err)
        return None

    def last_green_sha(self, mode: str, trigger: str) -> str | None:
        """The `git_sha` of the newest earlier eval run with the same mode and trigger that
        passed. Read before this eval run's row is written."""
        rows = self._do("read", _LAST_GREEN_SQL, (mode, trigger))
        return rows[0][0] if rows else None

    def start(self, run_info: dict[str, Any], selection: list[str], thresholds: Any) -> None:
        self._do(
            "eval_runs insert",
            _INSERT_RUN_SQL,
            (
                self.eval_run_id,
                run_info["trigger"],
                run_info["git_sha"],
                run_info["model_mode"],
                Jsonb(run_info["models"]),
                selection,
                Jsonb(thresholds),
            ),
        )

    def attempt(self, r: RunResult, case_hash: str) -> None:
        checks = [
            {**c, "args": c.get("args") or {}, "invariant": outcome.is_invariant(c)}
            for c in r.checks
        ]
        self._do(
            "eval_results insert",
            _INSERT_RESULT_SQL,
            (
                self.eval_run_id,
                r.case_id,
                r.repeat,
                r.category,
                case_hash,
                r.mode,
                [uuid.UUID(r.run_id)] if r.run_id else [],
                r.verdict,
                r.error_kind,
                r.reason,
                r.seconds,
                r.tokens,
                list(r.models_used),
                Jsonb(json.loads(json.dumps(checks, default=str))),
            ),
        )

    def finish(self, summary: dict[str, Any]) -> None:
        self._do(
            "eval_runs finish",
            _FINISH_RUN_SQL,
            (
                summary["outcome"],
                summary["outcome_detail"],
                summary["tokens"],
                Jsonb(summary["reason_counts"]),
                Jsonb(summary["error_kind_counts"]),
                self.eval_run_id,
            ),
        )


# The last real attempt per case and `case_hash` (evals.md §7). Attempts that never started an
# agent run (`budget_skipped`, `infra`) don't count, so a failed night loses nothing.
_LAST_REAL_SQL = """
SELECT case_id, case_hash, max(created_at) AS created_at FROM eval_results
WHERE model_mode = 'real' AND error_kind NOT IN ('budget_skipped', 'infra')
GROUP BY case_id, case_hash
"""


def last_real_attempts(dsn: str) -> list[dict[str, Any]]:
    """The history rows `evals/select.py` orders real mode by. A failed read (no history
    tables, no database) is logged and gives no rows, so every case counts as never run and
    the order falls back to id order."""
    try:
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            return list(conn.execute(_LAST_REAL_SQL).fetchall())
    except Exception as err:
        log.warning("eval history read for the case order failed: %s", err)
        return []


def list_cases(cases: list[dict[str, Any]], prefixes: Mapping[str, str]) -> list[str]:
    """`--list` (evals.md §5): one tab-separated line per case, in id order: id, category,
    tags, model modes and source design (`unmapped` when no prefix matches)."""
    lines = ["id\tcategory\ttags\tmodes\tsource"]
    for c in case_order.order(cases, [], "stub"):
        lines.append(
            "\t".join(
                [
                    c["id"],
                    c["category"],
                    ",".join(c["tags"]) or "-",
                    ",".join(model_modes(c)),
                    case_order.source_design(c["id"], prefixes) or "unmapped",
                ]
            )
        )
    return lines


def run_attempts(
    selected: list[dict[str, Any]],
    thresholds: dict[str, Any],
    repeats: int | None,
    mode: str,
    dsn: str,
    budget: Budget,
    history: History | None = None,
) -> list[RunResult]:
    """Each case's repeats in order. An attempt the budget can't afford is `budget_skipped`
    and not started. With `history`, each attempt's row is written when it finishes."""
    results: list[RunResult] = []
    tokens = 0
    for case in selected:
        digest = case_hash(case) if history is not None else ""
        for repeat in range(1, repeats_for(case, thresholds, repeats, mode) + 1):
            if budget.affords(tokens, time.monotonic()):
                res = run_case(case, repeat, dsn, budget.timeout, mode=mode)
            else:
                res = budget_skipped(case, repeat, mode)
            tokens += res.tokens
            results.append(res)
            if history is not None:
                history.attempt(res, digest)
    return results


# --- entry point ----------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m evals.run")
    parser.add_argument("--tag")
    parser.add_argument("--category")
    parser.add_argument("--case", dest="case_id")
    parser.add_argument("--all", dest="all_cases", action="store_true")
    # The PR gate runs each case once; thresholds.yaml's repeats apply nightly
    # (architecture item 63, amended 2026-11-03).
    parser.add_argument("--repeats", type=int, help="run every case N times (default: thresholds)")
    # evals.md §5.
    parser.add_argument("--cases-file", type=Path, help="a YAML list of case ids")
    parser.add_argument("--mode", choices=MODEL_MODES, default="stub")
    parser.add_argument("--trigger", choices=TRIGGERS, default=None)
    parser.add_argument("--list", dest="list_cases", action="store_true", help="list cases only")
    args = parser.parse_args(argv)
    if args.list_cases:
        return args
    if not (args.all_cases or args.tag or args.category or args.case_id or args.cases_file):
        parser.error("choose cases with --tag, --category, --case, --cases-file or --all")
    if args.repeats is not None and args.repeats < 1:
        parser.error("--repeats must be at least 1")
    return args


def repeats_for(
    case: dict[str, Any], thresholds: dict[str, Any], override: int | None, mode: str = "stub"
) -> int:
    """How many times to run `case`: `--repeats` when given, else its category's repeats for
    the model mode (evals.md §7)."""
    if override is not None:
        return override
    return int(thresholds["categories"][case["category"]]["repeats"][mode])


def _now() -> str:
    return datetime.now(UTC).isoformat()


def run_list() -> int:
    """`--list`: prints every case and its source design; runs nothing. Invalid cases exit 2."""
    try:
        cases = load_cases(set(load_thresholds()["categories"]))
    except CaseError as err:
        print(f"Invalid eval case: {err}", file=sys.stderr)
        return outcome.EXIT_CODES[outcome.INVALID_CASES]
    for line in list_cases(cases, case_order.load_coverage()["prefixes"]):
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    started = time.monotonic()
    args = parse_args(argv)
    if args.list_cases:
        return run_list()
    run_info: dict[str, Any] = {
        "eval_run_id": str(ids.new_eval_run_id()),
        "trigger": args.trigger,
        "git_sha": (os.environ.get("GIT_SHA") or "").strip() or None,
        "model_mode": args.mode,
        "models": {"people": None, "business": None},
        "last_green_sha": None,
        "started_at": _now(),
    }
    thresholds = load_thresholds()
    invalid = None
    try:
        cases = load_cases(set(thresholds["categories"]))
        case_ids = (
            load_case_ids(args.cases_file, {c["id"] for c in cases}) if args.cases_file else None
        )
        selected = select(
            cases,
            tag=args.tag,
            category=args.category,
            case_id=args.case_id,
            all_cases=args.all_cases,
            case_ids=case_ids,
            mode=args.mode,
        )
        if not selected:
            invalid = "No eval cases matched the selection."
    except CaseError as err:
        invalid = f"Invalid eval case: {err}"
    if invalid is not None:
        print(invalid, file=sys.stderr)
        name, detail, exit_code = outcome.outcome([], thresholds, args.mode, invalid=invalid)
        summary = {
            **run_info,
            "outcome": name,
            "outcome_detail": detail,
            "exit_code": exit_code,
        }
        print_report(f"# Eval results: {name}\n\n{invalid}\n", status_line(summary, []))
        return exit_code

    dsn = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    max_tokens = int(thresholds["budget"]["max_tokens_per_suite"])
    budget = Budget(
        max_tokens=max_tokens,
        run_ceiling=run_token_ceiling(max_tokens),
        timeout=float(os.environ.get("EVAL_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT_SECONDS),
        deadline=float(os.environ.get("EVAL_DEADLINE_SECONDS") or DEFAULT_DEADLINE_SECONDS),
        started=started,
    )
    # Eval-run-level checks, before any attempt (evals.md §5).
    run_error = None
    if args.trigger is not None and run_info["git_sha"] is None:
        run_error = NO_GIT_SHA
    if run_error is None and args.mode == "real":
        models, run_error = check_real_mode()
        if models is not None:
            run_info["models"] = models
    lock = None
    if run_error is None and args.trigger is not None:
        lock = take_lock(dsn, wait=LOCK_WAIT_SECONDS, poll=LOCK_POLL_SECONDS)
        if lock is None:
            run_error = LOCK_TIMEOUT

    # evals.md §7: real mode reads the history (under the lock, with `--trigger`).
    last = last_real_attempts(dsn) if run_error is None and args.mode == "real" else []
    selected = case_order.order(selected, last, args.mode, hashes=CASE_HASHES)
    selection = [c["id"] for c in selected]

    results: list[RunResult] = []
    history = None
    try:
        if run_error is None:
            if args.trigger is not None:
                history = History(dsn, run_info["eval_run_id"])
                run_info["last_green_sha"] = history.last_green_sha(args.mode, args.trigger)
                history.start(run_info, selection, thresholds)
            results = run_attempts(
                selected, thresholds, args.repeats, args.mode, dsn, budget, history
            )
        name, detail, exit_code = outcome.outcome(
            [_attempt_facts(r) for r in results], thresholds, args.mode, run_error=run_error
        )
        summary = {
            **summarize(results, thresholds, args.mode),
            "outcome": name,
            "outcome_detail": detail,
            "exit_code": exit_code,
            "passed": name == outcome.PASS,
            **run_info,
            "selection": selection,
        }
        if history is not None:
            history.finish(summary)
        summary["history_write_errors"] = history.errors if history is not None else 0
        summary["finished_at"] = _now()
    finally:
        release_lock(lock)
    markdown = to_markdown(summary, results)
    path = write_results(summary, results, markdown)
    print(f"Results: {path}")
    print_report(markdown, status_line(summary, results))
    return int(summary["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
