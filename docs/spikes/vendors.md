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

## Run 2 (extended checks, build step 0)

To be filled in after the owner runs `square_check.py`. One row per adapters.md Open
Questions row, with the fallback it selects:

| Unknown | Result | Fallback selected |
|---|---|---|
| A second location | | |
| Ad-hoc line items, replayed with the same key | | |
| Taxes on ad-hoc lines (and `CalculateOrder`) | | |
| Two simultaneous creates with one key | | |
| The same key with a different body | | |

## Fixtures

To be filled in after the owner runs `scripts/record_square.py`: the date, the
`Square-Version`, and the list of `tests/contract/fixtures/square_*.json` files.
