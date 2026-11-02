"""Shortfall buttons in `services/api/ingest.py` (agent-protocol §10 and P29; issue #70).

`choose_shortfall_option` itself is tested in `test_shortfall_tools.py`; here the MCP call
is faked and these tests check what `ingest` sends and records around it.
"""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from services.api import ingest as ingest_mod
from services.api.ingest import (
    ALREADY_DECIDED,
    API_AGENT,
    Content,
    ShortfallPick,
    ingest,
    parse_shortfall_button,
)
from services.api.whatsapp import sender
from services.common import ids
from services.common.mcp_client import AdapterUnavailable, ToolRefused
from services.common.run_context import RunContext
from tests.unit import api_world as w

db = pytest.mark.db

CHANNEL = "whatsapp"
STORE = "grocery:mock-us"
SID = uuid.UUID("44444444-4444-4444-8444-444444444444")


# --- parsing --------------------------------------------------------------------------------


def test_parse_shortfall_button() -> None:
    assert parse_shortfall_button(f"sf:{SID}:2:SKU-9") == ShortfallPick(SID, 2, "SKU-9")
    assert parse_shortfall_button(f"sf:{SID}:1:drop") == ShortfallPick(SID, 1, "drop")
    # A SKU may itself hold a colon; only the first three are separators.
    assert parse_shortfall_button(f"sf:{SID}:1:A:B") == ShortfallPick(SID, 1, "A:B")


@pytest.mark.parametrize(
    "payload",
    [
        "sf:",
        f"sf:{SID}:1",
        f"sf:{SID}:1:",
        "sf:not-a-uuid:1:drop",
        f"sf:{SID}:0:drop",
        f"sf:{SID}:-1:drop",
        f"sf:{SID}:x:drop",
        f"approve:{SID}:1:drop",
    ],
)
def test_parse_shortfall_button_refuses_malformed(payload: str) -> None:
    assert parse_shortfall_button(payload) is None


def test_already_decided_has_its_text() -> None:
    assert sender.TEXTS[ALREADY_DECIDED] == "Already decided."


# --- ingest ---------------------------------------------------------------------------------


class FakeCall:
    """Records `call_tool` calls and returns `result` or raises `error`."""

    def __init__(self, result: dict[str, Any] | None = None, error: Exception | None = None):
        self.result = result or {"outcome": "chosen"}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, server: str, tool: str, args: Mapping[str, Any], *, ctx: RunContext, token: str
    ) -> dict[str, Any]:
        self.calls.append(
            {"server": server, "tool": tool, "args": dict(args), "ctx": ctx, "token": token}
        )
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    w.patch_settings(monkeypatch)
    monkeypatch.setattr(ingest_mod, "_service_token", lambda: "api-token")


@pytest.fixture
def world(pg: str) -> str:
    w.seed(pg)
    return pg


def _shortfall(pg: str) -> tuple[uuid.UUID, uuid.UUID]:
    """A running run with a `courier_booked` order and a pending shortfall; returns ids."""
    run_id, order_id, address_ref = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    shortfall_id = ids.shortfall_id(order_id)
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
            " VALUES (%s, 'grocery', 'org:mock-store-us', 'us', 'agents')",
            (STORE,),
        )
        conn.execute(
            "INSERT INTO household_addresses"
            " (address_ref, household_id, line1, city, region, postal_code, country)"
            " VALUES (%s, %s, '1 Main St', 'Cincinnati', 'OH', '45202', 'US')",
            (address_ref, w.HOUSEHOLD_ID),
        )
        conn.execute(
            "INSERT INTO runs (run_id, household_id, kind, trigger, modes)"
            " VALUES (%s, %s, 'weekly', %s, '{}')",
            (run_id, w.HOUSEHOLD_ID, Jsonb({"source": "test"})),
        )
        conn.execute(
            "INSERT INTO orders (order_id, run_id, household_id, kind, state, seller_agent_id,"
            " total_minor, currency, address_ref)"
            " VALUES (%s, %s, %s, 'grocery', 'courier_booked', %s, 700, 'USD', %s)",
            (order_id, run_id, w.HOUSEHOLD_ID, STORE, address_ref),
        )
        conn.execute(
            "INSERT INTO shortfalls (shortfall_id, run_id, order_id, seller_agent_id, lines,"
            " due_at) VALUES (%s, %s, %s, %s, '[]', %s)",
            (shortfall_id, run_id, order_id, STORE, datetime(2026, 11, 3, tzinfo=UTC)),
        )
    return run_id, shortfall_id


def _tap(
    pg: str, external_id: str, payload: str, fake: FakeCall, monkeypatch: pytest.MonkeyPatch
) -> Any:
    monkeypatch.setattr(ingest_mod, "call_tool", fake)
    content = Content("button", button=payload)
    return w.run(pg, lambda conn: ingest(conn, CHANNEL, external_id, w.MEMBER_PHONE, content))


def _whatsapp_out(pg: str) -> list[dict[str, Any]]:
    return w.query(pg, "SELECT * FROM agent_messages WHERE type = 'whatsapp_out'")


@db
def test_shortfall_button_calls_choose_shortfall_option(
    world: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, shortfall_id = _shortfall(world)
    fake = FakeCall({"outcome": "chosen", "status": "pending"})

    assert _tap(world, "wamid.sf1", f"sf:{shortfall_id}:1:SKU-2", fake, monkeypatch) is None

    [call] = fake.calls
    assert (call["server"], call["tool"]) == ("household", "choose_shortfall_option")
    assert call["args"] == {
        "shortfall_id": str(shortfall_id),
        "line": 1,
        "pick": "SKU-2",
        "user_id": str(w.MEMBER),
    }
    assert call["ctx"] == RunContext(
        run_id=run_id,
        agent_id=API_AGENT,
        conversation_id=ids.shortfall_conversation_id(shortfall_id),
    )
    assert call["token"] == "api-token"
    # Nothing else is sent and no run starts; the tap is recorded against the shortfall run.
    assert _whatsapp_out(world) == []
    assert len(w.query(world, "SELECT * FROM runs")) == 1
    [inbound] = w.query(world, "SELECT run_id FROM inbound_messages")
    assert inbound["run_id"] == run_id

    # A redelivered tap is deduplicated before the tool is called.
    assert _tap(world, "wamid.sf1", f"sf:{shortfall_id}:1:SKU-2", fake, monkeypatch) is None
    assert len(fake.calls) == 1


@db
def test_already_decided_replies_already_decided(
    world: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, shortfall_id = _shortfall(world)
    fake = FakeCall({"outcome": "already_decided", "status": "closed"})

    _tap(world, "wamid.sf2", f"sf:{shortfall_id}:1:drop", fake, monkeypatch)

    [msg] = _whatsapp_out(world)
    assert msg["run_id"] == run_id
    assert msg["message_id"] == ids.message_id(ids.inbound_id(CHANNEL, "wamid.sf2"), 0)
    assert (msg["from_agent"], msg["to_agent"]) == (API_AGENT, w.WHATSAPP)
    assert msg["body"] == {
        "user_id": str(w.MEMBER),
        "template": ALREADY_DECIDED,
        "params": {},
        "buttons": [],
    }


@db
def test_refused_pick_sends_nothing(world: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _, shortfall_id = _shortfall(world)
    fake = FakeCall(error=ToolRefused("pick_not_in_notice"))
    _tap(world, "wamid.sf3", f"sf:{shortfall_id}:1:SKU-X", fake, monkeypatch)
    assert len(fake.calls) == 1
    assert _whatsapp_out(world) == []


@db
@pytest.mark.parametrize("payload", [f"sf:{SID}:1:drop", "sf:garbage"])
def test_unknown_or_malformed_shortfall_button_calls_nothing(
    world: str, monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    _shortfall(world)
    fake = FakeCall()
    _tap(world, "wamid.sf4", payload, fake, monkeypatch)
    assert fake.calls == []
    assert _whatsapp_out(world) == []


@db
def test_unavailable_tool_rolls_back_so_a_redelivery_retries(
    world: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, shortfall_id = _shortfall(world)
    payload = f"sf:{shortfall_id}:1:drop"
    with pytest.raises(AdapterUnavailable):
        _tap(
            world,
            "wamid.sf5",
            payload,
            FakeCall(error=AdapterUnavailable("transport")),
            monkeypatch,
        )
    assert w.query(world, "SELECT * FROM inbound_messages") == []

    fake = FakeCall()
    _tap(world, "wamid.sf5", payload, fake, monkeypatch)
    assert len(fake.calls) == 1


@db
def test_other_buttons_still_start_nothing(world: str, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCall()
    _tap(world, "wamid.ap", "approve:1", fake, monkeypatch)
    assert fake.calls == []
    assert w.query(world, "SELECT * FROM runs") == []
