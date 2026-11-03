"""Onboarding: registering people and children (identity-authz §3, §5 and I23).

*   `create_person(conn, phone_e164, display_name, household_id | None, role, address=None)`
    registers an adult. It creates the Keycloak user (username = the phone) through
    `svc.onboarding`, and writes `users`, `channel_identities` and `household_members`.
    For a new household's first admin (`household_id=None`) it first creates the household
    (`hh-<8 base32>`, the seeded `us` region's zone, timezone and currency, the caller's
    address), its `approval_rules` row with this admin as approver, and the household
    agent's card and Keycloak client (copied from `template.household`).
*   `create_child(conn, admin_user_id, household_id, display_name)` registers a child of a
    household an admin of which asks: a user with no phone (username
    `child.<household_id>.<n>`). Guardianship needs no write: every admin is a guardian.

Both create the person's consumer agent `consumer:<household_id>.<n>`, its card and its
client (copied from `template.consumer`), write `agent_clients`, the structural OpenFGA
tuples (§7) and `audit_events` (`user_onboarded`, `agent_client_created`). Each step looks
up first and creates only what is missing, so a retried onboarding does nothing twice.

An eval household's runs always use mock tokens (I-D24), so its people get no Keycloak
user or client: `keycloak_sub` is the user id itself and `kc_client_uuid` stays NULL.

The Postgres writes run in one transaction (a savepoint inside the caller's); the OpenFGA
tuples are written after it, and again on a retry if they are missing.
"""

import asyncio
import re
from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from services.common import ids
from services.identity import audit, claims, fga
from services.identity.errors import IdentityError
from services.safety.scope import unscoped
from services.seed import region_defaults

__all__ = [
    "ADULT_ROLES",
    "TEMPLATE_CONSUMER",
    "TEMPLATE_HOUSEHOLD",
    "Keycloak",
    "NotAdmin",
    "PhoneTaken",
    "create_child",
    "create_person",
]

TEMPLATE_CONSUMER = "template.consumer"
TEMPLATE_HOUSEHOLD = "template.household"
ADULT_ROLES = ("admin", "member")
E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")
ADDRESS_FIELDS = ("line1", "city", "region", "postal_code", "country")


class Keycloak(Protocol):
    """The two `keycloak_admin.KeycloakAdmin` calls onboarding makes."""

    async def create_user(
        self, username: str, display_name: str, attributes: dict[str, list[str]] | None = None
    ) -> str: ...
    async def create_agent_client(self, agent_id: str, template: str) -> str: ...


class NotAdmin(IdentityError):
    """Only an admin of the household may add a child (§5)."""

    reason = claims.AUTHZ_DENIED


class PhoneTaken(ValueError):
    """The phone already belongs to a member of another household."""


def _q(conn: AsyncConnection) -> Any:
    return unscoped(conn, "onboarding")


async def _one(conn: AsyncConnection, sql: str, params: tuple[Any, ...]) -> tuple[Any, ...] | None:
    cur = await _q(conn).execute(sql, params)
    return await cur.fetchone()


def _keycloak(env: str, keycloak: Keycloak | None) -> Keycloak | None:
    """No Keycloak for an eval household (I-D24); otherwise the given or the default admin."""
    if env == "eval":
        return None
    if keycloak is not None:
        return keycloak
    from services.identity import keycloak_admin

    return keycloak_admin.default()


async def _household(conn: AsyncConnection, household_id: str) -> Mapping[str, Any]:
    cur = await _q(conn).execute(
        "SELECT env, region, zone_id, currency FROM households WHERE household_id = %s",
        (household_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise LookupError(f"household {household_id} does not exist")
    return dict(zip(("env", "region", "zone_id", "currency"), row, strict=True))


async def _create_household(
    conn: AsyncConnection, household_id: str, display_name: str, address: Mapping[str, Any]
) -> None:
    missing = [f for f in ADDRESS_FIELDS if not address.get(f)]
    if missing:
        raise ValueError(f"the address needs {missing}")
    region = region_defaults()
    await _q(conn).execute(
        "INSERT INTO households (household_id, name, env, region, zone_id, timezone, currency)"
        " VALUES (%s, %s, 'live', %s, %s, %s, %s)",
        (
            household_id,
            f"{display_name}'s household",
            region["region"],
            region["zone_id"],
            region["timezone"],
            region["currency"],
        ),
    )
    await _q(conn).execute(
        "INSERT INTO household_addresses (address_ref, household_id, line1, line2, city,"
        " region, postal_code, country) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            ids.household_address_ref(household_id),
            household_id,
            address["line1"],
            address.get("line2"),
            address["city"],
            address["region"],
            str(address["postal_code"]),
            address["country"],
        ),
    )


async def _ensure_user(
    conn: AsyncConnection,
    user_id: UUID,
    display_name: str,
    username: str,
    keycloak: Keycloak | None,
) -> None:
    row = await _one(conn, "SELECT keycloak_sub FROM users WHERE user_id = %s", (user_id,))
    if row is not None and row[0]:
        return
    sub = str(user_id) if keycloak is None else await keycloak.create_user(username, display_name)
    await _q(conn).execute(
        "INSERT INTO users (user_id, display_name, keycloak_sub) VALUES (%s, %s, %s)"
        " ON CONFLICT (user_id) DO UPDATE"
        " SET keycloak_sub = coalesce(users.keycloak_sub, EXCLUDED.keycloak_sub)",
        (user_id, display_name, sub),
    )


async def _ensure_member(
    conn: AsyncConnection,
    household_id: str,
    user_id: UUID,
    role: str,
    actor: UUID | None,
) -> None:
    cur = await _q(conn).execute(
        "INSERT INTO household_members (household_id, user_id, role) VALUES (%s, %s, %s)"
        " ON CONFLICT (household_id, user_id) DO NOTHING",
        (household_id, user_id, role),
    )
    if cur.rowcount:
        await audit.record(
            conn,
            "user_onboarded",
            actor_user_id=actor,
            subject_user_id=user_id,
            household_id=household_id,
            detail={"role": role},
        )


async def _ensure_card(
    conn: AsyncConnection, agent_id: str, cls: str, owner_ref: str, household: Mapping[str, Any]
) -> None:
    """The agent's card with the seed's default people-card fields (`_people_card`)."""
    await _q(conn).execute(
        "INSERT INTO agent_cards (agent_id, class, owner_ref, region, zone_ids, currency,"
        " host_process, vendor, config, enabled)"
        " VALUES (%s, %s, %s, %s, %s, %s, 'agents', NULL, %s, true)"
        " ON CONFLICT (agent_id) DO NOTHING",
        (
            agent_id,
            cls,
            owner_ref,
            household["region"],
            [household["zone_id"]],
            household["currency"],
            Jsonb({}),
        ),
    )


async def _ensure_client(
    conn: AsyncConnection,
    agent_id: str,
    template: str,
    keycloak: Keycloak | None,
    household_id: str,
) -> None:
    row = await _one(
        conn, "SELECT kc_client_uuid FROM agent_clients WHERE agent_id = %s", (agent_id,)
    )
    if row is not None and (row[0] is not None or keycloak is None):
        return
    kc_uuid = None if keycloak is None else await keycloak.create_agent_client(agent_id, template)
    client_id = ids.keycloak_client_id(agent_id)
    await _q(conn).execute(
        "INSERT INTO agent_clients (agent_id, client_id, kc_client_uuid, kind)"
        " VALUES (%s, %s, %s, 'people') ON CONFLICT (agent_id) DO UPDATE"
        " SET kc_client_uuid = EXCLUDED.kc_client_uuid",
        (agent_id, client_id, kc_uuid),
    )
    if row is None:
        await audit.record(
            conn,
            "agent_client_created",
            agent_id=agent_id,
            household_id=household_id,
            detail={"client_id": client_id},
        )


async def _consumer_of(conn: AsyncConnection, user_id: UUID) -> str | None:
    row = await _one(
        conn,
        "SELECT agent_id FROM agent_cards WHERE class = 'consumer' AND owner_ref = %s"
        " ORDER BY agent_id LIMIT 1",
        (f"user:{user_id}",),
    )
    return None if row is None else str(row[0])


async def _next_n(conn: AsyncConnection, household_id: str) -> int:
    """One more than the highest `n` of the household's `consumer:<household_id>.<n>`."""
    prefix = f"consumer:{household_id}."
    cur = await _q(conn).execute(
        "SELECT agent_id FROM agent_cards WHERE starts_with(agent_id, %s)", (prefix,)
    )
    taken = [int(s) for (a,) in await cur.fetchall() if (s := a[len(prefix) :]).isdecimal()]
    return max(taken, default=0) + 1


async def _ensure_consumer(
    conn: AsyncConnection,
    user_id: UUID,
    household_id: str,
    household: Mapping[str, Any],
    keycloak: Keycloak | None,
    n: int | None = None,
) -> str:
    agent_id = await _consumer_of(conn, user_id)
    if agent_id is None:
        agent_id = ids.onboarded_consumer_agent_id(
            household_id, n if n is not None else await _next_n(conn, household_id)
        )
        await _ensure_card(conn, agent_id, "consumer", f"user:{user_id}", household)
    await _ensure_client(conn, agent_id, TEMPLATE_CONSUMER, keycloak, household_id)
    return agent_id


async def _write_tuples(tuples: set[fga.Tuple]) -> None:
    await asyncio.to_thread(fga.ensure, sorted(tuples))


async def create_person(
    conn: AsyncConnection,
    phone_e164: str,
    display_name: str,
    household_id: str | None,
    role: str,
    address: Mapping[str, Any] | None = None,
    *,
    channel_mode: str = "real",
    keycloak: Keycloak | None = None,
) -> UUID:
    """Register an adult (§5 step 1) and return their `user_id`.

    `household_id=None` creates a new household with this person as its first admin, and
    needs `role='admin'` and an `address`. Raises `PhoneTaken` if the phone belongs to a
    member of another household, `LookupError` for an unknown household and `ValueError`
    for an invalid argument; nothing is written then.
    """
    if not E164.match(phone_e164):
        raise ValueError("phone_e164 must be an E.164 number")
    if not display_name.strip():
        raise ValueError("display_name is required")
    if role not in ADULT_ROLES:
        raise ValueError(f"role must be one of {ADULT_ROLES}; children use create_child")
    if channel_mode not in ("real", "sim"):
        raise ValueError("channel_mode must be real or sim")
    if household_id is None and role != "admin":
        raise ValueError("a new household's first person is its admin")
    tuples: set[fga.Tuple] = set()
    async with conn.transaction():
        row = await _one(
            conn, "SELECT user_id FROM channel_identities WHERE phone_e164 = %s", (phone_e164,)
        )
        user_id: UUID = row[0] if row is not None else ids.person_user_id(phone_e164)
        cur = await _q(conn).execute(
            "SELECT household_id FROM household_members WHERE user_id = %s ORDER BY created_at",
            (user_id,),
        )
        joined = [h for (h,) in await cur.fetchall()]
        new_household = False
        if household_id is None:
            if joined:
                household_id = joined[0]  # a retried first-admin onboarding
            else:
                if address is None:
                    raise ValueError("a new household needs an address")
                household_id = ids.new_household_id()
                await _create_household(conn, household_id, display_name, address)
                new_household = True
        if any(h != household_id for h in joined):
            raise PhoneTaken("the phone already belongs to a member of another household")
        household = await _household(conn, household_id)
        keycloak = _keycloak(household["env"], keycloak)

        await _ensure_user(conn, user_id, display_name, phone_e164, keycloak)
        await _q(conn).execute(
            "INSERT INTO channel_identities (phone_e164, user_id, channel_mode)"
            " VALUES (%s, %s, %s) ON CONFLICT (phone_e164) DO NOTHING",
            (phone_e164, user_id, channel_mode),
        )
        await _ensure_member(conn, household_id, user_id, role, None)
        tuples.add(fga.member_tuple(household_id, user_id, role))
        if new_household:
            await _q(conn).execute(
                "INSERT INTO approval_rules (household_id, approver_user_id) VALUES (%s, %s)"
                " ON CONFLICT (household_id) DO NOTHING",
                (household_id, user_id),
            )
        household_agent = f"household:{household_id}"
        if new_household:
            await _ensure_card(conn, household_agent, "household", household_agent, household)
            await _ensure_client(conn, household_agent, TEMPLATE_HOUSEHOLD, keycloak, household_id)
            tuples |= fga.card_tuples(household_agent, "household", household_agent)
        consumer = await _ensure_consumer(conn, user_id, household_id, household, keycloak)
        tuples |= fga.card_tuples(consumer, "consumer", f"user:{user_id}")
    await _write_tuples(tuples)
    return user_id


async def create_child(
    conn: AsyncConnection,
    admin_user_id: UUID,
    household_id: str,
    display_name: str,
    *,
    keycloak: Keycloak | None = None,
) -> UUID:
    """Register a child of `household_id` (§5 step 3) and return their `user_id`.

    A child of the household with the same display name is the same child, so a repeated
    call returns it. Raises `NotAdmin` unless `admin_user_id` is an admin of the household.
    """
    if not display_name.strip():
        raise ValueError("display_name is required")
    tuples: set[fga.Tuple] = set()
    async with conn.transaction():
        household = await _household(conn, household_id)
        row = await _one(
            conn,
            "SELECT role FROM household_members WHERE household_id = %s AND user_id = %s",
            (household_id, admin_user_id),
        )
        if row is None or row[0] != "admin":
            raise NotAdmin("only an admin of the household may add a child")
        row = await _one(
            conn,
            "SELECT m.user_id FROM household_members m JOIN users u ON u.user_id = m.user_id"
            " WHERE m.household_id = %s AND m.role = 'child' AND u.display_name = %s"
            " ORDER BY m.created_at LIMIT 1",
            (household_id, display_name),
        )
        if row is not None:
            user_id: UUID = row[0]
            consumer = await _consumer_of(conn, user_id)
        else:
            keycloak = _keycloak(household["env"], keycloak)
            n = await _next_n(conn, household_id)
            user_id = ids.child_user_id(household_id, n)
            username = ids.child_username(household_id, n)
            await _ensure_user(conn, user_id, display_name, username, keycloak)
            await _ensure_member(conn, household_id, user_id, "child", admin_user_id)
            consumer = await _ensure_consumer(conn, user_id, household_id, household, keycloak, n)
        tuples.add(fga.member_tuple(household_id, user_id, "child"))
        if consumer is not None:
            tuples |= fga.card_tuples(consumer, "consumer", f"user:{user_id}")
    await _write_tuples(tuples)
    return user_id
