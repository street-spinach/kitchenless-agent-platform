"""`/me/household`, spending in `/me/history`, CSRF and the stylesheet (experience.md §4,
§8, EX-I36)."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from starlette.responses import Response

from services.api.routes import me
from services.common import db as common_db
from services.common import pantry
from services.common.mcp_client import AdapterUnavailable, ToolRefused
from services.identity.errors import AuthzUnavailable
from services.mcp.household import server as household
from services.mcp.meta import CallContext
from services.safety.guard import GuardRefused
from tests.unit import api_world as aw
from tests.unit import experience_world as ew
from tests.unit import identity_world as w
from tests.unit.test_me_routes import (
    HOUSEHOLD,
    HOUSEHOLD_AGENT,
    MEMBER_AGENT,
    FakeKeycloak,
    World,
    _client,
    _csrf,
    _session_cookie,
    make_kc,
    make_world,
)

db = pytest.mark.db


@pytest.fixture
def kc(monkeypatch: pytest.MonkeyPatch) -> FakeKeycloak:
    return make_kc(monkeypatch)


@pytest.fixture
def world(pg: str, kc: FakeKeycloak) -> World:
    return make_world(pg, kc)


STORE = "grocery:store-b-us"
EXPLANATION = "Store B was $4.10 cheaper for the same 12 items."


def _named(pg: str, world: World) -> None:
    with psycopg.connect(pg) as conn:
        for person, name in ((world.admin, "Maya"), (world.member, "Sam"), (world.child, "Kavi")):
            conn.execute(
                "UPDATE users SET display_name = %s WHERE user_id = %s", (name, person.user_id)
            )
        conn.execute(
            "UPDATE households SET name = 'The Rao household' WHERE household_id = %s", (HOUSEHOLD,)
        )
        for person, kind, value in (
            (world.admin, "allergy", "tree_nuts"),
            (world.admin, "dislike", "olives"),
            (world.member, "diet", "vegetarian"),
            (world.member, "dislike", "mushrooms"),
            (world.child, "allergy", "peanuts"),
        ):
            conn.execute(
                "INSERT INTO member_constraints (user_id, kind, value) VALUES (%s, %s, %s)",
                (person.user_id, kind, value),
            )


def _week(pg: str, world: World) -> dict[str, Any]:
    """This week's grocery order with its explanation, a budget, take-turns on, and two
    delivered dinners: one the member reacted to."""
    now = datetime.now(UTC)
    # The budget week starts Monday 00:00 household time (America/New_York): the dinners
    # are last week, whatever day the test runs.
    local = now.astimezone(ZoneInfo("America/New_York"))
    monday = (local - timedelta(days=local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO organizations (org_id, name, region) VALUES ('store-b', 'Store B', 'us')"
        )
        w.add_agent(conn, STORE, "org:store-b")
        conn.execute(
            "INSERT INTO budgets (household_id, period, amount_minor, currency)"
            " VALUES (%s, 'weekly', 15000, 'USD')",
            (HOUSEHOLD,),
        )
        conn.execute(
            "INSERT INTO household_rules (household_id, take_turns, set_by) VALUES (%s, true, %s)",
            (HOUSEHOLD, world.admin.user_id),
        )
        week_order, run_id = ew.order(conn, HOUSEHOLD, "courier_booked", now)
        conn.execute(
            "UPDATE orders SET seller_agent_id = %s, total_minor = 8420 WHERE order_id = %s",
            (STORE, week_order),
        )
        conn.execute(
            "UPDATE runs SET trigger = %s WHERE run_id = %s",
            (Jsonb({"user_id": str(world.member.user_id)}), run_id),
        )
        conn.execute(
            "INSERT INTO agent_traces (run_id, seq, agent_id, kind, payload)"
            " VALUES (%s, 1, %s, 'decision', %s)",
            (
                run_id,
                HOUSEHOLD_AGENT,
                Jsonb({"node": "choose_offer", "choice": "accept", "explanation": EXPLANATION}),
            ),
        )
        dinners = []
        for days, name in ((1, "Dal and rice"), (3, "Pasta bake")):
            dinner, _ = ew.order(conn, HOUSEHOLD, "delivered", monday - timedelta(days=days))
            conn.execute(
                "UPDATE orders SET kind = 'dinner', total_minor = 2600 WHERE order_id = %s",
                (dinner,),
            )
            conn.execute(
                "INSERT INTO order_items (order_id, line, sku, name, qty, unit_minor)"
                " VALUES (%s, 1, 'sku-1', %s, 1, 2600)",
                (dinner, name),
            )
            dinners.append(dinner)
        for person, value in ((world.member, 1), (world.admin, -1)):
            conn.execute(
                "INSERT INTO meal_reactions (order_id, user_id, value) VALUES (%s, %s, %s)",
                (dinners[0], person.user_id, value),
            )
    return {"order": week_order, "dinners": dinners}


def _page(world: World, person: w.Person, path: str = "/me/household") -> Any:
    client = _client(world.pg)
    client.cookies.update(_session_cookie(person))
    return client.get(path)


@db
def test_household_page_member_view(world: World, kc: FakeKeycloak) -> None:
    _named(world.pg, world)
    _week(world.pg, world)
    page = _page(world, world.member)
    assert page.status_code == 200
    text = page.text
    assert "<strong>Sam</strong> · The Rao household" in text
    assert '<a href="/me/household" aria-current="page">' in text
    # The week's order and its explanation, the spending total vs budget.
    assert "Groceries from Store B" in text and "Courier booked" in text
    assert EXPLANATION in text
    assert "$84.20 of $150.00 this week" in text  # the dinners were last week
    # Take-turns read-only for a non-approver.
    assert "Take turns is on." in text
    assert 'action="/me/household/take-turns"' not in text
    assert "Only the approver can change this." in text
    # Meals: two adults, so no totals; Sam's own reaction shows.
    assert "Dal and rice" in text and "Pasta bake" in text
    assert "👍 1" not in text and "👎 1" not in text
    assert 'aria-pressed="true"' in text
    assert text.count('aria-pressed="true"') == 1
    # Only Sam's own constraints, and no dislikes anywhere.
    assert "vegetarian" in text
    assert "tree nuts" not in text and "Maya" not in text
    assert "olives" not in text and "mushrooms" not in text
    # No per-order spending on the household page.
    assert "$26.00" not in text


@db
def test_household_page_admin_view(world: World, kc: FakeKeycloak) -> None:
    _named(world.pg, world)
    _week(world.pg, world)
    text = _page(world, world.admin).text
    # The approver gets the switch, with its CSRF field and the only script.
    assert 'action="/me/household/take-turns"' in text
    assert 'onchange="this.form.submit()"' in text and "<script" not in text
    assert 'name="csrf" value="' in text
    # Everyone's allergies and diets, the child's included; still no dislikes.
    for value in ("tree nuts", "vegetarian", "peanuts"):
        assert value in text
    assert "olives" not in text and "mushrooms" not in text
    # Two adults: totals hidden for the admin too.
    assert "👍 1" not in text
    # A third adult: totals show.
    with psycopg.connect(world.pg) as conn:
        w.add_person(conn, HOUSEHOLD, "member")
    text = _page(world, world.admin).text
    assert "👍 1 · 👎 1" in text
    assert "👍 0 · 👎 0" in text
    # An outsider's session for this household is signed out.
    assert _page(world, world.other_admin).status_code == 401


@db
def test_member_sees_no_child_constraint(world: World, kc: FakeKeycloak) -> None:
    _named(world.pg, world)
    _week(world.pg, world)
    member = _page(world, world.member).text
    assert "peanuts" not in member and "Kavi" not in member
    admin = _page(world, world.admin).text
    assert "Kavi" in admin and "peanuts" in admin
    # A child sees only their own row: nothing household-wide.
    child = _page(world, world.child)
    assert child.status_code == 200
    text = child.text
    assert "Kavi" in text and "peanuts" in text
    for hidden in ("tree nuts", "vegetarian", "Store B", EXPLANATION, "$84.20", "Take turns"):
        assert hidden not in text, hidden
    assert "Dal and rice" not in text


@db
def test_history_spending(world: World, kc: FakeKeycloak) -> None:
    """Spending in /me/history (§4): totals for adults; per order, an admin every order and
    a member the orders they started; a child nothing."""
    _named(world.pg, world)
    _week(world.pg, world)
    admin = _page(world, world.admin, "/me/history").text
    member = _page(world, world.member, "/me/history").text
    child = _page(world, world.child, "/me/history").text
    assert "$84.20" in admin and admin.count("Dinner") >= 1  # every order
    assert "$84.20" in member  # the order Sam started
    assert '<span class="muted">' in member
    assert "Dinner</span>" not in member  # the dinners were not Sam's
    assert "$84.20" not in child and "No spending yet." in child


def _pantry_event(pg: str, item: str, kind: str, age_hours: float = 0) -> None:
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO pantry_events (household_id, item, pantry_item, kind, delta, unit, source,"
            " ts) VALUES (%s, %s, %s, %s, 0, 'each', 'test',"
            " now() - make_interval(secs => %s::float8))",
            (HOUSEHOLD, item, item, kind, age_hours * 3600),
        )


def _section(text: str, heading: str) -> str:
    """One state's card on `/me/pantry`."""
    start = text.index(f">{heading}</h2>")
    return text[start : text.index("</section>", start)]


@db
def test_pantry_page_on_and_off(world: World, kc: FakeKeycloak) -> None:
    _named(world.pg, world)
    _pantry_event(world.pg, "rice", "verify", 2 * 24)  # 14-day window: trusted
    _pantry_event(world.pg, "milk", "add", 5 * 24)  # 3-day window: stale
    _pantry_event(world.pg, "bread", "verify", 30)
    _pantry_event(world.pg, "bread", "out", 1)  # the latest event wins
    page = _page(world, world.member, "/me/pantry")
    assert page.status_code == 200
    text = page.text
    assert '<a href="/me/pantry" aria-current="page">' in text
    at_home, unsure, out = (_section(text, h) for h in ("At home", "Not sure", "Out"))
    assert "<strong>rice</strong>" in at_home and "Last checked 2 days ago" in at_home
    assert "<strong>milk</strong>" in unsure and "Last checked 5 days ago" in unsure
    assert "<strong>bread</strong>" in out and "Last checked today" in out
    # Every vocabulary item is listed; one with no event is Not sure, never checked.
    assert "<strong>granola</strong>" in unsure and "Never checked" in unsure
    assert text.count('value="out">Ran out') == len(pantry.load_vocabulary().names)
    assert 'action="/me/pantry/almond%20milk"' in text
    assert 'name="csrf" value="' in text
    # A child sees no household-wide row, so nothing here to read or correct.
    child = _page(world, world.child, "/me/pantry")
    assert child.status_code == 200
    assert "<strong>" in child.text and "rice" not in child.text and "<form" not in child.text
    # Another household's events are not this household's pantry.
    with psycopg.connect(world.pg) as conn:
        conn.execute(
            "INSERT INTO pantry_events (household_id, item, pantry_item, kind, delta, unit, source)"
            " VALUES ('h-other', 'granola', 'granola', 'out', 0, 'each', 'test')"
        )
    assert "<strong>granola</strong>" in _section(
        _page(world, world.member, "/me/pantry").text, "Not sure"
    )
    # Signed out, and an outsider's session for this household.
    assert _client(world.pg).get("/me/pantry").status_code == 401
    assert _page(world, world.other_admin, "/me/pantry").status_code == 401
    # Off for a live household: the line, no items, even with events.
    with psycopg.connect(world.pg) as conn:
        conn.execute("UPDATE households SET env = 'live' WHERE household_id = %s", (HOUSEHOLD,))
    off = _page(world, world.member, "/me/pantry").text
    assert "Pantry tracking isn't on for your household yet." in off
    assert "rice" not in off and "<form" not in off


class FakeTool:
    """`call_tool_as` for `/me` POSTs: records the call, then calls the real tool in its run
    (`raises` instead fails the call the way the `mcp` service would)."""

    def __init__(self, pg: str, env: str = "eval") -> None:
        self.pg = pg
        self.env = env
        self.calls: list[tuple[str, str, dict[str, Any], Any]] = []
        self.raises: Exception | None = None

    async def __call__(self, server: str, tool: str, args: Any, *, ctx: Any) -> dict[str, Any]:
        self.calls.append((server, tool, dict(args), ctx))
        if self.raises is not None:
            raise self.raises
        call = CallContext(
            run_id=ctx.run_id,
            agent_id=ctx.agent_id,
            conversation_id=None,
            attempt=1,
            token=w.service_token(ctx.agent_id),
            env=self.env,
            modes={},
            on_behalf_of=None,
            household_id=HOUSEHOLD,
        )
        async with AsyncConnectionPool(self.pg, min_size=1, max_size=3, open=False) as pool:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(common_db, "get_pool", lambda: pool)
                try:
                    return await household.record_pantry_check(
                        call,
                        pantry_item=args["pantry_item"],
                        kind=args["kind"],
                        user_id=UUID(args["user_id"]),
                    )
                except GuardRefused as refused:
                    raise ToolRefused(refused.reason) from None


@pytest.fixture
def tool(world: World, monkeypatch: pytest.MonkeyPatch) -> FakeTool:
    """The tool behind `/me` POSTs, the household's grant and `system:api`'s client row."""
    with psycopg.connect(world.pg) as conn:
        w.grant_household(conn, HOUSEHOLD, world.admin.user_id)
        conn.execute(
            "INSERT INTO agent_clients (agent_id, client_id, kind) VALUES (%s, %s, 'service')",
            (household.API_AGENT, w.API_CLIENT),
        )
    fake = FakeTool(world.pg)
    monkeypatch.setattr(me, "call_tool_as", fake)
    return fake


def _pantry_post(
    world: World, person: w.Person, item: str = "rice", kind: str | None = "out"
) -> Any:
    client = _client(world.pg)
    client.cookies.update(_session_cookie(person))
    data = {"csrf": _csrf(person, "pantry")}
    if kind is not None:
        data["kind"] = kind
    return client.post(f"/me/pantry/{item}", data=data, follow_redirects=False)


def _pantry_runs(pg: str) -> list[dict[str, Any]]:
    return aw.query(
        pg, "SELECT household_id, kind, status, reason, trigger FROM runs ORDER BY started_at"
    )


@db
def test_pantry_post(world: World, kc: FakeKeycloak, tool: FakeTool) -> None:
    resp = _pantry_post(world, world.member)
    assert (resp.status_code, resp.headers["location"]) == (303, "/me/pantry")
    [(server, name, args, ctx)] = tool.calls
    assert (server, name) == ("household", "record_pantry_check")
    assert args == {"pantry_item": "rice", "kind": "out", "user_id": str(world.member.user_id)}
    assert ctx.agent_id == "system:api"
    # The §3 run: a household `chat` run of the session person, ended by `api`.
    assert _pantry_runs(world.pg) == [
        {
            "household_id": HOUSEHOLD,
            "kind": "chat",
            "status": "completed",
            "reason": "record_pantry_check",
            "trigger": {"user_id": str(world.member.user_id), "source": "me"},
        }
    ]
    [event] = aw.query(world.pg, "SELECT * FROM pantry_events")
    assert (event["kind"], event["pantry_item"]) == ("out", "rice")
    assert event["actor_user_id"] == world.member.user_id
    [run] = aw.query(world.pg, "SELECT run_id FROM runs")
    assert event["run_id"] == run["run_id"]
    # The page now shows it Out; a name with a space reaches the tool decoded.
    assert "<strong>rice</strong>" in _section(_page(world, world.member, "/me/pantry").text, "Out")
    assert _pantry_post(world, world.admin, "almond%20milk", "verify").status_code == 303
    assert tool.calls[-1][2]["pantry_item"] == "almond milk"


@db
def test_pantry_post_csrf_and_kind_first(world: World, kc: FakeKeycloak, tool: FakeTool) -> None:
    client = _client(world.pg)
    client.cookies.update(_session_cookie(world.member))
    for csrf in ({}, {"csrf": "forged"}, {"csrf": _csrf(world.member, "band")}):
        resp = client.post("/me/pantry/rice", data={"kind": "out", **csrf})
        assert resp.status_code == 403, csrf
    assert _client(world.pg).post("/me/pantry/rice", data={"kind": "out"}).status_code == 401
    for kind in ("add", "", None):
        assert _pantry_post(world, world.member, kind=kind).status_code == 400, kind
    # An outsider's session for this household is signed out.
    assert _pantry_post(world, world.other_admin).status_code == 401
    assert tool.calls == [] and _pantry_runs(world.pg) == []


@db
def test_pantry_post_refused(world: World, kc: FakeKeycloak, tool: FakeTool) -> None:
    """A refusal shows a page, writes nothing and ends the run `completed` with its reason."""
    child = _pantry_post(world, world.child)
    assert child.status_code == 403 and "Only an adult can change this." in child.text
    unknown = _pantry_post(world, world.member, "caviar")
    assert unknown.status_code == 404
    tool.env = "live"
    off = _pantry_post(world, world.member)
    assert off.status_code == 403
    assert aw.query(world.pg, "SELECT 1 FROM pantry_events") == []
    assert [(r["status"], r["reason"]) for r in _pantry_runs(world.pg)] == [
        ("completed", "not_adult"),
        ("completed", "unknown_pantry_item"),
        ("completed", "pantry_off"),
    ]


@db
def test_pantry_post_outage_ends_run_error(world: World, kc: FakeKeycloak, tool: FakeTool) -> None:
    tool.raises = AuthzUnavailable()
    down = _pantry_post(world, world.member)
    assert down.status_code == 503
    assert "We couldn&#39;t check your access just now." in down.text
    tool.raises = AdapterUnavailable("transport")
    assert _pantry_post(world, world.member).status_code == 503
    assert [(r["status"], r["reason"]) for r in _pantry_runs(world.pg)] == [
        ("error", "authz_unavailable"),
        ("error", "transport"),
    ]
    assert aw.query(world.pg, "SELECT 1 FROM pantry_events") == []


@db
def test_pantry_post_without_grant_starts_no_run(
    world: World, kc: FakeKeycloak, tool: FakeTool
) -> None:
    with psycopg.connect(world.pg) as conn:
        conn.execute("UPDATE agent_grants SET status = 'revoked'")
    resp = _pantry_post(world, world.member)
    assert resp.status_code == 403 and "needs renewing" in resp.text
    assert tool.calls == [] and _pantry_runs(world.pg) == []


@db
def test_csrf_required(world: World, kc: FakeKeycloak) -> None:
    with psycopg.connect(world.pg) as conn:
        grant = w.add_grant(conn, MEMBER_AGENT, world.member, HOUSEHOLD)

    def revoke(cookie: dict[str, str], **kw: Any) -> int:
        client = _client(world.pg)
        client.cookies.update(cookie)
        return client.post(
            f"/me/agents/{MEMBER_AGENT}/revoke", follow_redirects=False, **kw
        ).status_code

    cookie = _session_cookie(world.member)
    other = me.Session(world.member.user_id, HOUSEHOLD, "another-session")
    no_id = {
        me.SESSION_COOKIE: me._sign("me-session", {"u": str(world.member.user_id), "h": HOUSEHOLD})
    }
    for kw in (
        {},  # no token
        {"data": {"csrf": "forged"}},
        {"data": {"csrf": _csrf(world.member, "band")}},  # another form's token
        {"data": {"csrf": me.csrf_token(other, "revoke")}},  # another session's token
    ):
        assert revoke(cookie, **kw) == 403, kw
    # A session with no id can never post.
    assert (
        revoke(
            no_id,
            data={"csrf": me.csrf_token(me.Session(world.member.user_id, HOUSEHOLD), "revoke")},
        )
        == 403
    )
    [row] = aw.query(world.pg, "SELECT status FROM agent_grants WHERE grant_id = %s", grant)
    assert row["status"] == "active"
    assert kc.revoked == []
    # The band's JSON API needs the header; the form needs the field.
    admin = _client(world.pg)
    admin.cookies.update(_session_cookie(world.admin))
    assert admin.post("/me/household/band", json={"per_order_minor": 5000}).status_code == 403
    assert admin.post("/me/household/band", data={"per_order_minor": "5000"}).status_code == 403
    assert (
        aw.query(world.pg, "SELECT per_order_minor FROM approval_rules")[0]["per_order_minor"]
        is None
    )
    # With the right token: done.
    assert revoke(cookie, data={"csrf": _csrf(world.member, "revoke")}) == 303
    resp = admin.post(
        "/me/household/band",
        data={"per_order_minor": "5000", "csrf": _csrf(world.admin, "band")},
        follow_redirects=False,
    )
    assert (resp.status_code, resp.headers["location"]) == (303, "/me/household/band")
    assert (
        aw.query(world.pg, "SELECT per_order_minor FROM approval_rules")[0]["per_order_minor"]
        == 5000
    )
    # The band page renders for a browser, with the form for an admin only.
    page = admin.get("/me/household/band", headers={"accept": "text/html"})
    assert page.status_code == 200 and "$50.00" in page.text and "<form" in page.text
    member = _client(world.pg)
    member.cookies.update(_session_cookie(world.member))
    page = member.get("/me/household/band", headers={"accept": "text/html"})
    assert "<form" not in page.text and "Only an admin can change this." in page.text
    # A new sign-in carries a random session id, the subject of its CSRF tokens.
    ids_seen = set()
    for _ in range(2):
        out = Response()
        me._set_session(out, world.admin.user_id, HOUSEHOLD)
        value = out.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
        data = me._unsign("me-session", value, 60)
        assert data is not None and data["i"]
        ids_seen.add(data["i"])
    assert len(ids_seen) == 2


@db
def test_static_css_served(world: World, kc: FakeKeycloak) -> None:
    client = _client(world.pg)
    resp = client.get("/me/static/me.css")  # no session needed
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/css")
    assert "system-ui" in resp.text
    assert client.get("/me/static/nothing.css").status_code == 404
    # Signed out: the §8 copy, styled.
    page = client.get("/me/household")
    assert page.status_code == 401
    assert "This page timed out. Write *my page* in WhatsApp for a new link." in page.text
    assert '<link rel="stylesheet" href="/me/static/me.css">' in page.text
