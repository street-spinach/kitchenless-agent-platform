"""Control panel routes in `api` (architecture item 39, agent-protocol §8 and P31).

`/panel/*` sits behind Caddy basic auth. The routes call the mock control functions in
`services.mocks.control` (architecture item 47) and read traces and messages for the
"watch the agents talk" view:

*   `POST /panel/reset {env}`: `env: eval` only; refuses anything else with 400.
*   `POST /panel/events {env, kind, target_agent_id, params}` writes `mock_events`. The
    `shortfall` kind (agent-protocol P31, shortfall event) is refused with 501 until its
    route checks and `shortfall_detected` land (#70).
*   `PUT /panel/modes/{vendor} {mode}` writes `adapter_modes`.
*   `POST /panel/approvals/{id}/expire` makes that approval's expiry timer due now.
*   `GET /panel/runs/live` returns recent `message` and `error` trace rows, grouped per run
    into lanes by `conversation_id` (§8). Rows carry the summary, never the body.
*   `GET /panel/messages/{message_id}` returns the delivery fields of one message and, for
    the S1-S7 types only, the body, the A2A form and the rationale (§8). People and system
    types (S8) show the summary only, because their bodies can hold profile data.
"""

import uuid
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from services.common.protocol import schemas
from services.common.protocol.a2a import AgentMessageRow, QuoteRow, to_a2a_message
from services.common.protocol.catalog import render_summary
from services.mocks import control

__all__ = ["DETAIL_TYPES", "HOUSEHOLD_LANE", "LIVE_LIMIT", "LIVE_LIMIT_MAX", "routes"]

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
    async with request.app.state.connect() as conn:
        try:
            deleted = await control.reset(conn, env)
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
        return _error(501, "shortfall events are not available yet")
    async with request.app.state.connect() as conn, conn.transaction():
        try:
            event_id = await control.inject_event(
                conn, env=env, kind=kind, target_agent_id=target, params=params
            )
        except control.ControlRefused as exc:
            return _error(400, str(exc))
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


routes = [
    Route("/panel/reset", reset, methods=["POST"]),
    Route("/panel/events", events, methods=["POST"]),
    Route("/panel/modes/{vendor}", set_mode, methods=["PUT"]),
    Route("/panel/approvals/{approval_id}/expire", expire_approval, methods=["POST"]),
    Route("/panel/runs/live", runs_live, methods=["GET"]),
    Route("/panel/messages/{message_id}", message_detail, methods=["GET"]),
]
