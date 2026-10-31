"""The `agents` service skeleton (the `/health` part of architecture item 27).

Run with `python -m services.agents.host`. It serves on port 8003. `/health` reports whether
Postgres is reachable. The card count and the message backlog join it once the agent
directory and the outbox exist (build step 2).
"""

from collections.abc import Awaitable, Callable

import psycopg
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from services.common import logging as app_logging
from services.common.config import get_settings

SERVICE = "agents"
PORT = 8003
DB_TIMEOUT_SECONDS = 2


async def postgres_reachable() -> bool:
    """True if a `SELECT 1` succeeds on `DATABASE_URL` within the timeout."""
    try:
        dsn = get_settings().database_url
        if not dsn:
            return False
        async with await psycopg.AsyncConnection.connect(
            dsn, connect_timeout=DB_TIMEOUT_SECONDS
        ) as conn:
            await conn.execute("SELECT 1")
        return True
    except Exception:
        return False


def create_app(check_db: Callable[[], Awaitable[bool]] = postgres_reachable) -> Starlette:
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": SERVICE, "postgres": await check_db()})

    return Starlette(routes=[Route("/health", health)])


app = create_app()


def main() -> None:
    app_logging.configure(SERVICE)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_config=None)


if __name__ == "__main__":
    main()
