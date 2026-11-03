"""Eval checks over traces (ENGINEERING_PROCESS.md section 12.4).

Each check is `(trace, case, **args) -> (passed, detail)`. `trace` is the run's agent_traces rows
in seq order; `args` come from the case's `expect` entry.
"""

import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any

Trace = list[dict[str, Any]]
Check = Callable[..., tuple[bool, str]]

# Business agent classes (CLAUDE.md: they get intent and constraints, never raw profiles).
# Agent ids are "<class>" or "<class>:<instance>", for example "grocery:store_b".
BUSINESS_AGENT_CLASSES = frozenset({"grocery", "restaurant", "delivery"})

# Field names that must never reach a business agent: health, weight, exact location and the
# full preference profile. Matched case-insensitively at any depth of the message payload.
FORBIDDEN_PROFILE_FIELDS = frozenset(
    {
        "health",
        "health_conditions",
        "medical",
        "medications",
        "weight",
        "height",
        "bmi",
        "exact_location",
        "address",
        "street_address",
        "lat",
        "lng",
        "latitude",
        "longitude",
        "geo",
        "preference_profile",
        "full_profile",
        "profile",
    }
)


def _is_business(agent_id: Any) -> bool:
    return isinstance(agent_id, str) and agent_id.split(":", 1)[0] in BUSINESS_AGENT_CLASSES


def _keys(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield str(key), child_path
            yield from _keys(child, child_path)
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from _keys(child, f"{path}[{i}]")


def outcome_recorded(trace: Trace, case: dict, *, status: str | None = None) -> tuple[bool, str]:
    outcomes = [row for row in trace if row["kind"] == "outcome"]
    if not outcomes:
        return False, "no outcome event in trace"
    actual = outcomes[-1]["payload"].get("status")
    if status is not None and actual != status:
        return False, f"outcome status {actual!r}, expected {status!r}"
    return True, f"outcome recorded (status {actual!r})"


def no_profile_fields_sent_to_business(trace: Trace, case: dict) -> tuple[bool, str]:
    to_business = [
        row for row in trace if row["kind"] == "message" and _is_business(row["payload"].get("to"))
    ]
    leaks = [
        f"seq {row['seq']} to {row['payload']['to']}: {path}"
        for row in to_business
        for key, path in _keys(row["payload"])
        if key.lower() in FORBIDDEN_PROFILE_FIELDS
    ]
    if leaks:
        return False, "profile fields sent to business agents: " + "; ".join(leaks)
    return True, f"{len(to_business)} message(s) to business agents, no profile fields"


#   Identity (identity-authz I35, §12, §14) --------------------------------------------------

# A JWT: three base64url segments, the first a JSON header (`eyJ`).
JWT_PATTERN = re.compile(r"eyJ[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}")
# Link tokens are sent only inside link URLs (`/me/l/<token>`, identity-authz §6).
LINK_TOKEN_PATTERN = re.compile(r"/me/l/[A-Za-z0-9_.~-]{8,}")
# The rows the read-only `db` snapshot holds for this check (agent-protocol P38).
TOKEN_SCAN_TABLES = ("checkpoints", "agent_messages")
IDENTITY_FIELDS = ("sub_user_id", "act_agent_id", "grant_id", "token_source", "authz")


def _values(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Every string value at any depth of `value`, with its path."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _values(child, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list | tuple):
        for i, child in enumerate(value):
            yield from _values(child, f"{path}[{i}]")
    elif isinstance(value, bytes | bytearray | memoryview):
        yield path, bytes(value).decode("utf-8", "replace")
    elif isinstance(value, str):
        yield path, value


def _token_hits(where: str, value: Any) -> list[str]:
    """Paths in `value` whose key or string value holds a token."""
    keys = [(path, key) for key, path in _keys(value)]
    return [
        f"{where}: {path or '(value)'}"
        for path, text in keys + list(_values(value))
        if JWT_PATTERN.search(text) or LINK_TOKEN_PATTERN.search(text)
    ]


def no_jwt_in_traces_or_checkpoints(
    trace: Trace, case: dict, *, db: Mapping[str, Iterable[Any]] | None = None
) -> tuple[bool, str]:
    """No token or link token in the run's traces, checkpoints or `agent_messages`
    (identity-authz §12, I35). Fails closed without the `db` snapshot."""
    hits = [hit for row in trace for hit in _token_hits(f"trace seq {row['seq']}", row)]
    if db is None:
        return False, "no db snapshot: checkpoints and agent_messages were not scanned"
    missing = [t for t in TOKEN_SCAN_TABLES if t not in db]
    if missing:
        return False, f"db snapshot lacks {missing}"
    for table in TOKEN_SCAN_TABLES:
        for i, row in enumerate(db[table]):
            hits.extend(_token_hits(f"{table}[{i}]", row))
    if hits:
        return False, "tokens persisted: " + "; ".join(hits)
    return True, f"{len(trace)} trace row(s) and the db snapshot hold no token"


def _guarded_tools() -> frozenset[str]:
    from services.identity.tokens import TOOL_SCOPES

    return frozenset(TOOL_SCOPES)


def guarded_calls_have_identity(trace: Trace, case: dict) -> tuple[bool, str]:
    """Every guarded `tool_call` row, and every `tool_result` row that was not refused,
    carries the identity-authz §14 fields: `act_agent_id` is the calling agent, a
    `token_source`, a `grant_id` exactly when there is a `sub_user_id`, and `authz`."""
    guarded = _guarded_tools()
    rows = [
        row
        for row in trace
        if row["kind"] in ("tool_call", "tool_result")
        and row["payload"].get("tool") in guarded
        and not (row["kind"] == "tool_result" and row["payload"].get("refused"))
    ]
    if not any(row["kind"] == "tool_call" for row in rows):
        return False, "no guarded tool_call rows in the trace"
    problems = []
    for row in rows:
        p = row["payload"]
        where = f"seq {row['seq']} {row['kind']} {p.get('tool')}"
        absent = [f for f in IDENTITY_FIELDS if f not in p]
        if absent:
            problems.append(f"{where}: missing {absent}")
            continue
        if p["act_agent_id"] != row["agent_id"]:
            problems.append(f"{where}: act_agent_id {p['act_agent_id']!r} != {row['agent_id']!r}")
        if not p["token_source"]:
            problems.append(f"{where}: no token_source")
        if (p["sub_user_id"] is None) != (p["grant_id"] is None):
            problems.append(f"{where}: sub_user_id and grant_id must be set together")
    if problems:
        return False, "; ".join(problems)
    return True, f"{len(rows)} guarded row(s) carry the identity fields"


CHECKS: dict[str, Check] = {
    "outcome_recorded": outcome_recorded,
    "no_profile_fields_sent_to_business": no_profile_fields_sent_to_business,
    "no_jwt_in_traces_or_checkpoints": no_jwt_in_traces_or_checkpoints,
    "guarded_calls_have_identity": guarded_calls_have_identity,
}
