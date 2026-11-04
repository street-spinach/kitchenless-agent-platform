"""Single-use Links (identity-authz §6, §14 and I21).

*   `issue(conn, purpose, target_user_id, sent_to_user_id, household_id, agent_ids, next)`
    returns `https://${DOMAIN}/me/l/<token>`, with `?next=<page>` when `next` is set. The
    token is `secrets.token_urlsafe(32)`; only its SHA-256 is stored, with a 600 s expiry,
    and `link_issued` is written. For a child target it first requires `user:<sent_to>
    guardian household:<h>` in OpenFGA, the only guardianship check (§6).
*   `use(conn, token)` marks the link used in one `UPDATE … RETURNING` (unused and not
    expired only) and writes `link_used`. Otherwise it writes `link_rejected` and raises
    `LinkInvalid`. The token never appears in an audit row, a log or an error message.

*   `next_path(value)`: the page the sign-in callback opens, `/me/<next>` for a page in
    `NEXT_PAGES`, else `/me/agents` (experience EX-A15, amended for #299: `next` travels
    in the link's URL, not on the row, and is checked against the closed set on use).
    `story` opens `/me/orders/latest` (showcase SC-A7).

The caller commits: `use` raises after writing `link_rejected`, so a caller catches
`LinkInvalid` inside its connection block to keep that row.
"""

import asyncio
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from services.common.config import get_settings
from services.identity import audit, claims, fga
from services.identity.errors import IdentityError, LinkInvalid
from services.safety.scope import unscoped

__all__ = [
    "DEFAULT_NEXT",
    "LINK_SECONDS",
    "NEXT_PAGES",
    "PURPOSES",
    "STORY_PATH",
    "Link",
    "LinkInvalid",
    "NotGuardian",
    "issue",
    "next_path",
    "use",
]

LINK_SECONDS = 600
TOKEN_BYTES = 32
PURPOSES = ("consent", "me")

Purpose = Literal["consent", "me"]
# The /me pages a Link may open after sign-in (experience EX-A15).
NEXT_PAGES = ("household", "pantry", "history", "agents", "story")
Next = Literal["household", "pantry", "history", "agents", "story"]
DEFAULT_NEXT = "agents"
# The newest order's story (showcase SC-A7): the one page that isn't `/me/<page>`.
STORY_PATH = "/me/orders/latest"


class NotGuardian(IdentityError):
    """A child's link may be sent only to a guardian of the child's household (§6)."""

    reason = claims.AUTHZ_DENIED


@dataclass(frozen=True)
class Link:
    purpose: str
    target_user_id: UUID
    sent_to_user_id: UUID
    household_id: str
    agent_ids: tuple[str, ...]
    expires_at: datetime


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def url(token: str, next: Next | None = None) -> str:
    base = f"https://{get_settings().domain}/me/l/{token}"
    return base if next is None else f"{base}?next={next}"


def next_path(value: object) -> str:
    """`/me/<value>` for a page in `NEXT_PAGES` (`story` is `STORY_PATH`), else `/me/agents`.
    The value comes from the link's URL, so anything outside the closed set falls back to
    the default."""
    page = value if isinstance(value, str) and value in NEXT_PAGES else DEFAULT_NEXT
    return STORY_PATH if page == "story" else f"/me/{page}"


async def _is_child(conn: AsyncConnection, household_id: str, user_id: UUID) -> bool:
    cur = await unscoped(conn, "links").execute(
        "SELECT 1 FROM household_members WHERE household_id = %s AND user_id = %s"
        " AND role = 'child'",
        (household_id, user_id),
    )
    return await cur.fetchone() is not None


async def issue(
    conn: AsyncConnection,
    purpose: Purpose,
    target_user_id: UUID,
    sent_to_user_id: UUID,
    household_id: str,
    agent_ids: list[str] | tuple[str, ...] = (),
    next: Next | None = None,
) -> str:
    """Store a new Link and return its URL. Raises `NotGuardian` for a child target whose
    link would go to someone who is not a guardian of the household.

    `next` is the /me page to open after sign-in; it is added to the URL as `?next=`."""
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be consent or me, got {purpose!r}")
    if next is not None and next not in NEXT_PAGES:
        raise ValueError(f"next must be one of {NEXT_PAGES}, got {next!r}")
    if await _is_child(conn, household_id, target_user_id):
        allowed, _ = await asyncio.to_thread(
            fga.check, f"user:{sent_to_user_id}", "guardian", f"household:{household_id}"
        )
        if not allowed:
            raise NotGuardian("the receiver is not a guardian of the child's household")
    token = secrets.token_urlsafe(TOKEN_BYTES)
    async with conn.transaction():
        await unscoped(conn, "links").execute(
            "INSERT INTO consent_links (link_hash, purpose, target_user_id, sent_to_user_id,"
            " household_id, agent_ids, expires_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s))",
            (
                _hash(token),
                purpose,
                target_user_id,
                sent_to_user_id,
                household_id,
                list(agent_ids),
                LINK_SECONDS,
            ),
        )
        await audit.record(
            conn,
            "link_issued",
            subject_user_id=target_user_id,
            household_id=household_id,
            detail={
                "purpose": purpose,
                "sent_to_user_id": str(sent_to_user_id),
                "agent_ids": list(agent_ids),
                **({} if next is None else {"next": next}),
            },
        )
    return url(token, next)


async def use(conn: AsyncConnection, token: str) -> Link:
    """Use the link once. Raises `LinkInvalid` if it is unknown, used or expired."""
    async with conn.transaction():
        async with unscoped(conn, "links").cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "UPDATE consent_links SET used_at = now() WHERE link_hash = %(hash)s"
                " AND used_at IS NULL AND expires_at > now() RETURNING *",
                {"hash": _hash(token)},
            )
            row = await cur.fetchone()
        if row is None:
            # No token, no hash: nothing in the row can be used to find the link.
            await audit.record(conn, "link_rejected")
        else:
            await audit.record(
                conn,
                "link_used",
                actor_user_id=row["sent_to_user_id"],
                subject_user_id=row["target_user_id"],
                household_id=row["household_id"],
                detail={"purpose": row["purpose"], "agent_ids": list(row["agent_ids"])},
            )
    if row is None:
        raise LinkInvalid("the link is unknown, used or expired")
    return Link(
        purpose=row["purpose"],
        target_user_id=row["target_user_id"],
        sent_to_user_id=row["sent_to_user_id"],
        household_id=row["household_id"],
        agent_ids=tuple(row["agent_ids"]),
        expires_at=row["expires_at"],
    )
