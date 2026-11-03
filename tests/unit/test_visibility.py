"""The §4 visibility table as code (experience.md EX-I23, XG9).

`test_matrix` asserts one row per §4 cell for the three viewer columns (self adult, other
adult, admin). The "Business" column has no `/me` viewer, so it is not a `Member`.
"""

import asyncio
import uuid
from typing import get_args

import psycopg
import pytest
from psycopg import AsyncConnection

from services.safety import scope
from services.safety.visibility import DATA_CLASSES, DataClass, Member, can_see, load_member
from tests.unit import identity_world as w

ADMIN = Member(uuid.uuid4(), "admin", is_approver=True, household_adults=3)
ADULT = Member(uuid.uuid4(), "member", is_approver=False, household_adults=3)
OTHER = Member(uuid.uuid4(), "member", is_approver=False, household_adults=3)
CHILD = Member(uuid.uuid4(), "child", is_approver=False, household_adults=3)

# (row, viewer, subject, expected): "self" is the adult viewing their own row; "other" is an
# adult viewing another adult's (or a child's) row; "admin" is the parent viewing someone
# else's row. Household-wide rows have no subject.
MATRIX: list[tuple[DataClass, str, Member, uuid.UUID | None, bool]] = [
    # Own allergies, diets, dislikes: Yes / No / Yes.
    ("own_constraints", "self", ADULT, ADULT.user_id, True),
    ("own_constraints", "other", ADULT, OTHER.user_id, False),
    ("own_constraints", "admin", ADMIN, ADULT.user_id, True),
    # A child's data: — / No / Yes.
    ("child_data", "other", ADULT, CHILD.user_id, False),
    ("child_data", "admin", ADMIN, CHILD.user_id, True),
    # Plan, order, explanation: Yes / Yes / Yes.
    ("plan", "self", ADULT, None, True),
    ("plan", "other", OTHER, None, True),
    ("plan", "admin", ADMIN, None, True),
    # Spending total vs budget: Yes / Yes / Yes.
    ("spending_total", "self", ADULT, None, True),
    ("spending_total", "other", OTHER, None, True),
    ("spending_total", "admin", ADMIN, None, True),
    # Spending per order, approvals: orders the person started / No / Yes.
    ("spending_per_order", "self", ADULT, ADULT.user_id, True),
    ("spending_per_order", "other", ADULT, OTHER.user_id, False),
    ("spending_per_order", "admin", ADMIN, ADULT.user_id, True),
    # Take-turns state: Yes / Yes / Yes (the switch is an action, not visibility).
    ("take_turns", "self", ADULT, None, True),
    ("take_turns", "other", OTHER, None, True),
    ("take_turns", "admin", ADMIN, None, True),
    # Meal reaction totals: Yes if ≥3 adults, in every column.
    ("reaction_totals", "self", ADULT, None, True),
    ("reaction_totals", "other", OTHER, None, True),
    ("reaction_totals", "admin", ADMIN, None, True),
    # Pantry presence: Yes / Yes / Yes.
    ("pantry", "self", ADULT, None, True),
    ("pantry", "other", OTHER, None, True),
    ("pantry", "admin", ADMIN, None, True),
    # Name, address, phone: own / name only (contact details refused) / Yes.
    ("contact", "self", ADULT, ADULT.user_id, True),
    ("contact", "other", ADULT, OTHER.user_id, False),
    ("contact", "admin", ADMIN, ADULT.user_id, True),
]


@pytest.mark.parametrize(("data", "column", "viewer", "subject", "expected"), MATRIX)
def test_matrix(
    data: DataClass, column: str, viewer: Member, subject: uuid.UUID | None, expected: bool
) -> None:
    assert can_see(viewer, data, subject) is expected, (data, column)


def test_matrix_covers_every_row() -> None:
    assert {row[0] for row in MATRIX} == DATA_CLASSES == set(get_args(DataClass))
    assert len(DATA_CLASSES) == 9  # one per §4 row


def test_admin_sees_a_childs_constraints_and_contact() -> None:
    for data in ("own_constraints", "child_data", "contact", "spending_per_order"):
        assert can_see(ADMIN, data, CHILD.user_id), data  # type: ignore[arg-type]
    # An order nobody started (a schedule) is the admin's to see, not another adult's.
    assert can_see(ADMIN, "spending_per_order", None)
    assert not can_see(ADULT, "spending_per_order", None)


def test_child_sees_only_self() -> None:
    for data in get_args(DataClass):
        mine = can_see(CHILD, data, CHILD.user_id)
        assert mine is (data in ("own_constraints", "child_data", "contact")), data
        for other in (ADMIN.user_id, ADULT.user_id, uuid.uuid4()):
            assert not can_see(CHILD, data, other), data
        assert not can_see(CHILD, data, None), data


def test_reactions_hidden_below_three_adults() -> None:
    for adults in (0, 1, 2):
        for role in ("admin", "member", "child"):
            viewer = Member(
                uuid.uuid4(), role, is_approver=role == "admin", household_adults=adults
            )  # type: ignore[arg-type]
            assert not can_see(viewer, "reaction_totals", None), (role, adults)
    for adults in (3, 4):
        for role in ("admin", "member"):
            viewer = Member(uuid.uuid4(), role, is_approver=False, household_adults=adults)  # type: ignore[arg-type]
            assert can_see(viewer, "reaction_totals", None), (role, adults)
    # A child never sees household-wide rows, however many adults.
    assert not can_see(CHILD, "reaction_totals", None)


def test_fails_closed() -> None:
    # A per-person row with no subject, an unknown data class, an unknown role.
    for data in ("own_constraints", "child_data", "contact"):
        assert not can_see(ADMIN, data, None), data  # type: ignore[arg-type]
    assert not can_see(ADMIN, "budgets", None)  # type: ignore[arg-type]
    stranger = Member(uuid.uuid4(), "owner", is_approver=True, household_adults=5)  # type: ignore[arg-type]
    for data in get_args(DataClass):
        assert not can_see(stranger, data, stranger.user_id), data


def test_approver_flag_does_not_widen_visibility() -> None:
    approver = Member(ADULT.user_id, "member", is_approver=True, household_adults=3)
    for data in get_args(DataClass):
        for subject in (OTHER.user_id, CHILD.user_id):
            assert can_see(approver, data, subject) is can_see(ADULT, data, subject), data


@pytest.mark.db
def test_load_member(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        world = w.add_world(conn, "h-1")
        adult = w.add_person(conn, "h-1", "member")
        child = w.add_person(conn, "h-1", "child")
        w.add_world(conn, "h-2")
        stranger = w.add_person(conn, "h-2", "member")
        conn.commit()

    async def go() -> list[Member]:
        async with await AsyncConnection.connect(pg) as conn:
            hh = scope.HouseholdScope("h-1", "session")
            out = [await load_member(conn, hh, p.user_id) for p in (world.admin, adult, child)]
            with pytest.raises(LookupError):
                await load_member(conn, hh, stranger.user_id)
            with pytest.raises(LookupError):
                await load_member(conn, hh, uuid.uuid4())
            return out

    admin, member, kid = asyncio.run(go())
    assert admin == Member(world.admin.user_id, "admin", True, 2)
    assert member == Member(adult.user_id, "member", False, 2)
    assert kid == Member(child.user_id, "child", False, 2)
