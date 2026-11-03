"""Shared helpers for the identity tests (identity-authz build step 5).

Tokens are signed with the session test key that `tests/conftest.py` installs as
`MOCK_SIGNING_KEY`, through the same `token_source.mock_claims` and `sign_mock` that
`MockSource` uses.
"""

import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import psycopg

from services.common import ids
from services.identity import claims as c
from services.identity import grants, token_source

HOUSEHOLD_SCOPES = grants.CLASS_SCOPES["household"]
CONSUMER_SCOPES = grants.CLASS_SCOPES["consumer"]


def people_token(
    agent_id: str,
    sub: str,
    scopes: Iterable[str] = HOUSEHOLD_SCOPES,
    *,
    now: float | None = None,
    ttl_seconds: int = token_source.TOKEN_SECONDS,
    **overrides: Any,
) -> str:
    """A mock delegated token: `sub` = the person's Keycloak id, `act.client_id` = agent.
    `overrides` replace claims; a value of `None` removes the claim."""
    payload = token_source.mock_claims(
        sub=sub,
        client_id=c.agent_id_to_client_id(agent_id),
        scopes=tuple(scopes),
        delegated=True,
        now=time.time() if now is None else now,
        ttl_seconds=ttl_seconds,
    )
    return _sign(payload, overrides)


def service_token(agent_id: str, *, now: float | None = None, **overrides: Any) -> str:
    """A mock service token for a business agent or `system:api`."""
    client_id = API_CLIENT if agent_id == API_AGENT else c.agent_id_to_client_id(agent_id)
    payload = token_source.mock_claims(
        sub=f"service-account-{client_id}",
        client_id=client_id,
        delegated=False,
        now=time.time() if now is None else now,
    )
    return _sign(payload, overrides)


def _sign(payload: dict[str, Any], overrides: Mapping[str, Any]) -> str:
    for key, value in overrides.items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    return token_source.sign_mock(payload)


API_AGENT = "system:api"
API_CLIENT = "svc.api"


@dataclass
class Person:
    user_id: uuid.UUID
    keycloak_sub: str


def add_person(conn: psycopg.Connection, household_id: str, role: str = "admin") -> Person:
    """A user with a Keycloak id and a `household_members` row (household created)."""
    person = Person(uuid.uuid4(), str(uuid.uuid4()))
    add_household(conn, household_id)
    conn.execute(
        "INSERT INTO users (user_id, display_name, keycloak_sub) VALUES (%s, 'Person', %s)",
        (person.user_id, person.keycloak_sub),
    )
    conn.execute(
        "INSERT INTO household_members (household_id, user_id, role) VALUES (%s, %s, %s)",
        (household_id, person.user_id, role),
    )
    return person


def add_household(conn: psycopg.Connection, household_id: str, env: str = "live") -> None:
    conn.execute(
        "INSERT INTO households (household_id, name, env, region, zone_id, timezone, currency)"
        " VALUES (%s, 'Test household', %s, 'us', 'us-oh-45202', 'America/New_York', 'USD')"
        " ON CONFLICT DO NOTHING",
        (household_id, env),
    )


def add_agent(
    conn: psycopg.Connection, agent_id: str, owner_ref: str, *, kind: str | None = None
) -> None:
    """An enabled `agent_cards` row and its `agent_clients` row."""
    cls = ids.parse_agent_id(agent_id)[0] if agent_id != API_AGENT else "system"
    conn.execute(
        "INSERT INTO agent_cards (agent_id, class, owner_ref, region, zone_ids, host_process)"
        " VALUES (%s, %s, %s, 'us', '{us-oh-45202}', 'agents') ON CONFLICT DO NOTHING",
        (agent_id, cls, owner_ref),
    )
    kind = kind or ("people" if cls in ("consumer", "household") else "service")
    client_id = API_CLIENT if agent_id == API_AGENT else c.agent_id_to_client_id(agent_id)
    conn.execute(
        "INSERT INTO agent_clients (agent_id, client_id, kind) VALUES (%s, %s, %s)"
        " ON CONFLICT DO NOTHING",
        (agent_id, client_id, kind),
    )


def add_grant(
    conn: psycopg.Connection,
    agent_id: str,
    person: Person,
    household_id: str,
    *,
    status: str = "active",
    expires: str = "30 days",
    consented_by: uuid.UUID | None = None,
) -> uuid.UUID:
    """A grant row; `expires` is an interval from now (negative for a past expiry)."""
    grant_id = uuid.uuid4()
    kind = ids.parse_agent_id(agent_id)[0]
    conn.execute(
        "INSERT INTO agent_grants (grant_id, agent_id, agent_kind, user_id, consented_by,"
        " household_id, scopes, status, refresh_ciphertext, key_version, expires_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'x', 1, now() + %s::interval)",
        (
            grant_id,
            agent_id,
            kind,
            person.user_id,
            consented_by or person.user_id,
            household_id,
            list(grants.CLASS_SCOPES[kind]),
            status,
            expires,
        ),
    )
    return grant_id


def grant_household(
    conn: psycopg.Connection, household_id: str, user_id: uuid.UUID | None = None
) -> uuid.UUID:
    """An active grant for `household:<household_id>`, so `runs.start` starts its runs
    (identity-authz I39). Creates the holder (a new user unless `user_id` is given) and the
    household agent's card and client if missing. The household must exist."""
    holder = user_id or uuid.uuid4()
    conn.execute(
        "INSERT INTO users (user_id, display_name, keycloak_sub) VALUES (%s, 'Admin', %s)"
        " ON CONFLICT (user_id) DO UPDATE SET keycloak_sub ="
        " COALESCE(users.keycloak_sub, EXCLUDED.keycloak_sub)",
        (holder, str(uuid.uuid4())),
    )
    agent_id = f"household:{household_id}"
    add_agent(conn, agent_id, agent_id)
    sub = conn.execute("SELECT keycloak_sub FROM users WHERE user_id = %s", (holder,)).fetchone()
    return add_grant(conn, agent_id, Person(holder, sub[0]), household_id)


@dataclass
class Household:
    """A household with an admin, a household agent and the admin's consumer agent, each
    with an active grant (the shape the seed writes for eval households)."""

    household_id: str
    admin: Person
    household_agent: str
    consumer_agent: str
    grants: dict[str, uuid.UUID] = field(default_factory=dict)


def add_world(
    conn: psycopg.Connection, household_id: str = "h-1", *, env: str = "live"
) -> Household:
    add_household(conn, household_id, env)
    admin = add_person(conn, household_id)
    conn.execute(
        "INSERT INTO approval_rules (household_id, approver_user_id) VALUES (%s, %s)"
        " ON CONFLICT DO NOTHING",
        (household_id, admin.user_id),
    )
    world = Household(
        household_id, admin, f"household:{household_id}", f"consumer:{household_id}-a"
    )
    add_agent(conn, world.household_agent, f"household:{household_id}")
    add_agent(conn, world.consumer_agent, f"user:{admin.user_id}")
    for agent in (world.household_agent, world.consumer_agent):
        world.grants[agent] = add_grant(conn, agent, admin, household_id)
    return world


@dataclass(frozen=True)
class Ctx:
    """The guard's and `tokens.validate`'s view of a call (`meta.CallContext`)."""

    run_id: uuid.UUID
    agent_id: str
    token: str | None
    env: str = "live"
    on_behalf_of: str | None = None
    conversation_id: uuid.UUID | None = None


@dataclass
class MutableCtx:
    run_id: uuid.UUID
    agent_id: str
    token: str | None
    env: str = "live"
    on_behalf_of: str | None = None
    conversation_id: uuid.UUID | None = None
    claims: Any = None
    authz_form: str | None = None


def add_run(
    conn: psycopg.Connection, household_id: str | None, kind: str = "chat", status: str = "running"
) -> uuid.UUID:
    run_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO runs (run_id, household_id, kind, status, trigger, modes)"
        " VALUES (%s, %s, %s, %s, '{}', '{\"token_source\": \"mock\"}')",
        (run_id, household_id, kind, status),
    )
    return run_id
