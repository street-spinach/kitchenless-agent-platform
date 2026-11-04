"""`GET /me/orders/{ref}` and `GET /me/orders/latest` (showcase.md §4)."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest

from services.common import ids
from tests.unit import experience_world as ew
from tests.unit import identity_world as w
from tests.unit.test_me_household import _named
from tests.unit.test_me_routes import (
    HOUSEHOLD,
    FakeKeycloak,
    World,
    _client,
    _session_cookie,
    make_kc,
    make_world,
)
from tests.unit.test_story import RATIONALE, bare_order, seed_order

db = pytest.mark.db

# As the page escapes them.
NOT_FOUND = "We couldn&#39;t find that order."
NOT_AVAILABLE = "This page isn&#39;t available."


@pytest.fixture
def kc(monkeypatch: pytest.MonkeyPatch) -> FakeKeycloak:
    return make_kc(monkeypatch)


@pytest.fixture
def world(pg: str, kc: FakeKeycloak) -> World:
    made = make_world(pg, kc)
    _named(pg, made)
    return made


def _ref(order_id: UUID) -> str:
    return ids.org_order_ref(HOUSEHOLD, order_id)


def _get(world: World, person: w.Person, path: str, household: str = HOUSEHOLD) -> Any:
    client = _client(world.pg)
    client.cookies.update(_session_cookie(person, household))
    return client.get(path, follow_redirects=False)


@db
def test_admin_sees_every_section(world: World) -> None:
    seeded = seed_order(world.pg, world)
    page = _get(world, world.admin, f"/me/orders/{_ref(seeded.order_id)}")
    assert page.status_code == 200
    for heading in (
        "Who we asked",
        "Negotiation",
        "Checks in code",
        "Delivery",
        "Behind the scenes",
    ):
        assert heading in page.text
    assert "Fresh Market" in page.text and "$182.40" in page.text and "$200.00" in page.text
    assert "Courier booked" in page.text
    assert "tree nuts" in page.text and "tree_nuts" not in page.text
    assert RATIONALE in page.text
    assert 'aria-current="page"' in page.text  # the Household tab
    # The viewer's own name is in the header, as on every page; nobody else's is anywhere.
    assert "Sam" not in page.text and "Kavi" not in page.text


@db
def test_adult_who_started_the_order_sees_amounts(world: World) -> None:
    seeded = seed_order(world.pg, world, world.member)
    page = _get(world, world.member, f"/me/orders/{_ref(seeded.order_id)}")
    assert page.status_code == 200
    assert "$182.40" in page.text and "Approval" in page.text


@db
def test_other_adult_gets_the_story_without_spending_or_approvals(world: World) -> None:
    """Experience §4: "Spending per order, approvals" stays with the admin and the starter."""
    seeded = seed_order(world.pg, world, world.admin)
    page = _get(world, world.member, f"/me/orders/{_ref(seeded.order_id)}")
    assert page.status_code == 200
    assert "Fresh Market" in page.text and "Allergens" in page.text
    assert "$" not in page.text
    assert RATIONALE not in page.text
    assert "Approval" not in page.text.replace("Amounts and approvals", "")
    assert "Amounts and approvals show to the person who started this order." in page.text


@db
def test_child_is_refused(world: World) -> None:
    seeded = seed_order(world.pg, world)
    page = _get(world, world.child, f"/me/orders/{_ref(seeded.order_id)}")
    assert page.status_code == 403
    assert NOT_AVAILABLE in page.text
    assert "Fresh Market" not in page.text


@db
def test_child_learns_nothing_about_refs(world: World) -> None:
    seeded = seed_order(world.pg, world)
    known = _get(world, world.child, f"/me/orders/{_ref(seeded.order_id)}")
    unknown = _get(world, world.child, "/me/orders/AAAAAAAA")
    assert (known.status_code, known.text) == (unknown.status_code, unknown.text)


@db
def test_child_is_refused_latest(world: World) -> None:
    seed_order(world.pg, world)
    page = _get(world, world.child, "/me/orders/latest")
    assert page.status_code == 403
    assert "location" not in page.headers


@db
def test_other_household_gets_404(world: World) -> None:
    seeded = seed_order(world.pg, world)
    for ref in (_ref(seeded.order_id), ids.org_order_ref("h-other", seeded.order_id)):
        page = _get(world, world.other_admin, f"/me/orders/{ref}", "h-other")
        assert page.status_code == 404
        assert NOT_FOUND in page.text
        assert "Fresh Market" not in page.text


@db
def test_unknown_ref_is_404(world: World) -> None:
    page = _get(world, world.admin, "/me/orders/AAAAAAAA")
    assert page.status_code == 404
    assert NOT_FOUND in page.text


@db
def test_signed_out(world: World) -> None:
    for path in ("/me/orders/AAAAAAAA", "/me/orders/latest"):
        assert _client(world.pg).get(path).status_code == 401


@db
def test_session_of_a_non_member_is_signed_out(world: World) -> None:
    page = _get(world, world.other_admin, "/me/orders/latest")
    assert page.status_code == 401


@db
def test_latest_redirects_to_the_newest_order_past_draft(world: World) -> None:
    now = datetime.now(UTC)
    bare_order(world.pg, "delivered", now - timedelta(days=2))
    newest = bare_order(world.pg, "quoting", now - timedelta(days=1))
    bare_order(world.pg, "draft", now)
    page = _get(world, world.member, "/me/orders/latest")
    assert page.status_code == 303
    assert page.headers["location"] == f"/me/orders/{_ref(newest.order_id)}"


@db
def test_latest_with_no_orders(world: World) -> None:
    bare_order(world.pg, "draft")
    page = _get(world, world.admin, "/me/orders/latest")
    assert page.status_code == 200
    assert "No orders yet." in page.text


@db
def test_latest_stays_in_the_household(world: World) -> None:
    bare_order(world.pg, "quoting")
    page = _get(world, world.other_admin, "/me/orders/latest", "h-other")
    assert page.status_code == 200
    assert "No orders yet." in page.text


@db
def test_household_page_links_each_order_to_its_story(world: World) -> None:
    week = seed_order(world.pg, world)
    long_ago = datetime.now(UTC) - timedelta(days=30)
    with psycopg.connect(world.pg) as conn:
        dinner, _ = ew.order(conn, HOUSEHOLD, "delivered", long_ago)
        conn.execute("UPDATE orders SET kind = 'dinner' WHERE order_id = %s", (dinner,))
    page = _get(world, world.admin, "/me/household")
    assert f'href="/me/orders/{_ref(week.order_id)}"' in page.text
    assert f'href="/me/orders/{_ref(dinner)}"' in page.text
    assert page.text.count("See how this order was made →") == 2


@db
def test_child_household_page_has_no_story_link(world: World) -> None:
    seed_order(world.pg, world)
    page = _get(world, world.child, "/me/household")
    assert "/me/orders/" not in page.text
