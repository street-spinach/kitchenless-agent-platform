"""A2A forms of the stored records (agent-protocol §2, §7 "Quotes as A2A tasks", §11, P5).

Pure functions only: nothing here reads or writes the database. The stored columns are the
record; these functions compute the A2A card, message and task-state forms on request for
the HTTP binding and the panel.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from services.common import ids
from services.common.ids import NS_A2A
from services.common.protocol.catalog import render_summary

__all__ = [
    "A2A_PROTOCOL_VERSION",
    "JSONRPC_ERRORS",
    "NS_A2A",
    "A2AContext",
    "AgentCardRow",
    "AgentMessageRow",
    "OutboxArgs",
    "QuoteRow",
    "error_data",
    "from_a2a_message",
    "task_state",
    "to_a2a_card",
    "to_a2a_message",
]

# The only accepted `A2A-Version` value; empty or missing means 0.3 and is refused (Q3).
A2A_PROTOCOL_VERSION = "1.0"

# JSON-RPC error name -> (code, ErrorInfo.reason), exactly the Q2 table.
JSONRPC_ERRORS: dict[str, tuple[int, str | None]] = {
    "TaskNotFoundError": (-32001, "TASK_NOT_FOUND"),
    "TaskNotCancelableError": (-32002, "TASK_NOT_CANCELABLE"),
    "UnsupportedOperationError": (-32004, "UNSUPPORTED_OPERATION"),
    "VersionNotSupportedError": (-32009, "VERSION_NOT_SUPPORTED"),
    "JSONParseError": (-32700, None),
    "InvalidRequestError": (-32600, None),
    "MethodNotFoundError": (-32601, None),
    "InvalidParamsError": (-32602, None),
    "InternalError": (-32603, None),
}

A2A_DOMAIN = "a2a-protocol.org"
ERROR_INFO_TYPE = "type.googleapis.com/google.rpc.ErrorInfo"

ROLE_USER = "ROLE_USER"
ROLE_AGENT = "ROLE_AGENT"

TASK_STATE_SUBMITTED = "TASK_STATE_SUBMITTED"
TASK_STATE_WORKING = "TASK_STATE_WORKING"
TASK_STATE_INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
TASK_STATE_REJECTED = "TASK_STATE_REJECTED"
TASK_STATE_CANCELED = "TASK_STATE_CANCELED"
TASK_STATE_FAILED = "TASK_STATE_FAILED"

ON_BEHALF_OF_EXTENSION = "urn:kitchenless:on-behalf-of:v1"
# Types an outside caller may send with SendMessage (§11 step 2); `cancel` goes by CancelTask.
OUTSIDE_TYPES = ("quote_request", "counter_offer", "reject")
_SKILL_BY_CLASS = {"grocery": "grocery_quote", "restaurant": "dinner_quote"}
_ORDER_KIND_BY_CLASS = {"grocery": "grocery", "restaurant": "dinner"}
# Types sent by the side that opens a task: with no quote row yet, their sender is the opener.
_OPENING_TYPES = frozenset({"quote_request", "delivery_quote_request"})
# Fields the endpoint fills on a quote_request; client values for them are ignored (§11 step 4).
_REQUEST_FILLED = ("order_id", "zone_id", "currency", "window", "reply_by")


@dataclass(frozen=True)
class AgentMessageRow:
    """The `agent_messages` columns these functions read (architecture item 10)."""

    message_id: UUID
    run_id: UUID
    conversation_id: UUID | None
    causation_id: UUID | None
    from_agent: str
    to_agent: str
    type: str
    body: dict[str, Any]


@dataclass(frozen=True)
class QuoteRow:
    """The `quotes` columns these functions read (architecture item 11, `closed_by` §7)."""

    quote_id: UUID
    run_id: UUID
    order_id: UUID
    conversation_id: UUID
    seller_agent_id: str
    buyer_agent_id: str
    status: str
    expires_at: datetime
    closed_by: str | None = None


@dataclass(frozen=True)
class AgentCardRow:
    """An `agent_cards` row (architecture item 9), with the owner org's display name.

    `org_name` is `organizations.name` for `owner_ref`; the caller reads it with the card so
    `to_a2a_card` needs no database access.
    """

    agent_id: str
    agent_class: str
    owner_ref: str
    org_name: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class A2AContext:
    """The fields the endpoint fills from its own records, never from the client (§11)."""

    run_id: UUID
    order_id: UUID
    conversation_id: UUID
    quote_id: UUID | None = None
    zone_id: str | None = None
    currency: str | None = None
    window: dict[str, Any] | None = None
    reply_by: str | None = None


@dataclass(frozen=True)
class OutboxArgs:
    """Keyword arguments for `outbox.send()` (architecture item 19, P11)."""

    message_id: UUID
    run_id: UUID
    conversation_id: UUID | None
    causation_id: UUID | None
    from_agent: str
    to_agent: str
    type: str
    body: dict[str, Any]
    trace_extra: dict[str, Any] | None = None


def error_data(reason: str, domain: str = A2A_DOMAIN) -> list[dict[str, Any]]:
    """JSON-RPC `error.data`: an array holding one `google.rpc.ErrorInfo` (Q2)."""
    return [{"@type": ERROR_INFO_TYPE, "reason": reason, "domain": domain}]


def task_state(quote: QuoteRow | None, request_status: str, now: datetime) -> str:
    """The A2A task state of a quote, computed from the stored rows (§7 table).

    `request_status` is the `quote_request` row's status. Only the states an outside reader
    can reach are returned; any other combination raises `ValueError`.
    """
    if quote is None:
        if request_status == "pending":
            return TASK_STATE_SUBMITTED
        if request_status == "processing":
            return TASK_STATE_WORKING
        if request_status == "failed":
            return TASK_STATE_FAILED
        raise ValueError(f"no quote and request status {request_status!r} has no task state")
    if quote.status in ("open", "countered"):
        return TASK_STATE_INPUT_REQUIRED if quote.expires_at > now else TASK_STATE_FAILED
    if quote.status == "expired":
        return TASK_STATE_FAILED
    if quote.status == "rejected" and quote.closed_by == "seller":
        return TASK_STATE_REJECTED
    if quote.status == "rejected" and quote.closed_by == "buyer":
        return TASK_STATE_CANCELED
    raise ValueError(
        f"quote status {quote.status!r} closed by {quote.closed_by!r} has no task state"
    )


def to_a2a_message(row: AgentMessageRow, quote: QuoteRow | None) -> dict[str, Any]:
    """The A2A `Message` form of a stored row (P5).

    The data part holds `{type, body}` without `rationale`; the rationale, when present, is
    a separate text part (§5).
    """
    body = {k: v for k, v in row.body.items() if k != "rationale"}
    rationale = row.body.get("rationale")
    if quote is not None:
        opener = row.from_agent == quote.buyer_agent_id
    else:
        opener = row.type in _OPENING_TYPES
    parts: list[dict[str, Any]] = [{"data": {"type": row.type, "body": body}}]
    if rationale:
        parts.append({"text": rationale})
    message: dict[str, Any] = {
        "messageId": str(row.message_id),
        "role": ROLE_USER if opener else ROLE_AGENT,
        "parts": parts,
        "metadata": {
            "run_id": str(row.run_id),
            "from": row.from_agent,
            "to": row.to_agent,
            "causation_id": None if row.causation_id is None else str(row.causation_id),
            "summary": render_summary(row.type, row.body),
        },
    }
    if row.conversation_id is not None:
        message["contextId"] = str(row.conversation_id)
    task_id = row.body.get("quote_id") or (quote.quote_id if quote is not None else None)
    if task_id is not None:
        message["taskId"] = str(task_id)
    return message


def from_a2a_message(
    params: dict[str, Any], sender: str, to_agent: str, ctx: A2AContext
) -> OutboxArgs:
    """The `outbox.send()` arguments for a `SendMessage` request (§11).

    Requires `ROLE_USER` and exactly one data part `{type, body}`; text parts are ignored.
    The run, order, quote and conversation ids, and the quote_request's order fields, come
    from `ctx`, never from the client. Raises `ValueError` on a malformed message.
    """
    message = params.get("message")
    if not isinstance(message, dict):
        raise ValueError("params.message is required")
    if message.get("role") != ROLE_USER:
        raise ValueError("message.role must be ROLE_USER")
    a2a_id = message.get("messageId")
    if not isinstance(a2a_id, str) or not a2a_id:
        raise ValueError("message.messageId is required")
    parts = message.get("parts")
    data_parts = [p for p in parts if isinstance(p, dict) and "data" in p] if parts else []
    if len(data_parts) != 1:
        raise ValueError("message must have exactly one data part")
    data = data_parts[0]["data"]
    if not isinstance(data, dict) or not isinstance(data.get("type"), str):
        raise ValueError("the data part must be {type, body}")
    client_body = data.get("body")
    if not isinstance(client_body, dict):
        raise ValueError("the data part's body must be an object")
    type_ = data["type"]

    if type_ == "quote_request":
        to_class, _ = ids.parse_agent_id(to_agent)
        body: dict[str, Any] = {
            "items": client_body.get("items"),
            "constraints": client_body.get("constraints"),
            "kind": _ORDER_KIND_BY_CLASS.get(to_class),
        }
        filled = {name: getattr(ctx, name) for name in _REQUEST_FILLED}
        body.update({k: str(v) if isinstance(v, UUID) else v for k, v in filled.items()})
    else:
        body = dict(client_body)
        body["order_id"] = str(ctx.order_id)
        if ctx.quote_id is not None:
            body["quote_id"] = str(ctx.quote_id)

    request_hash = hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return OutboxArgs(
        message_id=ids.a2a_message_id(sender, a2a_id),
        run_id=ctx.run_id,
        conversation_id=ctx.conversation_id,
        causation_id=None,
        from_agent=sender,
        to_agent=to_agent,
        type=type_,
        body=body,
        trace_extra={"request_hash": request_hash},
    )


def to_a2a_card(card: AgentCardRow, base_url: str) -> dict[str, Any]:
    """The A2A `AgentCard` of a grocery or restaurant card (§2, Q4)."""
    skill = _SKILL_BY_CLASS.get(card.agent_class)
    if skill is None:
        raise ValueError(f"{card.agent_id}: only grocery and restaurant cards have A2A cards")
    url = f"{base_url.rstrip('/')}/a2a/{card.agent_id}"
    return {
        "name": card.agent_id,
        "description": card.config.get("description", ""),
        "supportedInterfaces": [
            {"url": url, "protocolBinding": "JSONRPC", "protocolVersion": A2A_PROTOCOL_VERSION}
        ],
        "provider": {"organization": card.org_name, "url": url},
        "version": card.config.get("card_version", "1.0.0"),
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extensions": [
                {"uri": ON_BEHALF_OF_EXTENSION, "params": {"on_behalf_of": card.owner_ref}}
            ],
        },
        "securitySchemes": {"bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}},
        "securityRequirements": [{"schemes": {"bearer": {"list": []}}}],
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "skills": [
            {
                "id": skill,
                "name": skill,
                "description": "Accepts message types: " + ", ".join(OUTSIDE_TYPES),
                "tags": list(OUTSIDE_TYPES),
            }
        ],
    }
