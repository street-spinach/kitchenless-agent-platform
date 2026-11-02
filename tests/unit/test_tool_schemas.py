"""The architecture item 43 tool arguments are still accepted (adapters §10, file 18)."""

import asyncio
import importlib
from typing import Any

import pytest

# Architecture item 43, with the #47 and #48 amendments: server -> tool -> argument -> type.
# A type is the JSON Schema `type` (with `format` for uuid and date-time), `object` for a
# model, or a tuple for a union. `open_shortfall(...)` is written as it is on main.
ITEM_43: dict[str, dict[str, dict[str, Any]]] = {
    "grocery": {
        "search_items": {"query": "string", "filters": "object"},
        "quote": {"items": "array"},
        "place_order": {
            "order_id": "uuid",
            "quote_id": "uuid",
            "items_hash": "string",
            "delivery_agent_id": "string",
        },
        "amend_order": {"order_id": "uuid", "shortfall_id": "uuid", "picks": "object"},
    },
    "restaurant": {
        "search_items": {"query": "string", "filters": "object"},
        "quote": {"items": "array"},
        "place_order": {
            "order_id": "uuid",
            "quote_id": "uuid",
            "items_hash": "string",
            "delivery_agent_id": "string",
        },
        "amend_order": {"order_id": "uuid", "shortfall_id": "uuid", "picks": "object"},
    },
    "delivery": {
        "quote_delivery": {"order_id": "uuid", "zone_id": "string", "window": "object"},
        "book_delivery": {"order_id": "uuid", "address_ref": "uuid", "window": "object"},
        "get_delivery_status": {"order_id": "uuid"},
    },
    "household": {
        "get_constraints": {"user_id": "uuid"},
        "create_order": {"order_id": "uuid", "kind": "string"},
        "set_order_state": {
            "order_id": "uuid",
            "expected": ("string", "array"),
            "new": "string",
        },
        "get_pantry": {},
        "get_budget": {},
        "authorize_spend": {
            "order_id": "uuid",
            "quote_id": "uuid",
            "items": "array",
            "total": "object",
        },
        "request_approval": {"order_id": "uuid", "amount": "object"},
        "decide_approval": {"approval_id": "uuid", "decision": "string", "user_id": "uuid"},
        "record_delivery": {"order_id": "uuid"},
        "choose_shortfall_option": {
            "shortfall_id": "uuid",
            "line": "integer",
            "pick": "string",
            "user_id": "uuid",
        },
        "authorize_amendment": {"order_id": "uuid", "shortfall_id": "uuid", "picks": "object"},
        "open_shortfall": {
            "shortfall_id": "uuid",
            "order_id": "uuid",
            "seller": "string",
            "lines": "array",
            "due_at": "date-time",
        },
        "get_shortfall": {"shortfall_id": "uuid"},
    },
    "knowledge": {
        "check_allergens": {"skus": "array", "allergen_codes": "array"},
        "find_substitutes": {"sku": "string", "constraints": "object"},
        "explain_path": {"sku": "string", "allergen_code": "string"},
    },
}


def _type(prop: dict[str, Any], defs: dict[str, Any]) -> Any:
    if "$ref" in prop:
        return _type(defs[prop["$ref"].rsplit("/", 1)[-1]], defs)
    if "anyOf" in prop:
        return tuple(_type(p, defs) for p in prop["anyOf"])
    if "enum" in prop and "type" not in prop:
        return "string"
    kind = prop.get("type", "object")
    if kind == "string" and prop.get("format") in ("uuid", "date-time"):
        return prop["format"]
    return kind


def _schemas(area: str) -> dict[str, dict[str, Any]]:
    server = importlib.import_module(f"services.mcp.{area}.server").server
    tools = asyncio.run(server.list_tools())
    return {t.name: t.input_schema for t in tools}


@pytest.mark.parametrize("area", sorted(ITEM_43))
def test_item_43_arguments_still_accepted(area: str) -> None:
    schemas = _schemas(area)
    for tool, args in ITEM_43[area].items():
        assert tool in schemas, f"{area}.{tool} is missing"
        schema = schemas[tool]
        props = schema.get("properties", {})
        defs = schema.get("$defs", {})
        for name, expected in args.items():
            assert name in props, f"{area}.{tool} no longer accepts {name}"
            assert _type(props[name], defs) == expected, f"{area}.{tool}.{name}"
        new_required = set(schema.get("required", [])) - set(args)
        assert not new_required, f"{area}.{tool} requires new arguments {new_required}"
