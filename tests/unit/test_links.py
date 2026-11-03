"""Single-use Links (identity-authz §6 and I21)."""

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import psycopg
import pytest

from services.identity import fga, links
from services.identity.errors import LinkInvalid
from tests.unit import api_world as aw
from tests.unit import identity_world as w

db = pytest.mark.db

HOUSEHOLD = "h-links"


@dataclass(frozen=True)
class _Settings:
    domain: str = "kitchenless.test"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(links, "get_settings", lambda: _Settings())


@dataclass
class _Checks:
    """OpenFGA checks made, answered from `allowed` (the guardian tuples)."""

    made: list[tuple[str, str, str]] = field(default_factory=list)
    allowed: set[tuple[str, str, str]] = field(default_factory=set)

    def check(self, user: str, relation: str, object: str) -> tuple[bool, str]:
        self.made.append((user, relation, object))
        return (user, relation, object) in self.allowed, "model-1"


@pytest.fixture
def checks(monkeypatch: pytest.MonkeyPatch) -> _Checks:
    fake = _Checks()
    monkeypatch.setattr(fga, "check", fake.check)
    return fake


def _issue(pg: str, target: uuid.UUID, sent_to: uuid.UUID, **kw: Any) -> str:
    kw.setdefault("purpose", "consent")
    kw.setdefault("agent_ids", [f"household:{HOUSEHOLD}"])
    return aw.run(
        pg,
        lambda conn: links.issue(conn, kw["purpose"], target, sent_to, HOUSEHOLD, kw["agent_ids"]),
    )


def _token(url: str) -> str:
    prefix = "https://kitchenless.test/me/l/"
    assert url.startswith(prefix)
    return url.removeprefix(prefix)


def _use(pg: str, token: str) -> links.Link:
    async def go(conn: psycopg.AsyncConnection) -> links.Link:
        try:
            return await links.use(conn, token)
        finally:
            await conn.commit()  # keep `link_rejected`, as the route does

    return aw.run(pg, go)


def _admin(pg: str) -> w.Person:
    with psycopg.connect(pg) as conn:
        return w.add_person(conn, HOUSEHOLD)


def _audit(pg: str) -> list[dict[str, Any]]:
    return aw.query(pg, "SELECT * FROM audit_events ORDER BY event_id")


@db
def test_link_single_use(pg: str) -> None:
    admin = _admin(pg)
    token = _token(_issue(pg, admin.user_id, admin.user_id))
    first = _use(pg, token)
    assert (first.purpose, first.target_user_id, first.household_id) == (
        "consent",
        admin.user_id,
        HOUSEHOLD,
    )
    assert first.agent_ids == (f"household:{HOUSEHOLD}",)
    with pytest.raises(LinkInvalid):
        _use(pg, token)
    with pytest.raises(LinkInvalid):
        _use(pg, "not-a-token")
    # Only the hash is stored.
    [row] = aw.query(pg, "SELECT * FROM consent_links")
    assert bytes(row["link_hash"]) == hashlib.sha256(token.encode()).digest()
    assert row["used_at"] is not None


@db
def test_link_expires_after_600s(pg: str) -> None:
    admin = _admin(pg)
    token = _token(_issue(pg, admin.user_id, admin.user_id, purpose="me", agent_ids=[]))
    [row] = aw.query(pg, "SELECT created_at, expires_at FROM consent_links")
    assert (
        row["expires_at"] - row["created_at"]
        == timedelta(seconds=links.LINK_SECONDS)
        == (timedelta(seconds=600))
    )
    with psycopg.connect(pg) as conn:
        conn.execute("UPDATE consent_links SET expires_at = now() - interval '1 second'")
    with pytest.raises(LinkInvalid):
        _use(pg, token)
    assert [e["kind"] for e in _audit(pg)] == ["link_issued", "link_rejected"]


@db
def test_child_link_requires_guardian(pg: str, checks: _Checks) -> None:
    admin = _admin(pg)
    with psycopg.connect(pg) as conn:
        child = w.add_person(conn, HOUSEHOLD, role="child")
        other = w.add_person(conn, "h-other")
    # Not a guardian: refused, nothing stored.
    with pytest.raises(links.NotGuardian):
        _issue(pg, child.user_id, other.user_id, agent_ids=["consumer:h-links.2"])
    assert checks.made == [(f"user:{other.user_id}", "guardian", f"household:{HOUSEHOLD}")]
    assert aw.query(pg, "SELECT * FROM consent_links") == []
    # A guardian of the child's household may receive it.
    checks.allowed.add((f"user:{admin.user_id}", "guardian", f"household:{HOUSEHOLD}"))
    _issue(pg, child.user_id, admin.user_id, agent_ids=["consumer:h-links.2"])
    [row] = aw.query(pg, "SELECT * FROM consent_links")
    assert (row["target_user_id"], row["sent_to_user_id"]) == (child.user_id, admin.user_id)
    # An adult target needs no guardianship check.
    checks.made.clear()
    _issue(pg, admin.user_id, admin.user_id)
    assert checks.made == []


@db
def test_link_token_not_in_audit_detail(pg: str) -> None:
    admin = _admin(pg)
    url = _issue(pg, admin.user_id, admin.user_id)
    token = _token(url)
    _use(pg, token)
    with pytest.raises(LinkInvalid):
        _use(pg, token)
    events = _audit(pg)
    assert [e["kind"] for e in events] == ["link_issued", "link_used", "link_rejected"]
    digest = hashlib.sha256(token.encode()).hexdigest()
    for event in events:
        text = json.dumps(event, default=str)
        assert token not in text and url not in text and digest not in text
    assert events[2]["detail"] == {}


@db
def test_link_used_audited(pg: str) -> None:
    admin = _admin(pg)
    _use(pg, _token(_issue(pg, admin.user_id, admin.user_id)))
    issued, used = _audit(pg)
    assert (issued["kind"], issued["subject_user_id"], issued["household_id"]) == (
        "link_issued",
        admin.user_id,
        HOUSEHOLD,
    )
    assert issued["detail"]["purpose"] == "consent"
    assert (used["kind"], used["actor_user_id"], used["subject_user_id"]) == (
        "link_used",
        admin.user_id,
        admin.user_id,
    )
    assert used["detail"] == {"purpose": "consent", "agent_ids": [f"household:{HOUSEHOLD}"]}
