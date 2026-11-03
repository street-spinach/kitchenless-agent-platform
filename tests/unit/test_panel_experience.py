"""`GET /panel/experience?household=` (experience §12, EX-I22; issue #291)."""

import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from services.api.routes import panel
from services.api.whatsapp import templates
from services.common.config import Settings
from tests.unit import api_world as w
from tests.unit import experience_world as x
from tests.unit.test_config import full_env

pytestmark = pytest.mark.db

HID = "us-7-eval"
OTHER = "us-8-eval"
CONFIGURED = ("approval_request", "order_summary")


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    env = full_env("api") | {templates.TEMPLATES[k].setting.upper(): "tpl_v1" for k in CONFIGURED}
    settings = Settings.from_env(env)
    monkeypatch.setattr(panel, "get_settings", lambda: settings)


def _seed(pg: str) -> dict[str, uuid.UUID]:
    now = datetime.now(UTC)
    with psycopg.connect(pg) as conn:
        x.household(conn, HID)
        x.household(conn, OTHER)
        alice = x.person(conn, HID, "Alice")
        bob = x.person(conn, HID, "Bob", "member")
        x.person(conn, HID, "Sam", "child")
        stranger = x.person(conn, OTHER, "Zed")
        # Alice: two asks this week, one last week. Bob: none this week.
        x.send(conn, alice, now, ["ap:1:yes"])
        x.send(conn, alice, now, ["tt:1:yes"], outcome="template")
        x.send(conn, alice, now - timedelta(days=7), ["ap:2:yes"])
        x.send(conn, bob, now - timedelta(days=7), ["ob:1:yes"])
        # Guards this week: one expired approval and one skipped send.
        x.approval(conn, HID, "expired", now)
        x.send(conn, bob, now, ["ap:3:yes"], outcome="skipped")
        # One pending held item here, one dropped; the other household's do not count.
        for user, status in ((alice, "pending"), (bob, "dropped"), (stranger, "pending")):
            mid = x.send(conn, user, now, ["ap:9:yes"], outcome="held")
            x.held(conn, user, mid, status, now)
        x.send(conn, stranger, now, ["ap:4:yes"])
        x.approval(conn, OTHER, "rejected", now)
    return {"alice": alice, "bob": bob}


def test_shape(pg: str) -> None:
    people = _seed(pg)
    response = w.client(pg).get("/panel/experience", params={"household": HID})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"weeks", "held_pending", "templates_configured"}
    this, last = body["weeks"]
    [today] = w.query(
        pg,
        "SELECT date_trunc('week', now() AT TIME ZONE timezone)::date AS week"
        " FROM households WHERE household_id = %s",
        HID,
    )
    assert this["week"] == today["week"].isoformat()
    assert last["week"] == (today["week"] - timedelta(days=7)).isoformat()
    # Adults only, with zero asks when they were asked nothing.
    alice, bob = str(people["alice"]), str(people["bob"])
    assert this["per_user"] == [
        {"user_id": alice, "display_name": "Alice", "asks": 2},
        {"user_id": bob, "display_name": "Bob", "asks": 0},
    ]
    assert last["per_user"] == [
        {"user_id": alice, "display_name": "Alice", "asks": 1},
        {"user_id": bob, "display_name": "Bob", "asks": 1},
    ]
    assert this["guards"] == {
        "approvals_expired": 1,
        "orders_ended": 0,
        "rejects": 0,
        "safety_refusals": 0,
        "revokes": 0,
        "held_dropped": 1,
        "sends_not_delivered": 1,
    }
    assert last["guards"] == dict.fromkeys(panel.GUARD_METRICS, 0)
    assert body["held_pending"] == 1
    assert body["templates_configured"] == list(CONFIGURED)


def test_page_three_lines(pg: str) -> None:
    _seed(pg)
    response = w.client(pg).get(
        "/panel/experience", params={"household": HID}, headers={"accept": "text/html"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = response.text
    assert "<script" not in page
    assert page.count("<p>") == 3
    assert "Alice 2 this week, 1 last week; Bob 0 this week, 1 last week" in page
    assert "approvals_expired 1" in page and "sends_not_delivered 1" in page
    assert "Held items pending: 1. Templates configured: approval_request, order_summary" in page


def test_unknown_household_404(pg: str) -> None:
    client = w.client(pg)
    assert client.get("/panel/experience", params={"household": "nope"}).status_code == 404
    assert client.get("/panel/experience").status_code == 400
