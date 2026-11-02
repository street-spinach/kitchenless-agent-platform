"""From card to vendor key, and the vendor labels (adapters §5 and §6, file 9).

*   `card_vendor_key(area, card, tool)`: the `modes` vendor key of a card's tool, or `None`
    for a `mock` card (which always uses the area's mock adapter). Every tool of a card
    maps to the same key. A vendor with no entry raises
    `AdapterFailure("adapter_not_configured", retryable=False)`.
*   `vendor_label(area, card, org_name, run_modes)`: the badge (`sandbox` or `simulated`)
    and the text shown on the run view, the panel and WhatsApp order text.
*   `DISCLAIMER`: the one copy of the disclaimer text.

Imported by `mcp` and `api`.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from services.common import modes
from services.common.directory import AgentCard

__all__ = ["DISCLAIMER", "MOCK_VENDOR", "VendorLabel", "card_vendor_key", "vendor_label"]

MOCK_VENDOR = "mock"

_SELLER_TOOLS = ("search_items", "quote", "place_order", "amend_order")
_DELIVERY_TOOLS = ("quote_delivery", "book_delivery", "get_delivery_status")

# area -> card vendor -> tool -> vendor key. One key per card (adapters §5).
_VENDOR_KEYS: dict[str, dict[str, dict[str, str]]] = {
    "grocery": {
        "square": dict.fromkeys(_SELLER_TOOLS, "square_retail"),
        "kroger": dict.fromkeys(_SELLER_TOOLS, "kroger_catalog"),
    },
    "restaurant": {
        "square": dict.fromkeys(_SELLER_TOOLS, "square_restaurant"),
    },
    "delivery": {
        "doordash": dict.fromkeys(_DELIVERY_TOOLS, "doordash"),
    },
}

_SIMULATED_SUFFIX = "(simulated)"

DISCLAIMER = (
    "Square orders go to Square's sandbox. Hillcrest Market, DoorDash Drive and Uber Direct"
    " are simulated by Kitchenless, this platform. No real orders, payments or deliveries are"
    " made."
)


def card_vendor_key(area: str, card: AgentCard, tool: str) -> str | None:
    """The vendor key of `card`'s `tool` in `area`, or `None` for a `mock` card.

    Raises `AdapterFailure("adapter_not_configured", retryable=False)` for a vendor, area
    or tool with no entry.
    """
    from services.mcp.meta import AdapterFailure

    if card.vendor == MOCK_VENDOR:
        return None
    key = _VENDOR_KEYS.get(area, {}).get(card.vendor or "", {}).get(tool)
    if key is None:
        raise AdapterFailure(modes.NOT_CONFIGURED, retryable=False)
    return key


@dataclass(frozen=True)
class VendorLabel:
    """How a card is shown in a run (adapters §6)."""

    badge: Literal["sandbox", "simulated"]
    text: str


def _card_key(area: str, card: AgentCard) -> str | None:
    tools = _DELIVERY_TOOLS if area == "delivery" else _SELLER_TOOLS
    return card_vendor_key(area, card, tools[0])


def vendor_label(
    area: str, card: AgentCard, org_name: str, run_modes: Mapping[str, str]
) -> VendorLabel:
    """`sandbox` when the card's key is `real` in `run_modes` and has a real class;
    otherwise `simulated`. The text is `org_name`, with ` (<badge>)` unless it already ends
    in "(simulated)"."""
    key = _card_key(area, card)
    real = key is not None and key not in modes.ALWAYS_MOCK and run_modes.get(key) == "real"
    badge: Literal["sandbox", "simulated"] = "sandbox" if real else "simulated"
    text = org_name if org_name.endswith(_SIMULATED_SUFFIX) else f"{org_name} ({badge})"
    return VendorLabel(badge=badge, text=text)
