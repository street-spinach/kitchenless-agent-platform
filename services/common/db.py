"""Database connections (architecture item 3).

One async pool per process with `max_size=5`. `LISTEN` uses its own autocommit connection
outside the pool, so a long-lived listener never holds a pool slot.
"""

from psycopg import AsyncConnection, sql
from psycopg_pool import AsyncConnectionPool

from services.common.config import get_settings

POOL_MAX_SIZE = 5

_pool: AsyncConnectionPool | None = None


def _dsn() -> str:
    dsn = get_settings().database_url
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")
    return dsn


def get_pool() -> AsyncConnectionPool:
    """The process-wide pool. It is created closed; `await get_pool().open()` at startup."""
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(_dsn(), min_size=1, max_size=POOL_MAX_SIZE, open=False)
    return _pool


async def listen_connection(channel: str) -> AsyncConnection:
    """A dedicated autocommit connection that has run `LISTEN <channel>`."""
    conn = await AsyncConnection.connect(_dsn(), autocommit=True)
    await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))
    return conn
