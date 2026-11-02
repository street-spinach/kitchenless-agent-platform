"""Message body schemas (agent-protocol §4, §5, P2)."""

import pytest
from pydantic import BaseModel, ValidationError

from services.common.protocol.schemas import (
    SCHEMAS,
    DeliveryRequest,
    Ref,
    SetupAnswer,
    SetupPerson,
)

TYPES_26 = {
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
    "protocol_error",
    "shortfall_detected",
    "shortfall_notice",
    "shortfall_relay",
    "shortfall_closed",
    "shortfall_decision",
}


def _models() -> set[type[BaseModel]]:
    return set(SCHEMAS.values())


def test_every_type_has_schema(samples):
    assert set(SCHEMAS) == TYPES_26
    for type_, model in SCHEMAS.items():
        assert issubclass(model, BaseModel)
        body = model.model_validate(samples[type_])
        assert body.schema_version == 1


def test_unknown_fields_ignored(samples):
    for type_, model in SCHEMAS.items():
        body = model.model_validate({**samples[type_], "added_later": {"x": 1}})
        assert "added_later" not in body.model_dump()


def test_schema_version_2_rejected_as_unsupported(samples):
    for type_, model in SCHEMAS.items():
        assert model.model_validate({**samples[type_], "schema_version": 1}).schema_version == 1
        with pytest.raises(ValidationError) as exc:
            model.model_validate({**samples[type_], "schema_version": 2})
        # The error sits on schema_version, so the check maps it to unsupported_version.
        assert [e["loc"] for e in exc.value.errors()] == [("schema_version",)]


def test_constraints_accept_only_known_codes(samples):
    request = samples["quote_request"]
    for bad in ({"allergens": ["nuts"], "diets": []}, {"allergens": [], "diets": ["paleo"]}):
        with pytest.raises(ValidationError):
            SCHEMAS["quote_request"].model_validate({**request, "constraints": bad})
    with pytest.raises(ValidationError):
        SCHEMAS["constraints"].model_validate({**samples["constraints"], "allergens": ["nut"]})
    with pytest.raises(ValidationError):
        SCHEMAS["constraints"].model_validate({**samples["constraints"], "diets": ["paleo"]})
    ok = SCHEMAS["quote_request"].model_validate(
        {**request, "constraints": {"allergens": ["sesame", "tree_nuts"], "diets": ["vegan"]}}
    )
    assert ok.model_dump(mode="json")["constraints"] == {
        "allergens": ["sesame", "tree_nuts"],
        "diets": ["vegan"],
    }


def test_address_ref_only_on_delivery_request(samples):
    with_ref = {m for m in _models() if "address_ref" in m.model_fields}
    assert with_ref == {DeliveryRequest}
    for type_, model in SCHEMAS.items():
        if model is DeliveryRequest:
            continue
        body = model.model_validate(
            {**samples[type_], "address_ref": samples["delivery_request"]["address_ref"]}
        )
        assert "address_ref" not in body.model_dump()
    body = samples["delivery_request"]
    del body["address_ref"]
    with pytest.raises(ValidationError):
        DeliveryRequest.model_validate(body)


MONEY_FIELDS = [
    ("offer", "total_minor"),
    ("counter_offer", "total_minor"),
    ("accept", "total_minor"),
    ("order_confirm", "total_minor"),
    ("delivery_quote", "price_minor"),
]


@pytest.mark.parametrize(("type_", "field"), MONEY_FIELDS)
def test_money_fields_are_minor_units(samples, type_, field):
    model = SCHEMAS[type_]
    assert model.model_validate({**samples[type_], field: 4210}).model_dump()[field] == 4210
    for bad in (42.10, 4210.0, "4210"):
        with pytest.raises(ValidationError):
            model.model_validate({**samples[type_], field: bad})
    # Line amounts are minor units too.
    offer = samples["offer"]
    offer["lines"] = [{**offer["lines"][0], "unit_minor": 3.99}]
    with pytest.raises(ValidationError):
        SCHEMAS["offer"].model_validate(offer)
    # Currency is an ISO 4217 code.
    for bad in ("usd", "US", "$"):
        with pytest.raises(ValidationError):
            SCHEMAS["offer"].model_validate({**samples["offer"], "currency": bad})


@pytest.mark.parametrize("type_", ["accept", "reject", "cancel"])
def test_quote_id_required_on_decisions(samples, type_):
    body = samples[type_]
    del body["quote_id"]
    with pytest.raises(ValidationError) as exc:
        SCHEMAS[type_].model_validate(body)
    assert ("quote_id",) in [e["loc"] for e in exc.value.errors()]


@pytest.mark.parametrize("type_", ["start_weekly_order", "start_dinner"])
def test_start_order_scope_defaults_to_household(samples, type_):
    # Agents AG-A4: `scope` is additive with a default, so a body without it still parses.
    body = {k: v for k, v in samples[type_].items() if k != "scope"}
    assert SCHEMAS[type_].model_validate(body).scope == "household"
    assert SCHEMAS[type_].model_validate({**body, "scope": "solo"}).scope == "solo"
    with pytest.raises(ValidationError):
        SCHEMAS[type_].model_validate({**body, "scope": "everyone"})


# Experience EX-I6.


def test_whatsapp_out_ref_optional(samples):
    out = SCHEMAS["whatsapp_out"].model_validate(samples["whatsapp_out"])
    assert out.ref is None
    with_ref = SCHEMAS["whatsapp_out"].model_validate(
        {**samples["whatsapp_out"], "ref": {"kind": "approval", "id": "abc"}}
    )
    assert with_ref.ref == Ref(kind="approval", id="abc")


def test_ref_kind_closed():
    for kind in ("approval", "order", "grant", "reminder"):
        assert Ref(kind=kind, id="1").kind == kind
    for kind in ("household", "", "Approval"):
        with pytest.raises(ValidationError):
            Ref.model_validate({"kind": kind, "id": "1"})


def test_setup_answer_rejects_unknown_codes():
    person = {"name": "Kavi", "child": True, "add_allergies": ["sesame"]}
    ok = SetupAnswer.model_validate(
        {"people": [person], "budget_minor": 15000, "unmatched": ["spicy"]}
    )
    assert ok.people[0] == SetupPerson(name="Kavi", child=True, add_allergies=["sesame"])
    assert ok.people[0].phone is None and ok.people[0].remove_diets == []
    for field in ("add_allergies", "remove_allergies"):
        with pytest.raises(ValidationError):
            SetupAnswer.model_validate({"people": [{**person, field: ["nuts"]}]})
    for field in ("add_diets", "remove_diets"):
        with pytest.raises(ValidationError):
            SetupAnswer.model_validate({"people": [{**person, field: ["paleo"]}]})
    for bad in (
        {"people": [{**person, "phone": "0771234567"}]},
        {"people": [{**person, "name": " "}]},
        {"people": [{"name": "Kavi"}]},
        {"budget_minor": -1},
        {"budget_minor": 12.5},
    ):
        with pytest.raises(ValidationError):
            SetupAnswer.model_validate(bad)
    assert SetupAnswer.model_validate({}).people == []
