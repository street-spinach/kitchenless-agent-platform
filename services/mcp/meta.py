"""The server side of a tool call (architecture item 41, amended 2026-11-01).

*   `current_token()` is the caller's bearer token. The item 40 middleware in
    `services/mcp/app.py` sets it for each HTTP request; it never comes from `_meta`.
*   `CallContext.from_meta(meta)` builds the call context from `_meta` (`run_id`,
    `agent_id`, `conversation_id`, `attempt`), the token, the run and the caller's card.
*   `AdapterFailure` is what adapters raise; `tool_error()` builds the wire error result;
    `tool_errors()` turns refusals and adapter failures into that result.
"""

import json
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Literal

from mcp.types import CallToolResult, TextContent

from services.common import db, directory
from services.common.trace import TraceStore
from services.identity.tokens import Claims
from services.safety.guard import GuardRefused

__all__ = [
    "AdapterFailure",
    "CallContext",
    "ToolErrors",
    "current_token",
    "reset_current_token",
    "set_current_token",
    "tool_error",
    "tool_errors",
]

# The reason `from_meta` refuses a call with no usable `run_id` or `agent_id`, or an
# `agent_id` with no card (amendment 2026-11-01: "refused like a missing agent_id").
META_REFUSED = "meta_invalid"
# The reason for a `run_id` with no `runs` row. Guard step 0 treats a missing run the same.
RUN_ENDED = "run_ended"

_token: ContextVar[str | None] = ContextVar("mcp_bearer_token", default=None)


def current_token() -> str | None:
    """The bearer token of the HTTP request being served, or `None`."""
    return _token.get()


def set_current_token(token: str | None) -> Token[str | None]:
    """Set the request's bearer token. Only the item 40 middleware calls this."""
    return _token.set(token)


def reset_current_token(reset: Token[str | None]) -> None:
    _token.reset(reset)


class AdapterFailure(Exception):
    """An adapter call failed.

    `adapter_timeout` and `adapter_5xx` are `retryable=True`; `adapter_4xx` is
    `retryable=False`.
    """

    def __init__(self, reason: str, retryable: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


@dataclass
class CallContext:
    """What a tool knows about its call. Separate from the client's `RunContext`."""

    run_id: uuid.UUID
    agent_id: str
    conversation_id: uuid.UUID | None
    attempt: int
    token: str | None
    env: str
    modes: dict[str, str]
    on_behalf_of: str | None
    # Set by the guard (identity-authz I6).
    claims: Claims | None = None
    authz_form: str | None = None

    @classmethod
    async def from_meta(cls, meta: Mapping[str, Any] | None) -> "CallContext":
        """Build the context for one call.

        Raises `GuardRefused` when `run_id` or `agent_id` is missing or malformed, when
        `agent_id` has no card, or when the run does not exist.
        """
        meta = meta or {}
        try:
            run_id = uuid.UUID(str(meta["run_id"]))
            agent_id = meta["agent_id"]
            if not isinstance(agent_id, str) or not agent_id:
                raise ValueError(agent_id)
            conversation = meta.get("conversation_id")
            conversation_id = uuid.UUID(str(conversation)) if conversation else None
            attempt = int(meta.get("attempt", 1))
        except (KeyError, TypeError, ValueError):
            raise GuardRefused(META_REFUSED) from None

        async with db.get_pool().connection() as conn:
            try:
                card = await directory.card(conn, agent_id)
            except directory.UnknownAgent:
                raise GuardRefused(META_REFUSED) from None
            cur = await conn.execute(
                "SELECT COALESCE(h.env, 'live'), r.modes FROM runs r"
                " LEFT JOIN households h ON h.household_id = r.household_id"
                " WHERE r.run_id = %s",
                (run_id,),
            )
            row = await cur.fetchone()
        if row is None:
            raise GuardRefused(RUN_ENDED)
        env, modes = row
        return cls(
            run_id=run_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            attempt=attempt,
            token=current_token(),
            env=env,
            modes=dict(modes),
            on_behalf_of=None if card.owner_ref == "system" else card.owner_ref,
        )


def tool_error(
    kind: Literal["refused", "unavailable"], reason: str, retryable: bool = False
) -> CallToolResult:
    """The wire error result: `isError=true` with `{kind, reason, retryable}`."""
    body = {"kind": kind, "reason": reason, "retryable": retryable}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(body))],
        structured_content=body,
        is_error=True,
    )


@dataclass
class ToolErrors:
    """The holder `tool_errors()` yields. `result` is set when the body failed."""

    result: CallToolResult | None = None


@asynccontextmanager
async def tool_errors(ctx: CallContext, server: str, tool: str) -> AsyncIterator[ToolErrors]:
    """Turn `GuardRefused` and `AdapterFailure` into the wire error result.

    An `AdapterFailure` first writes the §9 `error` trace row. Any other exception
    propagates.

        async with tool_errors(ctx, "household", "authorize_spend") as err:
            return await run_guarded(ctx, ...)
        return err.result
    """
    err = ToolErrors()
    try:
        yield err
    except GuardRefused as e:
        err.result = tool_error("refused", e.reason)
    except AdapterFailure as e:
        await TraceStore(db.get_pool()).write(
            ctx.run_id,
            ctx.agent_id,
            "error",
            {"where": f"{server}.{tool}", "reason": e.reason, "attempt": ctx.attempt},
            on_behalf_of=ctx.on_behalf_of,
            conversation_id=ctx.conversation_id,
        )
        err.result = tool_error("unavailable", e.reason, e.retryable)
