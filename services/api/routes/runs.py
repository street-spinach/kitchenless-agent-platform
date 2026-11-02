"""`POST /runs` and `GET /runs/{run_id}` (architecture item 36, amended by agent-protocol
A21 and P32). Internal: the eval runner and the control panel call them.

A run is started by `ingest` with a simulated message from the household's first member,
so it follows the same path as a WhatsApp message. Only the allowed `overrides` are stored,
in `runs.trigger.overrides`; any other key refuses the request.

`GET /runs/{run_id}` names the run's seller and courier with
`vendors.vendor_label(area, card, org_name, runs.modes).text` (adapters §6), or `null`
while the run's order has none.
"""

import uuid
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from services.api.ingest import Content, ingest
from services.common import directory, ids, vendors

__all__ = ["ALLOWED_OVERRIDES", "RUNS_CHANNEL", "SCENARIOS", "routes"]

RUNS_CHANNEL = "runs"
SCENARIOS = {"weekly_grocery_order": "weekly", "busy_night_dinner": "dinner"}
PAIR_SCENARIO = "weekly_grocery_order_pair"

ALLOWED_OVERRIDES = frozenset(
    {
        # Architecture item 36.
        "APPROVAL_TTL_SECONDS",
        "STUB_FAIL_NODE",
        "STUB_OUT_OF_BOUNDS",
        "STUB_DUPLICATE_MESSAGE",
        # Agent-protocol P32.
        "OFFER_TTL_SECONDS",
        "SHORTFALL_REPLY_SECONDS",
        "STUB_ACCEPT_DELAY_ROUNDS",
        "FAIL_FIRST_ATTEMPT",
        "CRASH_BEFORE_FINISH",
        "STUB_SHORTFALL_UNSAFE_SUBSTITUTE",
    }
)

# The household's first member with a phone: the admin first, then in the order added.
_FIRST_MEMBER_SQL = """
SELECT ci.phone_e164
FROM household_members hm
JOIN channel_identities ci ON ci.user_id = hm.user_id
WHERE hm.household_id = %s
ORDER BY hm.role = 'admin' DESC, hm.created_at, ci.created_at, ci.phone_e164
LIMIT 1
"""

_OUTCOME_SQL = """
SELECT payload->>'summary' AS summary FROM agent_traces
WHERE run_id = %s AND kind = 'outcome' ORDER BY seq DESC LIMIT 1
"""


# The run's latest order: its seller and courier cards (adapters §6).
_ORDER_SQL = """
SELECT seller_agent_id, delivery_agent_id FROM orders
WHERE run_id = %s ORDER BY created_at DESC, order_id LIMIT 1
"""

_ORG_NAME_SQL = "SELECT name FROM organizations WHERE 'org:' || org_id = %s"


class _Refused(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _overrides(raw: object) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _Refused(400, "overrides must be an object")
    unknown = sorted(set(raw) - ALLOWED_OVERRIDES)
    if unknown:
        raise _Refused(400, f"overrides not allowed: {', '.join(unknown)}")
    for key, value in raw.items():
        if not isinstance(value, str | int | bool):
            raise _Refused(400, f"override {key} must be a string, integer or boolean")
    return dict(raw)


async def _first_member_phone(conn: AsyncConnection, household_id: object) -> str:
    """The phone to simulate. Refuses unknown and `live` households.

    The control panel exception for `live` households waits for the panel routes (#74); until
    then every `live` household is refused.
    """
    if not isinstance(household_id, str) or not household_id:
        raise _Refused(400, "household_id is required")
    cur = await conn.execute("SELECT env FROM households WHERE household_id = %s", (household_id,))
    row = await cur.fetchone()
    if row is None:
        raise _Refused(404, f"unknown household {household_id}")
    if row[0] != "eval":
        raise _Refused(403, "runs for live households are refused")
    cur = await conn.execute(_FIRST_MEMBER_SQL, (household_id,))
    phone = await cur.fetchone()
    if phone is None:
        raise _Refused(409, f"household {household_id} has no member with a phone")
    return phone[0]


async def _start(
    conn: AsyncConnection, phone: str, kind: str, text: str, overrides: dict[str, Any]
) -> uuid.UUID:
    run_id = ids.new_run_id()
    started = await ingest(
        conn,
        RUNS_CHANNEL,
        str(run_id),
        phone,
        Content("text", text=text),
        kind=kind,
        overrides=overrides,
        run_id=run_id,
    )
    if started is None:
        raise _Refused(409, "the household's first member has no consumer agent")
    return started


async def _label(
    conn: AsyncConnection, agent_id: str | None, run_modes: dict[str, str] | None
) -> str | None:
    """The label text of card `agent_id` in a run pinned to `run_modes` (adapters §6)."""
    if agent_id is None:
        return None
    card = await directory.card(conn, agent_id)
    cur = await conn.execute(_ORG_NAME_SQL, (card.owner_ref,))
    org = await cur.fetchone()
    org_name = org[0] if org else card.owner_ref
    return vendors.vendor_label(card.agent_class, card, org_name, run_modes or {}).text


async def start_run(request: Request) -> JSONResponse:
    """`{scenario, household_id, input, overrides?}` returns `{run_id}`."""
    try:
        body = await request.json()
    except ValueError:
        return _error(400, "the body must be a JSON object")
    if not isinstance(body, dict):
        return _error(400, "the body must be a JSON object")
    scenario = body.get("scenario")
    text = body.get("input")
    try:
        if scenario not in SCENARIOS and scenario != PAIR_SCENARIO:
            raise _Refused(400, f"unknown scenario {scenario!r}")
        if not isinstance(text, str) or not text.strip():
            raise _Refused(400, "input must be the member's message text")
        overrides = _overrides(body.get("overrides"))
        kind = SCENARIOS.get(scenario, "weekly")
        async with request.app.state.connect() as conn, conn.transaction():
            if scenario != PAIR_SCENARIO:
                phone = await _first_member_phone(conn, body.get("household_id"))
                run_id = await _start(conn, phone, kind, text, overrides)
                return JSONResponse({"run_id": str(run_id)})
            pair = body.get("household_ids")
            if not isinstance(pair, list) or len(pair) != 2 or pair[0] == pair[1]:
                raise _Refused(400, "household_ids must be two different households")
            phones = [await _first_member_phone(conn, hid) for hid in pair]
            run_ids = [await _start(conn, phone, kind, text, overrides) for phone in phones]
            return JSONResponse({"run_id": str(run_ids[0]), "other_run_id": str(run_ids[1])})
    except _Refused as refused:
        return _error(refused.status, refused.message)


async def get_run(request: Request) -> JSONResponse:
    """Status, reason, outcome summary, and the seller and courier labels."""
    try:
        run_id = uuid.UUID(request.path_params["run_id"])
    except ValueError:
        return _error(404, "unknown run")
    async with request.app.state.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT status, reason, modes FROM runs WHERE run_id = %s", (run_id,))
        run = await cur.fetchone()
        if run is None:
            return _error(404, "unknown run")
        await cur.execute(_OUTCOME_SQL, (run_id,))
        outcome = await cur.fetchone()
        await cur.execute(_ORDER_SQL, (run_id,))
        order = await cur.fetchone() or {}
        seller = await _label(conn, order.get("seller_agent_id"), run["modes"])
        courier = await _label(conn, order.get("delivery_agent_id"), run["modes"])
    return JSONResponse(
        {
            "run_id": str(run_id),
            "status": run["status"],
            "reason": run["reason"],
            "summary": None if outcome is None else outcome["summary"],
            "seller": seller,
            "courier": courier,
        }
    )


routes = [
    Route("/runs", start_run, methods=["POST"]),
    Route("/runs/{run_id}", get_run, methods=["GET"]),
]
