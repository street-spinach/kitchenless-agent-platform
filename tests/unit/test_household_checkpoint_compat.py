"""A run paused at `awaiting_approval` resumes on a freshly built graph (agents §16, AG13).

Written against today's household graph (agents build step 3, issue #198), before any
graph change. It keeps only the state keys today's graph checkpoints, as a literal list,
loads them into a new graph on a new checkpointer, and resumes with the approval. New
state fields added later must have defaults, so this test keeps passing unchanged.
"""

import asyncio
import uuid
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from services.agents.household import graph as hg
from services.common import ids
from services.common.consumer import RunFinish
from services.identity import tokens
from tests.unit.test_household_graph import (
    HOUSEHOLD,
    SECRET,
    FakeHousehold,
    World,
    _awaiting_approval,
)

# Every key of `HouseholdState` on main at the time of issue #198. Do not add keys here
# when the graph gains fields: the point is that today's checkpoint still resumes.
TODAY_KEYS = (
    "state_version",
    "run_id",
    "household_id",
    "order_id",
    "kind",
    "quotes",
    "chosen_quote_id",
    "approval_id",
    "agent_id",
    "user_id",
    "region",
    "zone_id",
    "currency",
    "phase",
    "members",
    "constraints",
    "items",
    "conversations",
    "counters",
    "awaiting_counter",
    "remaining_minor",
    "excluded_sellers",
    "requotes",
    "approval_minor",
    "approved_minor",
    "approver_user_id",
    "accepts",
    "accept_delay_rounds",
    "offer_ttl_seconds",
    "shortfall",
    "message_id",
    "message_type",
    "from_agent",
    "conversation_id",
    "body",
    "decision",
    "outgoing",
    "timers",
    "finish",
    "sent",
)


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tokens, "_secret", lambda: SECRET)


def test_paused_approval_resumes_on_a_fresh_graph() -> None:
    w = World(remaining_minor=2000)
    _awaiting_approval(w)
    config = {"configurable": {"thread_id": ids.thread_id(HOUSEHOLD, w.run_id)}}
    saved = asyncio.run(w.agent.graph.aget_state(config)).values
    assert saved["phase"] == hg.AWAITING_APPROVAL
    old: dict[str, Any] = {k: saved[k] for k in TODAY_KEYS if k in saved}
    assert old["state_version"] == hg.STATE_VERSION == 1

    # A new graph, on a new checkpointer and a fresh MCP fake, holding only today's keys.
    # `as_node="send"` stores it as a finished invocation, as the paused run is.
    w.checkpointer = InMemorySaver()
    w.household = FakeHousehold(2000)
    w.agent = w.build()
    asyncio.run(w.agent.graph.aupdate_state(config, old, as_node="send"))
    assert asyncio.run(w.agent.graph.aget_state(config)).next == ()

    approved = w.approve()

    assert len(w.household.of("authorize_spend")) == 1
    [accept] = approved.messages
    assert (accept.type, accept.to_agent) == ("accept", "grocery:square-us")
    assert accept.body["total_minor"] == 2250
    assert accept.body["quote_id"] == old["chosen_quote_id"]
    assert approved.finish is None
    assert w.saved()["phase"] == hg.PLACING
    assert w.saved()["approval_id"] == str(ids.approval_id(uuid.UUID(w.order_id)))


def test_checkpoint_without_pantry_fields_resumes_with_defaults() -> None:
    # Pantry §5 (P5): a run checkpointed before `pantry_skipped` and `pantry_as_of` existed
    # resumes, and both read as their defaults (no skip line in the summary).
    w = World()
    w.accepted()
    config = {"configurable": {"thread_id": ids.thread_id(HOUSEHOLD, w.run_id)}}
    saved = asyncio.run(w.agent.graph.aget_state(config)).values
    assert saved["phase"] == hg.PLACING
    assert saved["pantry_skipped"] == ["rice"]
    old: dict[str, Any] = {
        k: v for k, v in saved.items() if k not in ("pantry_skipped", "pantry_as_of")
    }

    w.checkpointer = InMemorySaver()
    w.household = FakeHousehold()
    w.agent = w.build()
    asyncio.run(w.agent.graph.aupdate_state(config, old, as_node="send"))
    assert "pantry_skipped" not in asyncio.run(w.agent.graph.aget_state(config)).values

    w.order_update("order_confirm", "placed", "grocery:square-us")
    done = w.order_update("order_status", "delivered", "system:api")
    assert done.finish == RunFinish("completed")
    [summary] = done.messages
    assert "Skipped" not in summary.body["params"]["text"]
    assert w.saved()["state_version"] == hg.STATE_VERSION == 1
