"""The `mcp` service (architecture item 40 and §5).

One Starlette app mounts the five MCP servers at `/mcp/<area>` over streamable HTTP, plus
`/health`. A middleware stores each request's `Authorization` bearer token for
`meta.current_token()`.

Run with `python -m services.mcp.app`. It serves on port 8002.
"""

import contextlib
from collections.abc import AsyncIterator

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from services.common import logging as app_logging
from services.mcp import meta
from services.mcp.delivery.server import server as delivery
from services.mcp.grocery.server import server as grocery
from services.mcp.household.server import server as household
from services.mcp.knowledge.server import server as knowledge
from services.mcp.restaurant.server import server as restaurant

SERVICE = "mcp"
PORT = 8002

SERVERS: dict[str, MCPServer] = {
    "grocery": grocery,
    "restaurant": restaurant,
    "delivery": delivery,
    "household": household,
    "knowledge": knowledge,
}


class BearerTokenMiddleware:
    """Stores the request's `Authorization: Bearer` token for `meta.current_token()`."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = None
        for name, value in scope["headers"]:
            if name == b"authorization":
                scheme, _, credentials = value.decode("latin-1").partition(" ")
                if scheme.lower() == "bearer" and credentials.strip():
                    token = credentials.strip()
                break
        reset = meta.set_current_token(token)
        try:
            await self.app(scope, receive, send)
        finally:
            meta.reset_current_token(reset)


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": SERVICE})


def create_app() -> Starlette:
    routes: list[BaseRoute] = [Route("/health", health)]
    # The app is reached by service name inside the Compose network, not only on
    # localhost, so the SDK's localhost-only Host check is off.
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    for area, server in SERVERS.items():
        sub = server.streamable_http_app(
            streamable_http_path=f"/mcp/{area}", transport_security=security
        )
        routes.extend(sub.routes)
    managers = [server.session_manager for server in SERVERS.values()]

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as stack:
            for manager in managers:
                await stack.enter_async_context(manager.run())
            yield

    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(BearerTokenMiddleware)
    return app


app = create_app()


def main() -> None:
    app_logging.configure(SERVICE)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_config=None)


if __name__ == "__main__":
    main()
