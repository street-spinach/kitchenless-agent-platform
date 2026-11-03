"""The owner app's `/app/*` routes: `/app/rules` (identity-authz §10, I32), `/app/orders` and
`/app/notices` (experience.md §11, EX-I47). Owner tokens come from
`MockSource.owner_token`; OpenFGA is a set of tuples behind `fga.check`."""

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from starlette.testclient import TestClient

from services.api import main as api
from services.api import owner_views
from services.common import ids
from services.identity import claims as c
from services.identity import fga, token_source
from services.identity.errors import AuthzUnavailable
from tests.unit import api_world as aw
from tests.unit import experience_world as ew
from tests.unit import identity_world as iw

db = pytest.mark.db

ORG = "square-restaurant-us"
OTHER_ORG = "mock-restaurant-us"
RESTAURANT = "restaurant:square-us"
OTHER_RESTAURANT = "restaurant:mock-us"
FLOORS = {"SQ-DISH": 1140}


class FakeFga:
    def __init__(self) -> None:
        self.tuples: set[tuple[str, str, str]] = set()
        self.checks: list[tuple[str, str, str]] = []
        self.fail = False

    def check(self, user: str, relation: str, object: str) -> tuple[bool, str]:
        self.checks.append((user, relation, object))
        if self.fail:
            raise AuthzUnavailable("timeout")
        return (user, relation, object) in self.tuples, "model-1"


@pytest.fixture
def owner_fga(monkeypatch: pytest.MonkeyPatch) -> FakeFga:
    fake = FakeFga()
    monkeypatch.setattr(fga, "check", fake.check)
    return fake


def _owner_sub(org_id: str) -> str:
    return f"owner.{org_id}"  # `MockSource.owner_token`'s subject


def _token(org_id: str = ORG) -> str:
    return token_source.MockSource().owner_token(org_id)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def world(pg: str, owner_fga: FakeFga) -> str:
    with psycopg.connect(pg) as conn:
        for org_id in (ORG, OTHER_ORG):
            conn.execute(
                "INSERT INTO organizations (org_id, name, region) VALUES (%s, %s, 'us')",
                (org_id, org_id),
            )
        for agent_id, org_id in ((RESTAURANT, ORG), (OTHER_RESTAURANT, OTHER_ORG)):
            conn.execute(
                "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
                " VALUES (%s, 'restaurant', %s, 'us', 'agents')",
                (agent_id, f"org:{org_id}"),
            )
            conn.execute(
                "INSERT INTO business_rules (agent_id, kind, value) VALUES (%s, 'floors', %s)",
                (agent_id, Jsonb(FLOORS)),
            )
    owner_fga.tuples.add((f"user:{_owner_sub(ORG)}", "owner", f"org:{ORG}"))
    return pg


def _client(pg: str) -> TestClient:
    return TestClient(api.create_app(aw.connect_to(pg)))


def _rules(pg: str) -> dict[tuple[str, str], Any]:
    rows = aw.query(pg, "SELECT agent_id, kind, value FROM business_rules")
    return {(r["agent_id"], r["kind"]): r["value"] for r in rows}


def _audits(pg: str) -> list[dict[str, Any]]:
    return aw.query(pg, "SELECT * FROM audit_events WHERE kind = 'owner_rule_changed'")


@db
def test_owner_rule_requires_owner(world: str, owner_fga: FakeFga) -> None:
    client = _client(world)
    url = f"/app/rules?org_id={ORG}"
    body = {"max_discount_pct": 4}
    # No token, a bad token, and a token for another audience are refused before OpenFGA.
    assert client.post(url, json=body).status_code == 401
    assert client.post(url, json=body, headers=_auth("not-a-jwt")).status_code == 401
    now = time.time()
    mcp_token = token_source.sign_mock(
        token_source.mock_claims(
            sub=_owner_sub(ORG), client_id="owner-app", delegated=False, now=now
        )
    )
    resp = client.post(url, json=body, headers=_auth(mcp_token))
    assert (resp.status_code, resp.json()["error"]) == (401, c.TOKEN_INVALID_AUD)
    expired = token_source.sign_mock(
        token_source.mock_claims(
            sub=_owner_sub(ORG),
            client_id="owner-app",
            delegated=False,
            now=now - 3600,
            audience=c.OWNER_AUDIENCE,
        )
    )
    resp = client.post(url, json=body, headers=_auth(expired))
    assert (resp.status_code, resp.json()["error"]) == (401, c.TOKEN_EXPIRED)
    assert owner_fga.checks == []

    # A valid owner token for another org: `user owner org:<o>` fails, nothing changes.
    other = _auth(_token(OTHER_ORG))
    resp = client.post(url, json=body, headers=other)
    assert (resp.status_code, resp.json()["error"]) == (403, c.AUTHZ_DENIED)
    assert client.get(url, headers=other).status_code == 403
    assert owner_fga.checks[-1] == (f"user:{_owner_sub(OTHER_ORG)}", "owner", f"org:{ORG}")
    # An OpenFGA outage is not a pass.
    owner_fga.fail = True
    assert client.post(url, json=body, headers=_auth(_token())).status_code == 503
    owner_fga.fail = False
    assert (RESTAURANT, "max_discount_pct") not in _rules(world)
    assert _audits(world) == []

    # Out-of-range or malformed bodies, and a missing org, are refused.
    owner = _auth(_token())
    for bad in (
        {"max_discount_pct": 101},
        {"max_discount_pct": -1},
        {"max_discount_pct": "4"},
        {"max_discount_pct": True},
        {"max_discount_pct": 4, "x": 1},
        {},
    ):
        assert client.post(url, json=bad, headers=owner).status_code == 400, bad
    assert client.post("/app/rules", json=body, headers=owner).status_code == 400

    # The owner may.
    resp = client.post(url, json=body, headers=owner)
    assert resp.status_code == 200
    assert resp.json() == {"org_id": ORG, "max_discount_pct": 4}
    assert client.get(url, headers=owner).json() == {"org_id": ORG, "max_discount_pct": 4}
    rules = _rules(world)
    assert rules[(RESTAURANT, "max_discount_pct")] == 4
    assert (OTHER_RESTAURANT, "max_discount_pct") not in rules
    assert rules[(RESTAURANT, "floors")] == FLOORS


@db
def test_owner_rule_audited(world: str) -> None:
    client = _client(world)
    url = f"/app/rules?org_id={ORG}"
    owner = _auth(_token())
    assert client.get(url, headers=owner).json() == {"org_id": ORG, "max_discount_pct": None}
    assert client.post(url, json={"max_discount_pct": 4}, headers=owner).status_code == 200
    assert client.post(url, json={"max_discount_pct": 10}, headers=owner).status_code == 200
    first, second = sorted(_audits(world), key=lambda r: r["event_id"])
    assert first["org_id"] == ORG and first["household_id"] is None
    assert first["detail"] == {
        "rule": "max_discount_pct",
        "from": None,
        "to": 4,
        "agents": [RESTAURANT],
        "owner_sub": _owner_sub(ORG),
    }
    assert (second["detail"]["from"], second["detail"]["to"]) == (4, 10)
    assert _rules(world)[(RESTAURANT, "max_discount_pct")] == 10


@db
def test_other_app_paths_stay_reserved(world: str) -> None:
    resp = _client(world).get("/app/orders/1", headers=_auth(_token()))
    assert resp.status_code == 501


# --- orders and notices (experience.md §11, EX-I47) ------------------------------------------

HOUSEHOLD = "h-own-1"
OTHER_HOUSEHOLD = "h-own-2"
RATIONALE = "Fresh today and in stock; 2 lines at list price."


def _order(
    conn: psycopg.Connection, household_id: str, seller: str | None, state: str, at: datetime
) -> tuple[uuid.UUID, uuid.UUID]:
    order_id, run_id = ew.order(conn, household_id, state, at)
    conn.execute(
        "UPDATE orders SET seller_agent_id = %s, total_minor = 4250 WHERE order_id = %s",
        (seller, order_id),
    )
    return order_id, run_id


def _quote(
    conn: psycopg.Connection,
    order: tuple[uuid.UUID, uuid.UUID],
    seller: str,
    buyer: str,
    status: str,
    *,
    at: datetime,
    expires: datetime,
) -> None:
    order_id, run_id = order
    conn.execute(
        "INSERT INTO quotes (quote_id, run_id, order_id, conversation_id, seller_agent_id,"
        " buyer_agent_id, items, total_minor, currency, expires_at, status, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, '[]', 3990, 'USD', %s, %s, %s)",
        (uuid.uuid4(), run_id, order_id, uuid.uuid4(), seller, buyer, expires, status, at),
    )


_SEQ = iter(range(1, 10_000))


def _trace(
    conn: psycopg.Connection, run_id: uuid.UUID, agent: str, kind: str, payload: dict[str, Any]
) -> None:
    conn.execute(
        "INSERT INTO agent_traces (run_id, seq, agent_id, kind, payload)"
        " VALUES (%s, %s, %s, %s, %s)",
        (run_id, next(_SEQ), agent, kind, Jsonb(payload)),
    )


def _offer(
    conn: psycopg.Connection, order: tuple[uuid.UUID, uuid.UUID], seller: str, rationale: str
) -> None:
    order_id, run_id = order
    body = {
        "order_id": str(order_id),
        "round": 0,
        "lines": [{"sku": "SQ-DISH", "qty": 2}],
        "total_minor": 3990,
        "rationale": rationale,
    }
    _trace(conn, run_id, seller, "message", {"from": seller, "type": "offer", "body": body})


@pytest.fixture
def market(world: str) -> dict[str, Any]:
    """Each org has a live order, an open quote with an offer, an expired quote, a cancelled
    order, a delivered order and an owner-floor decision. Two households buy from both."""
    now = datetime.now(UTC)
    out: dict[str, Any] = {}
    with psycopg.connect(world) as conn:
        buyers = {h: iw.add_world(conn, h).household_agent for h in (HOUSEHOLD, OTHER_HOUSEHOLD)}
        for seller, household in ((RESTAURANT, HOUSEHOLD), (OTHER_RESTAURANT, OTHER_HOUSEHOLD)):
            buyer = buyers[household]
            live = _order(conn, household, seller, "placed", now - timedelta(minutes=5))
            _offer(conn, live, seller, RATIONALE)
            quoting = _order(conn, household, None, "quoting", now - timedelta(minutes=1))
            _quote(conn, quoting, seller, buyer, "open", at=now, expires=now + timedelta(minutes=5))
            _offer(conn, quoting, seller, "Call 555-0100 or see http://x.example now")
            stale = _order(conn, household, None, "expired", now - timedelta(hours=2))
            _quote(
                conn,
                stale,
                seller,
                buyer,
                "expired",
                at=now - timedelta(hours=2),
                expires=now - timedelta(hours=1),
            )
            _order(conn, household, seller, "cancelled", now - timedelta(minutes=30))
            _order(conn, household, seller, "delivered", now - timedelta(minutes=40))
            _trace(conn, live[1], seller, "decision", {"node": "owner_floor", "choice": "x"})
            # Another node's decision is never a notice.
            _trace(conn, live[1], seller, "decision", {"node": "respond_to_counter"})
            out[seller] = {"live": live[0], "quoting": quoting[0]}
    return out


def _get(pg: str, path: str, org_id: str = ORG, token_org: str = ORG) -> Any:
    sep = "&" if "?" in path else "?"
    return _client(pg).get(f"{path}{sep}org_id={org_id}", headers=_auth(_token(token_org)))


@db
def test_org_scoped(world: str, market: dict[str, Any], owner_fga: FakeFga) -> None:
    """XG14: an owner sees only their own organisation's orders, quotes and notices."""
    resp = _get(world, "/app/orders")
    assert resp.status_code == 200
    rows = resp.json()["orders"]
    mine = market[RESTAURANT]
    refs = {r["ref"]: r for r in rows}
    assert set(refs) == {
        ids.org_order_ref(ORG, mine["live"]),
        ids.org_order_ref(ORG, mine["quoting"]),
    }
    live = refs[ids.org_order_ref(ORG, mine["live"])]
    assert (live["kind"], live["state"], live["amount_minor"], live["currency"]) == (
        "order",
        "placed",
        4250,
        "USD",
    )
    assert live["rationale"] == RATIONALE
    quote = refs[ids.org_order_ref(ORG, mine["quoting"])]
    assert (quote["kind"], quote["state"], quote["amount_minor"]) == ("quote", "open", 3990)
    # A stored rationale that fails `check_rationale` (a URL, numbers not in the offer) is
    # not shown.
    assert quote["rationale"] is None
    # Newest first.
    assert [r["ref"] for r in rows] == [quote["ref"], live["ref"]]

    notices = _get(world, "/app/notices").json()["notices"]
    assert sorted(n["kind"] for n in notices) == [
        "counter_refused_floor",
        "offer_expired",
        "order_cancelled",
    ]
    # The other org's owner cannot read this org, and the other org's rows never appear.
    assert _get(world, "/app/orders", token_org=OTHER_ORG).status_code == 403
    assert _get(world, "/app/notices", token_org=OTHER_ORG).status_code == 403
    assert _get(world, "/app/orders?summary=1", token_org=OTHER_ORG).status_code == 403
    assert owner_fga.checks[-1] == (f"user:{_owner_sub(OTHER_ORG)}", "owner", f"org:{ORG}")
    theirs = market[OTHER_RESTAURANT]
    other_refs = {ids.org_order_ref(ORG, theirs[k]) for k in theirs}
    assert other_refs.isdisjoint(refs)


@db
def test_orders_require_owner(world: str, owner_fga: FakeFga) -> None:
    client = _client(world)
    for path in ("/app/orders", "/app/orders?summary=1", "/app/notices"):
        sep = "&" if "?" in path else "?"
        assert client.get(f"{path}{sep}org_id={ORG}").status_code == 401, path
        assert client.get(path, headers=_auth(_token())).status_code == 400, path
    assert owner_fga.checks == []
    owner_fga.fail = True
    assert _get(world, "/app/orders").status_code == 503
    assert _get(world, "/app/notices").status_code == 503
    owner_fga.fail = False
    for bad in (
        "/app/orders?summary=2",
        "/app/notices?limit=0",
        "/app/notices?limit=51",
        "/app/notices?limit=x",
    ):
        assert _get(world, bad).status_code == 400, bad


@db
def test_no_household_fields(world: str, market: dict[str, Any]) -> None:
    """XG14: no household id, name, person or address, and no order id, in any response."""
    with psycopg.connect(world) as conn:
        secrets = {HOUSEHOLD, OTHER_HOUSEHOLD, "Test household", "1 Main St", "45202"}
        for row in conn.execute("SELECT order_id, address_ref FROM orders").fetchall():
            secrets |= {str(row[0]), str(row[1])}
        for row in conn.execute("SELECT user_id FROM household_members").fetchall():
            secrets.add(str(row[0]))
        for row in conn.execute("SELECT agent_id FROM agent_cards").fetchall():
            if not row[0].startswith(("restaurant:", "grocery:")):
                secrets.add(row[0])
    for path in ("/app/orders", "/app/orders?summary=1", "/app/notices"):
        resp = _get(world, path)
        assert resp.status_code == 200, path
        text = resp.text
        assert [s for s in secrets if s in text] == [], path
    keys = {k for r in _get(world, "/app/orders").json()["orders"] for k in r}
    assert keys == {"ref", "kind", "state", "amount_minor", "currency", "updated_at", "rationale"}
    keys = {k for n in _get(world, "/app/notices").json()["notices"] for k in n}
    assert keys == {"kind", "text", "at"}


@db
def test_orders_summary(world: str, market: dict[str, Any]) -> None:
    with psycopg.connect(world) as conn:
        buyer = f"household:{HOUSEHOLD}"
        now = datetime.now(UTC)
        won = _order(conn, HOUSEHOLD, RESTAURANT, "authorized", now)
        _quote(conn, won, RESTAURANT, buyer, "accepted", at=now, expires=now + timedelta(minutes=5))
        # Yesterday's rows are not today's.
        old = now - timedelta(days=2)
        gone = _order(conn, HOUSEHOLD, RESTAURANT, "delivered", old)
        _quote(conn, gone, RESTAURANT, buyer, "accepted", at=old, expires=old)
    resp = _get(world, "/app/orders?summary=1")
    assert resp.status_code == 200
    # The expired quote was created 2 hours ago: today's only after 02:00 UTC.
    day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    expired_today = int(datetime.now(UTC) - timedelta(hours=2) >= day)
    # Orders today with this org's seller: placed, cancelled, delivered, authorized (each
    # created at most 40 minutes ago, so today after 00:40 UTC).
    recent = [timedelta(minutes=m) for m in (5, 30, 40, 0)]
    orders_today = sum(int(datetime.now(UTC) - d >= day) for d in recent)
    assert resp.json() == {
        "quotes_today": 2 + expired_today,  # the open one, the won one
        "won_today": 1,
        "orders_today": orders_today,
    }


@db
def test_notice_kinds_closed(world: str, market: dict[str, Any]) -> None:
    """Every notice is one of the three kinds, with its one fixed sentence, newest first."""
    assert set(owner_views.NOTICE_TEXT) == {
        "counter_refused_floor",
        "offer_expired",
        "order_cancelled",
    }
    notices = _get(world, "/app/notices").json()["notices"]
    assert len(notices) == 3
    for n in notices:
        assert n["text"] == owner_views.NOTICE_TEXT[n["kind"]]
    ats = [n["at"] for n in notices]
    assert ats == sorted(ats, reverse=True)
    assert [n["kind"] for n in _get(world, "/app/notices?limit=1").json()["notices"]] == [
        notices[0]["kind"]
    ]
