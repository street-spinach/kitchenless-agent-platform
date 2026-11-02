# Kitchenless adapters: Square for real, every other platform simulated

*   **Author:** madushankaf
*   **Status:** Proposed (2026-11-02, #146)
*   **Date:** 2026-11-02
*   **Path:** `docs/designs/adapters.md`
*   **Issue:** #146
*   **Builds on:** [architecture.md](architecture.md), [agent-protocol.md](agent-protocol.md)
    and [identity-authz.md](identity-authz.md), all approved. Where this design needs them
    to change, it lists the change in "Changes to approved designs" and does not assume it.
*   **Input:** the adapters brainstorm brief (accepted 2026-11-02) and the Square sandbox
    spike (`docs/spikes/vendors/square_check.py`, 2026-11-02).

--------------------------------------------------------------------------------

## Overview

The agents order food from other businesses' systems: grocery stores, restaurants and
couriers. Each system is reached through an adapter, a small class behind one of the
domain MCP servers (`services/mcp/`). Each adapter has a mock and possibly a real version,
and `modes.adapter_for` picks one per vendor for each run.

The approved architecture planned three real sandboxes: Square (retail and restaurant),
DoorDash Drive (courier) and Kroger (catalog only). Two of them are out of reach from the
owner's location:

*   Kroger's developer site does not load from Sri Lanka.
*   DoorDash's developer sign-up needs a US phone number.
*   Uber Direct, the obvious second courier, needs a store in a country it serves, and its
    customer id is tied to that region.
*   Square's sandbox accepted the account. The spike created a catalog item, set and read
    stock, created an order with a pickup fulfilment, and got the same order back when the
    create was replayed with the same idempotency key.

This design makes Square the one real vendor and runs every other platform as a simulation
on the existing mocks. Each simulated card carries the platform's name and a "simulated"
badge on every surface that names it. The owner adds a disclaimer to the demo. The adapter
interfaces stay vendor-neutral, so connecting a real DoorDash, Uber Direct or Kroger later
means writing one adapter class, its recorded contract tests and a few configuration lines,
with no change to agents, tools or the guard.

The design also sets the final MCP tool schemas for all five domain servers (grocery,
restaurant, delivery, household, knowledge), replacing the provisional contracts in
architecture §13 and item 43.

## The Problem

The demo has to show agents working with well-known food platforms, but only one of those
platforms will give this project an account. Four things break if the build just carries on
with the approved text:

1.  **The approved designs describe vendors we cannot connect.** Architecture §3 and §5,
    items 44 and 55, the DoorDash webhook route, the §8 status rows, CUJ 5, build step 7,
    Q6, the contract-test list, agent-protocol P27, the brief (D15, §4.7, §7), the SITEMAP
    and the card descriptions in `services/seed/data/agents.yaml` all say DoorDash and
    Kroger are real. The loop builds what the designs say. It would write a DoorDash adapter
    nobody can test and a Kroger sync that cannot run.
2.  **Two build steps are blocked for good.** Architecture build step 7 waits on "the
    adapters design, and Q6", and identity-authz step 13 (#128) waits on Q6 and step 7. Q6
    asks for DoorDash's status names and webhook signing, which only a DoorDash account can
    answer.
3.  **The mode switch would turn the simulations off.** Under the rule from #47, a vendor
    that resolves to `real` with no real adapter class raises `adapter_not_configured`. On a
    VM started with `MODE=real` to use Square, every simulated vendor would become
    unavailable, and the weekly order would fail at delivery.
4.  **A real-looking simulation can mislead.** A real retailer's name on invented prices,
    or a DoorDash label on a made-up status sequence, presents unverified vendor behaviour
    as fact. Judges and family members see the same panel and WhatsApp messages.

Building Square for real also exposes gaps the provisional contracts never had to answer:

*   No rule maps an agent card to its vendor key.
*   Square's item ids have no link to our SKUs or to the knowledge graph.
*   The wire contract has no way to say a seller cannot do an operation.
*   A failed adapter call leaves its idempotency key stuck, so the client's retry ends the
    run (#156).
*   An empty `MODE_<VENDOR>` value crashes mode resolution (#155).

## Goals

*   **G1. Square is the one real vendor.** The Square sandbox serves the retail store and
    the restaurant through real adapters. Every write uses a key derived from
    `ids.vendor_key`, so a replayed call creates nothing new. CI tests replay recorded
    Square responses with the network blocked. A manual live check, run only by the owner,
    confirms the sandbox still behaves the same way.
*   **G2. Every other platform is simulated in the open.** DoorDash Drive, Uber Direct and
    the grocery chain run on the existing mock cards and mock tables, driven by the
    existing panel events. No new agent cards are added. Their display names are
    "DoorDash Drive (simulated)", "Uber Direct (simulated)" and a fictional grocery name
    whose description says its catalog is modelled on Kroger.
*   **G3. A vendor with no real adapter can never resolve to `real`.** One per-vendor list in
    code declares which vendors have a real adapter class and which credential names it
    needs. `VENDORS`, `ALWAYS_MOCK`, the Settings fields and the panel's vendor list are
    derived from it. The panel refuses `real` for a vendor without a real class.
*   **G4. The badge tells the truth for the run in front of you.** The "real" or
    "simulated" badge is computed from the run's pinned modes (`runs.modes`), not the
    current toggle, and appears on every surface that names a vendor: the panel, A2A cards,
    card descriptions and WhatsApp text. One approved disclaimer string is used everywhere.
*   **G5. The final tool schemas extend item 43 and never break it.** Each final tool
    accepts every argument set the provisional tool accepts. New fields are optional with
    defaults, and a test checks this for every tool. Code built against item 43 before this
    design lands keeps working.
*   **G6. "Cannot do this" is a clean refusal.** An operation a seller's adapter does not
    support returns `kind=refused, reason=unsupported, retryable=false`. The guard checks it
    before claiming the idempotency key, and the refusal is traced.
*   **G7. Acceptance.**
    *   With `MODE=real` and Square credentials set, the weekly order completes, with Square
        for real and every other vendor simulated.
    *   A run pinned before a deploy that adds a vendor key still completes.
    *   The Square replay tests pass offline.
    *   Every existing eval case passes unchanged.
    *   Architecture build step 7 and identity-authz step 13 have no blocker that waits on a
        vendor account.

## Non-Goals

*   **Vendor-shaped fakes.** There are no fake HTTP servers imitating DoorDash, Uber or
    Kroger, and simulated vendors store no vendor-style ids or status names. We cannot read
    those vendors' current APIs, so any imitation would be a guess presented as their
    behaviour. Stored ids stay UUIDs and stored delivery states stay the four canonical
    ones.
*   **Real DoorDash, Uber Direct or Kroger adapters,** the Kroger catalog sync (item 55),
    the DoorDash webhook handler and an answer to Q6. All move to a future "connect a real
    vendor" step. The `/webhooks/doordash/<secret>` route stays reserved and returns 404.
*   **Instacart.** No journey, demo step, goal or eval uses a shopping-list handoff, and it
    would send diet and allergy data off the platform. It moves to Future Improvements.
*   **New agent cards per brand.** Each card costs a Keycloak client, OpenFGA tuples,
    organization and client rows, business rules, re-baselined evals and about 1.5k model
    tokens per run. The 14-agent roster stays, and only display names and descriptions
    change. Agent ids stay, because eval cases and Keycloak clients use them.
*   **A plugin system or capability discovery.** Adding a vendor is a written checklist, not
    a runtime mechanism.
*   **Square OAuth** ("connect your POS"). Square tokens stay in the environment.
*   **Payments.** A placed Square order is an unpaid `OPEN` order. Square Payments, Toast
    and Clover are out of scope.
*   **Live vendor calls in CI or the loop.** Tests replay recordings, and only the owner runs
    the recorder and the live check. This keeps the CLAUDE.md rule that tests never switch
    to real integrations.

## Open Questions

*   **Square sandbox details not yet checked.** The spike covered one location and one
    account. A second location (retail and restaurant need one each),
    `CalculateOrder`, `RetrieveOrder`, `UpdateOrder` on an `OPEN` order, Square's response
    to the same key with a different body, and location taxes are still unchecked. The
    extended spike (build step 0) answers them. The Technical Plan fixes a fallback for
    each, so no build step waits on the answers.
*   **The fictional grocery name.** The Technical Plan proposes one. The owner may change
    the string before the seed lands. Only display text depends on it.

## Critical User Journeys (CUJs)

### CUJ A1: Weekly order with Square for real

*   **Given**: the VM runs with `MODE=real`, `SQUARE_RETAIL_TOKEN` is set, and the Square
    sandbox location for `grocery:square-us` has the seeded catalog.
*   **When**: the household agent runs the weekly grocery order and picks the Square store.
*   **Then**: Square's sandbox holds one `OPEN` order with the authorised lines and total.
    The courier step runs on the simulated DoorDash Drive card. The panel shows "Square
    (sandbox)" and "DoorDash Drive (simulated)" for this run. Replaying `place_order` with
    the same key returns the same Square order id.

### CUJ A2: A simulated vendor under `MODE=real`

*   **Given**: `MODE=real`, with no real DoorDash, Uber Direct or Kroger adapter.
*   **When**: a run starts.
*   **Then**: `runs.modes` has `doordash`, `kroger_catalog` and `kroger_ordering` as
    `mock`, and the run completes. `PUT /panel/modes/doordash` with `real` returns 409
    `no_real_adapter`.

### CUJ A3: Square has a 5xx during `place_order`

*   **Given**: a run with `square_retail=real`.
*   **When**: Square returns a 503 to the create-order call.
*   **Then**: the adapter raises `AdapterFailure("adapter_5xx", retryable=True)`. The
    client retries once with the same key. The guard lets the retry take over the key,
    because the key was claimed by the same run, and calls Square again with the same
    vendor key. Square either creates the order or returns the one it already made. No run
    ends with `idempotency_conflict` (#156).

### CUJ A4: A seller that cannot amend

*   **Given**: the Square adapter declares that it does not support `amend_order` (the
    fallback if the spike shows `UpdateOrder` won't work), and a shortfall is opened on a
    Square order.
*   **When**: the seller agent calls `amend_order`.
*   **Then**: the call returns `refused / unsupported / retryable=false` before any
    idempotency key is claimed, and a `tool_result` row with `refused=true` is written.
    The panel would not have offered a shortfall event on that card in the first place,
    because panel events are refused on cards whose next-run mode is `real` (§8).

## Technical Plan (Architecture)

### 1. Changes to approved designs

Each change below is written into the named document as a dated amendment
("Amended 2026-11-0x (adapters, #146)") in the same PR that lands this design. Nothing else
in those documents changes.

| Document | Section | Today | After |
|---|---|---|---|
| architecture | §3 roster, `agents.yaml` | Kroger: real catalog; DoorDash: real sandbox | Agent ids unchanged. Display names: "Hillcrest Market (simulated)" for `grocery:kroger-us` and "DoorDash Drive (simulated)" for `delivery:doordash-us`. `delivery:mock-us` is shown as "Uber Direct (simulated)". Descriptions rewritten (§6). |
| architecture | §5 adapter table | Kroger real catalog; DoorDash real sandbox | Square retail and restaurant real (sandbox). Every other seller and courier is mock. |
| architecture | §5 mode resolution | `kroger_ordering` is always mock | Vendors with no real adapter class are always mock (§3 below). |
| architecture | §5 guard step 4 | Takeover only after 60 s | Also take over a same-hash key `in_progress` that the same run claimed (§9 below). |
| architecture | §8 courier status table | DoorDash names listed, "to be verified, Q6" | Mock names only. The DoorDash column moves to Future Improvements. |
| architecture | §9 failure matrix | Adapter timeout 15 s | Adapter HTTP timeout 10 s total, 3 s to connect. The client timeout stays 15 s. |
| architecture | Q6 | Open; blocks step 7 | Restated: "closes when the first real courier key exists." Blocks nothing. |
| architecture | build step 7 | Real Square, DoorDash and Kroger; blocked by Q6 | Square adapters, the per-vendor list, display names and badges. Blocked by this design only. |
| architecture | items 44, 55 | `KrogerCatalogAdapter`, `DoorDashDriveAdapter`, Kroger catalog sync | Removed from the build. Listed under "connect a real vendor" (§13 below). |
| architecture | `/webhooks/doordash/<secret>` | DoorDash status webhook | Reserved, returns 404 until a real courier exists. |
| architecture | §13 | MCP schemas provisional | Final schemas are in §11 below. |
| architecture | CUJ 5, Alternative V | DoorDash real; Uber rejected as a second real courier | CUJ 5 uses the Square card for the vendor failure. Alternative V stands: Uber Direct is simulated on the existing mock courier, not a new card. |
| agent-protocol | P27 | DoorDash real | Simulated. |
| identity-authz | build step 13 | Blocked by "Q6 answered; architecture build step 7" | Blocked by "architecture build step 7". |
| brief | D15, §4.7, §7 | Real DoorDash and Kroger | Replaced by a pointer to this design. |
| SITEMAP | adapters row | Real DoorDash and Kroger | This design. |

### 2. Components

```text
            agents (seller, courier)
                     │ MCP tool call (run_id, agent_id, token)
                     ▼
  services/mcp/<area>/server.py ──► services/safety/guard.py (steps 0-7)
                     │
       card.vendor + per-server vendor_keys map
                     │
                     ▼
  services/common/modes.py  adapter_for(vendor_key, runs.modes)
                     │            ▲
                     │            └── services/common/vendors.py (VENDOR_SPECS: one row per key)
                     ▼
     ┌───────────────┴────────────────────┐
     │ real                               │ mock
     ▼                                    ▼
  SquareRetailAdapter            MockStoreAdapter / MockRestaurantAdapter /
  SquareRestaurantAdapter        MockCourierAdapter  (services/mocks/*, mock_* tables)
     │ httpx, Square-Version pinned
     ▼
  connect.squareupsandbox.com/v2

  api: /panel/*, WhatsApp text, A2A cards ──► services/common/vendors.py vendor_label(card, run_modes)
```

### 3. One per-vendor list in code

`services/common/vendors.py` holds `VENDOR_SPECS`, one entry per vendor key:

| Key | Real adapter class | Credential settings | Notes |
|---|---|---|---|
| `square_retail` | `services.mcp.grocery.adapters.square:SquareRetailAdapter` | `square_retail_token` | |
| `square_restaurant` | `services.mcp.restaurant.adapters.square:SquareRestaurantAdapter` | `square_restaurant_token` | |
| `kroger_catalog` | none | — | Simulated ("Hillcrest Market") |
| `kroger_ordering` | none | — | Kept so runs pinned before this design still parse. No tool uses it after §5. |
| `doordash` | none | — | Simulated ("DoorDash Drive") |

*   `modes.VENDORS` is the set of keys, and `modes.ALWAYS_MOCK` is the keys with no real
    class. `_ADAPTERS` real entries come from the list. The mock entries stay in
    `modes.py`.
*   A vendor in `ALWAYS_MOCK` resolves to `mock` before the panel row, `MODE_<VENDOR>` or
    `MODE` is read. That fixes the `MODE=real` problem.
*   A test checks that every key has a `mode_<key>` Settings field, a Compose entry and an
    `.env.example` line, so one PR always adds all four.
*   The list holds credential **names** only. `api` (which serves the panel) reads the list
    to know whether a real class exists. It does not know whether `mcp` has the credentials.
    If a real Square adapter is chosen and its token is missing in `mcp`, the adapter raises
    `AdapterFailure("adapter_not_configured", retryable=False)` on the first call. That
    fails closed and shows up in the trace.
*   Square's `Square-Version` is one constant in `vendors.py`, set to the version the
    recordings were made with (`2025-01-23` today).

### 4. Mode resolution changes (`services/common/modes.py`)

*   **Missing key resolves to `mock`.** `adapter_for` treats a key missing from
    `runs.modes` as `mock`, not as `adapter_not_configured`. A run pinned before a deploy
    that adds a key keeps working, with the new vendor simulated. A key missing from
    `VENDORS` still raises.
*   **Empty value is unset.** An empty or blank `MODE_<VENDOR>` falls through to `MODE`
    (#155).
*   **Eval runs are pinned to mock.** `runs.start()` pins every vendor to `mock` for eval
    households (`households.env = 'eval'`), whatever the panel or environment says. Mock tables
    are split by `env`; the Square sandbox is not, so evals must never write to it. This
    amends #36's run start.
*   **Storage stays `real` / `mock`.** "Simulated" is a display label only (§6). The
    `adapter_modes` CHECK does not change.

### 5. From card to vendor key

The mapping already exists in code: each seller server passes `vendor_keys`, a map from a
card's `vendor` to the vendor key of each tool, and the delivery server has
`_VENDOR_KEYS`. A card with `vendor: mock` always gets the area's mock. This design keeps
that mechanism and adds two rules:

*   **One key per card.** Every tool of a card maps to the same vendor key. The `kroger`
    entry in the grocery server changes from the catalog/ordering split to
    `kroger_catalog` for all tools, including `amend_order`. A unit test checks the rule
    for every server's map.
*   **The seed fails** on a business card whose `vendor` has no entry in its server's map
    (other than `mock`).

`agent_cards.vendor` values do not change (`kroger`, `square`, `doordash`, `mock`).

### 6. Names, badges and the disclaimer

*   **Display names.** `organizations.name` and the card description carry the label:

    | Card | Organization name | Description |
    |---|---|---|
    | `grocery:kroger-us` | Hillcrest Market (simulated) | Simulated grocery store. Catalog modelled on Kroger's public product data. Not affiliated with Kroger. |
    | `grocery:square-us` | Square Sandbox Grocery | Grocery store on the Square sandbox (simulated when the platform runs in mock mode). |
    | `restaurant:square-us` | Square Sandbox Kitchen | Restaurant on the Square sandbox (simulated when the platform runs in mock mode). |
    | `delivery:doordash-us` | DoorDash Drive (simulated) | Simulated courier. Shows where a DoorDash Drive connection plugs in. Not affiliated with DoorDash. |
    | `delivery:mock-us` | Uber Direct (simulated) | Simulated courier. Shows where an Uber Direct connection plugs in. Not affiliated with Uber. |
    | `grocery:mock-us`, `restaurant:mock-us` | unchanged | unchanged |

    `org_id` values (`kroger-us`, `doordash-us`, `mock-courier-us`) do not change. They are
    ids, never shown.
*   **The badge.** `vendors.vendor_label(card, run_modes) -> {name, badge}` where `badge`
    is `sandbox` when the card's key is `real` in the run's pinned modes and a real class
    exists, and `simulated` otherwise. The panel, WhatsApp order text and the run view call
    it with `runs.modes`. The panel's mode toggle shows the next-run mode separately
    ("next run: real").
*   **Static surfaces** (A2A cards, directory) cannot know a run's modes, so they use the
    descriptions above.
*   **The disclaimer** is one constant, `vendors.DISCLAIMER`: "Square orders go to Square's
    sandbox. Hillcrest Market, DoorDash Drive and Uber Direct are simulated by Kitchenless.
    No real orders, payments or deliveries are made." The panel footer and the A2A demo page
    show it. The owner may reword it; there is only one copy.
*   **Mock delays.** Simulated vendors keep the mock's current timing. Any added display
    delay is deterministic and at most 2 s, inside the 10 s adapter timeout, the 30 s quote
    round and the 5 s business-model cap. Eval runs keep the short step time.

### 7. The Square adapters

`services/mcp/grocery/adapters/square.py` and `services/mcp/restaurant/adapters/square.py`
share a client in `services/mcp/square_client.py` (raw `httpx.AsyncClient`, no Square SDK,
because of the `mcp` container's 320 MB cap).

*   **Configuration.** Base URL `https://connect.squareupsandbox.com/v2`. Bearer token from
    `square_retail_token` or `square_restaurant_token`. `location_id` from
    `agent_cards.config.square_location_id`, written by the seed from
    `SQUARE_RETAIL_LOCATION_ID` and `SQUARE_RESTAURANT_LOCATION_ID`. `SQUARE_ACCESS_TOKEN`
    in `.env.example` is replaced by the two token names (it is unused today).
*   **Accounts.** If one sandbox account can hold two locations (spike step 0), both tokens
    hold the same value and the cards differ by location. If not, they are two sandbox
    accounts. The code is the same either way.
*   **Catalog and SKUs.** Our SKU stays the platform id. The seed upserts one Square
    `ITEM` with one `ITEM_VARIATION` per seller SKU, with the variation's `sku` field set
    to ours, using a stable client id (`#<sku>`) and `vendor_key("seed:" + agent_id + ":"
    + sku)` as the idempotency key, so re-seeding does not duplicate. The returned
    variation id is stored in a new nullable column `catalog_items.vendor_object_id`
    (additive migration). The seed fails if any seller SKU, real or simulated, has no
    product node in Neo4j (otherwise `check_allergens` fails closed on every spend).
*   **`search_items`** reads `catalog_items` in Postgres for both real and mock, because
    Square has no allergen or diet codes. Stock for real cards comes from Square's
    inventory counts (`/inventory/counts/batch-retrieve`), so `available_qty` is Square's.
*   **`quote`** prices lines from `catalog_items.price_minor`. Square locations have no
    taxes configured; the seed checks that and fails if a tax exists. If the spike shows
    that taxes can't be turned off, `quote` calls `CalculateOrder` instead and returns its
    total.
*   **`place_order`** sends `POST /orders` with `idempotency_key =
    vendor_key(guard_key)`, the location, line items by `catalog_object_id` and integer
    quantities as strings, and `reference_id = order_id`. No fulfilment recipient name,
    phone or address is sent; the fulfilment is `PICKUP`, with the recipient display name
    set to the order id. The result maps `total_money.amount` to `total_minor` and checks
    the currency. If the total differs from the authorised total, the adapter raises
    `AdapterFailure("total_mismatch", retryable=False)` and the order stays as an
    unpaid `OPEN` order in the sandbox. "Placed" means an unpaid `OPEN` order; payments are
    out of scope.
*   **`amend_order`** calls `UpdateOrder` with the order's `version` and
    `vendor_key(guard_key, part="update")`. If the spike shows updates fail on an `OPEN`
    order, the Square class declares `amend_order` unsupported (§8) and nothing else
    changes.
*   **Several calls per tool.** `ids.vendor_key(key, part=None)` gains an optional `part`
    (additive; `part=None` gives today's value). Each Square call inside one tool uses its
    own part. Every request body is a pure function of the guarded request, so a retry sends
    the same body.
*   **Error mapping** (all `AdapterFailure`):

    | Square response | `reason` | `retryable` |
    |---|---|---|
    | timeout or connection error | `adapter_timeout` | true |
    | 5xx | `adapter_5xx` | true |
    | 429 | `adapter_rate_limited` | true |
    | 401 / 403 | `adapter_not_configured` | false |
    | 400 `IDEMPOTENCY_KEY_REUSED` | `idempotency_conflict` | false |
    | 409 `VERSION_MISMATCH` (amend) | `adapter_conflict` | false |
    | other 4xx | `adapter_4xx` | false |
    | currency differs from the card | `currency_mismatch` | false |

### 8. Operations a seller cannot do

*   Each adapter class declares `OPERATIONS: frozenset[str]`, the seller tools it supports.
    The mock classes declare all four. A unit test checks that every class either lists a
    method in `OPERATIONS` or does not define it.
*   The seller tool checks the operation in its `limits` function. Limits run at guard step
    3, before the key claim at step 4. An unsupported operation raises
    `GuardRefused("unsupported")`, which reaches the client as `kind=refused,
    reason=unsupported, retryable=false` and writes a `tool_result` row with
    `refused=true`. No guard code changes.
*   `unsupported` is added to the refusal reason tables in agent-protocol (the seller
    agent's reaction is the same as for any refusal of that tool).
*   **Panel events on real cards are refused.** `POST /panel/events` returns 409
    `real_vendor` when the target card's next-run mode is `real`. Panel events only drive
    mocks, and before this they did nothing on a real card, silently.

### 9. Retrying after an adapter failure (#156)

Guard step 4 gains one rule: a key with the same request hash that is `in_progress` and was
claimed by **the same run** is taken over, whatever its age. A different run's
`in_progress` key under 60 s still returns `idempotency_conflict`. This is safe because the
retry sends the same vendor key, and Square and the mocks deduplicate on it. The adapter
HTTP timeout drops to 10 s total (3 s to connect), below the client's 15 s, so the first
attempt is finished before the retry starts.

The same rule lets two calls from one run that are in flight together both reach the
vendor (the vendor still makes one order). So step 7 becomes "first complete wins": the
store only succeeds while the key is `in_progress`; a later completer gets the stored
result back, skips the state change and returns that result. The owner chose this over
releasing the key on failure (2026-11-02).

### 10. Couriers: quotes and status

*   **Quotes stay zone-level.** `quote_delivery(order_id, zone_id, window)` keeps its
    arguments. The address reaches a courier only at `book_delivery` (G5 unchanged).
*   **Price check at booking.** A future real courier adapter re-prices with the address
    inside `book_delivery`. If that price exceeds the quote by more than
    `COURIER_PRICE_TOLERANCE_MINOR` (default 200), it raises
    `AdapterFailure("price_changed", retryable=False)` without booking. The mock never
    changes its price, so this rule is defined now and tested only with a fake real
    adapter in unit tests.
*   **Status by polling.** A real courier's status is read through `get_delivery_status`
    in the existing progression loop. A webhook is optional and stays reserved (404).

### 11. Final tool schemas

The tools, names and arguments in architecture item 43, with the #47 and #48 amendments,
are final. This design adds no required argument and removes nothing. The additions are:

*   the refusal reason `unsupported` (any seller tool);
*   the failure reasons in §7's table and `price_changed`;
*   optional result fields `vendor` and `badge` on `place_order` and `book_delivery`
    results, for the run view.

`tests/unit/test_tool_schemas.py` checks, for every tool on all five servers, that each
argument in item 43 is still accepted with the same type, and that every new argument (none
today) is optional. The household and knowledge servers have no adapters with modes and are
unchanged.

### 12. Contract tests

*   **Interface contract** (`tests/contract/test_seller_contract.py`,
    `test_courier_contract.py`): every adapter class, mock included, passes the same cases
    for results, idempotent replay and declared operations.
*   **Square wire contract** (`tests/contract/test_square_wire.py`): replays recorded
    Square responses from `tests/contract/fixtures/square_*.json` through
    `httpx.MockTransport`, with the network blocked. Recordings are scrubbed of tokens,
    merchant ids, location ids and addresses.
*   **The recorder** (`scripts/record_square.py`) is run only by the owner, by hand, with
    the token exported in their own terminal. The loop never runs it. CI never calls Square.
    This keeps "tests never switch to real integrations".
*   Only Square has recordings. Hand-written "recordings" for vendors we cannot reach would
    be fabricated evidence.
*   A contract test checks that no recorded request contains household fields (names,
    phones, addresses).

### 13. Observability

*   `tool_call`, `tool_result` and `error` trace rows gain `vendor`, `mode` and `adapter`
    (class name). `error` rows also gain the HTTP status, the vendor error code, the
    latency and the `mock_events` id that caused the failure, if any. These are payload
    fields; no migration.
*   Mode toggles and panel events are not trace rows: trace rows belong to a run, and
    `trace.KINDS` is fixed. They are already recorded in `adapter_modes.set_by` /
    `set_at` and in `mock_events`, and each run's `run_start` row records its pinned
    `modes`.

### 14. Connecting a real vendor later (checklist)

Worked example: Uber Direct.

1.  Add an `uber_direct` row to `VENDOR_SPECS` with its class path and credential names.
2.  Add the `mode_uber_direct` Settings field, the Compose entry and the `.env.example`
    lines (the §3 test fails until all four exist).
3.  Write `services/mcp/delivery/adapters/uber_direct.py`, passing the courier interface
    contract, with recorded wire tests.
4.  Change `delivery:mock-us` to `vendor: uber_direct` in `agents.yaml`, add it to the
    delivery server's map, and update its description.
5.  Run the recorder and the live check by hand.

No agent, tool, guard or eval change is needed. Kroger and DoorDash follow the same steps;
DoorDash also needs Q6 answered if its webhook is used.

## Alternatives Considered

### Alternative AD1: Keep DoorDash and Kroger real, as the architecture says

*   **Pros**: No amendments. The demo would show three real platforms.
*   **Cons**: Kroger's developer site does not load from Sri Lanka, and DoorDash's sign-up
    needs a US phone number. Build step 7 and identity step 13 stay blocked on Q6 with no
    way to answer it.
*   **Reason for Rejection**: It can't be built from here. The adapters would be untestable
    code, and two build steps would wait for good.

### Alternative AD2: Uber Direct as the real courier

*   **Pros**: A second big platform, and an alternative to DoorDash.
*   **Cons**: Uber Direct needs a store in a country it serves, and the customer id is tied
    to that region. Alternative V had already rejected it as a second courier.
*   **Reason for Rejection**: Same access problem as DoorDash. Uber Direct is simulated on the
    existing mock courier card instead.

### Alternative AD3: Vendor-shaped fake servers for each simulated platform

*   **Pros**: Simulated vendors would speak something like the real wire format, and the
    "real" adapter code would run against them.
*   **Cons**: We cannot read those vendors' current APIs, so the fakes would be guesses
    presented as vendor behaviour. Each fake is extra code to build and keep. Vendor-style
    ids and status names in storage would break `uuid5(UUID(external_delivery_id), …)`
    and the four-state CHECK on `mock_deliveries`, and changing a CHECK is not additive.
*   **Reason for Rejection**: The owner chose the level-1 approach: honest connection points
    on the existing mocks, labelled "simulated".

### Alternative AD4: New agent cards per brand

*   **Pros**: Each platform gets its own card next to the generic mocks.
*   **Cons**: Each card needs a Keycloak client, OpenFGA tuples, `organizations` and
    `agent_clients` rows, business rules, re-baselined evals and about 1.5k model tokens per
    run per courier.
*   **Reason for Rejection**: Relabelling the existing cards gives the same demo at no cost.
    Agent ids stay because eval cases and Keycloak clients use them.

### Alternative AD5: Real retailer names on simulated stores

*   **Pros**: "Kroger (simulated)" is recognisable to judges.
*   **Cons**: A real retailer's name on invented prices and stock is the highest-risk use of a
    brand. A badge can be cropped out of a screenshot.
*   **Reason for Rejection**: The grocery store gets a fictional name, with "catalog modelled
    on Kroger" in its description. Platform names (DoorDash Drive, Uber Direct) are kept with
    the badge, because they name a connection type, not a shop's prices.

### Alternative AD6: A `simulated` mode value in storage

*   **Pros**: Storage would match the label.
*   **Cons**: `adapter_modes` and the mode checks accept `real` and `mock` only. A new value
    means replacing a CHECK, which is not additive, and touching every mode check.
*   **Reason for Rejection**: Storage stays `real` / `mock`. "Simulated" is only a label.

### Alternative AD7: A Postgres table of vendors and credentials

*   **Pros**: The panel could list vendors from the database, and `api` could see
    availability without importing code.
*   **Cons**: Storing credentials would fall under identity-authz's encryption rules. The
    table would be one more place where vendors are listed, next to `VENDORS`, `ALWAYS_MOCK`,
    Settings and `agent_cards`.
*   **Reason for Rejection**: One list in code (`VENDOR_SPECS`) that holds credential names
    only, from which the others are derived.

### Alternative AD8: A `vendor_key` field in `agent_cards.config` (the brief's proposal)

*   **Pros**: The mapping would sit next to the card.
*   **Cons**: The servers already map a card's `vendor` to a vendor key per tool
    (`vendor_keys`, `_VENDOR_KEYS`). A second mapping in the database could disagree with
    the code.
*   **Reason for Rejection**: Keep the existing maps, require one key per card, and make the
    seed fail on an unmapped vendor.

### Alternative AD9: Supported operations per vendor

*   **Pros**: One table answers "can this vendor amend?".
*   **Cons**: The mock and the real class of one vendor can differ: the mock amends, and real
    Square may not.
*   **Reason for Rejection**: Each adapter class declares `OPERATIONS`.

### Alternative AD10: Raise `unsupported` inside the adapter

*   **Pros**: No change to the tool's limits.
*   **Cons**: The adapter runs at guard step 6, after the key claim. It would raise an
    `AdapterFailure`, which reaches the client as `unavailable`. The client would retry and
    waste a call, the refusal would not be traced as a refusal, and the key would stay
    `in_progress`.
*   **Reason for Rejection**: Check in `limits` (step 3) and refuse with `unsupported`.

### Alternative AD11: Separate catalog and ordering modes per card

*   **Pros**: It supported "real Kroger catalog, mock ordering".
*   **Cons**: With Kroger simulated, it only allows a real Square catalog to meet mock
    ordering, which fails in confusing ways.
*   **Reason for Rejection**: One vendor key per card.

### Alternative AD12: A courier webhook for status

*   **Pros**: Faster status updates.
*   **Cons**: It needs Q6 (signing and status names) answered, which needs a DoorDash account.
*   **Reason for Rejection**: Poll in the existing progression loop. The webhook route stays
    reserved.

### Alternative AD13: A guarded `address_ref` on the courier quote

*   **Pros**: A real courier could quote with the exact address.
*   **Cons**: It would share personal data before a courier is chosen, against G5, and needs
    a new data-share scope.
*   **Reason for Rejection**: Zone-level quotes, with a price check at booking that refuses
    if the price rises by more than `COURIER_PRICE_TOLERANCE_MINOR`.

### Alternative AD14: Live Square calls in CI

*   **Pros**: It catches sandbox drift on every PR.
*   **Cons**: It puts a secret in CI, makes CI depend on Square's uptime, and breaks the
    rule that tests never switch to real integrations.
*   **Reason for Rejection**: Recorded replays in CI; a manual recorder and live check run
    by the owner.

### Alternative AD15: The Square Python SDK

*   **Pros**: Typed models and less request code.
*   **Cons**: Extra memory in the `mcp` container (320 MB cap), and the SDK pins its own API
    version.
*   **Reason for Rejection**: Raw `httpx` with the `Square-Version` header pinned in one
    constant.

### Alternative AD16: A fault-injection wrapper for panel events on real cards

*   **Pros**: Judges could trigger "out of stock" on the real Square card.
*   **Cons**: It would show faked failures as the real vendor's behaviour. It is also more
    code.
*   **Reason for Rejection**: Refuse panel events on cards whose next-run mode is `real`.

### Alternative AD17: Instacart as a simulated shopping-list handoff

*   **Pros**: One more recognisable platform.
*   **Cons**: No journey, demo step, goal or eval uses it. It would send diet and allergy
    data off the platform, needing a guarded data share and scope. A simulated link sent to
    real WhatsApp phones would look like a fake instacart.com link.
*   **Reason for Rejection**: Deferred to Future Improvements.

### Alternative AD18: Keep the 60-second rule for retries (#156)

*   **Pros**: No change to guard step 4.
*   **Cons**: Every Square 5xx or timeout and every `adapter_down` panel event ends the run
    with `idempotency_conflict`, because the retry arrives within 60 s.
*   **Reason for Rejection**: Same-run takeover, with the adapter timeout below the client's.

## Non-Functional Requirements

*   **Security and privacy.** Square tokens live only in the `mcp` environment, as
    `SQUARE_RETAIL_TOKEN` and `SQUARE_RESTAURANT_TOKEN`. They are never logged or traced.
    No household name, phone or address is sent to Square (§7), and a contract test checks
    the recordings for that. Recordings are scrubbed before commit.
*   **Memory.** No SDK. The Square client is one shared `httpx.AsyncClient` per process.
*   **Rollout and rollback.** Every schema change is additive: one nullable column. Each
    build step is its own PR. Rolling back a step means reverting its PR; `MODE=mock` (the
    default) keeps every path on the mocks. The vendor keys and `adapter_modes` values do
    not change, so old runs and old panel rows still parse.

## Future Improvements

*   **Connect a real courier.** DoorDash Drive or Uber Direct once an account in a served
    region exists, following the §14 checklist. Q6 (DoorDash status names and webhook
    signing) closes then. The DoorDash status column moves back into architecture §8.
*   **A real grocery catalog.** The Kroger catalog sync (architecture item 55) if the
    developer site becomes reachable.
*   **Instacart shopping-list handoff,** through Instacart's own MCP server, as a guarded
    data share with its own scope row.
*   **Square OAuth** so a restaurant can connect its own POS.

## Detailed Implementation Plan

### Build steps (one PR each; C = critical path, needs human review)

| Step | Work | Files (below) | Blocked by | C |
|---|---|---|---|---|
| 0 | Extended Square spike, run by the owner | 1 | — | — |
| 1 | This design and the amendments | 2–6 | — | C |
| 2 | Per-vendor list and mode changes | 7–10, 30 | step 1; #155 | — |
| 3 | Guard: same-run takeover, first complete wins, timeouts | 11–13 | step 1 | C |
| 4 | `unsupported` and `OPERATIONS` | 14–16 | step 2 | — |
| 5 | Names, badges, disclaimer, panel checks | 17–20 | step 2 | — |
| 6 | Seed and migration: one key per card, `vendor_object_id`, graph check, Square catalog push | 21–25 | steps 2, 5 | C |
| 7 | Square adapters, wire contract, recorder | 26–29, 31–33 | steps 0, 3, 4, 6 | C (scripts/) |
| 8 | Trace fields | 34 | step 7 | — |

Step 7 here is architecture build step 7 as narrowed by §1. When it merges,
identity-authz step 13 (#128) is unblocked. #156 is implemented by step 3; #155 is
already ready for the loop.

### Target Files to Create/Modify

#### 1. `docs/spikes/vendors/square_check.py` and `docs/spikes/vendors.md` (Modify, Create)

*   **Type / Scope**: Spike script and record.
*   **Proposed Changes**: Add checks for a second location (or a second account),
    `CalculateOrder`, `RetrieveOrder`, `UpdateOrder` on an `OPEN` order, the same
    idempotency key with a different body, and location taxes. `vendors.md` records the
    first run's results (2026-11-02), the extended results, and which §7 fallback each
    answer selects. The script still prints ids, states and amounts only.

#### 2. `docs/designs/adapters.md` (Create)

*   **Type / Scope**: This design.
*   **Proposed Changes**: The final text of this draft.

#### 3. `docs/designs/architecture.md` (Modify)

*   **Type / Scope**: Dated amendments.
*   **Proposed Changes**: Every architecture row of §1's table, each as an "Amended
    2026-11-0x (adapters, #146)" note next to the text it changes. §1 "Brief decisions
    this design supersedes" gains D15, §4.7 and §7 of the brief (the brief is not in the
    repository, so the supersession is recorded here). Q6 is restated. Build step 7 is
    narrowed. Items 44 and 55 are marked "not built; see adapters.md §14".

#### 4. `docs/designs/agent-protocol.md` (Modify)

*   **Type / Scope**: Dated amendments.
*   **Proposed Changes**: P27 says DoorDash is simulated. The refusal reason tables gain
    `unsupported` (seller tools; the agent treats it like any refusal of that tool).

#### 5. `docs/designs/identity-authz.md` (Modify)

*   **Type / Scope**: Dated amendment.
*   **Proposed Changes**: Build step 13's blocker becomes "architecture build step 7".

#### 6. `docs/designs/SITEMAP.md` (Modify)

*   **Type / Scope**: Registry.
*   **Proposed Changes**: Adapters row: link, "Approved", date, author, components
    (`services/common/vendors.py`, `services/mcp/*/adapters/square.py`,
    `services/common/modes.py`, the guard step 4 change), description. The architecture,
    agent-protocol and identity rows get the amendment date.

#### 7. `services/common/vendors.py` (Create)

*   **Type / Scope**: Python module.
*   **Proposed Changes**:
    *   `VendorSpec` (frozen dataclass): `key`, `real_adapter: str | None` (`module:Class`),
        `credentials: tuple[str, ...]` (Settings field names).
    *   `VENDOR_SPECS: tuple[VendorSpec, ...]` with the five rows in §3.
    *   `SQUARE_VERSION = "2025-01-23"`, `DISCLAIMER` (§6 text).
    *   `has_real_adapter(key) -> bool`.
    *   `vendor_label(card, run_modes) -> VendorLabel{name, badge}` with `badge` in
        `{"sandbox", "simulated"}`: `sandbox` only if the card's vendor key is `real` in
        `run_modes` and `has_real_adapter` is true. A `mock` card is always `simulated`.
        `name` is `organizations.name`, read by the caller and passed in on the card.
    *   `card_vendor_key(area, card) -> str | None`, which reads each server's map (moved
        here from the servers so `api` can use it without importing MCP code).

#### 8. `services/common/modes.py` (Modify)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: `VENDORS` and `ALWAYS_MOCK` are built from `VENDOR_SPECS`
    (`ALWAYS_MOCK` = keys with `real_adapter is None`). Real entries of `_ADAPTERS` come
    from the specs. `adapter_for` treats a key in `VENDORS` but missing from `run_modes`
    as `mock`. `resolve_modes` treats an empty or blank `MODE_<VENDOR>` as unset (if #155
    has not already landed). New `pin_for_env(modes, env) -> modes`: returns all-`mock`
    for `env='eval'`.

#### 9. `services/common/runs.py` (Modify)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: `start()` passes the resolved modes through
    `modes.pin_for_env(…, household.env)` before storing `runs.modes`.

#### 10. `tests/unit/test_modes.py`, `tests/unit/test_vendors.py` (Modify, Create)

*   **Type / Scope**: Unit tests.
*   **Proposed Changes**:
    *   `test_always_mock_ignores_mode_real`: `MODE=real` resolves `doordash`,
        `kroger_catalog` and `kroger_ordering` to `mock`, and `square_*` to `real`.
    *   `test_always_mock_ignores_panel_real`: an `adapter_modes` row `doordash=real` is
        ignored.
    *   `test_missing_key_in_run_modes_is_mock`.
    *   `test_eval_household_pins_mock`: `MODE=real`, eval household → all `mock`.
    *   `test_every_vendor_has_setting_compose_and_env_example`: for every key, a
        `mode_<key>` Settings field exists, and `infra/docker-compose.yml` and
        `.env.example` contain `MODE_<KEY>` (read as text; `.env.example` only).
    *   `test_vendor_label`: one case per badge, with `real` mode but no real class →
        `simulated`.

#### 11. `services/safety/idempotency.py` (Modify)

*   **Type / Scope**: Python module (critical).
*   **Proposed Changes**:
    *   `claim()`: after the hash check, a key with `status='in_progress'` whose `run_id`
        equals the caller's run is taken over (returns `New`) at any age. Other runs keep
        the 60 s rule.
    *   `complete()` becomes "first complete wins": `UPDATE … WHERE key = %s AND status =
        'in_progress' RETURNING key`. If no row is updated and the key is `done`, it
        returns `Done(stored_result)`; if the key does not exist, it raises as today. It
        returns `None` when it stored the result.

#### 12. `services/safety/guard.py` (Modify)

*   **Type / Scope**: Python module (critical).
*   **Proposed Changes**: Step 7: if `complete()` returns `Done`, skip `apply`, write the
    `tool_result` row with the stored result, and return the stored result. Docstring step
    4 lists the same-run rule. No other step changes.

#### 13. `tests/unit/test_idempotency.py`, `tests/unit/test_guard.py` (Modify)

*   **Type / Scope**: Unit tests (database tests).
*   **Proposed Changes**:
    *   `test_same_run_in_progress_is_taken_over` (under 60 s).
    *   `test_other_run_in_progress_conflicts` (under 60 s, unchanged behaviour).
    *   `test_second_complete_returns_first_result`.
    *   `test_retry_after_adapter_failure_reaches_adapter` (guard: first `call()` raises
        `AdapterFailure(retryable=True)`; a second `run_guarded` with the same key and run
        calls the adapter again and succeeds; the vendor key is the same both times).
    *   `test_concurrent_same_run_calls_apply_once` (two `run_guarded` calls with one key:
        `apply` runs once, both return the same result).

#### 14. `services/mcp/seller.py` (Modify)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: `SellerAdapter` gains `OPERATIONS: ClassVar[frozenset[str]]`.
    Each seller tool's `limits` first checks `tool in type(adapter).OPERATIONS` and raises
    `GuardRefused("unsupported")` otherwise. Unguarded tools (`search_items`, `quote`)
    raise the same refusal through `meta.tool_errors()`. `vendor_keys` maps follow the
    one-key-per-card rule.

#### 15. `services/mcp/grocery/server.py`, `restaurant/server.py`, `delivery/server.py`, and the three mock adapters (Modify)

*   **Type / Scope**: Python modules.
*   **Proposed Changes**: The `kroger` map uses `kroger_catalog` for every tool including
    `amend_order`. The maps move to `vendors.card_vendor_key` (item 7). The mock adapters
    declare `OPERATIONS` with all their tools. The courier interface gains `OPERATIONS`
    the same way.

#### 16. `tests/unit/test_operations.py`, `tests/unit/test_tool_schemas.py` (Create)

*   **Type / Scope**: Unit tests.
*   **Proposed Changes**:
    *   `test_every_adapter_declares_its_methods`: every adapter class's `OPERATIONS`
        equals the interface methods it defines.
    *   `test_unsupported_refused_before_claim`: a fake adapter without `amend_order` →
        wire result `refused/unsupported/false`, no `idempotency_keys` row, one
        `tool_result refused=true` row.
    *   `test_one_vendor_key_per_card`: for each server map, every tool of a vendor maps to
        one key.
    *   `test_item_43_arguments_still_accepted`: for each tool on all five servers, the item
        43 argument names are accepted with their types, and no new argument is required.
        The item 43 list is written into the test as a literal.

#### 17. `services/mocks/control.py` (Modify)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: `set_mode()` raises `PanelRefused("no_real_adapter")` (409) for
    `mode='real'` on a key with no real class, and `PanelRefused("unknown_vendor")` (422)
    for a key not in `VENDORS`. `inject_event()` raises `PanelRefused("real_vendor")` (409)
    when the target card's vendor key resolves to `real` for the next run. Whatever route serves `/panel/*` maps
    `PanelRefused` to its status code.

#### 18. `services/seed/data/agents.yaml` (Modify)

*   **Type / Scope**: Seed data.
*   **Proposed Changes**: Organization names and card descriptions per the §6 table.
    `kroger-us` loses `location_id_from_env: KROGER_LOCATION_ID`. Square cards gain
    `square_location_id_from_env: SQUARE_RETAIL_LOCATION_ID` and
    `SQUARE_RESTAURANT_LOCATION_ID`. Agent ids, vendors and org ids do not change.

#### 19. Run view, WhatsApp order text, A2A card, panel footer (Modify)

*   **Type / Scope**: `services/api/routes/runs.py`, `services/api/whatsapp/sender.py` and
    whichever module renders A2A cards and the panel.
*   **Proposed Changes**: Wherever a seller or courier name is shown for a run, use
    `vendors.vendor_label(card, runs.modes)` and show the badge. The panel footer and the
    A2A demo page show `vendors.DISCLAIMER`. If a surface does not exist yet, its own issue
    picks up this rule from adapters.md §6.

#### 20. `tests/unit/test_panel_reset.py` or a new `tests/unit/test_panel_controls.py` (Create)

*   **Type / Scope**: Unit tests.
*   **Proposed Changes**: `test_mode_real_refused_without_adapter`,
    `test_event_refused_on_real_card`, `test_event_allowed_on_mock_card`,
    `test_unknown_vendor_refused`.

#### 21. `migrations/0010_vendor_object_id.sql` (Create)

*   **Type / Scope**: SQL migration (critical, additive).
*   **Proposed Changes**: `ALTER TABLE catalog_items ADD COLUMN vendor_object_id text
    NULL;` with a comment that it holds the vendor's id for our SKU (Square's variation
    id).

#### 22. `services/seed/__main__.py` (Modify)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: Remove the `_kroger_catalog_mode` check and the
    `kroger_location_id` argument (the Kroger card always seeds from the mock catalog).
    Write `config.square_location_id` from the env names in `agents.yaml`; a missing value
    is allowed (the card then can't be real; the adapter raises `adapter_not_configured`).
    Fail if any business card's vendor has no map entry, or if any seller SKU has no
    product in the graph data (`off_subset.json`).

#### 23. `services/seed/square.py` and `Makefile` target `seed-square` (Create, Modify)

*   **Type / Scope**: Python module and make target.
*   **Proposed Changes**: Pushes each Square card's catalog to its sandbox location:
    upsert items and variations with client ids `#<sku>`, our SKU in the variation's `sku`
    field, idempotency key `vendor_key("seed:<agent_id>:<sku>")`; set inventory counts
    from `mock_stock` (`live`); fail if the location has any tax; store the variation ids
    in `catalog_items.vendor_object_id`. Run by the owner (`make seed-square`) after
    `make seed`. It is never run by tests, CI or the loop. The Square adapters raise
    `adapter_not_configured` for a SKU with no `vendor_object_id`.

#### 24. `.env.example` (Modify)

*   **Type / Scope**: Config template (no secrets).
*   **Proposed Changes**: Replace `SQUARE_ACCESS_TOKEN` with `SQUARE_RETAIL_TOKEN`,
    `SQUARE_RESTAURANT_TOKEN`, `SQUARE_RETAIL_LOCATION_ID` and
    `SQUARE_RESTAURANT_LOCATION_ID`, each with a one-line comment. Add
    `COURIER_PRICE_TOLERANCE_MINOR=200`. Mark the `KROGER_*` and `DOORDASH_*` lines
    "unused until a real adapter exists (adapters.md §14)".

#### 25. `tests/unit/test_seed.py` (Modify)

*   **Type / Scope**: Unit tests.
*   **Proposed Changes**: `test_seed_fails_on_unmapped_vendor`,
    `test_seed_fails_on_sku_without_graph_node`, `test_seed_writes_square_location_id`,
    `test_kroger_card_seeds_from_mock_catalog`, `test_display_names_carry_simulated` (every
    card whose key is in `ALWAYS_MOCK`, and every `mock` card except the two generic ones,
    has "(simulated)" in its organization name).

#### 26. `services/mcp/square_client.py` (Create)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: `SquareClient(token, transport=None)` around one
    `httpx.AsyncClient` with base URL, `Square-Version: vendors.SQUARE_VERSION`, timeout
    10 s total and 3 s to connect. Methods: `create_order`, `update_order`,
    `retrieve_order`, `calculate_order`, `inventory_counts`, `upsert_catalog`,
    `list_taxes`. `_raise_for(response)` applies the §7 error table. It never logs bodies
    or headers. `transport` lets tests inject `httpx.MockTransport`.

#### 27. `services/mcp/grocery/adapters/square.py`, `services/mcp/restaurant/adapters/square.py` (Create)

*   **Type / Scope**: Python modules.
*   **Proposed Changes**: `SquareRetailAdapter` and `SquareRestaurantAdapter`, implementing
    `SellerAdapter` per §7: `search_items` over `catalog_items` plus Square inventory;
    `quote` from `catalog_items` (or `CalculateOrder` if step 0 selects it);
    `place_order` with `vendor_key(key)`, `reference_id=order_id`, `PICKUP` fulfilment
    with the order id as the display name, a currency check and a total check;
    `amend_order` with `UpdateOrder` and `vendor_key(key, part="update")`, or left out of
    `OPERATIONS` if step 0 says so. Results are the same minimal types as the mocks.

#### 28. `services/common/ids.py` (Modify)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: `vendor_key(key, part=None)`: with `part`, the uuid5 name is
    `key + "#" + part`. With `part=None`, output is unchanged. A test checks both.

#### 29. `tests/contract/grocery/test_grocery_contract.py`, `restaurant/…`, `delivery/…`, `tests/contract/shop.py` (Modify)

*   **Type / Scope**: Contract tests (interface layer).
*   **Proposed Changes**: Parametrise the existing contract cases over every adapter class
    for the area (mock and Square), with Square running on the replay transport. Add
    `OPERATIONS` cases.

#### 30. `infra/docker-compose.yml` (Modify)

*   **Type / Scope**: Compose (critical: infra/).
*   **Proposed Changes**: Pass `SQUARE_RETAIL_LOCATION_ID`, `SQUARE_RESTAURANT_LOCATION_ID`
    and `COURIER_PRICE_TOLERANCE_MINOR` to `mcp`. No change to the `MODE_*` entries.

#### 31. `tests/contract/test_square_wire.py` and `tests/contract/fixtures/square_*.json` (Create)

*   **Type / Scope**: Contract tests (wire layer).
*   **Proposed Changes**: Replays the recordings through `httpx.MockTransport`, with a
    conftest fixture that fails any real network call. Cases: order create, replay with the
    same key, total mismatch, 5xx, 429, key reuse with a different body, version conflict on
    update, inventory counts. `test_recorded_requests_have_no_household_fields`.

#### 32. `scripts/record_square.py` (Create)

*   **Type / Scope**: Owner-run recorder (critical: scripts/).
*   **Proposed Changes**: Reads the token from the environment, runs the wire cases against
    the sandbox, scrubs tokens, merchant ids, location ids and addresses, and writes the
    fixtures. A header comment says it is never run by CI or the loop.

#### 33. `services/mcp/delivery/server.py` (Modify)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: `book_delivery`'s documented failure reasons gain
    `price_changed`. `COURIER_PRICE_TOLERANCE_MINOR` is a Settings field (default 200),
    read by real courier adapters only. A unit test with a fake real courier checks that a
    price above quote + tolerance raises `price_changed` and books nothing.

#### 34. `services/safety/guard.py`, `services/mcp/meta.py`, `services/mcp/traced.py` (Modify)

*   **Type / Scope**: Trace payloads (guard part is critical).
*   **Proposed Changes**: `tool_call`, `tool_result` and `error` payloads gain `vendor`,
    `mode` and `adapter`. `error` payloads gain `http_status`, `vendor_code`, `latency_ms`
    and `mock_event_id` when known. `AdapterFailure` gains optional `http_status` and
    `vendor_code` attributes (additive). A unit test checks the fields on each row type.

### Verification Plan

*   **Static analysis & linting**: `make lint`.
*   **Unit tests**: `make test-unit` (database tests via `uv run --with pgserver`).
*   **Contract tests**: `make test-contract SERVICE=grocery`, `restaurant`, `delivery`, with
    the network blocked.
*   **Evals**: `make eval-fast` for any step that touches `services/agents/` (none
    planned). Every existing eval case passes unchanged.
*   **Manual (owner only)**: `square_check.py` extended run (step 0); `make seed-square` and
    `scripts/record_square.py` (step 7); then on the VM with `MODE=real`, CUJ A1 and A2 by
    hand.

--------------------------------------------------------------------------------

*Drafted with EGM*
