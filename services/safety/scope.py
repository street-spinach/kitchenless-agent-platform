"""The household query helper (identity-authz §8 "Scoping helper", I20).

*   `HouseholdScope(household_id, source)` is built from the run (`from_ctx`, agent calls)
    or from the `/me` session (`from_session`, pages), never from tool arguments.
*   `scoped(conn, scope)` returns a `Scoped` wrapper whose `fetch(sql, **params)` and
    `execute(sql, **params)` bind `%(household)s` to the scope's household. The SQL must
    contain `%(household)s`, use named parameters only, and may not pass `household`
    itself.
*   `unscoped(conn, reason)` is the plain wrapper for code with no household context (the
    callers classified in §8). It forwards to the connection unchanged and records
    `reason`; `tests/unit/test_scope.py` checks that every such place is named in its
    allowlist.

Every read or write of a table in `HOUSEHOLD_TABLES` goes through one of the two
(`test_scoped_tables_only_via_helper`).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from psycopg import AsyncConnection, AsyncCursor

__all__ = [
    "HOUSEHOLD_PARAM",
    "HOUSEHOLD_TABLES",
    "UNSCOPED_REASONS",
    "HouseholdScope",
    "NoHousehold",
    "Scoped",
    "Unscoped",
    "from_ctx",
    "from_session",
    "scoped",
    "unscoped",
]

# §8, with the agents (#186) and experience (#271) amendments.
HOUSEHOLD_TABLES = frozenset(
    {
        "orders",
        "order_items",
        "approvals",
        "approval_rules",
        "budgets",
        "member_constraints",
        "pantry_events",
        "household_members",
        "household_addresses",
        "agent_grants",
        "consent_links",
        "audit_events",
        "household_rules",
        "join_codes",
        "onboarding_proposals",
        "held_messages",
        "whatsapp_sends",
        "reminders",
        "meal_reactions",
    }
)

HOUSEHOLD_PARAM = "household"
_HOUSEHOLD_PLACEHOLDER = "%(household)s"
# A positional placeholder: `%s` not escaped as `%%s`.
_POSITIONAL = re.compile(r"(?<!%)%s")

# Every reason `unscoped` was called with in this process (for diagnostics only; the
# static test reads the source).
UNSCOPED_REASONS: set[str] = set()


class NoHousehold(ValueError):
    """The run or session has no household, so nothing household-owned can be read."""


@dataclass(frozen=True)
class HouseholdScope:
    household_id: str
    source: Literal["run", "session"]


class _RunContext(Protocol):
    @property
    def household_id(self) -> str | None: ...


class _Session(Protocol):
    @property
    def household_id(self) -> str | None: ...


def from_ctx(ctx: _RunContext) -> HouseholdScope:
    """The scope of an agent call: the run's household (`CallContext.household_id`)."""
    if not ctx.household_id:
        raise NoHousehold("the run has no household")
    return HouseholdScope(ctx.household_id, "run")


def from_session(session: _Session) -> HouseholdScope:
    """The scope of a `/me` page: the signed-in session's household."""
    if not session.household_id:
        raise NoHousehold("the session has no household")
    return HouseholdScope(session.household_id, "session")


def _bind(scope: HouseholdScope, sql: str, params: Mapping[str, Any]) -> dict[str, Any]:
    if _HOUSEHOLD_PLACEHOLDER not in sql:
        raise ValueError("scoped SQL must contain %(household)s")
    if _POSITIONAL.search(sql):
        raise ValueError("scoped SQL takes named parameters only")
    if HOUSEHOLD_PARAM in params:
        raise ValueError("the household is bound by the scope, never passed")
    return {**params, HOUSEHOLD_PARAM: scope.household_id}


class Scoped:
    """Queries bound to one household. Async connections only (agent calls and pages)."""

    def __init__(self, conn: AsyncConnection, scope: HouseholdScope) -> None:
        self._conn = conn
        self.scope = scope

    async def execute(self, sql: str, **params: Any) -> AsyncCursor[Any]:
        return await self._conn.execute(sql, _bind(self.scope, sql, params))

    async def fetch(self, sql: str, **params: Any) -> list[Any]:
        cur = await self._conn.execute(sql, _bind(self.scope, sql, params))
        return await cur.fetchall()


def scoped(conn: AsyncConnection, scope: HouseholdScope) -> Scoped:
    if not isinstance(scope, HouseholdScope):
        raise TypeError("scoped() needs a HouseholdScope")
    return Scoped(conn, scope)


class Unscoped:
    """A plain wrapper: `execute` and `cursor` go to the connection unchanged. Works for
    sync and async psycopg connections (an async one returns what it always does)."""

    def __init__(self, conn: Any, reason: str) -> None:
        self._conn = conn
        self.reason = reason

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        return self._conn.execute(*args, **kwargs)

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        return self._conn.cursor(*args, **kwargs)


def unscoped(conn: Any, reason: str) -> Unscoped:
    """A query wrapper for code with no household context. `reason` is a literal string
    named in `tests/unit/test_scope.py`'s allowlist."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("unscoped() needs a reason")
    UNSCOPED_REASONS.add(reason)
    return Unscoped(conn, reason)
