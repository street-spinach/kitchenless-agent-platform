"""The `@traced` wrapper for unguarded tools (architecture item 42 and §5 "Tool traces").

Guarded tools are traced by the guard. Every other tool is wrapped with `@traced(server)`,
which applies guard step 0 (refuse ended runs), writes the `tool_call` row, calls the tool
and writes the `tool_result` row. Each call therefore has exactly one trace writer.

    @traced("household")
    async def get_pantry(ctx: CallContext) -> dict: ...

The wrapped function takes the `CallContext` first and its tool arguments as keywords,
which the `tool_call` row records as `args`. The tool name in the trace is the function's
name.
"""

import functools
from collections.abc import Awaitable, Callable
from typing import Any

from services.common import db
from services.common.trace import TraceStore
from services.mcp.meta import CallContext
from services.safety.guard import GuardRefused, _check_run

__all__ = ["traced"]

type Tool[R] = Callable[..., Awaitable[R]]


def traced[R](server: str) -> Callable[[Tool[R]], Tool[R]]:
    """Wrap an unguarded tool of `server` so its call and result are traced."""

    def decorate(fn: Tool[R]) -> Tool[R]:
        tool = fn.__name__

        @functools.wraps(fn)
        async def wrapper(ctx: CallContext, **kwargs: Any) -> R:
            pool = db.get_pool()
            store = TraceStore(pool)

            async def trace(kind: str, payload: dict[str, Any]) -> None:
                await store.write(
                    ctx.run_id,
                    ctx.agent_id,
                    kind,
                    payload,
                    on_behalf_of=ctx.on_behalf_of,
                    conversation_id=ctx.conversation_id,
                )

            def result_row(result: Any, reason: str | None) -> dict[str, Any]:
                return {
                    "server": server,
                    "tool": tool,
                    "result": result,
                    "refused": reason is not None,
                    "reason": reason,
                }

            # Guard step 0: the run must still be going.
            try:
                async with pool.connection() as conn:
                    await _check_run(conn, ctx.run_id)
            except GuardRefused as e:
                await trace("tool_result", result_row(None, e.reason))
                raise

            await trace(
                "tool_call",
                {"server": server, "tool": tool, "args": kwargs, "idempotency_key": None},
            )
            result = await fn(ctx, **kwargs)
            await trace("tool_result", result_row(result, None))
            return result

        return wrapper

    return decorate
