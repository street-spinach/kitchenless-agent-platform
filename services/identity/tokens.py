"""Caller tokens for guarded tools (architecture §5 step 1, item 53).

`validate()` is the interface the guard calls. Until the identity-authz design replaces the
token source (Q4), every run uses `MockIssuer`, which signs HS256 JWTs with
`MOCK_TOKEN_SECRET`. Real Keycloak validation belongs to identity-authz.

`sub` is the person the agent acts for and `act` is the acting agent. `act` is carried in
the RFC 8693 shape, `{"sub": <agent id>}`, the shape Keycloak delegation uses.
"""

import time
from dataclasses import dataclass

import jwt

from services.common.config import get_settings

ALGORITHM = "HS256"
ISSUER = "mock-issuer"
DEFAULT_TTL_SECONDS = 300


class InvalidToken(Exception):
    """The token is malformed, unsigned by the issuer, expired, or missing `sub` or `act`."""


@dataclass(frozen=True)
class Claims:
    sub: str
    act: str
    scopes: tuple[str, ...]


def _secret() -> str:
    secret = get_settings().mock_token_secret
    if not secret:
        raise InvalidToken("MOCK_TOKEN_SECRET is not set")
    return secret


class MockIssuer:
    """Signs tokens with `MOCK_TOKEN_SECRET` (Q4)."""

    def __init__(self, secret: str | None = None, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self._secret = secret
        self._ttl_seconds = ttl_seconds

    def issue(self, sub: str, act: str, scopes: list[str] | tuple[str, ...]) -> str:
        if not sub or not act:
            raise ValueError("sub and act are required")
        now = int(time.time())
        payload = {
            "iss": ISSUER,
            "sub": sub,
            "act": {"sub": act},
            "scope": " ".join(scopes),
            "iat": now,
            "exp": now + self._ttl_seconds,
        }
        return jwt.encode(payload, self._secret or _secret(), algorithm=ALGORITHM)


def validate(token: str) -> Claims:
    """Verify the signature, issuer and expiry; return `sub`, `act` and scopes.

    Raises `InvalidToken` on any failure. Comparing `act` with the calling agent is the
    guard's job.
    """
    try:
        payload = jwt.decode(
            token,
            _secret(),
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            options={"require": ["iss", "sub", "act", "iat", "exp"]},
        )
    except jwt.PyJWTError as e:
        raise InvalidToken(type(e).__name__) from None
    act = payload["act"]
    if not isinstance(act, dict) or not isinstance(act.get("sub"), str) or not act["sub"]:
        raise InvalidToken("act must be an object with a sub")
    if not isinstance(payload["sub"], str) or not payload["sub"]:
        raise InvalidToken("sub must be a non-empty string")
    scope = payload.get("scope", "")
    if not isinstance(scope, str):
        raise InvalidToken("scope must be a string")
    return Claims(sub=payload["sub"], act=act["sub"], scopes=tuple(scope.split()))
