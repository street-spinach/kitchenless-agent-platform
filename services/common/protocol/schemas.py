"""Message body schemas shared by both bindings (agent-protocol §5, P2).

Every model carries `schema_version: Literal[1]` and ignores unknown fields (§4). Money is
integer minor units plus a currency code (architecture §3). Allergen and diet fields accept
only the architecture §5 codes.
"""

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, StringConstraints


class AllergenCode(StrEnum):
    """The EU 14 allergen codes (architecture §5)."""

    CELERY = "celery"
    GLUTEN = "gluten"
    CRUSTACEANS = "crustaceans"
    EGGS = "eggs"
    FISH = "fish"
    LUPIN = "lupin"
    MILK = "milk"
    MOLLUSCS = "molluscs"
    MUSTARD = "mustard"
    TREE_NUTS = "tree_nuts"
    PEANUTS = "peanuts"
    SESAME = "sesame"
    SOY = "soy"
    SULPHITES = "sulphites"


class DietCode(StrEnum):
    """The diet codes (architecture §5)."""

    VEGETARIAN = "vegetarian"
    VEGAN = "vegan"
    PESCATARIAN = "pescatarian"
    HALAL = "halal"
    KOSHER = "kosher"
    KETO = "keto"
    HIGH_PROTEIN = "high_protein"


Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
Minor = StrictInt  # money in integer minor units; floats and strings are refused
Qty = Annotated[StrictInt, Field(ge=1)]
Round = Annotated[StrictInt, Field(ge=0, le=2)]
Time = AwareDatetime

OrderState = Literal[
    "draft",
    "quoting",
    "negotiating",
    "awaiting_approval",
    "authorized",
    "placed",
    "courier_booked",
    "in_delivery",
    "delivered",
    "cancelled",
    "expired",
    "failed",
]
DecisionReason = Literal[
    "expired",
    "closed",
    "items_changed",
    "price",
    "no_courier",
    "already_placed",
    "buyer_cancelled",
    "other",
]
ErrorCode = Literal[
    "unknown_type",
    "unsupported_version",
    "invalid_body",
    "wrong_sender_class",
    "unknown_reference",
    "wrong_party",
]
ReasonCode = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: Literal[1] = 1


class _Part(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Window(_Part):
    start: Time
    end: Time


class RequestItem(_Part):
    item: str
    qty: Qty
    unit: str


class RequestConstraints(_Part):
    allergens: list[AllergenCode] = []
    diets: list[DietCode] = []


class QuoteLine(_Part):
    line: StrictInt
    requested_item: str
    offered_sku: str | None
    offered_name: str | None
    qty: Qty
    unit_minor: Minor
    substituted_for: str | None = None


class OrderLine(_Part):
    """One `order_items` row (architecture item 11)."""

    line: StrictInt
    sku: str
    name: str
    qty: Qty
    unit_minor: Minor
    substituted_for: str | None = None


# S1


class QuoteRequest(_Body):
    order_id: UUID
    kind: Literal["grocery", "dinner"]
    items: list[RequestItem]
    constraints: RequestConstraints
    currency: Currency
    zone_id: str
    window: Window
    reply_by: Time


# S2 (offer, counter_offer). `expires_at`, `final` and `rationale` are set on offers only.


class Quote(_Body):
    order_id: UUID
    quote_id: UUID
    round: Round
    lines: list[QuoteLine]
    total_minor: Minor
    currency: Currency
    expires_at: Time | None = None
    final: bool | None = None
    rationale: str | None = None


# S3 (accept, reject, cancel). `items_hash` and `total_minor` are set on accept only.


class QuoteDecision(_Body):
    order_id: UUID
    quote_id: UUID
    items_hash: str | None = None
    total_minor: Minor | None = None
    reason: DecisionReason | None = None


# S4 (order_confirm, delivery_booked, order_status). `lines` is set on order_confirm only.


class OrderUpdate(_Body):
    order_id: UUID
    state: OrderState
    lines: list[OrderLine] = []
    total_minor: Minor | None = None
    currency: Currency | None = None
    delivery_agent_id: str | None = None
    window: Window | None = None
    reason: ReasonCode | None = None


# S5


class DeliveryQuoteRequest(_Body):
    order_id: UUID
    zone_id: str
    window: Window
    pickup_agent_id: str


class DeliveryQuote(_Body):
    order_id: UUID
    quote_id: UUID
    window: Window
    price_minor: Minor
    currency: Currency
    expires_at: Time
    rationale: str | None = None


class DeliveryRequest(_Body):
    order_id: UUID
    quote_id: UUID
    window: Window
    zone_id: str
    address_ref: UUID


# S6 (shortfall_notice and shortfall_relay share ShortfallNotice)


class Substitute(_Part):
    sku: str
    name: str
    qty: Qty
    unit_minor: Minor


class ShortfallLine(_Part):
    line: StrictInt
    sku: str
    name: str
    qty_missing: Qty
    line_total_minor: Minor
    substitutes: Annotated[list[Substitute], Field(max_length=2)] = []


class ShortfallNotice(_Body):
    order_id: UUID
    shortfall_id: UUID
    lines: list[ShortfallLine]
    reply_by: Time


class ShortfallDecision(_Body):
    order_id: UUID
    shortfall_id: UUID
    picks: dict[int, str]  # line -> substitute SKU or "drop"


# S7


class ProtocolErrorBody(_Body):
    ref_message_id: UUID
    code: ErrorCode
    detail: Annotated[str, Field(max_length=200)]


# S8: people, system and channel types. None carries a profile to a business agent.


class UserMessage(_Body):
    inbound_id: UUID


class StartOrder(_Body):
    kind: Literal["grocery", "dinner"]


class ConstraintsRequest(_Body):
    order_id: UUID
    user_id: UUID


class Constraints(_Body):
    order_id: UUID
    allergens: list[AllergenCode]
    diets: list[DietCode]
    dislikes: list[str]


class ApprovalDecision(_Body):
    approval_id: UUID
    order_id: UUID
    status: Literal["approved", "rejected", "expired"]


class QuoteRoundClosed(_Body):
    order_id: UUID
    round: Literal["quote", "delivery"]


class Button(_Part):
    payload: str
    title: str


class WhatsAppOut(_Body):
    user_id: UUID
    template: str
    params: dict[str, str]
    buttons: list[Button]


class ShortfallDetected(_Body):
    order_id: UUID
    skus: list[str]
    event_id: UUID


class ShortfallClosed(_Body):
    shortfall_id: UUID
    order_id: UUID
    reason: Literal["all_picked", "timeout", "picked_up"]


SCHEMAS: dict[str, type[BaseModel]] = {
    "user_message": UserMessage,
    "start_weekly_order": StartOrder,
    "start_dinner": StartOrder,
    "constraints_request": ConstraintsRequest,
    "constraints": Constraints,
    "quote_request": QuoteRequest,
    "offer": Quote,
    "counter_offer": Quote,
    "accept": QuoteDecision,
    "reject": QuoteDecision,
    "cancel": QuoteDecision,
    "order_confirm": OrderUpdate,
    "delivery_quote_request": DeliveryQuoteRequest,
    "delivery_quote": DeliveryQuote,
    "delivery_request": DeliveryRequest,
    "delivery_booked": OrderUpdate,
    "order_status": OrderUpdate,
    "approval_decision": ApprovalDecision,
    "quote_round_closed": QuoteRoundClosed,
    "whatsapp_out": WhatsAppOut,
    "protocol_error": ProtocolErrorBody,
    "shortfall_detected": ShortfallDetected,
    "shortfall_notice": ShortfallNotice,
    "shortfall_relay": ShortfallNotice,
    "shortfall_closed": ShortfallClosed,
    "shortfall_decision": ShortfallDecision,
}
