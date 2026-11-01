"""The `api` service skeleton (architecture item 32): `/health` and the reserved routes.

Run with `python -m services.api.main`. It serves on port 8000.
"""

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from services.api.routes import reserved
from services.common import logging as app_logging

SERVICE = "api"
PORT = 8000


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": SERVICE})


def create_app() -> Starlette:
    return Starlette(routes=[Route("/health", health), *reserved.routes])


app = create_app()


def main() -> None:
    app_logging.configure(SERVICE)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_config=None)


if __name__ == "__main__":
    main()
