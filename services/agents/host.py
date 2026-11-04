"""The `agents` service (architecture item 27, amended by agent-protocol A20 / P18).

Run with `python -m services.agents.host`. At startup it loads the enabled cards with
`host_process='agents'` and builds a dispatcher with one handler per class. It then runs
`consume_forever("agents", dispatch)`, `sweep_forever()` and `/health` on port 8003.

*   `dispatch` routes each message by `to_agent` to the handler of that card's class. A
    message for a card this host has not loaded first reloads the hosted cards once, so
    cards seeded after startup are picked up without a restart (#59). A card still not
    loaded, or a class with no handler, raises `NoHandler`: an ordinary step failure,
    retried and then poisoned.
*   A `GrantError` from a step (identity-authz I28, §11): a household agent's ends the run
    with its reason. A consumer agent's, on a message from the household agent, skips that
    step with the trace note `MEMBER_SKIPPED`: a `constraints_request` is answered with
    empty lists, so the household stops waiting, and that member's preferences are not
    used. The household reads every member's allergies and diets itself. A consumer agent's
    on any other message (the member's own chat) ends the run, as it has no household run
    to continue.
*   The consumer is given `finish_run=runs.finish`, so an exception after the last attempt
    finishes the run with `status=error`.
*   `/health` reports Postgres reachability, the loaded card count and the message backlog
    per host (`pending`, `processing`, `oldest_pending_seconds`), excluding `external`. It
    also reports `model_mode`, `model_name` and `model_business_name` (evals E-A10), which
    `infra/eval-mode.sh` and the eval runner read to confirm the mode (evals EG6).

*   The A2A routes (agent-protocol P18, P19) are mounted on the same app and port.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any

import psycopg
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from services.agents import sweeper
from services.agents.a2a.server import A2AServer
from services.agents.consumer import graph as consumer_graph
from services.agents.delivery import handler as delivery_handler
from services.agents.grocery import handler as grocery_handler
from services.agents.household import graph as household_graph
from services.agents.restaurant import handler as restaurant_handler
from services.common import db, directory, ids, runs
from services.common import logging as app_logging
from services.common.config import Settings, get_settings
from services.common.consumer import Inbound, Outgoing, RunFinish, StepResult, consume_forever
from services.common.directory import AgentCard
from services.identity.errors import GrantError

__all__ = [
    "BACKLOG_HOSTS",
    "HANDLERS",
    "HOST",
    "MEMBER_SKIPPED",
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
# The trace note of a consumer step skipped for want of a grant (identity-authz I28).
MEMBER_SKIPPED = "member without consent: preferences skipped"
# Hosts always listed in the backlog, with zeros when they have no rows.
BACKLOG_HOSTS = ("agents", "api")

ClassHandler = Callable[[Inbound, AgentCard], Awaitable[StepResult]]
CardLoader = Callable[[], Awaitable[Iterable[AgentCard]]]
Backlog = dict[str, dict[str, Any]]

# One handler per agent class (`handle(msg, card) -> StepResult`, architecture items 28-30).
# The graphs and business handlers register here as their build steps land.
HANDLERS: dict[str, ClassHandler] = {
    "consumer": consumer_graph.handle,
    "household": household_graph.handle,
    "grocery": grocery_handler.handle,
    "restaurant": restaurant_handler.handle,
    "delivery": delivery_handler.handle,
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


def _skip_consumer_step(msg: Inbound, card: AgentCard) -> StepResult:
    """The step of a consumer agent with no active grant, inside a household run. Nothing of
    the member's is read or sent: the `constraints` reply is empty, and only frees the
    household agent, which waits for every member (identity-authz §11, I-CUJ 11)."""
    row = msg.row
    messages: list[Outgoing] = []
    if row.type == "constraints_request":
        messages.append(
            Outgoing(
                message_id=ids.message_id(row.message_id, 0),
                from_agent=card.agent_id,
                to_agent=row.from_agent,
                type="constraints",
                body={
                    "order_id": str(row.body["order_id"]),
                    "allergens": [],
                    "diets": [],
                    "dislikes": [],
                },
                conversation_id=row.conversation_id,
                causation_id=row.message_id,
            )
        )
    note = {"node": row.type, "choice": "skipped", "explanation": MEMBER_SKIPPED}
    return StepResult(messages=messages, decisions=[note])


def _on_grant_error(msg: Inbound, card: AgentCard, exc: GrantError) -> StepResult:
    """Identity-authz I28. Other classes hold no grant and re-raise, under the poison rule."""
    if card.agent_class == "consumer":
        if ids.parse_agent_id(msg.row.from_agent)[0] == "household":
            return _skip_consumer_step(msg, card)
    elif card.agent_class != "household":
        raise exc
    return StepResult(finish=RunFinish("error", exc.reason))


class Dispatcher:
    """Routes a claimed message by `to_agent` to its card's class handler.

    `reload`, when given, re-reads the hosted cards; it runs once per message whose
    `to_agent` is not among the loaded cards.
    """

    def __init__(
        self,
        cards: Iterable[AgentCard],
        handlers: Mapping[str, ClassHandler],
        reload: CardLoader | None = None,
    ) -> None:
        self._class_handlers = handlers
        self._reload = reload
        self.cards: dict[str, AgentCard] = {}
        self.handlers: dict[str, ClassHandler] = {}
        self._load(cards)

    def _load(self, cards: Iterable[AgentCard]) -> None:
        self.cards = {c.agent_id: c for c in cards}
        self.handlers = {}
        for card in self.cards.values():
            handler = self._class_handlers.get(card.agent_class)
            if handler is None:
                log.warning("no handler for class %s (card %s)", card.agent_class, card.agent_id)
                continue
            self.handlers[card.agent_id] = handler

    async def __call__(self, msg: Inbound) -> StepResult:
        to_agent = msg.row.to_agent
        if to_agent not in self.cards and self._reload is not None:
            self._load(await self._reload())
            log.info("reloaded %d cards for host %s", len(self.cards), HOST)
        handler = self.handlers.get(to_agent)
        if handler is None:
            raise NoHandler(f"no handler on host {HOST!r} for {to_agent!r}")
        card = self.cards[to_agent]
        try:
            return await handler(msg, card)
        except GrantError as exc:
            return _on_grant_error(msg, card, exc)


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
    settings: Settings | None = None,
    get_backlog: Callable[[], Awaitable[Backlog | None]] = read_backlog,
    card_count: Callable[[], int] = lambda: 0,
    a2a_routes: Sequence[BaseRoute] = (),
) -> Starlette:
    """The `/health` route plus `a2a_routes`; `settings` defaults to `get_settings()`."""

    async def health(request: Request) -> JSONResponse:
        s = settings if settings is not None else get_settings()
        return JSONResponse(
            {
                "status": "ok",
                "service": SERVICE,
                "postgres": await check_db(),
                "cards": card_count(),
                "backlog": await get_backlog(),
                # An unset MODEL_MODE is stub, as in `ModelClient` (agents/common/model.py).
                "model_mode": s.model_mode or "stub",
                "model_name": s.model_name,
                "model_business_name": s.model_business_name,
            }
        )

    return Starlette(routes=[Route("/health", health), *a2a_routes])


async def serve(handlers: Mapping[str, ClassHandler] = HANDLERS) -> None:
    """Load the cards, then run the consumer, the sweeper and `/health` until SIGTERM."""
    pool = db.get_pool()
    await pool.open()
    try:

        async def load_cards() -> list[AgentCard]:
            async with pool.connection() as conn:
                return await directory.hosted(conn, HOST)

        cards = await load_cards()
        log.info("loaded %d cards for host %s", len(cards), HOST)
        dispatch = Dispatcher(cards, handlers, reload=load_cards)
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(
                    card_count=lambda: len(dispatch.cards),
                    a2a_routes=A2AServer.from_settings(pool.connection).routes(),
                ),
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
