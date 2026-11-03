"""Who in a household sees what (experience.md §4 "Visibility", EX-I23).

The §4 table as code, used by every `/me` query. One `DataClass` per table row; the viewer
is a `Member` of the household, loaded by `load_member` through `scope.scoped`. The
"Business" column is not here: businesses never sign in to `/me` and get only what
agent-protocol and the insights rule (EX-OD12) give them.

*   Admin (parent): sees every row, for every member, children included.
*   Other adult (`member`): household-wide rows, plus their own constraints, their own
    spending per order (orders they started) and their own contact details.
*   Child: only its own row (its constraints, its child data, its contact details);
    nothing household-wide.
*   Meal reaction totals: only when the household has at least 3 adults, for everyone.

Anything not in the table (an unknown role or data class, or a per-person row with no
subject) is refused.
"""

from dataclasses import dataclass
from typing import Literal, get_args
from uuid import UUID

from psycopg import AsyncConnection

from services.safety import scope

__all__ = [
    "DATA_CLASSES",
    "REACTION_MIN_ADULTS",
    "DataClass",
    "Member",
    "Role",
    "can_see",
    "load_member",
]

DataClass = Literal[
    "own_constraints",  # own allergies, diets, dislikes
    "child_data",  # a child's data
    "plan",  # plan, order, explanation
    "spending_total",  # spending total vs budget
    "spending_per_order",  # spending per order, approvals
    "take_turns",  # take-turns state
    "reaction_totals",  # meal reaction totals
    "pantry",  # pantry presence
    "contact",  # name, address, phone
]
Role = Literal["admin", "member", "child"]

DATA_CLASSES: frozenset[str] = frozenset(get_args(DataClass))
REACTION_MIN_ADULTS = 3

# Rows every adult sees, about the household as a whole.
_HOUSEHOLD_WIDE = frozenset({"plan", "spending_total", "take_turns", "pantry"})
# Rows an adult member sees only about themselves.
_MEMBER_OWN = frozenset({"own_constraints", "spending_per_order", "contact"})
# Rows a child sees, only about itself.
_CHILD_OWN = frozenset({"own_constraints", "child_data", "contact"})
# Rows that are always about one person; with no subject they are refused.
_PER_PERSON = frozenset({"own_constraints", "child_data", "contact"})


@dataclass(frozen=True)
class Member:
    user_id: UUID
    role: Role
    is_approver: bool
    household_adults: int


_LOAD_SQL = (
    "SELECT m.role,"
    " EXISTS (SELECT 1 FROM approval_rules r WHERE r.household_id = %(household)s"
    " AND r.approver_user_id = m.user_id),"
    " (SELECT count(*) FROM household_members a WHERE a.household_id = %(household)s"
    " AND a.role IN ('admin', 'member'))"
    " FROM household_members m"
    " WHERE m.household_id = %(household)s AND m.user_id = %(user_id)s"
)


async def load_member(
    conn: AsyncConnection, household: scope.HouseholdScope, user_id: UUID
) -> Member:
    """The viewer `user_id` in the scope's household. Raises `LookupError` when they are
    not a member of it."""
    rows = await scope.scoped(conn, household).fetch(_LOAD_SQL, user_id=user_id)
    if not rows:
        raise LookupError(f"user {user_id} is not a member of household {household.household_id}")
    role, is_approver, adults = rows[0]
    return Member(
        user_id=user_id, role=role, is_approver=bool(is_approver), household_adults=int(adults)
    )


def can_see(viewer: Member, data: DataClass, subject_user_id: UUID | None) -> bool:
    """Whether `viewer` may see `data` about `subject_user_id` (the person the row is about:
    the constraint holder, the child, the order's starter, the contact; None for
    household-wide rows). The caller has already scoped the subject to the household."""
    if data not in DATA_CLASSES:
        return False
    if data in _PER_PERSON and subject_user_id is None:
        return False
    is_self = subject_user_id is not None and subject_user_id == viewer.user_id
    if viewer.role == "child":
        return is_self and data in _CHILD_OWN
    if viewer.role not in ("admin", "member"):
        return False
    if data == "reaction_totals":
        return viewer.household_adults >= REACTION_MIN_ADULTS
    if viewer.role == "admin" or data in _HOUSEHOLD_WIDE:
        return True
    return is_self and data in _MEMBER_OWN
