"""`audit_events` writer (identity-authz §14 and I10).

`record` inserts one row; the table is insert-only. `kind` must be one of I11's kinds, and
`detail` may not carry anything that looks like a secret: any key, at any depth, whose
lower-cased name contains `token`, `secret`, `link` or `password` is refused.

`record` takes a sync or an async psycopg connection. With an `AsyncConnection` it returns
a coroutine to await; with a `Connection` it writes before returning.
"""

import uuid
from collections.abc import Coroutine, Mapping
from typing import Any, overload

import psycopg
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

__all__ = ["FORBIDDEN_KEY_PARTS", "KINDS", "record"]

# The kinds in migrations/0010_identity.sql (I11), each with one writer (§14).
KINDS = frozenset(
    {
        "user_onboarded",
        "agent_client_created",
        "link_issued",
        "link_used",
        "link_rejected",
        "consent_granted",
        "consent_revoked",
        "grant_expired",
        "band_changed",
        "owner_rule_changed",
        "approval_decided",
        "fga_reconciled",
        "keycloak_revoke_failed",
    }
)

FORBIDDEN_KEY_PARTS = ("token", "secret", "link", "password")

_INSERT = (
    "INSERT INTO audit_events (kind, actor_user_id, subject_user_id, agent_id, household_id,"
    " org_id, grant_id, run_id, detail) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
)


def _check_detail(value: Any, path: str = "detail") -> None:
    if isinstance(value, Mapping):
        for key, inner in value.items():
            name = str(key).lower()
            if any(part in name for part in FORBIDDEN_KEY_PARTS):
                # The key name only: never echo the value.
                raise ValueError(f"audit detail key {path}.{key} looks like a secret")
            _check_detail(inner, f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for i, inner in enumerate(value):
            _check_detail(inner, f"{path}[{i}]")


def _params(
    kind: str,
    actor_user_id: uuid.UUID | None,
    subject_user_id: uuid.UUID | None,
    agent_id: str | None,
    household_id: str | None,
    org_id: str | None,
    grant_id: uuid.UUID | None,
    run_id: uuid.UUID | None,
    detail: Mapping[str, Any] | None,
) -> tuple[Any, ...]:
    if kind not in KINDS:
        raise ValueError(f"unknown audit kind {kind!r}")
    detail = dict(detail or {})
    _check_detail(detail)
    return (
        kind,
        actor_user_id,
        subject_user_id,
        agent_id,
        household_id,
        org_id,
        grant_id,
        run_id,
        Jsonb(detail),
    )


@overload
def record(
    conn: AsyncConnection,
    kind: str,
    *,
    actor_user_id: uuid.UUID | None = None,
    subject_user_id: uuid.UUID | None = None,
    agent_id: str | None = None,
    household_id: str | None = None,
    org_id: str | None = None,
    grant_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    detail: Mapping[str, Any] | None = None,
) -> Coroutine[Any, Any, None]: ...
@overload
def record(
    conn: psycopg.Connection,
    kind: str,
    *,
    actor_user_id: uuid.UUID | None = None,
    subject_user_id: uuid.UUID | None = None,
    agent_id: str | None = None,
    household_id: str | None = None,
    org_id: str | None = None,
    grant_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    detail: Mapping[str, Any] | None = None,
) -> None: ...
def record(
    conn: psycopg.Connection | AsyncConnection,
    kind: str,
    *,
    actor_user_id: uuid.UUID | None = None,
    subject_user_id: uuid.UUID | None = None,
    agent_id: str | None = None,
    household_id: str | None = None,
    org_id: str | None = None,
    grant_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    detail: Mapping[str, Any] | None = None,
) -> Coroutine[Any, Any, None] | None:
    """Insert one `audit_events` row. Raises `ValueError` on an unknown kind or a
    secret-looking `detail` key, before anything is written."""
    params = _params(
        kind,
        actor_user_id,
        subject_user_id,
        agent_id,
        household_id,
        org_id,
        grant_id,
        run_id,
        detail,
    )
    if isinstance(conn, AsyncConnection):

        async def _write() -> None:
            await conn.execute(_INSERT, params)

        return _write()
    conn.execute(_INSERT, params)
    return None
