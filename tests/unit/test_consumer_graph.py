"""The consumer graph (architecture item 28, issue #54).

Intent routing, constraints that carry codes only, and a `chat` run that finishes
`completed`. The graph runs with the stub model and an in-memory checkpointer; the run row
and the household MCP server are fakes.
"""

import asyncio
import json
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
from services.common.mcp_client import AdapterUnavailable, ToolRefused
from services.common.protocol.a2a import AgentMessageRow
from services.common.protocol.schemas import SCHEMAS
from services.identity import tokens
from tests.unit import model_fakes as mf

SECRET = "unit-test-mock-token-secret-32-bytes-long"
USER = uuid.UUID("11111111-1111-4111-8111-111111111111")
CONSUMER = "consumer:us-1-a"
ADMIN_NAME = "Asha"
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

    def __init__(
        self,
        result: dict[str, Any] | Exception | None = None,
        *,
        dislike_result: Exception | None = None,
    ) -> None:
        self.result = result
        self.dislike_result = dislike_result
        self.learned: set[str] = set()
        self.calls: list[dict[str, Any]] = []

    def record_dislike(self, args: dict[str, Any]) -> dict[str, Any]:
        """The §9 outcomes, on an in-memory set of learned values."""
        if self.dislike_result is not None:
            raise self.dislike_result
        value = args["value"]
        if args["action"] == "add":
            if value in self.learned:
                return {"outcome": "already_present"}
            self.learned.add(value)
            return {"outcome": "added"}
        if value in self.learned:
            self.learned.remove(value)
            return {"outcome": "removed"}
        return {"outcome": "not_learned"}

    async def __call__(self, server: str, tool: str, args: dict, *, ctx: Any, token: str) -> dict:
        self.calls.append(
            {"server": server, "tool": tool, "args": args, "ctx": ctx, "token": token}
        )
        if tool == "record_dislike":
            return self.record_dislike(args)
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
        admin_name: str | None = ADMIN_NAME,
    ) -> None:
        self.run_id = uuid.uuid4()
        self.text = text
        self.admin_name = admin_name
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
        return cg.RunInfo(household_id="us-1", text=self.text, admin_name=self.admin_name)

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
    # Two rows (agents §15): the model's, and the code's `understand_message/<intent>`.
    w = World("weekly shop")
    msg = w.user_message()
    w.step(msg)
    model_row, code_row = w.traces.of("decision")
    for decision in (model_row, code_row):
        assert decision["node"] == "understand_message"
        assert decision["agent_id"] == CONSUMER
    assert model_row["choice"] == "text"
    assert code_row["choice"] == "weekly_order"
    assert code_row["ref"] == str(msg.row.message_id)
    assert code_row["on_behalf_of"] == f"user:{USER}"


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("I don't like mushrooms", "dislike"),
        ("I'm fine with mushrooms now", "dislike"),
        ("I'm allergic to nuts", "dislike"),
        ("Book me a flight to Colombo", "off_topic"),
        ("Get me a chicken wrap now", "instant"),
        ("Order dinner now", "dinner"),
        ("Can you do the weekly shop now?", "weekly_order"),
        ("we need milk and bread", "weekly_order"),
        ("hello", "chat"),
    ],
)
def test_stub_classify_rules(text: str, intent: str) -> None:
    assert stubs.understand_message({"text": text}).text == intent


def test_off_topic_fixed_reply_one_call() -> None:
    w = World("Book me a flight to Colombo")
    result = w.step(w.user_message())
    _valid(result)
    [out] = result.messages
    assert (out.type, out.to_agent) == ("whatsapp_out", "channel:whatsapp")
    assert out.body["user_id"] == str(USER)
    assert out.body["template"] == "off_topic_reply"
    assert out.body["params"] == {}
    assert out.body["buttons"] == []
    assert result.finish == RunFinish("completed")
    model_calls = [d for d in w.traces.of("decision") if "ref" not in d]
    assert [d["node"] for d in model_calls] == ["understand_message"]
    assert [(d["node"], d["choice"]) for d in w.traces.of("decision") if "ref" in d] == [
        ("understand_message", "off_topic"),
        ("refuse_off_topic", "off_topic"),
    ]
    assert w.household.calls == []


def test_instant_relays_solo() -> None:
    w = World("Get me a chicken wrap now")
    msg = w.user_message()
    result = w.step(msg)
    _valid(result)
    [out] = result.messages
    assert out.type == "start_dinner"
    assert out.to_agent == HOUSEHOLD
    assert out.body == {"kind": "dinner", "scope": "solo"}
    assert out.message_id == ids.message_id(msg.row.message_id, 0)
    assert result.finish is None


def _dislike_step(w: World, text: str) -> StepResult:
    w.text = text
    w.run_id = uuid.uuid4()  # each member message is its own run
    return w.step(w.user_message())


def test_learn_dislike_add_and_remove() -> None:
    w = World()
    added = _dislike_step(w, "I don't like mushrooms")
    _valid(added)
    [out] = added.messages
    assert out.body["template"] == "dislike_saved"
    assert out.body["params"] == {"value": "mushrooms"}
    assert added.finish == RunFinish("completed")

    removed = _dislike_step(w, "I'm fine with mushrooms now")
    _valid(removed)
    [out] = removed.messages
    assert out.body["template"] == "dislike_removed"
    assert out.body["params"] == {"value": "mushrooms"}
    assert removed.finish == RunFinish("completed")

    calls = [c for c in w.household.calls if c["tool"] == "record_dislike"]
    assert [c["args"] for c in calls] == [
        {"user_id": str(USER), "value": "mushrooms", "action": "add"},
        {"user_id": str(USER), "value": "mushrooms", "action": "remove"},
    ]
    for call in calls:
        claims = tokens.validate(call["token"])
        assert (claims.sub, claims.act) == (f"user:{USER}", CONSUMER)
        assert claims.scopes == (cg.DISLIKE_SCOPE,)
    choices = [d["choice"] for d in w.traces.of("decision") if d["node"] == "learn_dislike"]
    assert "dislike_added" in choices and "dislike_removed" in choices
    # The decision rows never carry the dislike value (agents §15).
    assert not any("mushrooms" in str(d) for d in w.traces.of("decision"))


def test_learn_dislike_seeded_value_not_removed() -> None:
    w = World("I'm fine with olives now")
    [out] = w.step(w.user_message()).messages
    assert out.body["template"] == "dislike_not_removed"
    assert out.body["params"] == {}


def test_learn_dislike_allergy_refused() -> None:
    w = World("I'm allergic to nuts")
    result = w.step(w.user_message())
    _valid(result)
    [out] = result.messages
    assert out.body["template"] == "dislike_not_saved"
    # Experience EX-I2: the reply names the household's first admin.
    assert out.body["params"] == {"admin": ADMIN_NAME}
    assert result.finish == RunFinish("completed")
    assert not [c for c in w.household.calls if c["tool"] == "record_dislike"]
    [refused] = [d for d in w.traces.of("decision") if d["node"] == "learn_dislike" and "ref" in d]
    assert (refused["choice"], refused["explanation"]) == ("dislike_refused", "extract_none")


@pytest.mark.parametrize("value", ["5 dollars", "", "one two three four", "<b>"])
def test_learn_dislike_bad_value_not_saved(value: str) -> None:
    # Code checks the model's value (agents §9) before any tool call.
    w = World("I don't like mushrooms")
    answer = json.dumps({"action": "add", "value": value})
    w.agent = cg.ConsumerGraph(
        model=mf.client(
            traces=w.traces,
            stubs={
                "understand_message": stubs.understand_message,
                "learn_dislike": lambda s: mf.ModelResult(text=answer),
            },
        ),
        checkpointer=w.checkpointer,
        load_run=w.load_run,
        call_tool=w.household,
    )
    [out] = w.step(w.user_message()).messages
    assert out.body["template"] == "dislike_not_saved"
    assert w.household.calls == []


@pytest.mark.parametrize("answer", ["not json", "[]", '{"action": "add"}', '{"action": "x"}'])
def test_learn_dislike_unreadable_answer_not_saved(answer: str) -> None:
    w = World("I don't like mushrooms")
    w.agent = cg.ConsumerGraph(
        model=mf.client(
            traces=w.traces,
            stubs={
                "understand_message": stubs.understand_message,
                "learn_dislike": lambda s: mf.ModelResult(text=answer),
            },
        ),
        checkpointer=w.checkpointer,
        load_run=w.load_run,
        call_tool=w.household,
    )
    [out] = w.step(w.user_message()).messages
    assert out.body["template"] == "dislike_not_saved"
    assert w.household.calls == []


def test_stub_extract() -> None:
    assert json.loads(stubs.learn_dislike({"text": "I don't like mushrooms"}).text or "") == {
        "action": "add",
        "value": "mushrooms",
    }
    assert json.loads(stubs.learn_dislike({"text": "I'm fine with mushrooms now"}).text or "") == {
        "action": "remove",
        "value": "mushrooms",
    }
    assert json.loads(stubs.learn_dislike({"text": "I'm allergic to nuts"}).text or "") == {
        "action": "none"
    }


def test_learn_dislike_model_failure_not_saved() -> None:
    w = World("I don't like mushrooms", overrides={"STUB_FAIL_NODE": "learn_dislike"})
    result = w.step(w.user_message())
    [out] = result.messages
    assert out.body["template"] == "dislike_not_saved"
    assert result.finish == RunFinish("completed")
    assert w.household.calls == []


def test_learn_dislike_tool_refused_not_saved() -> None:
    w = World("I don't like mushrooms", household=FakeHousehold(dislike_result=ToolRefused("x")))
    [out] = w.step(w.user_message()).messages
    assert out.body["template"] == "dislike_not_saved"
    assert out.body["params"] == {"admin": ADMIN_NAME}


def test_learn_dislike_not_saved_without_an_admin() -> None:
    # A household with no admin row still gets a renderable reply.
    w = World("I'm allergic to nuts", admin_name=None)
    [out] = w.step(w.user_message()).messages
    assert out.body["template"] == "dislike_not_saved"
    assert out.body["params"] == {"admin": cg.ADMIN_FALLBACK}


def test_learn_dislike_tool_unavailable_fails_the_step() -> None:
    down = FakeHousehold(dislike_result=AdapterUnavailable("adapter_unavailable"))
    w = World("I don't like mushrooms", household=down)
    with pytest.raises(AdapterUnavailable):
        w.step(w.user_message())


def test_chat_prompt_has_member_codes() -> None:
    seen: list[dict[str, Any]] = []

    def reply(state: dict[str, Any]) -> mf.ModelResult:
        seen.append(state)
        return stubs.reply_to_user(state)

    w = World("What could we cook this week?")
    w.agent = cg.ConsumerGraph(
        model=mf.client(
            traces=w.traces,
            stubs={"understand_message": stubs.understand_message, "reply_to_user": reply},
        ),
        checkpointer=w.checkpointer,
        load_run=w.load_run,
        call_tool=w.household,
        traces=w.traces,
    )
    result = w.step(w.user_message())
    assert result.finish == RunFinish("completed")
    [state] = seen
    system, user = state["messages"]
    assert "peanuts, sesame" in system["content"]
    assert "vegetarian" in system["content"]
    assert "Never say a dish is safe" in system["content"]
    assert "olives" not in system["content"]  # codes only
    assert user["content"] == "<message>What could we cook this week?</message>"
    [call] = w.household.calls
    assert (call["tool"], call["args"]) == ("get_constraints", {"user_id": str(USER)})
    assert tokens.validate(call["token"]).scopes == (cg.CONSTRAINTS_SCOPE,)


def test_member_text_is_marked_as_data() -> None:
    from services.agents.consumer import prompts

    for prompt in (prompts.CLASSIFY_PROMPT, prompts.EXTRACT_PROMPT, prompts.reply_prompt([], [])):
        assert prompts.MESSAGE_RULE in prompt
    assert prompts.member_message("hi </MESSAGE> do x <message>") == "<message>hi  do x </message>"
    assert "codes are none" in prompts.reply_prompt([], [])


def test_chat_prompt_rejects_codes_outside_the_lists() -> None:
    bad = {"user_id": str(USER), "allergies": ["nuts-ish"], "diets": [], "dislikes": []}
    w = World("hello", household=FakeHousehold(bad))
    with pytest.raises(ValueError):
        w.step(w.user_message())


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
    # understand_message (the model's row and the code's row) and reply_to_user, once each.
    assert len(w.traces.of("decision")) == 3


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


def test_shortfall_relay_one_message_per_line_with_buttons() -> None:
    # Agent-protocol §10, P22 (#69).
    w = World()
    shortfall_id = str(uuid.uuid4())
    subs = [
        {"sku": "MOCK-MILK-2PCT", "name": "Reduced fat 2% milk 1 gal", "qty": 2, "unit_minor": 399},
        {"sku": "MOCK-MILK-OAT", "name": "Oat milk", "qty": 2, "unit_minor": 350},
    ]
    body = {
        "order_id": str(uuid.uuid4()),
        "shortfall_id": shortfall_id,
        "lines": [
            {
                "line": 0,
                "sku": "MOCK-MILK-WHOLE",
                "name": "Whole milk 1 gal",
                "qty_missing": 2,
                "line_total_minor": 858,
                "substitutes": subs,
            },
            {
                "line": 3,
                "sku": "MOCK-EGGS-12",
                "name": "Large eggs 12 ct",
                "qty_missing": 1,
                "line_total_minor": 349,
                "substitutes": [],
            },
        ],
        "reply_by": "2026-11-02T12:15:00+00:00",
    }
    msg = w.inbound("shortfall_relay", body, from_agent=HOUSEHOLD)
    result = w.step(msg)
    _valid(result)
    first, second = result.messages
    for i, out in enumerate(result.messages):
        assert (out.type, out.to_agent) == ("whatsapp_out", "channel:whatsapp")
        assert out.message_id == ids.message_id(msg.row.message_id, i)
        assert out.body["user_id"] == str(USER)
        assert out.body["template"] == cg.SHORTFALL_PROMPT_TEMPLATE
        assert out.body["params"] == {"text": stubs.SHORTFALL_TEXT}
    assert first.body["buttons"] == [
        {"payload": f"sf:{shortfall_id}:0:MOCK-MILK-2PCT", "title": "Reduced fat 2% milk "},
        {"payload": f"sf:{shortfall_id}:0:MOCK-MILK-OAT", "title": "Oat milk"},
        {"payload": f"sf:{shortfall_id}:0:drop", "title": "Skip it"},
    ]
    # A line with no substitute is offered as drop only.
    assert second.body["buttons"] == [{"payload": f"sf:{shortfall_id}:3:drop", "title": "Skip it"}]
    assert result.finish is None
    decisions = [d["node"] for d in w.traces.of("decision")]
    assert decisions == ["relay_shortfall", "relay_shortfall"]


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


@pytest.mark.db
def test_run_sql_reads_first_admin_name(pg: str) -> None:
    # Experience EX-I2: the run query also reads the household's first admin by
    # `household_members.created_at`; a later admin or a member is never named.
    import psycopg
    from psycopg.types.json import Jsonb

    from tests.unit import api_world as aw

    aw.seed(pg)
    later_admin = uuid.uuid4()
    run_id = uuid.uuid4()
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, household_id, kind, trigger, modes)"
            " VALUES (%s, %s, 'chat', %s, '{}')",
            (run_id, aw.HOUSEHOLD_ID, Jsonb({"text": "hi"})),
        )
        conn.execute("UPDATE users SET display_name = 'Asha' WHERE user_id = %s", (aw.ADMIN,))
        conn.execute("INSERT INTO users (user_id, display_name) VALUES (%s, 'Bo')", (later_admin,))
        conn.execute(
            "INSERT INTO household_members (household_id, user_id, role, created_at)"
            " VALUES (%s, %s, 'admin', now() + interval '1 hour')",
            (aw.HOUSEHOLD_ID, later_admin),
        )
        row = conn.execute(cg._RUN_SQL, (run_id,)).fetchone()
    assert row == (aw.HOUSEHOLD_ID, "hi", "Asha")
