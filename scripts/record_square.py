"""Record Square sandbox responses for the wire contract tests (adapters.md §11, file 30).

Run only by the owner, by hand, in their own terminal. CI and the loop never run this.

    export SQUARE_SANDBOX_TOKEN=...          # sandbox access token
    export SQUARE_LOCATION_ID=...            # optional; defaults to the first active location
    uv run python scripts/record_square.py

Writes tests/contract/fixtures/square_<case>.json. Each file holds the request (method, path,
body) and the response (status, body). The token is never written; merchant ids, location
ids and any address fields are replaced with placeholders. square_5xx.json and
square_429.json cannot be recorded on demand, so they are built from Square's documented
error shape and carry "synthetic": true.
"""

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx

BASE = "https://connect.squareupsandbox.com/v2"
VERSION = "2025-01-23"  # must equal services/mcp/square_client.py SQUARE_VERSION
OUT = Path(__file__).resolve().parent.parent / "tests" / "contract" / "fixtures"
ADDRESS_KEYS = {"address", "address_line_1", "address_line_2", "postal_code", "locality"}


def _send(token: str, method: str, path: str, body: dict[str, Any] | None) -> httpx.Response:
    return httpx.request(
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


def _drop_addresses(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("<scrubbed>" if k in ADDRESS_KEYS else _drop_addresses(v)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_drop_addresses(v) for v in value]
    return value


def _scrub(record: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
    text = json.dumps(_drop_addresses(record), indent=2, sort_keys=True)
    for real, placeholder in secrets.items():
        if real:
            text = text.replace(real, placeholder)
    return json.loads(text)


def main() -> int:
    token = os.environ.get("SQUARE_SANDBOX_TOKEN")
    if not token:
        print("Set SQUARE_SANDBOX_TOKEN first (export it in this terminal).", file=sys.stderr)
        return 1

    r = _send(token, "GET", "/locations", None)
    if r.status_code >= 400:
        print(f"listing locations failed: HTTP {r.status_code}", file=sys.stderr)
        return 1
    active = [x for x in r.json().get("locations", []) if x.get("status") == "ACTIVE"]
    wanted = os.environ.get("SQUARE_LOCATION_ID")
    loc = next((x for x in active if x["id"] == wanted), None) if wanted else active[0]
    if loc is None:
        print("SQUARE_LOCATION_ID is not an active location", file=sys.stderr)
        return 1
    currency = loc.get("currency", "USD")
    secrets = {
        token: "<token>",
        loc["id"]: "LOCATION_ID",
        loc.get("merchant_id", ""): "MERCHANT_ID",
    }

    def order(key: str, qty: int = 2, location_id: str | None = None) -> dict[str, Any]:
        return {
            "idempotency_key": key,
            "order": {
                "location_id": location_id or loc["id"],
                "reference_id": "11111111-1111-4111-8111-111111111111",
                "line_items": [
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
                ],
                "fulfillments": [
                    {
                        "type": "PICKUP",
                        "state": "PROPOSED",
                        "pickup_details": {
                            "recipient": {"display_name": "11111111-1111-4111-8111-111111111111"},
                            "schedule_type": "ASAP",
                        },
                    }
                ],
            },
        }

    key = str(uuid.uuid4())
    calc_body = {"order": order(key)["order"]}
    cases: list[tuple[str, str, str, dict[str, Any], str]] = [
        ("create_order", "POST", "/orders", order(key), token),
        ("create_order_replay", "POST", "/orders", order(key), token),
        ("key_reused_different_body", "POST", "/orders", order(key, qty=3), token),
        ("unauthorized", "POST", "/orders", order(str(uuid.uuid4())), "invalid-token"),
        (
            "bad_location",
            "POST",
            "/orders",
            order(str(uuid.uuid4()), location_id="NOTALOCATION"),
            token,
        ),
        ("calculate_order", "POST", "/orders/calculate", calc_body, token),
    ]

    OUT.mkdir(parents=True, exist_ok=True)
    for name, method, path, body, use_token in cases:
        resp = _send(use_token, method, path, body)
        record = {
            "case": name,
            "square_version": VERSION,
            "request": {"method": method, "path": path, "body": body},
            "response": {"status": resp.status_code, "body": resp.json() if resp.content else {}},
        }
        (OUT / f"square_{name}.json").write_text(
            json.dumps(_scrub(record, secrets), indent=2, sort_keys=True) + "\n"
        )
        print(f"recorded {name}: HTTP {resp.status_code}")

    for name, status, category, code in (
        ("5xx", 503, "API_ERROR", "SERVICE_UNAVAILABLE"),
        ("429", 429, "RATE_LIMIT_ERROR", "RATE_LIMITED"),
    ):
        record = {
            "case": name,
            "synthetic": True,
            "square_version": VERSION,
            "request": {"method": "POST", "path": "/orders", "body": order("<any>")},
            "response": {
                "status": status,
                "body": {"errors": [{"category": category, "code": code, "detail": "synthetic"}]},
            },
        }
        (OUT / f"square_{name}.json").write_text(
            json.dumps(_scrub(record, secrets), indent=2, sort_keys=True) + "\n"
        )
        print(f"wrote synthetic {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
