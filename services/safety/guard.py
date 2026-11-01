"""The guard for tools that spend money, commit an order or share personal data.

Architecture item 49 and §5 "Guard steps". `run_guarded()` runs the steps in order:

0.  refuse if the run has already ended;
1.  validate the caller's token (`sub`, `act`) and require `act` to be the calling agent;
2.  check the OpenFGA relation;
3.  check the limits;
4.  claim the idempotency key (`services/safety/idempotency.py`);
5.  write the `tool_call` trace row; if that write fails, stop before the adapter;
6.  call the adapter, which returns a minimal typed result;
7.  in one transaction, store the result under the key, apply the tool's order-state
    change and write the `tool_result` trace row.

A refusal at any step writes a `tool_result` row with `refused=true` and raises
`GuardRefused(reason)`. `meta.tool_errors()` turns that into the wire result.
"""

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from psycopg import AsyncConnection

from services.common import db
from services.common.trace import TraceStore
from services.identity import fga, tokens
from services.safety import idempotency

__all__ = ["GuardContext", "GuardRefused", "request_hash", "run_guarded"]

_ENDED_STATUSES = frozenset({"completed", "cancelled", "error", "timeout"})

Limits = Callable[[AsyncConnection], Awaitable[None]]
Call = Callable[[], Awaitable[Any]]
Apply = Callable[[AsyncConnection, Any], Awaitable[None]]


class GuardRefused(Exception):
    """A guard step refused the call. `reason` is the wire refusal reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GuardContext(Protocol):
    """What the guard reads from the call context (`meta.CallContext`, item 41)."""

    @property
    def run_id(self) -> uuid.UUID: ...
    @property
    def agent_id(self) -> str: ...
    @property
    def token(self) -> str | None: ...
    @property
    def on_behalf_of(self) -> str | None: ...
    @property
    def conversation_id(self) -> uuid.UUID | None: ...


def request_hash(request: Any) -> str:
    """A stable hash of the tool's request arguments."""
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _split(tool: str) -> tuple[str, str]:
    server, sep, name = tool.partition(".")
    if not sep or not server or not name:
        raise ValueError(f"tool must be '<server>.<tool>', got {tool!r}")
    return server, name


async def run_guarded(
    ctx: GuardContext,
    *,
    tool: str,
    relation: tuple[str, str] | None,
    limits: Limits | None,
    key: str | None,
    request: Any,
    call: Call,
    apply: Apply | None,
) -> Any:
    """Run the guard steps for one guarded tool call and return the tool's result.

    *   `tool` is `"<server>.<tool>"`, for example `"household.authorize_spend"`.
    *   `relation` is `(relation, object)`; the user is `agent:<ctx.agent_id>`. `None` for
        tools with no OpenFGA relation (§5 table).
    *   `limits(conn)` raises `GuardRefused` when a limit is broken.
    *   `key` is the idempotency key from `ids.idempotency_key`, or `None` for reads.
    *   `call()` calls the adapter and returns the minimal result.
    *   `apply(conn, result)` is the tool's order-state change, run in the step 7
        transaction.
    """
    server, name = _split(tool)
    pool = db.get_pool()
    store = TraceStore(pool)

    async def trace(
        kind: str, payload: dict[str, Any], conn: AsyncConnection | None = None
    ) -> None:
        await store.write(
            ctx.run_id,
            ctx.agent_id,
            kind,
            payload,
            on_behalf_of=ctx.on_behalf_of,
            conversation_id=ctx.conversation_id,
            conn=conn,
        )

    def result_row(result: Any, refused: bool, reason: str | None) -> dict[str, Any]:
        return {
            "server": server,
            "tool": name,
            "result": result,
            "refused": refused,
            "reason": reason,
        }

    async def refuse(reason: str) -> GuardRefused:
        await trace("tool_result", result_row(None, True, reason))
        return GuardRefused(reason)

    try:
        async with pool.connection() as conn:
            # Step 0: the run must still be going.
            await _check_run(conn, ctx.run_id)
            # Step 1: the caller's token.
            _check_token(ctx)
            # Step 2: the OpenFGA relation.
            if relation is not None:
                rel, obj = relation
                allowed = await asyncio.to_thread(fga.check, f"agent:{ctx.agent_id}", rel, obj)
                if not allowed:
                    raise GuardRefused("authz_denied")
            # Step 3: the limits.
            if limits is not None:
                await limits(conn)
            # Step 4: the idempotency key.
            claimed: idempotency.New | idempotency.Done | idempotency.Conflict
            claimed = idempotency.New()
            if key is not None:
                await conn.commit()  # the claim runs, and commits, in its own transaction
                claimed = await idempotency.claim(conn, key, ctx.run_id, request_hash(request))
                if isinstance(claimed, idempotency.Conflict):
                    raise GuardRefused("idempotency_conflict")
    except GuardRefused as e:
        raise await refuse(e.reason) from None

    # Step 5: the tool_call row. A failure here propagates and the adapter is not called.
    await trace(
        "tool_call",
        {"server": server, "tool": name, "args": request, "idempotency_key": key},
    )

    if isinstance(claimed, idempotency.Done):
        await trace("tool_result", result_row(claimed.result, False, None))
        return claimed.result

    # Step 6: the adapter.
    result = await call()

    # Step 7: store the result, apply the state change and trace, in one transaction.
    async with pool.connection() as conn, conn.transaction():
        if key is not None:
            await idempotency.complete(conn, key, result)
        if apply is not None:
            await apply(conn, result)
        await trace("tool_result", result_row(result, False, None), conn=conn)
    return result


async def _check_run(conn: AsyncConnection, run_id: uuid.UUID) -> None:
    cur = await conn.execute("SELECT status, ended_at FROM runs WHERE run_id = %s", (run_id,))
    row = await cur.fetchone()
    if row is None or row[1] is not None or row[0] in _ENDED_STATUSES:
        raise GuardRefused("run_ended")


def _check_token(ctx: GuardContext) -> None:
    if not ctx.token:
        raise GuardRefused("token_missing")
    try:
        claims = tokens.validate(ctx.token)
    except tokens.InvalidToken as e:
        expired = str(e) == "ExpiredSignatureError"
        raise GuardRefused("token_expired" if expired else "token_invalid") from None
    if claims.act != ctx.agent_id:
        raise GuardRefused("act_mismatch")
