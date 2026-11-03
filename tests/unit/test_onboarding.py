"""`services/identity/onboarding.py` (identity-authz §5 and I23), Keycloak admin mocked."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from services.common import ids
from services.identity import fga, onboarding

pytestmark = pytest.mark.db

PHONE = "+15550100601"
MEMBER_PHONE = "+15550100602"
ADDRESS = {
    "line1": "300 Main Street",
    "city": "Cincinnati",
    "region": "OH",
    "postal_code": "45202",
    "country": "US",
}


class FakeKeycloak:
    """Records the Admin API calls; ids are fixed per username or client, as Keycloak's
    409 path returns the existing id."""

    def __init__(self) -> None:
        self.users: list[tuple[str, str]] = []
        self.clients: list[tuple[str, str]] = []

    async def create_user(
        self, username: str, display_name: str, attributes: dict[str, list[str]] | None = None
    ) -> str:
        self.users.append((username, display_name))
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"kc-user|{username}"))

    async def create_agent_client(self, agent_id: str, template: str) -> str:
        self.clients.append((agent_id, template))
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"kc-client|{agent_id}"))


@pytest.fixture
def keycloak() -> FakeKeycloak:
    return FakeKeycloak()


@pytest.fixture(autouse=True)
def tuples(monkeypatch: pytest.MonkeyPatch) -> list[fga.Tuple]:
    written: list[fga.Tuple] = []
    monkeypatch.setattr(fga, "ensure", lambda ts: written.extend(ts))
    return written


def _run[T](pg: str, fn: Callable[[psycopg.AsyncConnection], Awaitable[T]]) -> T:
    async def go() -> T:
        async with await psycopg.AsyncConnection.connect(pg) as conn:
            return await fn(conn)

    return asyncio.run(go())


def _rows(pg: str, sql: str, *params: Any) -> list[dict[str, Any]]:
    with psycopg.connect(pg) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _admin(pg: str, keycloak: FakeKeycloak) -> uuid.UUID:
    return _run(
        pg,
        lambda conn: onboarding.create_person(
            conn, PHONE, "Asha", None, "admin", ADDRESS, keycloak=keycloak
        ),
    )


def _household(pg: str, user_id: uuid.UUID) -> str:
    [row] = _rows(pg, "SELECT household_id FROM household_members WHERE user_id = %s", user_id)
    return str(row["household_id"])


def test_first_admin_creates_household_and_approver(
    pg: str, keycloak: FakeKeycloak, tuples: list[fga.Tuple]
) -> None:
    user_id = _admin(pg, keycloak)

    assert user_id == ids.person_user_id(PHONE)
    hid = _household(pg, user_id)
    assert hid.startswith("hh-") and len(hid) == len("hh-") + 8
    [household] = _rows(pg, "SELECT * FROM households WHERE household_id = %s", hid)
    assert (household["env"], household["region"], household["zone_id"]) == (
        "live",
        "us",
        "us-oh-45202",
    )
    assert (household["timezone"], household["currency"]) == ("America/New_York", "USD")
    [address] = _rows(pg, "SELECT * FROM household_addresses WHERE household_id = %s", hid)
    assert address["address_ref"] == ids.household_address_ref(hid)
    assert address["line1"] == ADDRESS["line1"]
    [rules] = _rows(pg, "SELECT approver_user_id FROM approval_rules WHERE household_id = %s", hid)
    assert rules["approver_user_id"] == user_id
    [member] = _rows(pg, "SELECT role FROM household_members WHERE user_id = %s", user_id)
    assert member["role"] == "admin"
    [user] = _rows(pg, "SELECT display_name, keycloak_sub FROM users WHERE user_id = %s", user_id)
    assert user["display_name"] == "Asha"
    assert keycloak.users == [(PHONE, "Asha")]
    assert user["keycloak_sub"] == str(uuid.uuid5(uuid.NAMESPACE_URL, f"kc-user|{PHONE}"))
    [phone] = _rows(pg, "SELECT * FROM channel_identities WHERE user_id = %s", user_id)
    assert (phone["phone_e164"], phone["channel_mode"]) == (PHONE, "real")

    household_agent = f"household:{hid}"
    consumer = f"consumer:{hid}.1"
    cards = {
        r["agent_id"]: r
        for r in _rows(
            pg, "SELECT * FROM agent_cards WHERE agent_id IN (%s, %s)", household_agent, consumer
        )
    }
    assert cards[household_agent]["owner_ref"] == household_agent
    assert cards[household_agent]["class"] == "household"
    assert cards[consumer]["owner_ref"] == f"user:{user_id}"
    assert cards[consumer]["zone_ids"] == ["us-oh-45202"]
    assert keycloak.clients == [
        (household_agent, onboarding.TEMPLATE_HOUSEHOLD),
        (consumer, onboarding.TEMPLATE_CONSUMER),
    ]
    clients = _rows(pg, "SELECT agent_id, client_id, kind FROM agent_clients ORDER BY agent_id")
    assert [(c["agent_id"], c["client_id"], c["kind"]) for c in clients] == [
        (consumer, f"consumer.{hid}.1", "people"),
        (household_agent, f"household.{hid}", "people"),
    ]
    audits = _rows(pg, "SELECT kind, subject_user_id, agent_id FROM audit_events ORDER BY event_id")
    assert [(a["kind"], a["subject_user_id"], a["agent_id"]) for a in audits] == [
        ("user_onboarded", user_id, None),
        ("agent_client_created", None, household_agent),
        ("agent_client_created", None, consumer),
    ]
    assert set(tuples) == {
        (f"user:{user_id}", "admin", f"household:{hid}"),
        (f"agent:household.{hid}", "acts_for", f"household:{hid}"),
        (f"agent:household.{hid}", "can_spend", f"budget:{hid}"),
        (f"agent:consumer.{hid}.1", "acts_for", f"user:{user_id}"),
    }


def test_create_person_idempotent(pg: str, keycloak: FakeKeycloak) -> None:
    admin = _admin(pg, keycloak)
    # A retried first-admin onboarding finds the household the phone already joined.
    assert _admin(pg, keycloak) == admin
    hid = _household(pg, admin)
    assert len(_rows(pg, "SELECT 1 FROM households")) == 1

    def member(conn: psycopg.AsyncConnection) -> Awaitable[uuid.UUID]:
        return onboarding.create_person(conn, MEMBER_PHONE, "Dev", hid, "member", keycloak=keycloak)

    first = _run(pg, member)
    second = _run(pg, member)

    assert first == second == ids.person_user_id(MEMBER_PHONE)
    for sql in (
        "SELECT 1 FROM users WHERE user_id = %s",
        "SELECT 1 FROM household_members WHERE user_id = %s",
        "SELECT 1 FROM channel_identities WHERE user_id = %s",
        "SELECT 1 FROM agent_cards WHERE owner_ref = 'user:' || %s::text",
    ):
        assert len(_rows(pg, sql, first)) == 1, sql
    assert len(_rows(pg, "SELECT 1 FROM agent_clients")) == 3
    # Keycloak was asked once per person and once per client.
    assert keycloak.users == [(PHONE, "Asha"), (MEMBER_PHONE, "Dev")]
    assert [c for c, _ in keycloak.clients] == [
        f"household:{hid}",
        f"consumer:{hid}.1",
        f"consumer:{hid}.2",
    ]
    onboarded = _rows(pg, "SELECT subject_user_id FROM audit_events WHERE kind = 'user_onboarded'")
    assert [r["subject_user_id"] for r in onboarded] == [admin, first]


def test_create_person_phone_in_other_household_refused(pg: str, keycloak: FakeKeycloak) -> None:
    admin = _admin(pg, keycloak)
    other = _run(
        pg,
        lambda conn: onboarding.create_person(
            conn, MEMBER_PHONE, "Ravi", None, "admin", ADDRESS, keycloak=keycloak
        ),
    )
    other_hid = _household(pg, other)
    with pytest.raises(onboarding.PhoneTaken):
        _run(
            pg,
            lambda conn: onboarding.create_person(
                conn, PHONE, "Asha", other_hid, "member", keycloak=keycloak
            ),
        )
    assert len(_rows(pg, "SELECT 1 FROM household_members WHERE user_id = %s", admin)) == 1


@pytest.mark.parametrize(
    ("phone", "household", "role", "address"),
    [
        ("5550100601", None, "admin", ADDRESS),  # not E.164
        (PHONE, None, "member", ADDRESS),  # a new household's first person is its admin
        (PHONE, None, "child", ADDRESS),  # children use create_child
        (PHONE, None, "admin", None),  # a new household needs an address
    ],
)
def test_create_person_bad_arguments_write_nothing(
    pg: str, keycloak: FakeKeycloak, phone: str, household: str | None, role: str, address: Any
) -> None:
    with pytest.raises(ValueError):
        _run(
            pg,
            lambda conn: onboarding.create_person(
                conn, phone, "Asha", household, role, address, keycloak=keycloak
            ),
        )
    assert _rows(pg, "SELECT 1 FROM users") == []
    assert _rows(pg, "SELECT 1 FROM households") == []


def test_create_child_has_no_phone(
    pg: str, keycloak: FakeKeycloak, tuples: list[fga.Tuple]
) -> None:
    admin = _admin(pg, keycloak)
    hid = _household(pg, admin)
    tuples.clear()

    def child(conn: psycopg.AsyncConnection) -> Awaitable[uuid.UUID]:
        return onboarding.create_child(conn, admin, hid, "Mia", keycloak=keycloak)

    mia = _run(pg, child)

    # The admin's consumer agent is n = 1, so the child is n = 2.
    assert mia == ids.child_user_id(hid, 2)
    assert _rows(pg, "SELECT 1 FROM channel_identities WHERE user_id = %s", mia) == []
    [member] = _rows(pg, "SELECT role FROM household_members WHERE user_id = %s", mia)
    assert member["role"] == "child"
    assert keycloak.users[-1] == (f"child.{hid}.2", "Mia")
    [user] = _rows(pg, "SELECT keycloak_sub FROM users WHERE user_id = %s", mia)
    assert user["keycloak_sub"] == str(uuid.uuid5(uuid.NAMESPACE_URL, f"kc-user|child.{hid}.2"))
    agent = f"consumer:{hid}.2"
    [card] = _rows(pg, "SELECT owner_ref FROM agent_cards WHERE agent_id = %s", agent)
    assert card["owner_ref"] == f"user:{mia}"
    assert keycloak.clients[-1] == (agent, onboarding.TEMPLATE_CONSUMER)
    [onboarded] = _rows(
        pg,
        "SELECT actor_user_id FROM audit_events WHERE kind = 'user_onboarded'"
        " AND subject_user_id = %s",
        mia,
    )
    assert onboarded["actor_user_id"] == admin
    # Guardianship needs no write: the child is a child of the household; no `can_spend`.
    assert set(tuples) == {
        (f"user:{mia}", "child", f"household:{hid}"),
        (f"agent:consumer.{hid}.2", "acts_for", f"user:{mia}"),
    }

    # Asked again (a repeated tap), the same child is returned and nothing is created.
    calls = (len(keycloak.users), len(keycloak.clients))
    assert _run(pg, child) == mia
    assert (len(keycloak.users), len(keycloak.clients)) == calls
    assert len(_rows(pg, "SELECT 1 FROM household_members WHERE role = 'child'")) == 1


def test_create_child_needs_an_admin(pg: str, keycloak: FakeKeycloak) -> None:
    admin = _admin(pg, keycloak)
    hid = _household(pg, admin)
    dev = _run(
        pg,
        lambda conn: onboarding.create_person(
            conn, MEMBER_PHONE, "Dev", hid, "member", keycloak=keycloak
        ),
    )
    with pytest.raises(onboarding.NotAdmin):
        _run(pg, lambda conn: onboarding.create_child(conn, dev, hid, "Mia", keycloak=keycloak))
    assert _rows(pg, "SELECT 1 FROM household_members WHERE role = 'child'") == []
