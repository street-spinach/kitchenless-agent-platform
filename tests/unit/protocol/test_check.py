"""The sender check C1-C5 (agent-protocol §6, P4)."""

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from services.common import ids
from services.common.protocol import ProtocolError, check
from services.common.protocol.a2a import AgentMessageRow
from services.common.protocol.schemas import QuoteDecision

pytestmark = pytest.mark.db

HH1, HH2 = "household:hh1", "household:hh2"
GROCER = "grocery:mock-us"
COURIER, OTHER_COURIER = "delivery:mock-us", "delivery:other"
MEMBER1, MEMBER2 = "consumer:us-1-a", "consumer:us-2-a"
EXTERNAL = "external:a2a-demo"


@dataclass
class World:
    dsn: str
    run1: uuid.UUID  # weekly run of hh1
    run2: uuid.UUID  # weekly run of hh2
    demo_run: uuid.UUID  # a2a_demo run
    order1: uuid.UUID  # hh1's order: seller GROCER, courier COURIER
    quote1: uuid.UUID  # GROCER -> HH1 quote on order1
    courier_quote: uuid.UUID  # COURIER -> GROCER quote on order1
    shortfall1: uuid.UUID  # order1's shortfall
    request1: uuid.UUID  # HH1 -> GROCER quote_request in run1
    demo_order: uuid.UUID
    demo_quote: uuid.UUID  # GROCER -> EXTERNAL quote on demo_order


def _card(conn: psycopg.Connection, agent_id: str, cls: str, owner: str, host: str) -> None:
    conn.execute(
        "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
        " VALUES (%s, %s, %s, 'us', %s)",
        (agent_id, cls, owner, host),
    )


def _household(conn: psycopg.Connection, household_id: str, user_id: uuid.UUID) -> uuid.UUID:
    conn.execute(
        "INSERT INTO households (household_id, name, region, zone_id, timezone, currency)"
        " VALUES (%s, 'H', 'us', 'z1', 'UTC', 'USD')",
        (household_id,),
    )
    conn.execute("INSERT INTO users (user_id, display_name) VALUES (%s, 'M')", (user_id,))
    conn.execute(
        "INSERT INTO household_members (household_id, user_id, role) VALUES (%s, %s, 'admin')",
        (household_id, user_id),
    )
    address_ref = uuid.uuid4()
    conn.execute(
        "INSERT INTO household_addresses"
        " (address_ref, household_id, line1, city, region, postal_code, country)"
        " VALUES (%s, %s, '1 Main', 'C', 'R', '00000', 'US')",
        (address_ref, household_id),
    )
    return address_ref


def _run(conn: psycopg.Connection, kind: str, household_id: str | None, trigger: dict) -> uuid.UUID:
    run_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO runs (run_id, household_id, kind, trigger, modes)"
        " VALUES (%s, %s, %s, %s, '{}')",
        (run_id, household_id, kind, Jsonb(trigger)),
    )
    return run_id


def _order(
    conn: psycopg.Connection,
    run_id: uuid.UUID,
    household_id: str,
    address_ref: uuid.UUID,
    seller: str | None = None,
    courier: str | None = None,
) -> uuid.UUID:
    order_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO orders (order_id, run_id, household_id, kind, currency, address_ref,"
        " seller_agent_id, delivery_agent_id) VALUES (%s, %s, %s, 'grocery', 'USD', %s, %s, %s)",
        (order_id, run_id, household_id, address_ref, seller, courier),
    )
    return order_id


def _quote(
    conn: psycopg.Connection, run_id: uuid.UUID, order_id: uuid.UUID, seller: str, buyer: str
) -> uuid.UUID:
    quote_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO quotes (quote_id, run_id, order_id, conversation_id, seller_agent_id,"
        " buyer_agent_id, items, total_minor, currency, expires_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, '[]', 1248, 'USD', now() + interval '1 hour')",
        (quote_id, run_id, order_id, uuid.uuid4(), seller, buyer),
    )
    return quote_id


@pytest.fixture
def world(pg: str) -> World:
    user1, user2 = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(pg) as conn:
        address1 = _household(conn, "hh1", user1)
        address2 = _household(conn, "hh2", user2)
        _card(conn, HH1, "household", HH1, "agents")
        _card(conn, HH2, "household", HH2, "agents")
        _card(conn, MEMBER1, "consumer", f"user:{user1}", "agents")
        _card(conn, MEMBER2, "consumer", f"user:{user2}", "agents")
        _card(conn, GROCER, "grocery", "org:o1", "agents")
        _card(conn, COURIER, "delivery", "org:o2", "agents")
        _card(conn, OTHER_COURIER, "delivery", "org:o3", "agents")
        _card(conn, EXTERNAL, "external", "system", "external")

        run1 = _run(conn, "weekly", "hh1", {"source": "whatsapp", "user_id": str(user1)})
        run2 = _run(conn, "weekly", "hh2", {"source": "whatsapp", "user_id": str(user2)})
        demo_run = _run(conn, "a2a_demo", "hh1", {"source": "a2a"})

        order1 = _order(conn, run1, "hh1", address1, GROCER, COURIER)
        _order(conn, run2, "hh2", address2)
        quote1 = _quote(conn, run1, order1, GROCER, HH1)
        courier_quote = _quote(conn, run1, order1, COURIER, GROCER)
        shortfall1 = ids.shortfall_id(order1)
        conn.execute(
            "INSERT INTO shortfalls (shortfall_id, run_id, order_id, seller_agent_id, lines,"
            " due_at) VALUES (%s, %s, %s, %s, '[]', now() + interval '10 minutes')",
            (shortfall1, run1, order1, GROCER),
        )
        request1 = uuid.uuid4()
        conn.execute(
            "INSERT INTO agent_messages (message_id, run_id, from_agent, to_agent, to_host,"
            " type, body) VALUES (%s, %s, %s, %s, 'agents', 'quote_request', '{}')",
            (request1, run1, HH1, GROCER),
        )
        demo_order = _order(conn, demo_run, "hh1", address1)
        demo_quote = _quote(conn, demo_run, demo_order, GROCER, EXTERNAL)
    return World(
        dsn=pg,
        run1=run1,
        run2=run2,
        demo_run=demo_run,
        order1=order1,
        quote1=quote1,
        courier_quote=courier_quote,
        shortfall1=shortfall1,
        request1=request1,
        demo_order=demo_order,
        demo_quote=demo_quote,
    )


def _check(
    w: World, type_: str, from_agent: str, to_agent: str, body: dict[str, Any], run_id: uuid.UUID
) -> BaseModel:
    msg = AgentMessageRow(
        message_id=uuid.uuid4(),
        run_id=run_id,
        conversation_id=None,
        causation_id=None,
        from_agent=from_agent,
        to_agent=to_agent,
        type=type_,
        body=body,
    )

    async def go() -> BaseModel:
        async with await psycopg.AsyncConnection.connect(w.dsn) as conn:
            return await check(conn, msg)

    return asyncio.run(go())


def _refused(code: str, w: World, *args: Any) -> None:
    with pytest.raises(ProtocolError) as exc:
        _check(w, *args)
    assert exc.value.code == code
    assert len(exc.value.detail) <= 200


def _with(samples: dict[str, dict], type_: str, **ids_: Any) -> dict:
    return {**samples[type_], **{k: str(v) for k, v in ids_.items()}}


def test_valid_message_passes(world: World, samples: dict[str, dict]) -> None:
    w = world
    accept = _with(samples, "accept", order_id=w.order1, quote_id=w.quote1)
    body = _check(w, "accept", HH1, GROCER, accept, w.run1)
    assert isinstance(body, QuoteDecision)
    assert body.quote_id == w.quote1

    # People types: the sender is a consumer whose user is a member of the run's household.
    _check(w, "start_weekly_order", MEMBER1, HH1, samples["start_weekly_order"], w.run1)
    _check(w, "user_message", "channel:whatsapp", MEMBER1, samples["user_message"], w.run1)
    relay = _with(samples, "shortfall_relay", order_id=w.order1, shortfall_id=w.shortfall1)
    _check(w, "shortfall_relay", HH1, MEMBER1, relay, w.run1)
    # A seller's reject goes to the quote's buyer.
    reject = _with(samples, "reject", order_id=w.order1, quote_id=w.quote1)
    _check(w, "reject", GROCER, HH1, reject, w.run1)
    # The notice opens the shortfall, so its row need not exist yet.
    notice = _with(samples, "shortfall_notice", order_id=w.order1, shortfall_id=w.shortfall1)
    with psycopg.connect(w.dsn) as conn:
        conn.execute("DELETE FROM shortfalls")
    _check(w, "shortfall_notice", GROCER, HH1, notice, w.run1)


def test_unknown_type_refused(world: World) -> None:
    _refused("unknown_type", world, "price_hint", HH1, GROCER, {}, world.run1)


def test_wrong_sender_class_refused(world: World, samples: dict[str, dict]) -> None:
    w = world
    offer = _with(samples, "offer", order_id=w.order1, quote_id=w.quote1)
    _refused("wrong_sender_class", w, "offer", HH1, GROCER, offer, w.run1)
    # A business agent never talks to a consumer agent.
    status = _with(samples, "order_confirm", order_id=w.order1)
    _refused("wrong_sender_class", w, "order_confirm", GROCER, MEMBER1, status, w.run1)


def test_system_sender_only_for_own_types(world: World, samples: dict[str, dict]) -> None:
    w = world
    accept = _with(samples, "accept", order_id=w.order1, quote_id=w.quote1)
    _refused("wrong_sender_class", w, "accept", "system:api", GROCER, accept, w.run1)
    status = _with(samples, "order_status", order_id=w.order1)
    _refused("wrong_sender_class", w, "order_status", "system:sweeper", HH1, status, w.run1)
    _refused("wrong_sender_class", w, "order_status", "channel:whatsapp", HH1, status, w.run1)
    _check(w, "order_status", "system:api", HH1, status, w.run1)


def test_accept_from_other_household_refused(world: World, samples: dict[str, dict]) -> None:
    w = world
    accept = _with(samples, "accept", order_id=w.order1, quote_id=w.quote1)
    _refused("wrong_party", w, "accept", HH2, GROCER, accept, w.run1)
    # A consumer of another household cannot start this run's order either.
    _refused(
        "wrong_party", w, "start_weekly_order", MEMBER2, HH1, samples["start_weekly_order"], w.run1
    )


def test_reference_from_other_run_refused(world: World, samples: dict[str, dict]) -> None:
    w = world
    accept = _with(samples, "accept", order_id=w.order1, quote_id=w.quote1)
    _refused("unknown_reference", w, "accept", HH2, GROCER, accept, w.run2)
    error = _with(samples, "protocol_error", ref_message_id=w.request1)
    _refused("unknown_reference", w, "protocol_error", GROCER, HH1, error, w.run2)


def test_unknown_shortfall_refused(world: World, samples: dict[str, dict]) -> None:
    w = world
    decision = _with(samples, "shortfall_decision", order_id=w.order1, shortfall_id=uuid.uuid4())
    _refused("unknown_reference", w, "shortfall_decision", HH1, GROCER, decision, w.run1)
    # A notice must carry the order's own shortfall id.
    notice = _with(samples, "shortfall_notice", order_id=w.order1, shortfall_id=uuid.uuid4())
    _refused("wrong_party", w, "shortfall_notice", GROCER, HH1, notice, w.run1)


def test_delivery_request_to_unassigned_courier_refused(
    world: World, samples: dict[str, dict]
) -> None:
    w = world
    request = _with(samples, "delivery_request", order_id=w.order1, quote_id=w.courier_quote)
    _refused("wrong_party", w, "delivery_request", GROCER, OTHER_COURIER, request, w.run1)
    _check(w, "delivery_request", GROCER, COURIER, request, w.run1)


def test_protocol_error_without_matching_ref_refused(
    world: World, samples: dict[str, dict]
) -> None:
    w = world
    error = _with(samples, "protocol_error", ref_message_id=w.request1)
    # The refused quote_request went HH1 -> GROCER, so only GROCER may refuse it, to HH1.
    _refused("wrong_party", w, "protocol_error", COURIER, HH1, error, w.run1)
    _refused("wrong_party", w, "protocol_error", GROCER, HH2, error, w.run1)
    missing = _with(samples, "protocol_error", ref_message_id=uuid.uuid4())
    _refused("unknown_reference", w, "protocol_error", GROCER, HH1, missing, w.run1)
    _check(w, "protocol_error", GROCER, HH1, error, w.run1)


def test_external_sender_must_be_quote_buyer(world: World, samples: dict[str, dict]) -> None:
    w = world
    counter = _with(samples, "counter_offer", order_id=w.order1, quote_id=w.quote1)
    _refused("wrong_party", w, "counter_offer", EXTERNAL, GROCER, counter, w.run1)
    request = _with(samples, "quote_request", order_id=w.order1)
    _refused("wrong_party", w, "quote_request", EXTERNAL, GROCER, request, w.run1)

    demo_request = _with(samples, "quote_request", order_id=w.demo_order)
    _check(w, "quote_request", EXTERNAL, GROCER, demo_request, w.demo_run)
    demo_counter = _with(samples, "counter_offer", order_id=w.demo_order, quote_id=w.demo_quote)
    _check(w, "counter_offer", EXTERNAL, GROCER, demo_counter, w.demo_run)
