import pytest

from migrations.apply import pending
from services.agents.common.trace import KINDS, validate


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_known_kinds_are_accepted(kind: str) -> None:
    validate(kind, {})


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown trace kind"):
        validate("thought", {})


def test_non_dict_payload_is_rejected() -> None:
    with pytest.raises(TypeError):
        validate("message", ["not", "a", "dict"])


def test_kinds_match_design() -> None:
    assert KINDS == {"message", "tool_call", "tool_result", "decision", "approval", "outcome"}


def test_pending_migrations_are_sorted_and_skip_applied() -> None:
    available = ["0002_b.sql", "0001_a.sql", "0003_c.sql"]
    assert pending(available, {"0002_b.sql"}) == ["0001_a.sql", "0003_c.sql"]
