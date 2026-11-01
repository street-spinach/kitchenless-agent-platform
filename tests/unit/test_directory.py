"""The agent directory (architecture item 21, amended by #96; issue #97)."""

import asyncio
from collections.abc import Awaitable, Callable

import psycopg
import pytest

from services.common import directory
from services.common.directory import AgentCard, UnknownAgent

pytestmark = pytest.mark.db


def _card(
    conn: psycopg.Connection,
    agent_id: str,
    cls: str,
    *,
    region: str = "us",
    zones: list[str] | None = None,
    currency: str | None = "USD",
    host: str = "agents",
    enabled: bool = True,
) -> None:
    conn.execute(
        "INSERT INTO agent_cards"
        " (agent_id, class, owner_ref, region, zone_ids, currency, host_process, enabled)"
        " VALUES (%s, %s, 'org:o1', %s, %s, %s, %s, %s)",
        (agent_id, cls, region, zones if zones is not None else ["z1"], currency, host, enabled),
    )


def _seed(pg: str, *cards: tuple[str, str, dict]) -> None:
    with psycopg.connect(pg) as conn:
        for agent_id, cls, kw in cards:
            _card(conn, agent_id, cls, **kw)


def _run[T](pg: str, fn: Callable[[psycopg.AsyncConnection], Awaitable[T]]) -> T:
    async def go() -> T:
        async with await psycopg.AsyncConnection.connect(pg) as conn:
            return await fn(conn)

    return asyncio.run(go())


def _ids(cards: list[AgentCard]) -> list[str]:
    return [c.agent_id for c in cards]


def test_find_filters_zone_region_currency(pg: str) -> None:
    _seed(
        pg,
        ("grocery:a", "grocery", {"zones": ["z1", "z2"]}),
        ("grocery:other-zone", "grocery", {"zones": ["z9"]}),
        ("grocery:other-region", "grocery", {"region": "lk"}),
        ("grocery:other-currency", "grocery", {"currency": "LKR"}),
        ("grocery:no-currency", "grocery", {"currency": None}),
        ("restaurant:a", "restaurant", {}),
    )

    found = _run(pg, lambda c: directory.find(c, "grocery", "us", "z1", "USD"))

    assert _ids(found) == ["grocery:a"]
    assert found[0] == AgentCard(
        agent_id="grocery:a",
        agent_class="grocery",
        owner_ref="org:o1",
        region="us",
        zone_ids=("z1", "z2"),
        currency="USD",
        host_process="agents",
        vendor=None,
        config={},
        enabled=True,
        created_at=found[0].created_at,
    )
    assert found[0].created_at is not None


def test_disabled_cards_excluded(pg: str) -> None:
    _seed(
        pg,
        ("grocery:on", "grocery", {}),
        ("grocery:off", "grocery", {"enabled": False}),
    )

    assert _ids(_run(pg, lambda c: directory.find(c, "grocery", "us", "z1", "USD"))) == [
        "grocery:on"
    ]
    # card() still returns a disabled card.
    assert _run(pg, lambda c: directory.card(c, "grocery:off")).enabled is False


def test_channel_cards_never_returned(pg: str) -> None:
    _seed(pg, ("channel:whatsapp", "channel", {"host": "api"}))

    with pytest.raises(ValueError):
        _run(pg, lambda c: directory.find(c, "channel", "us", "z1", "USD"))


def test_external_cards_never_returned(pg: str) -> None:
    _seed(pg, ("external:a2a-demo", "external", {"host": "external"}))

    assert _run(pg, lambda c: directory.find(c, "external", "us", "z1", "USD")) == []


def test_card_unknown_agent_raises(pg: str) -> None:
    with pytest.raises(UnknownAgent) as err:
        _run(pg, lambda c: directory.card(c, "grocery:missing"))
    assert isinstance(err.value, LookupError)
