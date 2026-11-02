"""Square sandbox spike for the adapters design (docs/designs/adapters.md, build step 0).

Answers the Open Questions table in adapters.md. Run in your own terminal (the token never
goes in a file):

    export SQUARE_SANDBOX_TOKEN=...        # sandbox access token from the Developer Console
    uv run python docs/spikes/vendors/square_check.py

It prints ids, states, amounts and error codes only, never the token or full vendor bodies.
Everything it creates lives in the sandbox and is safe to leave there. If the account has
only one location, it creates a second one named "Kitchenless Kitchen (sandbox)".

The 2026-11-02 run of the first version (catalog item, inventory, order, replay) is recorded
in docs/spikes/vendors.md.
"""

import os
import sys
import threading
import uuid
from typing import Any

import httpx

BASE = "https://connect.squareupsandbox.com/v2"
VERSION = "2025-01-23"
SECOND_LOCATION = "Kitchenless Kitchen (sandbox)"


def _token() -> str:
    token = os.environ.get("SQUARE_SANDBOX_TOKEN")
    if not token:
        sys.exit("Set SQUARE_SANDBOX_TOKEN first (export it in this terminal).")
    return token


def raw(method: str, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.request(
        method,
        f"{BASE}{path}",
        json=body,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Square-Version": VERSION,
            "Content-Type": "application/json",
        },
        timeout=20,
    )


def call(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    r = raw(method, path, body)
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        print(f"FAIL {method} {path}: HTTP {r.status_code} {errors(data)}")
        sys.exit(1)
    return data


def errors(data: dict[str, Any]) -> list[tuple[str | None, str | None]]:
    return [(e.get("category"), e.get("code")) for e in data.get("errors", [])]


def lines(currency: str, qty: int = 2) -> list[dict[str, Any]]:
    """Two ad-hoc line items, priced by us, as the adapters will send them."""
    return [
        {
            "name": "Oat milk 1L",
            "quantity": str(qty),
            "base_price_money": {"amount": 399, "currency": currency},
            "note": "MOCK-OAT-MILK",
        },
        {
            "name": "Sourdough loaf",
            "quantity": "1",
            "base_price_money": {"amount": 550, "currency": currency},
            "note": "MOCK-SOURDOUGH",
        },
    ]


def order_body(key: str, location_id: str, currency: str, qty: int = 2) -> dict[str, Any]:
    return {
        "idempotency_key": key,
        "order": {
            "location_id": location_id,
            "reference_id": str(uuid.uuid4()),
            "line_items": lines(currency, qty),
            "fulfillments": [
                {
                    "type": "PICKUP",
                    "state": "PROPOSED",
                    "pickup_details": {
                        "recipient": {"display_name": "order-spike"},
                        "schedule_type": "ASAP",
                    },
                }
            ],
        },
    }


results: list[tuple[str, bool, str]] = []


def report() -> None:
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")


# 1. Locations: a second one for the restaurant card (retail and restaurant need one each).
locs = [x for x in call("GET", "/locations").get("locations", []) if x.get("status") == "ACTIVE"]
if not locs:
    print("FAIL locations: no active location")
    sys.exit(1)
if len(locs) < 2 and not any(x.get("name") == SECOND_LOCATION for x in locs):
    r = raw("POST", "/locations", {"location": {"name": SECOND_LOCATION}})
    data = r.json() if r.content else {}
    if r.status_code < 400:
        locs.append(data["location"])
    else:
        results.append(
            ("second location", False, f"create refused: HTTP {r.status_code} {errors(data)}")
        )
if len(locs) >= 2:
    results.append(
        (
            "second location",
            True,
            f"{len(locs)} active locations; currencies {sorted({x.get('currency') for x in locs})}",
        )
    )
loc = locs[0]
currency = loc.get("currency", "USD")
expected_total = 2 * 399 + 550

# 2. Ad-hoc line items with a pickup fulfilment, then a replay with the same key.
key = str(uuid.uuid4())
body = order_body(key, loc["id"], currency)
first = call("POST", "/orders", body).get("order", {})
replay = call("POST", "/orders", body).get("order", {})
results.append(
    (
        "ad-hoc order + replay",
        bool(first.get("id")) and first.get("id") == replay.get("id"),
        f"state={first.get('state')} lines={len(first.get('line_items', []))} "
        f"same id on replay: {first.get('id') == replay.get('id')}",
    )
)

# 3. Taxes: Square's total must equal the sum of our lines.
total = first.get("total_money", {}).get("amount")
tax = first.get("total_tax_money", {}).get("amount", 0)
results.append(
    ("no taxes on ad-hoc lines", total == expected_total and not tax, f"total={total} tax={tax}")
)
calc = raw("POST", "/orders/calculate", {"order": {k: v for k, v in body["order"].items()}})
calc_data = calc.json() if calc.content else {}
results.append(
    (
        "CalculateOrder (fallback for taxes)",
        calc.status_code < 400,
        f"HTTP {calc.status_code} total={calc_data.get('order', {}).get('total_money', {})}"
        f" {errors(calc_data)}",
    )
)

# 4. Two simultaneous creates with one key must give one order.
key = str(uuid.uuid4())
body = order_body(key, loc["id"], currency)
seen: list[tuple[int, str | None]] = []


def create() -> None:
    r = raw("POST", "/orders", body)
    data = r.json() if r.content else {}
    seen.append((r.status_code, data.get("order", {}).get("id")))


threads = [threading.Thread(target=create) for _ in range(2)]
for t in threads:
    t.start()
for t in threads:
    t.join()
ids = {order_id for _, order_id in seen if order_id}
results.append(
    (
        "concurrent creates, one key",
        len(ids) == 1,
        f"statuses={sorted(s for s, _ in seen)} distinct order ids={len(ids)}",
    )
)

# 5. The same key with a different body.
changed = order_body(key, loc["id"], currency, qty=3)
r = raw("POST", "/orders", changed)
data = r.json() if r.content else {}
results.append(
    (
        "same key, different body",
        r.status_code >= 400,
        f"HTTP {r.status_code} {errors(data)}"
        + ("" if r.status_code >= 400 else f" order id {data.get('order', {}).get('id')}"),
    )
)

report()
sys.exit(0 if all(ok for _, ok, _ in results) else 1)
