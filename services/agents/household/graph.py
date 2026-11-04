"""The household graph: the weekly order main path (architecture item 29, §8; agent-protocol
§7 replay safety).

A household agent is a LangGraph graph checkpointed under
`thread_id = <agent_id>:<run_id>`. Each inbound message is one invocation of the thread,
routed by its type and the order's phase:

*   `start_weekly_order` / `start_dinner` → `start_order` (the deterministic
    `ids.order_id(run_id)` is in the checkpoint before `create_order` uses it; then
    `get_household_rules`, snapshotted in state, agents §10) → `collect_constraints` (one
    `constraints_request` per member's consumer agent).
*   `constraints` → `record_constraints` (allergens, diets and dislikes); once every
    member has answered, `build_list` (the model's list, then in code: item names, the
    pantry skip on weekly grocery orders and the AG5 dislike rule, agents §6, pantry §5) →
    `request_quotes` (`directory.find`, one `quote_request` per seller, the 30 s
    `quote_round` timer, state `quoting`). Dislikes and pantry data are never sent to
    sellers. When the pantry skip emptied the list, `nothing_needed` instead: the draft
    order is cancelled and the run ends `completed`, reason `nothing_needed`.
*   `offer` while quoting, or the reply to our counter → `record_offer`. Any other offer is
    late: ignored with a `decision` trace `late_offer` (agent-protocol §7 arrival table).
*   `quote_round_closed` → `negotiate` (state `negotiating`; code only: at most one
    counter, under the six rules of agents §7) → `choose_offer` (the first offer of
    `compare_offers`, picked in code, and one explanation call under a 15 s timeout with a
    template fallback, agents §11) → `request_approval` when the total is over the
    remaining weekly budget, else `authorize` (`authorize_spend`, then `accept`).
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
    `whatsapp_out`: the delivered line, then the explanation, agents §11). A household
    dinner with an unavoidable dislike also sends the approver the take-turns offer, and
    `advance_turn` runs when take-turns was on (agents §10).
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
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from services.agents.common.model import (
    InvalidOutput,
    ModelClient,
    ModelUnavailable,
    TokenBudgetExceeded,
    TraceSink,
    check_rationale,
)
from services.agents.household import stubs
from services.agents.household.prompts import BUILD_LIST_PROMPT, EXPLAIN_PROMPT, NO_CODES
from services.common import dislikes, ids, mcp_client, pantry
from services.common.consumer import (
    Inbound,
    Outgoing,
    RunFinish,
    StepResult,
    TimerCreate,
)
from services.common.directory import AgentCard
from services.common.mcp_client import ToolRefused
from services.common.money import Money
from services.common.money import format as money_format
from services.common.protocol.schemas import RequestItem
from services.common.run_context import RunContext
from services.safety.scope import unscoped

__all__ = [
    "EXPLAIN_TIMEOUT_SECONDS",
    "HANDLED_TYPES",
    "HOUSEHOLD_MAX_COUNTERS",
    "MAX_COUNTER_ROUNDS",
    "OFFER_TTL_SECONDS",
    "QUOTE_ROUND_SECONDS",
    "STATE_VERSION",
    "HouseholdGraph",
    "HouseholdState",
    "Members",
    "RunInfo",
    "DislikeOutcome",
    "UnsupportedMessage",
    "apply_dislike_rule",
    "build_graph",
    "compare_offers",
    "explanation_template",
    "handle",
    "item_name_ok",
    "seller_label",
    "strip_rationale",
    "turn_holder",
]

log = logging.getLogger(__name__)

STATE_VERSION = 1
QUOTE_ROUND_SECONDS = 30
MAX_COUNTER_ROUNDS = 2  # the protocol's limit (agent-protocol §5)
HOUSEHOLD_MAX_COUNTERS = 1  # the household counters at most once per run (agents AG-A3)
EXPLAIN_TIMEOUT_SECONDS = 15  # the explanation call in `choose_offer` (agents §11)
LOCALE = "en"  # money in WhatsApp text, as the household MCP server formats it
HOUSEHOLD_SCOPE = "household"
SOLO_SCOPE = "solo"
ITEM_NAME_MAX_CHARS = 60  # agents §12, "Item names"
# Characters an item name may not contain (agents §12): markup and template braces.
ITEM_NAME_FORBIDDEN = frozenset("<>{}")
OFFER_TTL_SECONDS = 600  # agent-protocol §13; the run override `OFFER_TTL_SECONDS` wins
MAX_REQUOTES = 1
# The delivery window asked for in `quote_request`, relative to the request.
WINDOW_START = timedelta(hours=2)
WINDOW_END = timedelta(hours=4)
# A solo (instant) order's window (agents §8): now+30 min to now+90 min.
SOLO_WINDOW = (timedelta(minutes=30), timedelta(minutes=90))
HOUSEHOLD_SERVER = "household"
WHATSAPP = "channel:whatsapp"
SELLER_CLASS = {"grocery": "grocery", "dinner": "restaurant"}
START_KIND = {"start_weekly_order": "grocery", "start_dinner": "dinner"}
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
# Pantry §5, §6: the decision, the text and the run's reason when everything is at home.
NOTHING_NEEDED = "nothing_needed"
APPROVAL_CANCELLED_TEMPLATE = "approval_cancelled"
SHORTFALL_RESULT_TEMPLATE = "shortfall_result"
# The take-turns message from `finish` and its one button (agents §10).
TAKE_TURNS_OFFER_TEMPLATE = "take_turns_offer"
TAKE_TURNS_OFFER_OFF_TEMPLATE = "take_turns_offer_off"
TAKE_TURNS_TITLE = "Take turns"
STOP_TAKING_TURNS_TITLE = "Stop taking turns"
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
    # consumer agent id -> allergens, diets and dislikes (an old entry has no dislikes)
    constraints: dict[str, dict[str, list[str]]]
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
    negotiate_hold_seconds: int  # the run override `STUB_NEGOTIATE_HOLD_SECONDS`
    offer_ttl_seconds: int
    # The order's shortfall (agent-protocol §10): id, seller, lines, picks, status, the
    # members it was relayed to, the refused lines and the amended order.
    shortfall: dict[str, Any] | None
    # Agents §17. Each is read with its default, so `STATE_VERSION` stays 1.
    scope: str  # "household" (default) or "solo"
    take_turns: bool  # `get_household_rules`, snapshotted by `start_order`
    turn_index: int
    household_allergens: list[str]  # every member's allergens, from Postgres
    household_diets: list[str]  # every member's diets, from Postgres (not on a solo order)
    turn_holder: str | None  # the user id whose dislikes come first this run
    counter: dict[str, Any] | None  # {seller, from_minor, to_minor} of our one counter
    explanation: str | None  # the latest explanation sentence (choose_offer)
    dropped_items: list[str]
    unavoidable_dislikes: list[str]
    # Pantry §5 (agents §17, PT-A10): read with defaults, so `STATE_VERSION` stays 1.
    pantry_skipped: list[str]  # list names the pantry skip dropped (weekly orders)
    pantry_as_of: str | None  # `get_pantry`'s `as_of` for that skip
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
Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]
# The vendor label text of a seller card in a run (adapters §6): `(run_id, agent_id)`.
LabelSeller = Callable[[UUID, str], Awaitable[str]]
LoadVocabulary = Callable[[], pantry.Vocabulary]


async def _agent_id_label(run_id: UUID, agent_id: str) -> str:
    # Without a label source the seller is named by its agent id, with no badge.
    return agent_id


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


def _covered(offer: Mapping[str, Any]) -> int:
    return sum(1 for line in offer.get("lines") or [] if line.get("offered_sku"))


def _substitutions(offer: Mapping[str, Any]) -> int:
    return sum(1 for line in offer.get("lines") or [] if line.get("substituted_for"))


def compare_offers(offers: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The offers best first (agents §7): most lines with an `offered_sku`, then the lowest
    `total_minor`, then the fewest substitutions, then the seller id. A pure function."""
    return sorted(
        (dict(o) for o in offers),
        key=lambda o: (-_covered(o), o["total_minor"], _substitutions(o), o["seller"]),
    )


def _money(amount_minor: int | None, currency: str) -> str | None:
    if amount_minor is None:
        return None
    return money_format(Money(int(amount_minor), currency), LOCALE)


def explanation_template(facts: Mapping[str, Any]) -> str:
    """The template sentence used when the model's explanation can't be (agents §11)."""
    text = (
        f"Chose {facts['seller']}: {facts['items_covered']} of {facts['items_requested']} "
        f"items for {facts['total']}"
    )
    if facts.get("counter_from") is not None:
        text += f", after a counter from {facts['counter_from']}"
    return text + "."


def _summary(delivered: str, state: Mapping[str, Any]) -> str:
    """The order summary (agents §11), one line per part, joined into one parameter."""
    parts = [delivered]
    if state.get("explanation"):
        parts.append(str(state["explanation"]))
    dropped = list(state.get("dropped_items") or [])
    if dropped:
        parts.append(f"Left out because of a dislike: {', '.join(dropped)}.")
    kept = list(state.get("unavoidable_dislikes") or [])
    if kept:
        parts.append(
            f"Included, though someone dislikes it: {', '.join(kept)}; no other option fit."
        )
    skipped = list(state.get("pantry_skipped") or [])
    if skipped:
        parts.append(f"Skipped, already at home: {', '.join(skipped)}.")
    # WhatsApp template parameters can't hold newlines, so the parts are joined by spaces.
    return " ".join(parts)


def _codes(constraints: Sequence[Mapping[str, Any]], key: str) -> str:
    """The union of one kind of code across members, for a prompt."""
    values = sorted({str(v) for codes in constraints for v in codes.get(key) or []})
    return ", ".join(values) or NO_CODES


def _dislikes(codes: Mapping[str, Any]) -> list[str]:
    """One member's dislike values; an entry from an old checkpoint has none (agents §6)."""
    return [str(v) for v in codes.get("dislikes") or []]


def item_name_ok(name: str) -> bool:
    """Agents §12, "Item names": not empty after trimming, at most 60 characters, and no
    control character or `<`, `>`, `{`, `}`."""
    trimmed = name.strip()
    if not trimmed or len(trimmed) > ITEM_NAME_MAX_CHARS:
        return False
    return not any(ch in ITEM_NAME_FORBIDDEN or unicodedata.category(ch) == "Cc" for ch in trimmed)


def turn_holder(
    constraints: Mapping[str, Mapping[str, Any]], members: Mapping[str, str], turn_index: int
) -> str | None:
    """The user id whose turn it is (agents §6): among the collected members with at least
    one dislike, sorted by user id, the one at `turn_index` mod their count."""
    users = sorted(
        members[agent]
        for agent, codes in constraints.items()
        if agent in members and _dislikes(codes)
    )
    if not users:
        return None
    return users[turn_index % len(users)]


@dataclass(frozen=True)
class DislikeOutcome:
    """What the AG5 rule did to a list: the items kept, and for each item that matched a
    dislike, whether it was dropped or kept."""

    items: list[dict[str, Any]]
    dropped: list[str]  # item names
    kept: list[tuple[str, list[str]]]  # (item name, the dislike values it matches)


def apply_dislike_rule(
    items: Sequence[Mapping[str, Any]],
    owners: Mapping[str, Sequence[str]],
    removable_owners: frozenset[str],
) -> DislikeOutcome:
    """The AG5 dislike rule. `owners` maps each collected member to their dislikes.

    An item that matches a dislike is dropped when a member in `removable_owners` (the solo
    requester, or the turn holder) has a matching dislike, or when every member in the run
    has one. The last item is never dropped. Every other match is kept and reported.
    """
    kept_items: list[dict[str, Any]] = []
    dropped: list[str] = []
    kept: list[tuple[str, list[str]]] = []
    for index, item in enumerate(items):
        name = str(item["item"])
        matching = {
            owner: [d for d in values if dislikes.matches(name, d)]
            for owner, values in owners.items()
        }
        matching = {owner: values for owner, values in matching.items() if values}
        if not matching:
            kept_items.append(dict(item))
            continue
        removable = bool(removable_owners & set(matching)) or set(matching) >= set(owners)
        # Never the last item: nothing kept so far and nothing left after this one.
        last = not kept_items and index == len(items) - 1
        if removable and not last:
            dropped.append(name)
            continue
        kept_items.append(dict(item))
        kept.append((name, sorted({d for values in matching.values() for d in values})))
    return DislikeOutcome(items=kept_items, dropped=dropped, kept=kept)


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
    load_members: LoadMembers,
    find_sellers: FindSellers,
    clock: Clock = _now,
    sleep: Sleep = asyncio.sleep,
    load_whatsapp_members: LoadMembers | None = None,
    label_seller: LabelSeller = _agent_id_label,
    load_vocabulary: LoadVocabulary = pantry.load_vocabulary,
) -> StateGraph:
    """The household graph's nodes and edges, before `compile(checkpointer=...)`.

    `load_whatsapp_members` gives the members with a WhatsApp identity, the ones a
    shortfall is relayed to; without it nobody is asked (fails closed: every line drops).
    `label_seller` names a seller in member-facing text with its vendor label (adapters
    §6), from the run's pinned modes. `load_vocabulary` gives the pantry vocabulary
    (pantry §2); a failed load means nothing is skipped.
    """

    async def tool(
        state: HouseholdState, name: str, args: dict[str, Any], conversation_id: str | None = None
    ) -> dict[str, Any]:
        if not state.get("user_id"):
            # Fails closed: a household run always names the member who started it.
            raise ValueError("the run names no member to act for")
        # `call_tool_as` obtains the household grant's token (identity-authz I6).
        return await call_tool(HOUSEHOLD_SERVER, name, args, ctx=_ctx(state, conversation_id))

    async def trace_decision(
        state: HouseholdState,
        node: str,
        choice: str,
        explanation: str = "",
        facts: Mapping[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "node": node,
            "choice": choice,
            "explanation": explanation,
            "ref": state["message_id"],
        }
        if facts is not None:
            # The explanation's facts object (agents §15, amended by evals E-A12).
            payload["facts"] = dict(facts)
        await traces.write(
            UUID(state["run_id"]),
            state["agent_id"],
            "decision",
            payload,
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

    async def complete_text(
        state: HouseholdState, node: str, system: str, data: dict[str, Any]
    ) -> str:
        """One model step whose answer is plain text, not JSON (agents §11)."""
        messages = [{"role": "system", "content": system}]
        result = await model.complete(
            _ctx(state), messages, None, node, state={**data, "messages": messages}
        )
        if not isinstance(result.text, str):
            raise InvalidOutput("the model's answer is not text")
        return result.text

    async def explain(state: HouseholdState, facts: dict[str, Any]) -> tuple[str, str]:
        """The explanation sentence and its trace choice: the model's sentence when it
        arrives within `EXPLAIN_TIMEOUT_SECONDS` and passes `check_rationale`, else the
        template."""
        system = EXPLAIN_PROMPT.format(facts=json.dumps(facts, sort_keys=True))
        try:
            async with asyncio.timeout(EXPLAIN_TIMEOUT_SECONDS):
                text = await complete_text(state, "choose_offer", system, {"facts": facts})
        except (TimeoutError, ModelUnavailable, InvalidOutput, TokenBudgetExceeded):
            text = None
        checked = check_rationale(text, [facts]) if text is not None else None
        if checked is None:
            return explanation_template(facts), "explanation_template"
        return checked, "explanation_model"

    async def offer_facts(
        state: HouseholdState, pick: Mapping[str, Any], runner_up: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        """The facts the explanation may use (agents §11). Sellers are named by their
        vendor label (adapters §6)."""
        run_id = UUID(state["run_id"])
        currency = pick["currency"]
        counter = state.get("counter") or {}
        requested = len(state.get("items") or []) or len(pick.get("lines") or [])
        return {
            "seller": await label_seller(run_id, pick["seller"]),
            "total": _money(pick["total_minor"], currency),
            "items_covered": _covered(pick),
            "items_requested": requested,
            "substitutions": _substitutions(pick),
            "runner_up": None
            if runner_up is None
            else await label_seller(run_id, runner_up["seller"]),
            "runner_up_total": None
            if runner_up is None
            else _money(runner_up["total_minor"], currency),
            "remaining": _money(state.get("remaining_minor"), currency),
            "counter_from": _money(counter.get("from_minor"), currency) if counter else None,
            "counter_to": _money(counter.get("to_minor"), currency) if counter else None,
        }

    # Nodes ------------------------------------------------------------------------------

    async def accept_delay(state: HouseholdState) -> None:
        """`STUB_ACCEPT_DELAY_ROUNDS` (agent-protocol P21): stub mode only."""
        if model.mode != "stub":
            return
        seconds = stubs.accept_delay_seconds(state)
        if seconds > 0:
            await sleep(seconds)

    async def negotiate_hold(state: HouseholdState) -> None:
        """`STUB_NEGOTIATE_HOLD_SECONDS` (identity-authz I35): stub mode only."""
        seconds = int(state.get("negotiate_hold_seconds") or 0)
        if model.mode == "stub" and seconds > 0:
            await sleep(seconds)

    async def ignore(state: HouseholdState) -> dict[str, Any]:
        reason = {"offer": "late_offer"}.get(state["message_type"], "ignored")
        await trace_decision(state, "ignore", reason)
        return {}

    async def start_order(state: HouseholdState) -> dict[str, Any]:
        kind = START_KIND[state["message_type"]]
        order_id = state.get("order_id") or str(ids.order_id(UUID(state["run_id"])))
        await tool(state, "create_order", {"order_id": order_id, "kind": kind})
        # Every run snapshots the household's rules (agents §10).
        rules = await tool(state, "get_household_rules", {})
        approver = rules.get("approver_user_id")
        return {
            "order_id": order_id,
            "kind": kind,
            "scope": state["body"].get("scope") or HOUSEHOLD_SCOPE,
            "take_turns": bool(rules.get("take_turns")),
            "turn_index": _int(rules.get("turn_index"), 0),
            "approver_user_id": str(approver) if approver else None,
        }

    async def collect_constraints(state: HouseholdState) -> dict[str, Any]:
        members = dict(await load_members(state["household_id"]))
        if not members:
            raise ValueError(f"household {state['household_id']} has no consumer agents")
        update: dict[str, Any] = {}
        if (state.get("scope") or HOUSEHOLD_SCOPE) == SOLO_SCOPE:
            # A solo order asks only the requester and filters on every member's
            # allergens (agents §6, §8).
            requester = state.get("user_id")
            members = {a: u for a, u in members.items() if requester and u == requester}
            if not members:
                # Fails closed: the requester has no consumer agent in the household.
                raise ValueError(f"the requester has no consumer agent in {state['household_id']}")
            found = await tool(state, "get_household_allergens", {})
            update["household_allergens"] = sorted(str(a) for a in found.get("allergens") or [])
        else:
            # Every member's allergies and diets are read here, not asked for: a member whose
            # consumer agent has no grant answers with nothing (identity-authz I-CUJ 11).
            found = await tool(state, "get_constraints", {"user_id": min(members.values())})
            rows = found.get("members") or []
            update["household_allergens"] = sorted(
                {str(a) for row in rows for a in row.get("allergies") or []}
            )
            update["household_diets"] = sorted(
                {str(d) for row in rows for d in row.get("diets") or []}
            )
        out = list(state.get("outgoing") or [])
        for agent_id, user_id in sorted(members.items()):
            body = {"order_id": state["order_id"], "user_id": user_id}
            _add(state, out, agent_id, "constraints_request", body)
        return {
            **update,
            "members": members,
            "constraints": {},
            "phase": CONSTRAINTS,
            "outgoing": out,
        }

    async def record_constraints(state: HouseholdState) -> dict[str, Any]:
        body = state["body"]
        codes = {
            "allergens": list(body["allergens"]),
            "diets": list(body["diets"]),
            "dislikes": [str(d) for d in body.get("dislikes") or []],
        }
        return {"constraints": {**(state.get("constraints") or {}), state["from_agent"]: codes}}

    async def build_list(state: HouseholdState) -> dict[str, Any]:
        # Pantry §5, §9: either failure means nothing is skipped (`pantry_unavailable`).
        vocab: pantry.Vocabulary | None
        try:
            vocab = load_vocabulary()
        except Exception:
            log.warning("pantry vocabulary failed to load", exc_info=True)
            vocab = None
        found: dict[str, Any] | None
        try:
            found = await tool(state, "get_pantry", {})
        except Exception:
            log.warning("get_pantry failed", exc_info=True)
            found = None
        constraints = dict(state.get("constraints") or {})
        members = dict(state.get("members") or {})
        solo = (state.get("scope") or HOUSEHOLD_SCOPE) == SOLO_SCOPE
        codes = list(constraints.values())
        # What the members answered, plus every member's allergens (agents §6) and, unless
        # solo, diets (identity-authz I-CUJ 11).
        household = {
            "allergens": list(state.get("household_allergens") or []),
            "diets": list(state.get("household_diets") or []),
        }
        # Dislike values only, never tied to a member.
        system = BUILD_LIST_PROMPT.format(
            kind=state["kind"],
            people=1 if solo else (len(members) or 1),
            diets=_codes([*codes, household], "diets"),
            allergens=_codes([*codes, household], "allergens"),
            dislikes=_codes(codes, "dislikes"),
            pantry_names=", ".join(sorted(vocab.names)) if vocab is not None else NO_CODES,
        )
        # Only code skips (pantry PG3): the model sees no pantry data.
        answer = await complete(state, "build_list", system, {"kind": state["kind"]})
        try:
            items = [RequestItem.model_validate(i) for i in answer.get("items") or []]
        except ValidationError:
            raise InvalidOutput("build_list returned an invalid item") from None
        named = []
        for i in items:
            if item_name_ok(i.item):
                named.append(i)
            else:
                await trace_decision(state, "build_list", "item_rejected")
        if not named:
            raise InvalidOutput("build_list returned no items")
        items = named
        skipped: list[str] = []
        as_of: str | None = None
        weekly = (
            state.get("kind") == "grocery"
            and (state.get("scope") or HOUSEHOLD_SCOPE) == HOUSEHOLD_SCOPE
        )
        if weekly:
            # Pantry §5: code drops trusted items, one decision row per list item.
            estimates = None if found is None else list(found.get("estimates") or [])
            enabled = found is not None and found.get("enabled") is True
            as_of = None if found is None else found.get("as_of")
            items = []
            for i in named:
                choice, _, facts = pantry.decide(
                    vocab, estimates, i.item, as_of=as_of, enabled=enabled
                )
                await trace_decision(state, "build_list", choice, f"item={i.item}", facts)
                if choice == "pantry_skipped":
                    skipped.append(i.item)
                else:
                    items.append(i)
            if not items:
                # Everything is at home (PT-OD5): no quote request, the dislike rule is moot.
                return {
                    "items": [],
                    "pantry_skipped": skipped,
                    "pantry_as_of": as_of,
                    "decision": NOTHING_NEEDED,
                }
        # The AG5 dislike rule, with members named by user id (agents §6).
        owners = {
            members.get(agent, agent): _dislikes(c) for agent, c in sorted(constraints.items())
        }
        holder = None
        if state.get("take_turns") and state.get("kind") == "dinner" and not solo:
            holder = turn_holder(constraints, members, _int(state.get("turn_index"), 0))
        removable: set[str] = set()
        if solo and state.get("user_id"):
            removable.add(str(state["user_id"]))
        if holder is not None:
            removable.add(holder)
        outcome = apply_dislike_rule(
            [i.model_dump(mode="json") for i in items], owners, frozenset(removable)
        )
        for name in outcome.dropped:
            await trace_decision(state, "build_list", "dislike_dropped", f"item={name}")
        unavoidable: set[str] = set()
        for name, values in outcome.kept:
            unavoidable.update(values)
            text = f"item={name} dislikes={', '.join(values)}"
            await trace_decision(state, "build_list", "dislike_kept", text)
        return {
            "items": outcome.items,
            "dropped_items": outcome.dropped,
            "unavoidable_dislikes": sorted(unavoidable),
            "turn_holder": holder,
            "pantry_skipped": skipped,
            "pantry_as_of": as_of,
        }

    async def nothing_needed(state: HouseholdState) -> dict[str, Any]:
        """Pantry §5 step 6: the skip emptied the list. The draft order is cancelled, no
        `quote_request` is sent, and the run ends `completed`, reason `nothing_needed`."""
        try:
            await tool(
                state,
                "set_order_state",
                {"order_id": state["order_id"], "expected": "draft", "new": "cancelled"},
            )
        except ToolRefused as refused:
            return {"phase": DONE, "finish": {"status": "error", "reason": refused.reason}}
        out = list(state.get("outgoing") or [])
        if state.get("user_id"):
            body = {
                "user_id": state["user_id"],
                "template": NOTHING_NEEDED,
                "params": {"items": ", ".join(state.get("pantry_skipped") or [])},
                "buttons": [],
                "ref": {"kind": "order", "id": state["order_id"]},
            }
            _add(state, out, WHATSAPP, "whatsapp_out", body)
        return {
            "phase": DONE,
            "outgoing": out,
            "finish": {"status": "completed", "reason": NOTHING_NEEDED},
        }

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
        allergens.update(state.get("household_allergens") or [])
        diets.update(state.get("household_diets") or [])
        now = clock()
        start, end = WINDOW_START, WINDOW_END
        if (state.get("scope") or HOUSEHOLD_SCOPE) == SOLO_SCOPE:
            start, end = SOLO_WINDOW
        body = {
            "order_id": state["order_id"],
            "kind": state["kind"],
            "items": state["items"],
            "constraints": {"allergens": sorted(allergens), "diets": sorted(diets)},
            "currency": state["currency"],
            "zone_id": state["zone_id"],
            "window": {
                "start": (now + start).isoformat(),
                "end": (now + end).isoformat(),
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
            await negotiate_hold(state)
        offers = valid_offers(state, clock())
        if not offers:
            return {**update, "decision": "no_offers"}
        budget = await tool(state, "get_budget", {})
        remaining = int(budget["remaining"]["amount_minor"])
        counters = int(state.get("counters") or 0)
        update["remaining_minor"] = remaining
        # Code only (agents §7): one counter, at the remaining budget, to the best offer.
        ranked = compare_offers(offers)
        best = ranked[0]
        runner_up = ranked[1] if len(ranked) > 1 else None
        if (
            (state.get("scope") or HOUSEHOLD_SCOPE) == HOUSEHOLD_SCOPE
            and counters < HOUSEHOLD_MAX_COUNTERS
            and not best.get("final")
            and remaining > 0
            and best["total_minor"] > remaining
            and runner_up is not None
            and _covered(runner_up) == _covered(best)
        ):
            seller = best["seller"]
            body = {
                "order_id": state["order_id"],
                "quote_id": best["quote_id"],
                "round": best["round"],
                "lines": best["lines"],
                "total_minor": remaining,
                "currency": best["currency"],
            }
            out = list(state.get("outgoing") or [])
            conversation = (state.get("conversations") or {}).get(seller)
            _add(state, out, seller, "counter_offer", body, conversation_id=conversation)
            text = f"best={best['total_minor']} remaining={remaining} counter={remaining}"
            await trace_decision(state, "negotiate", "counter", text)
            return {
                **update,
                "decision": "counter",
                "counters": counters + 1,
                "counter": {
                    "seller": seller,
                    "from_minor": best["total_minor"],
                    "to_minor": remaining,
                },
                "awaiting_counter": seller,
                "outgoing": out,
            }
        await trace_decision(state, "negotiate", "choose")
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
        # Code picks (agents §7); the model only writes the explanation (§11).
        ranked = compare_offers(valid_offers(state, clock()))
        if not ranked:
            return {"decision": "no_offers"}
        pick = ranked[0]
        runner_up = ranked[1] if len(ranked) > 1 else None
        remaining = state.get("remaining_minor")
        over = remaining is None or pick["total_minor"] > remaining
        approved = state.get("approved_minor")
        if over and approved is not None and pick["total_minor"] <= approved:
            # A re-quoted offer at or below the approved total uses that approval (§7).
            over = False
        decision = "request_approval" if over else "authorize"
        await trace_decision(state, "choose_offer", "request_approval" if over else "chosen")
        facts = await offer_facts(state, pick, runner_up)
        explanation, choice = await explain(state, facts)
        await trace_decision(state, "choose_offer", choice, explanation, facts)
        return {
            "chosen_quote_id": pick["quote_id"],
            "decision": decision,
            "explanation": explanation,
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
            if refused.reason == "needs_approval":
                # Agents §7, AG10: ask the approver unless an approval already covers the
                # total; a refusal after such an approval is a loop and ends the run.
                approved = state.get("approved_minor")
                if approved is None or approved < offer["total_minor"]:
                    await trace_decision(state, "authorize", "needs_approval_routed")
                    return {"decision": "request_approval"}
                await trace_decision(state, "authorize", "approval_loop")
                return {
                    "decision": "approval_loop",
                    "phase": DONE,
                    "finish": {"status": "error", "reason": "approval_loop"},
                }
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
            delivered = (
                f"Your order {state['order_id'][:8]} was delivered: "
                f"{count} items, {total} {currency} minor units."
            )
            body = {
                "user_id": state["user_id"],
                "template": SUMMARY_TEMPLATE,
                "params": {"text": _summary(delivered, state)},
                "buttons": [],
                "ref": {"kind": "order", "id": state["order_id"]},
            }
            _add(state, out, WHATSAPP, "whatsapp_out", body)
        # Take-turns (agents §10, AG7): household dinners only, never solo or weekly.
        household_dinner = (
            state.get("kind") == "dinner" and (state.get("scope") or HOUSEHOLD_SCOPE) != SOLO_SCOPE
        )
        on = bool(state.get("take_turns"))
        unavoidable = list(state.get("unavoidable_dislikes") or [])
        approver = state.get("approver_user_id")
        if household_dinner and unavoidable and approver:
            # Filed with this step by `send`, so it goes out at most once per order. It
            # names the dislike values, never the member.
            offer = {
                "user_id": approver,
                "template": TAKE_TURNS_OFFER_OFF_TEMPLATE if on else TAKE_TURNS_OFFER_TEMPLATE,
                "params": {"values": ", ".join(unavoidable)},
                "buttons": [
                    {
                        "payload": ids.take_turns_button(UUID(state["order_id"]), not on),
                        "title": STOP_TAKING_TURNS_TITLE if on else TAKE_TURNS_TITLE,
                    }
                ],
                "ref": {"kind": "order", "id": state["order_id"]},
            }
            _add(state, out, WHATSAPP, "whatsapp_out", offer)
            await trace_decision(state, "finish", "take_turns_offered", f"on={str(on).lower()}")
        if household_dinner and on:
            # A replay changes nothing (agents §10), so a retried step is safe.
            result = await tool(state, "advance_turn", {"order_id": state["order_id"]})
            if result.get("advanced"):
                await trace_decision(state, "finish", "turn_advanced")
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

    def after_build_list(state: HouseholdState) -> str:
        return NOTHING_NEEDED if state.get("decision") == NOTHING_NEEDED else "request_quotes"

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

    def after_authorize(state: HouseholdState) -> str:
        return "request_approval" if state.get("decision") == "request_approval" else "send"

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
        "nothing_needed": nothing_needed,
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
    g.add_conditional_edges("build_list", after_build_list, ["request_quotes", NOTHING_NEEDED])
    g.add_edge(NOTHING_NEEDED, "send")
    g.add_edge("request_quotes", "send")
    g.add_conditional_edges("record_offer", after_offer, ["negotiate", "send"])
    g.add_conditional_edges("negotiate", after_negotiate, ["send", "choose_offer", "no_offers"])
    g.add_conditional_edges(
        "choose_offer", after_choose, ["authorize", "request_approval", "no_offers"]
    )
    g.add_edge("no_offers", "send")
    g.add_edge("request_approval", "send")
    g.add_conditional_edges("approval_decided", after_approval, ["authorize", "send"])
    g.add_conditional_edges("authorize", after_authorize, ["request_approval", "send"])
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
        call_tool: CallTool = mcp_client.call_tool_as,
        clock: Clock = _now,
        sleep: Sleep = asyncio.sleep,
        offer_ttl_seconds: int = OFFER_TTL_SECONDS,
        load_whatsapp_members: LoadMembers | None = None,
        label_seller: LabelSeller = _agent_id_label,
        load_vocabulary: LoadVocabulary = pantry.load_vocabulary,
    ) -> None:
        self._load_run = load_run
        self._offer_ttl_seconds = offer_ttl_seconds
        self.builder = build_graph(
            model,
            traces,
            call_tool,
            load_members,
            find_sellers,
            clock,
            sleep,
            load_whatsapp_members,
            label_seller,
            load_vocabulary,
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
                negotiate_hold_seconds=_int(run.overrides.get("STUB_NEGOTIATE_HOLD_SECONDS"), 0),
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
_RUN_MODES_SQL = "SELECT modes FROM runs WHERE run_id = %s"
_ORG_NAME_SQL = "SELECT name FROM organizations WHERE 'org:' || org_id = %s"
_default: HouseholdGraph | None = None


async def seller_label(conn: Any, run_id: UUID, agent_id: str) -> str:
    """`vendors.vendor_label(area, card, org_name, runs.modes).text` for seller card
    `agent_id` in run `run_id`, from the run's pinned modes (adapters §6). A run with no
    pinned modes labels every card `simulated`."""
    from services.common import directory, vendors

    card = await directory.card(conn, agent_id)
    cur = await unscoped(conn, "household_graph").execute(_ORG_NAME_SQL, (card.owner_ref,))
    org = await cur.fetchone()
    cur = await unscoped(conn, "household_graph").execute(_RUN_MODES_SQL, (run_id,))
    run = await cur.fetchone()
    run_modes = run[0] if run and isinstance(run[0], dict) else {}
    org_name = org[0] if org else card.owner_ref
    return vendors.vendor_label(card.agent_class, card, org_name, run_modes).text


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
            cur = await unscoped(conn, "household_graph").execute(_RUN_SQL, (run_id,))
            row = await cur.fetchone()
        if row is None:
            raise LookupError(f"run {run_id} does not exist")
        overrides = row[1] if isinstance(row[1], dict) else {}
        return RunInfo(user_id=row[0], overrides=overrides)

    async def load_members(household_id: str) -> Members:
        async with pool.connection() as conn:
            cur = await unscoped(conn, "household_graph").execute(_MEMBERS_SQL, (household_id,))
            return {agent_id: user_id for agent_id, user_id in await cur.fetchall()}

    async def load_whatsapp_members(household_id: str) -> Members:
        async with pool.connection() as conn:
            cur = await unscoped(conn, "household_graph").execute(
                _WHATSAPP_MEMBERS_SQL, (household_id,)
            )
            return {agent_id: user_id for agent_id, user_id in await cur.fetchall()}

    async def find_sellers(cls: str, region: str, zone_id: str, currency: str) -> list[str]:
        async with pool.connection() as conn:
            cards = await directory.find(conn, cls, region, zone_id, currency)
        return [c.agent_id for c in cards]

    async def label_seller(run_id: UUID, agent_id: str) -> str:
        async with pool.connection() as conn:
            return await seller_label(conn, run_id, agent_id)

    return HouseholdGraph(
        model=ModelClient(traces, PgRunState(pool)),
        traces=traces,
        checkpointer=AsyncPostgresSaver(pool),  # type: ignore[arg-type]
        load_run=load_run,
        load_members=load_members,
        find_sellers=find_sellers,
        offer_ttl_seconds=get_settings().offer_ttl_seconds,
        load_whatsapp_members=load_whatsapp_members,
        label_seller=label_seller,
    )


async def handle(msg: Inbound, card: AgentCard) -> StepResult:
    """The household class handler registered in `services.agents.host.HANDLERS`."""
    global _default
    if _default is None:
        _default = _build_default()
    return await _default(msg, card)
