from uuid import UUID, uuid4, uuid5

import pytest

from services.common import ids

ORDER = UUID("11111111-1111-4111-8111-111111111111")


def test_idempotency_key_is_deterministic() -> None:
    a = ids.idempotency_key(ORDER, "place_order", "grocery:square-us")
    b = ids.idempotency_key(ORDER, "place_order", "grocery:square-us")
    assert a == b == f"{ORDER}:place_order:grocery:square-us"


@pytest.mark.parametrize(
    ("order_id", "action", "agent"),
    [
        (None, "place_order", "grocery:square-us"),
        ("", "place_order", "grocery:square-us"),
        (ORDER, "", "grocery:square-us"),
        (ORDER, None, "grocery:square-us"),
        (ORDER, "place_order", ""),
        (ORDER, "place_order", None),
    ],
)
def test_idempotency_key_rejects_missing_parts(order_id, action, agent) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        ids.idempotency_key(order_id, action, agent)


def test_seed_ids_are_deterministic_and_distinct() -> None:
    assert ids.seed_user_id("us-1-a") == ids.seed_user_id("us-1-a")
    assert ids.seed_user_id("us-1-a") != ids.seed_user_id("us-1-a-eval")
    assert ids.seed_address_ref("us-1") != ids.seed_address_ref("us-2")
    with pytest.raises(ValueError):
        ids.seed_user_id("")


def test_idempotency_key_scope_appended() -> None:
    quote = uuid4()
    base = ids.idempotency_key(ORDER, "authorize_spend", "household:h1")
    scoped = ids.idempotency_key(ORDER, "authorize_spend", "household:h1", scope=quote)
    assert scoped == f"{base}:{quote}"
    assert ids.idempotency_key(ORDER, "authorize_spend", "household:h1", scope=None) == base
    with pytest.raises(ValueError):
        ids.idempotency_key(ORDER, "authorize_spend", "household:h1", scope="")  # type: ignore[arg-type]


def test_vendor_key_fits_square_limit() -> None:
    key = ids.idempotency_key(ORDER, "place_order", "grocery:square-us", scope=uuid4())
    vk = ids.vendor_key(key)
    assert len(vk) == 36 <= 45
    assert vk == ids.vendor_key(key)
    assert UUID(vk) == uuid5(ids.KEY_NAMESPACE, key)


def test_message_id_same_cause_same_ordinal_same_id() -> None:
    cause = uuid4()
    assert ids.message_id(cause, 0) == ids.message_id(cause, 0) == uuid5(cause, "0")
    assert ids.message_id(cause, 0) != ids.message_id(cause, 1)
    assert ids.message_id(cause, 0) != ids.message_id(uuid4(), 0)


def test_thread_id_includes_run() -> None:
    run = uuid4()
    assert ids.thread_id("household:h1", run) == f"household:h1:{run}"
    assert ids.thread_id("household:h1", run) != ids.thread_id("household:h1", uuid4())


def test_parse_agent_id_matches_checks_format() -> None:
    for agent_id in ("grocery:square-us", "delivery:doordash:us", "household:h1"):
        agent_class, name = ids.parse_agent_id(agent_id)
        assert agent_class == agent_id.split(":", 1)[0]
        assert f"{agent_class}:{name}" == agent_id
    with pytest.raises(ValueError):
        ids.parse_agent_id("grocery")


def test_quote_id_from_request() -> None:
    request = uuid4()
    assert ids.quote_id(request) == ids.quote_id(request) == uuid5(request, "quote")
    assert ids.quote_id(request) != ids.quote_id(uuid4())


def test_shortfall_id_one_per_order() -> None:
    assert ids.shortfall_id(ORDER) == ids.shortfall_id(ORDER) == uuid5(ORDER, "shortfall")
    assert ids.shortfall_id(ORDER) != ids.shortfall_id(uuid4())
    sf = ids.shortfall_id(ORDER)
    assert ids.shortfall_conversation_id(sf) == uuid5(sf, "conversation")


def test_a2a_message_id_namespaced_by_sender() -> None:
    a = ids.a2a_message_id("external:a2a-demo", "m-1")
    assert a == ids.a2a_message_id("external:a2a-demo", "m-1")
    assert a == uuid5(ids.NS_A2A, "external:a2a-demo|m-1")
    assert a != ids.a2a_message_id("external:other", "m-1")
    assert ids.a2a_run_id(a) == uuid5(a, "run")
    assert ids.a2a_order_id(a) == uuid5(a, "order")
    assert ids.a2a_run_id(a) != ids.a2a_order_id(a)


def test_other_protocol_ids() -> None:
    m = uuid4()
    assert ids.conversation_id(m) == uuid5(m, "conversation")
    assert ids.protocol_error_id(m) == uuid5(m, "protocol_error")
