# Vendor access spike (adapters design)

*   **Design:** [docs/designs/adapters.md](../designs/adapters.md), build step 0 (#164).
*   **Scripts:** [`vendors/square_check.py`](vendors/square_check.py) (behaviour checks) and
    [`scripts/record_square.py`](../../scripts/record_square.py) (wire-test fixtures). The
    owner runs both by hand; the loop and CI never do.

## Access (2026-11-02)

| Vendor | Result |
|---|---|
| Square (sandbox) | Account created from Sri Lanka; sandbox token works. |
| Kroger | Developer site does not load from Sri Lanka. |
| DoorDash Drive | Sign-up needs a US phone number. |
| Uber Direct | Needs a store in a served country; customer id is tied to that region. |

## Run 1 (2026-11-02, first version of square_check.py)

| Check | Result |
|---|---|
| Locations | PASS: one location, `US`, `USD`, `ACTIVE` |
| Catalog item with a priced variation | PASS |
| Inventory count of 5 set and read back | PASS |
| Order with `PICKUP` fulfilment | PASS: `OPEN`, total 798 USD, fulfilment `PICKUP` / `PROPOSED` |
| Same idempotency key replayed | PASS: same order id |

## Run 2 (2026-11-02, extended checks, build step 0)

| Unknown | Result | Fallback selected |
|---|---|---|
| A second location | PASS: 2 active locations in one account, both USD (the script created "Kitchenless Kitchen (sandbox)") | None. One account; both tokens hold the same value; the cards differ by location id. |
| Ad-hoc line items, replayed with the same key | PASS: `OPEN`, 2 lines, same order id on replay | None. §7 as written. |
| Taxes on ad-hoc lines (and `CalculateOrder`) | PASS: total 1348, tax 0. `CalculateOrder` also works (HTTP 200, 1348) | None. `quote` prices from our tables. |
| Two simultaneous creates with one key | PASS: both HTTP 200, one order id | None. Same-run takeover (§9) as written. |
| The same key with a different body | No error: HTTP 200 with the **original** order (same id, original quantities and total) | Square cannot report it, so the adapter detects it: after a create, it compares the returned line quantities, notes (our SKUs) and total with the ones it sent; any difference raises `AdapterFailure("vendor_key_reused", retryable=False)`. The guard's request-hash check (`idempotency_conflict`) already stops a changed body before Square in normal use. |

Other result: an unknown `location_id` returns **403** `AUTHENTICATION_ERROR / FORBIDDEN`, not 404. That maps to `adapter_not_configured` (not retryable) under the §7 table.

## Fixtures (2026-11-02, `Square-Version` 2025-01-23)

Recorded by `scripts/record_square.py` into `tests/contract/fixtures/`:

| File | Status | Note |
|---|---|---|
| `square_create_order.json` | 200 | |
| `square_create_order_replay.json` | 200 | Same order id as the create |
| `square_key_reused_different_body.json` | 200 | Original order returned (see above) |
| `square_unauthorized.json` | 401 | |
| `square_bad_location.json` | 403 | |
| `square_calculate_order.json` | 200 | |
| `square_5xx.json` | 503 | `"synthetic": true` |
| `square_429.json` | 429 | `"synthetic": true` |

Token, merchant id, location ids and address fields are scrubbed.
