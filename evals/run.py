"""Eval runner (ENGINEERING_PROCESS.md section 12.6).

For each selected case and repeat: reset and seed mocks, start the scenario, drive the open
run (`approve_as`, `inject`, `shortfall`; architecture item 65, agent-protocol P38), wait for
an `outcome` trace event, then run the case's checks over the trace (and, for checks that take
it, a read-only `db` snapshot of the run's rows). Writes
evals/results/<timestamp>.json and .md and exits non-zero if a gated category falls below its
threshold or the suite exceeds its token budget.

Cases follow the format of evals.md §4 and are validated before any attempt (exit 2). Flags
(evals.md §5): `--tag`, `--category`, `--case`, `--cases-file`, `--all`, `--repeats`,
`--mode stub|real` (default `stub`; only cases whose `model_modes` include it run, each its
category's `repeats[mode]` times) and `--trigger`.

Config (env): ORCHESTRATOR_URL, DATABASE_URL, EVAL_TIMEOUT_SECONDS.
"""

import argparse
import asyncio
import inspect
import json
import os
import sys
import time
import urllib.request
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import psycopg
import yaml
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from evals.checks import CHECKS
from services.agents.common.trace import read_trace
from services.common import directory, ids, orders
from services.common.protocol import render_summary
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
    reason: str | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


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
    _validate_expect(case, source)
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


SCENARIOS: dict[str, Callable[[dict[str, Any]], uuid.UUID]] = {
    "hello": start_hello,
    "weekly_grocery_order": _start_run("weekly_grocery_order"),
    PAIR: start_pair,
    "busy_night_dinner": _start_run("busy_night_dinner"),
}


# --- runner steps while the run is open (approve_as, inject, shortfall) ----------------------

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


def run_steps(case: dict[str, Any], dsn: str, run_id: uuid.UUID) -> list[Step]:
    steps = []
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
    """Run the case's checks; a check that takes `db` gets the read-only snapshot."""
    results = []
    for item in case["expect"]:
        check = CHECKS[item["check"]]
        args = dict(item.get("args") or {})
        if _takes_db(check):
            args["db"] = db
        try:
            passed, detail = check(trace, case, **args)
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
        deadline = time.monotonic() + timeout
        drive(dsn, run_id, run_steps(case, dsn, run_id), deadline)
        trace = wait_for_outcome(dsn, run_id, max(0.0, deadline - time.monotonic()))
        result.tokens = sum((r["tokens_in"] or 0) + (r["tokens_out"] or 0) for r in trace)
        result.reason = [r for r in trace if r["kind"] == "outcome"][-1]["payload"].get("reason")
        db = db_snapshot(dsn, run_id) if needs_db(case) else None
        result.checks = run_checks(trace, case, db)
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
    reason_counts = Counter(r.reason for r in results if r.reason is not None)
    return {
        "categories": categories,
        "tokens": tokens,
        "max_tokens_per_suite": budget["max_tokens_per_suite"],
        "reason_counts": dict(sorted(reason_counts.items())),
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
    # The PR gate runs each case once; thresholds.yaml's repeats apply nightly
    # (architecture item 63, amended 2026-11-03).
    parser.add_argument("--repeats", type=int, help="run every case N times (default: thresholds)")
    # evals.md §5.
    parser.add_argument("--cases-file", type=Path, help="a YAML list of case ids")
    parser.add_argument("--mode", choices=MODEL_MODES, default="stub")
    parser.add_argument("--trigger", choices=TRIGGERS, default=None)
    args = parser.parse_args(argv)
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    thresholds = load_thresholds()
    try:
        cases = load_cases(set(thresholds["categories"]))
        case_ids = (
            load_case_ids(args.cases_file, {c["id"] for c in cases}) if args.cases_file else None
        )
    except CaseError as err:
        print(f"Invalid eval case: {err}", file=sys.stderr)
        return 2
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
        print("No eval cases matched the selection.", file=sys.stderr)
        return 2

    dsn = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    timeout = float(os.environ.get("EVAL_TIMEOUT_SECONDS", "300"))
    results = [
        run_case(case, repeat, dsn, timeout)
        for case in selected
        for repeat in range(1, repeats_for(case, thresholds, args.repeats, args.mode) + 1)
    ]
    summary = summarize(results, thresholds)
    # Without --trigger the runner writes no history (evals.md §5); the eval run's history
    # and the GIT_SHA check come with build step 5c.
    summary["model_mode"], summary["trigger"] = args.mode, args.trigger
    markdown = to_markdown(summary, results)
    path = write_results(summary, results, markdown)
    print(markdown)
    print(f"Results: {path}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
