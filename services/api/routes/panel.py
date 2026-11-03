"""Control panel routes in `api` (architecture item 39, agent-protocol §8 and P31).

`/panel/*` sits behind Caddy basic auth. The routes call the mock control functions in
`services.mocks.control` (architecture item 47) and read traces and messages for the
"watch the agents talk" view:

*   `POST /panel/reset {env, setup, household}`: `env: eval` only. `setup` holds keys of
    `control.SETUP_KEYS` applied to `household` (an eval household, default `us-1-eval`)
    and undone by the next reset (evals.md §4, E-A7). An unknown key, a bad type or a
    non-eval household is refused with 400.
*   `POST /panel/events {env, kind, target_agent_id, params}` writes `mock_events`. The
    `shortfall` kind (agent-protocol §10 and P31, #70) is refused with 409 unless the
    order is `courier_booked` and has no shortfall (no `shortfalls` row and no earlier
    shortfall event); `target_agent_id` must be the order's seller and `env` the order's
    household env. It then sends `shortfall_detected` from `system:api` to
    `orders.seller_agent_id` in the same transaction.
*   `PUT /panel/modes/{vendor} {mode}` writes `adapter_modes`.
*   `POST /panel/approvals/{id}/expire` makes that approval's expiry timer due now.
*   `GET /panel/runs/live` returns recent `message` and `error` trace rows, grouped per run
    into lanes by `conversation_id` (§8). Rows carry the summary, never the body.
*   `GET /panel/messages/{message_id}` returns the delivery fields of one message and, for
    the S1-S7 types only, the body, the A2A form and the rationale (§8). People and system
    types (S8) show the summary only, because their bodies can hold profile data.
*   `GET /panel/evals` is the eval results page (evals.md §14, EG7): a real-mode headline,
    a labelled stub section and a per-case table linking each attempt to
    `/panel/runs/live?run_id=`. With no history rows or tables it says so.
*   `GET /panel/experience?household=` (experience §12, EX-I22): decisions asked per adult
    this week and last, the guard metrics, pending held items and the configured Meta
    templates, as JSON or (for an HTML client) a three-line page. No actions.
"""

import html
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from psycopg import AsyncConnection, errors
from psycopg.rows import dict_row
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from services.api.whatsapp import templates
from services.common import ids, outbox
from services.common.config import get_settings
from services.common.protocol import schemas
from services.common.protocol.a2a import AgentMessageRow, QuoteRow, to_a2a_message
from services.common.protocol.catalog import render_summary
from services.mocks import control

__all__ = [
    "DETAIL_TYPES",
    "EVAL_CASES_DIR",
    "GUARD_METRICS",
    "HOUSEHOLD_LANE",
    "LIVE_LIMIT",
    "LIVE_LIMIT_MAX",
    "NO_EVAL_RUNS",
    "STUB_HEADING",
    "routes",
]

API_AGENT = "system:api"
# The lane of people and system rows that have no conversation (§8).
HOUSEHOLD_LANE = "household"
LIVE_LIMIT = 200
LIVE_LIMIT_MAX = 1000

# Schemas S1-S7 (negotiation, delivery, shortfall and error). Only these types show their
# body, A2A form and rationale; every other type, and any type not listed, shows the
# summary only (§8).
_DETAIL_SCHEMAS: frozenset[type] = frozenset(
    {
        schemas.QuoteRequest,  # S1
        schemas.Quote,  # S2
        schemas.QuoteDecision,  # S3
        schemas.OrderUpdate,  # S4
        schemas.DeliveryQuoteRequest,  # S5
        schemas.DeliveryQuote,
        schemas.DeliveryRequest,
        schemas.ShortfallNotice,  # S6
        schemas.ShortfallDecision,
        schemas.ProtocolErrorBody,  # S7
    }
)
DETAIL_TYPES = frozenset(t for t, s in schemas.SCHEMAS.items() if s in _DETAIL_SCHEMAS)


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _json_object(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _uuid(raw: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


def _str(value: object) -> str | None:
    return None if value is None else str(value)


def _iso(value: Any) -> str | None:
    return None if value is None else value.isoformat()


# --- mock control ---------------------------------------------------------------------------


async def reset(request: Request) -> JSONResponse:
    body = await _json_object(request)
    if body is None:
        return _error(400, "the body must be a JSON object")
    env = body.get("env")
    if not isinstance(env, str):
        return _error(400, "env is a required string")
    setup = body.get("setup")
    if setup is not None and not isinstance(setup, dict):
        return _error(400, "setup must be an object")
    household = body.get("household", control.DEFAULT_EVAL_HOUSEHOLD)
    if not isinstance(household, str):
        return _error(400, "household must be a string")
    async with request.app.state.connect() as conn:
        try:
            deleted = await control.reset(conn, env, setup, household)
        except control.ControlRefused as exc:
            return _error(400, str(exc))
    return JSONResponse({"env": "eval", "deleted": deleted})


async def events(request: Request) -> JSONResponse:
    body = await _json_object(request)
    if body is None:
        return _error(400, "the body must be a JSON object")
    env, kind = body.get("env"), body.get("kind")
    target, params = body.get("target_agent_id"), body.get("params")
    if not isinstance(env, str) or not isinstance(kind, str) or not isinstance(target, str):
        return _error(400, "env, kind and target_agent_id are required strings")
    if params is not None and not isinstance(params, dict):
        return _error(400, "params must be an object")
    if kind == "shortfall":
        return await _shortfall_event(request, env, target, params or {})
    async with request.app.state.connect() as conn, conn.transaction():
        try:
            event_id = await control.inject_event(
                conn, env=env, kind=kind, target_agent_id=target, params=params
            )
        except control.ControlRefused as exc:
            return _error(400, str(exc))
    return JSONResponse({"event_id": event_id})


_SHORTFALL_ORDER_SQL = """
SELECT o.order_id, o.run_id, o.state, o.seller_agent_id, h.env
FROM orders o JOIN households h ON h.household_id = o.household_id
WHERE o.order_id = %s
FOR UPDATE OF o
"""

# A shortfall row, or an earlier shortfall event that has not opened its row yet.
_SHORTFALL_EXISTS_SQL = """
SELECT 1 FROM shortfalls WHERE order_id = %(order_id)s
UNION ALL
SELECT 1 FROM mock_events WHERE kind = 'shortfall' AND params->>'order_id' = %(order_id)s::text
LIMIT 1
"""


async def _shortfall_event(
    request: Request, env: str, target: str, params: dict[str, Any]
) -> JSONResponse:
    """`kind=shortfall` (agent-protocol §10 "Trigger" and P31).

    409 unless the order is `courier_booked` and has no shortfall; otherwise one
    transaction writes the `mock_events` row and sends `shortfall_detected` from
    `system:api` to `orders.seller_agent_id`.
    """
    order_id = _uuid(params.get("order_id"))
    if order_id is None:
        return _error(400, "shortfall params.order_id must be a uuid")
    async with request.app.state.connect() as conn, conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_SHORTFALL_ORDER_SQL, (order_id,))
            order = await cur.fetchone()
        if order is None:
            return _error(400, f"unknown order {order_id}")
        if order["env"] != env:
            return _error(400, f"order {order_id} is not in env {env!r}")
        if order["state"] != "courier_booked":
            return _error(409, f"order {order_id} is {order['state']}, not courier_booked")
        cur = await conn.execute(_SHORTFALL_EXISTS_SQL, {"order_id": order_id})
        if await cur.fetchone() is not None:
            return _error(409, f"order {order_id} already has a shortfall")
        if target != order["seller_agent_id"]:
            return _error(400, "target_agent_id must be the order's seller")
        try:
            event_id = await control.inject_event(
                conn, env=env, kind="shortfall", target_agent_id=target, params=params
            )
        except control.ControlRefused as exc:
            return _error(400, str(exc))
        source = ids.mock_event_id(event_id)
        await outbox.send(
            conn,
            message_id=ids.message_id(source, 0),
            run_id=order["run_id"],
            conversation_id=None,
            causation_id=None,
            from_agent=API_AGENT,
            to_agent=target,
            type="shortfall_detected",
            body={"order_id": str(order_id), "skus": params["skus"], "event_id": str(source)},
        )
    return JSONResponse({"event_id": event_id})


async def set_mode(request: Request) -> JSONResponse:
    vendor = request.path_params["vendor"]
    body = await _json_object(request)
    if body is None:
        return _error(400, "the body must be a JSON object")
    mode = body.get("mode")
    if not isinstance(mode, str):
        return _error(400, "mode is a required string")
    async with request.app.state.connect() as conn, conn.transaction():
        try:
            await control.set_mode(conn, vendor, mode)
        except control.ControlRefused as exc:
            return _error(400, str(exc))
    return JSONResponse({"vendor": vendor, "mode": mode})


async def expire_approval(request: Request) -> JSONResponse:
    approval_id = _uuid(request.path_params["approval_id"])
    if approval_id is None:
        return _error(404, "unknown approval")
    async with request.app.state.connect() as conn, conn.transaction():
        try:
            await control.expire_approval(conn, approval_id)
        except LookupError as exc:
            return _error(404, str(exc))
    return JSONResponse({"approval_id": str(approval_id), "expiring": True})


# --- live view ------------------------------------------------------------------------------

_LIVE_SQL = """
WITH recent AS (
  SELECT run_id, seq, ts, agent_id, kind, conversation_id, message_id, payload
  FROM agent_traces
  WHERE kind IN ('message', 'error') AND (%(run_id)s::uuid IS NULL OR run_id = %(run_id)s)
  ORDER BY ts DESC, seq DESC
  LIMIT %(limit)s
)
SELECT t.run_id, t.seq, t.ts, t.agent_id, t.kind, t.conversation_id, t.message_id,
       t.payload->>'from' AS from_agent, t.payload->>'to' AS to_agent,
       t.payload->>'type' AS type, t.payload->>'summary' AS summary,
       t.payload->>'causation_id' AS causation_id,
       t.payload->>'ref_message_id' AS ref_message_id,
       t.payload->>'where' AS where_, t.payload->>'reason' AS reason,
       t.payload->'attempt' AS attempt,
       m.conversation_id AS ref_conversation_id
FROM recent t
LEFT JOIN agent_messages m
  ON t.kind = 'error' AND m.message_id::text = t.payload->>'ref_message_id'
"""


def _live_row(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The row's lane and its fields. No body, rationale or free-text detail (§8)."""
    common = {"kind": row["kind"], "seq": row["seq"], "ts": _iso(row["ts"])}
    if row["kind"] == "message":
        conversation = row["conversation_id"]
        fields = {
            **common,
            "message_id": _str(row["message_id"]),
            "conversation_id": _str(conversation),
            "causation_id": row["causation_id"],
            "from": row["from_agent"],
            "to": row["to_agent"],
            "type": row["type"],
            "summary": row["summary"],
        }
    else:
        # An error row appears in the lane of its ref_message_id (§8).
        conversation = row["ref_conversation_id"] or row["conversation_id"]
        fields = {
            **common,
            "agent_id": row["agent_id"],
            "ref_message_id": row["ref_message_id"],
            "where": row["where_"],
            "reason": row["reason"],
            "attempt": row["attempt"],
        }
    return (HOUSEHOLD_LANE if conversation is None else str(conversation)), fields


async def runs_live(request: Request) -> JSONResponse:
    """`?run_id=&limit=`: the latest `limit` message and error rows, as runs of lanes."""
    raw_run = request.query_params.get("run_id")
    run_id = None if raw_run is None else _uuid(raw_run)
    if raw_run is not None and run_id is None:
        return _error(400, "run_id must be a uuid")
    try:
        limit = int(request.query_params.get("limit", LIVE_LIMIT))
    except ValueError:
        return _error(400, "limit must be an integer")
    if not 1 <= limit <= LIVE_LIMIT_MAX:
        return _error(400, f"limit must be 1 to {LIVE_LIMIT_MAX}")
    async with request.app.state.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_LIVE_SQL, {"run_id": run_id, "limit": limit})
        rows = await cur.fetchall()
    runs: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for row in sorted(rows, key=lambda r: (r["ts"], r["seq"])):
        lane, fields = _live_row(row)
        runs.setdefault(str(row["run_id"]), {}).setdefault(lane, []).append(fields)
    out = [
        {"run_id": run, "lanes": [{"lane": lane, "rows": rows} for lane, rows in lanes.items()]}
        for run, lanes in runs.items()
    ]
    return JSONResponse({"runs": out})


# --- message detail -------------------------------------------------------------------------

_MESSAGE_SQL = """
SELECT message_id, run_id, conversation_id, causation_id, from_agent, to_agent, type, body,
       status, attempts, last_error, processed_at
FROM agent_messages WHERE message_id = %s
"""

_QUOTE_SQL = """
SELECT quote_id, run_id, order_id, conversation_id, seller_agent_id, buyer_agent_id, status,
       expires_at, closed_by
FROM quotes WHERE quote_id = %s
"""


async def _quote(conn: AsyncConnection, body: dict[str, Any]) -> QuoteRow | None:
    quote_id = _uuid(body.get("quote_id")) if body.get("quote_id") else None
    if quote_id is None:
        return None
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_QUOTE_SQL, (quote_id,))
        row = await cur.fetchone()
    return None if row is None else QuoteRow(**row)


async def message_detail(request: Request) -> JSONResponse:
    message_id = _uuid(request.path_params["message_id"])
    if message_id is None:
        return _error(404, "unknown message")
    async with request.app.state.connect() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_MESSAGE_SQL, (message_id,))
            msg = await cur.fetchone()
        if msg is None:
            return _error(404, "unknown message")
        detail = msg["type"] in DETAIL_TYPES
        quote = await _quote(conn, msg["body"]) if detail else None
    out: dict[str, Any] = {
        "message_id": str(msg["message_id"]),
        "run_id": str(msg["run_id"]),
        "conversation_id": _str(msg["conversation_id"]),
        "causation_id": _str(msg["causation_id"]),
        "from": msg["from_agent"],
        "to": msg["to_agent"],
        "type": msg["type"],
        "summary": render_summary(msg["type"], msg["body"]),
        "status": msg["status"],
        "attempts": msg["attempts"],
        "last_error": msg["last_error"],
        "processed_at": _iso(msg["processed_at"]),
    }
    if detail:
        row = AgentMessageRow(
            message_id=msg["message_id"],
            run_id=msg["run_id"],
            conversation_id=msg["conversation_id"],
            causation_id=msg["causation_id"],
            from_agent=msg["from_agent"],
            to_agent=msg["to_agent"],
            type=msg["type"],
            body=msg["body"],
        )
        out["body"] = msg["body"]
        out["a2a"] = to_a2a_message(row, quote)
        out["rationale"] = msg["body"].get("rationale")
    return JSONResponse(out)


# --- eval results page (evals.md §14, EG7) --------------------------------------------------

# The case files the api image holds (infra/Dockerfile copies evals/).
EVAL_CASES_DIR = Path(__file__).resolve().parents[3] / "evals" / "cases"
EVAL_WINDOW_DAYS = 7
EVAL_STALE_HOURS = 48
NO_EVAL_RUNS = "No eval runs recorded yet"
STUB_HEADING = "Stub model (deterministic), not a measure of model behaviour"
_DEFAULT_MODEL_MODES = ("stub", "real")

# Every attempt with its eval run's fields. Rows are few (one per attempt), so the page
# picks "latest" per case in Python.
_EVAL_ROWS_SQL = """
SELECT r.case_id, r.repeat, r.category, r.model_mode, r.verdict, r.agent_run_ids,
       r.created_at, e.eval_run_id, e.trigger, e.git_sha, e.models, e.started_at,
       r.created_at >= now() - make_interval(days => %(days)s) AS in_window
FROM eval_results r JOIN eval_runs e ON e.eval_run_id = r.eval_run_id
ORDER BY r.created_at DESC, r.repeat DESC
"""

_EVAL_STALE_SQL = """
SELECT coalesce(max(started_at) < now() - make_interval(hours => %(hours)s), true)
FROM eval_runs WHERE model_mode = 'real'
"""


def _case_modes(cases_dir: Path) -> dict[str, list[str]]:
    """`{case id: model_modes}` for every case file (evals.md §4; default `[stub, real]`)."""
    modes: dict[str, list[str]] = {}
    for path in sorted(cases_dir.glob("*/*.yaml")):
        case = yaml.safe_load(path.read_text())
        if isinstance(case, dict):
            raw = case.get("model_modes", _DEFAULT_MODEL_MODES)
            modes[str(case.get("id", path.stem))] = [str(m) for m in raw]
    return modes


def _latest_per_case(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The first row per case id; `rows` are newest first."""
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        latest.setdefault(row["case_id"], row)
    return latest


def _last_green(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per case, the most recent real nightly where every repeat passed (evals.md §5, §14)."""
    runs: dict[tuple[str, uuid.UUID], dict[str, Any]] = {}
    for row in rows:
        if row["model_mode"] != "real" or row["trigger"] != "nightly":
            continue
        key = (row["case_id"], row["eval_run_id"])
        seen = runs.setdefault(key, {**row, "green": True})
        seen["green"] = seen["green"] and row["verdict"] == "pass"
    green: dict[str, dict[str, Any]] = {}
    for (case_id, _), run in sorted(runs.items(), key=lambda kv: kv[1]["started_at"]):
        if run["green"]:
            green[case_id] = run
    return green


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _date(value: datetime | None) -> str:
    return "" if value is None else value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _when(row: dict[str, Any] | None, at: str = "created_at") -> str:
    if row is None:
        return "never"
    return f"{_e(row['git_sha'])} {_e(_date(row[at]))}"


def _trace_link(row: dict[str, Any] | None) -> str:
    if row is None or not row["agent_run_ids"]:
        return ""
    run_id = _e(row["agent_run_ids"][0])
    return f'<a href="/panel/runs/live?run_id={run_id}">trace</a>'


def _verdict_table(latest: dict[str, dict[str, Any]]) -> str:
    head = "<tr><th>Case</th><th>Category</th><th>Verdict</th><th>SHA and date</th><th></th></tr>"
    body = "".join(
        f"<tr><td>{_e(case_id)}</td><td>{_e(row['category'])}</td><td>{_e(row['verdict'])}</td>"
        f"<td>{_when(row)}</td><td>{_trace_link(row)}</td></tr>"
        for case_id, row in sorted(latest.items())
    )
    return f"<table>{head}{body}</table>"


def _headline(rows: list[dict[str, Any]], case_modes: dict[str, list[str]], stale: bool) -> str:
    latest = _latest_per_case([r for r in rows if r["model_mode"] == "real" and r["in_window"]])
    real_capable = sum(1 for modes in case_modes.values() if "real" in modes)
    parts = [f"<h2>Real model, last {EVAL_WINDOW_DAYS} days</h2>"]
    if stale:
        parts.append(
            f'<p class="stale"><strong>stale</strong>: the newest real eval run is more than'
            f" {EVAL_STALE_HOURS} hours old</p>"
        )
    parts.append(
        f"<p>{len(latest)} of {real_capable} real-capable cases run in {EVAL_WINDOW_DAYS} days</p>"
    )
    if not latest:
        return "".join(parts)
    # Pass rates count only pass and fail; skipped counts in neither (evals.md §5).
    safety = [r["verdict"] for r in latest.values() if r["category"] == "safety"]
    passed, failed = safety.count("pass"), safety.count("fail")
    rate = (
        f"{passed} of {passed + failed} ({passed / (passed + failed):.0%})"
        if passed + failed
        else "no pass or fail attempts"
    )
    people = sorted({str((r["models"] or {}).get("people")) for r in latest.values()})
    business = sorted({str((r["models"] or {}).get("business")) for r in latest.values()})
    oldest = min(latest.values(), key=lambda r: r["created_at"])
    newest = max(latest.values(), key=lambda r: r["created_at"])
    parts += [
        f"<p>Safety pass rate: {_e(rate)}</p>",
        f"<p>Models: people {_e(', '.join(people))}; business {_e(', '.join(business))}</p>",
        f"<p>Oldest: {_when(oldest)}. Newest: {_when(newest)}.</p>",
        _verdict_table(latest),
    ]
    return "".join(parts)


def _stub_section(rows: list[dict[str, Any]]) -> str:
    nightly = [r for r in rows if r["model_mode"] == "stub" and r["trigger"] == "nightly"]
    parts = [f"<h2>{_e(STUB_HEADING)}</h2>"]
    if not nightly:
        return "".join([*parts, "<p>No stub nightly recorded.</p>"])
    run = max(nightly, key=lambda r: r["started_at"])["eval_run_id"]
    latest = _latest_per_case([r for r in nightly if r["eval_run_id"] == run])
    return "".join([*parts, _verdict_table(latest)])


def _case_table(rows: list[dict[str, Any]], case_modes: dict[str, list[str]]) -> str:
    by_mode = {
        mode: _latest_per_case([r for r in rows if r["model_mode"] == mode])
        for mode in _DEFAULT_MODEL_MODES
    }
    green = _last_green(rows)
    head = (
        "<tr><th>Case</th><th>Category</th><th>Modes</th><th>Last stub</th><th>Last real</th>"
        "<th>Last green</th></tr>"
    )
    body = []
    for case_id in sorted({r["case_id"] for r in rows}):
        seen = sorted({r["model_mode"] for r in rows if r["case_id"] == case_id})
        modes = case_modes.get(case_id, seen)
        category = next(r["category"] for r in rows if r["case_id"] == case_id)
        cells = []
        for mode in _DEFAULT_MODEL_MODES:
            row = by_mode[mode].get(case_id)
            cells.append(
                "<td>never</td>"
                if row is None
                else f"<td>{_e(row['verdict'])} {_when(row)} {_trace_link(row)}</td>"
            )
        body.append(
            f"<tr><td>{_e(case_id)}</td><td>{_e(category)}</td><td>{_e(', '.join(modes))}</td>"
            f"{''.join(cells)}<td>{_when(green.get(case_id), 'started_at')}</td></tr>"
        )
    return f"<h2>Cases</h2><table>{head}{''.join(body)}</table>"


def _page(content: str) -> HTMLResponse:
    return HTMLResponse(
        '<!doctype html><html><head><meta charset="utf-8"><title>Evals</title></head>'
        f"<body><h1>Evals</h1>{content}</body></html>"
    )


async def evals_page(request: Request) -> HTMLResponse:
    """`GET /panel/evals` (evals.md §14): HTML, no JavaScript."""
    async with request.app.state.connect() as conn:
        try:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(_EVAL_ROWS_SQL, {"days": EVAL_WINDOW_DAYS})
                rows = await cur.fetchall()
                await cur.execute(_EVAL_STALE_SQL, {"hours": EVAL_STALE_HOURS})
                stale_row = await cur.fetchone()
        except errors.UndefinedTable:
            return _page(f"<p>{NO_EVAL_RUNS}</p>")
    if not rows:
        return _page(f"<p>{NO_EVAL_RUNS}</p>")
    stale = bool(stale_row is None or next(iter(stale_row.values())))
    case_modes = _case_modes(EVAL_CASES_DIR)
    return _page(
        _headline(rows, case_modes, stale) + _stub_section(rows) + _case_table(rows, case_modes)
    )


# --- experience metric (experience §12, EX-I22) ---------------------------------------------

# Field order of `guards`, as in the `experience_weekly_guards` view.
GUARD_METRICS = (
    "approvals_expired",
    "orders_ended",
    "rejects",
    "safety_refusals",
    "revokes",
    "held_dropped",
    "sends_not_delivered",
)

# The household and its current week, in its own time zone (the views' `week` expression).
_EXPERIENCE_HOUSEHOLD_SQL = """
SELECT household_id, date_trunc('week', now() AT TIME ZONE timezone)::date AS week
FROM households WHERE household_id = %s
"""

# Every adult of the household with their asks for `week` (0 when the view has no row).
_EXPERIENCE_ASKS_SQL = """
SELECT u.user_id, u.display_name, coalesce(a.asks, 0) AS asks
FROM household_members hm
JOIN users u ON u.user_id = hm.user_id
LEFT JOIN experience_weekly_asks a
  ON a.household_id = hm.household_id AND a.user_id = hm.user_id AND a.week = %(week)s
WHERE hm.household_id = %(household)s AND hm.role IN ('admin', 'member')
ORDER BY u.display_name, u.user_id
"""

_EXPERIENCE_GUARDS_SQL = f"""
SELECT {", ".join(GUARD_METRICS)}
FROM experience_weekly_guards WHERE household_id = %(household)s AND week = %(week)s
"""

_EXPERIENCE_HELD_SQL = """
SELECT count(*) AS n
FROM held_messages x JOIN household_members hm ON hm.user_id = x.user_id
WHERE hm.household_id = %s AND x.status = 'pending'
"""


async def _experience_week(cur: Any, household: str, week: Any) -> dict[str, Any]:
    args = {"household": household, "week": week}
    await cur.execute(_EXPERIENCE_ASKS_SQL, args)
    per_user = [
        {"user_id": str(r["user_id"]), "display_name": r["display_name"], "asks": int(r["asks"])}
        for r in await cur.fetchall()
    ]
    await cur.execute(_EXPERIENCE_GUARDS_SQL, args)
    row = await cur.fetchone()
    guards = {m: int(row[m]) if row else 0 for m in GUARD_METRICS}
    return {"week": week.isoformat(), "per_user": per_user, "guards": guards}


def _experience_page(data: dict[str, Any]) -> HTMLResponse:
    """The three lines (EX-I22): asks per adult this week and last; guard totals this week;
    pending held items and configured templates."""
    this, last = data["weeks"]
    last_asks = {u["user_id"]: u["asks"] for u in last["per_user"]}
    asks = "; ".join(
        f"{_e(u['display_name'])} {u['asks']} this week, {last_asks.get(u['user_id'], 0)} last week"
        for u in this["per_user"]
    )
    guards = ", ".join(f"{m} {this['guards'][m]}" for m in GUARD_METRICS)
    templates = ", ".join(_e(k) for k in data["templates_configured"]) or "none"
    lines = (
        f"<p>Asks per adult: {asks or 'no adults'}</p>",
        f"<p>Guards this week ({_e(this['week'])}): {guards}</p>",
        f"<p>Held items pending: {data['held_pending']}. Templates configured: {templates}</p>",
    )
    return HTMLResponse(
        '<!doctype html><html><head><meta charset="utf-8"><title>Experience</title></head>'
        f"<body><h1>Experience</h1>{''.join(lines)}</body></html>"
    )


async def experience(request: Request) -> Response:
    """`GET /panel/experience?household=` (experience §12, EX-I22). JSON, or the three-line
    page when the client asks for HTML. An unknown household is 404."""
    household = request.query_params.get("household")
    if not household:
        return _error(400, "household is required")
    async with request.app.state.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_EXPERIENCE_HOUSEHOLD_SQL, (household,))
        found = await cur.fetchone()
        if found is None:
            return _error(404, "unknown household")
        this_week = found["week"]
        weeks = [
            await _experience_week(cur, household, this_week),
            await _experience_week(cur, household, this_week - timedelta(days=7)),
        ]
        await cur.execute(_EXPERIENCE_HELD_SQL, (household,))
        held = await cur.fetchone()
    settings = get_settings()
    data = {
        "weeks": weeks,
        "held_pending": int(held["n"]) if held else 0,
        "templates_configured": [
            key for key in templates.TEMPLATES if templates.meta_name(key, settings)
        ],
    }
    if "text/html" in request.headers.get("accept", ""):
        return _experience_page(data)
    return JSONResponse(data)


routes = [
    Route("/panel/reset", reset, methods=["POST"]),
    Route("/panel/events", events, methods=["POST"]),
    Route("/panel/modes/{vendor}", set_mode, methods=["PUT"]),
    Route("/panel/approvals/{approval_id}/expire", expire_approval, methods=["POST"]),
    Route("/panel/runs/live", runs_live, methods=["GET"]),
    Route("/panel/messages/{message_id}", message_detail, methods=["GET"]),
    Route("/panel/evals", evals_page, methods=["GET"]),
    Route("/panel/experience", experience, methods=["GET"]),
]
