"""Internal routes: the simulated WhatsApp channel and the mock courier's status posts
(architecture item 35).

Caddy does not route `/internal/*`.
"""

from typing import Any

from psycopg.rows import dict_row
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from services.api.ingest import Content, ingest
from services.api.routes.delivery import UnknownStatus, apply_delivery_status

__all__ = ["SIM_CHANNEL", "routes"]

# Simulated messages dedupe with real WhatsApp ones: they stand in for the same channel.
SIM_CHANNEL = "whatsapp"
OUTBOX_LIMIT = 100


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _json_object(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _content(body: dict[str, Any]) -> Content | None:
    text, button = body.get("text"), body.get("button")
    if (text is None) == (button is None):
        return None
    if text is not None:
        return Content("text", text=text) if isinstance(text, str) else None
    return Content("button", button=button) if isinstance(button, str) else None


async def sim_whatsapp(request: Request) -> JSONResponse:
    """`{phone, text | button, external_id}`: only phones with `channel_mode='sim'`."""
    body = await _json_object(request)
    if body is None:
        return _error(400, "the body must be a JSON object")
    phone, external_id = body.get("phone"), body.get("external_id")
    content = _content(body)
    if not isinstance(phone, str) or not isinstance(external_id, str) or not external_id:
        return _error(400, "phone and external_id are required strings")
    if content is None:
        return _error(400, "exactly one of text or button is required")
    async with request.app.state.connect() as conn:
        cur = await conn.execute(
            "SELECT channel_mode FROM channel_identities WHERE phone_e164 = %s", (phone,)
        )
        row = await cur.fetchone()
        if row is None or row[0] != "sim":
            return _error(403, "only simulated phones can use this route")
        run_id = await ingest(conn, SIM_CHANNEL, external_id, phone, content)
    return JSONResponse({"run_id": None if run_id is None else str(run_id)})


def _phone_param(raw: str | None) -> str | None:
    """The E.164 `phone` query value. An unencoded `+` decodes to a space; E.164 has no
    spaces, so a leading space can only be that `+`."""
    if raw is not None and raw.startswith(" "):
        return "+" + raw[1:]
    return raw


async def sim_outbox(request: Request) -> JSONResponse:
    """`?phone=&after_id=`: the phone's `sim_outbound` rows after `after_id`, oldest first."""
    phone = _phone_param(request.query_params.get("phone"))
    if not phone:
        return _error(400, "phone is required")
    try:
        after_id = int(request.query_params.get("after_id", "0"))
    except ValueError:
        return _error(400, "after_id must be an integer")
    async with request.app.state.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, run_id, body, created_at FROM sim_outbound"
            " WHERE phone_e164 = %s AND id > %s ORDER BY id LIMIT %s",
            (phone, after_id, OUTBOX_LIMIT),
        )
        rows = await cur.fetchall()
    return JSONResponse(
        {
            "messages": [
                {
                    "id": r["id"],
                    "run_id": None if r["run_id"] is None else str(r["run_id"]),
                    "body": r["body"],
                    "created_at": r["created_at"].isoformat(),
                }
                for r in rows
            ]
        }
    )


async def delivery_status(request: Request) -> JSONResponse:
    """`{external_delivery_id, status, event_id}` from the mock courier (item 46).

    Repeats are harmless because `apply_delivery_status` only moves forward, so the post is
    not deduplicated. A known delivery always gets a 2xx, even when nothing changes, so the
    mock courier moves on; an unknown delivery gets 404.
    """
    body = await _json_object(request)
    if body is None:
        return _error(400, "the body must be a JSON object")
    edi, status, event_id = (body.get(k) for k in ("external_delivery_id", "status", "event_id"))
    if not all(isinstance(v, str) and v for v in (edi, status, event_id)):
        return _error(400, "external_delivery_id, status and event_id are required strings")
    async with request.app.state.connect() as conn:
        try:
            result = await apply_delivery_status(conn, edi, status)
        except UnknownStatus:
            return _error(400, f"unknown courier status {status!r}")
    if result == "unknown_delivery":
        return _error(404, "no order has this delivery")
    return JSONResponse({"result": result})


routes = [
    Route("/internal/sim/whatsapp", sim_whatsapp, methods=["POST"]),
    Route("/internal/sim/outbox", sim_outbox, methods=["GET"]),
    Route("/internal/delivery-status", delivery_status, methods=["POST"]),
]
