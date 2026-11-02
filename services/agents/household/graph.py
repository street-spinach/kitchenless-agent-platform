"""The household graph: the weekly order main path (architecture item 29, §8; agent-protocol
§7 replay safety).

A household agent is a LangGraph graph checkpointed under
`thread_id = <agent_id>:<run_id>`. Each inbound message is one invocation of the thread,
routed by its type and the order's phase:

*   `start_weekly_order` / `start_dinner` → `start_order` (the deterministic
    `ids.order_id(run_id)` is in the checkpoint before `create_order` uses it) →
    `collect_constraints` (one `constraints_request` per member's consumer agent).
*   `constraints` → `record_constraints`; once every member has answered, `build_list`
    (pantry check, then the model's list) → `request_quotes` (`directory.find`, one
    `quote_request` per seller, the 30 s `quote_round` timer, state `quoting`).
*   `offer` while quoting, or the reply to our counter → `record_offer`. Any other offer is
    late: ignored with a `decision` trace `late_offer` (agent-protocol §7 arrival table).
*   `quote_round_closed` → `negotiate` (state `negotiating`; at most 2 counter rounds) →
    `choose_offer` (the cheapest valid offer) → `request_approval` when the total is over
    the remaining weekly budget, else `authorize` (`authorize_spend`, then `accept`).
*   `request_approval` calls the tool (which sends the approver's buttons, creates the
    `approval_expiry` timer and pauses `run_timeout`) and leaves the thread in the
    checkpointed `awaiting_approval` phase: the run waits there, across restarts, until an
    `approval_decision` arrives; other messages meanwhile are ignored.
*   `approval_decision` `approved` → `authorize`; `rejected` or `expired` → the run ends
    `cancelled`. On `expired` (CUJ 3) the household also sends `cancel` to the seller and
    "Order cancelled: no approval." to the approver. A re-quoted offer at or below the
    approved total uses that approval; a higher total asks again.
*   `reject` from a seller → `seller_rejected`, the household rule of agent-protocol §7:
    `expired` (answering our `accept`) → `requote` once (`authorized → quoting`, a new
    round of `quote_request`), a second time `error, offer_expired`; `no_courier` → the
    next valid offer, else `error, no_courier`; `already_placed` → ignored; any other
    reason → the seller is excluded from the run and the next valid offer is taken.
*   `protocol_error` → `protocol_error`, the agent-protocol §6 reaction by the refused
    message's type (read from `sent`): a `quote_request` or `counter_offer` drops that
    seller; an `accept` or `cancel` ends the run `error, protocol_error`; anything else is
    a `decision` trace.
*   `order_confirm`, `delivery_booked`, `order_status` → `track_order`; on `delivered`,
    `record_delivery` (weekly orders only) → `finish` (`completed` and a summary
    `whatsapp_out`).
*   The shortfall (agent-protocol §10, P21): `shortfall_notice` → `handle_shortfall`
    (`open_shortfall`, the `shortfall_expiry` timer at the notice's `reply_by`, one
    `shortfall_relay` per member with a WhatsApp identity) → `wait_shortfall`.
    `shortfall_closed` → `apply_shortfall` (`get_shortfall`, every line without a pick
    becomes a drop, `authorize_amendment`, then `shortfall_decision` with the returned
    picks). The seller's next `order_confirm` → `shortfall_result`: one `whatsapp_out` per
    relayed member listing what was substituted and dropped; with `state=failed` the run
    ends `error, nothing_to_deliver`.

**Replay safety (agent-protocol §7).** Decision nodes never write to Postgres or send:
they put the step's outgoing messages, timer changes and run outcome into graph state. A
separate `send` node files exactly those under the inbound `message_id` in `sent`, and the
handler returns the `StepResult` built from that entry, which the consumer commits in one
transaction. A replay of a message already filed returns the same result without running
anything. A replay of an invocation that stopped after a checkpoint resumes it from that
checkpoint, so the decision nodes do not run again and the bodies sent are the
checkpointed ones (P-CUJ 8).

**Business rationale.** `rationale` is removed from every offer as it enters state, so no
model prompt can contain it (architecture item 29).
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from services.agents.common.model import InvalidOutput, ModelClient, TraceSink
from services.agents.household import stubs
from services.common import ids, mcp_client
from services.common.consumer import (
    Inbound,
    Outgoing,
    RunFinish,
    StepResult,
    TimerCreate,
)
from services.common.directory import AgentCard
from services.common.mcp_client import ToolRefused
from services.common.protocol.schemas import RequestItem
from services.common.run_context import RunContext
from services.identity.tokens import MockIssuer

__all__ = [
    "HANDLED_TYPES",
    "MAX_COUNTER_ROUNDS",
    "OFFER_TTL_SECONDS",
    "QUOTE_ROUND_SECONDS",
    "STATE_VERSION",
    "HouseholdGraph",
    "HouseholdState",
    "Members",
    "RunInfo",
    "UnsupportedMessage",
    "build_graph",
    "handle",
    "strip_rationale",
]

log = logging.getLogger(__name__)

STATE_VERSION = 1
QUOTE_ROUND_SECONDS = 30
MAX_COUNTER_ROUNDS = 2
OFFER_TTL_SECONDS = 600  # agent-protocol §13; the run override `OFFER_TTL_SECONDS` wins
MAX_REQUOTES = 1
# The delivery window asked for in `quote_request`, relative to the request.
WINDOW_START = timedelta(hours=2)
WINDOW_END = timedelta(hours=4)
HOUSEHOLD_SERVER = "household"
WHATSAPP = "channel:whatsapp"
SELLER_CLASS = {"grocery": "grocery", "dinner": "restaurant"}
START_KIND = {"start_weekly_order": "grocery", "start_dinner": "dinner"}
# Scopes per household tool (identity-authz §8 step 1b).
SCOPES: Mapping[str, tuple[str, ...]] = {
    "create_order": ("orders:place",),
    "authorize_spend": ("orders:place",),
    "request_approval": ("approvals:request",),
    "record_delivery": ("pantry:write",),
    "get_pantry": ("pantry:read",),
    "get_budget": (),
    "set_order_state": ("orders:place",),
    "open_shortfall": (),
    "get_shortfall": (),
    "authorize_amendment": ("orders:place",),
}
HANDLED_TYPES = frozenset(
    {
        "start_weekly_order",
        "start_dinner",
        "constraints",
        "offer",
        "quote_round_closed",
        "approval_decision",
        "order_confirm",
        "delivery_booked",
        "order_status",
        "reject",
        "protocol_error",
        "shortfall_notice",
        "shortfall_closed",
    }
)
# agent-protocol §6 step 4: what the household does when one of its messages is refused.
DROP_SELLER_ON_REFUSAL = frozenset({"quote_request", "counter_offer"})
END_RUN_ON_REFUSAL = frozenset({"accept", "cancel"})
SUMMARY_TEMPLATE = "order_summary"
NO_OFFERS_TEMPLATE = "no_offers"
APPROVAL_CANCELLED_TEMPLATE = "approval_cancelled"
SHORTFALL_RESULT_TEMPLATE = "shortfall_result"
DROP = "drop"
NOTHING_TO_DELIVER = "nothing_to_deliver"
# `state.shortfall.status`: waiting for `shortfall_closed`, `shortfall_decision` sent, the
# seller's `order_confirm` told the members, or `authorize_amendment` refused.
SF_PENDING = "pending"
SF_DECIDED = "decided"
SF_APPLIED = "applied"
SF_REFUSED = "refused"

# Phases of the household's order, kept in graph state.
NEW = "new"
CONSTRAINTS = "constraints"
QUOTING = "quoting"
NEGOTIATING = "negotiating"
AWAITING_APPROVAL = "awaiting_approval"
PLACING = "placing"
DONE = "done"

BUILD_LIST_PROMPT = (
    "Plan this household's order. Answer with one JSON object "
    '{"items": [{"item": <name>, "qty": <int>, "unit": <unit>}]}. '
    "Skip what the pantry already has."
)
NEGOTIATE_PROMPT = (
    "Decide whether to counter one offer or to choose. Answer with one JSON object: "
    '{"action": "choose"} or {"action": "counter", "quote_id": <id>, "total_minor": <int>}.'
)
CHOOSE_PROMPT = (
    'Choose one offer. Answer with one JSON object {"quote_id": <id>, "explanation": <text>}.'
)


class UnsupportedMessage(ValueError):
    """The household graph has no path for this message type."""


class HouseholdState(TypedDict, total=False):
    # Required by architecture item 29.
    state_version: int
    run_id: str
    household_id: str
    order_id: str | None
    kind: str | None
    quotes: dict[str, dict[str, Any]]  # seller agent id -> its latest offer, no rationale
    chosen_quote_id: str | None
    approval_id: str | None
    # The run and the household.
    agent_id: str
    user_id: str | None  # the member who started the run
    region: str
    zone_id: str
    currency: str
    phase: str
    members: dict[str, str]  # consumer agent id -> user id
    constraints: dict[str, dict[str, list[str]]]  # consumer agent id -> codes
    items: list[dict[str, Any]]
    conversations: dict[str, str]  # seller agent id -> conversation id
    counters: int
    awaiting_counter: str | None  # the seller whose reply to our counter we wait for
    remaining_minor: int | None
    excluded_sellers: list[str]  # sellers dropped from the run (agent-protocol §6, §7)
    requotes: int  # 0 or 1 (agent-protocol §7 re-quote)
    approval_minor: int | None  # the total the pending approval asks for
    approved_minor: int | None  # the total an `approved` approval covers
    approver_user_id: str | None  # who the pending approval asks (`request_approval`)
    accepts: int  # `accept` messages decided so far
    accept_delay_rounds: int  # the run override `STUB_ACCEPT_DELAY_ROUNDS`
    offer_ttl_seconds: int
    # The order's shortfall (agent-protocol §10): id, seller, lines, picks, status, the
    # members it was relayed to, the refused lines and the amended order.
    shortfall: dict[str, Any] | None
    # The inbound message of the current invocation.
    message_id: str
    message_type: str
    from_agent: str
    conversation_id: str | None
    body: dict[str, Any]
    # The current invocation's decisions, and every filed step result.
    decision: str | None
    outgoing: list[dict[str, Any]]
    timers: list[dict[str, Any]]
    finish: dict[str, Any] | None
    sent: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class RunInfo:
    """What the graph reads from the `runs` row: the member who started the run, and the
    run's overrides (`runs.trigger.overrides`)."""

    user_id: str | None
    overrides: Mapping[str, Any] = field(default_factory=dict)


Members = Mapping[str, str]
LoadRun = Callable[[UUID], Awaitable[RunInfo]]
LoadMembers = Callable[[str], Awaitable[Members]]
FindSellers = Callable[[str, str, str, str], Awaitable[Sequence[str]]]
CallTool = Callable[..., Awaitable[dict[str, Any]]]
IssueToken = Callable[[str, str, Sequence[str]], str]
Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


def _mock_token(sub: str, act: str, scopes: Sequence[str]) -> str:
    # Every run uses the mock issuer until identity-authz replaces the token source (Q4).
    return MockIssuer().issue(sub, act, list(scopes))


def _now() -> datetime:
    return datetime.now(UTC)


def strip_rationale(body: Mapping[str, Any]) -> dict[str, Any]:
    """The body without the business `rationale` (architecture item 29)."""
    return {k: v for k, v in body.items() if k != "rationale"}


def _household_id(card: AgentCard) -> str:
    kind, sep, value = card.owner_ref.partition(":")
    if kind != "household" or not sep or not value:
        raise ValueError(f"household card {card.agent_id} is not owned by a household")
    return value


def _ctx(state: HouseholdState, conversation_id: str | None = None) -> RunContext:
    conversation = conversation_id or state.get("conversation_id")
    return RunContext(
        run_id=UUID(state["run_id"]),
        agent_id=state["agent_id"],
        conversation_id=UUID(conversation) if conversation else None,
    )


def _add(
    state: HouseholdState,
    out: list[dict[str, Any]],
    to_agent: str,
    type: str,
    body: dict[str, Any],
    *,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Append one outgoing message; its id is `message_id(<inbound>, <ordinal>)`."""
    msg = {
        "message_id": str(ids.message_id(UUID(state["message_id"]), len(out))),
        "to_agent": to_agent,
        "type": type,
        "body": body,
        "conversation_id": conversation_id,
        "causation_id": state["message_id"],
    }
    out.append(msg)
    return msg


def _expired(offer: Mapping[str, Any], now: datetime) -> bool:
    expires = offer.get("expires_at")
    if not expires:
        return True
    return datetime.fromisoformat(str(expires)) <= now


def valid_offers(state: HouseholdState, now: datetime) -> list[dict[str, Any]]:
    """Offers that can be taken: live, in the household's currency, with an offered line,
    from a seller not excluded from the run."""
    valid = []
    excluded = set(state.get("excluded_sellers") or [])
    for seller, offer in sorted((state.get("quotes") or {}).items()):
        if seller in excluded:
            continue
        if offer.get("currency") != state.get("currency") or _expired(offer, now):
            continue
        if not any(line.get("offered_sku") for line in offer.get("lines", [])):
            continue
        valid.append({**offer, "seller": seller})
    return valid


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _exclude(state: HouseholdState, seller: str) -> dict[str, Any]:
    """The state update that drops `seller` from the run, as if its offer never came."""
    excluded = sorted({*(state.get("excluded_sellers") or []), seller})
    quotes = {s: q for s, q in (state.get("quotes") or {}).items() if s != seller}
    update: dict[str, Any] = {"excluded_sellers": excluded, "quotes": quotes}
    if state.get("awaiting_counter") == seller:
        update["awaiting_counter"] = None
    return update


def _sent_message(state: HouseholdState, message_id: str) -> dict[str, Any] | None:
    """One of the household's own filed outgoing messages, by id."""
    for entry in (state.get("sent") or {}).values():
        for m in entry.get("messages") or []:
            if m["message_id"] == message_id:
                return m
    return None


def _cheapest(offers: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    return min(offers, key=lambda o: (o["total_minor"], o["seller"]), default=None)


def _json(text: str | None) -> dict[str, Any]:
    try:
        answer = json.loads(text or "")
    except (TypeError, ValueError):
        raise InvalidOutput("the model's answer is not JSON") from None
    if not isinstance(answer, dict):
        raise InvalidOutput("the model's answer is not a JSON object")
    return answer


def _prompt(system: str, data: Mapping[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(data, default=str, sort_keys=True)},
    ]


def build_graph(
    model: ModelClient,
    traces: TraceSink,
    call_tool: CallTool,
    issue_token: IssueToken,
    load_members: LoadMembers,
    find_sellers: FindSellers,
    clock: Clock = _now,
    sleep: Sleep = asyncio.sleep,
    load_whatsapp_members: LoadMembers | None = None,
) -> StateGraph:
    """The household graph's nodes and edges, before `compile(checkpointer=...)`.

    `load_whatsapp_members` gives the members with a WhatsApp identity, the ones a
    shortfall is relayed to; without it nobody is asked (fails closed: every line drops).
    """

    async def tool(
        state: HouseholdState, name: str, args: dict[str, Any], conversation_id: str | None = None
    ) -> dict[str, Any]:
        user_id = state.get("user_id")
        if not user_id:
            # Fails closed: a tool call needs the member the household agent acts for.
            raise ValueError("the run names no member to act for")
        token = issue_token(f"user:{user_id}", state["agent_id"], SCOPES[name])
        return await call_tool(
            HOUSEHOLD_SERVER, name, args, ctx=_ctx(state, conversation_id), token=token
        )

    async def trace_decision(state: HouseholdState, node: str, choice: str) -> None:
        await traces.write(
            UUID(state["run_id"]),
            state["agent_id"],
            "decision",
            {"node": node, "choice": choice, "explanation": "", "ref": state["message_id"]},
            on_behalf_of=f"household:{state['household_id']}",
            conversation_id=_ctx(state).conversation_id,
        )

    async def complete(
        state: HouseholdState, node: str, system: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """One model step. The prompt and the stub see the same rationale-free `data`."""
        messages = _prompt(system, data)
        result = await model.complete(
            _ctx(state), messages, None, node, state={**data, "messages": messages}
        )
        return _json(result.text)

    # Nodes ------------------------------------------------------------------------------

    async def accept_delay(state: HouseholdState) -> None:
        """`STUB_ACCEPT_DELAY_ROUNDS` (agent-protocol P21): stub mode only."""
        if model.mode != "stub":
            return
        seconds = stubs.accept_delay_seconds(state)
        if seconds > 0:
            await sleep(seconds)

    async def ignore(state: HouseholdState) -> dict[str, Any]:
        reason = {"offer": "late_offer"}.get(state["message_type"], "ignored")
        await trace_decision(state, "ignore", reason)
        return {}

    async def start_order(state: HouseholdState) -> dict[str, Any]:
        kind = START_KIND[state["message_type"]]
        order_id = state.get("order_id") or str(ids.order_id(UUID(state["run_id"])))
        await tool(state, "create_order", {"order_id": order_id, "kind": kind})
        return {"order_id": order_id, "kind": kind}

    async def collect_constraints(state: HouseholdState) -> dict[str, Any]:
        members = dict(await load_members(state["household_id"]))
        if not members:
            raise ValueError(f"household {state['household_id']} has no consumer agents")
        out = list(state.get("outgoing") or [])
        for agent_id, user_id in sorted(members.items()):
            body = {"order_id": state["order_id"], "user_id": user_id}
            _add(state, out, agent_id, "constraints_request", body)
        return {"members": members, "constraints": {}, "phase": CONSTRAINTS, "outgoing": out}

    async def record_constraints(state: HouseholdState) -> dict[str, Any]:
        body = state["body"]
        codes = {"allergens": list(body["allergens"]), "diets": list(body["diets"])}
        return {"constraints": {**(state.get("constraints") or {}), state["from_agent"]: codes}}

    async def build_list(state: HouseholdState) -> dict[str, Any]:
        pantry = await tool(state, "get_pantry", {})
        have = {str(p["item"]).lower() for p in pantry.get("items", [])}
        answer = await complete(
            state,
            "build_list",
            BUILD_LIST_PROMPT,
            {"kind": state["kind"], "pantry": pantry.get("items", [])},
        )
        try:
            items = [RequestItem.model_validate(i) for i in answer.get("items") or []]
        except ValidationError:
            raise InvalidOutput("build_list returned an invalid item") from None
        # Code decides: what the pantry already holds is not ordered again.
        items = [i for i in items if i.item.lower() not in have]
        if not items:
            raise InvalidOutput("build_list returned no items")
        return {"items": [i.model_dump(mode="json") for i in items]}

    async def request_quotes(state: HouseholdState) -> dict[str, Any]:
        if not state.get("requotes"):
            # On a re-quote, `requote` has already moved the order `authorized → quoting`.
            await tool(
                state,
                "set_order_state",
                {"order_id": state["order_id"], "expected": "draft", "new": "quoting"},
            )
        cls = SELLER_CLASS[state["kind"] or "grocery"]
        found = await find_sellers(cls, state["region"], state["zone_id"], state["currency"])
        excluded = set(state.get("excluded_sellers") or [])
        sellers = [s for s in found if s not in excluded]
        allergens: set[str] = set()
        diets: set[str] = set()
        for codes in (state.get("constraints") or {}).values():
            allergens.update(codes["allergens"])
            diets.update(codes["diets"])
        now = clock()
        body = {
            "order_id": state["order_id"],
            "kind": state["kind"],
            "items": state["items"],
            "constraints": {"allergens": sorted(allergens), "diets": sorted(diets)},
            "currency": state["currency"],
            "zone_id": state["zone_id"],
            "window": {
                "start": (now + WINDOW_START).isoformat(),
                "end": (now + WINDOW_END).isoformat(),
            },
            "reply_by": (now + timedelta(seconds=QUOTE_ROUND_SECONDS)).isoformat(),
        }
        out = list(state.get("outgoing") or [])
        conversations = dict(state.get("conversations") or {})
        for seller in sellers:
            message_id = ids.message_id(UUID(state["message_id"]), len(out))
            conversation = str(ids.conversation_id(message_id))
            _add(state, out, seller, "quote_request", body, conversation_id=conversation)
            conversations[seller] = conversation
        timer = {
            "kind": "quote_round",
            "seconds": QUOTE_ROUND_SECONDS,
            "ref": {"order_id": state["order_id"], "round": "quote", "to": state["agent_id"]},
        }
        return {
            "phase": QUOTING,
            "quotes": {},
            "conversations": conversations,
            "outgoing": out,
            "timers": [*(state.get("timers") or []), timer],
        }

    async def record_offer(state: HouseholdState) -> dict[str, Any]:
        offer = strip_rationale(state["body"])
        seller = state["from_agent"]
        return {"quotes": {**(state.get("quotes") or {}), seller: offer}, "awaiting_counter": None}

    async def negotiate(state: HouseholdState) -> dict[str, Any]:
        update: dict[str, Any] = {}
        if state.get("phase") == QUOTING:
            await tool(
                state,
                "set_order_state",
                {"order_id": state["order_id"], "expected": "quoting", "new": "negotiating"},
            )
            update["phase"] = NEGOTIATING
        offers = valid_offers(state, clock())
        if not offers:
            return {**update, "decision": "no_offers"}
        budget = await tool(state, "get_budget", {})
        remaining = int(budget["remaining"]["amount_minor"])
        counters = int(state.get("counters") or 0)
        answer = await complete(
            state,
            "negotiate",
            NEGOTIATE_PROMPT,
            {"offers": offers, "remaining_minor": remaining, "counters": counters},
        )
        update["remaining_minor"] = remaining
        # Code decides whether the proposed counter is allowed.
        target = next((o for o in offers if o["quote_id"] == answer.get("quote_id")), None)
        total = answer.get("total_minor")
        if (
            answer.get("action") == "counter"
            and counters < MAX_COUNTER_ROUNDS
            and target is not None
            and not target.get("final")
            and isinstance(total, int)
            and not isinstance(total, bool)
            and 0 < total < target["total_minor"]
        ):
            seller = target["seller"]
            body = {
                "order_id": state["order_id"],
                "quote_id": target["quote_id"],
                "round": target["round"],
                "lines": target["lines"],
                "total_minor": total,
                "currency": target["currency"],
            }
            out = list(state.get("outgoing") or [])
            conversation = (state.get("conversations") or {}).get(seller)
            _add(state, out, seller, "counter_offer", body, conversation_id=conversation)
            return {
                **update,
                "decision": "counter",
                "counters": counters + 1,
                "awaiting_counter": seller,
                "outgoing": out,
            }
        return {**update, "decision": "choose"}

    async def no_offers(state: HouseholdState) -> dict[str, Any]:
        out = list(state.get("outgoing") or [])
        if state.get("user_id"):
            body = {
                "user_id": state["user_id"],
                "template": NO_OFFERS_TEMPLATE,
                "params": {},
                "buttons": [],
            }
            _add(state, out, WHATSAPP, "whatsapp_out", body)
        return {
            "phase": DONE,
            "outgoing": out,
            "finish": {"status": "error", "reason": "no_offers"},
        }

    async def choose_offer(state: HouseholdState) -> dict[str, Any]:
        offers = valid_offers(state, clock())
        answer = await complete(
            state,
            "choose_offer",
            CHOOSE_PROMPT,
            {"offers": offers, "remaining_minor": state.get("remaining_minor")},
        )
        # Code decides: a pick outside the valid offers gives the cheapest valid offer.
        pick = next((o for o in offers if o["quote_id"] == answer.get("quote_id")), None)
        pick = pick or _cheapest(offers)
        if pick is None:
            return {"decision": "no_offers"}
        remaining = state.get("remaining_minor")
        over = remaining is None or pick["total_minor"] > remaining
        approved = state.get("approved_minor")
        if over and approved is not None and pick["total_minor"] <= approved:
            # A re-quoted offer at or below the approved total uses that approval (§7).
            over = False
        return {
            "chosen_quote_id": pick["quote_id"],
            "decision": "request_approval" if over else "authorize",
        }

    async def request_approval(state: HouseholdState) -> dict[str, Any]:
        offer = _chosen(state)
        amount = {"amount_minor": offer["total_minor"], "currency": offer["currency"]}
        result = await tool(
            state, "request_approval", {"order_id": state["order_id"], "amount": amount}
        )
        # The tool sends the approver's prompt, creates the `approval_expiry` timer and
        # pauses `run_timeout` in its own transaction. The thread now waits: the next
        # invocation that reaches a decision node is the `approval_decision` (§8).
        return {
            "approval_id": str(result["approval_id"]),
            "approval_minor": offer["total_minor"],
            "approver_user_id": result.get("approver_user_id"),
            "phase": AWAITING_APPROVAL,
        }

    async def approval_decided(state: HouseholdState) -> dict[str, Any]:
        body = state["body"]
        if str(body["approval_id"]) != state.get("approval_id"):
            await trace_decision(state, "approval_decided", "unknown_approval")
            return {"decision": "ignore"}
        if body["status"] == "approved":
            return {"decision": "authorize", "approved_minor": state.get("approval_minor")}
        update: dict[str, Any] = {
            "decision": "cancelled",
            "phase": DONE,
            "finish": {"status": "cancelled", "reason": f"approval_{body['status']}"},
        }
        if body["status"] == "expired":
            # CUJ 3: the seller's open quote is closed and the admin is told.
            out = list(state.get("outgoing") or [])
            offer = _chosen(state)
            seller = _seller_of(state, offer["quote_id"])
            cancel = {
                "order_id": state["order_id"],
                "quote_id": offer["quote_id"],
                "reason": "buyer_cancelled",
            }
            conversation = (state.get("conversations") or {}).get(seller)
            _add(state, out, seller, "cancel", cancel, conversation_id=conversation)
            admin = state.get("approver_user_id") or state.get("user_id")
            if admin:
                notice = {
                    "user_id": admin,
                    "template": APPROVAL_CANCELLED_TEMPLATE,
                    "params": {},
                    "buttons": [],
                }
                _add(state, out, WHATSAPP, "whatsapp_out", notice)
            update["outgoing"] = out
        return update

    async def authorize(state: HouseholdState) -> dict[str, Any]:
        await accept_delay(state)
        offer = _chosen(state)
        items = [
            {
                "line": line["line"],
                "sku": line["offered_sku"],
                "name": line["offered_name"] or line["offered_sku"],
                "qty": line["qty"],
                "unit_minor": line["unit_minor"],
                "substituted_for": line.get("substituted_for"),
            }
            for line in offer["lines"]
            if line.get("offered_sku")
        ]
        seller = _seller_of(state, offer["quote_id"])
        conversation = (state.get("conversations") or {}).get(seller)
        try:
            result = await tool(
                state,
                "authorize_spend",
                {
                    "order_id": state["order_id"],
                    "quote_id": offer["quote_id"],
                    "items": items,
                    "total": {"amount_minor": offer["total_minor"], "currency": offer["currency"]},
                },
                conversation,
            )
        except ToolRefused as refused:
            # Fails closed: nothing is accepted (architecture §9, guard refusal).
            return {"phase": DONE, "finish": {"status": "error", "reason": refused.reason}}
        body = {
            "order_id": state["order_id"],
            "quote_id": offer["quote_id"],
            "items_hash": result["items_hash"],
            "total_minor": offer["total_minor"],
        }
        out = list(state.get("outgoing") or [])
        _add(state, out, seller, "accept", body, conversation_id=conversation)
        accepts = int(state.get("accepts") or 0) + 1
        return {"phase": PLACING, "outgoing": out, "accepts": accepts}

    async def seller_rejected(state: HouseholdState) -> dict[str, Any]:
        """A seller's `reject`: the household rule of agent-protocol §7."""
        body = state["body"]
        reason = body.get("reason") or "other"
        quote_id = str(body["quote_id"])
        try:
            seller = _seller_of(state, quote_id)
        except LookupError:
            await trace_decision(state, "seller_rejected", "unknown_quote")
            return {"decision": "ignore"}
        phase = state.get("phase")
        after_accept = phase == PLACING and quote_id == state.get("chosen_quote_id")
        countered = phase == NEGOTIATING and state.get("awaiting_counter") == seller
        if reason == "already_placed" or not (after_accept or countered or phase == QUOTING):
            await trace_decision(state, "seller_rejected", f"ignored_{reason}")
            return {"decision": "ignore"}
        if reason == "expired" and after_accept:
            if int(state.get("requotes") or 0) >= MAX_REQUOTES:
                await trace_decision(state, "seller_rejected", "offer_expired")
                return {
                    "decision": "finish",
                    "phase": DONE,
                    "finish": {"status": "error", "reason": "offer_expired"},
                }
            await trace_decision(state, "seller_rejected", "requote")
            return {"decision": "requote"}
        # `no_courier` and any other reason: drop the seller, take the next valid offer.
        update = _exclude(state, seller)
        await trace_decision(state, "seller_rejected", f"excluded_{reason}")
        if phase == QUOTING:
            return {**update, "decision": "ignore"}
        if valid_offers({**state, **update}, clock()):  # type: ignore[typeddict-item]
            return {**update, "decision": "next_offer"}
        if reason == "no_courier":
            return {
                **update,
                "decision": "finish",
                "phase": DONE,
                "finish": {"status": "error", "reason": "no_courier"},
            }
        return {**update, "decision": "no_offers"}

    async def requote(state: HouseholdState) -> dict[str, Any]:
        """The one re-quote after an expired offer: `authorized → quoting` (A7)."""
        await tool(
            state,
            "set_order_state",
            {"order_id": state["order_id"], "expected": "authorized", "new": "quoting"},
        )
        return {
            "requotes": int(state.get("requotes") or 0) + 1,
            "chosen_quote_id": None,
            "awaiting_counter": None,
        }

    async def protocol_error(state: HouseholdState) -> dict[str, Any]:
        """The agent-protocol §6 reaction, by the type of the refused message."""
        refused = _sent_message(state, str(state["body"]["ref_message_id"]))
        refused_type = refused["type"] if refused else "unknown"
        if refused is not None and refused_type in DROP_SELLER_ON_REFUSAL:
            seller = refused["to_agent"]
            update = _exclude(state, seller)
            await trace_decision(state, "protocol_error", f"dropped_{refused_type}")
            if state.get("phase") == NEGOTIATING and state.get("awaiting_counter") == seller:
                return {**update, "decision": "negotiate"}
            return {**update, "decision": "ignore"}
        if refused is not None and refused_type in END_RUN_ON_REFUSAL:
            await trace_decision(state, "protocol_error", f"end_{refused_type}")
            return {
                "decision": "finish",
                "phase": DONE,
                "finish": {"status": "error", "reason": "protocol_error"},
            }
        await trace_decision(state, "protocol_error", f"ignored_{refused_type}")
        return {"decision": "ignore"}

    async def track_order(state: HouseholdState) -> dict[str, Any]:
        sf = state.get("shortfall") or {}
        if state["message_type"] == "order_confirm" and sf.get("status") == SF_DECIDED:
            return {"decision": "shortfall_result"}
        delivered = state["message_type"] == "order_status" and (
            state["body"]["state"] == "delivered"
        )
        return {"decision": "delivered" if delivered else "wait"}

    async def record_delivery(state: HouseholdState) -> dict[str, Any]:
        await tool(state, "record_delivery", {"order_id": state["order_id"]})
        return {}

    # The shortfall (agent-protocol §10) ------------------------------------------------

    async def handle_shortfall(state: HouseholdState) -> dict[str, Any]:
        body = state["body"]
        shortfall_id = str(body["shortfall_id"])
        conversation = str(ids.shortfall_conversation_id(UUID(shortfall_id)))
        notice = {
            "order_id": state["order_id"],
            "shortfall_id": shortfall_id,
            "lines": list(body["lines"]),
            "reply_by": body["reply_by"],
        }
        await tool(
            state,
            "open_shortfall",
            {
                "shortfall_id": shortfall_id,
                "order_id": state["order_id"],
                "seller": state["from_agent"],
                "lines": notice["lines"],
                "due_at": notice["reply_by"],
            },
            conversation,
        )
        relay: dict[str, str] = {}
        if load_whatsapp_members is not None:
            relay = dict(await load_whatsapp_members(state["household_id"]))
        out = list(state.get("outgoing") or [])
        for agent_id in sorted(relay):
            _add(state, out, agent_id, "shortfall_relay", notice, conversation_id=conversation)
        timer = {
            "kind": "shortfall_expiry",
            "due_at": notice["reply_by"],
            "ref": {"shortfall_id": shortfall_id, "order_id": state["order_id"]},
        }
        return {
            "shortfall": {
                "id": shortfall_id,
                "seller": state["from_agent"],
                "lines": notice["lines"],
                "picks": {},
                "status": SF_PENDING,
                "users": [relay[a] for a in sorted(relay)],
                "refused": [],
            },
            "outgoing": out,
            "timers": [*(state.get("timers") or []), timer],
        }

    async def wait_shortfall(state: HouseholdState) -> dict[str, Any]:
        """The thread waits here until `shortfall_closed` arrives (§10)."""
        return {}

    async def apply_shortfall(state: HouseholdState) -> dict[str, Any]:
        sf = dict(state["shortfall"] or {})
        conversation = str(ids.shortfall_conversation_id(UUID(sf["id"])))
        row = await tool(state, "get_shortfall", {"shortfall_id": sf["id"]}, conversation)
        picks = row.get("picks") or {}
        # Lines without a pick are dropped (§10 "Closing early").
        given = {
            str(line["line"]): str(picks.get(str(line["line"])) or DROP)
            for line in row.get("lines") or sf["lines"]
        }
        try:
            result = await tool(
                state,
                "authorize_amendment",
                {"order_id": state["order_id"], "shortfall_id": sf["id"], "picks": given},
                conversation,
            )
        except ToolRefused as refused:
            # Fails closed: no decision is sent, so the order is not amended.
            await trace_decision(state, "apply_shortfall", f"amendment_refused_{refused.reason}")
            return {"shortfall": {**sf, "status": SF_REFUSED}}
        final = {str(k): str(v) for k, v in (result.get("picks") or {}).items()}
        decision = {"order_id": state["order_id"], "shortfall_id": sf["id"], "picks": final}
        out = list(state.get("outgoing") or [])
        _add(state, out, sf["seller"], "shortfall_decision", decision, conversation_id=conversation)
        sf.update(
            status=SF_DECIDED,
            picks=final,
            refused=[int(line) for line in result.get("refused") or []],
        )
        return {"shortfall": sf, "outgoing": out}

    async def shortfall_result(state: HouseholdState) -> dict[str, Any]:
        """The seller's `order_confirm` after the decision: tell every relayed member."""
        body = state["body"]
        sf = dict(state["shortfall"] or {})
        failed = body.get("state") == "failed"
        text = _shortfall_text(state["order_id"], sf, failed)
        out = list(state.get("outgoing") or [])
        for user_id in sf.get("users") or []:
            notice = {
                "user_id": user_id,
                "template": SHORTFALL_RESULT_TEMPLATE,
                "params": {"text": text},
                "buttons": [],
            }
            _add(state, out, WHATSAPP, "whatsapp_out", notice)
        sf.update(
            status=SF_APPLIED,
            amended={
                "lines": list(body.get("lines") or []),
                "total_minor": body.get("total_minor"),
                "currency": body.get("currency"),
            },
        )
        update: dict[str, Any] = {"shortfall": sf, "outgoing": out}
        if failed:
            update.update(phase=DONE, finish={"status": "error", "reason": NOTHING_TO_DELIVER})
        return update

    async def finish(state: HouseholdState) -> dict[str, Any]:
        out = list(state.get("outgoing") or [])
        offer = _chosen(state)
        if state.get("user_id"):
            count = len([x for x in offer["lines"] if x.get("offered_sku")])
            total, currency = offer["total_minor"], offer["currency"]
            amended = (state.get("shortfall") or {}).get("amended")
            if amended:
                # The order the shortfall left (§10): the final lines and total.
                count, total = len(amended["lines"]), amended["total_minor"]
                currency = amended["currency"] or currency
            text = (
                f"Your order {state['order_id'][:8]} was delivered: "
                f"{count} items, {total} {currency} minor units."
            )
            body = {
                "user_id": state["user_id"],
                "template": SUMMARY_TEMPLATE,
                "params": {"text": text},
                "buttons": [],
            }
            _add(state, out, WHATSAPP, "whatsapp_out", body)
        return {"phase": DONE, "outgoing": out, "finish": {"status": "completed"}}

    async def send(state: HouseholdState) -> dict[str, Any]:
        """File exactly the checkpointed messages, timers and outcome under the inbound id."""
        entry = {
            "messages": list(state.get("outgoing") or []),
            "timers": list(state.get("timers") or []),
            "finish": state.get("finish"),
        }
        return {"sent": {**(state.get("sent") or {}), state["message_id"]: entry}}

    # Routing ----------------------------------------------------------------------------

    def route_message(state: HouseholdState) -> str:
        phase = state.get("phase") or NEW
        t = state["message_type"]
        if t in START_KIND:
            return "start_order" if phase == NEW else "ignore"
        if t == "constraints":
            known = state["from_agent"] in (state.get("members") or {})
            return "record_constraints" if phase == CONSTRAINTS and known else "ignore"
        if t == "offer":
            if state["from_agent"] in (state.get("excluded_sellers") or []):
                return "ignore"
            if phase == QUOTING:
                return "record_offer"
            if phase == NEGOTIATING and state.get("awaiting_counter") == state["from_agent"]:
                return "record_offer"
            return "ignore"
        if t == "quote_round_closed":
            fits = phase == QUOTING and state["body"].get("round") == "quote"
            return "negotiate" if fits else "ignore"
        if t == "approval_decision":
            return "approval_decided" if phase == AWAITING_APPROVAL else "ignore"
        if t in ("order_confirm", "delivery_booked", "order_status"):
            return "track_order" if phase == PLACING else "ignore"
        if t == "shortfall_notice":
            # One shortfall per order (§10), and only once the order is placed.
            return (
                "handle_shortfall" if phase == PLACING and not state.get("shortfall") else "ignore"
            )
        if t == "shortfall_closed":
            sf = state.get("shortfall") or {}
            fits = (
                phase == PLACING
                and sf.get("status") == SF_PENDING
                and sf.get("id") == str(state["body"].get("shortfall_id"))
            )
            return "apply_shortfall" if fits else "ignore"
        if t in ("reject", "protocol_error"):
            if phase in (NEW, CONSTRAINTS, DONE):
                return "ignore"
            return "seller_rejected" if t == "reject" else "protocol_error"
        raise UnsupportedMessage(f"the household graph does not handle {t!r}")

    def after_constraints(state: HouseholdState) -> str:
        have = set(state.get("constraints") or {})
        return "build_list" if have >= set(state.get("members") or {}) else "send"

    def after_offer(state: HouseholdState) -> str:
        return "negotiate" if state.get("phase") == NEGOTIATING else "send"

    def after_negotiate(state: HouseholdState) -> str:
        return {"counter": "send", "choose": "choose_offer"}.get(
            state.get("decision") or "", "no_offers"
        )

    def after_choose(state: HouseholdState) -> str:
        return {"authorize": "authorize", "request_approval": "request_approval"}.get(
            state.get("decision") or "", "no_offers"
        )

    def after_approval(state: HouseholdState) -> str:
        return "authorize" if state.get("decision") == "authorize" else "send"

    def after_rejected(state: HouseholdState) -> str:
        return {
            "requote": "requote",
            "next_offer": "choose_offer",
            "no_offers": "no_offers",
        }.get(state.get("decision") or "", "send")

    def after_protocol_error(state: HouseholdState) -> str:
        return "negotiate" if state.get("decision") == "negotiate" else "send"

    def after_track(state: HouseholdState) -> str:
        if state.get("decision") == "shortfall_result":
            return "shortfall_result"
        if state.get("decision") != "delivered":
            return "send"
        return "record_delivery" if state.get("kind") == "grocery" else "finish"

    g = StateGraph(HouseholdState)
    nodes: dict[str, Callable[[HouseholdState], Awaitable[dict[str, Any]]]] = {
        "ignore": ignore,
        "start_order": start_order,
        "collect_constraints": collect_constraints,
        "record_constraints": record_constraints,
        "build_list": build_list,
        "request_quotes": request_quotes,
        "record_offer": record_offer,
        "negotiate": negotiate,
        "no_offers": no_offers,
        "choose_offer": choose_offer,
        "request_approval": request_approval,
        "approval_decided": approval_decided,
        "authorize": authorize,
        "track_order": track_order,
        "seller_rejected": seller_rejected,
        "requote": requote,
        "protocol_error": protocol_error,
        "record_delivery": record_delivery,
        "finish": finish,
        "handle_shortfall": handle_shortfall,
        "wait_shortfall": wait_shortfall,
        "apply_shortfall": apply_shortfall,
        "shortfall_result": shortfall_result,
        "send": send,
    }
    for name, fn in nodes.items():
        g.add_node(name, fn)
    entries = [
        "start_order",
        "ignore",
        "record_constraints",
        "record_offer",
        "negotiate",
        "approval_decided",
        "track_order",
        "seller_rejected",
        "protocol_error",
        "handle_shortfall",
        "apply_shortfall",
    ]
    g.add_conditional_edges(START, route_message, {n: n for n in entries})
    g.add_edge("ignore", "send")
    g.add_edge("start_order", "collect_constraints")
    g.add_edge("collect_constraints", "send")
    g.add_conditional_edges("record_constraints", after_constraints, ["build_list", "send"])
    g.add_edge("build_list", "request_quotes")
    g.add_edge("request_quotes", "send")
    g.add_conditional_edges("record_offer", after_offer, ["negotiate", "send"])
    g.add_conditional_edges("negotiate", after_negotiate, ["send", "choose_offer", "no_offers"])
    g.add_conditional_edges(
        "choose_offer", after_choose, ["authorize", "request_approval", "no_offers"]
    )
    g.add_edge("no_offers", "send")
    g.add_edge("request_approval", "send")
    g.add_conditional_edges("approval_decided", after_approval, ["authorize", "send"])
    g.add_edge("authorize", "send")
    g.add_conditional_edges(
        "track_order", after_track, ["record_delivery", "finish", "shortfall_result", "send"]
    )
    g.add_edge("handle_shortfall", "wait_shortfall")
    g.add_edge("wait_shortfall", "send")
    g.add_edge("apply_shortfall", "send")
    g.add_edge("shortfall_result", "send")
    g.add_conditional_edges(
        "seller_rejected", after_rejected, ["requote", "choose_offer", "no_offers", "send"]
    )
    g.add_edge("requote", "request_quotes")
    g.add_conditional_edges("protocol_error", after_protocol_error, ["negotiate", "send"])
    g.add_edge("record_delivery", "finish")
    g.add_edge("finish", "send")
    g.add_edge("send", END)
    return g


def _chosen(state: HouseholdState) -> dict[str, Any]:
    chosen = state.get("chosen_quote_id")
    for offer in (state.get("quotes") or {}).values():
        if offer.get("quote_id") == chosen:
            return offer
    raise LookupError(f"chosen quote {chosen} is not in state")


def _shortfall_text(order_id: str, sf: Mapping[str, Any], failed: bool) -> str:
    """What the shortfall changed, written in code from the notice and the final picks."""
    notice = {str(line["line"]): line for line in sf.get("lines") or []}
    refused = {str(line) for line in sf.get("refused") or []}
    substituted: list[str] = []
    dropped: list[str] = []
    for line, pick in sorted((sf.get("picks") or {}).items(), key=lambda kv: int(kv[0])):
        entry = notice.get(line) or {}
        name = str(entry.get("name") or entry.get("sku") or line)
        if pick == DROP:
            dropped.append(f"{name} (substitute refused)" if line in refused else name)
            continue
        subs = {str(s["sku"]): s for s in entry.get("substitutes") or []}
        sub = str((subs.get(pick) or {}).get("name") or pick)
        substituted.append(f"{sub} for {name}")
    head = f"Order {order_id[:8]}"
    if failed:
        return f"{head}: every item was missing, so nothing will be delivered."
    parts = [f"{head} changed."]
    if substituted:
        parts.append("Substituted: " + "; ".join(substituted) + ".")
    if dropped:
        parts.append("Dropped: " + "; ".join(dropped) + ".")
    return " ".join(parts)


def _seller_of(state: HouseholdState, quote_id: str) -> str:
    for seller, offer in (state.get("quotes") or {}).items():
        if offer.get("quote_id") == quote_id:
            return seller
    raise LookupError(f"quote {quote_id} is not in state")


class HouseholdGraph:
    """The household class handler, `handle(msg, card) -> StepResult` (architecture item 27)."""

    def __init__(
        self,
        *,
        model: ModelClient,
        traces: TraceSink,
        checkpointer: BaseCheckpointSaver,
        load_run: LoadRun,
        load_members: LoadMembers,
        find_sellers: FindSellers,
        call_tool: CallTool = mcp_client.call_tool,
        issue_token: IssueToken = _mock_token,
        clock: Clock = _now,
        sleep: Sleep = asyncio.sleep,
        offer_ttl_seconds: int = OFFER_TTL_SECONDS,
        load_whatsapp_members: LoadMembers | None = None,
    ) -> None:
        self._load_run = load_run
        self._offer_ttl_seconds = offer_ttl_seconds
        self.builder = build_graph(
            model,
            traces,
            call_tool,
            issue_token,
            load_members,
            find_sellers,
            clock,
            sleep,
            load_whatsapp_members,
        )
        self.graph = self.builder.compile(checkpointer=checkpointer)

    async def __call__(self, msg: Inbound, card: AgentCard) -> StepResult:
        row = msg.row
        if row.type not in HANDLED_TYPES:
            raise UnsupportedMessage(f"the household graph does not handle {row.type!r}")
        config = {"configurable": {"thread_id": ids.thread_id(card.agent_id, row.run_id)}}
        key = str(row.message_id)

        snapshot = await self.graph.aget_state(config)
        saved: Mapping[str, Any] = snapshot.values or {}
        if saved and saved.get("state_version") != STATE_VERSION:
            return StepResult(finish=RunFinish("error", "state_version"))
        if snapshot.next:
            # An invocation stopped after a checkpoint: finish it from there, so its
            # decisions are not made again (agent-protocol §7 replay safety).
            saved = await self.graph.ainvoke(None, config)
        done = (saved.get("sent") or {}).get(key)
        if done is not None:
            return self._result(card, done)

        inbound: dict[str, Any] = {
            "state_version": STATE_VERSION,
            "run_id": str(row.run_id),
            "agent_id": card.agent_id,
            "message_id": key,
            "message_type": row.type,
            "from_agent": row.from_agent,
            "conversation_id": str(row.conversation_id) if row.conversation_id else None,
            "body": msg.body.model_dump(mode="json"),
            "decision": None,
            "outgoing": [],
            "timers": [],
            "finish": None,
        }
        if not saved:
            run = await self._load_run(row.run_id)
            inbound.update(
                household_id=_household_id(card),
                user_id=run.user_id,
                region=card.region,
                zone_id=card.zone_ids[0],
                currency=card.currency or "",
                phase=NEW,
                order_id=None,
                kind=None,
                quotes={},
                chosen_quote_id=None,
                approval_id=None,
                counters=0,
                excluded_sellers=[],
                requotes=0,
                approval_minor=None,
                approved_minor=None,
                approver_user_id=None,
                accepts=0,
                accept_delay_rounds=_int(run.overrides.get("STUB_ACCEPT_DELAY_ROUNDS"), 0),
                offer_ttl_seconds=_int(
                    run.overrides.get("OFFER_TTL_SECONDS"), self._offer_ttl_seconds
                ),
                shortfall=None,
                sent={},
            )
        state = await self.graph.ainvoke(inbound, config)
        done = (state.get("sent") or {}).get(key)
        if done is None:
            raise RuntimeError(f"the household invocation for {key} did not reach send")
        return self._result(card, done)

    @staticmethod
    def _result(card: AgentCard, entry: Mapping[str, Any]) -> StepResult:
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
            for m in entry.get("messages") or []
        ]
        timers = [
            TimerCreate(
                kind=t["kind"],
                # An absolute `due_at` (the shortfall's `reply_by`), else seconds from now.
                due_at=datetime.fromisoformat(t["due_at"])
                if t.get("due_at")
                else timedelta(seconds=t["seconds"]),
                ref=t["ref"],
            )
            for t in entry.get("timers") or []
        ]
        fin = entry.get("finish")
        return StepResult(
            messages=messages,
            timers=timers,
            finish=RunFinish(fin["status"], fin.get("reason")) if fin else None,
        )


# The host's handler. Built on first use, after the host has opened the pool.

_RUN_SQL = "SELECT trigger->>'user_id', trigger->'overrides' FROM runs WHERE run_id = %s"
_MEMBERS_SQL = """
SELECT c.agent_id, m.user_id::text
FROM household_members m
JOIN agent_cards c ON c.class = 'consumer' AND c.enabled AND c.owner_ref = 'user:' || m.user_id
WHERE m.household_id = %s
ORDER BY c.agent_id
"""
# The members a shortfall is relayed to: those with a WhatsApp identity (§10).
_WHATSAPP_MEMBERS_SQL = """
SELECT c.agent_id, m.user_id::text
FROM household_members m
JOIN agent_cards c ON c.class = 'consumer' AND c.enabled AND c.owner_ref = 'user:' || m.user_id
WHERE m.household_id = %s
  AND EXISTS (SELECT 1 FROM channel_identities ci WHERE ci.user_id = m.user_id)
ORDER BY c.agent_id
"""
_default: HouseholdGraph | None = None


def _build_default() -> HouseholdGraph:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from services.agents.common.model import PgRunState
    from services.common import db, directory
    from services.common.config import get_settings
    from services.common.trace import TraceStore

    pool = db.get_pool()
    traces = TraceStore(pool)

    async def load_run(run_id: UUID) -> RunInfo:
        async with pool.connection() as conn:
            cur = await conn.execute(_RUN_SQL, (run_id,))
            row = await cur.fetchone()
        if row is None:
            raise LookupError(f"run {run_id} does not exist")
        overrides = row[1] if isinstance(row[1], dict) else {}
        return RunInfo(user_id=row[0], overrides=overrides)

    async def load_members(household_id: str) -> Members:
        async with pool.connection() as conn:
            cur = await conn.execute(_MEMBERS_SQL, (household_id,))
            return {agent_id: user_id for agent_id, user_id in await cur.fetchall()}

    async def load_whatsapp_members(household_id: str) -> Members:
        async with pool.connection() as conn:
            cur = await conn.execute(_WHATSAPP_MEMBERS_SQL, (household_id,))
            return {agent_id: user_id for agent_id, user_id in await cur.fetchall()}

    async def find_sellers(cls: str, region: str, zone_id: str, currency: str) -> list[str]:
        async with pool.connection() as conn:
            cards = await directory.find(conn, cls, region, zone_id, currency)
        return [c.agent_id for c in cards]

    return HouseholdGraph(
        model=ModelClient(traces, PgRunState(pool)),
        traces=traces,
        checkpointer=AsyncPostgresSaver(pool),  # type: ignore[arg-type]
        load_run=load_run,
        load_members=load_members,
        find_sellers=find_sellers,
        offer_ttl_seconds=get_settings().offer_ttl_seconds,
        load_whatsapp_members=load_whatsapp_members,
    )


async def handle(msg: Inbound, card: AgentCard) -> StepResult:
    """The household class handler registered in `services.agents.host.HANDLERS`."""
    global _default
    if _default is None:
        _default = _build_default()
    return await _default(msg, card)
