"""The `mcp` app mounts five MCP servers and passes each request's token (item 40)."""

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator

import httpx2
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer
from starlette.routing import Route

from services.mcp import app as mcp_app
from services.mcp import meta

AREAS = ["grocery", "restaurant", "delivery", "household", "knowledge"]


def test_app_mounts_five_servers() -> None:
    assert list(mcp_app.SERVERS) == AREAS
    paths = {r.path for r in mcp_app.create_app().routes if isinstance(r, Route)}
    assert paths == {"/health", *(f"/mcp/{area}" for area in AREAS)}


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Serve the app over HTTP, with a test `whoami` tool on every area."""
    servers = {area: MCPServer(name=area) for area in AREAS}
    for server in servers.values():

        @server.tool()
        async def whoami() -> str:
            return json.dumps({"token": meta.current_token()})

    monkeypatch.setattr(mcp_app, "SERVERS", servers)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(mcp_app.create_app(), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "mcp app did not start"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_each_request_carries_its_own_bearer_token(served: str) -> None:
    token = {"value": "token-1"}

    async def set_header(request: httpx2.Request) -> None:
        request.headers["Authorization"] = f"Bearer {token['value']}"

    async def whoami(session: ClientSession) -> str | None:
        result = await session.call_tool("whoami", {})
        return json.loads(result.content[0].text)["token"]

    async def go() -> list[str | None]:
        seen = []
        async with httpx2.AsyncClient(event_hooks={"request": [set_header]}) as http:
            for area in AREAS:
                url = f"{served}/mcp/{area}"
                async with streamable_http_client(url, http_client=http) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        token["value"] = f"{area}-1"
                        seen.append(await whoami(session))
                        # A second request on the same session carries its own token.
                        token["value"] = f"{area}-2"
                        seen.append(await whoami(session))
        return seen

    assert asyncio.run(go()) == [f"{area}-{n}" for area in AREAS for n in (1, 2)]


def test_serve_opens_the_pool_and_runs_courier_progression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression (#59): `mcp` never opened its pool or started the mock courier loop, so on
    # the stack every database tool failed and no mock delivery reached `delivered`.
    events: list[str] = []

    class FakePool:
        async def open(self) -> None:
            events.append("pool_open")

        async def close(self) -> None:
            events.append("pool_close")

    started = asyncio.Event()

    async def progress() -> None:
        events.append("progress_start")
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("progress_cancelled")
            raise

    class FakeServer:
        def __init__(self, config: uvicorn.Config) -> None:
            assert config.app is mcp_app.app

        async def serve(self) -> None:
            await asyncio.wait_for(started.wait(), timeout=5)
            events.append("served")

    monkeypatch.setattr(mcp_app.db, "get_pool", lambda: FakePool())
    monkeypatch.setattr(mcp_app.uvicorn, "Server", FakeServer)

    asyncio.run(mcp_app.serve(progress))

    assert events == [
        "pool_open",
        "progress_start",
        "served",
        "progress_cancelled",
        "pool_close",
    ]
