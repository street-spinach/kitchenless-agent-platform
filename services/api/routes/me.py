"""The `/me` pages (identity-authz §6, §9, §11, §13 and I24; experience §4, §8, EX-I36).
Pages render from `services/api/templates/` with `/me/static/me.css`.

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
*   `GET /me/household`: this week's order and its explanation, spending against the
    budget, take-turns (the switch for the approver), the last delivered dinners with
    reactions, and the allergies and diets the viewer may see. Every read goes through
    `scope.scoped` and `visibility.can_see`; a child sees only their own row.
*   `POST /me/household/take-turns` and `POST /me/meals/{order_id}/reaction` (EX-I37)
    start a household `chat` run (`trigger.source = "me"`), call
    `set_household_take_turns` or `react_to_meal` as `system:api` with the session
    person as `user_id`, finish the run and go back to `/me/household`.
*   `GET /me/history` lists the person's audit rows and approvals, and spending per week
    (per order: an admin every order, others the orders they started).
*   `GET /me/household/band` returns the band (a page for `Accept: text/html`, else JSON);
    `POST /me/household/band`, the page's form or `{per_order_minor: int | null}`, sets it
    (admins only) and writes `band_changed`.

Every POST but the Link's carries a CSRF token, `csrf_token(session, form)`: an
itsdangerous signature over the session's random id and the form's name, checked first
(experience §8). The Link's POST happens before there is a session; the Link is its own
single-use secret.

The session cookie is signed (`SESSION_SECRET`), HttpOnly, Secure, SameSite=Lax and lasts
30 minutes (§13). Every household read goes through `scope.scoped` with the session's
scope (§8).
"""

import html
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import UUID
from zoneinfo import ZoneInfo

import jwt
from itsdangerous import BadSignature, URLSafeTimedSerializer
from psycopg import AsyncConnection
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from services.common import ids, mcp_client, runs
from services.common.config import get_settings
from services.common.money import Money
from services.common.money import format as money_format
from services.common.orders import PLACED_OR_LATER
from services.common.run_context import RunContext
from services.identity import audit, grants, keycloak_admin, links, token_source
from services.identity.errors import (
    DependencyUnavailable,
    GrantError,
    IdentityError,
    LinkInvalid,
)
from services.safety import scope, visibility

__all__ = [
    "CSRF_FIELD",
    "CSRF_HEADER",
    "SESSION_COOKIE",
    "SESSION_SECONDS",
    "STATE_SECONDS",
    "TEMPLATES",
    "Session",
    "csrf_ok",
    "csrf_token",
    "routes",
]

SESSION_COOKIE = "me_session"
SESSION_SECONDS = 30 * 60
# A consent chain must finish within a link's lifetime.
STATE_SECONDS = links.LINK_SECONDS
REVOKE_REASON = "user"

log = logging.getLogger(__name__)

# A `/me` POST that calls a tool (experience §3): a household `chat` run started by `api`,
# `trigger = {user_id, source: "me"}`, the tool called as `system:api`, the run finished.
API_AGENT = "system:api"
HOUSEHOLD_SERVER = "household"
ME_SOURCE = "me"
WRONG_APPROVER = "wrong_approver"
# `mcp_client.call_tool_as`; tests replace it with the in-process server.
CALL_TOOL = mcp_client.call_tool_as

# The page templates and stylesheet (experience §8, EX-I35, EX-I36).
_API_DIR = Path(__file__).resolve().parents[1]
TEMPLATES = Jinja2Templates(directory=str(_API_DIR / "templates"))
STATIC_DIR = _API_DIR / "static"

# Every POST carries a CSRF token: an itsdangerous signature over the session id and the
# form's name, checked before any other work (§8). Forms send it as the `csrf` field; the
# band's JSON API as the `X-CSRF-Token` header.
CSRF_FIELD = "csrf"
CSRF_HEADER = "X-CSRF-Token"
FORM_REVOKE = "revoke"
FORM_BAND = "band"
FORM_TAKE_TURNS = "take_turns"
FORM_REACTION = "reaction"
FORM_PANTRY = "pantry"

# §8 page copy.
SESSION_EXPIRED = "This page timed out. Write *my page* in WhatsApp for a new link."
FORM_EXPIRED = "This page is out of date. Go back, reload it and try again."
LOCALE = "en"
MEALS_SHOWN = 7
SPENDING_WEEKS = 8
_STATE_TEXT = {
    "draft": "Being planned",
    "quoting": "Getting quotes",
    "negotiating": "Comparing offers",
    "awaiting_approval": "Waiting for approval",
    "authorized": "Approved",
    "placed": "Ordered",
    "courier_booked": "Courier booked",
    "in_delivery": "On its way",
    "delivered": "Delivered",
    "cancelled": "Cancelled",
    "expired": "Expired",
    "failed": "Failed",
}

# Nonces of states already used, with when they stop mattering (their signature expires).
# `api` is one process, so this is the whole record.
_spent_states: dict[str, float] = {}


@dataclass(frozen=True)
class Session:
    user_id: UUID
    household_id: str
    # A random id per sign-in, the subject of every CSRF token (§8); None for a cookie
    # from before it existed, which can then post nothing.
    session_id: str | None = None


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
    session_id = data.get("i")
    try:
        return Session(
            UUID(data["u"]),
            str(data["h"]),
            session_id if isinstance(session_id, str) and session_id else None,
        )
    except (KeyError, ValueError):
        return None


def _set_session(resp: Response, user_id: UUID, household_id: str) -> None:
    resp.set_cookie(
        SESSION_COOKIE,
        _sign("me-session", {"u": str(user_id), "h": household_id, "i": ids.new_token_id()}),
        max_age=SESSION_SECONDS,
        path="/me",
        secure=True,
        httponly=True,
        samesite="lax",
    )


# --- CSRF -----------------------------------------------------------------------------


def csrf_token(session: Session, form: str) -> str:
    """The token a page puts in `form`'s `csrf` field (§8)."""
    return _sign("me-csrf", {"s": session.session_id, "f": form})


def csrf_ok(session: Session, form: str, token: str | None) -> bool:
    """Whether `token` was issued for this session and this form, within the session's
    lifetime. A session with no id never passes."""
    if not session.session_id:
        return False
    data = _unsign("me-csrf", token, SESSION_SECONDS)
    return data is not None and data.get("s") == session.session_id and data.get("f") == form


async def _posted_token(request: Request) -> str | None:
    """The CSRF token of a POST: the header, else the form field (form encodings only)."""
    header = request.headers.get(CSRF_HEADER)
    if header:
        return header
    kind = request.headers.get("content-type", "")
    if kind.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        value = (await request.form()).get(CSRF_FIELD)
        return value if isinstance(value, str) else None
    return None


# --- pages ----------------------------------------------------------------------------


def _error(heading: str, message: str, status_code: int) -> HTMLResponse:
    body = TEMPLATES.get_template("error.html").render(
        person=None, household=None, tab=None, heading=heading, message=message
    )
    return HTMLResponse(body, status_code=status_code)


def _csrf_refused() -> HTMLResponse:
    return _error("Out of date", FORM_EXPIRED, 403)


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<link rel='stylesheet' href='/me/static/me.css'><title>{html.escape(title)}</title>"
        f"</head><body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>",
        status_code=status_code,
    )


def _signed_out() -> HTMLResponse:
    return _error("Session expired", SESSION_EXPIRED, 401)


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
    # The page to open after sign-in (experience EX-A15): only from the closed set.
    raw_next = request.query_params.get("next")
    next_page = raw_next if raw_next in links.NEXT_PAGES else None
    if request.method != "POST":
        # GET and HEAD never change state (§6).
        suffix = "" if next_page is None else f"?{urlencode({'next': next_page})}"
        action = html.escape(f"/me/l/{token}{suffix}", quote=True)
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
            "x": next_page,
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
        resp = RedirectResponse(links.next_path(state.get("x")), status_code=302)
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


# --- shared page parts -----------------------------------------------------------------


def _money(amount_minor: int, currency: str) -> str:
    return money_format(Money(int(amount_minor), str(currency).strip()), LOCALE)


def _clock(at: datetime) -> str:
    return f"{at:%I:%M %p}".lstrip("0").lower()


def _day(at: datetime) -> str:
    return f"{at:%a} {at.day} {at:%b}"


def _code_text(code: str) -> str:
    return code.replace("_", " ")


async def _frame(q: scope.Scoped, session: Session, tab: str | None) -> dict[str, Any]:
    """base.html's context: the person's name, the household's name and the tab."""
    rows = await q.fetch(
        "SELECT u.display_name, h.name FROM household_members m"
        " JOIN users u ON u.user_id = m.user_id"
        " JOIN households h ON h.household_id = m.household_id"
        " WHERE m.household_id = %(household)s AND m.user_id = %(user)s",
        user=session.user_id,
    )
    name, household = rows[0] if rows else (None, None)
    return {
        "person": None if name is None else {"name": name},
        "household": None if household is None else {"name": household},
        "tab": tab,
    }


async def _viewer(conn: AsyncConnection, session: Session) -> visibility.Member | None:
    try:
        return await visibility.load_member(conn, scope.from_session(session), session.user_id)
    except LookupError:
        return None


def _render(request: Request, name: str, context: dict[str, Any]) -> Response:
    return TEMPLATES.TemplateResponse(request, name, context)


# --- household ------------------------------------------------------------------------


async def _this_week_order(q: scope.Scoped) -> dict[str, Any] | None:
    """The household's newest order this budget week (Monday 00:00, household time), with
    the household agent's checked explanation from its `choose_offer` decision."""
    rows = await q.fetch(
        "SELECT o.order_id, o.kind, o.state, lower(o.delivery_window),"
        " upper(o.delivery_window), h.timezone, g.name FROM orders o"
        " JOIN households h ON h.household_id = o.household_id"
        " LEFT JOIN agent_cards c ON c.agent_id = o.seller_agent_id"
        " LEFT JOIN organizations g ON c.owner_ref = 'org:' || g.org_id"
        " WHERE o.household_id = %(household)s AND o.state <> 'draft'"
        " AND o.created_at >= (date_trunc('week', now() AT TIME ZONE h.timezone)"
        " AT TIME ZONE h.timezone)"
        " ORDER BY o.created_at DESC LIMIT 1"
    )
    if not rows:
        return None
    order_id, kind, state, start, end, tz, store = rows[0]
    summary = "Groceries" if kind == "grocery" else "Dinner"
    if store:
        summary += f" from {store}"
    if start is not None and end is not None:
        zone = ZoneInfo(tz)
        start, end = start.astimezone(zone), end.astimezone(zone)
        summary += f", {_day(start)} {_clock(start)}-{_clock(end)}"
    explained = await q.fetch(
        "SELECT t.payload->>'explanation' FROM agent_traces t"
        " JOIN orders o ON o.run_id = t.run_id"
        " WHERE o.household_id = %(household)s AND o.order_id = %(order)s"
        " AND t.kind = 'decision' AND t.payload->>'node' = 'choose_offer'"
        " AND coalesce(t.payload->>'explanation', '') <> ''"
        " ORDER BY t.seq DESC LIMIT 1",
        order=order_id,
    )
    return {
        "summary": summary,
        "state": _STATE_TEXT.get(state, state),
        "explanation": explained[0][0] if explained else None,
    }


async def _spending(q: scope.Scoped) -> dict[str, Any]:
    """This budget week's orders `placed` or later against the weekly budget, the rule
    `orders.remaining_weekly_budget` uses."""
    spent_rows = await q.fetch(
        "SELECT coalesce(sum(o.total_minor), 0)::bigint, h.currency FROM households h"
        " LEFT JOIN orders o ON o.household_id = h.household_id"
        " AND o.state = ANY(%(states)s)"
        " AND o.created_at >= (date_trunc('week', now() AT TIME ZONE h.timezone)"
        " AT TIME ZONE h.timezone)"
        " WHERE h.household_id = %(household)s GROUP BY h.currency",
        states=list(PLACED_OR_LATER),
    )
    spent, currency = spent_rows[0] if spent_rows else (0, "USD")
    budget = await q.fetch(
        "SELECT amount_minor, currency FROM budgets"
        " WHERE household_id = %(household)s AND period = 'weekly'"
    )
    return {
        "spent": _money(spent, currency),
        "budget": _money(budget[0][0], budget[0][1]) if budget else None,
    }


async def _meals(
    q: scope.Scoped, session: Session, viewer: visibility.Member
) -> list[dict[str, Any]]:
    """The last delivered dinners, with reaction totals only when the viewer may see them
    and the viewer's own reaction always."""
    rows = await q.fetch(
        "SELECT o.order_id, coalesce(upper(o.delivery_window), o.updated_at), h.timezone,"
        " (SELECT string_agg(i.name, ', ' ORDER BY i.line) FROM (SELECT name, line"
        " FROM order_items WHERE order_id = o.order_id ORDER BY line LIMIT 3) i),"
        " (SELECT count(*) FROM meal_reactions r WHERE r.order_id = o.order_id"
        " AND r.value = 1),"
        " (SELECT count(*) FROM meal_reactions r WHERE r.order_id = o.order_id"
        " AND r.value = -1),"
        " (SELECT r.value FROM meal_reactions r WHERE r.order_id = o.order_id"
        " AND r.user_id = %(user)s)"
        " FROM orders o JOIN households h ON h.household_id = o.household_id"
        " WHERE o.household_id = %(household)s AND o.kind = 'dinner'"
        " AND o.state = 'delivered' ORDER BY o.updated_at DESC LIMIT %(limit)s",
        user=session.user_id,
        limit=MEALS_SHOWN,
    )
    totals = visibility.can_see(viewer, "reaction_totals", None)
    token = csrf_token(session, FORM_REACTION)
    return [
        {
            "order_id": str(order_id),
            "title": items or "Dinner",
            "delivered": _day(at.astimezone(ZoneInfo(tz))),
            "totals": {"up": int(up), "down": int(down)} if totals else None,
            "mine": None if mine is None else int(mine),
            "csrf": token,
        }
        for order_id, at, tz, items, up, down, mine in rows
    ]


async def _people(q: scope.Scoped, viewer: visibility.Member) -> list[dict[str, Any]]:
    """Each person whose allergies and diets the viewer may see (§4): an admin everyone,
    children included; an adult or a child only themselves. Dislikes are not shown."""
    rows = await q.fetch(
        "SELECT m.user_id, u.display_name, m.role, c.kind, c.value FROM household_members m"
        " JOIN users u ON u.user_id = m.user_id"
        " LEFT JOIN member_constraints c ON c.user_id = m.user_id"
        " AND c.kind IN ('allergy', 'diet')"
        " WHERE m.household_id = %(household)s"
        " ORDER BY m.role, u.display_name, m.user_id, c.kind, c.value"
    )
    people: dict[UUID, dict[str, Any]] = {}
    for user_id, name, role, kind, value in rows:
        data = "child_data" if role == "child" else "own_constraints"
        if not visibility.can_see(viewer, data, user_id):
            continue
        person = people.setdefault(user_id, {"name": name, "allergies": [], "diets": []})
        if kind == "allergy":
            person["allergies"].append(_code_text(value))
        elif kind == "diet":
            person["diets"].append(_code_text(value))
    return list(people.values())


async def household_page(request: Request) -> Response:
    """`GET /me/household` (§8, EX-I36): every read through `scope.scoped` and
    `visibility.can_see`."""
    session = _session(request)
    if session is None:
        return _signed_out()
    async with _connect(request) as conn:
        viewer = await _viewer(conn, session)
        if viewer is None:
            return _signed_out()
        q = scope.scoped(conn, scope.from_session(session))
        context = await _frame(q, session, "household")
        wide = visibility.can_see(viewer, "plan", None)
        context["household_wide"] = wide
        context["people"] = await _people(q, viewer)
        if wide:
            context["order"] = await _this_week_order(q)
            context["spending"] = await _spending(q)
            rules = await q.fetch(
                "SELECT take_turns FROM household_rules WHERE household_id = %(household)s"
            )
            context["take_turns"] = {
                "on": bool(rules[0][0]) if rules else False,
                "can_switch": viewer.is_approver,
                "csrf": csrf_token(session, FORM_TAKE_TURNS),
            }
            context["meals"] = await _meals(q, session, viewer)
    return _render(request, "household.html", context)


# --- household actions (EX-I37) ---------------------------------------------------------


class _ToolFailed(Exception):
    """The tool could not be reached or failed; the run ended `error`."""


async def _me_tool(
    request: Request, session: Session, tool: str, args: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """Call `tool` for the session person in a new §3 `/me` run: `(result, None)`, or
    `(None, reason)` for a refusal (the run `completed` with that reason) or a household
    grant that is not active (no run starts). A failure to reach the tool ends the run
    `error` and raises `_ToolFailed`."""
    async with _connect(request) as conn:
        try:
            run_id = await runs.start(
                conn,
                household_id=session.household_id,
                kind="chat",
                trigger={"user_id": session.user_id, "source": ME_SOURCE},
            )
        except GrantError as refused:
            return None, refused.reason
        await conn.commit()  # the guard reads the run from its own connection
    ctx = RunContext(run_id=run_id, agent_id=API_AGENT, conversation_id=None)
    result: dict[str, Any] | None = None
    reason: str | None = None
    status = "completed"
    try:
        result = await CALL_TOOL(HOUSEHOLD_SERVER, tool, args, ctx=ctx)
    except mcp_client.ToolRefused as refused:
        reason = refused.reason
    except Exception:
        log.exception("/me tool %s failed", tool)
        status = "error"
    async with _connect(request) as conn:
        await runs.finish(conn, run_id, API_AGENT, status, reason or tool)
        await conn.commit()
    if status == "error":
        raise _ToolFailed(tool)
    return result, reason


def _action_refused(reason: str | None) -> HTMLResponse:
    if reason == WRONG_APPROVER:
        return _error("Not allowed", "Only the approver can change this.", 403)
    if reason is not None and reason.startswith("grant_"):
        return _error(
            "Agent paused",
            "Your household agent needs its consent renewed. Ask an admin to tap Renew in"
            " WhatsApp.",
            409,
        )
    return _error("Not saved", "That didn't go through. Go back, reload and try again.", 403)


async def take_turns(request: Request) -> Response:
    """`POST /me/household/take-turns`: the approver's switch (`on` present means on)."""
    session = _session(request)
    if session is None:
        return _signed_out()
    if not csrf_ok(session, FORM_TAKE_TURNS, await _posted_token(request)):
        return _csrf_refused()
    on = (await request.form()).get("on") is not None
    args = {"on": on, "user_id": str(session.user_id)}
    try:
        result, reason = await _me_tool(request, session, "set_household_take_turns", args)
    except _ToolFailed:
        return _error("Not saved", "Something went wrong. Try again in a minute.", 503)
    if result is None:
        return _action_refused(reason)
    return RedirectResponse("/me/household", status_code=303)


async def reaction(request: Request) -> Response:
    """`POST /me/meals/{order_id}/reaction`: `value` 1 or -1 for the session person."""
    session = _session(request)
    if session is None:
        return _signed_out()
    if not csrf_ok(session, FORM_REACTION, await _posted_token(request)):
        return _csrf_refused()
    try:
        order_id = UUID(request.path_params["order_id"])
    except ValueError:
        return _error("Not found", "That meal isn't there.", 404)
    raw = (await request.form()).get("value")
    if raw not in ("1", "-1"):
        return _error("Not saved", "Pick 👍 or 👎.", 400)
    args = {"order_id": str(order_id), "value": int(str(raw)), "user_id": str(session.user_id)}
    try:
        result, reason = await _me_tool(request, session, "react_to_meal", args)
    except _ToolFailed:
        return _error("Not saved", "Something went wrong. Try again in a minute.", 503)
    if result is None:
        return _action_refused(reason)
    return RedirectResponse("/me/household", status_code=303)


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
        q = scope.scoped(conn, q_scope)
        rows = await q.fetch(
            "SELECT agent_id, user_id, expires_at FROM agent_grants"
            " WHERE household_id = %(household)s AND status = 'active'"
            " AND (user_id = %(user)s OR agent_kind = 'household')",
            user=session.user_id,
        )
        my_role = await _role(q, session.user_id)
        context = await _frame(q, session, None)
    active = {r[0]: r for r in rows}
    token = csrf_token(session, FORM_REVOKE)
    listed = []
    for agent_id, cls in cards:
        grant = active.get(agent_id)
        listed.append(
            {
                "agent_id": agent_id,
                "label": "Household agent" if cls == "household" else agent_id,
                "until": None if grant is None else f"{grant[2]:%Y-%m-%d}",
                # §11: the grant holder, or any admin for the household agent.
                "can_revoke": grant is not None
                and (grant[1] == session.user_id or (cls == "household" and my_role == "admin")),
                "csrf": token,
            }
        )
    context["agents"] = listed
    return _render(request, "agents.html", context)


async def revoke(request: Request) -> Response:
    session = _session(request)
    if session is None:
        return _signed_out()
    if not csrf_ok(session, FORM_REVOKE, await _posted_token(request)):
        return _csrf_refused()
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


async def _spending_history(q: scope.Scoped, viewer: visibility.Member) -> list[dict[str, Any]]:
    """Spending per budget week, for the last weeks: every adult sees the totals; the
    orders listed are those `visibility.can_see` allows (an admin every order, others the
    orders whose run they started)."""
    if not visibility.can_see(viewer, "spending_total", None):
        return []
    rows = await q.fetch(
        "SELECT (date_trunc('week', o.created_at AT TIME ZONE h.timezone))::date,"
        " o.created_at, h.timezone, o.kind, o.total_minor, o.currency,"
        " r.trigger->>'user_id' FROM orders o"
        " JOIN households h ON h.household_id = o.household_id"
        " JOIN runs r ON r.run_id = o.run_id"
        " WHERE o.household_id = %(household)s AND o.state = ANY(%(states)s)"
        " AND o.total_minor IS NOT NULL"
        " AND o.created_at >= (date_trunc('week', now() AT TIME ZONE h.timezone)"
        " AT TIME ZONE h.timezone) - make_interval(weeks => %(weeks)s)"
        " ORDER BY o.created_at DESC",
        states=list(PLACED_OR_LATER),
        weeks=SPENDING_WEEKS - 1,
    )
    weeks: dict[Any, dict[str, Any]] = {}
    for week, at, tz, kind, total, currency, starter in rows:
        entry = weeks.setdefault(
            week, {"week": f"Week of {week.day} {week:%b}", "sum": 0, "cur": currency, "orders": []}
        )
        entry["sum"] += int(total)
        try:
            starter_id = UUID(starter) if starter else None
        except ValueError:
            starter_id = None
        if visibility.can_see(viewer, "spending_per_order", starter_id):
            entry["orders"].append(
                {
                    "at": _day(at.astimezone(ZoneInfo(tz))),
                    "amount": _money(total, currency),
                    "label": "Groceries" if kind == "grocery" else "Dinner",
                }
            )
    return [
        {"week": e["week"], "total": _money(e["sum"], e["cur"]), "orders": e["orders"]}
        for e in weeks.values()
    ]


async def history(request: Request) -> Response:
    session = _session(request)
    if session is None:
        return _signed_out()
    async with _connect(request) as conn:
        viewer = await _viewer(conn, session)
        if viewer is None:
            return _signed_out()
        q = scope.scoped(conn, scope.from_session(session))
        context = await _frame(q, session, "history")
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
        context["spending"] = await _spending_history(q, viewer)
    context["events"] = [
        {"at": f"{ts:%Y-%m-%d %H:%M}", "text": kind + (f" {agent}" if agent else "")}
        for ts, kind, agent in events
    ]
    context["approvals"] = [
        {"at": f"{ts:%Y-%m-%d %H:%M}", "amount": _money(amount, currency), "status": status}
        for ts, amount, currency, status in approvals
    ]
    return _render(request, "history.html", context)


def _band_value(body: Any) -> tuple[bool, int | None]:
    if not isinstance(body, dict) or set(body) != {"per_order_minor"}:
        return False, None
    value = body["per_order_minor"]
    if value is None:
        return True, None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return False, None
    return True, value


def _band_form_value(raw: Any) -> tuple[bool, int | None]:
    """The band page's form field: empty for no band, else digits only."""
    if not isinstance(raw, str):
        return False, None
    text = raw.strip()
    if text == "":
        return True, None
    if not text.isascii() or not text.isdigit():
        return False, None
    return True, int(text)


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


async def band(request: Request) -> Response:
    """The approval band: HTML for a browser (`Accept: text/html`), JSON otherwise.

    A POST is the page's form (`per_order_minor`, empty for none; CSRF in the field, then
    a redirect back) or identity-authz's JSON `{per_order_minor}` (CSRF in the header)."""
    session = _session(request)
    page = _wants_html(request)
    if session is None:
        if page:
            return _signed_out()
        return JSONResponse({"error": "signed_out"}, status_code=401)
    household_id = session.household_id
    is_form = False
    if request.method == "POST":
        if not csrf_ok(session, FORM_BAND, await _posted_token(request)):
            if request.headers.get("content-type", "").startswith("application/json"):
                return JSONResponse({"error": "csrf"}, status_code=403)
            return _csrf_refused()
        kind = request.headers.get("content-type", "")
        is_form = kind.startswith(("application/x-www-form-urlencoded", "multipart/form-data"))
        if is_form:
            ok, value = _band_form_value((await request.form()).get("per_order_minor"))
        else:
            try:
                body = await request.json()
            except ValueError:
                body = None
            ok, value = _band_value(body)
        if not ok:
            if is_form:
                return _error(
                    "Not saved", "Enter a whole number of minor units, or leave it empty.", 400
                )
            return JSONResponse({"error": "per_order_minor must be an integer >= 0 or null"}, 400)
    async with _connect(request) as conn:
        q = scope.scoped(conn, scope.from_session(session))
        rows = await q.fetch(
            "SELECT per_order_minor FROM approval_rules WHERE household_id = %(household)s"
            " FOR UPDATE",
        )
        if not rows:
            if page:
                return _error("Not set up", "This household has no approval rules yet.", 404)
            return JSONResponse({"error": "no_approval_rules"}, status_code=404)
        current = rows[0][0]
        role = await _role(q, session.user_id)
        if request.method != "POST":
            if page:
                context = await _frame(q, session, None)
                currency = await q.fetch(
                    "SELECT currency FROM households WHERE household_id = %(household)s"
                )
                code = str(currency[0][0]).strip() if currency else ""
                context.update(
                    band=None
                    if current is None
                    else {"amount": _money(current, code), "per_order_minor": current},
                    currency=code,
                    can_set=role == "admin",
                    csrf=csrf_token(session, FORM_BAND),
                )
                return _render(request, "band.html", context)
            return JSONResponse({"household_id": household_id, "per_order_minor": current})
        # §9: only an admin may set the band.
        if role != "admin":
            if is_form:
                return _error("Not allowed", "Only an admin can change this.", 403)
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
    if is_form:
        return RedirectResponse("/me/household/band", status_code=303)
    return JSONResponse({"household_id": household_id, "per_order_minor": value})


async def home(request: Request) -> Response:
    return RedirectResponse("/me/agents", status_code=303)


routes = [
    # The stylesheet first, before identity-authz's routes (EX-I36).
    Mount("/me/static", app=StaticFiles(directory=str(STATIC_DIR)), name="me_static"),
    Route("/me", home, methods=["GET"]),
    Route("/me/", home, methods=["GET"]),
    Route("/me/l/{token}", link, methods=["GET", "POST"]),
    Route("/me/callback", callback, methods=["GET"]),
    Route("/me/consent/done", consent_done, methods=["GET"]),
    Route("/me/household", household_page, methods=["GET"]),
    Route("/me/household/take-turns", take_turns, methods=["POST"]),
    Route("/me/meals/{order_id}/reaction", reaction, methods=["POST"]),
    Route("/me/agents", agents, methods=["GET"]),
    Route("/me/agents/{agent_id}/revoke", revoke, methods=["POST"]),
    Route("/me/history", history, methods=["GET"]),
    Route("/me/household/band", band, methods=["GET", "POST"]),
]
