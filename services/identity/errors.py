"""Identity exception classes (identity-authz I1).

Every class carries `reason`, one of the §12 codes in `claims.REASONS`.

*   `IdentityError`: a refusal. `TokenRefused(reason)` for the `token_*`, `act_*`,
    `unknown_agent` and `scope_missing` codes; `GrantError` (`GrantRevoked`,
    `GrantExpired`, `GrantMissing`) for the grant codes; `LinkInvalid` for a link.
*   `DependencyUnavailable`: an outage, never a refusal (I-G9). `IdentityUnavailable`
    (Keycloak) and `AuthzUnavailable` (OpenFGA).
"""

from services.identity import claims

__all__ = [
    "AuthzUnavailable",
    "DependencyUnavailable",
    "GrantError",
    "GrantExpired",
    "GrantMissing",
    "GrantRevoked",
    "IdentityError",
    "IdentityUnavailable",
    "LinkInvalid",
    "TokenRefused",
    "grant_error",
]


class IdentityError(Exception):
    """An identity check refused the caller."""

    reason: str

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or self.reason)


class TokenRefused(IdentityError):
    """The token was refused. `reason` is one of the token, `act_*`, `unknown_agent` or
    `scope_missing` codes."""

    reason = claims.TOKEN_INVALID

    def __init__(self, reason: str = claims.TOKEN_INVALID, detail: str = "") -> None:
        if reason not in claims.REASONS:
            raise ValueError(f"unknown reason {reason!r}")
        self.reason = reason
        super().__init__(detail or reason)


class GrantError(IdentityError):
    """The agent's grant is not active."""


class GrantRevoked(GrantError):
    reason = claims.GRANT_REVOKED


class GrantExpired(GrantError):
    reason = claims.GRANT_EXPIRED


class GrantMissing(GrantError):
    reason = claims.GRANT_MISSING


class LinkInvalid(IdentityError):
    """A link is unknown, used or expired. §12 has no link code; a link is a token, so
    this is `token_invalid` (decision recorded on #115)."""

    reason = claims.TOKEN_INVALID


_GRANT_ERRORS: dict[str, type[GrantError]] = {
    claims.GRANT_REVOKED: GrantRevoked,
    claims.GRANT_EXPIRED: GrantExpired,
    claims.GRANT_MISSING: GrantMissing,
}


def grant_error(reason: str) -> type[GrantError]:
    """The `GrantError` class for a `grant_*` reason. Raises `KeyError` for any other."""
    return _GRANT_ERRORS[reason]


class DependencyUnavailable(Exception):
    """An identity dependency did not answer. Not a refusal (I-G9)."""

    reason: str  # set by each subclass, from the §12 reason codes

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or self.reason)


class IdentityUnavailable(DependencyUnavailable):
    """Keycloak (or the token source) timed out, could not be reached or answered 5xx."""

    reason = claims.IDENTITY_UNAVAILABLE


class AuthzUnavailable(DependencyUnavailable):
    """OpenFGA timed out or could not be reached (identity-authz §8 step 2, §12)."""

    reason = claims.AUTHZ_UNAVAILABLE
