"""The agent directory over `agent_cards` (architecture item 21, amended by #96).

`find` is internal discovery (architecture §4, agent-protocol §2): it returns enabled cards
only, refuses `cls='channel'` and never returns `external` cards. `card` looks up one
card by id whether or not it is enabled.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

__all__ = ["AgentCard", "UnknownAgent", "card", "find"]

_COLUMNS = (
    "agent_id, class AS agent_class, owner_ref, region, zone_ids, currency, host_process,"
    " vendor, config, enabled, created_at"
)


class UnknownAgent(LookupError):
    """No `agent_cards` row has this `agent_id`."""


@dataclass(frozen=True)
class AgentCard:
    """One `agent_cards` row (architecture item 9 and agent-protocol P7).

    `agent_class` holds the `class` column. This is separate from
    `protocol.a2a.AgentCardRow`, which holds only what `to_a2a_card` renders.
    """

    agent_id: str
    agent_class: str
    owner_ref: str
    region: str
    zone_ids: tuple[str, ...]
    currency: str | None
    host_process: str
    vendor: str | None
    config: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    created_at: datetime | None = None


def _to_card(row: dict[str, Any]) -> AgentCard:
    return AgentCard(**{**row, "zone_ids": tuple(row["zone_ids"])})


async def find(
    conn: AsyncConnection, cls: str, region: str, zone_id: str, currency: str
) -> list[AgentCard]:
    """Enabled cards of class `cls` in `region` that serve `zone_id` in `currency`.

    Raises `ValueError` for `cls='channel'`. `external` cards are never returned.
    """
    if cls == "channel":
        raise ValueError("directory.find refuses cls='channel'")
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM agent_cards"
            " WHERE class = %s AND class <> 'external' AND enabled"
            " AND region = %s AND %s = ANY(zone_ids) AND currency = %s"
            " ORDER BY agent_id",
            (cls, region, zone_id, currency),
        )
        rows = await cur.fetchall()
    return [_to_card(row) for row in rows]


async def card(conn: AsyncConnection, agent_id: str) -> AgentCard:
    """The card for `agent_id`, enabled or not. Raises `UnknownAgent` when there is none."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(f"SELECT {_COLUMNS} FROM agent_cards WHERE agent_id = %s", (agent_id,))
        row = await cur.fetchone()
    if row is None:
        raise UnknownAgent(agent_id)
    return _to_card(row)
