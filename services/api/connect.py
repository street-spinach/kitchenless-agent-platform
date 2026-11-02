"""How `api` code gets a database connection.

Routes read `request.app.state.connect`; tests pass their own. The default borrows from
the process pool, which `services.api.main.serve` opens at startup.
"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from psycopg import AsyncConnection

from services.common import db

Connect = Callable[[], AbstractAsyncContextManager[AsyncConnection]]


def pool_connect() -> AbstractAsyncContextManager[AsyncConnection]:
    return db.get_pool().connection()
