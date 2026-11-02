import base64
import hashlib
from uuid import UUID, uuid4, uuid5

import pytest

from services.common import ids

ORDER = UUID("11111111-1111-4111-8111-111111111111")


def test_approval_message_ids_are_deterministic_and_distinct() -> None:
    approval = uuid4()
    assert ids.approval_prompt_id(approval) == uuid5(approval, "approval_prompt")
    assert ids.approval_decision_id(approval) == uuid5(approval, "approval_decision")
    assert ids.approval_prompt_id(approval) != ids.approval_decision_id(approval)


def test_approval_button_round_trips_and_rejects_anything_else() -> None:
    approval = uuid4()
    for decision in ("approved", "rejected"):
        payload = ids.approval_button(approval, decision)
        assert payload == f"ap:{approval}:{decision}"
        assert ids.parse_approval_button(payload) == (approval, decision)
    with pytest.raises(ValueError):
        ids.approval_button(approval, "expired")
    others = ["", "approve:1", f"ap:{approval}:expired", "ap:x:approved", f"sf:{approval}:1:drop"]
    for payload in others:
        assert ids.parse_approval_button(payload) is None


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


def test_approval_id_one_per_order() -> None:
    assert ids.approval_id(ORDER) == ids.approval_id(ORDER) == uuid5(ORDER, "approval")
    assert ids.approval_id(ORDER) != ids.approval_id(uuid4())


def test_vendor_key_fits_square_limit() -> None:
    key = ids.idempotency_key(ORDER, "place_order", "grocery:square-us", scope=uuid4())
    vk = ids.vendor_key(key)
    assert len(vk) == 36 <= 45
    assert vk == ids.vendor_key(key)
    assert UUID(vk) == uuid5(ids.KEY_NAMESPACE, key)


def test_mock_batch_id_derives_from_external_delivery_id() -> None:
    edi = ids.vendor_key(ids.idempotency_key(ORDER, "book_delivery", "delivery:mock-us"))
    assert ids.mock_batch_id(edi) == ids.mock_batch_id(edi) == uuid5(UUID(edi), "batch")
    assert ids.mock_batch_id(edi) != ids.mock_batch_id(str(uuid4()))
    with pytest.raises(ValueError):
        ids.mock_batch_id("")


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
    assert ids.shortfall_closed_id(sf) == uuid5(sf, "shortfall_closed")
    assert ids.shortfall_closed_id(sf) != ids.shortfall_conversation_id(sf)


def test_order_status_id_one_per_order_state() -> None:
    sent = ids.order_status_id(ORDER, "in_delivery")
    assert sent == ids.order_status_id(ORDER, "in_delivery")
    assert sent == uuid5(ORDER, "order_status|in_delivery")
    assert sent != ids.order_status_id(ORDER, "delivered")
    assert sent != ids.order_status_id(uuid4(), "in_delivery")
    with pytest.raises(ValueError):
        ids.order_status_id(ORDER, "")


def test_a2a_message_id_namespaced_by_sender() -> None:
    a = ids.a2a_message_id("external:a2a-demo", "m-1")
    assert a == ids.a2a_message_id("external:a2a-demo", "m-1")
    assert a == uuid5(ids.NS_A2A, "external:a2a-demo|m-1")
    assert a != ids.a2a_message_id("external:other", "m-1")
    assert ids.a2a_run_id(a) == uuid5(a, "run")
    assert ids.a2a_order_id(a) == uuid5(a, "order")
    assert ids.a2a_run_id(a) != ids.a2a_order_id(a)


def test_a2a_cancel_id_one_per_quote() -> None:
    q = uuid4()
    assert ids.a2a_cancel_id(q) == ids.a2a_cancel_id(q) == uuid5(q, "a2a_cancel")
    assert ids.a2a_cancel_id(q) != ids.a2a_cancel_id(uuid4())
    with pytest.raises(ValueError):
        ids.a2a_cancel_id(None)  # type: ignore[arg-type]


def test_other_protocol_ids() -> None:
    m = uuid4()
    assert ids.conversation_id(m) == uuid5(m, "conversation")
    assert ids.protocol_error_id(m) == uuid5(m, "protocol_error")


def test_retry_prompt_id_is_deterministic_per_run() -> None:
    run = uuid4()
    assert ids.retry_prompt_id(run) == ids.retry_prompt_id(run) == uuid5(run, "retry_prompt")
    assert ids.retry_prompt_id(run) != ids.retry_prompt_id(uuid4())


def test_timer_event_id_is_deterministic_per_timer() -> None:
    assert ids.timer_event_id(7) == ids.timer_event_id(7) == uuid5(ids.NS_TIMER, "timer|7")
    assert ids.timer_event_id(7) != ids.timer_event_id(8)
    with pytest.raises(ValueError):
        ids.timer_event_id("7")  # type: ignore[arg-type]


def test_mock_event_id_is_deterministic_per_event() -> None:
    expected = uuid5(ids.NS_MOCK_EVENT, "mock_event|7")
    assert ids.mock_event_id(7) == ids.mock_event_id(7) == expected
    assert ids.mock_event_id(7) != ids.mock_event_id(8)
    assert ids.mock_event_id(7) != ids.timer_event_id(7)
    with pytest.raises(ValueError):
        ids.mock_event_id("7")  # type: ignore[arg-type]


def test_inbound_id_is_deterministic_per_channel_and_external_id() -> None:
    expected = uuid5(ids.NS_INBOUND, "whatsapp|wamid.1")
    assert ids.inbound_id("whatsapp", "wamid.1") == expected
    assert ids.inbound_id("runs", "wamid.1") != expected
    with pytest.raises(ValueError):
        ids.inbound_id("whatsapp", "")


def test_new_run_id_is_fresh() -> None:
    assert ids.new_run_id() != ids.new_run_id()


def test_new_eval_run_id_is_fresh() -> None:
    # Evals §6: random (uuid4), so two calls differ.
    first, second = ids.new_eval_run_id(), ids.new_eval_run_id()
    assert first != second
    assert first.version == 4


def test_keycloak_client_id_replaces_the_class_separator() -> None:
    assert ids.keycloak_client_id("household:us-1") == "household.us-1"
    assert ids.keycloak_client_id("consumer:us-1-a") == "consumer.us-1-a"
    assert ids.keycloak_client_id("grocery:square-us") == "grocery.square-us"
    for bad in ("", "household", "household:", "consumer:hh-1:2"):
        with pytest.raises(ValueError):
            ids.keycloak_client_id(bad)


def test_take_turns_button_round_trips_and_rejects_anything_else() -> None:
    # Agents §10: `tt:<order_id>:on|off`; anything else is not a take-turns tap.
    order = uuid4()
    for on, state in ((True, "on"), (False, "off")):
        payload = ids.take_turns_button(order, on)
        assert payload == f"tt:{order}:{state}"
        assert ids.parse_take_turns_button(payload) == (order, on)
    others = ["", "tt:x:on", f"tt:{order}:no", f"tt:{order}", f"ap:{order}:approved"]
    for payload in others:
        assert ids.parse_take_turns_button(payload) is None


# Experience EX-I5.


def test_setup_button_round_trips() -> None:
    proposal = uuid4()
    for answer in ("yes", "no"):
        payload = ids.setup_button(proposal, answer)
        assert payload == f"ob:{proposal}:{answer}"
        assert ids.parse_setup_button(payload) == (proposal, answer, None)
    for answer in ("remove", "keep"):
        for n in (0, 3, 12):
            payload = ids.setup_button(proposal, answer, n)
            assert payload == f"ob:{proposal}:{answer}:{n}"
            assert ids.parse_setup_button(payload) == (proposal, answer, n)


def test_setup_button_n_required_for_remove_keep_and_forbidden_otherwise() -> None:
    proposal = uuid4()
    for answer in ("remove", "keep"):
        for bad_n in (None, -1, True, "1"):
            with pytest.raises(ValueError):
                ids.setup_button(proposal, answer, bad_n)
    for answer in ("yes", "no"):
        with pytest.raises(ValueError):
            ids.setup_button(proposal, answer, 0)
    with pytest.raises(ValueError):
        ids.setup_button(proposal, "maybe")


def test_parse_setup_button_rejects_malformed_payloads() -> None:
    proposal = uuid4()
    for payload in (
        "",
        None,
        42,
        "ob",
        f"ob:{proposal}",
        f"ob:{proposal}:maybe",
        f"ob:{proposal}:yes:0",
        f"ob:{proposal}:no:1",
        f"ob:{proposal}:remove",
        f"ob:{proposal}:keep:",
        f"ob:{proposal}:keep:-1",
        f"ob:{proposal}:keep:01",
        f"ob:{proposal}:keep:x",
        f"ob:{proposal}:keep:1:2",
        f"ob:{proposal.hex}:yes",
        f"ob:{str(proposal).upper()}:yes",
        "ob:not-a-uuid:yes",
        f"ap:{proposal}:yes",
        f"OB:{proposal}:yes",
    ):
        assert ids.parse_setup_button(payload) is None, payload


def test_reminder_stop_payload() -> None:
    assert ids.REMINDER_STOP == "rs:stop"


def test_page_button_round_trips_and_rejects_anything_else() -> None:
    for page in ("household", "pantry", "renew"):
        payload = ids.page_button(page)
        assert payload == f"me:{page}"
        assert ids.parse_page_button(payload) == page
    with pytest.raises(ValueError):
        ids.page_button("admin")
    for payload in ("", None, "me", "me:", "me:admin", "me:pantry:1", "rs:stop", "ME:pantry"):
        assert ids.parse_page_button(payload) is None, payload


def test_proposal_id_is_derived_from_run_id() -> None:
    run = uuid4()
    assert ids.proposal_id(run) == uuid5(run, "proposal") == ids.proposal_id(run)
    assert ids.proposal_id(run) != ids.proposal_id(uuid4())
    assert ids.proposal_id(run) != ids.order_id(run)


def test_new_join_code_is_8_base32_characters_and_fresh() -> None:
    codes = {ids.new_join_code() for _ in range(50)}
    assert len(codes) == 50
    for code in codes:
        assert len(code) == 8
        assert set(code) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


def test_join_code_listed_as_random_in_module_docstring() -> None:
    doc = ids.__doc__ or ""
    for name in ("new_run_id", "new_eval_run_id", "new_join_code"):
        assert name in doc


def test_held_ids_are_fixed_and_distinct() -> None:
    assert ids.NS_HELD == UUID("37630901-4b78-4ad9-ac16-f8b7f0af0841")
    assert ids.held_release_id(7) == uuid5(ids.NS_HELD, "7") == ids.held_release_id(7)
    assert ids.held_release_id(7) != ids.held_release_id(8)
    inbound = uuid4()
    assert ids.held_cancelled_id(inbound) == uuid5(ids.NS_HELD, f"cancelled:{inbound}")
    assert ids.held_cancelled_id(inbound) != ids.held_cancelled_id(uuid4())
    for bad in ("7", True, None):
        with pytest.raises(ValueError):
            ids.held_release_id(bad)


def test_reminder_message_id_is_fixed() -> None:
    assert ids.NS_REMINDER == UUID("173484b3-c5d5-4a5d-966b-be5f299ae04d")
    assert ids.reminder_message_id(3) == uuid5(ids.NS_REMINDER, "3")
    assert ids.reminder_message_id(3) != ids.reminder_message_id(4)
    assert ids.reminder_message_id(3) != ids.held_release_id(3)
    for bad in ("3", False, None):
        with pytest.raises(ValueError):
            ids.reminder_message_id(bad)


def test_org_order_ref_is_short_stable_and_per_org() -> None:
    order = uuid4()
    ref = ids.org_order_ref("org-1", order)
    expected = base64.b32encode(hashlib.sha256(f"org-1:{order}".encode()).digest())[:8]
    assert ref == expected.decode()
    assert len(ref) == 8 and ref == ids.org_order_ref("org-1", order)
    assert ref != ids.org_order_ref("org-2", order)
    with pytest.raises(ValueError):
        ids.org_order_ref("", order)
