"""The `/me` pages (identity-authz §6, §9, §11, §13 and I24). Minimal HTML and JSON; the
visual design is the experience design's.

*   `GET /me/l/{token}` renders a confirm page and changes nothing (HEAD likewise), so link
    previews and scanners cannot use up a link.
*   `POST /me/l/{token}` calls `links.use`, then `keycloak_admin.magic_link` for the target
    person, and redirects to that sign-in to `me-pages`.
*   `GET /me/callback` verifies the signed `state`, which carries the link's fields, the
    agent's position and a one-time nonce, so a callback can't be replayed or reordered.
    For `me-pages` it redeems the code (`private_key_jwt`), checks that the ID token's
    `sub` is the link's target person and sets the session cookie; then, for a consent
    link, it redirects to the first agent's authorization request with `prompt=consent`.
    For an agent client it redeems the code, calls `grants.create`, and redirects to the
    next agent or to the done page. A declined agent gets no grant and is listed as
    skipped.
*   `GET /me/consent/done` shows the decoded claims of one `TokenSource.sample_token`,
    without the signature.
*   `GET /me/agents` lists the person's agents and grants; `POST
    /me/agents/{agent_id}/revoke` applies the §11 rule, then calls `grants.revoke` and
    `grants.after_revoke`.
*   `GET /me/history` lists the person's audit rows and approvals.
*   `GET /me/household/band` returns the band; `POST /me/household/band` with
    `{per_order_minor: int | null}` sets it (admins only) and writes `band_changed`.

The session cookie is signed (`SESSION_SECRET`), HttpOnly, Secure, SameSite=Lax and lasts
30 minutes (§13). Every household read goes through `scope.scoped` with the session's
scope (§8).
"""

import html
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import jwt
from itsdangerous import BadSignature, URLSafeTimedSerializer
from psycopg import AsyncConnection
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from services.common import ids
from services.common.config import get_settings
from services.identity import audit, grants, keycloak_admin, links, token_source
from services.identity.errors import DependencyUnavailable, IdentityError, LinkInvalid
from services.safety import scope

__all__ = ["SESSION_COOKIE", "SESSION_SECONDS", "STATE_SECONDS", "Session", "routes"]

SESSION_COOKIE = "me_session"
SESSION_SECONDS = 30 * 60
# A consent chain must finish within a link's lifetime.
STATE_SECONDS = links.LINK_SECONDS
REVOKE_REASON = "user"

# Nonces of states already used, with when they stop mattering (their signature expires).
# `api` is one process, so this is the whole record.
_spent_states: dict[str, float] = {}


@dataclass(frozen=True)
class Session:
    user_id: UUID
    household_id: str


# --- signing --------------------------------------------------------------------------


def _serializer(salt: str) -> URLSafeTimedSerializer:
    secret = get_settings().session_secret
    if not secret:
        raise RuntimeError("SESSION_SECRET is required")
    return URLSafeTimedSerializer(secret, salt=salt)


def _sign(salt: str, value: dict[str, Any]) -> str:
    return _serializer(salt).dumps(value)


def _unsign(salt: str, value: str | None, max_age: int) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        data = _serializer(salt).loads(value, max_age=max_age)
    except BadSignature:
        return None
    return data if isinstance(data, dict) else None


def _spend(nonce: str) -> bool:
    """True the first time a state nonce is seen; False on a replay."""
    now = time.time()
    for old in [n for n, until in _spent_states.items() if until < now]:
        del _spent_states[old]
    if nonce in _spent_states:
        return False
    _spent_states[nonce] = now + STATE_SECONDS
    return True


def _session(request: Request) -> Session | None:
    data = _unsign("me-session", request.cookies.get(SESSION_COOKIE), SESSION_SECONDS)
    if data is None:
        return None
    try:
        return Session(UUID(data["u"]), str(data["h"]))
    except (KeyError, ValueError):
        return None


def _set_session(resp: Response, user_id: UUID, household_id: str) -> None:
    resp.set_cookie(
        SESSION_COOKIE,
        _sign("me-session", {"u": str(user_id), "h": household_id}),
        max_age=SESSION_SECONDS,
        path="/me",
        secure=True,
        httponly=True,
        samesite="lax",
    )


# --- pages ----------------------------------------------------------------------------


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
        f"</head><body><h1>{html.escape(title)}</h1>{body}</body></html>",
        status_code=status_code,
    )


def _signed_out() -> HTMLResponse:
    return _page("Signed out", "<p>Open the newest link we sent you to sign in.</p>", 401)


def _bad_state() -> HTMLResponse:
    return _page("This sign-in has expired", "<p>Ask for a new link.</p>", 400)


def _callback_uri() -> str:
    return f"https://{get_settings().domain}/me/callback"


def _connect(request: Request) -> Any:
    return request.app.state.connect()


async def _keycloak_sub(conn: AsyncConnection, user_id: UUID) -> str | None:
    cur = await conn.execute("SELECT keycloak_sub FROM users WHERE user_id = %s", (user_id,))
    row = await cur.fetchone()
    return str(row[0]) if row and row[0] else None


async def _role(q: scope.Scoped, user_id: UUID) -> str | None:
    rows = await q.fetch(
        "SELECT role FROM household_members WHERE household_id = %(household)s"
        " AND user_id = %(user)s",
        user=user_id,
    )
    return rows[0][0] if rows else None


# --- links ----------------------------------------------------------------------------


async def link(request: Request) -> Response:
    token = request.path_params["token"]
    if request.method != "POST":
        # GET and HEAD never change state (§6).
        action = html.escape(f"/me/l/{token}", quote=True)
        return _page(
            "Sign in to Kitchenless",
            f"<form method='post' action='{action}'><button type='submit'>Continue</button></form>",
        )
    async with _connect(request) as conn:
        try:
            used = await links.use(conn, token)
        except LinkInvalid:
            used = None  # `link_rejected` is committed with the connection
        sub = None if used is None else await _keycloak_sub(conn, used.target_user_id)
    if used is None:
        return _page("This link has expired", "<p>Ask for a new link.</p>", 410)
    if sub is None:
        return _page("Not ready yet", "<p>This person can't sign in yet.</p>", 409)
    state = _sign(
        "me-state",
        {
            "n": ids.new_token_id(),
            "p": used.purpose,
            "t": str(used.target_user_id),
            "s": str(used.sent_to_user_id),
            "h": used.household_id,
            "a": list(used.agent_ids),
            "i": None,
            "g": [],
            "k": [],
        },
    )
    try:
        username = await keycloak_admin.username(sub)
        sign_in = await keycloak_admin.magic_link(username, _callback_uri(), state)
    except (DependencyUnavailable, keycloak_admin.KeycloakError):
        return _page("Sign-in is unavailable", "<p>Try again with a new link soon.</p>", 503)
    return RedirectResponse(sign_in, status_code=303)


# --- callback -------------------------------------------------------------------------


def _agent_authorize(state: dict[str, Any]) -> RedirectResponse:
    """The authorization request for agent `state["i"]` (§6): `prompt=consent`, the
    delegation scope and the class scopes. The Keycloak session means no new login."""
    agent_id = state["a"][state["i"]]
    client_id = ids.keycloak_client_id(agent_id)
    agent_kind = ids.parse_agent_id(agent_id)[0]
    scopes = [
        "openid",
        "offline_access",
        f"delegation:client:{client_id}",
        *grants.CLASS_SCOPES[agent_kind],
    ]
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": _callback_uri(),
            "scope": " ".join(scopes),
            "prompt": "consent",
            "state": _sign("me-state", {**state, "n": ids.new_token_id()}),
        }
    )
    return RedirectResponse(
        f"{get_settings().issuer_url}/protocol/openid-connect/auth?{query}", status_code=302
    )


def _next(state: dict[str, Any], index: int) -> RedirectResponse:
    if index < len(state["a"]):
        return _agent_authorize({**state, "i": index})
    done = _sign("me-done", {"g": state["g"], "k": state["k"], "u": state["t"]})
    return RedirectResponse(f"/me/consent/done?{urlencode({'r': done})}", status_code=302)


async def _sign_in_callback(request: Request, state: dict[str, Any]) -> Response:
    code = request.query_params.get("code")
    if not code:
        return _bad_state()
    target = UUID(state["t"])
    try:
        tokens = await keycloak_admin.redeem_code(keycloak_admin.ME_PAGES, code, _callback_uri())
    except (DependencyUnavailable, keycloak_admin.KeycloakError):
        return _page("Sign-in is unavailable", "<p>Try again with a new link soon.</p>", 503)
    # Received straight from the token endpoint over the back channel (OIDC Core 3.1.3.7).
    try:
        id_claims = jwt.decode(str(tokens.get("id_token", "")), options={"verify_signature": False})
    except jwt.PyJWTError:
        return _page("Sign-in failed", "<p>Ask for a new link.</p>", 403)
    aud = id_claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    async with _connect(request) as conn:
        expected = await _keycloak_sub(conn, target)
    if (
        expected is None
        or id_claims.get("sub") != expected
        or keycloak_admin.ME_PAGES not in auds
        or id_claims.get("iss") != get_settings().issuer_url
    ):
        return _page("Sign-in failed", "<p>This link was for someone else.</p>", 403)
    if state["p"] == "consent":
        resp: Response = _next(state, 0)
    else:
        resp = RedirectResponse("/me/agents", status_code=302)
    _set_session(resp, target, state["h"])
    return resp


async def _agent_callback(request: Request, state: dict[str, Any], index: int) -> Response:
    session = _session(request)
    if session is None or str(session.user_id) != state["t"] or session.household_id != state["h"]:
        return _page("Sign-in failed", "<p>Open the newest link we sent you.</p>", 403)
    agents: list[str] = state["a"]
    if not 0 <= index < len(agents):
        return _bad_state()
    agent_id = agents[index]
    if request.query_params.get("error"):
        # Declined (or refused) on Keycloak's consent screen: no grant; go on (§6).
        return _next({**state, "k": [*state["k"], agent_id]}, index + 1)
    code = request.query_params.get("code")
    if not code:
        return _bad_state()
    try:
        tokens = await keycloak_admin.redeem_code(
            ids.keycloak_client_id(agent_id), code, _callback_uri()
        )
    except (DependencyUnavailable, keycloak_admin.KeycloakError):
        return _page("Consent is unavailable", "<p>Try again with a new link soon.</p>", 503)
    offline_token = tokens.get("refresh_token")
    if not offline_token:
        return _page("Consent failed", "<p>Try again with a new link.</p>", 502)
    agent_kind = ids.parse_agent_id(agent_id)[0]
    async with _connect(request) as conn:
        await grants.create(
            conn,
            agent_id=agent_id,
            agent_kind=agent_kind,
            user_id=UUID(state["t"]),
            consented_by=UUID(state["s"]),
            household_id=state["h"],
            scopes=grants.CLASS_SCOPES[agent_kind],
            offline_token=str(offline_token),
        )
    return _next({**state, "g": [*state["g"], agent_id]}, index + 1)


async def callback(request: Request) -> Response:
    state = _unsign("me-state", request.query_params.get("state"), STATE_SECONDS)
    if state is None or not isinstance(state.get("n"), str) or not _spend(state["n"]):
        return _bad_state()
    index = state.get("i")
    if index is None:
        return await _sign_in_callback(request, state)
    if not isinstance(index, int):
        return _bad_state()
    return await _agent_callback(request, state, index)


async def consent_done(request: Request) -> Response:
    session = _session(request)
    if session is None:
        return _signed_out()
    done = _unsign("me-done", request.query_params.get("r"), STATE_SECONDS)
    if done is None or done.get("u") != str(session.user_id):
        return _bad_state()
    granted: list[str] = done["g"]
    skipped: list[str] = done["k"]
    parts = []
    if granted:
        names = ", ".join(html.escape(a) for a in granted)
        parts.append(f"<p>You consented to: {names}.</p>")
        async with _connect(request) as conn:
            cur = await conn.execute(
                "SELECT env FROM households WHERE household_id = %s", (session.household_id,)
            )
            row = await cur.fetchone()
            try:
                source = token_source.get_source(row[0] if row else "live")
                sample = await source.sample_token(conn, granted[0], session.user_id)
            except (DependencyUnavailable, IdentityError):
                sample = None
        if sample is None:
            parts.append("<p>A sample token could not be fetched right now.</p>")
        else:
            # The claims only; the signature is removed (§6, demo moment 1).
            claims = jwt.decode(sample, options={"verify_signature": False})
            shown = {k: claims.get(k) for k in ("sub", "act", "azp", "aud", "scope", "exp")}
            parts.append(
                "<p>An agent's token now looks like this:</p>"
                f"<pre>{html.escape(json.dumps(shown, indent=2))}</pre>"
            )
    if skipped:
        parts.append(f"<p>Skipped: {', '.join(html.escape(a) for a in skipped)}.</p>")
    parts.append("<p><a href='/me/agents'>Your agents</a></p>")
    return _page("All done", "".join(parts))


# --- agents ---------------------------------------------------------------------------


async def agents(request: Request) -> Response:
    session = _session(request)
    if session is None:
        return _signed_out()
    q_scope = scope.from_session(session)
    async with _connect(request) as conn:
        cur = await conn.execute(
            "SELECT agent_id, class FROM agent_cards WHERE enabled"
            " AND owner_ref IN (%s, %s) ORDER BY class, agent_id",
            (f"user:{session.user_id}", f"household:{session.household_id}"),
        )
        cards = await cur.fetchall()
        rows = await scope.scoped(conn, q_scope).fetch(
            "SELECT agent_id, user_id, expires_at FROM agent_grants"
            " WHERE household_id = %(household)s AND status = 'active'"
            " AND (user_id = %(user)s OR agent_kind = 'household')",
            user=session.user_id,
        )
    active = {r[0]: r for r in rows}
    items = []
    for agent_id, _cls in cards:
        name = html.escape(agent_id)
        grant = active.get(agent_id)
        if grant is None:
            items.append(f"<li>{name}: no consent</li>")
            continue
        action = html.escape(f"/me/agents/{agent_id}/revoke", quote=True)
        items.append(
            f"<li>{name}: consented until {grant[2]:%Y-%m-%d}"
            f"<form method='post' action='{action}'><button type='submit'>Revoke</button>"
            "</form></li>"
        )
    return _page("Your agents", f"<ul>{''.join(items)}</ul>")


async def revoke(request: Request) -> Response:
    session = _session(request)
    if session is None:
        return _signed_out()
    agent_id = request.path_params["agent_id"]
    async with _connect(request) as conn:
        q = scope.scoped(conn, scope.from_session(session))
        active = await q.fetch(
            "SELECT g.grant_id, g.agent_kind, g.user_id, m.role FROM agent_grants g"
            " LEFT JOIN household_members m ON m.household_id = g.household_id"
            " AND m.user_id = g.user_id WHERE g.household_id = %(household)s"
            " AND g.agent_id = %(agent)s AND g.status = 'active'",
            agent=agent_id,
        )
        if not active:
            return _page("Not found", "<p>No active consent for this agent.</p>", 404)
        my_role = await _role(q, session.user_id)
        allowed = [
            grant_id
            for grant_id, kind, holder, holder_role in active
            # §11: the grant holder; any guardian (admin) for a child's agent; any admin
            # for the household agent.
            if holder == session.user_id
            or (my_role == "admin" and (kind == "household" or holder_role == "child"))
        ]
        if not allowed:
            return _page("Not allowed", "<p>You can't revoke this agent.</p>", 403)
        revoked = [
            g for g in allowed if await grants.revoke(conn, g, session.user_id, REVOKE_REASON)
        ]
        await conn.commit()  # §11 step 2 runs after the commit
        for grant_id in revoked:
            await grants.after_revoke(conn, grant_id)
    return RedirectResponse("/me/agents", status_code=303)


# --- history and band -----------------------------------------------------------------


async def history(request: Request) -> Response:
    session = _session(request)
    if session is None:
        return _signed_out()
    async with _connect(request) as conn:
        q = scope.scoped(conn, scope.from_session(session))
        events = await q.fetch(
            "SELECT ts, kind, agent_id FROM audit_events WHERE household_id = %(household)s"
            " AND (actor_user_id = %(user)s OR subject_user_id = %(user)s)"
            " ORDER BY ts DESC, event_id DESC LIMIT 100",
            user=session.user_id,
        )
        approvals = await q.fetch(
            "SELECT a.requested_at, a.amount_minor, a.currency, a.status FROM approvals a"
            " JOIN approval_rules r ON r.household_id = a.household_id"
            " WHERE a.household_id = %(household)s"
            " AND (r.approver_user_id = %(user)s OR a.decided_by = %(user)s)"
            " ORDER BY a.requested_at DESC LIMIT 100",
            user=session.user_id,
        )
    event_items = "".join(
        f"<li>{ts:%Y-%m-%d %H:%M} {html.escape(kind)}"
        f"{' ' + html.escape(agent) if agent else ''}</li>"
        for ts, kind, agent in events
    )
    approval_items = "".join(
        f"<li>{ts:%Y-%m-%d %H:%M} {amount / 100:.2f} {html.escape(currency)}"
        f" {html.escape(status)}</li>"
        for ts, amount, currency, status in approvals
    )
    return _page(
        "History",
        f"<h2>Events</h2><ul>{event_items}</ul><h2>Approvals</h2><ul>{approval_items}</ul>",
    )


def _band_value(body: Any) -> tuple[bool, int | None]:
    if not isinstance(body, dict) or set(body) != {"per_order_minor"}:
        return False, None
    value = body["per_order_minor"]
    if value is None:
        return True, None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return False, None
    return True, value


async def band(request: Request) -> Response:
    session = _session(request)
    if session is None:
        return JSONResponse({"error": "signed_out"}, status_code=401)
    household_id = session.household_id
    if request.method == "POST":
        try:
            body = await request.json()
        except ValueError:
            body = None
        ok, value = _band_value(body)
        if not ok:
            return JSONResponse({"error": "per_order_minor must be an integer >= 0 or null"}, 400)
    async with _connect(request) as conn:
        q = scope.scoped(conn, scope.from_session(session))
        rows = await q.fetch(
            "SELECT per_order_minor FROM approval_rules WHERE household_id = %(household)s"
            " FOR UPDATE",
        )
        if not rows:
            return JSONResponse({"error": "no_approval_rules"}, status_code=404)
        current = rows[0][0]
        if request.method != "POST":
            return JSONResponse({"household_id": household_id, "per_order_minor": current})
        # §9: only an admin may set the band.
        if await _role(q, session.user_id) != "admin":
            return JSONResponse({"error": "admins_only"}, status_code=403)
        await q.execute(
            "UPDATE approval_rules SET per_order_minor = %(band)s"
            " WHERE household_id = %(household)s",
            band=value,
        )
        await audit.record(
            conn,
            "band_changed",
            actor_user_id=session.user_id,
            household_id=household_id,
            detail={"from": current, "to": value},
        )
    return JSONResponse({"household_id": household_id, "per_order_minor": value})


async def home(request: Request) -> Response:
    return RedirectResponse("/me/agents", status_code=303)


routes = [
    Route("/me", home, methods=["GET"]),
    Route("/me/", home, methods=["GET"]),
    Route("/me/l/{token}", link, methods=["GET", "POST"]),
    Route("/me/callback", callback, methods=["GET"]),
    Route("/me/consent/done", consent_done, methods=["GET"]),
    Route("/me/agents", agents, methods=["GET"]),
    Route("/me/agents/{agent_id}/revoke", revoke, methods=["POST"]),
    Route("/me/history", history, methods=["GET"]),
    Route("/me/household/band", band, methods=["GET", "POST"]),
]
