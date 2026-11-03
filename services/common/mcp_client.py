"""The caller side of a tool call (architecture item 23, amended 2026-11-01).

`call_tool(server, tool, args, *, ctx, token)` calls one tool on the `mcp` service over
streamable HTTP at `http://mcp:8002/mcp/<server>`, with a 15 s timeout per attempt.

*   The token goes only in the `Authorization: Bearer` header, never in `_meta`.
*   `_meta` carries `run_id`, `agent_id`, `conversation_id` and `attempt` (1 or 2).
*   Results are read as:
    *   success (`isError` false): returns `structuredContent`;
    *   `kind="refused"`: raises `ToolRefused(reason)`, no retry;
    *   `kind="unavailable"`: retries once if `retryable`, else raises
        `AdapterUnavailable(reason)`;
    *   transport failure (timeout, connection error or HTTP 5xx): retries once;
    *   `isError=true` without a valid `structuredContent`: raises
        `AdapterUnavailable("malformed_error")`, no retry.
*   At most 2 attempts per call (§9), both with the same `args`, so the same idempotency
    key. When the second attempt also fails, `AdapterUnavailable` carries the last reason
    (`transport` for a transport failure).
"""

from collections.abc import Mapping
from typing import Any

import anyio
import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult

from services.common import db
from services.common.run_context import RunContext
from services.identity import errors, token_source

__all__ = ["AdapterUnavailable", "ToolRefused", "call_tool", "call_tool_as"]

BASE_URL = "http://mcp:8002"
TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 2

TRANSPORT = "transport"
MALFORMED_ERROR = "malformed_error"
MALFORMED_RESULT = "malformed_result"
PROTOCOL_ERROR = "protocol_error"


class ToolRefused(Exception):
    """The server refused the call (`kind="refused"`). Never retried."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AdapterUnavailable(Exception):
    """The call could not be completed: an unavailable result or a transport failure."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Retry(Exception):
    """One attempt failed in a way that may be retried."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _url(server: str) -> str:
    return f"{BASE_URL}/mcp/{server}"


def _meta(ctx: RunContext, attempt: int) -> dict[str, Any]:
    return {
        "run_id": str(ctx.run_id),
        "agent_id": ctx.agent_id,
        "conversation_id": str(ctx.conversation_id) if ctx.conversation_id else None,
        "attempt": attempt,
    }


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for inner in exc.exceptions for leaf in _leaves(inner)]
    return [exc]


def _read(result: CallToolResult) -> dict[str, Any]:
    """Read one result. Raises `ToolRefused`, `AdapterUnavailable` or `_Retry`."""
    body = result.structured_content
    if not result.is_error:
        if not isinstance(body, dict):
            raise AdapterUnavailable(MALFORMED_RESULT)
        return body
    if (
        not isinstance(body, Mapping)
        or body.get("kind") not in ("refused", "unavailable")
        or not isinstance(body.get("reason"), str)
        or not body["reason"]
        or not isinstance(body.get("retryable", False), bool)
    ):
        raise AdapterUnavailable(MALFORMED_ERROR)
    if body["kind"] == "refused":
        raise ToolRefused(body["reason"])
    if body.get("retryable", False):
        raise _Retry(body["reason"])
    raise AdapterUnavailable(body["reason"])


async def _attempt(
    server: str, tool: str, args: Mapping[str, Any], ctx: RunContext, token: str, attempt: int
) -> CallToolResult:
    """One attempt. Raises `_Retry(TRANSPORT)` on a timeout, connection error or HTTP 5xx."""
    server_error = False

    async def note_status(response: httpx2.Response) -> None:
        nonlocal server_error
        if response.status_code >= 500:
            server_error = True

    try:
        with anyio.fail_after(TIMEOUT_SECONDS):
            async with httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {token}"},
                event_hooks={"response": [note_status]},
                timeout=TIMEOUT_SECONDS,
            ) as http:
                async with streamable_http_client(_url(server), http_client=http) as (
                    read,
                    write,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        return await session.call_tool(tool, dict(args), meta=_meta(ctx, attempt))
    except BaseException as exc:
        leaves = _leaves(exc)
        if any(isinstance(e, (TimeoutError, httpx2.TransportError)) for e in leaves):
            raise _Retry(TRANSPORT) from exc
        if server_error and all(isinstance(e, MCPError) for e in leaves):
            raise _Retry(TRANSPORT) from exc
        if all(isinstance(e, MCPError) for e in leaves):
            # A JSON-RPC error that is not a 5xx (unknown tool, bad arguments, 4xx).
            raise AdapterUnavailable(PROTOCOL_ERROR) from exc
        raise


async def call_tool(
    server: str, tool: str, args: Mapping[str, Any], *, ctx: RunContext, token: str
) -> dict[str, Any]:
    """Call `tool` on `server` and return its `structuredContent`.

    Raises `ToolRefused` or `AdapterUnavailable`, each with `reason`.
    """
    reason = TRANSPORT
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return _read(await _attempt(server, tool, args, ctx, token, attempt))
        except _Retry as retry:
            reason = retry.reason
    raise AdapterUnavailable(reason)


# --- call_tool_as (identity-authz I6) ---------------------------------------------------------

_TOKEN_REFUSALS = ("token_", "act_")
_UNAVAILABLE = {
    "identity_unavailable": errors.IdentityUnavailable,
    "authz_unavailable": errors.AuthzUnavailable,
}


async def _token(ctx: RunContext, *, fresh: bool = False) -> str:
    """A token for `ctx.agent_id` in `ctx.run_id`: a people or a service token by
    `agent_clients.kind`. An agent with no client is refused `unknown_agent`."""
    async with db.get_pool().connection() as conn:
        cur = await conn.execute(
            "SELECT c.kind, COALESCE(h.env, 'live'), r.modes->>'token_source'"
            " FROM agent_clients c"
            " LEFT JOIN runs r ON r.run_id = %s"
            " LEFT JOIN households h ON h.household_id = r.household_id"
            " WHERE c.agent_id = %s",
            (ctx.run_id, ctx.agent_id),
        )
        row = await cur.fetchone()
        if row is None:
            raise errors.TokenRefused("unknown_agent")
        kind, env, recorded = row
        source = token_source.get_source(env, recorded)
        if fresh:
            source.invalidate(ctx.run_id if kind == "people" else None, ctx.agent_id)
        if kind == "service":
            return await source.service_token(ctx.agent_id)
        try:
            return await source.people_token(conn, ctx.run_id, ctx.agent_id)
        except errors.GrantError:
            await conn.commit()  # keep an expiry mark from `grants.load_active`
            raise


def _mapped(exc: ToolRefused) -> Exception:
    if exc.reason.startswith("grant_"):
        return errors.grant_error(exc.reason)()
    return exc


async def call_tool_as(
    server: str, tool: str, args: Mapping[str, Any], *, ctx: RunContext
) -> dict[str, Any]:
    """`call_tool` with the token `TokenSource` gives `ctx.agent_id`. The one place that
    obtains tokens (identity-authz I6).

    *   A `token_*` or `act_*` refusal: one new token and one more call (the same `args`,
        so the same idempotency key), then `TokenRefused`.
    *   A `grant_*` refusal raises the matching `GrantError`.
    *   `AdapterUnavailable` with `identity_unavailable` or `authz_unavailable` (after
        `call_tool`'s own retry) raises the matching `DependencyUnavailable`.
    *   Anything else is re-raised unchanged.
    """
    try:
        try:
            return await call_tool(server, tool, args, ctx=ctx, token=await _token(ctx))
        except ToolRefused as refused:
            if not refused.reason.startswith(_TOKEN_REFUSALS):
                raise _mapped(refused) from None
        try:
            return await call_tool(
                server, tool, args, ctx=ctx, token=await _token(ctx, fresh=True)
            )
        except ToolRefused as again:
            if again.reason.startswith(_TOKEN_REFUSALS):
                raise errors.TokenRefused(again.reason) from None
            raise _mapped(again) from None
    except AdapterUnavailable as unavailable:
        mapped = _UNAVAILABLE.get(unavailable.reason)
        if mapped is None:
            raise
        raise mapped() from None
