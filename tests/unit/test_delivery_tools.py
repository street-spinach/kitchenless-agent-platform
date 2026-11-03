"""The `/mcp/delivery` tools and the `book_delivery` guard limits (architecture §5, item 43;
issue #48)."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from services.common import db, ids
from services.identity import fga
from services.mcp.delivery import server as delivery
from services.mcp.meta import AdapterFailure, CallContext
from services.mocks import courier
from services.safety.guard import GuardRefused
from tests.unit import identity_world as w

pytestmark = pytest.mark.db

HOUSEHOLD = "us-1"
ZONE = "us-oh-45202"
COURIER = "delivery:mock-us"
OTHER_COURIER = "delivery:other-us"
FAR_COURIER = "delivery:far-us"
DOORDASH = "delivery:doordash-us"
ORG = "org:mock-courier-us"
LINE1 = "1 Main St"
RULE = {"fee_minor": 599, "slot_minutes": 60, "slot_capacity": 2}


@pytest.fixture(autouse=True)
def _auth(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    settings = SimpleNamespace(mock_courier_step_seconds=20, api_url="http://api.invalid")
    monkeypatch.setattr(courier, "get_settings", lambda: settings)
    checks: list[tuple[str, str, str]] = []

    def check(user: str, relation: str, object: str) -> tuple[bool, str]:
        checks.append((user, relation, object))
        return True, "model-1"

    monkeypatch.setattr(fga, "check", check)
    return checks


def _query(pg: str, sql: str, *params: Any) -> list[dict[str, Any]]:
    with psycopg.connect(pg) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _clients(conn: psycopg.Connection) -> None:
    """An `agent_clients` row (`service`) for every business card, so the guard maps the
    callers' service tokens (identity-authz §8 step 1)."""
    for (agent_id,) in conn.execute("SELECT agent_id FROM agent_cards").fetchall():
        conn.execute(
            "INSERT INTO agent_clients (agent_id, client_id, kind) VALUES (%s, %s, 'service')"
            " ON CONFLICT DO NOTHING",
            (agent_id, ids.keycloak_client_id(agent_id)),
        )


def _seed(pg: str) -> tuple[uuid.UUID, uuid.UUID]:
    """A household in ZONE with a `placed` order for COURIER, and the courier cards."""
    address_ref = ids.seed_address_ref(HOUSEHOLD)
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO households (household_id, name, region, zone_id, timezone, currency)"
            " VALUES (%s, 'Test', 'us', %s, 'America/New_York', 'USD')",
            (HOUSEHOLD, ZONE),
        )
        conn.execute(
            "INSERT INTO household_addresses"
            " (address_ref, household_id, line1, city, region, postal_code, country)"
            " VALUES (%s, %s, %s, 'Cincinnati', 'OH', '45202', 'US')",
            (address_ref, HOUSEHOLD, LINE1),
        )
        for agent_id, zones, vendor in (
            (COURIER, [ZONE], "mock"),
            (OTHER_COURIER, [ZONE], "mock"),
            (FAR_COURIER, ["us-xx-00000"], "mock"),
            (DOORDASH, [ZONE], "doordash"),
        ):
            conn.execute(
                "INSERT INTO agent_cards (agent_id, class, owner_ref, region, zone_ids,"
                " currency, host_process, vendor) VALUES (%s, 'delivery', %s, 'us', %s, 'USD',"
                " 'agents', %s)",
                (agent_id, ORG, zones, vendor),
            )
            conn.execute(
                "INSERT INTO business_rules (agent_id, kind, value) VALUES (%s, 'delivery', %s)",
                (agent_id, Jsonb(RULE)),
            )
        _clients(conn)
        run_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO runs (run_id, household_id, kind, trigger, modes)"
            " VALUES (%s, %s, 'weekly', %s, '{}')",
            (run_id, HOUSEHOLD, Jsonb({})),
        )
    return run_id, address_ref


def _placed_order(pg: str, run_id: uuid.UUID, courier_id: str) -> uuid.UUID:
    order_id = uuid.uuid4()
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO orders (order_id, run_id, household_id, kind, state, delivery_agent_id,"
            " total_minor, currency, address_ref)"
            " VALUES (%s, %s, %s, 'grocery', 'placed', %s, 1207, 'USD', %s)",
            (order_id, run_id, HOUSEHOLD, courier_id, ids.seed_address_ref(HOUSEHOLD)),
        )
    return order_id


def _ctx(run_id: uuid.UUID, agent_id: str = COURIER, modes: dict[str, str] | None = None) -> Any:
    return CallContext(
        run_id=run_id,
        agent_id=agent_id,
        conversation_id=None,
        attempt=1,
        token=w.service_token(agent_id),
        env="live",
        modes=modes or {},
        on_behalf_of=ORG,
    )


def _with_pool[T](pg: str, monkeypatch: pytest.MonkeyPatch, fn: Callable[[], Awaitable[T]]) -> T:
    async def go() -> T:
        async with AsyncConnectionPool(pg, min_size=1, max_size=3, open=False) as pool:
            monkeypatch.setattr(db, "get_pool", lambda: pool)
            return await fn()

    return asyncio.run(go())


def _window(hours_from_now: float = 2) -> dict[str, str]:
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=hours_from_now)
    return {"start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat()}


def _book(
    pg: str,
    monkeypatch: pytest.MonkeyPatch,
    ctx: Any,
    order_id: uuid.UUID,
    address_ref: uuid.UUID,
    window: dict[str, str],
) -> dict[str, Any]:
    return _with_pool(
        pg,
        monkeypatch,
        lambda: delivery.book_delivery(
            ctx, order_id=order_id, address_ref=address_ref, window=window
        ),
    )


def _data_shares(pg: str) -> list[dict[str, Any]]:
    return _query(pg, "SELECT payload FROM agent_traces WHERE kind = 'data_share' ORDER BY seq")


def test_book_delivery_limits_then_books_once(
    pg: str, monkeypatch: pytest.MonkeyPatch, _auth: list[tuple[str, str, str]]
) -> None:
    run_id, address_ref = _seed(pg)
    order_id = _placed_order(pg, run_id, COURIER)
    window = _window()

    # Not the order's courier, a courier outside the zone, a past window, another address.
    far_order = _placed_order(pg, run_id, FAR_COURIER)
    for ctx, order, ref, win, reason in (
        (_ctx(run_id, OTHER_COURIER), order_id, address_ref, window, "authz_denied"),
        (_ctx(run_id, FAR_COURIER), far_order, address_ref, window, "out_of_zone"),
        (_ctx(run_id), order_id, address_ref, _window(-3), "outside_window"),
        (_ctx(run_id), order_id, uuid.uuid4(), window, "authz_denied"),
        (_ctx(run_id), uuid.uuid4(), address_ref, window, "authz_denied"),
    ):
        with pytest.raises(GuardRefused) as e:
            _book(pg, monkeypatch, ctx, order, ref, win)
        assert e.value.reason == reason
    assert _query(pg, "SELECT * FROM mock_deliveries") == []
    assert _data_shares(pg) == []
    [order] = _query(pg, "SELECT state FROM orders WHERE order_id = %s", order_id)
    assert order["state"] == "placed"

    result = _book(pg, monkeypatch, _ctx(run_id), order_id, address_ref, window)

    key = ids.idempotency_key(order_id, "book_delivery", COURIER)
    edi = ids.vendor_key(key)
    start = datetime.fromisoformat(window["start"])
    assert result == {
        "order_id": str(order_id),
        "state": "courier_booked",
        "external_delivery_id": edi,
        "window": {"start": window["start"], "end": window["end"]},
    }
    # The address goes to the adapter only: never into the result.
    assert LINE1 not in str(result) and "45202" not in str(result)
    [row] = _query(
        pg,
        "SELECT state, external_delivery_id, lower(delivery_window) AS start FROM orders"
        " WHERE order_id = %s",
        order_id,
    )
    assert row == {"state": "courier_booked", "external_delivery_id": edi, "start": start}
    assert (fga.agent_object(COURIER), "can_deliver_for", f"household:{HOUSEHOLD}") in _auth
    [share] = _data_shares(pg)
    assert share["payload"] == {
        "address_ref": str(address_ref),
        "recipient": COURIER,
        "purpose": "delivery",
    }

    # A replay returns the stored result and books nothing more.
    assert _book(pg, monkeypatch, _ctx(run_id), order_id, address_ref, window) == result
    assert len(_query(pg, "SELECT * FROM mock_deliveries")) == 1

    # The order's courier can read the delivery status; no one else can.
    status = _with_pool(
        pg,
        monkeypatch,
        lambda: delivery.get_delivery_status(_ctx(run_id), order_id=str(order_id)),
    )
    assert status == {
        "order_id": str(order_id),
        "external_delivery_id": edi,
        "state": "booked",
        "window": result["window"],
    }
    with pytest.raises(GuardRefused) as e:
        _with_pool(
            pg,
            monkeypatch,
            lambda: delivery.get_delivery_status(
                _ctx(run_id, OTHER_COURIER), order_id=str(order_id)
            ),
        )
    assert e.value.reason == "authz_denied"


def test_quote_delivery_returns_fee_and_slots(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id, _ = _seed(pg)
    start = datetime(2030, 1, 7, 10, tzinfo=UTC)
    window = {"start": start.isoformat(), "end": (start + timedelta(hours=2)).isoformat()}

    quote = _with_pool(
        pg,
        monkeypatch,
        lambda: delivery.quote_delivery(
            _ctx(run_id), order_id=str(uuid.uuid4()), zone_id=ZONE, window=window
        ),
    )

    assert quote["price_minor"] == 599 and quote["currency"] == "USD"
    assert [w["start"] for w in quote["windows"]] == [
        start.isoformat(),
        (start + timedelta(hours=1)).isoformat(),
    ]


def test_doordash_real_mode_is_not_configured(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id, address_ref = _seed(pg)
    order_id = _placed_order(pg, run_id, DOORDASH)

    with pytest.raises(AdapterFailure) as e:
        _book(
            pg,
            monkeypatch,
            _ctx(run_id, DOORDASH, {"doordash": "real"}),
            order_id,
            address_ref,
            _window(),
        )

    assert (e.value.reason, e.value.retryable) == ("adapter_not_configured", False)
    assert _query(pg, "SELECT * FROM mock_deliveries") == []
    [order] = _query(pg, "SELECT state FROM orders WHERE order_id = %s", order_id)
    assert order["state"] == "placed"

    # In mock mode the DoorDash card books against the mock courier.
    mock_order = _placed_order(pg, run_id, DOORDASH)
    result = _book(
        pg,
        monkeypatch,
        _ctx(run_id, DOORDASH, {"doordash": "mock"}),
        mock_order,
        address_ref,
        _window(),
    )
    assert result["state"] == "courier_booked"
