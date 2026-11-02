"""The `agents` service (architecture item 27, amended by agent-protocol A20 / P18).

Run with `python -m services.agents.host`. At startup it loads the enabled cards with
`host_process='agents'` and builds a dispatcher with one handler per class. It then runs
`consume_forever("agents", dispatch)`, `sweep_forever()` and `/health` on port 8003.

*   `dispatch` routes each message by `to_agent` to the handler of that card's class. A
    message for a card this host did not load, or for a class with no handler, raises
    `NoHandler`: an ordinary step failure, retried and then poisoned.
*   The consumer is given `finish_run=runs.finish`, so an exception after the last attempt
    finishes the run with `status=error`.
*   `/health` reports Postgres reachability, the loaded card count and the message backlog
    per host (`pending`, `processing`, `oldest_pending_seconds`), excluding `external`.

Mounting the A2A routes (agent-protocol P18, P19) belongs to issue #77.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

import psycopg
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from services.agents import sweeper
from services.agents.consumer import graph as consumer_graph
from services.agents.grocery import handler as grocery_handler
from services.agents.household import graph as household_graph
from services.agents.restaurant import handler as restaurant_handler
from services.common import db, directory, runs
from services.common import logging as app_logging
from services.common.config import get_settings
from services.common.consumer import Inbound, StepResult, consume_forever
from services.common.directory import AgentCard

__all__ = [
    "BACKLOG_HOSTS",
    "HANDLERS",
    "HOST",
    "ClassHandler",
    "Dispatcher",
    "NoHandler",
    "create_app",
    "read_backlog",
]

log = logging.getLogger(__name__)

SERVICE = "agents"
HOST = "agents"
PORT = 8003
DB_TIMEOUT_SECONDS = 2
EXTERNAL = "external"
# Hosts always listed in the backlog, with zeros when they have no rows.
BACKLOG_HOSTS = ("agents", "api")

ClassHandler = Callable[[Inbound, AgentCard], Awaitable[StepResult]]
Backlog = dict[str, dict[str, Any]]

# One handler per agent class (`handle(msg, card) -> StepResult`, architecture items 28-30).
# The graphs and business handlers register here as their build steps land.
HANDLERS: dict[str, ClassHandler] = {
    "consumer": consumer_graph.handle,
    "household": household_graph.handle,
    "grocery": grocery_handler.handle,
    "restaurant": restaurant_handler.handle,
}

_BACKLOG_SQL = """
SELECT to_host,
       count(*) FILTER (WHERE status = 'pending') AS pending,
       count(*) FILTER (WHERE status = 'processing') AS processing,
       floor(extract(epoch FROM now() - min(created_at) FILTER (WHERE status = 'pending')))
         AS oldest_pending_seconds
FROM agent_messages
WHERE status IN ('pending', 'processing') AND to_host <> %s
GROUP BY to_host
"""


class NoHandler(LookupError):
    """This host has no handler for the message's `to_agent`."""


class Dispatcher:
    """Routes a claimed message by `to_agent` to its card's class handler."""

    def __init__(self, cards: Iterable[AgentCard], handlers: Mapping[str, ClassHandler]) -> None:
        self.cards: dict[str, AgentCard] = {c.agent_id: c for c in cards}
        self.handlers: dict[str, ClassHandler] = {}
        for card in self.cards.values():
            handler = handlers.get(card.agent_class)
            if handler is None:
                log.warning("no handler for class %s (card %s)", card.agent_class, card.agent_id)
                continue
            self.handlers[card.agent_id] = handler

    async def __call__(self, msg: Inbound) -> StepResult:
        to_agent = msg.row.to_agent
        handler = self.handlers.get(to_agent)
        if handler is None:
            raise NoHandler(f"no handler on host {HOST!r} for {to_agent!r}")
        return await handler(msg, self.cards[to_agent])


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


async def backlog(conn: psycopg.AsyncConnection) -> Backlog:
    """`{<host>: {pending, processing, oldest_pending_seconds}}`, excluding `external`.

    `oldest_pending_seconds` is the age of the oldest `pending` row, `None` when there is
    none.
    """
    result: Backlog = {
        h: {"pending": 0, "processing": 0, "oldest_pending_seconds": None} for h in BACKLOG_HOSTS
    }
    cur = await conn.execute(_BACKLOG_SQL, (EXTERNAL,))
    for host, pending, processing, oldest in await cur.fetchall():
        result[host] = {
            "pending": pending,
            "processing": processing,
            "oldest_pending_seconds": None if oldest is None else int(oldest),
        }
    return result


async def read_backlog() -> Backlog | None:
    """The backlog over a new short connection; `None` if Postgres cannot be read."""
    try:
        dsn = get_settings().database_url
        if not dsn:
            return None
        async with await psycopg.AsyncConnection.connect(
            dsn, connect_timeout=DB_TIMEOUT_SECONDS
        ) as conn:
            return await backlog(conn)
    except Exception:
        log.warning("backlog read failed", exc_info=True)
        return None


def create_app(
    check_db: Callable[[], Awaitable[bool]] = postgres_reachable,
    *,
    get_backlog: Callable[[], Awaitable[Backlog | None]] = read_backlog,
    card_count: Callable[[], int] = lambda: 0,
) -> Starlette:
    async def health(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "service": SERVICE,
                "postgres": await check_db(),
                "cards": card_count(),
                "backlog": await get_backlog(),
            }
        )

    return Starlette(routes=[Route("/health", health)])


async def serve(handlers: Mapping[str, ClassHandler] = HANDLERS) -> None:
    """Load the cards, then run the consumer, the sweeper and `/health` until SIGTERM."""
    pool = db.get_pool()
    await pool.open()
    try:
        async with pool.connection() as conn:
            cards = await directory.hosted(conn, HOST)
        log.info("loaded %d cards for host %s", len(cards), HOST)
        dispatch = Dispatcher(cards, handlers)
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(card_count=lambda: len(cards)),
                host="0.0.0.0",
                port=PORT,
                log_config=None,
            )
        )
        stop = asyncio.Event()
        loops = [
            asyncio.create_task(
                consume_forever(HOST, dispatch, finish_run=runs.finish, pool=pool, stop=stop)
            ),
            asyncio.create_task(sweeper.sweep_forever(pool=pool, stop=stop)),
        ]

        async def stop_server() -> None:
            # The loops' SIGTERM handler sets `stop`; a loop that dies also stops the host.
            waits = [asyncio.create_task(stop.wait()), *loops]
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            waits[0].cancel()
            server.should_exit = True

        stopper = asyncio.create_task(stop_server())
        try:
            await server.serve()
        finally:
            stop.set()
            results = await asyncio.gather(*loops, return_exceptions=True)
            for res in results:
                if isinstance(res, BaseException) and not isinstance(res, asyncio.CancelledError):
                    log.error("host loop failed", exc_info=res)
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
