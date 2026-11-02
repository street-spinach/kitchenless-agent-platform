"""The internal A2A JSON-RPC 2.0 endpoint (agent-protocol §11, P19).

Hand-rolled with the pydantic models in this file (Q1, Alternative T). `A2AServer.routes()`
gives two Starlette routes that the agents host mounts on port 8003 (P18):

*   `GET /a2a/{agent_id}/.well-known/agent-card.json`: `to_a2a_card` for an enabled grocery
    or restaurant card; 404 for any other `agent_id`.
*   `POST /a2a/{agent_id}`: the bearer check (constant time, 401), the size limit (413), a
    token-bucket rate limit (429), the JSON-RPC parse, the `A2A-Version` check, then
    `SendMessage`, `GetTask` or `CancelTask`. Any other method gets `MethodNotFound`.

The sender is always `external:a2a-demo`. `SendMessage` turns the request into an ordinary
`agent_messages` row through `outbox.send()`, so the business handler and the live view treat
it like internal traffic. Every JSON-RPC error carries `error.data` as an array (Q2).
"""

import asyncio
import contextlib
import hmac
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Literal

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from services.agents.a2a import demo
from services.common import directory, ids, outbox
from services.common.config import get_settings
from services.common.directory import AgentCard
from services.common.outbox import _on_behalf_of
from services.common.protocol.a2a import A2A_PROTOCOL_VERSION as VERSION
from services.common.protocol.a2a import (
    JSONRPC_ERRORS,
    OUTSIDE_TYPES,
    TASK_STATE_CANCELED,
    TASK_STATE_FAILED,
    TASK_STATE_REJECTED,
    TASK_STATE_SUBMITTED,
    A2AContext,
    AgentCardRow,
    AgentMessageRow,
    QuoteRow,
    error_data,
    from_a2a_message,
    request_hash,
    task_state,
    to_a2a_card,
    to_a2a_message,
)
from services.common.protocol.catalog import CATALOG
from services.common.protocol.check import ProtocolError, check_static
from services.common.trace import TraceStore

__all__ = ["A2AServer", "BASE_URL", "RpcError", "SENDER", "TokenBucket"]

log = logging.getLogger(__name__)

SENDER = "external:a2a-demo"
# The card's interface URL (§2): the agents host on the Docker network.
BASE_URL = "http://agents:8003"
VERSION_HEADER = "A2A-Version"
POLL_SECONDS = 0.25
CONNECT_TIMEOUT_SECONDS = 2
# A protocol check's ErrorInfo domain (§11 "Error shape").
PROTOCOL_DOMAIN = "kitchenless"
A2A_CLASSES = frozenset({"grocery", "restaurant"})
TERMINAL_STATES = frozenset({TASK_STATE_REJECTED, TASK_STATE_CANCELED, TASK_STATE_FAILED})
_IN_FLIGHT = ("pending", "processing")

Connect = Callable[[], AbstractAsyncContextManager[AsyncConnection]]

# `TraceStore.write(..., conn=...)` never touches the pool.
_TRACES = TraceStore(None)  # type: ignore[arg-type]


# JSON-RPC and A2A request models -----------------------------------------------------------


class RpcRequest(BaseModel):
    jsonrpc: Literal["2.0"]
    id: str | int | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class SendConfiguration(BaseModel):
    model_config = ConfigDict(extra="ignore")
    returnImmediately: bool = False  # noqa: N815 (A2A field name)


class SendMessageParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    message: dict[str, Any]
    configuration: SendConfiguration | None = None


class GetTaskParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: uuid.UUID
    historyLength: int | None = Field(default=None, ge=0)  # noqa: N815 (A2A field name)


class CancelTaskParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: uuid.UUID


class RpcError(Exception):
    """A JSON-RPC error. `data` is always an array (Q2)."""

    def __init__(self, name: str, message: str, data: list[dict[str, Any]] | None = None) -> None:
        code, reason = JSONRPC_ERRORS[name]
        self.code = code
        self.message = message
        self.data = data if data is not None else (error_data(reason) if reason else [])
        super().__init__(f"{name}: {message}")

    def to_json(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "data": self.data}


def _invalid_params(message: str) -> RpcError:
    return RpcError("InvalidParamsError", message)


def _protocol_refused(code: str, detail: str) -> RpcError:
    """`InvalidParams` for a failed protocol check, with the code as an ErrorInfo (§11)."""
    return RpcError("InvalidParamsError", detail, error_data(code.upper(), PROTOCOL_DOMAIN))


class TokenBucket:
    """At most `rate` requests per second per process, with bursts of up to `rate`."""

    def __init__(self, rate: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.rate = float(rate)
        self.tokens = float(rate)
        self.clock = clock
        self.last = clock()

    def take(self) -> bool:
        now = self.clock()
        self.tokens = min(self.rate, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


def _short_connect(dsn: str) -> Connect:
    """A new short autocommit connection each time: the wait holds no pool slot (P19)."""

    @contextlib.asynccontextmanager
    async def connect() -> AsyncIterator[AsyncConnection]:
        async with await psycopg.AsyncConnection.connect(
            dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS
        ) as conn:
            yield conn

    return connect


# Reads ------------------------------------------------------------------------------------

_QUOTE_SQL = """
SELECT quote_id, run_id, order_id, conversation_id, seller_agent_id, buyer_agent_id, status,
       expires_at, closed_by
FROM quotes WHERE quote_id = %s
"""
_MESSAGE_COLUMNS = (
    "message_id, run_id, conversation_id, causation_id, from_agent, to_agent, type, body"
)


async def _one(conn: AsyncConnection, query: str, params: tuple[Any, ...]) -> dict | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchone()


async def _all(conn: AsyncConnection, query: str, params: tuple[Any, ...]) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


async def _quote(conn: AsyncConnection, quote_id: uuid.UUID) -> QuoteRow | None:
    row = await _one(conn, _QUOTE_SQL, (quote_id,))
    return None if row is None else QuoteRow(**row)


async def _a2a_card(conn: AsyncConnection, agent_id: str) -> AgentCard | None:
    """The enabled grocery or restaurant card `agent_id`, else `None` (§2)."""
    try:
        card = await directory.card(conn, agent_id)
    except directory.UnknownAgent:
        return None
    if not card.enabled or card.agent_class not in A2A_CLASSES:
        return None
    return card


async def _request_of(conn: AsyncConnection, task_id: uuid.UUID) -> dict | None:
    """The outside `quote_request` row whose future quote is `task_id`, for a task with no
    quote row yet. Its id is the `inbound_id` of an `a2a_demo` run (§11 step 3)."""
    rows = await _all(
        conn,
        "SELECT trigger->>'inbound_id' AS inbound_id FROM runs"
        " WHERE kind = %s AND trigger ? 'inbound_id'",
        (demo.RUN_KIND,),
    )
    for row in rows:
        try:
            message_id = uuid.UUID(row["inbound_id"])
        except (TypeError, ValueError):
            continue
        if ids.quote_id(message_id) == task_id:
            return await _one(
                conn,
                "SELECT message_id, conversation_id, status, to_agent FROM agent_messages"
                " WHERE message_id = %s AND from_agent = %s AND type = 'quote_request'",
                (message_id, SENDER),
            )
    return None


def _strip(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if k != "rationale"}


async def build_task(
    conn: AsyncConnection,
    agent_id: str,
    task_id: uuid.UUID,
    *,
    history_length: int | None = None,
    state: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """The A2A `Task` for quote `task_id` from the stored rows (§7, §11 `GetTask`).

    `None` when the task is unknown, its buyer is not `external:a2a-demo`, or its seller is
    not `agent_id`. `state` overrides the computed state (`returnImmediately`).
    """
    quote = await _quote(conn, task_id)
    if quote is not None:
        if quote.buyer_agent_id != SENDER or quote.seller_agent_id != agent_id:
            return None
        conversation_id = quote.conversation_id
        request = await _one(
            conn,
            "SELECT status FROM agent_messages WHERE conversation_id = %s"
            " AND type = 'quote_request' AND from_agent = %s ORDER BY created_at LIMIT 1",
            (conversation_id, SENDER),
        )
    else:
        request = await _request_of(conn, task_id)
        if request is None or request["to_agent"] != agent_id:
            return None
        conversation_id = request["conversation_id"]
    if state is None:
        try:
            state = task_state(
                quote, request["status"] if request else "failed", now or datetime.now(UTC)
            )
        except ValueError:
            # A combination with no outside state (for example a processed request with no
            # quote): fail closed, the task cannot continue.
            state = TASK_STATE_FAILED

    rows = await _all(
        conn,
        f"SELECT {_MESSAGE_COLUMNS} FROM agent_messages WHERE conversation_id = %s"
        " ORDER BY created_at, message_id",
        (conversation_id,),
    )
    messages = [AgentMessageRow(**row) for row in rows]
    offers = [m for m in messages if m.type == "offer" and m.from_agent == agent_id]
    artifacts = [
        {
            "artifactId": str(m.message_id),
            "name": "offer",
            "parts": [{"data": {"type": m.type, "body": _strip(m.body)}}],
        }
        for m in offers[-1:]
    ]
    if history_length is not None:
        messages = messages[-history_length:] if history_length else []
    return {
        "id": str(task_id),
        "contextId": str(conversation_id),
        "status": {"state": state},
        "artifacts": artifacts,
        "history": [to_a2a_message(m, quote) for m in messages],
    }


# The endpoint -----------------------------------------------------------------------------


class A2AServer:
    """The A2A routes and their per-process state (the rate limiter)."""

    def __init__(
        self,
        connect: Connect,
        *,
        token: str,
        poll_connect: Connect | None = None,
        max_request_bytes: int = 32768,
        rate_per_second: int = 10,
        send_wait_seconds: float = 10,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not token:
            raise ValueError("A2A_DEMO_TOKEN must not be empty")
        self.connect = connect
        self.poll_connect = poll_connect or connect
        self.token = token.encode()
        self.max_request_bytes = max_request_bytes
        self.send_wait_seconds = send_wait_seconds
        self.bucket = TokenBucket(rate_per_second, clock)

    @classmethod
    def from_settings(cls, connect: Connect) -> "A2AServer":
        settings = get_settings()
        dsn = settings.database_url
        return cls(
            connect,
            token=settings.a2a_demo_token,
            poll_connect=_short_connect(dsn) if dsn else None,
            max_request_bytes=settings.a2a_max_request_bytes,
            rate_per_second=settings.a2a_rate_per_second,
            send_wait_seconds=settings.a2a_send_wait_seconds,
        )

    def routes(self) -> list[BaseRoute]:
        return [
            Route("/a2a/{agent_id}/.well-known/agent-card.json", self.card, methods=["GET"]),
            Route("/a2a/{agent_id}", self.rpc, methods=["POST"]),
        ]

    # Card ---------------------------------------------------------------------------------

    async def card(self, request: Request) -> Response:
        agent_id = request.path_params["agent_id"]
        async with self.connect() as conn:
            card = await _a2a_card(conn, agent_id)
            if card is None:
                return JSONResponse({"error": "not_found"}, status_code=404)
            org = await _one(
                conn,
                "SELECT name FROM organizations WHERE 'org:' || org_id = %s",
                (card.owner_ref,),
            )
        row = AgentCardRow(
            agent_id=card.agent_id,
            agent_class=card.agent_class,
            owner_ref=card.owner_ref,
            org_name=org["name"] if org else card.owner_ref,
            config=card.config,
        )
        return JSONResponse(to_a2a_card(row, BASE_URL))

    # JSON-RPC -----------------------------------------------------------------------------

    def _authorized(self, request: Request) -> bool:
        scheme, _, given = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer":
            given = ""
        # Constant time whatever the input; an empty token never matches.
        return hmac.compare_digest(given.strip().encode(), self.token)

    async def rpc(self, request: Request) -> Response:
        if not self._authorized(request):
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > self.max_request_bytes):
            return JSONResponse({"error": "request_too_large"}, status_code=413)
        raw = await request.body()
        if len(raw) > self.max_request_bytes:
            return JSONResponse({"error": "request_too_large"}, status_code=413)
        if not self.bucket.take():
            return JSONResponse({"error": "rate_limited"}, status_code=429)

        rpc_id: str | int | None = None
        try:
            try:
                payload = json.loads(raw)
            except ValueError:
                raise RpcError("JSONParseError", "Parse error") from None
            if isinstance(payload, dict) and isinstance(payload.get("id"), str | int):
                rpc_id = payload["id"]
            try:
                call = RpcRequest.model_validate(payload)
            except ValidationError:
                raise RpcError("InvalidRequestError", "Invalid request") from None
            version = request.headers.get(VERSION_HEADER) or request.query_params.get(
                VERSION_HEADER
            )
            if version != VERSION:
                # A missing or empty value means 0.3 (Q3).
                raise RpcError(
                    "VersionNotSupportedError", f"A2A-Version {version or '0.3'} is not supported"
                )
            agent_id = request.path_params["agent_id"]
            if call.method == "SendMessage":
                result = await self.send_message(agent_id, call.params)
            elif call.method == "GetTask":
                result = await self.get_task(agent_id, call.params)
            elif call.method == "CancelTask":
                result = await self.cancel_task(agent_id, call.params)
            else:
                raise RpcError("MethodNotFoundError", f"Method {call.method!r} not found")
        except RpcError as err:
            return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "error": err.to_json()})
        except Exception:
            log.exception("A2A request failed")
            err = RpcError("InternalError", "Internal error")
            return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "error": err.to_json()})
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})

    async def _target(self, conn: AsyncConnection, agent_id: str) -> AgentCard:
        card = await _a2a_card(conn, agent_id)
        if card is None:
            raise _invalid_params(f"{agent_id} does not accept A2A requests")
        return card

    # SendMessage --------------------------------------------------------------------------

    async def send_message(self, agent_id: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            p = SendMessageParams.model_validate(params)
        except ValidationError:
            raise _invalid_params("params.message is required") from None
        message = p.message
        a2a_id = message.get("messageId")
        if not isinstance(a2a_id, str) or not a2a_id:
            raise _invalid_params("message.messageId is required")
        parts = message.get("parts")
        data_parts = (
            [x for x in parts if isinstance(x, dict) and "data" in x]
            if isinstance(parts, list)
            else []
        )
        if len(data_parts) != 1 or not isinstance(data_parts[0]["data"], dict):
            raise _invalid_params("message must have exactly one data part {type, body}")
        data = data_parts[0]["data"]
        type_ = data.get("type")
        message_id = ids.a2a_message_id(SENDER, a2a_id)
        new_task = type_ == "quote_request"

        async with self.connect() as conn:
            await self._target(conn, agent_id)
            # Step 1: a duplicate SendMessage has no second effect.
            existing = await _one(
                conn,
                "SELECT m.type, m.body, m.to_agent, t.payload->>'request_hash' AS hash"
                " FROM agent_messages m LEFT JOIN agent_traces t"
                " ON t.message_id = m.message_id AND t.kind = 'message'"
                " WHERE m.message_id = %s",
                (message_id,),
            )
            if existing is not None:
                if existing["hash"] != request_hash(data) or existing["to_agent"] != agent_id:
                    raise _invalid_params("messageId was already used for a different message")
                task_id = (
                    ids.quote_id(message_id)
                    if existing["type"] == "quote_request"
                    else uuid.UUID(existing["body"]["quote_id"])
                )
                task = await build_task(conn, agent_id, task_id)
                if task is None:
                    raise RpcError("TaskNotFoundError", "Task not found")
                return {"task": task}

            # Step 2: only quote_request, counter_offer and reject come in here.
            if type_ == "accept":
                await self._refuse_accept(conn, agent_id, message, message_id)
                raise RpcError("UnsupportedOperationError", "accept is not allowed over A2A")
            if type_ == "cancel":
                raise _invalid_params("use CancelTask")
            if type_ not in OUTSIDE_TYPES:
                code = "wrong_sender_class" if type_ in CATALOG else "unknown_type"
                raise _protocol_refused(code, f"{type_!r} cannot be sent over A2A")

            async with conn.transaction():
                ctx = await self._context(conn, agent_id, message, message_id, new_task)
                try:
                    args = from_a2a_message(params, SENDER, agent_id, ctx)
                except ValueError as exc:
                    raise _invalid_params(str(exc)) from None
                try:  # Step 6: C1-C3; a failure rolls back the run and the order.
                    check_static(args.type, args.from_agent, args.to_agent, args.body)
                except ProtocolError as exc:
                    raise _protocol_refused(exc.code, exc.detail) from None
                await outbox.send(conn, **asdict(args))  # Step 7
            task_id = ids.quote_id(message_id) if new_task else ctx.quote_id
            if task_id is None:
                raise RpcError("InternalError", "Internal error")

        if new_task and p.configuration is not None and p.configuration.returnImmediately:
            async with self.poll_connect() as conn:
                task = await build_task(conn, agent_id, task_id, state=TASK_STATE_SUBMITTED)
        else:
            await self._wait(message_id)
            async with self.poll_connect() as conn:
                task = await build_task(conn, agent_id, task_id)
        if task is None:
            raise RpcError("TaskNotFoundError", "Task not found")
        return {"task": task}

    async def _context(
        self,
        conn: AsyncConnection,
        agent_id: str,
        message: dict[str, Any],
        message_id: uuid.UUID,
        new_task: bool,
    ) -> A2AContext:
        """Steps 3-5: the ids and order fields the endpoint fills itself."""
        card = await self._target(conn, agent_id)
        if new_task:
            if message.get("contextId") or message.get("taskId"):
                raise _invalid_params("a quote_request starts a new context and task")
            return await demo.start_context(conn, message_id, card)
        quote = await self._own_quote(conn, agent_id, message.get("taskId"))
        if message.get("contextId") != str(quote.conversation_id):
            raise _invalid_params("contextId does not match the task")
        return A2AContext(
            run_id=quote.run_id,
            order_id=quote.order_id,
            conversation_id=quote.conversation_id,
            quote_id=quote.quote_id,
        )

    async def _own_quote(self, conn: AsyncConnection, agent_id: str, task_id: Any) -> QuoteRow:
        """The quote named by `task_id`, if its buyer is `external:a2a-demo` and its seller
        is `agent_id`; otherwise `TaskNotFoundError`."""
        try:
            quote = await _quote(conn, uuid.UUID(str(task_id)))
        except ValueError:
            quote = None
        if quote is None or quote.buyer_agent_id != SENDER or quote.seller_agent_id != agent_id:
            raise RpcError("TaskNotFoundError", "Task not found")
        return quote

    async def _refuse_accept(
        self,
        conn: AsyncConnection,
        agent_id: str,
        message: dict[str, Any],
        message_id: uuid.UUID,
    ) -> None:
        """An `error` trace in the conversation's lane; no row (§11 step 2). With no task of
        this buyer to name, there is no run to trace in."""
        try:
            quote = await self._own_quote(conn, agent_id, message.get("taskId"))
        except RpcError:
            return
        async with conn.transaction():
            await _TRACES.write(
                quote.run_id,
                agent_id,
                "error",
                {
                    "where": "a2a",
                    "reason": "accept_not_allowed",
                    "attempt": 0,
                    "a2a_message_id": str(message_id),
                },
                on_behalf_of=await _on_behalf_of(conn, agent_id),
                conversation_id=quote.conversation_id,
                conn=conn,
            )
        # The caller raises next, which rolls back an open transaction: keep the trace.
        await conn.commit()

    async def _wait(self, message_id: uuid.UUID) -> None:
        """Wait up to `A2A_SEND_WAIT_SECONDS` for the sent row to leave `pending` and
        `processing`, with a new short connection per poll and nothing held between."""
        deadline = time.monotonic() + self.send_wait_seconds
        while True:
            async with self.poll_connect() as conn:
                row = await _one(
                    conn, "SELECT status FROM agent_messages WHERE message_id = %s", (message_id,)
                )
            if row is None or row["status"] not in _IN_FLIGHT:
                return
            if time.monotonic() + POLL_SECONDS > deadline:
                return
            await asyncio.sleep(POLL_SECONDS)

    # GetTask and CancelTask ---------------------------------------------------------------

    async def get_task(self, agent_id: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            p = GetTaskParams.model_validate(params)
        except ValidationError:
            raise _invalid_params("params.id must be a task id") from None
        async with self.connect() as conn:
            await self._target(conn, agent_id)
            task = await build_task(conn, agent_id, p.id, history_length=p.historyLength)
        if task is None:
            raise RpcError("TaskNotFoundError", "Task not found")
        return task

    async def cancel_task(self, agent_id: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            p = CancelTaskParams.model_validate(params)
        except ValidationError:
            raise _invalid_params("params.id must be a task id") from None
        async with self.connect() as conn:
            await self._target(conn, agent_id)
            task = await build_task(conn, agent_id, p.id)
            if task is None:
                raise RpcError("TaskNotFoundError", "Task not found")
            quote = await _quote(conn, p.id)
            if quote is None or task["status"]["state"] in TERMINAL_STATES:
                # No quote yet means nothing to cancel; a terminal task stays as it is.
                raise RpcError("TaskNotCancelableError", "Task cannot be canceled")
            async with conn.transaction():
                await outbox.send(
                    conn,
                    message_id=ids.a2a_cancel_id(quote.quote_id),
                    run_id=quote.run_id,
                    conversation_id=quote.conversation_id,
                    causation_id=None,
                    from_agent=SENDER,
                    to_agent=agent_id,
                    type="cancel",
                    body={
                        "order_id": str(quote.order_id),
                        "quote_id": str(quote.quote_id),
                        "reason": "buyer_cancelled",
                    },
                )
            task = await build_task(conn, agent_id, p.id)
        if task is None:
            raise RpcError("TaskNotFoundError", "Task not found")
        return task
