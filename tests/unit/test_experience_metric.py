"""The `experience_weekly_asks` and `experience_weekly_guards` views (experience XG1, EX-I4,
EX-I21; issue #291)."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import pytest

from services.common import ids
from tests.unit import api_world as w
from tests.unit import experience_world as x

pytestmark = pytest.mark.db

HID = "us-7-eval"
OTHER = "us-8-eval"
WEEK = date(2026, 10, 5)  # a Monday
# Wednesday of WEEK, midday in New York.
WED = datetime(2026, 10, 7, 16, 0, tzinfo=UTC)


def _asks(pg: str) -> list[dict[str, Any]]:
    return w.query(
        pg,
        "SELECT household_id, user_id, week, asks FROM experience_weekly_asks"
        " ORDER BY household_id, user_id, week",
    )


def _guards(pg: str, household_id: str = HID) -> list[dict[str, Any]]:
    return w.query(
        pg,
        "SELECT * FROM experience_weekly_guards WHERE household_id = %s ORDER BY week",
        household_id,
    )


def test_counts_delivered_decisions_only(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        x.household(conn, HID)
        alice = x.person(conn, HID, "Alice")
        # Delivered decisions: each decision prefix, sent or as a template, any non-failed
        # status.
        x.send(conn, alice, WED, ["ap:1:yes", "ap:1:no"])
        x.send(conn, alice, WED, ["tt:1:yes"], outcome="template")
        x.send(conn, alice, WED, ["ob:1:yes"], status="delivered")
        x.send(conn, alice, WED, ["sf:1:0"], status="read")
        # Not delivered, or not a decision.
        x.send(conn, alice, WED, ["ap:2:yes"], status="failed")
        for outcome in ("held", "skipped", "refused"):
            x.send(conn, alice, WED, ["ap:3:yes"], outcome=outcome)
        x.send(conn, alice, WED, [])
        x.send(conn, alice, WED, ["xap:1:yes"])
    assert _asks(pg) == [{"household_id": HID, "user_id": alice, "week": WEEK, "asks": 4}]


def test_held_counted_once(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        x.household(conn, HID)
        alice = x.person(conn, HID, "Alice")
        # Held outside the window, then released on Alice's next message (EX-I20): two send
        # rows for one decision, and only the release is a delivery.
        original = x.send(conn, alice, WED, ["ap:1:yes"], outcome="held")
        held_id = x.held(conn, alice, original, "released", WED)
        x.send(
            conn,
            alice,
            WED + timedelta(hours=2),
            ["ap:1:yes"],
            message_id=ids.held_release_id(held_id),
        )
    assert [r["asks"] for r in _asks(pg)] == [1]


def test_stop_and_open_buttons_not_counted(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        x.household(conn, HID)
        alice = x.person(conn, HID, "Alice")
        x.send(conn, alice, WED, [ids.REMINDER_STOP])
        x.send(conn, alice, WED, [ids.page_button("household")])
        x.send(conn, alice, WED, [ids.page_button("pantry")])
        x.send(conn, alice, WED, [ids.page_button("renew")])
        assert _asks(pg) == []
        # A decision with an open button next to it is still one decision.
        x.send(conn, alice, WED, ["ap:1:yes", ids.page_button("household")])
    assert [r["asks"] for r in _asks(pg)] == [1]


def test_guard_metrics(pg: str) -> None:
    with psycopg.connect(pg) as conn:
        for hid in (HID, OTHER):
            x.household(conn, hid)
        alice = x.person(conn, HID, "Alice")
        bob = x.person(conn, OTHER, "Bob")
        for hid, user in ((HID, alice), (OTHER, bob)):
            x.approval(conn, hid, "expired", WED)
            x.approval(conn, hid, "rejected", WED)
            x.order(conn, hid, "cancelled", WED)
            x.order(conn, hid, "expired", WED)
            x.tool_result(conn, hid, True, WED)
            x.audit(conn, user, "consent_revoked", WED)
            dropped = x.send(conn, user, WED, ["ap:1:yes"], outcome="held")
            x.held(conn, user, dropped, "dropped", WED)
            x.send(conn, user, WED, ["ap:2:yes"], outcome="skipped")
            x.send(conn, user, WED, ["ap:3:yes"], outcome="refused")
        # Rows that are not guard events.
        x.approval(conn, HID, "approved", WED)
        x.approval(conn, HID, "pending", WED)
        x.order(conn, HID, "delivered", WED)
        x.tool_result(conn, HID, False, WED)
        x.audit(conn, alice, "consent_granted", WED)
        pending = x.send(conn, alice, WED, ["ap:4:yes"], outcome="held")
        x.held(conn, alice, pending, "pending", WED)
        x.send(conn, alice, WED, ["ap:5:yes"])
        x.send(conn, alice, WED, ["ap:6:yes"], outcome="template")
    [row] = _guards(pg)
    assert row == {
        "household_id": HID,
        "week": WEEK,
        "approvals_expired": 1,
        "orders_ended": 2,
        "rejects": 1,
        "safety_refusals": 1,
        "revokes": 1,
        "held_dropped": 1,
        "sends_not_delivered": 2,
    }


def test_week_in_household_timezone(pg: str) -> None:
    # Monday 2026-10-12 03:00 UTC is still Sunday in Los Angeles and already Monday in Tokyo.
    at = datetime(2026, 10, 12, 3, 0, tzinfo=UTC)
    with psycopg.connect(pg) as conn:
        x.household(conn, HID, "America/Los_Angeles")
        x.household(conn, OTHER, "Asia/Tokyo")
        west = x.person(conn, HID, "Alice")
        east = x.person(conn, OTHER, "Bob")
        for hid, user in ((HID, west), (OTHER, east)):
            x.send(conn, user, at, ["ap:1:yes"])
            x.approval(conn, hid, "expired", at)
    weeks = {r["household_id"]: r["week"] for r in _asks(pg)}
    assert weeks == {HID: date(2026, 10, 5), OTHER: date(2026, 10, 12)}
    assert [r["week"] for r in _guards(pg, HID)] == [date(2026, 10, 5)]
    assert [r["week"] for r in _guards(pg, OTHER)] == [date(2026, 10, 12)]


def test_falls_across_weeks(pg: str) -> None:
    # Three weeks: asks fall 3, 2, 1 for each adult while the guard metrics hold steady
    # (XG1's success target).
    with psycopg.connect(pg) as conn:
        x.household(conn, HID)
        adults = [x.person(conn, HID, "Alice"), x.person(conn, HID, "Bob", "member")]
        for n, asks in enumerate((3, 2, 1)):
            at = WED + timedelta(weeks=n)
            for user in adults:
                for i in range(asks):
                    x.send(conn, user, at, [f"ap:{n}{i}:yes"])
            x.approval(conn, HID, "expired", at)
            x.send(conn, adults[0], at, ["ap:x:yes"], outcome="skipped")
    weeks = [WEEK + timedelta(weeks=n) for n in range(3)]
    for user in adults:
        rows = [r for r in _asks(pg) if r["user_id"] == user]
        assert [(r["week"], r["asks"]) for r in rows] == list(zip(weeks, (3, 2, 1), strict=True))
    guards = _guards(pg)
    assert [g["week"] for g in guards] == weeks
    for metric in ("approvals_expired", "sends_not_delivered"):
        assert [g[metric] for g in guards] == [1, 1, 1]
    for earlier, later in zip(guards, guards[1:], strict=False):
        for metric, value in later.items():
            if metric not in ("household_id", "week"):
                assert value <= earlier[metric], metric
