"""Every shared id and idempotency key (architecture item 4, agent-protocol P6).

All ids are deterministic: they are derived from values passed in and never from random or
time-based parts, so a replayed step produces the same ids. The one exception is
`new_run_id`, which is only called once per trigger (see its docstring).
"""

from uuid import UUID, uuid4, uuid5

# Fixed namespaces. Changing either value changes every derived id; never edit them.
KEY_NAMESPACE = UUID("7ee0682a-39c6-4ff0-90ae-b071ffe25312")
# Namespace for inbound A2A message ids (agent-protocol §3, §11). protocol/a2a.py (P5)
# re-exports it; it lives here so ids.py has no dependency on the protocol package.
NS_A2A = UUID("6d80f7e3-7e4c-4ad0-ab4b-b5866e276302")


def _require(name: str, value: object) -> str:
    text = "" if value is None else str(value)
    if not text:
        raise ValueError(f"{name} is required")
    return text


def parse_agent_id(agent_id: str) -> tuple[str, str]:
    """Split `<class>:<name>` on the first `:`, as evals/checks.py does."""
    agent_class, sep, name = _require("agent_id", agent_id).partition(":")
    if not sep or not agent_class or not name:
        raise ValueError(f"agent_id must be '<class>:<name>', got {agent_id!r}")
    return agent_class, name


def thread_id(agent_id: str, run_id: UUID) -> str:
    """Checkpointer thread id: `<agent_id>:<run_id>`."""
    return f"{_require('agent_id', agent_id)}:{_require('run_id', run_id)}"


def message_id(cause_id: UUID, ordinal: int) -> UUID:
    """The `ordinal`-th message sent in the step caused by `cause_id`."""
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
        raise ValueError(f"ordinal must be a non-negative int, got {ordinal!r}")
    return uuid5(cause_id, str(ordinal))


def idempotency_key(
    order_id: UUID, action: str, acting_agent_id: str, *, scope: UUID | None = None
) -> str:
    """`<order_id>:<action>:<acting_agent_id>`, plus `:<scope>` when `scope` is given (A16)."""
    parts = [
        _require("order_id", order_id),
        _require("action", action),
        _require("acting_agent_id", acting_agent_id),
    ]
    if scope is not None:
        parts.append(_require("scope", scope))
    return ":".join(parts)


def vendor_key(key: str) -> str:
    """The idempotency key sent to vendors: 36 characters, within Square's 45 limit."""
    return str(uuid5(KEY_NAMESPACE, _require("key", key)))


# Agent-protocol P6 helpers (Technical Plan §3, §7, §11).


def quote_id(request_message_id: UUID) -> UUID:
    """Id of the quote answering a `quote_request` (or `delivery_quote_request`)."""
    return uuid5(request_message_id, "quote")


def approval_id(order_id: UUID) -> UUID:
    """An order has at most one approval (its `request_approval` key has no scope)."""
    return uuid5(order_id, "approval")


def conversation_id(opening_message_id: UUID) -> UUID:
    """Conversation opened by `opening_message_id` (a negotiation or store-to-courier)."""
    return uuid5(opening_message_id, "conversation")


def shortfall_id(order_id: UUID) -> UUID:
    """An order has at most one shortfall."""
    return uuid5(order_id, "shortfall")


def shortfall_conversation_id(shortfall_id: UUID) -> UUID:
    """Conversation carried by every message of a shortfall."""
    return uuid5(shortfall_id, "conversation")


def protocol_error_id(refused_message_id: UUID) -> UUID:
    """Id of the `protocol_error` sent back for a refused message."""
    return uuid5(refused_message_id, "protocol_error")


def retry_prompt_id(run_id: UUID) -> UUID:
    """`message_id` of a run's retry prompt (architecture item 4, amended for #36)."""
    return uuid5(run_id, "retry_prompt")


NS_TIMER = UUID("b4c1e0d2-5f3a-4e8b-9c7d-1a2b3c4d5e6f")


def timer_event_id(timer_id: int) -> UUID:
    """The source event id of a fired timer; the sweeper's messages use
    `message_id(timer_event_id(timer_id), ordinal)` (architecture §3)."""
    if not isinstance(timer_id, int) or isinstance(timer_id, bool):
        raise ValueError(f"timer_id must be an int, got {timer_id!r}")
    return uuid5(NS_TIMER, f"timer|{timer_id}")


NS_INBOUND = UUID("1663a266-7392-4541-9021-c5940595acec")


def inbound_id(channel: str, external_id: str) -> UUID:
    """The source event id of an `inbound_messages` row (architecture items 10, 33).

    It is `runs.trigger.inbound_id` and `user_message.inbound_id`; the messages `ingest`
    sends use `message_id(inbound_id(...), ordinal)` (architecture §3).
    """
    return uuid5(
        NS_INBOUND, f"{_require('channel', channel)}|{_require('external_id', external_id)}"
    )


def new_run_id() -> UUID:
    """A fresh `run_id` for `runs.start()` when the caller has none to derive it from.

    The one random id in this module. A run is started once, inside the transaction that
    inserts its trigger (an `inbound_messages` row or a `POST /runs` call), so a replay never
    reaches it. A2A demo runs derive theirs with `a2a_run_id`.
    """
    return uuid4()


def a2a_message_id(sender: str, a2a_message_id: str) -> UUID:
    """Our `message_id` for an inbound A2A `messageId`, namespaced by the sender."""
    return uuid5(
        NS_A2A, f"{_require('sender', sender)}|{_require('a2a_message_id', a2a_message_id)}"
    )


def a2a_run_id(message_id: UUID) -> UUID:
    """Run started by an inbound A2A message."""
    return uuid5(message_id, "run")


def a2a_order_id(message_id: UUID) -> UUID:
    """Draft order created for an inbound A2A message."""
    return uuid5(message_id, "order")


# Seeded rows (architecture item 55). Seeding twice must give the same rows, so the UUID
# keys of seeded users and addresses are derived from their seed names.
NS_SEED = UUID("3a1f7c52-9a0e-4d1b-8f6e-2c4b5d7e9a10")


def seed_user_id(member_id: str) -> UUID:
    """`users.user_id` of a seeded member, for example `us-1-a`."""
    return uuid5(NS_SEED, f"user|{_require('member_id', member_id)}")


def seed_address_ref(household_id: str) -> UUID:
    """`address_ref` of a seeded household's one address."""
    return uuid5(NS_SEED, f"address|{_require('household_id', household_id)}")
