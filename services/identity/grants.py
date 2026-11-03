"""Grants: one person's consent for one agent (identity-authz §6, §11 and I4).

*   `create` revokes the grant it replaces (`replaced`, audited `consent_revoked`),
    encrypts the offline token (AES-256-GCM, a 12-byte nonce, the `grant_id` bytes as
    additional data; stored as `nonce || ciphertext || tag`), inserts the row with
    `expires_at = now() + 30 days` and writes `consent_granted`.
*   `load_active(conn, agent_id, user_id=None)` returns the active grant or raises
    `GrantMissing`, `GrantExpired` (marking the row `expired` and writing
    `grant_expired`) or `GrantRevoked`. With no `user_id` it loads the agent's one
    household grant. Its writes are in the caller's transaction: a caller that refuses
    on `GrantExpired` commits first, so the mark is kept.
*   `revoke` is §11 step 1: one row update and `consent_revoked`, in one transaction.
*   `after_revoke` is §11 steps 2 and 3: after the commit, one best-effort Keycloak
    per-client consent revoke (a failure writes `keycloak_revoke_failed` and is not
    retried), then the revoke notice `consent_revoked` with a Renew button and
    `ref = {grant, <grant_id>}` (experience §6, EX-I19).
*   `mark_refused` handles Keycloak's `invalid_grant` (§12 failures).
*   `client_assertion` is the `private_key_jwt` assertion for the token endpoint.
"""

import base64
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from services.common import ids
from services.common.config import get_settings
from services.identity import audit
from services.identity.errors import GrantExpired, GrantMissing, GrantRevoked
from services.safety.scope import unscoped

__all__ = [
    "CLASS_SCOPES",
    "GRANT_DAYS",
    "RENEW_BUTTON",
    "Grant",
    "GrantExpired",
    "GrantMissing",
    "GrantRevoked",
    "after_revoke",
    "client_assertion",
    "create",
    "decrypt",
    "encrypt",
    "load_active",
    "mark_refused",
    "revoke",
]

log = logging.getLogger(__name__)

GRANT_DAYS = 30
NONCE_BYTES = 12
ASSERTION_SECONDS = 60

# §6 scopes per agent class, with the agents amendment's `constraints:write` (#186).
CLASS_SCOPES: dict[str, tuple[str, ...]] = {
    "consumer": ("constraints:read", "constraints:write", "pantry:read", "meals:plan"),
    "household": (
        "constraints:read",
        "pantry:read",
        "pantry:write",
        "orders:place",
        "approvals:request",
    ),
}

# Mock grants (identity-authz I27), written by the seed and by the eval reset's re-run of
# the seed's eval part: no offline token, so placeholder bytes and key version 0, which no
# key holds. The upsert renews the row: active again, 30 days from now.
MOCK_CIPHERTEXT = b"mock-grant"
MOCK_KEY_VERSION = 0
MOCK_UPSERT_SQL = f"""
INSERT INTO agent_grants (grant_id, agent_id, agent_kind, user_id, consented_by, household_id,
                          scopes, refresh_ciphertext, key_version, expires_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, {MOCK_KEY_VERSION},
        now() + make_interval(days => %s))
ON CONFLICT (grant_id) DO UPDATE SET
  scopes = EXCLUDED.scopes, status = 'active', expires_at = EXCLUDED.expires_at,
  revoked_at = NULL, revoked_by = NULL, revoke_reason = NULL
"""
# A mock grant whose agent (and person) already has another active grant, for example a
# real consent, is left alone.
OTHER_ACTIVE_SQL = """
SELECT 1 FROM agent_grants WHERE agent_id = %s AND status = 'active'
  AND grant_id <> %s AND (agent_kind = 'household' OR user_id = %s)
"""

_COLUMNS = (
    "grant_id, agent_id, agent_kind, user_id, consented_by, household_id, scopes, status,"
    " refresh_ciphertext, key_version, expires_at, expires_at <= now() AS past_expiry"
)


@dataclass(frozen=True)
class Grant:
    grant_id: UUID
    agent_id: str
    agent_kind: str
    user_id: UUID
    consented_by: UUID
    household_id: str
    scopes: tuple[str, ...]
    status: str
    refresh_ciphertext: bytes
    key_version: int
    expires_at: datetime


def _grant(row: dict[str, Any]) -> Grant:
    return Grant(
        grant_id=row["grant_id"],
        agent_id=row["agent_id"],
        agent_kind=row["agent_kind"],
        user_id=row["user_id"],
        consented_by=row["consented_by"],
        household_id=row["household_id"],
        scopes=tuple(row["scopes"]),
        status=row["status"],
        refresh_ciphertext=bytes(row["refresh_ciphertext"]),
        key_version=row["key_version"],
        expires_at=row["expires_at"],
    )


# --- encryption ---------------------------------------------------------------------------


def _key() -> tuple[bytes, int]:
    settings = get_settings()
    if not settings.grant_encryption_key or settings.grant_key_version is None:
        raise RuntimeError("GRANT_ENCRYPTION_KEY and GRANT_KEY_VERSION are required")
    key = base64.b64decode(settings.grant_encryption_key)
    if len(key) != 32:
        raise RuntimeError("GRANT_ENCRYPTION_KEY must be 32 bytes, base64")
    return key, settings.grant_key_version


def encrypt(grant_id: UUID, offline_token: str) -> tuple[bytes, int]:
    """`(nonce || ciphertext || tag, key_version)` of the offline token."""
    key, version = _key()
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, offline_token.encode(), grant_id.bytes), version


def decrypt(grant: Grant) -> str:
    """The grant's offline token. Raises if the key version is not the current one."""
    key, version = _key()
    if grant.key_version != version:
        raise RuntimeError(f"grant key version {grant.key_version} is not held")
    data = grant.refresh_ciphertext
    nonce, sealed = data[:NONCE_BYTES], data[NONCE_BYTES:]
    return AESGCM(key).decrypt(nonce, sealed, grant.grant_id.bytes).decode()


# --- rows -----------------------------------------------------------------------------------


async def create(
    conn: AsyncConnection,
    *,
    agent_id: str,
    agent_kind: str,
    user_id: UUID,
    consented_by: UUID,
    household_id: str,
    scopes: list[str] | tuple[str, ...],
    offline_token: str,
) -> UUID:
    """Store a new active grant, replacing the one it supersedes (§6)."""
    if agent_kind not in CLASS_SCOPES:
        raise ValueError(f"agent_kind must be consumer or household, got {agent_kind!r}")
    grant_id = ids.new_grant_id()
    ciphertext, version = encrypt(grant_id, offline_token)
    q = unscoped(conn, "grants")
    async with conn.transaction():
        # A consumer grant replaces the same agent and person's; a household grant replaces
        # any active grant of that agent.
        cur = await q.execute(
            "UPDATE agent_grants SET status = 'revoked', revoked_at = now(), revoked_by = %s,"
            " revoke_reason = 'replaced'"
            " WHERE agent_id = %s AND status = 'active' AND (agent_kind = 'household'"
            " OR user_id = %s) RETURNING grant_id, user_id, household_id",
            (consented_by, agent_id, user_id),
        )
        for old_id, old_user, old_household in await cur.fetchall():
            await audit.record(
                conn,
                "consent_revoked",
                actor_user_id=consented_by,
                subject_user_id=old_user,
                agent_id=agent_id,
                household_id=old_household,
                grant_id=old_id,
                detail={"reason": "replaced"},
            )
        await q.execute(
            "INSERT INTO agent_grants (grant_id, agent_id, agent_kind, user_id, consented_by,"
            " household_id, scopes, refresh_ciphertext, key_version, expires_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,"
            " now() + make_interval(days => %s))",
            (
                grant_id,
                agent_id,
                agent_kind,
                user_id,
                consented_by,
                household_id,
                list(scopes),
                ciphertext,
                version,
                GRANT_DAYS,
            ),
        )
        await audit.record(
            conn,
            "consent_granted",
            actor_user_id=consented_by,
            subject_user_id=user_id,
            agent_id=agent_id,
            household_id=household_id,
            grant_id=grant_id,
            detail={"scopes": list(scopes)},
        )
    return grant_id


async def _latest(
    conn: AsyncConnection, agent_id: str, user_id: UUID | None
) -> dict[str, Any] | None:
    """The agent's (and person's) active row if there is one, else its latest row."""
    where = "agent_id = %s" + ("" if user_id is None else " AND user_id = %s")
    params: tuple[Any, ...] = (agent_id,) if user_id is None else (agent_id, user_id)
    async with unscoped(conn, "grants").cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM agent_grants WHERE {where}"
            " ORDER BY status = 'active' DESC, created_at DESC LIMIT 1",
            params,
        )
        return await cur.fetchone()


async def _expire(conn: AsyncConnection, row: dict[str, Any]) -> None:
    cur = await unscoped(conn, "grants").execute(
        "UPDATE agent_grants SET status = 'expired' WHERE grant_id = %s AND status = 'active'",
        (row["grant_id"],),
    )
    if cur.rowcount:
        await audit.record(
            conn,
            "grant_expired",
            subject_user_id=row["user_id"],
            agent_id=row["agent_id"],
            household_id=row["household_id"],
            grant_id=row["grant_id"],
        )


async def load_active(conn: AsyncConnection, agent_id: str, user_id: UUID | None = None) -> Grant:
    """The active grant of `agent_id` (for `user_id`, or the household grant)."""
    row = await _latest(conn, agent_id, user_id)
    if row is None:
        raise GrantMissing(f"no grant for {agent_id}")
    if row["status"] == "active" and row["past_expiry"]:
        await _expire(conn, row)
        raise GrantExpired(f"the grant of {agent_id} has expired")
    if row["status"] == "expired":
        raise GrantExpired(f"the grant of {agent_id} has expired")
    if row["status"] == "revoked":
        raise GrantRevoked(f"the grant of {agent_id} is revoked")
    return _grant(row)


async def revoke(
    conn: AsyncConnection, grant_id: UUID, revoked_by: UUID | None, revoke_reason: str
) -> bool:
    """§11 step 1: mark the grant `revoked` and write `consent_revoked`, in one
    transaction. Returns whether an active grant was revoked."""
    async with conn.transaction():
        cur = await unscoped(conn, "grants").execute(
            "UPDATE agent_grants SET status = 'revoked', revoked_at = now(), revoked_by = %s,"
            " revoke_reason = %s WHERE grant_id = %s AND status = 'active'"
            " RETURNING agent_id, user_id, household_id",
            (revoked_by, revoke_reason, grant_id),
        )
        row = await cur.fetchone()
        if row is None:
            return False
        agent_id, user_id, household_id = row
        await audit.record(
            conn,
            "consent_revoked",
            actor_user_id=revoked_by,
            subject_user_id=user_id,
            agent_id=agent_id,
            household_id=household_id,
            grant_id=grant_id,
            detail={"reason": revoke_reason},
        )
    return True


async def after_revoke(conn: AsyncConnection, grant_id: UUID) -> bool:
    """§11 steps 2 and 3, after `revoke` has committed: remove the grant holder's stored
    consent for the agent's client in Keycloak, once, then send the revoke notice
    `consent_revoked`. A Keycloak failure writes `keycloak_revoke_failed` (the
    `consent_revoked` row is never updated) and returns False; the notice is sent either
    way."""
    from services.identity import keycloak_admin  # it imports this module

    cur = await unscoped(conn, "grants").execute(
        "SELECT g.agent_id, g.user_id, g.household_id, u.keycloak_sub FROM agent_grants g"
        " JOIN users u ON u.user_id = g.user_id WHERE g.grant_id = %s AND g.status = 'revoked'",
        (grant_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise ValueError("after_revoke needs a revoked grant")
    agent_id, user_id, household_id, keycloak_sub = row
    done = True
    try:
        if not keycloak_sub:
            raise RuntimeError("the grant holder has no Keycloak user id")
        await keycloak_admin.revoke_consent(keycloak_sub, ids.keycloak_client_id(agent_id))
    except Exception as exc:  # best effort (§11): every failure is audited, none retried
        log.warning("keycloak consent revoke failed: %s", type(exc).__name__)
        await audit.record(
            conn,
            "keycloak_revoke_failed",
            subject_user_id=user_id,
            agent_id=agent_id,
            household_id=household_id,
            grant_id=grant_id,
            detail={"error": type(exc).__name__},
        )
        done = False
    await _revoke_notice(conn, grant_id)
    return done


# --- the revoke notice (§11 step 3, §12; experience §6, EX-I19) -----------------------------

# `api` writes the notice (architecture §3) to the WhatsApp channel.
NOTICE_FROM = "system:api"
NOTICE_TO = "channel:whatsapp"
RENEW_TITLE = "Renew"
# The Renew button (experience §6, §7): `me:renew`, the same payload inside and outside the
# window.
RENEW_BUTTON: dict[str, str] = {"payload": ids.page_button("renew"), "title": RENEW_TITLE}
# The notice's subject (experience §5): a fixed phrase, never a member's or agent's name.
_NOTICE_AGENT = {"household": "The household agent", "consumer": "Your agent"}
PLACED_LINE = " Your order from {store} was already placed and will still arrive."

# Who gets the notice: the grant holder, or for a child the guardian who consented
# (identity-authz §11 "Expiry").
_NOTICE_TO_SQL = """
SELECT g.agent_kind, g.household_id,
       CASE WHEN m.role = 'child' THEN g.consented_by ELSE g.user_id END
FROM agent_grants g
LEFT JOIN household_members m ON m.household_id = g.household_id AND m.user_id = g.user_id
WHERE g.grant_id = %s
"""

# The household's latest order that was placed and has not yet arrived (I-CUJ 3: it
# finishes), with its seller's `organizations.name`.
_PLACED_SQL = """
SELECT org.name
FROM orders o
JOIN agent_cards c ON c.agent_id = o.seller_agent_id
JOIN organizations org ON c.owner_ref = 'org:' || org.org_id
WHERE o.household_id = %s AND o.state IN ('placed', 'courier_booked', 'in_delivery')
ORDER BY o.created_at DESC, o.order_id LIMIT 1
"""


async def _revoke_notice(conn: AsyncConnection, grant_id: UUID) -> None:
    """`consent_revoked` with `agent`, `placed_line`, the Renew button and
    `ref = {grant, <grant_id>}`, in a `chat` run with no household that ends `completed`
    at once. The sender sends it inside the window, as its Meta template outside, or holds
    it (experience §5)."""
    from services.common import outbox, runs  # `runs` imports this module

    q = unscoped(conn, "grants")
    cur = await q.execute(_NOTICE_TO_SQL, (grant_id,))
    row = await cur.fetchone()
    if row is None or row[2] is None:
        return
    agent_kind, household_id, to_user = row
    placed_line = ""
    if agent_kind == "household":
        cur = await q.execute(_PLACED_SQL, (household_id,))
        placed = await cur.fetchone()
        if placed is not None:
            placed_line = PLACED_LINE.format(store=placed[0])
    async with conn.transaction():
        run_id = await runs.start(
            conn,
            household_id=None,
            kind="chat",
            trigger={"source": "code", "user_id": to_user, "grant_id": grant_id},
        )
        await outbox.send(
            conn,
            message_id=ids.message_id(grant_id, 0),
            run_id=run_id,
            conversation_id=None,
            causation_id=None,
            from_agent=NOTICE_FROM,
            to_agent=NOTICE_TO,
            type="whatsapp_out",
            body={
                "user_id": str(to_user),
                "template": "consent_revoked",
                "params": {"agent": _NOTICE_AGENT[agent_kind], "placed_line": placed_line},
                "buttons": [RENEW_BUTTON],
                "ref": {"kind": "grant", "id": str(grant_id)},
            },
        )
        await runs.finish(conn, run_id, NOTICE_FROM, "completed", "grant_revoked")


async def mark_refused(conn: AsyncConnection, grant_id: UUID) -> None:
    """Keycloak refused the grant (`invalid_grant`): `expired` past its expiry (writes
    `grant_expired`), otherwise `revoked` with `keycloak_invalid_grant`."""
    async with conn.transaction():
        async with unscoped(conn, "grants").cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_COLUMNS} FROM agent_grants WHERE grant_id = %s FOR UPDATE",
                (grant_id,),
            )
            row = await cur.fetchone()
        if row is None or row["status"] != "active":
            return
        if row["past_expiry"]:
            await _expire(conn, row)
        else:
            await revoke(conn, grant_id, None, "keycloak_invalid_grant")


def client_assertion(client_id: str) -> str:
    """The `private_key_jwt` client assertion (§3): RS256 with `AGENT_SIGNING_KEY`,
    `iss` = `sub` = `client_id`, `aud` = the realm issuer, 60 s, a uuid `jti`, no `kid`."""
    settings = get_settings()
    if not settings.agent_signing_key:
        raise RuntimeError("AGENT_SIGNING_KEY is required")
    now = int(time.time())
    payload = {
        "iss": client_id,
        "sub": client_id,
        "aud": settings.issuer_url,
        "iat": now,
        "exp": now + ASSERTION_SECONDS,
        "jti": ids.new_token_id(),
    }
    return jwt.encode(payload, settings.agent_signing_key, algorithm="RS256")
