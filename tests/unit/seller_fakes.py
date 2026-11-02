"""Fakes for the grocery and restaurant handler tests (test_handlers.py,
test_grocery_handler.py): a seller MCP server over a small catalog, and stub choosers."""

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from services.agents.common import seller_stubs
from services.agents.common.model import ModelResult
from services.common.consumer import Inbound
from services.common.directory import AgentCard
from services.common.mcp_client import ToolRefused
from services.common.protocol.a2a import AgentMessageRow
from services.common.protocol.schemas import SCHEMAS
from tests.unit import model_fakes as mf

GROCERY = "grocery:mock-us"
HOUSEHOLD = "household:us-1"
ZONE = "us-oh-45202"
CARD = AgentCard(
    agent_id=GROCERY,
    agent_class="grocery",
    owner_ref="org:mock",
    region="us",
    zone_ids=(ZONE,),
    currency="USD",
    host_process="agents",
    vendor="mock",
)
# sku -> (name, allergens, unit price, stock)
CATALOG: Mapping[str, tuple[str, tuple[str, ...], int, int]] = {
    "MOCK-MILK-WHOLE": ("Whole milk 1 gal", ("milk",), 429, 10),
    "MOCK-MILK-2PCT": ("Reduced fat 2% milk 1 gal", ("milk",), 399, 10),
    "MOCK-MILK-ALMOND": ("Almond milk 64 oz", ("tree_nuts",), 379, 10),
    "MOCK-EGGS-12": ("Large eggs 12 ct", ("eggs",), 349, 10),
    "MOCK-BREAD-WHEAT": ("Whole wheat bread", ("gluten",), 299, 10),
}
# Floors are 95% of list price, rounded up (services/seed/data/rules.yaml).
FLOORS = {sku: -(-price * 95 // 100) for sku, (_, _, price, _) in CATALOG.items()}
RULES = {"floors": FLOORS}


@dataclass
class FakeSeller:
    """`call_tool` for `/mcp/grocery`: search, quote and place_order over `CATALOG`."""

    stock: dict[str, int] = field(default_factory=lambda: {k: v[3] for k, v in CATALOG.items()})
    calls: list[dict[str, Any]] = field(default_factory=list)
    refuse_place: str | None = None

    def of(self, tool: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["tool"] == tool]

    async def __call__(self, server: str, tool: str, args: dict, *, ctx: Any, token: str) -> dict:
        assert server == "grocery"
        self.calls.append({"tool": tool, "args": args, "ctx": ctx, "token": token})
        if tool == "search_items":
            words = args["query"].lower().split()
            banned = set(args["filters"].get("allergen_codes") or [])
            items = [
                {"sku": sku, "name": name, "unit_minor": price, "currency": "USD"}
                for sku, (name, allergens, price, _) in CATALOG.items()
                if all(w in name.lower() for w in words)
                and not banned & set(allergens)
                and self.stock.get(sku, 0) > 0
            ]
            return {"items": items}
        if tool == "quote":
            lines = []
            for i in args["items"]:
                name, _, price, _ = CATALOG[i["sku"]]
                avail = self.stock.get(i["sku"], 0) >= i["qty"]
                lines.append(
                    {
                        "sku": i["sku"],
                        "name": name,
                        "qty": i["qty"],
                        "unit_minor": price,
                        "available": avail,
                    }
                )
            return {"currency": "USD", "lines": lines}
        if tool == "place_order":
            if self.refuse_place:
                raise ToolRefused(self.refuse_place)
            return {
                "order_id": args["order_id"],
                "quote_id": args["quote_id"],
                "state": "placed",
                "external_order_id": "ext-1",
                "delivery_agent_id": args["delivery_agent_id"],
                "total_minor": 0,
                "currency": "USD",
            }
        raise AssertionError(f"unexpected tool {tool}")


class RecordingStubs:
    """The seller stubs, recording `(point, inputs)`; `answers` overrides a point's pick."""

    def __init__(self, answers: Mapping[str, Any] | None = None) -> None:
        self.seen: list[tuple[str, dict[str, Any]]] = []
        self.answers = dict(answers or {})

    def mapping(self) -> dict[str, Any]:
        def wrap(name: str, fn: Any) -> Any:
            def stub(inputs: Any, default: Any, *, out_of_bounds: bool = False) -> ModelResult:
                self.seen.append((name, json.loads(json.dumps(inputs, default=str))))
                if name in self.answers:
                    pick = self.answers[name]
                    return ModelResult(text=json.dumps({"pick": pick, "rationale": "ok"}))
                return fn(inputs, default, out_of_bounds=out_of_bounds)

            return stub

        return {name: wrap(name, fn) for name, fn in seller_stubs.STUBS.items()}


def chooser(stubs: RecordingStubs, traces: mf.FakeTraces, overrides: dict | None = None):
    return mf.client(
        traces=traces, stubs=stubs.mapping(), run_state=mf.FakeRunState(overrides or {})
    )


def request_body(order_id: uuid.UUID, items: list[tuple[str, int]], **extra: Any) -> dict:
    body = {
        "schema_version": 1,
        "order_id": str(order_id),
        "kind": "grocery",
        "items": [{"item": item, "qty": qty, "unit": "each"} for item, qty in items],
        "constraints": {"allergens": [], "diets": []},
        "currency": "USD",
        "zone_id": ZONE,
        "window": {"start": "2026-11-02T14:00:00+00:00", "end": "2026-11-02T16:00:00+00:00"},
        "reply_by": "2026-11-02T12:00:30+00:00",
    }
    body.update(extra)
    return body


def inbound(
    run_id: uuid.UUID,
    type: str,
    body: dict[str, Any],
    *,
    from_agent: str = HOUSEHOLD,
    to_agent: str = GROCERY,
    conversation_id: uuid.UUID | None = None,
    message_id: uuid.UUID | None = None,
) -> Inbound:
    raw = {"schema_version": 1, **body}
    row = AgentMessageRow(
        message_id=message_id or uuid.uuid4(),
        run_id=run_id,
        conversation_id=conversation_id,
        causation_id=None,
        from_agent=from_agent,
        to_agent=to_agent,
        type=type,
        body=raw,
    )
    return Inbound(row=row, attempts=1, body=SCHEMAS[type].model_validate(raw))
