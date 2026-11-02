"""From card to vendor key, labels and the disclaimer (adapters §5 and §6; issue #165)."""

import pytest

from services.common import vendors
from services.common.directory import AgentCard
from services.common.modes import VENDORS
from services.mcp.meta import AdapterFailure

SELLER_TOOLS = ("search_items", "quote", "place_order", "amend_order")
DELIVERY_TOOLS = ("quote_delivery", "book_delivery", "get_delivery_status")
# Every (area, vendor) that the seeded cards use (services/seed/data/agents.yaml).
CARDS = (
    ("grocery", "kroger"),
    ("grocery", "square"),
    ("grocery", "mock"),
    ("restaurant", "square"),
    ("restaurant", "mock"),
    ("delivery", "doordash"),
    ("delivery", "mock"),
)


def _card(area: str, vendor: str | None) -> AgentCard:
    return AgentCard(
        agent_id=f"{area}:{vendor}-us",
        agent_class=area,
        owner_ref=f"org:{vendor}",
        region="us",
        zone_ids=("us-oh-45202",),
        currency="USD",
        host_process="agents",
        vendor=vendor,
    )


def _tools(area: str) -> tuple[str, ...]:
    return DELIVERY_TOOLS if area == "delivery" else SELLER_TOOLS


def test_one_vendor_key_per_card() -> None:
    for area, vendor in CARDS:
        card = _card(area, vendor)
        keys = {vendors.card_vendor_key(area, card, tool) for tool in _tools(area)}
        assert len(keys) == 1, (area, vendor, keys)
        [key] = keys
        if vendor == "mock":
            assert key is None
        else:
            assert key in VENDORS


def test_card_vendor_keys() -> None:
    def key(area: str, vendor: str) -> str | None:
        return vendors.card_vendor_key(area, _card(area, vendor), _tools(area)[0])

    assert key("grocery", "kroger") == "kroger_catalog"
    assert key("grocery", "square") == "square_retail"
    assert key("restaurant", "square") == "square_restaurant"
    assert key("delivery", "doordash") == "doordash"


def test_square_maps_amend_order() -> None:
    for area, expected in (("grocery", "square_retail"), ("restaurant", "square_restaurant")):
        assert vendors.card_vendor_key(area, _card(area, "square"), "amend_order") == expected


@pytest.mark.parametrize(
    ("area", "vendor", "tool"),
    [
        ("grocery", "acme", "quote"),
        ("grocery", None, "quote"),
        ("restaurant", "kroger", "quote"),
        ("delivery", "square", "book_delivery"),
        ("grocery", "square", "book_delivery"),
        ("nowhere", "square", "quote"),
    ],
)
def test_unmapped_vendor_not_configured(area: str, vendor: str | None, tool: str) -> None:
    with pytest.raises(AdapterFailure) as e:
        vendors.card_vendor_key(area, _card(area, vendor), tool)
    assert (e.value.reason, e.value.retryable) == ("adapter_not_configured", False)


def test_vendor_label() -> None:
    real = dict.fromkeys(VENDORS, "real")
    mock = dict.fromkeys(VENDORS, "mock")
    cases = [
        ("grocery", "square", "Square Grocery", real, "sandbox", "Square Grocery (sandbox)"),
        ("grocery", "square", "Square Grocery", mock, "simulated", "Square Grocery (simulated)"),
        ("restaurant", "square", "Square Kitchen", real, "sandbox", "Square Kitchen (sandbox)"),
        (
            "delivery",
            "doordash",
            "DoorDash Drive (simulated)",
            mock,
            "simulated",
            "DoorDash Drive (simulated)",
        ),
        # `real` in the run but no real class: still simulated.
        (
            "delivery",
            "doordash",
            "DoorDash Drive (simulated)",
            real,
            "simulated",
            "DoorDash Drive (simulated)",
        ),
        (
            "grocery",
            "kroger",
            "Hillcrest Market",
            real,
            "simulated",
            "Hillcrest Market (simulated)",
        ),
        # A mock card is always simulated.
        (
            "delivery",
            "mock",
            "Uber Direct (simulated)",
            real,
            "simulated",
            "Uber Direct (simulated)",
        ),
        ("grocery", "mock", "Mock Grocery US", real, "simulated", "Mock Grocery US (simulated)"),
        # A key missing from the run's modes is mock.
        ("grocery", "square", "Square Grocery", {}, "simulated", "Square Grocery (simulated)"),
    ]
    for area, vendor, org_name, run_modes, badge, text in cases:
        label = vendors.vendor_label(area, _card(area, vendor), org_name, run_modes)
        assert label == vendors.VendorLabel(badge=badge, text=text), (area, vendor, run_modes)


def test_disclaimer() -> None:
    assert vendors.DISCLAIMER == (
        "Square orders go to Square's sandbox. Hillcrest Market, DoorDash Drive and Uber Direct"
        " are simulated by Kitchenless, this platform. No real orders, payments or deliveries"
        " are made."
    )
