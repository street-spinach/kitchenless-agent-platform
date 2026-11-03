"""`call_tool_as` and the server's identity context (identity-authz I6)."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import jwt
import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from services.common import mcp_client
from services.common.mcp_client import AdapterUnavailable, ToolRefused
from services.common.run_context import RunContext
from services.identity import claims as c
from services.identity import errors
from services.mcp import meta
from tests.unit import identity_world as w

RUN = uuid.UUID("00000000-0000-4000-8000-00000000000f")
CTX = RunContext(run_id=RUN, agent_id="household:h-1", conversation_id=None)
ARGS = {"order_id": "o-1", "amount_minor": 100}


class Calls:
    """A fake `call_tool`: answers from `outcomes` in order, recording every call."""

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, server: str, tool: str, args: Any, *, ctx: RunContext, token: str
    ) -> dict[str, Any]:
        self.calls.append({"server": server, "tool": tool, "args": args, "token": token})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def tokens_seen(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Replace `_token`: token `t<n>`, recording whether a fresh one was asked for."""
    asked: list[bool] = []

    async def token(ctx: RunContext, *, fresh: bool = False) -> str:
        asked.append(fresh)
        return f"t{len(asked)}"

    monkeypatch.setattr(mcp_client, "_token", token)
    return asked


def _call(monkeypatch: pytest.MonkeyPatch, fake: Calls) -> dict[str, Any]:
    monkeypatch.setattr(mcp_client, "call_tool", fake)
    return asyncio.run(mcp_client.call_tool_as("household", "authorize_spend", ARGS, ctx=CTX))


def test_token_not_in_meta(monkeypatch: pytest.MonkeyPatch, tokens_seen: list[bool]) -> None:
    for attempt in (1, 2):
        sent = mcp_client._meta(CTX, attempt)
        assert set(sent) == {"run_id", "agent_id", "conversation_id", "attempt"}
    fake = Calls({"ok": True})
    assert _call(monkeypatch, fake) == {"ok": True}
    [call] = fake.calls
    assert call["token"] == "t1"  # only through `token=`, the bearer header
    assert call["args"] == ARGS
    assert not any("t1" in str(v) for v in call["args"].values())


def test_token_refusal_refreshes_once(
    monkeypatch: pytest.MonkeyPatch, tokens_seen: list[bool]
) -> None:
    fake = Calls(ToolRefused(c.TOKEN_EXPIRED), {"ok": True})
    assert _call(monkeypatch, fake) == {"ok": True}
    assert tokens_seen == [False, True]
    assert [x["token"] for x in fake.calls] == ["t1", "t2"]
    assert fake.calls[0]["args"] == fake.calls[1]["args"]  # same idempotency key

    # Refused twice: one retry only, then `TokenRefused` with the reason.
    tokens_seen.clear()
    fake = Calls(ToolRefused(c.ACT_MISMATCH), ToolRefused(c.TOKEN_INVALID))
    with pytest.raises(errors.TokenRefused) as e:
        _call(monkeypatch, fake)
    assert e.value.reason == c.TOKEN_INVALID
    assert len(fake.calls) == 2


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (ToolRefused(c.GRANT_REVOKED), errors.GrantRevoked),
        (ToolRefused(c.GRANT_EXPIRED), errors.GrantExpired),
        (ToolRefused(c.GRANT_MISSING), errors.GrantMissing),
        (AdapterUnavailable(c.IDENTITY_UNAVAILABLE), errors.IdentityUnavailable),
        (AdapterUnavailable(c.AUTHZ_UNAVAILABLE), errors.AuthzUnavailable),
        (ToolRefused(c.AUTHZ_DENIED), ToolRefused),
        (ToolRefused(c.SCOPE_MISSING), ToolRefused),
        (AdapterUnavailable("transport"), AdapterUnavailable),
    ],
)
def test_refusal_reason_maps_to_exception(
    monkeypatch: pytest.MonkeyPatch,
    tokens_seen: list[bool],
    raised: Exception,
    expected: type[Exception],
) -> None:
    fake = Calls(raised)
    with pytest.raises(expected) as e:
        _call(monkeypatch, fake)
    assert type(e.value) is expected
    assert len(fake.calls) == 1  # none of these gets a new token
    if expected in (ToolRefused, AdapterUnavailable):
        assert e.value is raised  # re-raised unchanged


def test_dependency_unavailable_is_retryable_result() -> None:
    ctx: Any = object()  # `tool_errors` reads the context only for adapter failures

    async def go(exc: Exception) -> Any:
        async with meta.tool_errors(ctx, "household", "authorize_spend") as err:
            raise exc
        return err.result

    for exc, reason in (
        (errors.AuthzUnavailable(), c.AUTHZ_UNAVAILABLE),
        (errors.IdentityUnavailable(), c.IDENTITY_UNAVAILABLE),
    ):
        result = asyncio.run(go(exc))
        assert result.is_error
        assert result.structured_content == {
            "kind": "unavailable",
            "reason": reason,
            "retryable": True,
        }


# --- db ---------------------------------------------------------------------------------------


def _with_pool[T](
    pg: str, monkeypatch: pytest.MonkeyPatch, module: Any, fn: Callable[[], Awaitable[T]]
) -> T:
    async def go() -> T:
        async with AsyncConnectionPool(pg, min_size=1, max_size=2, open=False) as pool:
            monkeypatch.setattr(module.db, "get_pool", lambda: pool)
            return await fn()

    return asyncio.run(go())


@pytest.mark.db
def test_call_tool_as_picks_token_by_client_kind(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    seller = "grocery:mock-us"
    with psycopg.connect(pg) as conn:
        world = w.add_world(conn)
        w.add_agent(conn, seller, "org:mock")
        run_id = w.add_run(conn, world.household_id)
    fake = Calls({"ok": 1}, {"ok": 2})
    monkeypatch.setattr(mcp_client, "call_tool", fake)

    async def go() -> None:
        for agent in (world.household_agent, seller):
            ctx = RunContext(run_id=run_id, agent_id=agent, conversation_id=None)
            await mcp_client.call_tool_as("household", "x", {}, ctx=ctx)

    _with_pool(pg, monkeypatch, mcp_client, go)
    people, service = (
        jwt.decode(x["token"], options={"verify_signature": False}) for x in fake.calls
    )
    assert people["sub"] == world.admin.keycloak_sub
    assert people["act"]["client_id"] == "household.h-1"
    assert "act" not in service
    assert service["azp"] == "grocery.mock-us"

    # An agent with no client gets no token: refused `unknown_agent`, nothing called.
    async def unknown() -> None:
        ctx = RunContext(run_id=run_id, agent_id="grocery:nobody", conversation_id=None)
        await mcp_client.call_tool_as("household", "x", {}, ctx=ctx)

    with pytest.raises(errors.TokenRefused) as e:
        _with_pool(pg, monkeypatch, mcp_client, unknown)
    assert e.value.reason == c.UNKNOWN_AGENT
    assert len(fake.calls) == 2


@pytest.mark.db
def test_second_token_on_same_session_is_used(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`from_meta` takes the token of the request being served (`meta.current_token()`,
    set per HTTP request by the item 40 middleware, see `test_mcp_app.py`), so a second
    request on one session carries its own token."""
    with psycopg.connect(pg) as conn:
        world = w.add_world(conn)
        run_id = w.add_run(conn, world.household_id)
    sent = {"run_id": str(run_id), "agent_id": world.household_agent, "attempt": 1}

    async def go() -> list[str | None]:
        seen = []
        for token in ("token-1", "token-2"):
            reset = meta.set_current_token(token)
            try:
                seen.append((await meta.CallContext.from_meta(sent)).token)
            finally:
                meta.reset_current_token(reset)
        return seen

    assert _with_pool(pg, monkeypatch, meta, go) == ["token-1", "token-2"]
