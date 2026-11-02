"""`call_tool` against a fake MCP server over streamable HTTP (architecture item 23)."""

import asyncio
import json
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent
from starlette.applications import Starlette
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from services.common import mcp_client
from services.common.mcp_client import AdapterUnavailable, ToolRefused, call_tool
from services.common.run_context import RunContext
from services.mcp.meta import tool_error

TOKEN = "token-abc"
CTX = RunContext(run_id=uuid.uuid4(), agent_id="household:us-1", conversation_id=uuid.uuid4())
ARGS = {"idempotency_key": "idem-1", "amount_cents": 1200}


@dataclass
class Fake:
    """What the fake server does on each `tools/call`, and what it saw."""

    script: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def next(self) -> str:
        return self.script.pop(0) if self.script else "ok"


class Recorder:
    """Records every `tools/call` request and answers `http500` steps with a 500."""

    def __init__(self, app: ASGIApp, fake: Fake) -> None:
        self.app = app
        self.fake = fake

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        messages: list[Message] = []
        body = b""
        while True:
            message = await receive()
            messages.append(message)
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        payload = json.loads(body) if body else {}
        if payload.get("method") == "tools/call":
            headers = {k.decode(): v.decode() for k, v in scope["headers"]}
            step = self.fake.next()
            self.fake.calls.append({"headers": headers, "params": payload["params"], "step": step})
            if step == "http500":
                await send({"type": "http.response.start", "status": 500, "headers": []})
                await send({"type": "http.response.body", "body": b"boom"})
                return

        async def replay() -> Message:
            return messages.pop(0) if messages else await receive()

        await self.app(scope, replay, send)


def _result(step: str) -> CallToolResult:
    if step == "refused":
        return tool_error("refused", "over_budget")
    if step == "unavailable_retryable":
        return tool_error("unavailable", "adapter_5xx", retryable=True)
    if step == "unavailable":
        return tool_error("unavailable", "adapter_4xx", retryable=False)
    if step == "malformed":
        return CallToolResult(content=[TextContent(type="text", text="oops")], is_error=True)
    return CallToolResult(
        content=[TextContent(type="text", text="{}")],
        structured_content={"state": "authorized"},
    )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[Fake]:
    state = Fake()
    server = MCPServer(name="household")

    @server.tool()
    async def authorize_spend(idempotency_key: str, amount_cents: int) -> CallToolResult:
        step = state.calls[-1]["step"]
        if step == "slow":
            await asyncio.sleep(5)
        return _result(step)

    sub = server.streamable_http_app(streamable_http_path="/mcp/household")
    app = Starlette(routes=sub.routes, lifespan=lambda app: server.session_manager.run())
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    uv = uvicorn.Server(
        uvicorn.Config(Recorder(app, state), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not uv.started:
        assert time.monotonic() < deadline, "fake MCP server did not start"
        time.sleep(0.05)
    monkeypatch.setattr(mcp_client, "BASE_URL", f"http://127.0.0.1:{port}")
    try:
        yield state
    finally:
        uv.should_exit = True
        thread.join(timeout=10)


def _call() -> dict[str, Any]:
    return asyncio.run(call_tool("household", "authorize_spend", ARGS, ctx=CTX, token=TOKEN))


def test_token_sent_as_bearer_header_not_meta(fake: Fake) -> None:
    assert _call() == {"state": "authorized"}
    [call] = fake.calls
    assert call["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in json.dumps(call["params"])


def test_meta_carries_run_agent_conversation_attempt(fake: Fake) -> None:
    fake.script = ["unavailable_retryable", "ok"]
    assert _call() == {"state": "authorized"}
    metas = [call["params"]["_meta"] for call in fake.calls]
    for attempt, meta in enumerate(metas, start=1):
        assert meta["run_id"] == str(CTX.run_id)
        assert meta["agent_id"] == CTX.agent_id
        assert meta["conversation_id"] == str(CTX.conversation_id)
        assert meta["attempt"] == attempt
    assert len(metas) == 2


def test_refused_raises_tool_refused_with_reason_no_retry(fake: Fake) -> None:
    fake.script = ["refused", "ok"]
    with pytest.raises(ToolRefused) as exc:
        _call()
    assert exc.value.reason == "over_budget"
    assert len(fake.calls) == 1


def test_retryable_unavailable_retried_once_same_idempotency_key(fake: Fake) -> None:
    fake.script = ["unavailable_retryable", "unavailable_retryable", "ok"]
    with pytest.raises(AdapterUnavailable) as exc:
        _call()
    assert exc.value.reason == "adapter_5xx"
    assert len(fake.calls) == 2
    assert [c["params"]["arguments"] for c in fake.calls] == [ARGS, ARGS]


def test_non_retryable_unavailable_raises_without_retry(fake: Fake) -> None:
    fake.script = ["unavailable", "ok"]
    with pytest.raises(AdapterUnavailable) as exc:
        _call()
    assert exc.value.reason == "adapter_4xx"
    assert len(fake.calls) == 1


def test_transport_timeout_retried_then_adapter_unavailable(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_client, "TIMEOUT_SECONDS", 0.5)
    fake.script = ["slow", "slow", "ok"]
    with pytest.raises(AdapterUnavailable) as exc:
        _call()
    assert exc.value.reason == "transport"
    assert len(fake.calls) == 2
    assert [c["params"]["arguments"] for c in fake.calls] == [ARGS, ARGS]


def test_transport_http_5xx_retried_then_succeeds(fake: Fake) -> None:
    fake.script = ["http500", "ok"]
    assert _call() == {"state": "authorized"}
    assert [c["params"]["_meta"]["attempt"] for c in fake.calls] == [1, 2]


def test_connection_error_retried_then_adapter_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(mcp_client, "BASE_URL", f"http://127.0.0.1:{port}")
    with pytest.raises(AdapterUnavailable) as exc:
        _call()
    assert exc.value.reason == "transport"


def test_malformed_error_result_raises_adapter_unavailable(fake: Fake) -> None:
    fake.script = ["malformed", "ok"]
    with pytest.raises(AdapterUnavailable) as exc:
        _call()
    assert exc.value.reason == "malformed_error"
    assert len(fake.calls) == 1
