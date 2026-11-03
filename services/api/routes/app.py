"""The owner app's `/app/*` routes (identity-authz §10 and I32).

*   Every request carries the owner app's bearer token, validated by
    `tokens.validate_owner` (RS256, the active issuer, audience `owner-app`, expiry).
*   The organisation is the `org_id` query parameter. The caller must pass the OpenFGA
    check `user:<sub> owner org:<org_id>` before anything is read or changed; an OpenFGA
    outage answers 503, never a pass.
*   `GET /app/rules` returns `{org_id, max_discount_pct}` (null when no rule is set).
*   `POST /app/rules` with `{max_discount_pct: int 0-100}` writes a `business_rules` row of
    kind `max_discount_pct` for each of the org's grocery and restaurant cards, and one
    `owner_rule_changed` audit row. The rule raises the handlers' floors (I33).

Other `/app/*` paths stay reserved (`reserved.py`) until the experience design builds them.
"""

import asyncio
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from services.identity import audit, fga, tokens
from services.identity.errors import AuthzUnavailable, IdentityUnavailable, TokenRefused

__all__ = ["RULE_KIND", "routes"]

RULE_KIND = "max_discount_pct"
SELLER_CLASSES = ("grocery", "restaurant")
_BEARER = "bearer "


def _bearer(request: Request) -> str:
    value = request.headers.get("authorization", "")
    return value[len(_BEARER) :].strip() if value.lower().startswith(_BEARER) else ""


def _rule_value(body: Any) -> int | None:
    if not isinstance(body, dict) or set(body) != {RULE_KIND}:
        return None
    value = body[RULE_KIND]
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        return None
    return value


async def _seller_agents(conn: AsyncConnection, org_id: str) -> list[str]:
    cur = await conn.execute(
        "SELECT agent_id FROM agent_cards WHERE owner_ref = %s AND class = ANY(%s)"
        " ORDER BY agent_id",
        (f"org:{org_id}", list(SELLER_CLASSES)),
    )
    return [r[0] for r in await cur.fetchall()]


async def _current(conn: AsyncConnection, agents: list[str]) -> int | None:
    cur = await conn.execute(
        "SELECT max(value::int) FROM business_rules"
        " WHERE agent_id = ANY(%s) AND kind = %s AND jsonb_typeof(value) = 'number'",
        (agents, RULE_KIND),
    )
    row = await cur.fetchone()
    return None if row is None or row[0] is None else int(row[0])


async def rules(request: Request) -> Response:
    try:
        sub = tokens.validate_owner(_bearer(request))
    except TokenRefused as e:
        return JSONResponse({"error": e.reason}, status_code=401)
    except IdentityUnavailable:
        return JSONResponse({"error": "identity_unavailable"}, status_code=503)
    org_id = request.query_params.get("org_id", "")
    if not org_id:
        return JSONResponse({"error": "org_id is required"}, status_code=400)
    value: int | None = None
    if request.method == "POST":
        try:
            body = await request.json()
        except ValueError:
            body = None
        value = _rule_value(body)
        if value is None:
            return JSONResponse(
                {"error": f"{RULE_KIND} must be an integer from 0 to 100"}, status_code=400
            )
    # §10: `user owner org:<o>` before anything else.
    try:
        allowed, _ = await asyncio.to_thread(fga.check, f"user:{sub}", "owner", f"org:{org_id}")
    except AuthzUnavailable:
        return JSONResponse({"error": "authz_unavailable"}, status_code=503)
    if not allowed:
        return JSONResponse({"error": "authz_denied"}, status_code=403)

    async with request.app.state.connect() as conn:
        agents = await _seller_agents(conn, org_id)
        if not agents:
            return JSONResponse({"error": "no_seller_agents"}, status_code=404)
        current = await _current(conn, agents)
        if request.method != "POST":
            return JSONResponse({"org_id": org_id, RULE_KIND: current})
        async with conn.transaction():
            for agent_id in agents:
                await conn.execute(
                    "INSERT INTO business_rules (agent_id, kind, value) VALUES (%s, %s, %s)"
                    " ON CONFLICT (agent_id, kind)"
                    " DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                    (agent_id, RULE_KIND, Jsonb(value)),
                )
            await audit.record(
                conn,
                "owner_rule_changed",
                org_id=org_id,
                detail={
                    "rule": RULE_KIND,
                    "from": current,
                    "to": value,
                    "agents": agents,
                    "owner_sub": sub,
                },
            )
    return JSONResponse({"org_id": org_id, RULE_KIND: value})


routes = [Route("/app/rules", rules, methods=["GET", "POST"])]
