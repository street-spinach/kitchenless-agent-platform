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

The owner app's queries (experience.md EX-I46) use the organisation counterpart:

*   `OrgScope(org_id)`, built by the owner API after its `user owner org:<id>` check.
*   `org_scoped(conn, scope)` returns a `Scoped` whose SQL must contain `%(org)s`, which the
    helper binds. SQL that names `quotes` or `orders` must also contain `ORG_SELLER_FILTER`,
    so it only sees rows whose seller agent belongs to the organisation.
*   `ORG_TABLES` may be read in `routes/app.py` and `owner_views.py` only through
    `org_scoped` (`test_org_tables_only_via_helper`).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from psycopg import AsyncConnection, AsyncCursor

__all__ = [
    "HOUSEHOLD_PARAM",
    "HOUSEHOLD_TABLES",
    "ORG_PARAM",
    "ORG_SELLER_FILTER",
    "ORG_TABLES",
    "UNSCOPED_REASONS",
    "HouseholdScope",
    "NoHousehold",
    "OrgScope",
    "Scoped",
    "Unscoped",
    "from_ctx",
    "from_session",
    "org_scoped",
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

# EX-I46: the tables the owner app reads, always bound to one organisation.
ORG_TABLES = ("quotes", "orders", "adapter_modes", "agent_traces")
ORG_PARAM = "org"
_ORG_PLACEHOLDER = "%(org)s"
# Required in any SQL that names `quotes` or `orders` (whitespace-insensitive).
ORG_SELLER_FILTER = (
    "seller_agent_id IN (SELECT agent_id FROM agent_cards WHERE owner_ref = 'org:' || %(org)s)"
)
_ORG_SELLER_TABLES = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+(?:ONLY\s+)?(?:quotes|orders)\b", re.IGNORECASE
)
_SPACE = re.compile(r"\s+")

# Every reason `unscoped` was called with in this process (for diagnostics only; the
# static test reads the source).
UNSCOPED_REASONS: set[str] = set()


class NoHousehold(ValueError):
    """The run or session has no household, so nothing household-owned can be read."""


@dataclass(frozen=True)
class HouseholdScope:
    household_id: str
    source: Literal["run", "session"]


@dataclass(frozen=True)
class OrgScope:
    """One organisation, after the caller passed `user owner org:<org_id>`."""

    org_id: str


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


def _bind(scope: HouseholdScope | OrgScope, sql: str, params: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(scope, OrgScope):
        return _bind_org(scope, sql, params)
    if _HOUSEHOLD_PLACEHOLDER not in sql:
        raise ValueError("scoped SQL must contain %(household)s")
    if _POSITIONAL.search(sql):
        raise ValueError("scoped SQL takes named parameters only")
    if HOUSEHOLD_PARAM in params:
        raise ValueError("the household is bound by the scope, never passed")
    return {**params, HOUSEHOLD_PARAM: scope.household_id}


def _bind_org(scope: OrgScope, sql: str, params: Mapping[str, Any]) -> dict[str, Any]:
    if _ORG_PLACEHOLDER not in sql:
        raise ValueError("org-scoped SQL must contain %(org)s")
    if _POSITIONAL.search(sql):
        raise ValueError("org-scoped SQL takes named parameters only")
    if ORG_PARAM in params or HOUSEHOLD_PARAM in params:
        raise ValueError("the organisation is bound by the scope, never passed")
    if _ORG_SELLER_TABLES.search(sql) and _SPACE.sub(" ", ORG_SELLER_FILTER) not in _SPACE.sub(
        " ", sql
    ):
        raise ValueError("SQL on quotes or orders must restrict to the org's seller agents")
    return {**params, ORG_PARAM: scope.org_id}


class Scoped:
    """Queries bound to one household or one organisation. Async connections only (agent
    calls, pages and the owner API)."""

    def __init__(self, conn: AsyncConnection, scope: HouseholdScope | OrgScope) -> None:
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


def org_scoped(conn: AsyncConnection, scope: OrgScope) -> Scoped:
    if not isinstance(scope, OrgScope) or not scope.org_id:
        raise TypeError("org_scoped() needs an OrgScope with an org id")
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
