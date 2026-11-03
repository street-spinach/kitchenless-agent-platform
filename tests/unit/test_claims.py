"""Claims, client ids and reason codes (identity-authz I1)."""

import inspect

import pytest

from services.identity import claims as c
from services.identity import errors


@pytest.mark.parametrize(
    ("agent_id", "client_id"),
    [
        ("household:us-1", "household.us-1"),
        ("consumer:us-1-a", "consumer.us-1-a"),
        ("grocery:square-us", "grocery.square-us"),
        # A `.` after the `:` stays: only the first separator is swapped.
        ("consumer:us-1.2", "consumer.us-1.2"),
    ],
)
def test_client_id_round_trip(agent_id: str, client_id: str) -> None:
    assert c.agent_id_to_client_id(agent_id) == client_id
    assert c.client_id_to_agent_id(client_id) == agent_id
    assert c.client_id_to_agent_id(c.agent_id_to_client_id(agent_id)) == agent_id


def test_every_error_has_a_known_reason() -> None:
    classes = [
        obj
        for _, obj in inspect.getmembers(errors, inspect.isclass)
        if issubclass(obj, errors.IdentityError | errors.DependencyUnavailable)
    ]
    assert {cls.__name__ for cls in classes} >= {
        "IdentityError",
        "TokenRefused",
        "GrantError",
        "GrantRevoked",
        "GrantExpired",
        "GrantMissing",
        "LinkInvalid",
        "DependencyUnavailable",
        "IdentityUnavailable",
        "AuthzUnavailable",
    }
    for cls in classes:
        if cls in (errors.IdentityError, errors.GrantError, errors.DependencyUnavailable):
            continue  # abstract bases: every concrete subclass sets `reason`
        assert cls().reason in c.REASONS, cls.__name__
    assert errors.GrantRevoked().reason == c.GRANT_REVOKED
    assert errors.GrantExpired().reason == c.GRANT_EXPIRED
    assert errors.GrantMissing().reason == c.GRANT_MISSING
    assert errors.IdentityUnavailable().reason == c.IDENTITY_UNAVAILABLE
    assert errors.AuthzUnavailable().reason == c.AUTHZ_UNAVAILABLE
    assert errors.TokenRefused(c.ACT_MISMATCH).reason == c.ACT_MISMATCH
    with pytest.raises(ValueError):
        errors.TokenRefused("not_a_reason")
    for reason in (c.GRANT_REVOKED, c.GRANT_EXPIRED, c.GRANT_MISSING):
        assert errors.grant_error(reason)().reason == reason
