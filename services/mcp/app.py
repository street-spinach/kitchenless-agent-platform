"""The `mcp` service skeleton (the `/health` part of architecture item 40).

Run with `python -m services.mcp.app`. It serves on port 8002.
"""

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from services.common import logging as app_logging

SERVICE = "mcp"
PORT = 8002


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": SERVICE})


def create_app() -> Starlette:
    return Starlette(routes=[Route("/health", health)])


app = create_app()


def main() -> None:
    app_logging.configure(SERVICE)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_config=None)


if __name__ == "__main__":
    main()
