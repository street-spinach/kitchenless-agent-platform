"""The consumer graph (architecture item 28, §4 "Agent runtime", §8).

A member's consumer agent is a LangGraph graph checkpointed under
`thread_id = <agent_id>:<run_id>`. Each inbound message is one invocation of the thread:

*   `user_message` → `understand_message` classifies the member's text (`runs.trigger.text`)
    as a weekly order, a dinner or chat. Then:
    *   weekly order or dinner → `relay_to_household` sends `start_weekly_order` or
        `start_dinner` to `household:<runs.household_id>`;
    *   chat → `reply_to_user` sends `whatsapp_out` to the member and finishes the run
        `completed`.
*   `constraints_request` → `answer_constraints_request` calls `/mcp/household
    get_constraints` for the member and returns `constraints`: allergen codes, diet codes and
    dislikes only (an intent-only summary, never the profile row).
*   `shortfall_relay` → `relay_shortfall` (agent-protocol §10, P22): one `whatsapp_out` to
    the member per missing line. The model writes the text; code builds the buttons: up to
    2 substitutes and "Skip it", payloads `sf:<shortfall_id>:<line>:<sku>` and
    `sf:<shortfall_id>:<line>:drop`.

Nodes never write to Postgres. They put the step's outgoing messages (ids from
`ids.message_id(<inbound message_id>, <ordinal>)`) and the run outcome in graph state, and
`ConsumerGraph` returns them as the `StepResult` the consumer commits in one transaction.
A replayed message whose invocation already finished returns the same result from the
checkpoint without running the graph again. A thread with an unknown `state_version` ends
its run with `status=error, reason=state_version`.

A tool refusal or outage (`ToolRefused`, `AdapterUnavailable`) and a model failure raise out
of the step: an ordinary step failure, retried and then poisoned (§4, §9).
"""

import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypedDict
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from services.agents.common.model import ModelClient
from services.common import ids, mcp_client
from services.common.consumer import Inbound, Outgoing, RunFinish, StepResult
from services.common.directory import AgentCard
from services.common.protocol.schemas import Constraints
from services.common.run_context import RunContext
from services.identity.tokens import MockIssuer

__all__ = [
    "CHAT_REPLY_TEMPLATE",
    "CONSTRAINTS_SCOPE",
    "INTENTS",
    "STATE_VERSION",
    "ConsumerGraph",
    "ConsumerState",
    "ConstraintsMismatch",
    "RunInfo",
    "UnsupportedMessage",
    "build_graph",
    "handle",
]

log = logging.getLogger(__name__)

STATE_VERSION = 1
INTENTS = ("weekly_order", "dinner", "chat")
CHAT = "chat"
# The household message and `kind` for each order intent (agent-protocol §5 rows 2, 3).
RELAY: Mapping[str, tuple[str, str]] = {
    "weekly_order": ("start_weekly_order", "grocery"),
    "dinner": ("start_dinner", "dinner"),
}
CHAT_REPLY_TEMPLATE = "chat_reply"
CHAT_REPLY_MAX_CHARS = 1000
WHATSAPP = "channel:whatsapp"
HOUSEHOLD_SERVER = "household"
CONSTRAINTS_SCOPE = "constraints:read"
HANDLED_TYPES = frozenset({"user_message", "constraints_request", "shortfall_relay"})
# The shortfall question (agent-protocol §10). WhatsApp allows 3 reply buttons of at most
# 20 characters each: 2 substitutes and "Skip it".
SHORTFALL_PROMPT_TEMPLATE = "shortfall_prompt"
SHORTFALL_PREFIX = "sf:"
MAX_SHORTFALL_SUBSTITUTES = 2
SKIP_TITLE = "Skip it"
BUTTON_TITLE_MAX_CHARS = 20

CLASSIFY_PROMPT = (
    "Classify the member's message as exactly one of: weekly_order, dinner, chat. "
    "Answer with that one word."
)
REPLY_PROMPT = "Reply briefly and kindly to the member's message."
SHORTFALL_PROMPT = (
    "The store is out of one item of the member's order. In one short, friendly message, "
    "say what is missing and ask the member to pick one of the substitutes or skip it. "
    "Plain text only."
)


def shortfall_buttons(shortfall_id: str, line: Mapping[str, Any]) -> list[dict[str, str]]:
    """Up to 2 substitute buttons and "Skip it" for one notice line (agent-protocol §10)."""
    prefix = f"{SHORTFALL_PREFIX}{shortfall_id}:{int(line['line'])}:"
    buttons = [
        {"payload": prefix + str(s["sku"]), "title": str(s["name"])[:BUTTON_TITLE_MAX_CHARS]}
        for s in list(line.get("substitutes") or [])[:MAX_SHORTFALL_SUBSTITUTES]
    ]
    return [*buttons, {"payload": prefix + "drop", "title": SKIP_TITLE}]


class UnsupportedMessage(ValueError):
    """The consumer graph has no path for this message type."""


class ConstraintsMismatch(ValueError):
    """A `constraints_request` or `get_constraints` result names a different person."""


class ConsumerState(TypedDict, total=False):
    # Required by architecture item 28.
    state_version: int
    run_id: str
    agent_id: str
    member_user_id: str
    # The inbound message of the current invocation.
    message_id: str
    message_type: str
    from_agent: str
    conversation_id: str | None
    body: dict[str, Any]
    household_id: str | None
    text: str
    # Decisions and the step's result.
    intent: str | None
    outgoing: list[dict[str, Any]]
    finish: str | None
    last_message_id: str | None


@dataclass(frozen=True)
class RunInfo:
    """What the graph reads from the `runs` row: the household and the member's text."""

    household_id: str | None
    text: str


LoadRun = Callable[[UUID], Awaitable[RunInfo]]
CallTool = Callable[..., Awaitable[dict[str, Any]]]
IssueToken = Callable[[str, str, Sequence[str]], str]


def _mock_token(sub: str, act: str, scopes: Sequence[str]) -> str:
    # Every run uses the mock issuer until identity-authz replaces the token source (Q4).
    return MockIssuer().issue(sub, act, list(scopes))


def _member_user_id(card: AgentCard) -> UUID:
    """The member a consumer card serves: its `owner_ref` `user:<user_id>`."""
    kind, sep, value = card.owner_ref.partition(":")
    if kind != "user" or not sep:
        raise ValueError(f"consumer card {card.agent_id} is not owned by a user")
    return UUID(value)


def _ctx(state: ConsumerState) -> RunContext:
    conversation = state.get("conversation_id")
    return RunContext(
        run_id=UUID(state["run_id"]),
        agent_id=state["agent_id"],
        conversation_id=UUID(conversation) if conversation else None,
    )


def _out(state: ConsumerState, ordinal: int, to_agent: str, type: str, body: dict) -> dict:
    return {
        "message_id": str(ids.message_id(UUID(state["message_id"]), ordinal)),
        "to_agent": to_agent,
        "type": type,
        "body": body,
        "conversation_id": state.get("conversation_id"),
        "causation_id": state["message_id"],
    }


def build_graph(model: ModelClient, call_tool: CallTool, issue_token: IssueToken) -> StateGraph:
    """The consumer graph's nodes and edges, before `compile(checkpointer=...)`."""

    async def understand_message(state: ConsumerState) -> dict[str, Any]:
        text = state.get("text", "")
        result = await model.complete(
            _ctx(state),
            [{"role": "system", "content": CLASSIFY_PROMPT}, {"role": "user", "content": text}],
            None,
            "understand_message",
            state={"text": text},
        )
        answer = (result.text or "").strip().lower()
        # Code decides: anything outside the three intents is chat, which orders nothing.
        return {"intent": answer if answer in INTENTS else CHAT}

    async def relay_to_household(state: ConsumerState) -> dict[str, Any]:
        household_id = state.get("household_id")
        if not household_id:
            raise ValueError("the run has no household to relay the order to")
        type, kind = RELAY[state["intent"] or CHAT]
        out = _out(state, 0, f"household:{household_id}", type, {"kind": kind})
        return {"outgoing": [out]}

    async def reply_to_user(state: ConsumerState) -> dict[str, Any]:
        text = state.get("text", "")
        result = await model.complete(
            _ctx(state),
            [{"role": "system", "content": REPLY_PROMPT}, {"role": "user", "content": text}],
            None,
            "reply_to_user",
            state={"text": text},
        )
        reply = (result.text or "").strip()[:CHAT_REPLY_MAX_CHARS]
        body = {
            "user_id": state["member_user_id"],
            "template": CHAT_REPLY_TEMPLATE,
            "params": {"text": reply},
            "buttons": [],
        }
        return {"outgoing": [_out(state, 0, WHATSAPP, "whatsapp_out", body)], "finish": "completed"}

    async def answer_constraints_request(state: ConsumerState) -> dict[str, Any]:
        member = state["member_user_id"]
        body = state["body"]
        if str(body.get("user_id")) != member:
            raise ConstraintsMismatch("constraints_request names another person")
        token = issue_token(f"user:{member}", state["agent_id"], [CONSTRAINTS_SCOPE])
        rows = await call_tool(
            HOUSEHOLD_SERVER, "get_constraints", {"user_id": member}, ctx=_ctx(state), token=token
        )
        if str(rows.get("user_id")) != member:
            raise ConstraintsMismatch("get_constraints returned another person's rows")
        # Codes only: a value outside the §5 code lists fails validation (fails closed).
        summary = Constraints.model_validate(
            {
                "order_id": body["order_id"],
                "allergens": rows.get("allergies", []),
                "diets": rows.get("diets", []),
                "dislikes": rows.get("dislikes", []),
            }
        ).model_dump(mode="json")
        return {"outgoing": [_out(state, 0, state["from_agent"], "constraints", summary)]}

    async def relay_shortfall(state: ConsumerState) -> dict[str, Any]:
        body = state["body"]
        shortfall_id = str(body["shortfall_id"])
        out = []
        for line in body["lines"]:
            # The model sees only the missing item and its substitutes, never a profile.
            facts = {
                "missing": line["name"],
                "qty": line["qty_missing"],
                "substitutes": [s["name"] for s in line.get("substitutes") or []],
            }
            result = await model.complete(
                _ctx(state),
                [
                    {"role": "system", "content": SHORTFALL_PROMPT},
                    {"role": "user", "content": json.dumps(facts, sort_keys=True)},
                ],
                None,
                "relay_shortfall",
                state=facts,
            )
            text = (result.text or "").strip()[:CHAT_REPLY_MAX_CHARS]
            notice = {
                "user_id": state["member_user_id"],
                "template": SHORTFALL_PROMPT_TEMPLATE,
                "params": {"text": text},
                "buttons": shortfall_buttons(shortfall_id, line),
            }
            out.append(_out(state, len(out), WHATSAPP, "whatsapp_out", notice))
        return {"outgoing": out}

    def route_message(state: ConsumerState) -> str:
        return state["message_type"]

    def route_intent(state: ConsumerState) -> str:
        return "relay_to_household" if state.get("intent") in RELAY else "reply_to_user"

    def mark_handled(fn: Callable[[ConsumerState], Awaitable[dict[str, Any]]]):
        async def node(state: ConsumerState) -> dict[str, Any]:
            return {**await fn(state), "last_message_id": state["message_id"]}

        return node

    g = StateGraph(ConsumerState)
    g.add_node("understand_message", understand_message)
    g.add_node("answer_constraints_request", mark_handled(answer_constraints_request))
    g.add_node("relay_to_household", mark_handled(relay_to_household))
    g.add_node("reply_to_user", mark_handled(reply_to_user))
    g.add_node("relay_shortfall", mark_handled(relay_shortfall))
    g.add_conditional_edges(
        START,
        route_message,
        {
            "user_message": "understand_message",
            "constraints_request": "answer_constraints_request",
            "shortfall_relay": "relay_shortfall",
        },
    )
    g.add_conditional_edges(
        "understand_message",
        route_intent,
        {"relay_to_household": "relay_to_household", "reply_to_user": "reply_to_user"},
    )
    g.add_edge("relay_to_household", END)
    g.add_edge("reply_to_user", END)
    g.add_edge("answer_constraints_request", END)
    g.add_edge("relay_shortfall", END)
    return g


class ConsumerGraph:
    """The consumer class handler, `handle(msg, card) -> StepResult` (architecture item 27)."""

    def __init__(
        self,
        *,
        model: ModelClient,
        checkpointer: BaseCheckpointSaver,
        load_run: LoadRun,
        call_tool: CallTool = mcp_client.call_tool,
        issue_token: IssueToken = _mock_token,
    ) -> None:
        self._load_run = load_run
        self.graph = build_graph(model, call_tool, issue_token).compile(checkpointer=checkpointer)

    async def __call__(self, msg: Inbound, card: AgentCard) -> StepResult:
        row = msg.row
        if row.type not in HANDLED_TYPES:
            raise UnsupportedMessage(f"the consumer graph does not handle {row.type!r}")
        member = _member_user_id(card)
        config = {"configurable": {"thread_id": ids.thread_id(card.agent_id, row.run_id)}}

        snapshot = await self.graph.aget_state(config)
        saved: Mapping[str, Any] = snapshot.values or {}
        if saved and saved.get("state_version") != STATE_VERSION:
            return StepResult(finish=RunFinish("error", "state_version"))
        if saved.get("last_message_id") == str(row.message_id) and not snapshot.next:
            # The invocation finished but its step did not commit: return the same result.
            return self._result(card, saved)

        run = await self._load_run(row.run_id)
        body = msg.body.model_dump(mode="json")
        state = await self.graph.ainvoke(
            ConsumerState(
                state_version=STATE_VERSION,
                run_id=str(row.run_id),
                agent_id=card.agent_id,
                member_user_id=str(member),
                message_id=str(row.message_id),
                message_type=row.type,
                from_agent=row.from_agent,
                conversation_id=str(row.conversation_id) if row.conversation_id else None,
                body=body,
                household_id=run.household_id,
                text=run.text,
                intent=None,
                outgoing=[],
                finish=None,
            ),
            config,
        )
        return self._result(card, state)

    @staticmethod
    def _result(card: AgentCard, state: Mapping[str, Any]) -> StepResult:
        messages = [
            Outgoing(
                message_id=UUID(m["message_id"]),
                from_agent=card.agent_id,
                to_agent=m["to_agent"],
                type=m["type"],
                body=m["body"],
                conversation_id=UUID(m["conversation_id"]) if m["conversation_id"] else None,
                causation_id=UUID(m["causation_id"]),
            )
            for m in state.get("outgoing") or []
        ]
        status: Literal["completed"] | None = state.get("finish")
        return StepResult(
            messages=messages, finish=RunFinish(status) if status is not None else None
        )


# The host's handler. Built on first use, after the host has opened the pool.

_RUN_SQL = "SELECT household_id, trigger->>'text' FROM runs WHERE run_id = %s"
_default: ConsumerGraph | None = None


def _build_default() -> ConsumerGraph:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from services.agents.common.model import PgRunState
    from services.common import db
    from services.common.trace import TraceStore

    pool = db.get_pool()

    async def load_run(run_id: UUID) -> RunInfo:
        async with pool.connection() as conn:
            cur = await conn.execute(_RUN_SQL, (run_id,))
            row = await cur.fetchone()
        if row is None:
            raise LookupError(f"run {run_id} does not exist")
        return RunInfo(household_id=row[0], text=row[1] or "")

    return ConsumerGraph(
        model=ModelClient(TraceStore(pool), PgRunState(pool)),
        checkpointer=AsyncPostgresSaver(pool),  # type: ignore[arg-type]
        load_run=load_run,
    )


async def handle(msg: Inbound, card: AgentCard) -> StepResult:
    """The consumer class handler registered in `services.agents.host.HANDLERS`."""
    global _default
    if _default is None:
        _default = _build_default()
    return await _default(msg, card)
