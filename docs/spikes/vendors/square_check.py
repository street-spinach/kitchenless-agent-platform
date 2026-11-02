"""Square sandbox spike: one pass that proves what the adapters design needs.

Run in your own terminal (the token never goes in a file):
    export SQUARE_SANDBOX_TOKEN=...        # sandbox access token from the Developer Console
    uv run --with requests python docs/spikes/vendors/square_check.py

It prints ids, states and amounts only, never the token or full vendor bodies.
Everything it creates lives in the sandbox and is safe to leave there.
"""

import os
import sys
import uuid

import requests

BASE = "https://connect.squareupsandbox.com/v2"
VERSION = "2025-01-23"


def call(method: str, path: str, body: dict | None = None) -> dict:
    token = os.environ.get("SQUARE_SANDBOX_TOKEN")
    if not token:
        sys.exit("Set SQUARE_SANDBOX_TOKEN first (export it in this terminal).")
    r = requests.request(
        method,
        f"{BASE}{path}",
        json=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Square-Version": VERSION,
            "Content-Type": "application/json",
        },
        timeout=20,
    )
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        errs = [(e.get("category"), e.get("code"), e.get("detail")) for e in data.get("errors", [])]
        print(f"FAIL {method} {path}: HTTP {r.status_code} {errs}")
        sys.exit(1)
    return data


results: list[tuple[str, bool, str]] = []

# 1. Locations: the default test account's mock location, and its country and currency.
locs = call("GET", "/locations").get("locations", [])
loc = locs[0] if locs else {}
results.append(
    (
        "locations",
        bool(loc),
        f"{len(locs)} location(s); first: country={loc.get('country')} "
        f"currency={loc.get('currency')} status={loc.get('status')}",
    )
)
if not loc:
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    sys.exit(1)
currency = loc.get("currency", "USD")

# 2. Catalog: one item with one priced variation.
item = call(
    "POST",
    "/catalog/object",
    {
        "idempotency_key": str(uuid.uuid4()),
        "object": {
            "type": "ITEM",
            "id": "#spike-item",
            "item_data": {
                "name": "Spike oat milk 1L",
                "variations": [
                    {
                        "type": "ITEM_VARIATION",
                        "id": "#spike-var",
                        "item_variation_data": {
                            "name": "1L",
                            "pricing_type": "FIXED_PRICING",
                            "price_money": {"amount": 399, "currency": currency},
                            "track_inventory": True,
                        },
                    }
                ],
            },
        },
    },
)
var_id = next(
    m["object_id"] for m in item.get("id_mappings", []) if m["client_object_id"] == "#spike-var"
)
results.append(("catalog item", True, f"variation id {var_id}"))

# 3. Inventory: set 5 in stock, read it back.
call(
    "POST",
    "/inventory/changes/batch-create",
    {
        "idempotency_key": str(uuid.uuid4()),
        "changes": [
            {
                "type": "PHYSICAL_COUNT",
                "physical_count": {
                    "catalog_object_id": var_id,
                    "location_id": loc["id"],
                    "state": "IN_STOCK",
                    "quantity": "5",
                    "occurred_at": "2026-11-02T00:00:00Z",
                },
            }
        ],
    },
)
counts = call(
    "POST",
    "/inventory/counts/batch-retrieve",
    {"catalog_object_ids": [var_id], "location_ids": [loc["id"]]},
).get("counts", [])
qty = counts[0].get("quantity") if counts else None
results.append(("inventory", qty == "5", f"quantity read back: {qty}"))

# 4. Order with a pickup fulfillment (restaurant-style) for 2 of the item.
order = call(
    "POST",
    "/orders",
    {
        "idempotency_key": str(uuid.uuid4()),
        "order": {
            "location_id": loc["id"],
            "line_items": [{"catalog_object_id": var_id, "quantity": "2"}],
            "fulfillments": [
                {
                    "type": "PICKUP",
                    "state": "PROPOSED",
                    "pickup_details": {
                        "recipient": {"display_name": "Spike Test"},
                        "schedule_type": "ASAP",
                    },
                }
            ],
        },
    },
).get("order", {})
total = order.get("total_money", {})
ful = (order.get("fulfillments") or [{}])[0]
results.append(
    (
        "order + pickup fulfillment",
        bool(order.get("id")),
        f"order state={order.get('state')} total={total.get('amount')} "
        f"{total.get('currency')} fulfillment={ful.get('type')}/{ful.get('state')}",
    )
)

# 5. Same idempotency key twice returns the same order (the adapters rely on this).
key = str(uuid.uuid4())
body = {
    "idempotency_key": key,
    "order": {
        "location_id": loc["id"],
        "line_items": [{"catalog_object_id": var_id, "quantity": "1"}],
    },
}
a = call("POST", "/orders", body).get("order", {}).get("id")
b = call("POST", "/orders", body).get("order", {}).get("id")
results.append(("idempotent create", bool(a) and a == b, f"same order id on replay: {a == b}"))

for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
sys.exit(0 if all(ok for _, ok, _ in results) else 1)
