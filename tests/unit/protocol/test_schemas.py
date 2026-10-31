"""Message body schemas (agent-protocol §4, §5, P2)."""

import pytest
from pydantic import BaseModel, ValidationError

from services.common.protocol.schemas import SCHEMAS, DeliveryRequest

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
