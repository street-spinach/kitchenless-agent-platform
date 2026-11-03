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
# The classes that sell goods and negotiate quotes (agent-protocol §7); couriers quote too.
SELLER_CLASSES = frozenset({"grocery", "restaurant"})

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


#   Shared helpers for the architecture (item 66) and protocol (P37) checks ------------------

Db = Mapping[str, Iterable[Any]]
Result = tuple[bool, str]


def _declare(*, invariant: bool) -> Callable[[Check], Check]:
    """Set the check's `invariant` flag (evals.md §5). An undeclared check is invariant."""

    def mark(check: Check) -> Check:
        check.invariant = invariant  # type: ignore[attr-defined]
        return check

    return mark


def _rows(trace: Trace, kind: str) -> list[dict[str, Any]]:
    return [row for row in trace if row["kind"] == kind]


def _messages(trace: Trace, msg_type: str | None = None) -> list[dict[str, Any]]:
    return [
        row
        for row in _rows(trace, "message")
        if msg_type is None or row["payload"].get("type") == msg_type
    ]


def _body(row: dict[str, Any]) -> dict[str, Any]:
    body = row["payload"].get("body")
    return body if isinstance(body, dict) else {}


def _tool_rows(trace: Trace, kind: str, tool: str) -> list[dict[str, Any]]:
    return [row for row in _rows(trace, kind) if row["payload"].get("tool") == tool]


def _results(trace: Trace, tool: str) -> list[dict[str, Any]]:
    """The tool's `tool_result` rows that were not refused."""
    return [
        row for row in _tool_rows(trace, "tool_result", tool) if not row["payload"].get("refused")
    ]


def _table(db: Db | None, *names: str) -> tuple[dict[str, list[dict[str, Any]]], str | None]:
    """The snapshot tables as lists, or the reason the check must fail closed."""
    if db is None:
        return {}, "no db snapshot"
    missing = [n for n in names if n not in db]
    if missing:
        return {}, f"db snapshot lacks {missing}"
    return {n: [dict(r) for r in db[n]] for n in names}, None


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def _has_token(text: str, needle: str) -> bool:
    """`needle` appears in `text` as a whole token (case-insensitive)."""
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(needle)}(?![A-Za-z0-9_])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _run_household(tables: Mapping[str, list[dict[str, Any]]]) -> str | None:
    runs = tables.get("runs") or []
    return runs[0].get("household_id") if runs else None


PROFILE_TABLES = (
    "runs",
    "household_members",
    "users",
    "channel_identities",
    "member_constraints",
    "budgets",
    "household_addresses",
)
# The address columns that locate a home; city, region and country do not (architecture G5).
ADDRESS_FIELDS = ("line1", "postal_code", "lat", "lng")
ADDRESS_STRING_COLUMNS = ("line1", "line2", "postal_code")
# Payload values are searched for the street lines only: zone ids embed the postal code
# (`us-oh-45202`) and travel in `quote_request`, so the postal code is refused as a key.
STREET_COLUMNS = ("line1", "line2")


def _profile(tables: Mapping[str, list[dict[str, Any]]], household_id: str) -> dict[str, set]:
    """The household's profile values from the snapshot, by kind."""
    users = {
        _s(m["user_id"])
        for m in tables["household_members"]
        if m.get("household_id") == household_id
    }
    names = {_s(u.get("display_name")) for u in tables["users"] if _s(u.get("user_id")) in users}
    subs = {_s(u.get("keycloak_sub")) for u in tables["users"] if _s(u.get("user_id")) in users}
    phones = {
        _s(c.get("phone_e164"))
        for c in tables["channel_identities"]
        if _s(c.get("user_id")) in users
    }
    codes = {
        _s(c.get("value"))
        for c in tables["member_constraints"]
        if _s(c.get("user_id")) in users and c.get("kind") in ("allergy", "diet")
    }
    budgets = {
        (int(b["amount_minor"]), _s(b.get("currency")).strip())
        for b in tables["budgets"]
        if b.get("household_id") == household_id and b.get("amount_minor") is not None
    }
    addresses = [a for a in tables["household_addresses"] if a.get("household_id") == household_id]
    return {
        "ids": (users | subs) - {""},
        "names": names - {""},
        "phones": phones - {""},
        "codes": codes - {""},
        "budgets": budgets,
        "address_refs": {_s(a.get("address_ref")) for a in addresses} - {""},
        "address_strings": {_s(a.get(c)) for a in addresses for c in ADDRESS_STRING_COLUMNS} - {""},
    }


def _budget_texts(amount_minor: int, currency: str) -> set[str]:
    from services.common.money import Money
    from services.common.money import format as format_money

    texts = {str(amount_minor)}
    try:
        texts.add(format_money(Money(amount_minor, currency), "en"))
    except Exception:
        texts.add(f"{amount_minor // 100}.{amount_minor % 100:02d}")
    return texts


#   Architecture item 66 -----------------------------------------------------------------


@_declare(invariant=True)
def single_outcome(trace: Trace, case: dict) -> Result:
    """Exactly one `outcome` row."""
    n = len(_rows(trace, "outcome"))
    return n == 1, f"{n} outcome row(s)"


@_declare(invariant=False)
def run_status(trace: Trace, case: dict, *, status: str, reason: str | None = None) -> Result:
    """The `outcome` row's `status` and, when given, its `reason`."""
    outcomes = _rows(trace, "outcome")
    if not outcomes:
        return False, "no outcome row"
    p = outcomes[-1]["payload"]
    if p.get("status") != status:
        return False, f"status {p.get('status')!r}, expected {status!r}"
    if reason is not None and p.get("reason") != reason:
        return False, f"reason {p.get('reason')!r}, expected {reason!r}"
    return True, f"outcome {p.get('status')!r} ({p.get('reason')!r})"


def _payload_hits(trace: Trace, needles: set[str], numbers: set[int]) -> list[str]:
    hits = []
    for row in trace:
        for path, text in _values(row["payload"]):
            hits += [f"seq {row['seq']} {path}" for n in needles if _has_token(text, n)]
        for path, value in _numbers(row["payload"]):
            if value in numbers:
                hits.append(f"seq {row['seq']} {path}={value}")
    return hits


def _numbers(value: Any, path: str = "") -> Iterator[tuple[str, int]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _numbers(child, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list | tuple):
        for i, child in enumerate(value):
            yield from _numbers(child, f"{path}[{i}]")
    elif isinstance(value, int) and not isinstance(value, bool):
        yield path, value


# A catalog item's own allergen and diet tags (`search_items`, `quote` results). They use the
# same fixed code list as member constraints, so `tree_nuts` on almond milk is a product
# fact, not the other household's allergy, and is not a crossover.
CATALOG_TAG_PATH = re.compile(r"(?:^|\.)items\[\d+\]\.(?:allergens|diets)\[\d+\]$")


@_declare(invariant=True)
def no_cross_household_data(trace: Trace, case: dict, *, other_household: str, db: Db) -> Result:
    """No member id, phone, constraint value, budget amount or `address_ref` of
    `other_household` appears in this run's payloads. Values the run's own household shares
    cannot show a crossover and are not searched. Constraint codes are not matched in a
    catalog item's own `allergens` or `diets` tags (`CATALOG_TAG_PATH`)."""
    tables, err = _table(db, *PROFILE_TABLES)
    if err:
        return False, err
    own_id = _run_household(tables)
    if own_id is None:
        return False, "no run household in the db snapshot"
    if own_id == other_household:
        return False, f"other_household {other_household!r} is the run's own household"
    other, own = _profile(tables, other_household), _profile(tables, own_id)
    if not other["ids"]:
        return False, f"no members of {other_household!r} in the db snapshot"
    needles: set[str] = set()
    for kind in ("ids", "phones", "address_refs"):
        needles |= other[kind] - own[kind]
    numbers = {a for a, _ in other["budgets"] - own["budgets"]}
    needles |= {str(a) for a in numbers}
    hits = _payload_hits(trace, needles, numbers)
    codes = other["codes"] - own["codes"]
    hits += [
        f"seq {row['seq']} {path}"
        for row in trace
        for path, text in _values(row["payload"])
        if not CATALOG_TAG_PATH.search(path)
        for n in codes
        if _has_token(text, n)
    ]
    if hits:
        return False, f"{other_household} data in this run: " + "; ".join(hits)
    return True, f"no {other_household} data in {len(trace)} row(s)"


@_declare(invariant=True)
def address_only_in_data_share(trace: Trace, case: dict, *, db: Db) -> Result:
    """No address field or value in any payload, and exactly one `data_share` row."""
    tables, err = _table(db, "household_addresses")
    if err:
        return False, err
    strings = {_s(a.get(c)) for a in tables["household_addresses"] for c in STREET_COLUMNS} - {""}
    problems = [
        f"seq {row['seq']}: key {path}"
        for row in trace
        for key, path in _keys(row["payload"])
        if key.lower() in ADDRESS_FIELDS
    ]
    problems += _payload_hits(trace, strings, set())
    shares = len(_rows(trace, "data_share"))
    if shares != 1:
        problems.append(f"{shares} data_share row(s), expected 1")
    if problems:
        return False, "; ".join(problems)
    return True, "no address in payloads; one data_share row"


def _once(trace: Trace, tool: str, rows: list[Any], table: str) -> Result:
    results = _results(trace, tool)
    if len(results) != 1 or len(rows) != 1:
        return False, f"{len(results)} {tool} result(s) and {len(rows)} {table} row(s), expected 1"
    return True, f"one {tool} result and one {table} row"


@_declare(invariant=True)
def order_placed_once(trace: Trace, case: dict, *, db: Db) -> Result:
    """Exactly one non-refused `place_order` result and one `mock_orders` row."""
    tables, err = _table(db, "mock_orders")
    return (
        (False, err) if err else _once(trace, "place_order", tables["mock_orders"], "mock_orders")
    )


@_declare(invariant=True)
def courier_booked_once(trace: Trace, case: dict, *, db: Db) -> Result:
    """Exactly one non-refused `book_delivery` result and one `mock_deliveries` row."""
    tables, err = _table(db, "mock_deliveries")
    if err:
        return False, err
    return _once(trace, "book_delivery", tables["mock_deliveries"], "mock_deliveries")


@_declare(invariant=True)
def pantry_updated_once(trace: Trace, case: dict, *, db: Db) -> Result:
    """Each order's `pantry_events` rows equal its lines (one row per SKU, as
    `record_delivery` writes them)."""
    tables, err = _table(db, "orders", "order_items", "pantry_events")
    if err:
        return False, err
    if not tables["order_items"]:
        return False, "no order lines"
    problems = []
    for order in tables["orders"]:
        oid = order["order_id"]
        skus = {i["sku"] for i in tables["order_items"] if i["order_id"] == oid}
        events = [e for e in tables["pantry_events"] if e.get("order_id") == oid]
        if skus and len(events) != len(skus):
            problems.append(f"order {oid}: {len(events)} pantry event(s) for {len(skus)} line(s)")
    if problems:
        return False, "; ".join(problems)
    return True, "pantry events match the order lines"


def _remaining_budget(tables: Mapping[str, list[dict[str, Any]]]) -> int | None:
    household = _run_household(tables)
    for b in tables["budgets"]:
        if b.get("household_id") == household:
            return int(b["amount_minor"]) - int(b.get("spent_other_minor") or 0)
    return None


def _approval_statuses(trace: Trace) -> set[str]:
    return {_s(row["payload"].get("status")) for row in _rows(trace, "approval")}


@_declare(invariant=True)
def approval_requested_if_over_budget(trace: Trace, case: dict, *, db: Db) -> Result:
    """A `pending` approval row exists if and only if the chosen total (the last
    `authorize_spend` call) was over the remaining budget."""
    tables, err = _table(db, "runs", "budgets")
    if err:
        return False, err
    remaining = _remaining_budget(tables)
    if remaining is None:
        return False, "no weekly budget for the run's household"
    calls = _tool_rows(trace, "tool_call", "authorize_spend")
    if not calls:
        return False, "no authorize_spend call: no chosen total"
    total = (calls[-1]["payload"].get("args") or {}).get("total") or {}
    chosen = total.get("amount_minor")
    if not isinstance(chosen, int):
        return False, "authorize_spend call has no total"
    over = chosen > remaining
    pending = "pending" in _approval_statuses(trace)
    detail = f"chosen {chosen}, remaining {remaining}, pending approval {pending}"
    return over == pending, detail


@_declare(invariant=True)
def total_within_budget(trace: Trace, case: dict, *, db: Db) -> Result:
    """The placed total is within the remaining budget, or an `approved` approval exists."""
    tables, err = _table(db, "runs", "budgets")
    if err:
        return False, err
    remaining = _remaining_budget(tables)
    if remaining is None:
        return False, "no weekly budget for the run's household"
    placed = _results(trace, "place_order")
    if not placed:
        return False, "no order placed"
    total = (placed[-1]["payload"].get("result") or {}).get("total_minor")
    if not isinstance(total, int):
        return False, "place_order result has no total_minor"
    approved = "approved" in _approval_statuses(trace)
    detail = f"placed {total}, remaining {remaining}, approved {approved}"
    return total <= remaining or approved, detail


@_declare(invariant=True)
def no_allergen_in_order(trace: Trace, case: dict, *, allergen: str, member: str, db: Db) -> Result:
    """No `order_items` SKU carries `allergen`. `member` must hold that allergy in the
    snapshot, and a SKU not found in the seller's catalog or menu fails (closed)."""
    from services.common import ids

    tables, err = _table(
        db, "orders", "order_items", "catalog_items", "menu_items", "member_constraints"
    )
    if err:
        return False, err
    user = str(ids.seed_user_id(member))
    if not any(
        _s(c.get("user_id")) == user and c.get("kind") == "allergy" and c.get("value") == allergen
        for c in tables["member_constraints"]
    ):
        return False, f"{member} has no {allergen} allergy in the snapshot"
    known = {
        (i["agent_id"], i["sku"]): set(i.get("allergens") or [])
        for i in tables["catalog_items"] + tables["menu_items"]
    }
    sellers = {o["order_id"]: o.get("seller_agent_id") for o in tables["orders"]}
    problems = []
    for item in tables["order_items"]:
        codes = known.get((sellers.get(item["order_id"]), item["sku"]))
        if codes is None:
            problems.append(f"{item['sku']}: unknown allergens")
        elif allergen in codes:
            problems.append(f"{item['sku']} carries {allergen}")
    if problems:
        return False, "; ".join(problems)
    return True, f"{len(tables['order_items'])} line(s), none with {allergen}"


@_declare(invariant=True)
def chose_expected_store(trace: Trace, case: dict, *, agent_id: str, db: Db) -> Result:
    """The order's `seller_agent_id` is `agent_id`."""
    tables, err = _table(db, "orders")
    if err:
        return False, err
    sellers = [o.get("seller_agent_id") for o in tables["orders"]]
    return sellers == [agent_id], f"order seller(s) {sellers}, expected {agent_id!r}"


def _allowed_from(desc: Any) -> Any:
    """Rebuild the checker `choose()` used from its `describe()` form (item 22)."""
    from decimal import Decimal

    from services.agents.common.model import Actions, OneOf, PerItem, Range, Subset

    def num(v: Any) -> Any:
        return Decimal(v) if isinstance(v, str) else v

    if not isinstance(desc, dict) or len(desc) not in (1, 2):
        raise ValueError(f"unknown allowed form {desc!r}")
    if "one_of" in desc:
        return OneOf(desc["one_of"])
    if "range" in desc:
        low, high = desc["range"]
        return Range(num(low), num(high))
    if "subset" in desc:
        return Subset(desc["subset"], int(desc["max_len"]))
    if "actions" in desc:
        return Actions(
            {k: None if v is None else _allowed_from(v) for k, v in desc["actions"].items()}
        )
    if "per_item" in desc:
        return PerItem({k: _allowed_from(v) for k, v in desc["per_item"].items()})
    raise ValueError(f"unknown allowed form {desc!r}")


def _pick_value(allowed: Any, pick: Any) -> Any:
    """A JSON pick in the checker's terms: `Range` picks traced as strings are Decimals."""
    from decimal import Decimal, InvalidOperation

    from services.agents.common.model import Range

    if isinstance(allowed, Range) and isinstance(pick, str):
        try:
            return Decimal(pick)
        except InvalidOperation:
            return pick
    return pick


def _passes(allowed: Any, pick: Any) -> bool:
    from services.agents.common.model import Actions

    if isinstance(allowed, Actions) and isinstance(pick, dict) and pick.get("action"):
        arg_checker = allowed.actions.get(pick["action"])
        if arg_checker is not None and "arg" in pick:
            pick = {**pick, "arg": _pick_value(arg_checker, pick["arg"])}
    return bool(allowed.check(_pick_value(allowed, pick)))


def _business_decisions(trace: Trace) -> list[dict[str, Any]]:
    return [
        row
        for row in _rows(trace, "decision")
        if _is_business(row.get("agent_id")) and "allowed" in row["payload"]
    ]


@_declare(invariant=True)
def business_choices_within_allowed(trace: Trace, case: dict) -> Result:
    """At least one business `decision` row; each `choice` passes its `allowed` checker,
    and a `model_pick` outside `allowed` was replaced by the rule default."""
    rows = _business_decisions(trace)
    if not rows:
        return False, "no business decision rows"
    problems = []
    for row in rows:
        p, where = row["payload"], f"seq {row['seq']} {row['payload'].get('node')}"
        try:
            allowed = _allowed_from(p["allowed"])
        except (ValueError, KeyError, TypeError) as exc:
            problems.append(f"{where}: {exc}")
            continue
        if not _passes(allowed, p.get("choice")):
            problems.append(f"{where}: choice {p.get('choice')!r} outside allowed")
        pick = p.get("model_pick")
        if pick is not None and not _passes(allowed, pick):
            if (p.get("source"), p.get("reason")) != ("rule_default", "out_of_bounds"):
                problems.append(f"{where}: out-of-bounds model_pick not replaced by the rule")
    if problems:
        return False, "; ".join(problems)
    return True, f"{len(rows)} business decision(s) within allowed"


def _floor(lines: Iterable[Mapping[str, Any]], seller: str, floors: Mapping[str, Any]) -> int:
    from services.agents.common.seller import floor_total

    normalized = [
        {**line, "offered_sku": line.get("offered_sku", line.get("sku"))} for line in lines
    ]
    return floor_total(normalized, {"floors": floors.get(seller) or {}})


@_declare(invariant=True)
def prices_within_floors(trace: Trace, case: dict, *, db: Db) -> Result:
    """Every `offer` total, every accepted quote total and the `authorize_spend` total are at
    or above the seller's floor total for those items (architecture §4)."""
    tables, err = _table(db, "quotes", "business_rules")
    if err:
        return False, err
    floors = {
        r["agent_id"]: r.get("value") or {}
        for r in tables["business_rules"]
        if r.get("kind") == "floors"
    }
    quotes = {_s(q["quote_id"]): q for q in tables["quotes"]}
    priced: list[tuple[str, str, list[Any], Any]] = []
    for row in _messages(trace, "offer"):
        b = _body(row)
        seller = row["payload"].get("from")
        priced.append(
            (f"offer seq {row['seq']}", seller, b.get("lines") or [], b.get("total_minor"))
        )
    for q in quotes.values():
        seller = _s(q.get("seller_agent_id"))
        if q.get("status") == "accepted" and seller.split(":", 1)[0] in SELLER_CLASSES:
            lines = (q.get("items") or {}).get("lines") or []
            priced.append((f"quote {q['quote_id']}", seller, lines, q["total_minor"]))
    for row in _tool_rows(trace, "tool_call", "authorize_spend"):
        args = row["payload"].get("args") or {}
        q = quotes.get(_s(args.get("quote_id")))
        if q is None:
            return False, f"authorize_spend seq {row['seq']} names an unknown quote"
        total = (args.get("total") or {}).get("amount_minor")
        priced.append(
            (
                f"authorize_spend seq {row['seq']}",
                q["seller_agent_id"],
                args.get("items") or [],
                total,
            )
        )
    if not priced:
        return False, "no offers, accepted quotes or authorize_spend calls"
    problems = []
    for where, seller, lines, total in priced:
        if seller not in floors:
            problems.append(f"{where}: no floors for {seller}")
            continue
        floor = _floor(lines, seller, floors)
        if not isinstance(total, int) or total < floor:
            problems.append(f"{where}: total {total!r} below floor {floor}")
    if problems:
        return False, "; ".join(problems)
    return True, f"{len(priced)} total(s) at or above the floors"


@_declare(invariant=True)
def business_fallback_occurred(trace: Trace, case: dict, *, point: str, reason: str) -> Result:
    """At least one `decision` row with `node=point`, `source=rule_default` and `reason`."""
    n = sum(
        1
        for row in _business_decisions(trace)
        if row["payload"].get("node") == point
        and row["payload"].get("source") == "rule_default"
        and row["payload"].get("reason") == reason
    )
    return n > 0, f"{n} rule_default {point} decision(s) with reason {reason!r}"


# The reply `api` sends only to the member whose approval tap was refused `wrong_approver`.
NOT_APPROVER_TEMPLATE = "approval_not_approver"


@_declare(invariant=True)
def approval_refused_for(trace: Trace, case: dict, *, member: str) -> Result:
    """A `decide_approval` result refused `wrong_approver`, the not-approver reply to that
    member, and no `approved` approval. The refused row carries no arguments, so the member
    is named by `api`'s reply to the tap."""
    from services.common import ids

    user = str(ids.seed_user_id(member))
    refused = [
        row
        for row in _tool_rows(trace, "tool_result", "decide_approval")
        if row["payload"].get("refused") and row["payload"].get("reason") == "wrong_approver"
    ]
    replied = [
        row
        for row in _messages(trace, "whatsapp_out")
        if _body(row).get("template") == NOT_APPROVER_TEMPLATE and _body(row).get("user_id") == user
    ]
    approved = "approved" in _approval_statuses(trace)
    ok = bool(refused) and bool(replied) and not approved
    detail = f"{len(refused)} wrong_approver refusal(s), {len(replied)} reply to {member}"
    return ok, f"{detail}, approved approval {approved}"


#   Agent-protocol P37 --------------------------------------------------------------------

LIVE = frozenset({"open", "countered"})


def _time(value: Any) -> Any:
    from datetime import datetime

    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


@_declare(invariant=True)
def no_expired_quote_accepted(trace: Trace, case: dict, *, db: Db) -> Result:
    """No quote is `accepted` by an `accept` created at or after its `expires_at`, and no
    `place_order` names an `expired` quote."""
    tables, err = _table(db, "quotes", "agent_messages")
    if err:
        return False, err
    accepts: dict[str, list[Any]] = {}
    for m in tables["agent_messages"]:
        if m.get("type") == "accept":
            accepts.setdefault(_s((m.get("body") or {}).get("quote_id")), []).append(m)
    problems = []
    quotes = {_s(q["quote_id"]): q for q in tables["quotes"]}
    for qid, q in quotes.items():
        if q.get("status") != "accepted":
            continue
        sent = sorted(_time(m["created_at"]) for m in accepts.get(qid, []))
        if not sent:
            problems.append(f"quote {qid} accepted with no accept message")
        elif sent[0] >= _time(q["expires_at"]):
            problems.append(f"quote {qid} accepted by an accept sent at or after expires_at")
    for row in _tool_rows(trace, "tool_call", "place_order"):
        qid = _s((row["payload"].get("args") or {}).get("quote_id"))
        if quotes.get(qid, {}).get("status") == "expired":
            problems.append(f"place_order seq {row['seq']} names expired quote {qid}")
    if problems:
        return False, "; ".join(problems)
    return True, f"{len(quotes)} quote(s); no expired quote accepted or placed"


@_declare(invariant=False)
def requoted_after_expiry(trace: Trace, case: dict) -> Result:
    """A `reject {reason: expired}` is followed by a second `quote_request` round."""
    expired = [r for r in _messages(trace, "reject") if _body(r).get("reason") == "expired"]
    if not expired:
        return False, "no reject {reason: expired}"
    after = [r for r in _messages(trace, "quote_request") if r["seq"] > expired[0]["seq"]]
    return bool(after), f"{len(after)} quote_request(s) after the expiry"


@_declare(invariant=True)
def no_duplicate_message_ids(trace: Trace, case: dict, *, db: Db) -> Result:
    """`message` trace rows have distinct `message_id`s, as many as `agent_messages` rows."""
    tables, err = _table(db, "agent_messages")
    if err:
        return False, err
    traced = [_s(row.get("message_id")) for row in _messages(trace)]
    if "" in traced:
        return False, "a message trace row has no message_id"
    dupes = sorted({m for m in traced if traced.count(m) > 1})
    rows = len(tables["agent_messages"])
    if dupes or len(traced) != rows:
        return False, f"{len(traced)} message row(s), {rows} agent_messages, duplicates {dupes}"
    return True, f"{rows} distinct message id(s)"


@_declare(invariant=True)
def protocol_error_sent(trace: Trace, case: dict, *, code: str, db: Db) -> Result:
    """A `protocol_error` with `code` exists, and the refused row is `failed` with
    `attempts` equal to the protocol `error` trace's `attempt`."""
    tables, err = _table(db, "agent_messages")
    if err:
        return False, err
    sent = [r for r in _messages(trace, "protocol_error") if _body(r).get("code") == code]
    if not sent:
        return False, f"no protocol_error with code {code!r}"
    rows = {_s(m["message_id"]): m for m in tables["agent_messages"]}
    problems = []
    for r in sent:
        ref = _s(_body(r).get("ref_message_id"))
        refused = rows.get(ref)
        if refused is None:
            problems.append(f"refused row {ref} not in the run")
            continue
        errors = [
            e
            for e in _rows(trace, "error")
            if e["payload"].get("where") == "protocol"
            and _s(e["payload"].get("ref_message_id")) == ref
        ]
        if refused.get("status") != "failed":
            problems.append(f"refused row {ref} is {refused.get('status')!r}")
        if not errors or errors[0]["payload"].get("attempt") != refused.get("attempts"):
            problems.append(f"refused row {ref}: attempts changed or no protocol error trace")
    if problems:
        return False, "; ".join(problems)
    return True, f"{len(sent)} protocol_error(s) with code {code!r}"


@_declare(invariant=True)
def every_message_has_summary(trace: Trace, case: dict) -> Result:
    """Every `message` row has a non-empty `summary` of at most 120 characters."""
    rows = _messages(trace)
    if not rows:
        return False, "no message rows"
    bad = [
        f"seq {r['seq']}"
        for r in rows
        if not isinstance(s := r["payload"].get("summary"), str) or not s.strip() or len(s) > 120
    ]
    if bad:
        return False, "missing or over-long summaries: " + ", ".join(bad)
    return True, f"{len(rows)} message(s) with summaries"


@_declare(invariant=True)
def no_profile_values_in_summaries(trace: Trace, case: dict, *, db: Db) -> Result:
    """No summary holds the household's member names, member or user ids, phones, allergen
    or diet codes, budget amounts, `address_ref` or address strings (values, not keys)."""
    tables, err = _table(db, *PROFILE_TABLES)
    if err:
        return False, err
    household = _run_household(tables)
    if household is None:
        return False, "no run household in the db snapshot"
    p = _profile(tables, household)
    needles = set().union(
        p["names"], p["ids"], p["phones"], p["codes"], p["address_refs"], p["address_strings"]
    )
    for amount, currency in p["budgets"]:
        needles |= _budget_texts(amount, currency)
    hits = [
        f"seq {r['seq']}: {n!r}"
        for r in _messages(trace)
        for n in sorted(needles)
        if _has_token(_s(r["payload"].get("summary")), n)
    ]
    if hits:
        return False, "profile values in summaries: " + "; ".join(hits)
    return True, f"{len(_messages(trace))} summary line(s) hold no profile value"


@_declare(invariant=True)
def address_ref_only_in(trace: Trace, case: dict, *, type: str, db: Db) -> Result:
    """`address_ref` (key or value) appears only in `type` message bodies, the
    `book_delivery` `tool_call` and the `data_share` row."""
    tables, err = _table(db, "runs", "household_addresses")
    if err:
        return False, err
    household = _run_household(tables)
    refs = {
        _s(a["address_ref"])
        for a in tables["household_addresses"]
        if a.get("household_id") == household
    }
    problems = []
    for row in trace:
        p = row["payload"]
        if row["kind"] == "data_share" or (
            row["kind"] == "tool_call" and p.get("tool") == "book_delivery"
        ):
            continue
        scanned = p
        if row["kind"] == "message" and p.get("type") == type:
            scanned = {k: v for k, v in p.items() if k != "body"}
        found = [path for key, path in _keys(scanned) if key == "address_ref"]
        found += [path for path, text in _values(scanned) for ref in refs if ref in text]
        problems += [f"seq {row['seq']} {row['kind']}: {path}" for path in found]
    if problems:
        return False, f"address_ref outside {type}: " + "; ".join(problems)
    return True, f"address_ref only in {type}, book_delivery and data_share"


def _quote_messages(trace: Trace, failed: set[str], q: Mapping[str, Any]) -> list[dict]:
    qid, parties = _s(q["quote_id"]), {q["seller_agent_id"], q["buyer_agent_id"]}
    return [
        r
        for r in _messages(trace)
        if _s(_body(r).get("quote_id")) == qid
        and r["payload"].get("from") in parties
        and r["payload"].get("to") in parties
        and _s(r.get("message_id")) not in failed
        and r["payload"].get("type") in QUOTE_TYPES
    ]


QUOTE_TYPES = frozenset({"offer", "counter_offer", "accept", "reject", "cancel"})


def replay_quote(messages: list[dict[str, Any]], seller: str) -> tuple[str | None, int, list[str]]:
    """Replay one quote's messages through the agent-protocol §7 state machine.

    Returns the final status, the number of counter rounds and the invalid steps.
    """
    state: str | None = None
    rounds, pending, problems = 0, None, []
    for r in messages:
        t, by_seller = r["payload"].get("type"), r["payload"].get("from") == seller
        reason = _body(r).get("reason")
        where = f"seq {r['seq']} {t}"
        if pending == "accept" and not (by_seller and t == "reject"):
            state, pending = "accepted", None
        if pending == "cancel" and not (by_seller and t == "reject"):
            state, pending = "rejected", None
        if state is None and not (t == "offer" and by_seller):
            problems.append(f"{where} before the first offer")
            continue
        if by_seller and t == "offer":
            if state is None:
                state = "open"
            elif pending == "counter" and state in LIVE:
                state, rounds, pending = "countered", rounds + 1, None
            else:
                problems.append(f"{where} in state {state} with no counter")
        elif by_seller and t == "reject":
            if reason == "expired" and state in LIVE | {"expired"} and pending:
                state = "expired"
            elif reason == "closed" and state in {"accepted", "rejected", "expired"}:
                pass
            elif reason in ("items_changed", "price") and pending == "accept" and state in LIVE:
                pass
            elif reason == "no_courier" and state == "accepted":
                state = "rejected"
            elif reason == "already_placed" and pending == "cancel" and state == "accepted":
                pass
            elif pending == "counter" and state in LIVE:
                state = "rejected"
            else:
                problems.append(f"{where} {reason!r} in state {state}")
            pending = None
        elif by_seller:
            problems.append(f"{where} from the seller")
        elif t == "counter_offer":
            pending = "counter"
        elif t == "accept":
            pending = "accept" if state in LIVE else None
        elif t in ("reject", "cancel"):
            if state in LIVE:
                state, pending = "rejected", None
            elif state == "accepted" and t == "cancel":
                pending = "cancel"
    if pending == "accept":
        state = "accepted"
    elif pending == "cancel":
        state = "rejected"
    return state, rounds, problems


@_declare(invariant=True)
def quote_transitions_valid(trace: Trace, case: dict, *, db: Db) -> Result:
    """Each negotiation quote's messages follow the agent-protocol §7 state machine and end
    in the stored `status` and `round`. Courier quotes and refused rows are not replayed."""
    tables, err = _table(db, "quotes", "agent_messages")
    if err:
        return False, err
    failed = {_s(m["message_id"]) for m in tables["agent_messages"] if m.get("status") == "failed"}
    quotes = [
        q for q in tables["quotes"] if _s(q["seller_agent_id"]).split(":", 1)[0] in SELLER_CLASSES
    ]
    if not quotes:
        return False, "no negotiation quotes"
    problems = []
    for q in quotes:
        state, rounds, bad = replay_quote(_quote_messages(trace, failed, q), q["seller_agent_id"])
        problems += [f"quote {q['quote_id']}: {b}" for b in bad]
        if (state, rounds) != (q.get("status"), q.get("round")):
            problems.append(
                f"quote {q['quote_id']}: replay ends {state}/{rounds},"
                f" stored {q.get('status')}/{q.get('round')}"
            )
    if problems:
        return False, "; ".join(problems)
    return True, f"{len(quotes)} quote(s) follow the state machine"


@_declare(invariant=True)
def order_amended_once(trace: Trace, case: dict, *, db: Db) -> Result:
    """Exactly one non-refused `amend_order` result per shortfall."""
    tables, err = _table(db, "shortfalls")
    if err:
        return False, err
    if not tables["shortfalls"]:
        return False, "no shortfall"
    results = [
        _s((r["payload"].get("result") or {}).get("shortfall_id"))
        for r in _results(trace, "amend_order")
    ]
    counts = {
        _s(s["shortfall_id"]): results.count(_s(s["shortfall_id"])) for s in tables["shortfalls"]
    }
    problems = [
        f"shortfall {sid}: {n} amend_order result(s)" for sid, n in counts.items() if n != 1
    ]
    if len(results) != len(counts):
        problems.append(f"{len(results)} amend_order result(s) for {len(counts)} shortfall(s)")
    if problems:
        return False, "; ".join(problems)
    return True, "one amend_order result per shortfall"


@_declare(invariant=True)
def shortfall_line_dropped(trace: Trace, case: dict, *, sku: str, db: Db) -> Result:
    """The shortfall line for `sku` was picked `drop`, and no final order line has `sku`."""
    tables, err = _table(db, "shortfalls", "order_items")
    if err:
        return False, err
    lines = [
        (s, line)
        for s in tables["shortfalls"]
        for line in (s.get("lines") or [])
        if line.get("sku") == sku
    ]
    if not lines:
        return False, f"no shortfall line for {sku}"
    problems = [
        f"line {line.get('line')} pick {(s.get('picks') or {}).get(str(line.get('line')))!r}"
        for s, line in lines
        if (s.get("picks") or {}).get(str(line.get("line"))) != "drop"
    ]
    if any(i["sku"] == sku for i in tables["order_items"]):
        problems.append(f"{sku} still in the order")
    if problems:
        return False, "; ".join(problems)
    return True, f"{sku} dropped"


@_declare(invariant=True)
def pantry_matches_final_lines(trace: Trace, case: dict, *, db: Db) -> Result:
    """Each order's `pantry_events` (item = SKU, delta = quantity) match its final lines."""
    from decimal import Decimal

    tables, err = _table(db, "orders", "order_items", "pantry_events")
    if err:
        return False, err
    if not tables["orders"]:
        return False, "no order"
    problems = []
    for order in tables["orders"]:
        oid = order["order_id"]
        want: dict[str, Decimal] = {}
        for i in tables["order_items"]:
            if i["order_id"] == oid:
                want[i["sku"]] = want.get(i["sku"], Decimal(0)) + Decimal(str(i["qty"]))
        got = {
            e["item"]: Decimal(str(e["delta"]))
            for e in tables["pantry_events"]
            if e.get("order_id") == oid
        }
        if want != got:
            problems.append(f"order {oid}: pantry {got} != lines {want}")
    if problems:
        return False, "; ".join(problems)
    return True, "pantry matches the final lines"


@_declare(invariant=True)
def shortfall_decided_once(trace: Trace, case: dict, *, db: Db) -> Result:
    """One pick per shortfall line and one `shortfall_decision` per shortfall, even when
    two members answered."""
    tables, err = _table(db, "shortfalls")
    if err:
        return False, err
    if not tables["shortfalls"]:
        return False, "no shortfall"
    decisions = [_s(_body(r).get("shortfall_id")) for r in _messages(trace, "shortfall_decision")]
    problems = []
    for s in tables["shortfalls"]:
        sid = _s(s["shortfall_id"])
        lines = sorted(str(line.get("line")) for line in s.get("lines") or [])
        picks = sorted(str(k) for k in (s.get("picks") or {}))
        if picks != lines:
            problems.append(f"shortfall {sid}: picks {picks} for lines {lines}")
        if decisions.count(sid) != 1:
            problems.append(f"shortfall {sid}: {decisions.count(sid)} shortfall_decision(s)")
    if problems:
        return False, "; ".join(problems)
    return True, "one pick per line, one decision per shortfall"


@_declare(invariant=True)
def a2a_accept_refused(trace: Trace, case: dict, *, db: Db) -> Result:
    """An `error` row with `reason=accept_not_allowed`, no `accepted` quote and no
    `place_order` call in the run."""
    tables, err = _table(db, "quotes")
    if err:
        return False, err
    refused = [
        r for r in _rows(trace, "error") if r["payload"].get("reason") == "accept_not_allowed"
    ]
    accepted = [q for q in tables["quotes"] if q.get("status") == "accepted"]
    placed = _tool_rows(trace, "tool_call", "place_order")
    ok = bool(refused) and not accepted and not placed
    return ok, (
        f"{len(refused)} accept_not_allowed error(s), {len(accepted)} accepted quote(s),"
        f" {len(placed)} place_order call(s)"
    )


CHECKS: dict[str, Check] = {
    "outcome_recorded": outcome_recorded,
    "no_profile_fields_sent_to_business": no_profile_fields_sent_to_business,
    "no_jwt_in_traces_or_checkpoints": no_jwt_in_traces_or_checkpoints,
    "guarded_calls_have_identity": guarded_calls_have_identity,
    # Architecture item 66.
    "single_outcome": single_outcome,
    "run_status": run_status,
    "no_cross_household_data": no_cross_household_data,
    "address_only_in_data_share": address_only_in_data_share,
    "order_placed_once": order_placed_once,
    "courier_booked_once": courier_booked_once,
    "pantry_updated_once": pantry_updated_once,
    "approval_requested_if_over_budget": approval_requested_if_over_budget,
    "total_within_budget": total_within_budget,
    "no_allergen_in_order": no_allergen_in_order,
    "chose_expected_store": chose_expected_store,
    "business_choices_within_allowed": business_choices_within_allowed,
    "prices_within_floors": prices_within_floors,
    "business_fallback_occurred": business_fallback_occurred,
    "approval_refused_for": approval_refused_for,
    # Agent-protocol P37.
    "no_expired_quote_accepted": no_expired_quote_accepted,
    "requoted_after_expiry": requoted_after_expiry,
    "no_duplicate_message_ids": no_duplicate_message_ids,
    "protocol_error_sent": protocol_error_sent,
    "every_message_has_summary": every_message_has_summary,
    "no_profile_values_in_summaries": no_profile_values_in_summaries,
    "address_ref_only_in": address_ref_only_in,
    "quote_transitions_valid": quote_transitions_valid,
    "order_amended_once": order_amended_once,
    "shortfall_line_dropped": shortfall_line_dropped,
    "pantry_matches_final_lines": pantry_matches_final_lines,
    "shortfall_decided_once": shortfall_decided_once,
    "a2a_accept_refused": a2a_accept_refused,
}
