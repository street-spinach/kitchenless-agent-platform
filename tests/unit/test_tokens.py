"""Mock token issuer and validation (architecture §5 step 1, item 53, Q4)."""

import base64
import json
import time

import jwt
import pytest

from services.identity import tokens
from services.identity.tokens import Claims, InvalidToken, MockIssuer, validate

SECRET = "unit-test-mock-token-secret-32-bytes-long"


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tokens, "_secret", lambda: SECRET)


def _payload(**overrides: object) -> dict[str, object]:
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": tokens.ISSUER,
        "sub": "user:u-1",
        "act": {"sub": "agent:household:h-1"},
        "scope": "orders",
        "iat": now,
        "exp": now + 60,
    }
    payload.update(overrides)
    return payload


def _sign(payload: dict[str, object], secret: str = SECRET) -> str:
    return jwt.encode(payload, secret, algorithm=tokens.ALGORITHM)


def test_sign_and_verify_round_trip() -> None:
    token = MockIssuer().issue("user:u-1", "agent:household:h-1", ["orders", "spend"])
    assert validate(token) == Claims(
        sub="user:u-1", act="agent:household:h-1", scopes=("orders", "spend")
    )


def test_issuer_uses_its_own_secret_when_given() -> None:
    token = MockIssuer(secret=SECRET).issue("user:u-1", "agent:household:h-1", [])
    assert validate(token).scopes == ()


def test_expired_token_is_rejected() -> None:
    token = MockIssuer(ttl_seconds=-1).issue("user:u-1", "agent:household:h-1", [])
    with pytest.raises(InvalidToken, match="ExpiredSignature"):
        validate(token)


def test_token_without_exp_is_rejected() -> None:
    payload = _payload()
    del payload["exp"]
    with pytest.raises(InvalidToken):
        validate(_sign(payload))


def test_wrong_act_is_reported_not_the_caller() -> None:
    """The claims carry the agent the token was issued to; a guard compares it to the caller."""
    token = MockIssuer().issue("user:u-1", "agent:household:h-1", [])
    assert validate(token).act != "agent:grocery:mock-us"


def test_act_rewritten_after_signing_is_rejected() -> None:
    token = MockIssuer().issue("user:u-1", "agent:household:h-1", [])
    header, body, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    claims["act"] = {"sub": "agent:grocery:mock-us"}
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    with pytest.raises(InvalidToken, match="InvalidSignature"):
        validate(f"{header}.{forged}.{signature}")


@pytest.mark.parametrize("act", [None, "agent:household:h-1", {}, {"sub": ""}, {"sub": 7}])
def test_missing_or_malformed_act_is_rejected(act: object) -> None:
    payload = _payload(act=act)
    if act is None:
        del payload["act"]
    with pytest.raises(InvalidToken):
        validate(_sign(payload))


def test_missing_sub_is_rejected() -> None:
    payload = _payload()
    del payload["sub"]
    with pytest.raises(InvalidToken):
        validate(_sign(payload))


def test_token_signed_with_another_secret_is_rejected() -> None:
    with pytest.raises(InvalidToken):
        validate(_sign(_payload(), secret="another-secret-that-is-also-32-bytes"))


def test_wrong_issuer_is_rejected() -> None:
    with pytest.raises(InvalidToken):
        validate(_sign(_payload(iss="someone-else")))


def test_unsigned_token_is_rejected() -> None:
    token = jwt.encode(_payload(), None, algorithm="none")
    with pytest.raises(InvalidToken):
        validate(token)


def test_garbage_is_rejected() -> None:
    with pytest.raises(InvalidToken):
        validate("not-a-token")


def test_issue_requires_sub_and_act() -> None:
    with pytest.raises(ValueError):
        MockIssuer().issue("user:u-1", "", [])
    with pytest.raises(ValueError):
        MockIssuer().issue("", "agent:household:h-1", [])


def test_missing_secret_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()

    class _Settings:
        mock_token_secret = None

    monkeypatch.setattr(tokens, "get_settings", lambda: _Settings())
    with pytest.raises(InvalidToken, match="MOCK_TOKEN_SECRET"):
        validate(_sign(_payload()))
