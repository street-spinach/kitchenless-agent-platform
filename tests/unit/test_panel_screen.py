"""The presenter view `/panel/screen` and its feed (showcase.md §7, SC-A4; issue #417)."""

import contextlib
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from starlette.testclient import TestClient

from services.api import main as api
from services.api.routes import panel
from tests.unit import api_world as w

db = pytest.mark.db

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
HOUSEHOLD = f"household:{w.HOUSEHOLD_ID}"
STORE = "grocery:fresh-us"
STORE_2 = "grocery:corner-us"
COURIER = "delivery:quick-us"
CONVERSATION = uuid.UUID("33333333-3333-4333-8333-333333333333")
HOUSEHOLD_NAME = "Maya's household"
MEMBER_NAME = "Maya Perera"
ADDRESS = "12 Elm Street"

# Identity fields `run_guarded` writes on every tool_result (identity-authz §14).
IDENTITY = {
    "sub_user_id": str(w.ADMIN),
    "act_agent_id": HOUSEHOLD,
    "grant_id": None,
    "token_source": "mock",
    "authz": None,
}


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    w.patch_settings(monkeypatch)


@contextlib.asynccontextmanager
async def _no_db() -> AsyncIterator[Any]:
    raise AssertionError("this request must not reach the database")
    yield  # pragma: no cover


def _client_no_db() -> TestClient:
    return TestClient(api.create_app(_no_db))


# --- page and static files (no database) -----------------------------------------------------


def test_screen_routes_are_mounted() -> None:
    paths = {getattr(r, "path", None) for r in api.create_app(_no_db).routes}
    assert {"/panel/screen", "/panel/screen/feed", "/panel/static"} <= paths


def test_screen_page_has_csp_and_no_inline_script() -> None:
    resp = _client_no_db().get("/panel/screen")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["content-security-policy"] == "default-src 'self'"
    page = resp.text
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", page, re.S)
    assert scripts == [(' src="/panel/static/screen.js" defer', "")]
    assert "<style" not in page
    assert " style=" not in page
    assert not re.search(r"\son[a-z]+=", page)
    assert 'href="/panel/static/screen.css"' in page
    assert 'href="/me/static/brand.css"' in page
    assert "Live: agents at work" in page


@pytest.mark.parametrize(
    ("path", "kind"),
    [("/panel/static/screen.js", "javascript"), ("/panel/static/screen.css", "text/css")],
)
def test_screen_assets_are_served(path: str, kind: str) -> None:
    resp = _client_no_db().get(path)
    assert resp.status_code == 200
    assert kind in resp.headers["content-type"]


def test_screen_js_polls_every_1500_ms_and_never_writes_html() -> None:
    js = (panel._PANEL_STATIC / "screen.js").read_text()
    assert "POLL_MS = 1500" in js
    assert "/panel/screen/feed" in js
    assert "innerHTML" not in js
    assert "eval(" not in js


def test_screen_css_uses_the_dark_brand_background_and_18px_minimum() -> None:
    css = (panel._PANEL_STATIC / "screen.css").read_text()
    assert "background: var(--brand-green-900)" in css
    sizes = [int(n) for n in re.findall(r"font(?:-size)?:\s*(\d+)px", css)]
    assert sizes and min(sizes) >= 18


@pytest.mark.parametrize(
    ("query", "error"),
    [("?run_id=nope", "run_id"), ("?after=yesterday", "after")],
)
def test_feed_refuses_bad_query_before_the_database(query: str, error: str) -> None:
    resp = _client_no_db().get(f"/panel/screen/feed{query}")
    assert resp.status_code == 400
    assert error in resp.json()["error"]


# --- the feed (database) ---------------------------------------------------------------------


@pytest.fixture
def world(pg: str) -> str:
    w.seed(pg)
    with psycopg.connect(pg) as conn:
        conn.execute(
            "UPDATE households SET name = %s WHERE household_id = %s",
            (HOUSEHOLD_NAME, w.HOUSEHOLD_ID),
        )
        conn.execute("UPDATE users SET display_name = %s", (MEMBER_NAME,))
        for org, name in (("fresh", "Fresh Market"), ("quick", "Quick Couriers")):
            conn.execute(
                "INSERT INTO organizations (org_id, name, region) VALUES (%s, %s, 'us')",
                (org, name),
            )
        for agent_id, cls, owner in (
            (STORE, "grocery", "org:fresh"),
            (STORE_2, "grocery", "org:missing"),
            (COURIER, "delivery", "org:quick"),
        ):
            conn.execute(
                "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
                " VALUES (%s, %s, %s, 'us', 'agents')",
                (agent_id, cls, owner),
            )
    return pg


def _run(conn: psycopg.Connection, started_at: datetime = T0, kind: str = "weekly") -> uuid.UUID:
    run_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO runs (run_id, household_id, kind, trigger, modes, started_at)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (run_id, w.HOUSEHOLD_ID, kind, Jsonb({"source": "test"}), Jsonb({}), started_at),
    )
    return run_id


def _trace(
    conn: psycopg.Connection,
    run_id: uuid.UUID,
    seq: int,
    kind: str,
    payload: dict[str, Any],
    *,
    agent_id: str = HOUSEHOLD,
    at: datetime | None = None,
    **cols: Any,
) -> None:
    names = "".join(f", {c}" for c in cols)
    marks = "".join(", %s" for _ in cols)
    conn.execute(
        f"INSERT INTO agent_traces (run_id, seq, ts, agent_id, kind, payload{names})"
        f" VALUES (%s, %s, %s, %s, %s, %s{marks})",
        (
            run_id,
            seq,
            at or T0 + timedelta(seconds=seq),
            agent_id,
            kind,
            Jsonb(payload),
            *cols.values(),
        ),
    )


def _message(
    conn: psycopg.Connection,
    run_id: uuid.UUID,
    seq: int,
    *,
    from_agent: str,
    to_agent: str,
    type_: str,
    body: dict[str, Any],
    summary: str,
) -> None:
    payload = {
        "from": from_agent,
        "to": to_agent,
        "type": type_,
        "body": body,
        "summary": summary,
        "causation_id": None,
    }
    _trace(conn, run_id, seq, "message", payload, agent_id=from_agent)


def _guard_result(tool: str, refused: bool, reason: str | None) -> dict[str, Any]:
    result = None if refused else {"ok": True, "member": MEMBER_NAME}
    return {
        "server": "household",
        "tool": tool,
        "result": result,
        "refused": refused,
        "reason": reason,
        **IDENTITY,
    }


def _seed_weekly(conn: psycopg.Connection) -> uuid.UUID:
    """One weekly run: a people message, a quote request, an offer, two decisions, guard
    checks, an approval, and rows the feed must leave out."""
    run_id = _run(conn)
    _message(
        conn,
        run_id,
        1,
        from_agent=w.WHATSAPP,
        to_agent=HOUSEHOLD,
        type_="user_message",
        body={"text": f"weekly shop for {MEMBER_NAME}, peanuts allergy", "phone": w.ADMIN_PHONE},
        summary="Message from a member",
    )
    _message(
        conn,
        run_id,
        2,
        from_agent=HOUSEHOLD,
        to_agent=STORE,
        type_="quote_request",
        body={"items": ["peanut-free bread"], "address": ADDRESS},
        summary="Quote request: 18 items",
    )
    _message(
        conn,
        run_id,
        3,
        from_agent=STORE,
        to_agent=HOUSEHOLD,
        type_="offer",
        body={"total_minor": 19190, "rationale": "Cheapest oat milk in stock"},
        summary="Offer: $191.90",
    )
    _trace(
        conn,
        run_id,
        4,
        "decision",
        {
            "node": "choose_offer",
            "choice": STORE,
            "explanation": "Fresh Market is cheapest for Maya",
            "source": "model",
        },
        model="muse-spark",
        latency_ms=840,
        tokens_in=1200,
        tokens_out=34,
    )
    _trace(
        conn,
        run_id,
        5,
        "decision",
        {
            "node": "respond_to_counter",
            "choice": {"counter_minor": 18240, "note": "for Maya"},
            "explanation": "free text rationale",
            "source": "rule_default",
            "reason": "timeout",
        },
        agent_id=STORE,
        model="muse-spark",
    )
    _trace(
        conn,
        run_id,
        6,
        "tool_result",
        _guard_result("authorize_spend", True, "needs_approval"),
    )
    _trace(
        conn,
        run_id,
        7,
        "approval",
        {
            "approval_id": str(uuid.uuid4()),
            "status": "pending",
            "amount_minor": 18240,
            "currency": "USD",
            "user_id": str(w.ADMIN),
        },
    )
    _trace(conn, run_id, 8, "tool_result", _guard_result("authorize_spend", False, None))
    # Not on the screen: a tool_call, an unguarded (`traced`) result, a data share and a
    # free-text error.
    _trace(
        conn,
        run_id,
        9,
        "tool_call",
        {
            "server": "household",
            "tool": "authorize_spend",
            "args": {"address": ADDRESS},
            "idempotency_key": None,
        },
    )
    _trace(
        conn,
        run_id,
        10,
        "tool_result",
        {
            "server": "grocery",
            "tool": "search",
            "result": {"q": "bread"},
            "refused": False,
            "reason": None,
        },
        agent_id=STORE,
    )
    _trace(
        conn,
        run_id,
        11,
        "data_share",
        {"address_ref": "addr-secret-1", "recipient": COURIER, "purpose": "delivery"},
    )
    _message(
        conn,
        run_id,
        12,
        from_agent=HOUSEHOLD,
        to_agent=COURIER,
        type_="delivery_quote_request",
        body={"address": ADDRESS},
        summary="Delivery quote request",
    )
    _message(
        conn,
        run_id,
        13,
        from_agent=HOUSEHOLD,
        to_agent=STORE_2,
        type_="quote_request",
        body={},
        summary="Quote request: 18 items",
    )
    return run_id


@db
def test_feed_rows_fields_and_names(world: str) -> None:
    with psycopg.connect(world) as conn:
        run_id = _seed_weekly(conn)
    resp = w.client(world).get("/panel/screen/feed")
    assert resp.status_code == 200
    feed = resp.json()
    assert feed["run"]["run_id"] == str(run_id)
    assert feed["run"]["kind"] == "weekly"
    assert datetime.fromisoformat(feed["run"]["started_at"]) == T0

    # One column per agent: household first, then sellers, then the courier, then people.
    assert [(a["id"], a["name"]) for a in feed["agents"]] == [
        (HOUSEHOLD, "Household agent"),
        (STORE, "Fresh Market"),
        (STORE_2, "Store"),
        (COURIER, "Quick Couriers"),
        (w.WHATSAPP, HOUSEHOLD_NAME),
    ]

    rows = feed["rows"]
    assert [r["seq"] for r in rows] == [1, 2, 3, 4, 5, 6, 7, 8, 12, 13]
    for row in rows:
        assert datetime.fromisoformat(row.pop("ts")) == T0 + timedelta(seconds=row["seq"])
    by_seq = {r["seq"]: r for r in rows}
    assert by_seq[1] == {
        "seq": 1,
        "agent_id": w.WHATSAPP,
        "kind": "message",
        "from_id": w.WHATSAPP,
        "to_id": HOUSEHOLD,
        "from": HOUSEHOLD_NAME,
        "to": "Household agent",
        "type": "user_message",
        "summary": "Message from a member",
    }
    assert by_seq[3]["from"] == "Fresh Market"
    assert by_seq[3]["summary"] == "Offer: $191.90"
    assert by_seq[4] == {
        "seq": 4,
        "agent_id": HOUSEHOLD,
        "kind": "decision",
        "agent": "Household agent",
        "node": "choose_offer",
        "source": "model",
        "choice": "Fresh Market",
        "model": "muse-spark",
        "latency_ms": 840,
    }
    # An object choice is never shown.
    assert by_seq[5]["source"] == "rule_default"
    assert by_seq[5]["choice"] is None
    assert by_seq[5]["agent"] == "Fresh Market"
    assert by_seq[6] == {
        "seq": 6,
        "agent_id": HOUSEHOLD,
        "kind": "check",
        "agent": "Household agent",
        "tool": "authorize_spend",
        "result": "needs_approval",
    }
    assert by_seq[8]["result"] == "ok"
    assert by_seq[7] == {
        "seq": 7,
        "agent_id": HOUSEHOLD,
        "kind": "approval",
        "agent": "Household agent",
        "status": "requested",
    }
    assert by_seq[12]["to"] == "Quick Couriers"


@db
def test_feed_has_no_bodies_phones_names_or_addresses(world: str) -> None:
    with psycopg.connect(world) as conn:
        _seed_weekly(conn)
    text = w.client(world).get("/panel/screen/feed").text
    for leak in (
        MEMBER_NAME,
        "Maya Perera",
        "for Maya",
        w.ADMIN_PHONE,
        str(w.ADMIN),
        ADDRESS,
        "addr-secret-1",
        "peanut",
        "Cheapest oat milk",
        "free text",
        "is cheapest",
        '"body"',
        '"rationale"',
        '"explanation"',
        '"args"',
        "amount_minor",
        "18240",
    ):
        assert leak not in text, leak


@db
def test_feed_totals(world: str) -> None:
    with psycopg.connect(world) as conn:
        _seed_weekly(conn)
        # A people-profile model step: no `source`, but a model column.
        run_id = conn.execute("SELECT run_id FROM runs").fetchone()[0]  # type: ignore[index]
        _trace(
            conn,
            run_id,
            14,
            "decision",
            {"node": "classify", "choice": ["start_weekly"], "explanation": ""},
            model="muse-spark",
            latency_ms=300,
            tokens_in=100,
            tokens_out=10,
        )
    feed = w.client(world).get("/panel/screen/feed").json()
    assert feed["totals"] == {
        # Distinct trace writers: household, store, channel.
        "agents": 3,
        "messages": 5,
        "model_calls": 2,
        "tokens": 1200 + 34 + 100 + 10,
        "elapsed_s": 13.0,
    }
    assert feed["rows"][-1]["choice"] == "start_weekly"
    assert feed["rows"][-1]["source"] == "model"


@db
def test_feed_after_returns_newer_rows_and_next(world: str) -> None:
    with psycopg.connect(world) as conn:
        run_id = _seed_weekly(conn)
    client = w.client(world)
    after = (T0 + timedelta(seconds=7)).isoformat()
    feed = client.get("/panel/screen/feed", params={"after": after}).json()
    assert [r["seq"] for r in feed["rows"]] == [7, 8, 12, 13]
    assert datetime.fromisoformat(feed["next"]) == T0 + timedelta(seconds=13)
    # Totals are over the whole run, not the page.
    assert feed["totals"]["messages"] == 5
    # Nothing newer: no rows, and `next` stays.
    later = (T0 + timedelta(minutes=5)).isoformat()
    feed = client.get("/panel/screen/feed", params={"after": later, "run_id": str(run_id)}).json()
    assert feed["rows"] == []
    assert datetime.fromisoformat(feed["next"]) == T0 + timedelta(minutes=5)


@db
def test_feed_follows_the_newest_run_unless_pinned(world: str) -> None:
    with psycopg.connect(world) as conn:
        old = _seed_weekly(conn)
        new = _run(conn, started_at=T0 + timedelta(hours=1), kind="dinner")
        _message(
            conn,
            new,
            1,
            from_agent=HOUSEHOLD,
            to_agent=STORE,
            type_="quote_request",
            body={},
            summary="Quote request: 2 items",
        )
        # A row of the new run before its start is not shown by default.
        conn.execute(
            "UPDATE agent_traces SET ts = %s WHERE run_id = %s",
            (T0 + timedelta(hours=1, seconds=5), new),
        )
    client = w.client(world)
    feed = client.get("/panel/screen/feed").json()
    assert feed["run"]["run_id"] == str(new)
    assert feed["run"]["kind"] == "dinner"
    assert [r["summary"] for r in feed["rows"]] == ["Quote request: 2 items"]
    pinned = client.get("/panel/screen/feed", params={"run_id": str(old)}).json()
    assert pinned["run"]["run_id"] == str(old)
    assert len(pinned["rows"]) == 10


@db
def test_feed_default_after_is_the_run_start(world: str) -> None:
    with psycopg.connect(world) as conn:
        run_id = _run(conn, started_at=T0 + timedelta(seconds=2, milliseconds=500))
        for seq in (1, 2, 3, 4):
            _message(
                conn,
                run_id,
                seq,
                from_agent=HOUSEHOLD,
                to_agent=STORE,
                type_="quote_request",
                body={},
                summary=f"q{seq}",
            )
    feed = w.client(world).get("/panel/screen/feed").json()
    assert [r["seq"] for r in feed["rows"]] == [3, 4]


@db
def test_feed_limit(world: str) -> None:
    with psycopg.connect(world) as conn:
        run_id = _run(conn)
        for seq in range(1, panel.SCREEN_LIMIT + 6):
            _message(
                conn,
                run_id,
                seq,
                from_agent=HOUSEHOLD,
                to_agent=STORE,
                type_="quote_request",
                body={},
                summary=f"q{seq}",
            )
    feed = w.client(world).get("/panel/screen/feed").json()
    assert len(feed["rows"]) == panel.SCREEN_LIMIT
    assert feed["rows"][0]["seq"] == 1
    assert feed["totals"]["messages"] == panel.SCREEN_LIMIT + 5


@db
def test_feed_unknown_run_is_404_and_no_runs_is_empty(world: str) -> None:
    client = w.client(world)
    assert client.get("/panel/screen/feed").json() == {
        "run": None,
        "agents": [],
        "rows": [],
        "totals": None,
    }
    resp = client.get("/panel/screen/feed", params={"run_id": str(uuid.uuid4())})
    assert resp.status_code == 404
