"""The internal A2A endpoint and the demo context (issue #77).

Agent-protocol §11 (the HTTP A2A binding), P19 (`services/agents/a2a/server.py`) and P20
(`services/agents/a2a/demo.py`). The *db* tests use the `pg` fixture; the others never open
a connection.
"""

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from starlette.applications import Starlette
from starlette.testclient import TestClient

from services.agents.a2a import demo
from services.agents.a2a.server import SENDER, A2AServer
from services.common import ids, modes, outbox, runs
from services.common.config import Settings
from services.common.protocol.a2a import AgentMessageRow
from services.common.protocol.check import check

db = pytest.mark.db
_ROW_FIELDS = (
    "message_id",
    "run_id",
    "conversation_id",
    "causation_id",
    "from_agent",
    "to_agent",
    "type",
    "body",
)

TOKEN = "test-a2a-token"
GROCERY = "grocery:mock-us"
RESTAURANT = "restaurant:mock-us"
ZONE = "us-oh-45202"
A2A_DOMAIN = "a2a-protocol.org"
ERROR_INFO = "type.googleapis.com/google.rpc.ErrorInfo"
LINES = [
    {
        "line": 0,
        "requested_item": "oat milk",
        "offered_sku": "SKU-1",
        "offered_name": "Oat milk 1L",
        "qty": 2,
        "unit_minor": 399,
        "substituted_for": None,
    }
]


# Fixtures ---------------------------------------------------------------------------------


def _seed(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO organizations (org_id, name, region) VALUES"
            " ('mock-grocery-us', 'Mock Grocery US', 'us'),"
            " ('mock-restaurant-us', 'Mock Kitchen US', 'us')"
        )
        cards = (
            (GROCERY, "grocery", "org:mock-grocery-us", "agents", True),
            (RESTAURANT, "restaurant", "org:mock-restaurant-us", "agents", True),
            ("grocery:off-us", "grocery", "org:mock-grocery-us", "agents", False),
            ("delivery:mock-us", "delivery", "org:mock-courier-us", "agents", True),
            ("household:us-1", "household", "household:us-1", "agents", True),
            (SENDER, "external", "system", "external", True),
        )
        for agent_id, cls, owner, host, enabled in cards:
            conn.execute(
                "INSERT INTO agent_cards (agent_id, class, owner_ref, region, zone_ids, currency,"
                " host_process, config, enabled) VALUES (%s, %s, %s, 'us', %s, 'USD', %s, %s, %s)",
                (
                    agent_id,
                    cls,
                    owner,
                    [ZONE],
                    host,
                    Jsonb({"description": f"{agent_id} card", "card_version": "1.0.0"}),
                    enabled,
                ),
            )
        for household_id in (demo.HOUSEHOLD_ID, "us-1"):
            conn.execute(
                "INSERT INTO households (household_id, name, env, region, zone_id, timezone,"
                " currency) VALUES (%s, 'Test', 'eval', 'us', %s, 'America/New_York', 'USD')",
                (household_id, ZONE),
            )
            conn.execute(
                "INSERT INTO household_addresses (address_ref, household_id, line1, city, region,"
                " postal_code, country) VALUES (%s, %s, '300 Main Street', 'Cincinnati', 'OH',"
                " '45202', 'US')",
                (ids.seed_address_ref(household_id), household_id),
            )


@pytest.fixture
def world(pg: str, monkeypatch: pytest.MonkeyPatch) -> str:
    settings = Settings.from_env(
        {"SERVICE_NAME": "migrate", "DATABASE_URL": "x", "LOG_LEVEL": "info"}
    )
    for module in (runs, modes, demo):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    _seed(pg)
    return pg


def _connect_to(pg: str):  # noqa: ANN202
    @contextlib.asynccontextmanager
    async def connect() -> AsyncIterator[psycopg.AsyncConnection]:
        async with await psycopg.AsyncConnection.connect(pg) as conn:
            yield conn

    return connect


def _no_db():  # noqa: ANN202
    @contextlib.asynccontextmanager
    async def connect() -> AsyncIterator[psycopg.AsyncConnection]:
        raise AssertionError("this test must not open a connection")
        yield  # pragma: no cover

    return connect


def _client(pg: str | None = None, **kwargs: Any) -> TestClient:
    options: dict[str, Any] = {"send_wait_seconds": 0, "rate_per_second": 1000, **kwargs}
    connect = _connect_to(pg) if pg else _no_db()
    server = A2AServer(connect, token=TOKEN, **options)
    return TestClient(Starlette(routes=server.routes()))


def _rpc(
    client: TestClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    agent: str = GROCERY,
    token: str | None = TOKEN,
    version: str | None = "1.0",
) -> Any:
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if version is not None:
        headers["A2A-Version"] = version
    payload = {"jsonrpc": "2.0", "id": 7, "method": method, "params": params or {}}
    return client.post(f"/a2a/{agent}", json=payload, headers=headers)


def _message(
    type_: str,
    body: dict[str, Any],
    *,
    message_id: str = "m-1",
    task_id: str | None = None,
    context_id: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "messageId": message_id,
        "role": "ROLE_USER",
        "parts": [{"text": "hello"}, {"data": {"type": type_, "body": body}}],
    }
    if task_id:
        message["taskId"] = task_id
    if context_id:
        message["contextId"] = context_id
    return message


REQUEST_BODY = {
    "items": [{"item": "oat milk", "qty": 2, "unit": "l"}],
    "constraints": {"allergens": ["tree_nuts"], "diets": []},
    # Filled by the endpoint; these client values must be ignored (§11 step 4).
    "order_id": str(uuid.uuid4()),
    "zone_id": "elsewhere",
    "currency": "EUR",
}


def _send_request(client: TestClient, message_id: str = "m-1", **config: Any) -> Any:
    params: dict[str, Any] = {
        "message": _message("quote_request", REQUEST_BODY, message_id=message_id)
    }
    if config:
        params["configuration"] = config
    return _rpc(client, "SendMessage", params)


def _query(pg: str, sql: str, *params: Any) -> list[dict[str, Any]]:
    with psycopg.connect(pg) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _error(resp: Any) -> dict[str, Any]:
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert "result" not in body, body
    assert isinstance(body["error"]["data"], list)
    return body["error"]


def _result(resp: Any) -> dict[str, Any]:
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" not in body, body
    return body["result"]


def _seller_offer(
    pg: str, a2a_message_id: str = "m-1", *, status: str = "open", closed_by: str | None = None
) -> dict[str, Any]:
    """Act as the seller's step: mark the request processed, insert the quote and send the
    offer to `external:a2a-demo` (inserted `processed`, §11 "Replies to the client")."""
    message_id = ids.a2a_message_id(SENDER, a2a_message_id)
    quote_id = ids.quote_id(message_id)
    run_id = ids.a2a_run_id(message_id)
    order_id = ids.a2a_order_id(message_id)
    conversation_id = ids.conversation_id(message_id)
    expires_at = datetime.now(UTC) + timedelta(minutes=10)

    async def go() -> None:
        async with await psycopg.AsyncConnection.connect(pg) as conn:
            await conn.execute(
                "UPDATE agent_messages SET status = 'processed' WHERE message_id = %s",
                (message_id,),
            )
            await conn.execute(
                "INSERT INTO quotes (quote_id, run_id, order_id, conversation_id,"
                " seller_agent_id, buyer_agent_id, items, total_minor, currency, expires_at,"
                " status, closed_by) VALUES (%s, %s, %s, %s, %s, %s, %s, 798, 'USD', %s, %s, %s)",
                (
                    quote_id,
                    run_id,
                    order_id,
                    conversation_id,
                    GROCERY,
                    SENDER,
                    Jsonb(LINES),
                    expires_at,
                    status,
                    closed_by,
                ),
            )
            await outbox.send(
                conn,
                message_id=ids.message_id(message_id, 0),
                run_id=run_id,
                conversation_id=conversation_id,
                causation_id=message_id,
                from_agent=GROCERY,
                to_agent=SENDER,
                type="offer",
                body={
                    "order_id": str(order_id),
                    "quote_id": str(quote_id),
                    "round": 0,
                    "lines": LINES,
                    "total_minor": 798,
                    "currency": "USD",
                    "expires_at": expires_at.isoformat(),
                    "final": False,
                    "rationale": "Closest match in stock.",
                },
            )

    asyncio.run(go())
    return {
        "task_id": str(quote_id),
        "context_id": str(conversation_id),
        "run_id": run_id,
        "order_id": order_id,
    }


# HTTP checks -------------------------------------------------------------------------------


def test_missing_token_401() -> None:
    resp = _rpc(_client(), "GetTask", {"id": str(uuid.uuid4())}, token=None)
    assert resp.status_code == 401


def test_wrong_token_401() -> None:
    client = _client()
    assert _rpc(client, "GetTask", {"id": str(uuid.uuid4())}, token="nope").status_code == 401
    assert _rpc(client, "GetTask", {"id": str(uuid.uuid4())}, token="").status_code == 401
    resp = client.post(
        f"/a2a/{GROCERY}", content=b"{}", headers={"Authorization": f"Basic {TOKEN}"}
    )
    assert resp.status_code == 401


def test_request_over_32kb_413() -> None:
    big = {"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"pad": "x" * 33000}}
    resp = _client().post(
        f"/a2a/{GROCERY}",
        json=big,
        headers={"Authorization": f"Bearer {TOKEN}", "A2A-Version": "1.0"},
    )
    assert resp.status_code == 413


def test_rate_limit_429() -> None:
    now = [100.0]
    client = _client(rate_per_second=2, clock=lambda: now[0])
    codes = [_rpc(client, "Nope").status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    now[0] += 1.0  # the bucket refills at the rate
    assert _rpc(client, "Nope").status_code == 200


# Versions and error shape ------------------------------------------------------------------


def test_missing_version_header_not_supported() -> None:
    err = _error(_rpc(_client(), "GetTask", {"id": str(uuid.uuid4())}, version=None))
    assert err["code"] == -32009
    assert err["data"] == [
        {"@type": ERROR_INFO, "reason": "VERSION_NOT_SUPPORTED", "domain": A2A_DOMAIN}
    ]
    err = _error(_rpc(_client(), "GetTask", {"id": str(uuid.uuid4())}, version=""))
    assert err["code"] == -32009


def test_wrong_version_not_supported() -> None:
    client = _client()
    for version in ("0.3", "1.1", "1.0.0", "2.0"):
        assert _error(_rpc(client, "GetTask", {}, version=version))["code"] == -32009
    # `1.0` as a request parameter is accepted: the call reaches method handling.
    resp = client.post(
        f"/a2a/{GROCERY}?A2A-Version=1.0",
        json={"jsonrpc": "2.0", "id": 1, "method": "Nope", "params": {}},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert _error(resp)["code"] == -32601


def test_errors_carry_error_info_array() -> None:
    client = _client()
    headers = {"Authorization": f"Bearer {TOKEN}", "A2A-Version": "1.0"}
    parse = _error(client.post(f"/a2a/{GROCERY}", content=b"{not json", headers=headers))
    assert parse["code"] == -32700
    assert parse["data"] == []
    invalid = _error(client.post(f"/a2a/{GROCERY}", json={"id": 1}, headers=headers))
    assert invalid["code"] == -32600
    assert invalid["data"] == []
    missing = _error(_rpc(client, "tasks/get"))
    assert missing["code"] == -32601
    assert missing["data"] == []
    version = _error(_rpc(client, "SendMessage", version="0.3"))
    assert version["data"][0]["@type"] == ERROR_INFO
    assert version["data"][0]["domain"] == A2A_DOMAIN


# SendMessage -------------------------------------------------------------------------------


@db
def test_quote_request_creates_run_order_and_row(world: str) -> None:
    task = _result(_send_request(_client(world)))["task"]
    message_id = ids.a2a_message_id(SENDER, "m-1")
    run_id = ids.a2a_run_id(message_id)
    order_id = ids.a2a_order_id(message_id)
    assert task["id"] == str(ids.quote_id(message_id))
    assert task["contextId"] == str(ids.conversation_id(message_id))
    assert task["status"]["state"] == "TASK_STATE_SUBMITTED"

    [run] = _query(world, "SELECT * FROM runs")
    assert (run["run_id"], run["kind"], run["household_id"]) == (run_id, "a2a_demo", "a2a-demo")
    assert run["trigger"] == {"source": "a2a", "inbound_id": str(message_id)}
    [order] = _query(world, "SELECT * FROM orders")
    assert (order["order_id"], order["state"], order["kind"]) == (order_id, "draft", "grocery")
    assert order["address_ref"] == ids.seed_address_ref("a2a-demo")

    [row] = _query(world, "SELECT * FROM agent_messages")
    assert row["message_id"] == message_id
    assert (row["from_agent"], row["to_agent"], row["type"]) == (SENDER, GROCERY, "quote_request")
    assert row["status"] == "pending"
    assert row["conversation_id"] == ids.conversation_id(message_id)
    body = row["body"]
    assert body["order_id"] == str(order_id)
    assert (body["zone_id"], body["currency"], body["kind"]) == (ZONE, "USD", "grocery")
    assert body["items"] == REQUEST_BODY["items"]
    assert body["constraints"] == REQUEST_BODY["constraints"]
    [trace] = _query(world, "SELECT * FROM agent_traces WHERE kind = 'message'")
    assert trace["message_id"] == message_id
    assert len(trace["payload"]["request_hash"]) == 64
    assert trace["payload"]["summary"].startswith("Quote request for order")

    # The seller's consumer runs C1-C5 on the row (§6); it passes, so it is not refused.
    async def seller_check() -> None:
        async with await psycopg.AsyncConnection.connect(world) as conn:
            await check(conn, AgentMessageRow(**{k: row[k] for k in _ROW_FIELDS}))

    asyncio.run(seller_check())


@db
def test_failed_check_leaves_no_run(world: str) -> None:
    bad = {"items": [{"item": "oat milk", "unit": "l"}], "constraints": {"allergens": []}}
    params = {"message": _message("quote_request", bad)}
    err = _error(_rpc(_client(world), "SendMessage", params))
    assert err["code"] == -32602
    assert err["data"] == [{"@type": ERROR_INFO, "reason": "INVALID_BODY", "domain": "kitchenless"}]
    for table in ("runs", "orders", "agent_messages", "timers", "agent_traces"):
        assert _query(world, f"SELECT 1 FROM {table}") == [], table


@db
def test_duplicate_send_returns_same_task(world: str) -> None:
    client = _client(world)
    first = _result(_send_request(client))["task"]
    second = _result(_send_request(client))["task"]
    assert first["id"] == second["id"]
    assert first["contextId"] == second["contextId"]
    assert len(_query(world, "SELECT 1 FROM agent_messages")) == 1
    assert len(_query(world, "SELECT 1 FROM runs")) == 1
    assert len(_query(world, "SELECT 1 FROM orders")) == 1


@db
def test_reused_message_id_different_body_invalid_params(world: str) -> None:
    client = _client(world)
    _result(_send_request(client))
    other = {**REQUEST_BODY, "items": [{"item": "rye bread", "qty": 1, "unit": "each"}]}
    params = {"message": _message("quote_request", other)}
    err = _error(_rpc(client, "SendMessage", params))
    assert err["code"] == -32602
    [row] = _query(world, "SELECT body FROM agent_messages")
    assert row["body"]["items"] == REQUEST_BODY["items"]


@db
def test_accept_refused_with_unsupported_operation_and_no_row(world: str) -> None:
    client = _client(world)
    _result(_send_request(client))
    task = _seller_offer(world)
    before = _query(world, "SELECT message_id FROM agent_messages ORDER BY message_id")
    accept = _message(
        "accept",
        {"items_hash": "x" * 64, "total_minor": 798},
        message_id="m-accept",
        task_id=task["task_id"],
        context_id=task["context_id"],
    )
    err = _error(_rpc(client, "SendMessage", {"message": accept}))
    assert err["code"] == -32004
    assert err["data"] == [
        {"@type": ERROR_INFO, "reason": "UNSUPPORTED_OPERATION", "domain": A2A_DOMAIN}
    ]
    assert _query(world, "SELECT message_id FROM agent_messages ORDER BY message_id") == before
    assert _query(world, "SELECT 1 FROM agent_messages WHERE type = 'accept'") == []
    [trace] = _query(world, "SELECT * FROM agent_traces WHERE kind = 'error'")
    assert trace["payload"]["where"] == "a2a"
    assert trace["payload"]["reason"] == "accept_not_allowed"
    assert str(trace["conversation_id"]) == task["context_id"]
    assert trace["run_id"] == task["run_id"]

    got = _result(_rpc(client, "GetTask", {"id": task["task_id"]}))
    assert got["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    [order] = _query(world, "SELECT state FROM orders")
    assert order["state"] == "draft"


@db
def test_cancel_via_send_message_invalid_params(world: str) -> None:
    client = _client(world)
    _result(_send_request(client))
    task = _seller_offer(world)
    cancel = _message(
        "cancel",
        {"reason": "buyer_cancelled"},
        message_id="m-cancel",
        task_id=task["task_id"],
        context_id=task["context_id"],
    )
    err = _error(_rpc(client, "SendMessage", {"message": cancel}))
    assert err["code"] == -32602
    assert "CancelTask" in err["message"]
    assert _query(world, "SELECT 1 FROM agent_messages WHERE type = 'cancel'") == []


@db
def test_text_only_message_invalid_params(world: str) -> None:
    message = {"messageId": "m-text", "role": "ROLE_USER", "parts": [{"text": "two oat milks"}]}
    err = _error(_rpc(_client(world), "SendMessage", {"message": message}))
    assert err["code"] == -32602
    assert _query(world, "SELECT 1 FROM agent_messages") == []
    assert _query(world, "SELECT 1 FROM runs") == []


@db
def test_counter_needs_known_task(world: str) -> None:
    client = _client(world)
    counter_body = {"round": 1, "lines": LINES, "total_minor": 700, "currency": "USD"}
    unknown = _message(
        "counter_offer",
        counter_body,
        message_id="m-counter-0",
        task_id=str(uuid.uuid4()),
        context_id=str(uuid.uuid4()),
    )
    err = _error(_rpc(client, "SendMessage", {"message": unknown}))
    assert err["code"] == -32001
    assert _query(world, "SELECT 1 FROM agent_messages") == []

    _result(_send_request(client))
    task = _seller_offer(world)
    known = _message(
        "counter_offer",
        counter_body,
        message_id="m-counter-1",
        task_id=task["task_id"],
        context_id=task["context_id"],
    )
    got = _result(_rpc(client, "SendMessage", {"message": known}))["task"]
    assert got["id"] == task["task_id"]
    [row] = _query(world, "SELECT * FROM agent_messages WHERE type = 'counter_offer'")
    assert (row["from_agent"], row["to_agent"]) == (SENDER, GROCERY)
    assert row["run_id"] == task["run_id"]
    assert row["body"]["quote_id"] == task["task_id"]
    assert row["body"]["order_id"] == str(task["order_id"])


@db
def test_get_task_of_other_buyer_not_found(world: str) -> None:
    run_id, order_id, quote_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(world) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, household_id, kind, trigger, modes)"
            " VALUES (%s, 'us-1', 'weekly', '{}', '{}')",
            (run_id,),
        )
        conn.execute(
            "INSERT INTO orders (order_id, run_id, household_id, kind, currency, address_ref)"
            " VALUES (%s, %s, 'us-1', 'grocery', 'USD', %s)",
            (order_id, run_id, ids.seed_address_ref("us-1")),
        )
        conn.execute(
            "INSERT INTO quotes (quote_id, run_id, order_id, conversation_id, seller_agent_id,"
            " buyer_agent_id, items, total_minor, currency, expires_at) VALUES"
            " (%s, %s, %s, %s, %s, 'household:us-1', '[]', 798, 'USD', now() + interval '1h')",
            (quote_id, run_id, order_id, uuid.uuid4(), GROCERY),
        )
    client = _client(world)
    assert _error(_rpc(client, "GetTask", {"id": str(quote_id)}))["code"] == -32001
    assert _error(_rpc(client, "GetTask", {"id": str(uuid.uuid4())}))["code"] == -32001
    assert _error(_rpc(client, "CancelTask", {"id": str(quote_id)}))["code"] == -32001


@db
def test_cancel_task_inserts_buyer_cancelled(world: str) -> None:
    client = _client(world)
    _result(_send_request(client))
    task = _seller_offer(world)
    got = _result(_rpc(client, "CancelTask", {"id": task["task_id"]}))
    assert got["id"] == task["task_id"]
    [row] = _query(world, "SELECT * FROM agent_messages WHERE type = 'cancel'")
    assert row["message_id"] == ids.a2a_cancel_id(uuid.UUID(task["task_id"]))
    assert (row["from_agent"], row["to_agent"]) == (SENDER, GROCERY)
    assert row["body"] == {
        "order_id": str(task["order_id"]),
        "quote_id": task["task_id"],
        "reason": "buyer_cancelled",
    }
    assert row["status"] == "pending"
    # A second CancelTask before the seller's step sends nothing new.
    _result(_rpc(client, "CancelTask", {"id": task["task_id"]}))
    assert len(_query(world, "SELECT 1 FROM agent_messages WHERE type = 'cancel'")) == 1


@db
def test_cancel_terminal_task_not_cancelable(world: str) -> None:
    client = _client(world)
    _result(_send_request(client))
    task = _seller_offer(world, status="rejected", closed_by="seller")
    got = _result(_rpc(client, "GetTask", {"id": task["task_id"]}))
    assert got["status"]["state"] == "TASK_STATE_REJECTED"
    err = _error(_rpc(client, "CancelTask", {"id": task["task_id"]}))
    assert err["code"] == -32002
    assert err["data"][0]["reason"] == "TASK_NOT_CANCELABLE"
    assert _query(world, "SELECT 1 FROM agent_messages WHERE type = 'cancel'") == []


@db
def test_return_immediately_gives_submitted(world: str) -> None:
    client = _client(world, send_wait_seconds=10)
    started = time.monotonic()
    task = _result(_send_request(client, returnImmediately=True))["task"]
    assert time.monotonic() - started < 5
    assert task["status"]["state"] == "TASK_STATE_SUBMITTED"
    # GetTask finds the task before the seller has quoted.
    got = _result(_rpc(client, "GetTask", {"id": task["id"]}))
    assert got["status"]["state"] == "TASK_STATE_SUBMITTED"
    assert [m["parts"][0]["data"]["type"] for m in got["history"]] == ["quote_request"]
    # After the seller's offer: input required, with the offer as an artifact.
    _seller_offer(world)
    got = _result(_rpc(client, "GetTask", {"id": task["id"], "historyLength": 1}))
    assert got["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    [artifact] = got["artifacts"]
    assert artifact["parts"][0]["data"]["type"] == "offer"
    assert "rationale" not in artifact["parts"][0]["data"]["body"]
    [latest] = got["history"]
    assert latest["role"] == "ROLE_AGENT"
    assert latest["parts"][1] == {"text": "Closest match in stock."}


@db
def test_card_only_for_grocery_and_restaurant(world: str) -> None:
    client = _client(world)
    resp = client.get(f"/a2a/{GROCERY}/.well-known/agent-card.json")
    assert resp.status_code == 200
    card = resp.json()
    assert card["name"] == GROCERY
    assert card["provider"]["organization"] == "Mock Grocery US"
    assert card["supportedInterfaces"][0]["url"] == f"http://agents:8003/a2a/{GROCERY}"
    assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
    assert card["skills"][0]["id"] == "grocery_quote"
    resp = client.get(f"/a2a/{RESTAURANT}/.well-known/agent-card.json")
    assert resp.status_code == 200
    assert resp.json()["skills"][0]["id"] == "dinner_quote"
    for agent_id in ("household:us-1", "delivery:mock-us", SENDER, "grocery:off-us", "nope"):
        resp = client.get(f"/a2a/{agent_id}/.well-known/agent-card.json")
        assert resp.status_code == 404, agent_id
