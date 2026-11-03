"""Validated token claims, client ids and reason codes (identity-authz §8, §12 and I1).

`Claims` is what `tokens.validate` returns for a guarded call. The reason codes are the
§12 table, the only source of every identity refusal reason.
"""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from services.common import ids

__all__ = [
    "ACT_MISMATCH",
    "ACT_MISSING",
    "AUTHZ_DENIED",
    "AUTHZ_UNAVAILABLE",
    "GRANT_EXPIRED",
    "GRANT_MISSING",
    "GRANT_REVOKED",
    "IDENTITY_UNAVAILABLE",
    "MCP_AUDIENCE",
    "NEEDS_APPROVAL",
    "OWNER_AUDIENCE",
    "REASONS",
    "SCOPE_MISSING",
    "TOKEN_EXPIRED",
    "TOKEN_INVALID",
    "TOKEN_INVALID_AUD",
    "TOKEN_INVALID_ISS",
    "TOKEN_MISSING",
    "UNKNOWN_AGENT",
    "Claims",
    "agent_id_to_client_id",
    "client_id_to_agent_id",
]

# The audience of every agent and service token (the `kitchenless-mcp` client, §3).
MCP_AUDIENCE = "kitchenless-mcp"
# The audience of the owner app's tokens (§3, §10).
OWNER_AUDIENCE = "owner-app"

# §12 reason codes.
TOKEN_MISSING = "token_missing"
TOKEN_INVALID = "token_invalid"
TOKEN_EXPIRED = "token_expired"
TOKEN_INVALID_ISS = "token_invalid_iss"
TOKEN_INVALID_AUD = "token_invalid_aud"
ACT_MISSING = "act_missing"
ACT_MISMATCH = "act_mismatch"
UNKNOWN_AGENT = "unknown_agent"
SCOPE_MISSING = "scope_missing"
GRANT_REVOKED = "grant_revoked"
GRANT_EXPIRED = "grant_expired"
GRANT_MISSING = "grant_missing"
NEEDS_APPROVAL = "needs_approval"
AUTHZ_DENIED = "authz_denied"
AUTHZ_UNAVAILABLE = "authz_unavailable"
IDENTITY_UNAVAILABLE = "identity_unavailable"

REASONS = frozenset(
    {
        TOKEN_MISSING,
        TOKEN_INVALID,
        TOKEN_EXPIRED,
        TOKEN_INVALID_ISS,
        TOKEN_INVALID_AUD,
        ACT_MISSING,
        ACT_MISMATCH,
        UNKNOWN_AGENT,
        SCOPE_MISSING,
        GRANT_REVOKED,
        GRANT_EXPIRED,
        GRANT_MISSING,
        NEEDS_APPROVAL,
        AUTHZ_DENIED,
        AUTHZ_UNAVAILABLE,
        IDENTITY_UNAVAILABLE,
    }
)


@dataclass(frozen=True)
class Claims:
    """The caller of a guarded call. `user_id` and `grant_id` are `None` for service
    tokens."""

    user_id: UUID | None
    agent_id: str
    kind: Literal["people", "service"]
    scopes: frozenset[str]
    grant_id: UUID | None
    token_source: str


def agent_id_to_client_id(agent_id: str) -> str:
    """`household:us-1` -> `household.us-1`: the first `:` becomes `.`."""
    return ids.keycloak_client_id(agent_id)


def client_id_to_agent_id(client_id: str) -> str:
    """`consumer.us-1.2` -> `consumer:us-1.2`: only the first `.` becomes `:`."""
    return ids.agent_id_from_client_id(client_id)
