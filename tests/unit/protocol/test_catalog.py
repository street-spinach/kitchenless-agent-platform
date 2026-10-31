"""The message catalog and summary lines (agent-protocol §5, §8, P3)."""

import pytest

from services.common.protocol import CATALOG, render_summary
from services.common.protocol.catalog import (
    PARTY_TERMS,
    SLOTS,
    CatalogRow,
    PartyRule,
    template_slots,
)
from services.common.protocol.schemas import SCHEMAS

# Architecture §13 as first written listed these 20 types; agent-protocol §5 keeps them all.
ARCHITECTURE_SECTION13_TYPES = [
    "user_message",
    "start_weekly_order",
    "start_dinner",
    "constraints_request",
    "constraints",
    "quote_request",
    "offer",
    "counter_offer",
    "accept",
    "reject",
    "cancel",
    "order_confirm",
    "delivery_quote_request",
    "delivery_quote",
    "delivery_request",
    "delivery_booked",
    "order_status",
    "approval_decision",
    "quote_round_closed",
    "whatsapp_out",
]
NEW_TYPES = [
    "protocol_error",
    "shortfall_detected",
    "shortfall_notice",
    "shortfall_relay",
    "shortfall_closed",
    "shortfall_decision",
]
OUTBOX_ONLY = {
    "user_message",
    "start_weekly_order",
    "start_dinner",
    "constraints_request",
    "constraints",
    "order_status",
    "approval_decision",
    "quote_round_closed",
    "whatsapp_out",
    "shortfall_detected",
    "shortfall_relay",
    "shortfall_closed",
}


def test_every_type_has_catalog_row():
    assert len(CATALOG) == 26
    assert set(CATALOG) == set(SCHEMAS) == set(ARCHITECTURE_SECTION13_TYPES + NEW_TYPES)
    for type_, row in CATALOG.items():
        assert isinstance(row, CatalogRow)
        assert row.type == type_
        assert row.schema is SCHEMAS[type_]
        assert row.senders and row.receivers
    assert {t for t, row in CATALOG.items() if row.outbox_only} == OUTBOX_ONLY
    # protocol_error never goes to system or channel senders.
    receivers = CATALOG["protocol_error"].receivers
    assert not any(r == "system" or r.startswith(("system:", "channel")) for r in receivers)


def test_every_type_has_summary_template(samples):
    for type_, row in CATALOG.items():
        assert row.template.strip()
        summary = render_summary(type_, samples[type_])
        assert summary and "{" not in summary
    with pytest.raises(ValueError):
        CatalogRow(
            type="x",
            senders=frozenset({"household"}),
            receivers=frozenset({"grocery"}),
            schema=SCHEMAS["offer"],
            template="",
            party=PartyRule("order", None, None),
            outbox_only=False,
        )


def test_templates_use_only_allowlisted_slots():
    allowed = {
        "order",
        "quote",
        "shortfall",
        "ref",
        "total",
        "price",
        "n_items",
        "n_lines",
        "n_offered",
        "n_subs",
        "n_drop",
        "n_codes",
        "round",
        "reason",
        "code",
        "state",
        "status",
        "window",
        "expires",
        "reply_by",
    }
    assert set(SLOTS) == allowed
    for row in CATALOG.values():
        assert template_slots(row.template) <= allowed
    # A bad template raises when the row is built, which for CATALOG is import time.
    for bad in ("Offer from {offered_name}", "Order {order_id}", "Total {total:>10}"):
        with pytest.raises(ValueError):
            CatalogRow(
                type="offer",
                senders=frozenset({"grocery"}),
                receivers=frozenset({"household"}),
                schema=SCHEMAS["offer"],
                template=bad,
                party=PartyRule("quote", None, None),
                outbox_only=False,
            )


def test_every_slot_function_defined(samples):
    for name, slot in SLOTS.items():
        assert callable(slot), name
    for type_, row in CATALOG.items():
        body = row.schema.model_validate(samples[type_])
        for name in template_slots(row.template):
            assert isinstance(SLOTS[name](body), str)
    assert render_summary("offer", samples["offer"]) == (
        "Offer $12.48: 2 of 3 items, 1 substitutes, expires 18:30"
    )
    assert render_summary("quote_request", samples["quote_request"]) == (
        "Quote request for order 11111111: 1 items, reply by 17:00"
    )
    assert render_summary("delivery_quote", samples["delivery_quote"]) == (
        "Courier quote $5.99, window 17:00–18:30"
    )
    assert render_summary("shortfall_decision", samples["shortfall_decision"]) == (
        "Shortfall 33333333: 1 substituted, 1 dropped"
    )
    assert render_summary("constraints", samples["constraints"]) == (
        "Constraints received (3 codes)"
    )
    assert render_summary("protocol_error", samples["protocol_error"]) == (
        "Refused 44444444: wrong_party"
    )


def test_summary_capped_at_120(samples, monkeypatch):
    for type_ in CATALOG:
        assert len(render_summary(type_, samples[type_])) <= 120
    row = CATALOG["order_status"]
    long_row = CatalogRow(
        type=row.type,
        senders=row.senders,
        receivers=row.receivers,
        schema=row.schema,
        template="Order {order} is {state}. " * 10,
        party=row.party,
        outbox_only=row.outbox_only,
    )
    monkeypatch.setitem(CATALOG, "order_status", long_row)
    summary = render_summary("order_status", samples["order_status"])
    assert len(summary) == 120
    assert summary.endswith("...")
    assert summary[:117] == long_row.template.format(order="11111111", state="in_delivery")[:117]


def test_summary_never_contains_rationale(samples):
    for type_ in ("offer", "delivery_quote"):
        assert "SECRET-RATIONALE" in samples[type_]["rationale"]
        assert "SECRET-RATIONALE" not in render_summary(type_, samples[type_])
    for row in CATALOG.values():
        assert "rationale" not in row.template
    # No summary carries names, profile values or addresses from the sample bodies.
    forbidden = ["Oat milk", "Soy milk", "olives", "tree_nuts", "vegetarian", "hello", "us-oh"]
    for type_ in CATALOG:
        summary = render_summary(type_, samples[type_])
        assert not any(word in summary for word in forbidden), (type_, summary)
    assert samples["delivery_request"]["address_ref"][:8] not in render_summary(
        "delivery_request", samples["delivery_request"]
    )


def test_architecture_section13_types_all_present():
    for type_ in ARCHITECTURE_SECTION13_TYPES:
        assert type_ in CATALOG, type_
    for type_ in NEW_TYPES:
        assert type_ in CATALOG, type_


def test_every_row_has_party_rule():
    for type_, row in CATALOG.items():
        party = row.party
        assert isinstance(party, PartyRule), type_
        assert party.ref in {"order", "quote", "shortfall", "message", "run"}, type_
        assert party.sender or party.receiver, type_
        for term in (party.sender or ()) + (party.receiver or ()):
            assert term in PARTY_TERMS, (type_, term)
    assert CATALOG["accept"].party.ref == "quote"
    assert CATALOG["delivery_request"].party.receiver == ("orders.delivery_agent_id",)
    assert CATALOG["protocol_error"].party.ref == "message"
    assert CATALOG["shortfall_decision"].party.receiver == ("shortfalls.seller_agent_id",)
    with pytest.raises(ValueError):
        PartyRule("order", ("anyone",), None)
