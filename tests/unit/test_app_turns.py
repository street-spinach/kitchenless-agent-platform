"""`GET /app/orders/{ref}/turns` (showcase.md §6): the owner's negotiation sheet. The org's
own turns only, `floor_minor` where the owner rule bound (identity-authz I33), `live` while
the order is quoting or negotiating, and no household field or other seller's offer."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from starlette.testclient import TestClient

from services.api import main as api
from services.common import ids
from services.identity import fga, token_source
from tests.unit import api_world as aw
from tests.unit import experience_world as ew
from tests.unit import identity_world as iw

db = pytest.mark.db

ORG = "square-restaurant-us"
OTHER_ORG = "mock-restaurant-us"
RESTAURANT = "restaurant:square-us"
OTHER_RESTAURANT = "restaurant:mock-us"
COURIER = "delivery:mock-us"
HOUSEHOLD = "h-turns-1"
RATIONALE = "Fresh today and in stock."
TURN_KEYS = {"at", "by", "kind", "text", "amount_minor", "rationale", "floor_minor"}


class FakeFga:
    def __init__(self) -> None:
        self.tuples: set[tuple[str, str, str]] = set()

    def check(self, user: str, relation: str, object: str) -> tuple[bool, str]:
        return (user, relation, object) in self.tuples, "model-1"


def _sub(org_id: str) -> str:
    return f"owner.{org_id}"  # `MockSource.owner_token`'s subject


@pytest.fixture
def owner_fga(monkeypatch: pytest.MonkeyPatch) -> FakeFga:
    fake = FakeFga()
    monkeypatch.setattr(fga, "check", fake.check)
    for org_id in (ORG, OTHER_ORG):
        fake.tuples.add((f"user:{_sub(org_id)}", "owner", f"org:{org_id}"))
    return fake


_CLOCK = iter(range(1, 10_000))


def _msg(
    conn: psycopg.Connection,
    run_id: uuid.UUID,
    conversation: uuid.UUID | None,
    sender: str,
    to: str,
    mtype: str,
    body: dict[str, Any],
    start: datetime,
) -> uuid.UUID:
    message_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO agent_messages (message_id, run_id, conversation_id, from_agent, to_agent,"
        " to_host, type, body, created_at) VALUES (%s, %s, %s, %s, %s, 'agents', %s, %s, %s)",
        (
            message_id,
            run_id,
            conversation,
            sender,
            to,
            mtype,
            Jsonb(body),
            start + timedelta(seconds=next(_CLOCK)),
        ),
    )
    return message_id


def _quote(
    conn: psycopg.Connection,
    order_id: uuid.UUID,
    run_id: uuid.UUID,
    seller: str,
    buyer: str,
    status: str,
) -> uuid.UUID:
    conversation = uuid.uuid4()
    conn.execute(
        "INSERT INTO quotes (quote_id, run_id, order_id, conversation_id, seller_agent_id,"
        " buyer_agent_id, items, total_minor, currency, expires_at, status)"
        " VALUES (%s, %s, %s, %s, %s, %s, '[]', 4800, 'USD', now() + interval '5 min', %s)",
        (uuid.uuid4(), run_id, order_id, conversation, seller, buyer, status),
    )
    return conversation


def _request(order_id: uuid.UUID) -> dict[str, Any]:
    return {
        "order_id": str(order_id),
        "kind": "dinner",
        "items": [
            {"item": "Margherita", "qty": 1, "unit": "each"},
            {"item": "Salad", "qty": 1, "unit": "each"},
        ],
        "constraints": {"allergens": ["peanuts"], "diets": ["vegetarian"]},
        "currency": "USD",
        "zone_id": "us-cin-1",
        "window": {"start": "2026-11-04T18:00:00Z", "end": "2026-11-04T19:00:00Z"},
        "reply_by": "2026-11-04T17:00:00Z",
    }


def _offer(order_id: uuid.UUID, total: int, rationale: str | None = None) -> dict[str, Any]:
    return {
        "order_id": str(order_id),
        "quote_id": str(uuid.uuid4()),
        "round": 0,
        "lines": [],
        "total_minor": total,
        "currency": "USD",
        "rationale": rationale,
    }


@pytest.fixture
def world(pg: str, owner_fga: FakeFga) -> dict[str, Any]:
    """One household asks both orgs for dinner (negotiating, live): the org offers, the
    household counters below the owner's floor and the org answers at the floor; the other
    org offers less. A second order the org sold is placed, booked and in delivery."""
    start = datetime.now(UTC) - timedelta(hours=1)
    with psycopg.connect(pg) as conn:
        for org_id in (ORG, OTHER_ORG):
            conn.execute(
                "INSERT INTO organizations (org_id, name, region) VALUES (%s, %s, 'us')",
                (org_id, org_id),
            )
        for agent_id, cls, org_id in (
            (RESTAURANT, "restaurant", ORG),
            (OTHER_RESTAURANT, "restaurant", OTHER_ORG),
            (COURIER, "delivery", "mock-courier-us"),
        ):
            conn.execute(
                "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
                " VALUES (%s, %s, %s, 'us', 'agents')",
                (agent_id, cls, f"org:{org_id}"),
            )
        buyer = iw.add_world(conn, HOUSEHOLD).household_agent

        # The live negotiation.
        live, run = ew.order(conn, HOUSEHOLD, "negotiating", start)
        mine = _quote(conn, live, run, RESTAURANT, buyer, "countered")
        theirs = _quote(conn, live, run, OTHER_RESTAURANT, buyer, "open")
        _msg(conn, run, mine, buyer, RESTAURANT, "quote_request", _request(live), start)
        _msg(conn, run, theirs, buyer, OTHER_RESTAURANT, "quote_request", _request(live), start)
        _msg(conn, run, mine, RESTAURANT, buyer, "offer", _offer(live, 4800, RATIONALE), start)
        _msg(conn, run, theirs, OTHER_RESTAURANT, buyer, "offer", _offer(live, 4555), start)
        counter = _msg(
            conn, run, mine, buyer, RESTAURANT, "counter_offer", _offer(live, 3000), start
        )
        conn.execute(
            "INSERT INTO agent_traces (run_id, seq, agent_id, kind, payload)"
            " VALUES (%s, 1, %s, 'decision', %s)",
            (
                run,
                RESTAURANT,
                Jsonb({"node": "owner_floor", "floor_minor": 3840, "ref": str(counter)}),
            ),
        )
        final = {**_offer(live, 3840), "final": True}
        _msg(conn, run, mine, RESTAURANT, buyer, "offer", final, start)
        # The other org's own counter turn, with its own floor decision, never shows here.
        other_counter = _msg(
            conn, run, theirs, buyer, OTHER_RESTAURANT, "counter_offer", _offer(live, 4100), start
        )
        conn.execute(
            "INSERT INTO agent_traces (run_id, seq, agent_id, kind, payload)"
            " VALUES (%s, 2, %s, 'decision', %s)",
            (
                run,
                OTHER_RESTAURANT,
                Jsonb({"node": "owner_floor", "floor_minor": 4321, "ref": str(other_counter)}),
            ),
        )

        # The sold order.
        sold, run2 = ew.order(conn, HOUSEHOLD, "in_delivery", start)
        conn.execute(
            "UPDATE orders SET seller_agent_id = %s, total_minor = 3840 WHERE order_id = %s",
            (RESTAURANT, sold),
        )
        conv = _quote(conn, sold, run2, RESTAURANT, buyer, "accepted")
        accept = {"order_id": str(sold), "quote_id": str(uuid.uuid4()), "total_minor": 3840}
        confirm = {"order_id": str(sold), "state": "placed", "delivery_agent_id": COURIER}
        booked = {"order_id": str(sold), "state": "courier_booked", "delivery_agent_id": COURIER}
        status = {"order_id": str(sold), "state": "in_delivery"}
        _msg(conn, run2, conv, buyer, RESTAURANT, "accept", accept, start)
        _msg(conn, run2, conv, RESTAURANT, buyer, "order_confirm", confirm, start)
        _msg(conn, run2, uuid.uuid4(), COURIER, RESTAURANT, "delivery_booked", booked, start)
        _msg(conn, run2, None, COURIER, buyer, "delivery_booked", booked, start)
        _msg(conn, run2, None, "system:api", buyer, "order_status", status, start)

        # An order the org never quoted.
        stranger, _ = ew.order(conn, HOUSEHOLD, "quoting", start)
    return {"pg": pg, "live": live, "sold": sold, "stranger": stranger}


def _get(pg: str, ref: str, org_id: str = ORG, token_org: str | None = None) -> Any:
    client = TestClient(api.create_app(aw.connect_to(pg)))
    token = token_source.MockSource().owner_token(token_org or org_id)
    return client.get(
        f"/app/orders/{ref}/turns?org_id={org_id}",
        headers={"Authorization": f"Bearer {token}"},
    )


@db
def test_turns_fields(world: dict[str, Any]) -> None:
    pg = world["pg"]
    ref = ids.org_order_ref(ORG, world["live"])
    resp = _get(pg, ref)
    assert resp.status_code == 200
    data = resp.json()
    assert (data["ref"], data["state"], data["live"]) == (ref, "negotiating", True)
    turns = data["turns"]
    for turn in turns:
        assert set(turn) <= TURN_KEYS
        datetime.fromisoformat(turn["at"])
    plain = [{k: v for k, v in t.items() if k != "at"} for t in turns]
    assert plain == [
        {"by": "them", "kind": "request", "text": "2 items · no peanuts, vegetarian"},
        {"by": "you", "kind": "offer", "amount_minor": 4800, "rationale": RATIONALE},
        {"by": "them", "kind": "counter", "amount_minor": 3000, "floor_minor": 3840},
        {"by": "you", "kind": "offer", "amount_minor": 3840, "rationale": None},
    ]
    # Another seller's offer, counter or floor is never shown.
    assert "4555" not in resp.text and "4100" not in resp.text and "4321" not in resp.text

    sold = _get(pg, ids.org_order_ref(ORG, world["sold"])).json()
    assert (sold["state"], sold["live"]) == ("in_delivery", False)
    plain = [{k: v for k, v in t.items() if k != "at"} for t in sold["turns"]]
    assert plain == [
        {"by": "them", "kind": "accept", "amount_minor": 3840},
        {"by": "you", "kind": "status", "text": "Placed"},
        {"by": "system", "kind": "courier", "text": "Courier booked"},
        {"by": "system", "kind": "status", "text": "In delivery"},
    ]


@db
def test_other_org_404(world: dict[str, Any]) -> None:
    pg = world["pg"]
    mine = ids.org_order_ref(ORG, world["live"])
    sold = ids.org_order_ref(ORG, world["sold"])
    # Another org's owner asking for this org's refs, under their own org: not found.
    assert _get(pg, mine, org_id=OTHER_ORG).status_code == 404
    assert _get(pg, sold, org_id=OTHER_ORG).status_code == 404
    # ... and under this org: refused before anything is read.
    assert _get(pg, mine, org_id=ORG, token_org=OTHER_ORG).status_code == 403
    # An order the org never quoted, and a malformed ref, are not found either.
    assert _get(pg, ids.org_order_ref(ORG, world["stranger"])).status_code == 404
    assert _get(pg, "not-a-ref").status_code == 404
    # The other org sees its own turns of the shared order, never this org's.
    theirs = _get(pg, ids.org_order_ref(OTHER_ORG, world["live"]), org_id=OTHER_ORG)
    assert theirs.status_code == 200
    amounts = [t.get("amount_minor") for t in theirs.json()["turns"]]
    assert amounts == [None, 4555, 4100]
    assert theirs.json()["turns"][2]["floor_minor"] == 4321
    # No token: 401.
    client = TestClient(api.create_app(aw.connect_to(pg)))
    assert client.get(f"/app/orders/{mine}/turns?org_id={ORG}").status_code == 401


@db
def test_no_household_fields(world: dict[str, Any]) -> None:
    pg = world["pg"]
    with psycopg.connect(pg) as conn:
        secrets = {HOUSEHOLD, "Test household", "1 Main St", "45202", "Margherita", "us-cin-1"}
        for row in conn.execute("SELECT order_id, address_ref FROM orders").fetchall():
            secrets |= {str(row[0]), str(row[1])}
        for row in conn.execute("SELECT user_id FROM household_members").fetchall():
            secrets.add(str(row[0]))
        for row in conn.execute("SELECT display_name FROM users").fetchall():
            if row[0]:
                secrets.add(row[0])
        for row in conn.execute("SELECT message_id, run_id FROM agent_messages").fetchall():
            secrets |= {str(row[0]), str(row[1])}
        secrets |= {"household:", COURIER, OTHER_RESTAURANT, RESTAURANT, "system:api"}
    for order in ("live", "sold"):
        resp = _get(pg, ids.org_order_ref(ORG, world[order]))
        assert resp.status_code == 200
        assert [s for s in secrets if s in resp.text] == [], order
        assert set(resp.json()) == {"ref", "state", "live", "turns"}
