"""Reminders (experience.md §10, EX-I41): due rules, skips, and the send."""

import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest

from services.agents import reminders
from services.api.whatsapp import sender, templates
from services.common import ids
from tests.unit import api_world as aw
from tests.unit import experience_world as ew
from tests.unit import identity_world as iw

db = pytest.mark.db

TZ = ZoneInfo("America/New_York")
EVAL = aw.HOUSEHOLD_ID  # us-2-eval: the pantry is on
LIVE = aw.LIVE_HOUSEHOLD_ID  # us-2: the pantry is off
STORE = "grocery:store-b-us"
FRIDAY_NOON = datetime(2026, 11, 6, 12, 0, tzinfo=TZ)
SATURDAY_NOON = datetime(2026, 11, 7, 12, 0, tzinfo=TZ)


@pytest.fixture
def world(pg: str, monkeypatch: pytest.MonkeyPatch) -> str:
    aw.seed(pg)
    aw.patch_settings(monkeypatch)
    with psycopg.connect(pg) as conn:
        conn.execute(
            "INSERT INTO organizations (org_id, name, region) VALUES ('store-b', 'Store B', 'us')"
        )
        conn.execute(
            "INSERT INTO agent_cards (agent_id, class, owner_ref, region, host_process)"
            " VALUES (%s, 'grocery', 'org:store-b', 'us', 'agents')",
            (STORE,),
        )
        for household, approver in ((EVAL, aw.ADMIN), (LIVE, aw.LIVE_ADMIN)):
            conn.execute(
                "INSERT INTO approval_rules (household_id, approver_user_id) VALUES (%s, %s)",
                (household, approver),
            )
    return pg


def _delivery(
    pg: str, starts: datetime, state: str = "courier_booked", household: str = EVAL
) -> uuid.UUID:
    with psycopg.connect(pg) as conn:
        order_id, _ = ew.order(conn, household, state, starts - timedelta(days=1))
        conn.execute(
            "UPDATE orders SET seller_agent_id = %s, delivery_window = tstzrange(%s, %s)"
            " WHERE order_id = %s",
            (STORE, starts, starts + timedelta(hours=1), order_id),
        )
    return order_id


def _pass(pg: str, now: datetime) -> int:
    return aw.run(pg, lambda conn: reminders.reminder_pass(conn, now))


def _rows(pg: str) -> list[dict[str, Any]]:
    return aw.query(
        pg,
        "SELECT kind, ref_id, user_id, status, reason, message_id FROM reminders"
        " ORDER BY reminder_id",
    )


def _sent(pg: str) -> list[dict[str, Any]]:
    return aw.query(
        pg,
        "SELECT message_id, run_id, from_agent, to_agent, body FROM agent_messages"
        " WHERE type = 'whatsapp_out' ORDER BY created_at, message_id",
    )


@db
def test_delivery_today_due(world: str) -> None:
    order_id = _delivery(world, FRIDAY_NOON.replace(hour=18))
    _delivery(world, FRIDAY_NOON.replace(hour=18) + timedelta(days=1))  # tomorrow
    _delivery(world, FRIDAY_NOON.replace(hour=18), state="placed")  # not yet booked

    # Before 08:00 household time: not due.
    assert _pass(world, FRIDAY_NOON.replace(hour=7, minute=59)) == 0
    assert _rows(world) == []

    assert _pass(world, FRIDAY_NOON) == 2
    rows = _rows(world)
    assert {(r["kind"], r["ref_id"], r["user_id"], r["status"]) for r in rows} == {
        ("delivery_today", str(order_id), aw.ADMIN, "sent"),
        ("delivery_today", str(order_id), aw.MEMBER, "sent"),
    }
    sent = _sent(world)
    assert len(sent) == 2
    for msg in sent:
        body = msg["body"]
        reminder = next(r for r in rows if str(r["user_id"]) == body["user_id"])
        assert msg["message_id"] == reminder["message_id"]
        assert msg["from_agent"] == "system:sweeper" and msg["to_agent"] == "channel:whatsapp"
        assert body["template"] == "reminder_delivery_today"
        assert body["params"] == {"store": "Store B", "window": "6:00 pm-7:00 pm"}
        assert body["buttons"] == [{"payload": ids.REMINDER_STOP, "title": "Stop these"}]
        assert body["ref"]["kind"] == "reminder"
        assert msg["message_id"] == ids.reminder_message_id(int(body["ref"]["id"]))
        # The text renders with the code-built params.
        assert sender.graph_payload("+15550100299", body) is not None
    runs = aw.query(
        world, "SELECT household_id, trigger FROM runs WHERE run_id = %s", sent[0]["run_id"]
    )
    assert runs[0]["household_id"] is None
    assert runs[0]["trigger"]["source"] == "reminder"
    decisions = aw.query(
        world,
        "SELECT agent_id, payload FROM agent_traces WHERE kind = 'decision' AND run_id = %s",
        sent[0]["run_id"],
    )
    assert decisions[0]["agent_id"] == "system:sweeper"
    assert decisions[0]["payload"]["node"] == "reminder"


def _pantry_event(conn: psycopg.Connection, item: str, kind: str, at: datetime) -> None:
    conn.execute(
        "INSERT INTO pantry_events (household_id, item, pantry_item, delta, unit, source, kind, ts)"
        " VALUES (%s, %s, %s, 0, 'each', 'test', %s, %s)",
        (EVAL, item, item, kind, at),
    )


@db
def test_pantry_summary_saturday(world: str) -> None:
    with psycopg.connect(world) as conn:
        _pantry_event(conn, "rice", "verify", SATURDAY_NOON - timedelta(days=1))
        _pantry_event(conn, "milk", "add", SATURDAY_NOON - timedelta(hours=1))
        _pantry_event(conn, "eggs", "add", SATURDAY_NOON - timedelta(days=10))
        _pantry_event(conn, "bread", "add", SATURDAY_NOON - timedelta(days=5))
        _pantry_event(conn, "pasta", "out", SATURDAY_NOON - timedelta(hours=2))

    assert _pass(world, SATURDAY_NOON - timedelta(days=1)) == 0  # Friday
    assert _pass(world, SATURDAY_NOON.replace(hour=9, minute=59)) == 0  # before 10:00
    assert _pass(world, SATURDAY_NOON) == 1
    [row] = _rows(world)
    assert (row["kind"], row["ref_id"], row["user_id"]) == (
        "pantry_summary",
        f"{EVAL}:2026-W45",
        aw.ADMIN,
    )
    [msg] = _sent(world)
    body = msg["body"]
    assert body["template"] == "reminder_pantry_summary"
    assert body["params"] == {
        "at_home": "milk, rice",
        "unsure": "bread, eggs",
        "at_home_count": "2",
        "unsure_count": "2",
    }
    assert body["buttons"] == [
        {"payload": ids.page_button("pantry"), "title": "Open pantry"},
        {"payload": ids.REMINDER_STOP, "title": "Stop these"},
    ]


@db
def test_pantry_off_no_summary(world: str) -> None:
    """A live household's pantry is off (EX-A17): no summary, and nothing recorded."""
    assert _pass(world, SATURDAY_NOON) == 1  # the eval household's only
    assert {r["ref_id"].split(":")[0] for r in _rows(world)} == {EVAL}
    assert all(r["ref_id"].split(":")[0] != LIVE for r in _rows(world))


@db
def test_daily_cap(world: str) -> None:
    """At most one reminder per person per household day; the rest are recorded skipped."""
    first = _delivery(world, FRIDAY_NOON.replace(hour=17))
    second = _delivery(world, FRIDAY_NOON.replace(hour=19))
    assert _pass(world, FRIDAY_NOON) == 2  # one each for the admin and the member
    rows = _rows(world)
    sent = [r for r in rows if r["status"] == "sent"]
    skipped = [r for r in rows if r["status"] == "skipped"]
    assert {r["user_id"] for r in sent} == {aw.ADMIN, aw.MEMBER}
    assert {(r["user_id"], r["reason"]) for r in skipped} == {
        (aw.ADMIN, "daily_cap"),
        (aw.MEMBER, "daily_cap"),
    }
    assert {r["ref_id"] for r in rows} == {str(first), str(second)}
    # Saturday is a new day: the pantry summary goes out.
    assert _pass(world, SATURDAY_NOON) == 1


@db
def test_quiet_hours(world: str) -> None:
    """21:00-08:00: nothing is sent and nothing recorded, so it is not lost."""
    _delivery(world, FRIDAY_NOON.replace(hour=22))
    assert _pass(world, FRIDAY_NOON.replace(hour=21, minute=30)) == 0
    assert _rows(world) == []
    assert _sent(world) == []


@db
def test_stop_these(world: str) -> None:
    """A person who tapped "Stop these" (`reminders_off_at`) is skipped, recorded."""
    with psycopg.connect(world) as conn:
        conn.execute(
            "UPDATE channel_identities SET reminders_off_at = now() WHERE user_id = %s",
            (aw.MEMBER,),
        )
    _delivery(world, FRIDAY_NOON.replace(hour=18))
    assert _pass(world, FRIDAY_NOON) == 1
    rows = {r["user_id"]: r for r in _rows(world)}
    assert (rows[aw.MEMBER]["status"], rows[aw.MEMBER]["reason"]) == ("skipped", "opted_out")
    assert rows[aw.ADMIN]["status"] == "sent"
    assert [m["body"]["user_id"] for m in _sent(world)] == [str(aw.ADMIN)]
    # Every reminder carries the button.
    assert _sent(world)[0]["body"]["buttons"][-1]["payload"] == ids.REMINDER_STOP


@db
def test_no_children(world: str) -> None:
    with psycopg.connect(world) as conn:
        child = iw.add_person(conn, EVAL, role="child")
    _delivery(world, FRIDAY_NOON.replace(hour=18))
    _pass(world, FRIDAY_NOON)
    rows = {r["user_id"]: r for r in _rows(world)}
    assert (rows[child.user_id]["status"], rows[child.user_id]["reason"]) == ("skipped", "child")
    assert str(child.user_id) not in {m["body"]["user_id"] for m in _sent(world)}


@db
def test_never_twice(world: str) -> None:
    _delivery(world, FRIDAY_NOON.replace(hour=18))
    assert _pass(world, FRIDAY_NOON) == 2
    assert _pass(world, FRIDAY_NOON + timedelta(minutes=1)) == 0
    assert _pass(world, FRIDAY_NOON + timedelta(hours=3)) == 0
    assert len(_sent(world)) == 2
    assert len(_rows(world)) == 2


def test_never_held() -> None:
    """Reminder keys are never holdable; outside the window with no template the sender
    records them skipped, reason `no_template` (§10)."""
    for key in reminders.TEMPLATES.values():
        assert key in templates.REMINDER_KEYS
        assert templates.holdable(key, "reminder") is False
    assert sender.NO_TEMPLATE == "no_template"


@db
def test_member_left_household_not_sent(world: str) -> None:
    """A due row for someone no longer in the household is neither sent nor recorded."""
    due = reminders.Due("delivery_today", str(uuid.uuid4()), uuid.uuid4(), EVAL)
    out = aw.run(world, lambda conn: reminders.apply_skips(conn, [due], FRIDAY_NOON))
    assert out == []
    assert _rows(world) == []


@db
def test_send_without_facts_sends_nothing(world: str) -> None:
    """A delivery whose order is gone is not sent (fails closed)."""
    due = reminders.Due("delivery_today", str(uuid.uuid4()), aw.ADMIN, EVAL)
    assert aw.run(world, lambda conn: reminders.send(conn, due, FRIDAY_NOON)) is None
    assert _rows(world) == [] and _sent(world) == []
