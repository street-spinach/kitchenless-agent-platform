"""The sender check run on every inbound message (agent-protocol §6, P4).

`check_static` runs C1-C3 and needs no database; the consumer and the A2A endpoint both use
it. `check` adds C4 and C5, reading only the rows the body names (`orders`, `quotes`,
`shortfalls`, `agent_messages` for `ref_message_id`), by primary key, plus `runs` for the
message's own run and, for people types, the consumer's card and one `household_members` row.
The first failing predicate raises `ProtocolError(code, detail)`.
"""

from typing import Any, get_args
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import BaseModel, ValidationError

from services.common import ids
from services.common.protocol import catalog as cat
from services.common.protocol.a2a import AgentMessageRow
from services.common.protocol.catalog import CATALOG, PartyRule
from services.common.protocol.schemas import ErrorCode

CODES = frozenset(
    {
        "unknown_type",
        "unsupported_version",
        "invalid_body",
        "wrong_sender_class",
        "unknown_reference",
        "wrong_party",
    }
)
if CODES != frozenset(get_args(ErrorCode)):
    raise RuntimeError("CODES must equal the protocol_error code list in schemas.py")

DETAIL_MAX = 200
EXTERNAL_DEMO = "external:a2a-demo"

# Body fields that name a stored row (C4), with the table and primary key they refer to.
_REFERENCES = (
    ("order_id", "orders", "order_id"),
    ("quote_id", "quotes", "quote_id"),
    ("shortfall_id", "shortfalls", "shortfall_id"),
    ("ref_message_id", "agent_messages", "message_id"),
)
# A shortfall_notice opens the shortfall: the household inserts the row from it (§10), so its
# shortfall_id is checked by the party rule (uuid5(order_id, "shortfall")), not by C4.
_OPENS_SHORTFALL = frozenset({"shortfall_notice"})


class ProtocolError(Exception):
    """An inbound message refused by the sender check. Never retried (§6)."""

    def __init__(self, code: str, detail: str) -> None:
        if code not in CODES:
            raise ValueError(f"unknown protocol error code {code!r}")
        self.code = code
        self.detail = detail[:DETAIL_MAX]
        super().__init__(f"{code}: {self.detail}")


class OutboundInvalid(Exception):
    """An agent's own outgoing body failed validation in `outbox.send()` (§6).

    The bug is in the sender's code, so it follows the normal retry and poison rules.
    """

    def __init__(self, type: str, errors: list[Any]) -> None:
        self.type = type
        self.errors = errors
        super().__init__(f"outbound {type} body is invalid: {len(errors)} error(s)")


def _agent_class(agent_id: str) -> str | None:
    try:
        return ids.parse_agent_id(agent_id)[0]
    except ValueError:
        return None


def _allowed(agent_id: str, allowed: frozenset[str]) -> bool:
    """True if `agent_id` is listed exactly (`system:api`) or by its class (`household`)."""
    agent_class = _agent_class(agent_id)
    return agent_class is not None and (agent_id in allowed or agent_class in allowed)


def check_static(type: str, from_agent: str, to_agent: str, body: Any) -> BaseModel:
    """C1-C3: the type is known, the body validates, the sender and receiver classes fit."""
    row = CATALOG.get(type)
    if row is None:  # C1
        raise ProtocolError("unknown_type", "message type is not in the catalog")
    if isinstance(body, dict) and body.get("schema_version", 1) != 1:  # C2
        raise ProtocolError("unsupported_version", f"{type}: only schema_version 1 is known")
    try:
        parsed = row.schema.model_validate(body)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in err["loc"]) for err in exc.errors()})
        raise ProtocolError("invalid_body", f"{type}: invalid {', '.join(fields)}") from None
    if not _allowed(from_agent, row.senders) or not _allowed(to_agent, row.receivers):  # C3
        raise ProtocolError("wrong_sender_class", f"{type} is not allowed between these classes")
    return parsed


async def _one(conn: AsyncConnection, query: str, params: tuple[Any, ...]) -> dict | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchone()


class _Rows:
    """The rows C4 read, plus the lazily read run, used to resolve party rule terms."""

    def __init__(self, conn: AsyncConnection, msg: AgentMessageRow) -> None:
        self.conn = conn
        self.msg = msg
        self.rows: dict[str, dict] = {}
        self._run: dict | None = None

    async def run(self) -> dict:
        if self._run is None:
            self._run = await _one(
                self.conn,
                "SELECT run_id, household_id, kind, trigger FROM runs WHERE run_id = %s",
                (self.msg.run_id,),
            )
            if self._run is None:
                raise ProtocolError("unknown_reference", "the message's run does not exist")
        return self._run

    async def consumer_owner(self, agent_id: str) -> str | None:
        if _agent_class(agent_id) != "consumer":
            return None
        card = await _one(
            self.conn,
            "SELECT owner_ref FROM agent_cards WHERE agent_id = %s AND class = 'consumer'",
            (agent_id,),
        )
        return None if card is None else card["owner_ref"]


async def _check_references(rows: _Rows, type: str, body: BaseModel) -> None:
    """C4: every order, quote, shortfall or message the body names exists in this run."""
    for field, table, key in _REFERENCES:
        value = getattr(body, field, None)
        if value is None or (field == "shortfall_id" and type in _OPENS_SHORTFALL):
            continue
        row = await _one(rows.conn, f"SELECT * FROM {table} WHERE {key} = %s", (value,))
        if row is None:
            raise ProtocolError("unknown_reference", f"{field} {str(value)[:8]} does not exist")
        if row["run_id"] != rows.msg.run_id:
            raise ProtocolError("unknown_reference", f"{field} {str(value)[:8]} is in another run")
        rows.rows[table] = row


def _household(household_id: object) -> str | None:
    return None if household_id is None else f"household:{household_id}"


async def _term_holds(rows: _Rows, term: str, agent: str, side: str) -> bool:
    order = rows.rows.get("orders") or {}
    quote = rows.rows.get("quotes") or {}
    msg = rows.msg
    match term:
        case cat.HH_ORDER:
            return agent == _household(order.get("household_id"))
        case cat.HH_RUN:
            return agent == _household((await rows.run())["household_id"])
        case cat.MEMBER_OF_RUN:
            household_id = (await rows.run())["household_id"]
            owner = await rows.consumer_owner(agent)
            if household_id is None or owner is None or not owner.startswith("user:"):
                return False
            try:
                user_id = UUID(owner.removeprefix("user:"))
            except ValueError:
                return False
            member = await _one(
                rows.conn,
                "SELECT 1 FROM household_members WHERE household_id = %s AND user_id = %s",
                (household_id, user_id),
            )
            return member is not None
        case cat.CONSUMER_OF_TRIGGER:
            user_id = ((await rows.run())["trigger"] or {}).get("user_id")
            owner = await rows.consumer_owner(agent)
            return user_id is not None and owner == f"user:{user_id}"
        case cat.EXTERNAL_IF_A2A_DEMO:
            return agent == EXTERNAL_DEMO and (await rows.run())["kind"] == "a2a_demo"
        case cat.ORDER_SELLER:
            return agent == order.get("seller_agent_id")
        case cat.ORDER_COURIER:
            return agent == order.get("delivery_agent_id")
        case cat.QUOTE_SELLER:
            return agent == quote.get("seller_agent_id")
        case cat.QUOTE_BUYER:
            return agent == quote.get("buyer_agent_id")
        case cat.QUOTE_OTHER_PARTY:
            buyer, seller = quote.get("buyer_agent_id"), quote.get("seller_agent_id")
            other = {buyer: seller, seller: buyer}.get(msg.from_agent)
            return other is not None and agent == other
        case cat.SHORTFALL_SELLER:
            return agent == (rows.rows.get("shortfalls") or {}).get("seller_agent_id")
        case cat.REF_TO_AGENT:
            return agent == (rows.rows.get("agent_messages") or {}).get("to_agent")
        case cat.REF_FROM_AGENT:
            return agent == (rows.rows.get("agent_messages") or {}).get("from_agent")
        case cat.ANY_SYSTEM:
            return _agent_class(agent) == "system"
    raise ValueError(f"unknown party rule term {term!r} on the {side} side")


async def _check_party(rows: _Rows, type: str, rule: PartyRule, body: BaseModel) -> None:
    """C5: the sender and receiver are the parties the type's rule names (§5)."""
    msg = rows.msg
    for side, agent, terms in (
        ("sender", msg.from_agent, rule.sender),
        ("receiver", msg.to_agent, rule.receiver),
    ):
        if terms is None:
            continue
        for term in terms:  # any term may match; stop reading at the first that does
            if await _term_holds(rows, term, agent, side):
                break
        else:
            raise ProtocolError("wrong_party", f"{type}: the {side} is not a party to it")
    if type in _OPENS_SHORTFALL and body.shortfall_id != ids.shortfall_id(body.order_id):
        raise ProtocolError("wrong_party", f"{type}: shortfall_id is not the order's shortfall")


async def check(conn: AsyncConnection, msg: AgentMessageRow) -> BaseModel:
    """C1-C5 for a claimed `agent_messages` row. Returns the parsed body."""
    body = check_static(msg.type, msg.from_agent, msg.to_agent, msg.body)
    rows = _Rows(conn, msg)
    await _check_references(rows, msg.type, body)
    await _check_party(rows, msg.type, CATALOG[msg.type].party, body)
    return body
