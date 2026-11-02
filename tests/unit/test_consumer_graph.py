"""The consumer graph (architecture item 28, issue #54).

Intent routing, constraints that carry codes only, and a `chat` run that finishes
`completed`. The graph runs with the stub model and an in-memory checkpointer; the run row
and the household MCP server are fakes.
"""

import asyncio
import uuid
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from services.agents import host
from services.agents.consumer import graph as cg
from services.agents.consumer import stubs
from services.common import ids
from services.common.consumer import Inbound, RunFinish, StepResult
from services.common.directory import AgentCard
from services.common.mcp_client import ToolRefused
from services.common.protocol.a2a import AgentMessageRow
from services.common.protocol.schemas import SCHEMAS
from services.identity import tokens
from tests.unit import model_fakes as mf

SECRET = "unit-test-mock-token-secret-32-bytes-long"
USER = uuid.UUID("11111111-1111-4111-8111-111111111111")
CONSUMER = "consumer:us-1-a"
HOUSEHOLD = "household:us-1"
CARD = AgentCard(
    agent_id=CONSUMER,
    agent_class="consumer",
    owner_ref=f"user:{USER}",
    region="us",
    zone_ids=("us-oh-45202",),
    currency="USD",
    host_process="agents",
    vendor=None,
)


class FakeHousehold:
    """`call_tool` for `/mcp/household`, recording each call."""

    def __init__(self, result: dict[str, Any] | Exception | None = None) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, server: str, tool: str, args: dict, *, ctx: Any, token: str) -> dict:
        self.calls.append(
            {"server": server, "tool": tool, "args": args, "ctx": ctx, "token": token}
        )
        if isinstance(self.result, Exception):
            raise self.result
        if self.result is not None:
            return self.result
        return {
            "user_id": args["user_id"],
            "allergies": ["peanuts", "sesame"],
            "diets": ["vegetarian"],
            "dislikes": ["olives"],
        }


class World:
    def __init__(
        self,
        text: str = "",
        *,
        household: FakeHousehold | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> None:
        self.run_id = uuid.uuid4()
        self.text = text
        self.traces = mf.FakeTraces()
        self.household = household or FakeHousehold()
        self.checkpointer = InMemorySaver()
        self.runs_loaded = 0
        self.agent = cg.ConsumerGraph(
            model=mf.client(
                traces=self.traces, run_state=mf.FakeRunState(overrides), stubs=stubs.STUBS
            ),
            checkpointer=self.checkpointer,
            load_run=self.load_run,
            call_tool=self.household,
            issue_token=lambda sub, act, scopes: tokens.MockIssuer(secret=SECRET).issue(
                sub, act, list(scopes)
            ),
        )

    async def load_run(self, run_id: uuid.UUID) -> cg.RunInfo:
        assert run_id == self.run_id
        self.runs_loaded += 1
        return cg.RunInfo(household_id="us-1", text=self.text)

    def inbound(self, type: str, body: dict[str, Any], *, from_agent: str) -> Inbound:
        raw = {"schema_version": 1, **body}
        row = AgentMessageRow(
            message_id=uuid.uuid4(),
            run_id=self.run_id,
            conversation_id=None,
            causation_id=None,
            from_agent=from_agent,
            to_agent=CONSUMER,
            type=type,
            body=raw,
        )
        return Inbound(row=row, attempts=1, body=SCHEMAS[type].model_validate(raw))

    def user_message(self) -> Inbound:
        return self.inbound(
            "user_message", {"inbound_id": str(uuid.uuid4())}, from_agent="channel:whatsapp"
        )

    def constraints_request(self, user_id: uuid.UUID = USER) -> Inbound:
        return self.inbound(
            "constraints_request",
            {"order_id": str(uuid.uuid4()), "user_id": str(user_id)},
            from_agent=HOUSEHOLD,
        )

    def step(self, msg: Inbound) -> StepResult:
        return asyncio.run(self.agent(msg, CARD))


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tokens, "_secret", lambda: SECRET)


def _valid(result: StepResult) -> None:
    for m in result.messages:
        SCHEMAS[m.type].model_validate(m.body)
        assert m.from_agent == CONSUMER


# Intent routing ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "type", "kind"),
    [
        ("Can you do the weekly shop?", "start_weekly_order", "grocery"),
        ("we need milk and bread", "start_weekly_order", "grocery"),
        ("Order dinner for tonight please", "start_dinner", "dinner"),
    ],
)
def test_order_intent_relayed_to_household(text: str, type: str, kind: str) -> None:
    w = World(text)
    msg = w.user_message()
    result = w.step(msg)
    _valid(result)
    [out] = result.messages
    assert out.type == type
    assert out.to_agent == HOUSEHOLD
    assert out.body == {"kind": kind}
    assert out.message_id == ids.message_id(msg.row.message_id, 0)
    assert out.causation_id == msg.row.message_id
    assert result.finish is None  # the household graph finishes order runs


def test_chat_intent_replies_to_member() -> None:
    w = World("hi there, how are you?")
    result = w.step(w.user_message())
    _valid(result)
    [out] = result.messages
    assert out.type == "whatsapp_out"
    assert out.to_agent == "channel:whatsapp"
    assert out.body["user_id"] == str(USER)
    assert out.body["template"] == cg.CHAT_REPLY_TEMPLATE
    assert out.body["params"] == {"text": stubs.CHAT_REPLY}
    assert out.body["buttons"] == []


def test_answer_outside_the_intents_is_chat() -> None:
    w = World("weekly shop")
    w.agent = cg.ConsumerGraph(
        model=mf.client(
            traces=w.traces,
            stubs={
                "understand_message": lambda s: mf.ModelResult(text="buy everything"),
                "reply_to_user": stubs.reply_to_user,
            },
        ),
        checkpointer=w.checkpointer,
        load_run=w.load_run,
        call_tool=w.household,
    )
    result = w.step(w.user_message())
    assert [m.type for m in result.messages] == ["whatsapp_out"]
    assert result.finish == RunFinish("completed")


def test_understand_message_traces_a_decision() -> None:
    w = World("weekly shop")
    w.step(w.user_message())
    [decision] = w.traces.of("decision")
    assert decision["node"] == "understand_message"
    assert decision["agent_id"] == CONSUMER


def test_unhandled_message_type_fails_the_step() -> None:
    w = World()
    msg = w.inbound(
        "approval_decision",
        {"approval_id": str(uuid.uuid4()), "order_id": str(uuid.uuid4()), "status": "approved"},
        from_agent="system:api",
    )
    with pytest.raises(cg.UnsupportedMessage):
        w.step(msg)


# Constraints carry codes only ---------------------------------------------------------


def test_constraints_carry_codes_only() -> None:
    w = World()
    msg = w.constraints_request()
    result = w.step(msg)
    _valid(result)
    [out] = result.messages
    assert out.type == "constraints"
    assert out.to_agent == HOUSEHOLD
    assert out.body == {
        "schema_version": 1,
        "order_id": msg.row.body["order_id"],
        "allergens": ["peanuts", "sesame"],
        "diets": ["vegetarian"],
        "dislikes": ["olives"],
    }
    assert "user_id" not in out.body


def test_constraints_drop_extra_profile_fields() -> None:
    profile = {
        "user_id": str(USER),
        "allergies": ["milk"],
        "diets": [],
        "dislikes": [],
        "name": "Asha",
        "phone_e164": "+15135550100",
    }
    w = World(household=FakeHousehold(profile))
    [out] = w.step(w.constraints_request()).messages
    assert set(out.body) == {"schema_version", "order_id", "allergens", "diets", "dislikes"}
    assert out.body["allergens"] == ["milk"]


def test_constraints_call_household_for_the_member() -> None:
    w = World()
    w.step(w.constraints_request())
    [call] = w.household.calls
    assert (call["server"], call["tool"], call["args"]) == (
        "household",
        "get_constraints",
        {"user_id": str(USER)},
    )
    assert call["ctx"].agent_id == CONSUMER
    assert call["ctx"].run_id == w.run_id
    claims = tokens.validate(call["token"])
    assert (claims.sub, claims.act) == (f"user:{USER}", CONSUMER)
    assert claims.scopes == (cg.CONSTRAINTS_SCOPE,)


def test_constraint_value_outside_the_codes_fails_closed() -> None:
    bad = {"user_id": str(USER), "allergies": ["nuts-ish"], "diets": [], "dislikes": []}
    w = World(household=FakeHousehold(bad))
    with pytest.raises(ValueError):
        w.step(w.constraints_request())


def test_constraints_request_for_another_person_is_refused() -> None:
    w = World()
    with pytest.raises(cg.ConstraintsMismatch):
        w.step(w.constraints_request(uuid.uuid4()))
    assert w.household.calls == []


def test_tool_refusal_fails_the_step() -> None:
    w = World(household=FakeHousehold(ToolRefused("authz_denied")))
    with pytest.raises(ToolRefused):
        w.step(w.constraints_request())


# The chat run finishes completed ------------------------------------------------------


def test_chat_run_finishes_completed() -> None:
    w = World("hello")
    result = w.step(w.user_message())
    assert result.finish == RunFinish("completed")


def test_replayed_message_returns_the_same_result_without_rerunning() -> None:
    w = World("hello")
    msg = w.user_message()
    first = w.step(msg)
    second = w.step(msg)
    assert second == first
    assert w.runs_loaded == 1
    assert len(w.traces.of("decision")) == 2  # understand_message and reply_to_user, once


def test_later_message_on_the_same_thread_runs_again() -> None:
    w = World("weekly shop")
    first = w.step(w.user_message())
    second = w.step(w.constraints_request())
    assert [m.type for m in first.messages] == ["start_weekly_order"]
    assert [m.type for m in second.messages] == ["constraints"]
    assert second.finish is None


def test_unknown_state_version_ends_the_run() -> None:
    w = World("hello")
    msg = w.user_message()
    w.step(msg)
    config = {"configurable": {"thread_id": ids.thread_id(CONSUMER, w.run_id)}}
    asyncio.run(w.agent.graph.aupdate_state(config, {"state_version": 99}))
    result = w.step(w.user_message())
    assert result == StepResult(finish=RunFinish("error", "state_version"))


def test_host_registers_the_consumer_handler() -> None:
    assert host.HANDLERS["consumer"] is cg.handle


@pytest.mark.db
def test_postgres_checkpointer_on_the_shared_pool(pg: str) -> None:
    """The host's wiring: `AsyncPostgresSaver` on a plain (non-autocommit) pool."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg_pool import AsyncConnectionPool

    from migrations import checkpointer

    checkpointer.main(pg)
    w = World("hello")

    async def go() -> tuple[StepResult, StepResult]:
        pool = AsyncConnectionPool(pg, min_size=1, max_size=2, open=False)
        await pool.open()
        try:
            w.agent = cg.ConsumerGraph(
                model=mf.client(traces=w.traces, stubs=stubs.STUBS),
                checkpointer=AsyncPostgresSaver(pool),  # type: ignore[arg-type]
                load_run=w.load_run,
                call_tool=w.household,
            )
            msg = w.user_message()
            return await w.agent(msg, CARD), await w.agent(msg, CARD)
        finally:
            await pool.close()

    first, replay = asyncio.run(go())
    assert first.finish == RunFinish("completed")
    assert replay == first
    assert w.runs_loaded == 1
