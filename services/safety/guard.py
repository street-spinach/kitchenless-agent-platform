"""The guard for tools that spend money, commit an order or share personal data.

Architecture item 49 and §5 "Guard steps". `run_guarded()` runs the steps in order:

0.  refuse if the run has already ended;
1.  validate the caller's token (`tokens.validate`, identity-authz §8 step 1): the
    signature, issuer, audience and expiry, the agent (`act`, or `azp` for a service
    token) equal to the calling agent, and for a delegated token the person's active
    grant;
1b. check the §8 step 1b table (`tokens.TOOL_SCOPES`): the caller kind, the agent class
    and the scope;
2.  refuse a `household_id` argument that is not the run's household (`authz_denied`),
    then check the OpenFGA relation and set `ctx.authz_form`;
3.  check the limits;
4.  claim the idempotency key (`services/safety/idempotency.py`). A same-request key that
    is `in_progress` is taken over when the same run claimed it, whatever its age, so the
    client's retry after an adapter failure reaches the vendor again (the vendor
    deduplicates on the same vendor key). Held by another run, it is taken over only after
    60 s; before that the call is refused with `idempotency_conflict`;
5.  write the `tool_call` trace row; if that write fails, stop before the adapter;
6.  call the adapter, which returns a minimal typed result;
7.  in one transaction, store the result under the key, apply the tool's order-state
    change and write the `tool_result` trace row. First complete wins: the result is
    stored only while the key is `in_progress`. If another call of the same run completed
    the key first, `apply` is skipped and the stored result is traced and returned.

A refusal at any step writes a `tool_result` row with `refused=true` and raises
`GuardRefused(reason)`; identity refusals (`IdentityError`) become `GuardRefused` with
their §12 reason. `DependencyUnavailable` (Keycloak or OpenFGA down) is not a refusal and
propagates; `meta.tool_errors()` turns both into the wire result.

The `tool_call` and `tool_result` rows carry the identity fields of identity-authz §14:
`sub_user_id`, `act_agent_id`, `grant_id` (`null` for service tokens), `token_source` and
`authz` (`{relation, object, allowed, model_id}`, `null` for tools with no relation).
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
from services.identity import claims as c
from services.identity import fga, tokens
from services.identity.claims import Claims
from services.identity.errors import IdentityError
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
    @property
    def env(self) -> str: ...

    # Set by the guard (identity-authz I6).
    claims: Claims | None
    authz_form: str | None


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
    authz_form: str | None = None,
) -> Any:
    """Run the guard steps for one guarded tool call and return the tool's result.

    *   `tool` is `"<server>.<tool>"`, for example `"household.authorize_spend"`.
    *   `relation` is `(relation, object)`; the user is the agent's OpenFGA object
        (`fga.agent_object`, `agent:household.us-1`). `None` for tools with no OpenFGA
        relation (§5 table).
    *   `authz_form` names the form of the tool that the relation checks (for example the
        `get_constraints` form, identity-authz §8 step 2); the guard sets it on
        `ctx.authz_form` once the relation passes.
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

    # The identity fields of every tool_call and tool_result row (identity-authz §14).
    identity: dict[str, Any] = {
        "sub_user_id": None,
        "act_agent_id": None,
        "grant_id": None,
        "token_source": None,
        "authz": None,
    }

    def result_row(result: Any, refused: bool, reason: str | None) -> dict[str, Any]:
        return {
            "server": server,
            "tool": name,
            "result": result,
            "refused": refused,
            "reason": reason,
            **identity,
        }

    async def refuse(reason: str) -> GuardRefused:
        await trace("tool_result", result_row(None, True, reason))
        return GuardRefused(reason)

    try:
        async with pool.connection() as conn:
            # Step 0: the run must still be going.
            household = await _check_run(conn, ctx.run_id)
            # Step 1: the caller's token; step 1b: the scope table.
            claims = await _check_token(conn, ctx, name)
            identity.update(
                sub_user_id=None if claims.user_id is None else str(claims.user_id),
                act_agent_id=claims.agent_id,
                grant_id=None if claims.grant_id is None else str(claims.grant_id),
                token_source=claims.token_source,
            )
            # Step 2: the household argument, then the OpenFGA relation. A timeout raises
            # `AuthzUnavailable`, which is not a refusal and propagates (§8 step 2, I-G9).
            _check_household_argument(request, household)
            if relation is not None:
                rel, obj = relation
                allowed, model_id = await asyncio.to_thread(
                    fga.check, fga.agent_object(ctx.agent_id), rel, obj
                )
                identity["authz"] = {
                    "relation": rel,
                    "object": obj,
                    "allowed": allowed,
                    "model_id": model_id,
                }
                if not allowed:
                    raise GuardRefused(c.AUTHZ_DENIED)
            ctx.authz_form = authz_form
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
        {"server": server, "tool": name, "args": request, "idempotency_key": key, **identity},
    )

    if isinstance(claimed, idempotency.Done):
        await trace("tool_result", result_row(claimed.result, False, None))
        return claimed.result

    # Step 6: the adapter.
    result = await call()

    # Step 7: store the result, apply the state change and trace, in one transaction.
    async with pool.connection() as conn, conn.transaction():
        if key is not None:
            earlier = await idempotency.complete(conn, key, result)
            if earlier is not None:
                result = earlier.result
                await trace("tool_result", result_row(result, False, None), conn=conn)
                return result
        if apply is not None:
            await apply(conn, result)
        await trace("tool_result", result_row(result, False, None), conn=conn)
    return result


async def _check_run(conn: AsyncConnection, run_id: uuid.UUID) -> str | None:
    """The run's household; refuses `run_ended` for a missing or ended run."""
    cur = await conn.execute(
        "SELECT status, ended_at, household_id FROM runs WHERE run_id = %s", (run_id,)
    )
    row = await cur.fetchone()
    if row is None or row[1] is not None or row[0] in _ENDED_STATUSES:
        raise GuardRefused("run_ended")
    return row[2]


async def _check_token(conn: AsyncConnection, ctx: GuardContext, tool: str) -> Claims:
    """Steps 1 and 1b. An identity refusal becomes `GuardRefused` with its reason; an
    expiry mark written by `grants.load_active` is committed first."""
    if not ctx.token:
        raise GuardRefused(c.TOKEN_MISSING)
    try:
        claims = await tokens.validate(conn, ctx.token, ctx)
        tokens.check_tool_scope(tool, claims)
    except IdentityError as e:
        await conn.commit()
        raise GuardRefused(e.reason) from None
    ctx.claims = claims
    return claims


def _check_household_argument(request: Any, household: str | None) -> None:
    """§8 step 2: a `household_id` argument must be the run's household (I-CUJ 9)."""
    if isinstance(request, dict) and "household_id" in request:
        if household is None or str(request["household_id"]) != household:
            raise GuardRefused(c.AUTHZ_DENIED)
