"""The `api` service (architecture item 32): `/health`, the reserved routes, the simulated
WhatsApp routes, `/runs`, the real WhatsApp webhook, the control panel routes,
`POST /hello/run`, and the consumer that sends `whatsapp_out` messages.

Run with `python -m services.api.main`. It serves on port 8000.
"""

import asyncio
import contextlib
import logging

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from services.api.connect import Connect, pool_connect
from services.api.routes import hello, internal, panel, reserved, runs, whatsapp
from services.api.whatsapp.sender import GraphSender, Sender
from services.common import db
from services.common import logging as app_logging
from services.common import runs as common_runs
from services.common.config import get_settings
from services.common.consumer import consume_forever

SERVICE = "api"
HOST = "api"
PORT = 8000

log = logging.getLogger(__name__)


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": SERVICE})


def create_app(connect: Connect = pool_connect) -> Starlette:
    app = Starlette(
        routes=[
            Route("/health", health),
            *reserved.routes,
            *internal.routes,
            *runs.routes,
            *whatsapp.routes,
            *panel.routes,
            *hello.routes,
        ]
    )
    app.state.connect = connect
    return app


app = create_app()


async def serve() -> None:
    """Run the HTTP server and the `api` consumer until SIGTERM."""
    pool = db.get_pool()
    await pool.open()
    try:
        server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=PORT, log_config=None))
        stop = asyncio.Event()
        consumer = asyncio.create_task(
            consume_forever(
                HOST,
                Sender(send_real=GraphSender.from_settings(get_settings())),
                finish_run=common_runs.finish,
                pool=pool,
                stop=stop,
            )
        )

        async def stop_server() -> None:
            # The consumer's SIGTERM handler sets `stop`; a consumer that dies stops the server.
            waits = [asyncio.create_task(stop.wait()), consumer]
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            waits[0].cancel()
            server.should_exit = True

        stopper = asyncio.create_task(stop_server())
        try:
            await server.serve()
        finally:
            stop.set()
            [res] = await asyncio.gather(consumer, return_exceptions=True)
            if isinstance(res, BaseException) and not isinstance(res, asyncio.CancelledError):
                log.error("api consumer failed", exc_info=res)
            stopper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopper
    finally:
        await pool.close()


def main() -> None:
    app_logging.configure(SERVICE)
    asyncio.run(serve())


if __name__ == "__main__":
    main()
