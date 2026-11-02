# Kitchenless adapters: Square sandbox as the one real vendor, other platforms simulated

*   **Author:** madushankaf
*   **Status:** Approved (2026-11-02, #146)
*   **Date:** 2026-11-02
*   **Path:** `docs/designs/adapters.md`
*   **Issue:** #146
*   **Builds on:** [architecture.md](architecture.md), [agent-protocol.md](agent-protocol.md)
    and [identity-authz.md](identity-authz.md), all approved. Where this design needs them
    to change, it lists the change in §1 below ("Changes to approved designs") and does not
    assume it. identity-authz needs no change.
*   **Input:** the adapters brainstorm (accepted by the owner on 2026-11-02) and the Square
    sandbox spike (`docs/spikes/vendors/square_check.py`, run 2026-11-02).

In this document, "§N" means a section of this design's Technical Plan, and "architecture
§N", "architecture item N" or "architecture G5" means the architecture design. "Build step
N" means the build table in this design's Detailed Implementation Plan unless another
design is named. "The loop" is `scripts/loop.sh`, the unattended agent that implements
issues labelled `ready`. "The panel" is the control panel (`/panel/*`, architecture item
47), the operator page the owner uses during the demo to switch vendor modes and to inject
mock events. Mock events are `mock_events` rows (out of stock, kitchen overloaded, courier
delayed, adapter down, shortfall) that only the mocks read. "Judges" are the hackathon
judges who watch the demo.

--------------------------------------------------------------------------------

## Overview

The agents order food from other businesses' systems: grocery stores, restaurants and
couriers. Each system is reached through an adapter, a class behind one of the domain MCP
servers (`services/mcp/`). Each adapter has a mock version and may have a real version.
`modes.adapter_for` picks one for each vendor in each run.

The approved architecture planned three real integrations: Square (retail and restaurant),
DoorDash Drive (courier) and Kroger (catalog only). Only Square is reachable from the
owner's location:

*   Kroger's developer site does not load from Sri Lanka.
*   DoorDash's developer sign-up needs a US phone number.
*   Uber Direct, the other well-known courier API, needs a store in a country it serves,
    and its customer id is tied to that region.
*   Square's sandbox accepted the account. The spike created a catalog item, set and read
    stock, created an order with a pickup fulfilment, and got the same order back when the
    create was replayed with the same idempotency key.

This design makes Square the one real vendor and runs every other platform as a simulation
on the existing mocks. Each simulated card carries the platform's name and the word
"simulated" on every surface that names it. The owner adds a disclaimer to the demo. The
adapter interfaces stay vendor-neutral. Connecting a real DoorDash, Uber Direct or Kroger
later means writing one adapter class, its contract tests, one entry in each of four
configuration files and one card change (§12). No agent code, tool, guard or eval changes.

The design also fixes the MCP tool schemas that architecture §13 left provisional. They stay
as architecture item 43 lists them, with only the additions in §10.

## The Problem

The demo has to show agents working with well-known food platforms, but only one of those
platforms will give this project an account. Four things break if the build follows the
approved text as it stands:

1.  **The approved designs describe vendors this project cannot connect.** Architecture §3
    and §5, items 44 and 55, the DoorDash webhook route, the §8 status rows, CUJ 5, build
    step 7, Q6, the contract-test list, three decisions of the project
    brief (D15, §4.7 and §7) and the card descriptions in `services/seed/data/agents.yaml`
    all say DoorDash and Kroger are real. The loop builds what the designs say. It would
    write a DoorDash adapter nobody can test and a Kroger sync that cannot run.
2.  **Two build steps can never start.** Architecture build step 7 waits on "the adapters
    design, and Q6". Architecture Q6 asks for DoorDash's status names and webhook signing,
    which only a DoorDash account can answer. Identity-authz build step 13 (#128) waits on
    architecture build step 7 (and on identity-authz's own Q6, the Expo redirect URI, which
    this design does not touch).
3.  **The mode switch would turn the simulations off.** Under the rule from #47, a vendor
    that resolves to `real` with no real adapter class raises `adapter_not_configured`. On
    a VM started with `MODE=real` to use Square, every simulated vendor would become
    unavailable, and the weekly order would fail at delivery.
4.  **A real-looking simulation can mislead.** A real retailer's name on invented prices,
    or a DoorDash label on an invented status sequence, presents unverified vendor
    behaviour as fact. Judges and family members see the same panel and WhatsApp messages.

Building Square for real also exposes gaps the provisional contracts never had to answer:

*   The wire contract has no way to say that a seller cannot do an operation.
*   A failed adapter call leaves its idempotency key `in_progress`, so the client's one
    retry ends the run with `idempotency_conflict` (#156).
*   An empty `MODE_<VENDOR>` value crashes mode resolution (#155, already filed).

## Goals

*   **G1. Square is the one real vendor.** The Square sandbox serves the retail store and
    the restaurant through real adapters. Every order create uses a key from
    `ids.vendor_key`, so a replayed call creates nothing new. CI replays recorded Square
    responses with the network blocked. The owner runs the live check by hand
    (`REAL=1 make test-contract SERVICE=grocery` and `SERVICE=restaurant`, as architecture
    already defines).
*   **G2. Every other platform is simulated and says so.** DoorDash Drive, Uber Direct and
    the grocery chain run on the existing mock cards and mock tables, driven by the
    existing panel events. No agent cards are added. Their display names are "DoorDash
    Drive (simulated)", "Uber Direct (simulated)" and "Hillcrest Market (simulated)", a
    fictional store whose description says its catalog is modelled on Kroger's.
*   **G3. A vendor with no real adapter can never resolve to `real`.** `modes.ALWAYS_MOCK`
    is derived from `modes._ADAPTERS`: every vendor key with no real class. These keys
    resolve to `mock` before the panel row, `MODE_<VENDOR>` or `MODE` is read, and the
    panel refuses `real` for them.
*   **G4. The label matches the run being shown.** Every surface that names a seller or
    courier for a run shows `sandbox` or `simulated`, computed from that run's pinned modes
    (`runs.modes`), not from the current toggle. Surfaces with no run (A2A cards, the
    directory) use the card descriptions in §6. One disclaimer string is used everywhere.
*   **G5. The tool schemas extend architecture item 43 and never break it.** Every tool
    accepts every argument set it accepts today. No argument is added or removed. A test
    checks this for every tool on all five servers.
*   **G6. An operation a seller cannot do is refused before anything is claimed.** It
    returns `kind=refused, reason=unsupported, retryable=false` before the idempotency key
    is claimed, and the refusal is traced.

### Acceptance criteria

*   With `MODE=real` and the Square settings present, the weekly order completes, with
    Square real and every other vendor simulated.
*   A run pinned before a deploy that adds a vendor key still completes.
*   The Square replay tests pass offline.
*   Every existing eval case passes unchanged.
*   Architecture build step 7 has no blocker that waits on a vendor account, so
    identity-authz build step 13 waits only on its own blockers.

## Non-Goals

*   **Vendor-shaped fakes.** There are no fake HTTP servers imitating DoorDash, Uber or
    Kroger, and simulated vendors store no vendor-style ids or status names. This project
    cannot read those vendors' current APIs, so any imitation would be a guess presented as
    their behaviour. Stored ids stay UUIDs, and stored delivery states stay the four in the
    `mock_deliveries` CHECK.
*   **Real DoorDash, Uber Direct or Kroger adapters,** the Kroger catalog sync
    (architecture item 55), the DoorDash webhook handler and an answer to Q6. All move to
    "connecting a real vendor later" (§12). The `/webhooks/doordash/<secret>` route stays
    reserved and returns 404.
*   **Amending Square orders.** Agent-protocol already makes "shortfalls reported by real
    vendors" a Non-Goal: a shortfall is opened only from the panel, against a mock-mode
    order. The Square classes refuse `amend_order` (§8).
*   **Pushing our catalog into Square.** Square orders use ad-hoc line items priced from
    our tables (§7). Square holds no copy of our catalog, and we store no Square item ids.
*   **Instacart.** No journey, demo step, goal or eval uses a shopping-list handoff, and it
    would send diet and allergy data off the platform. It moves to Future Improvements.
*   **New agent cards per brand.** Each card costs a Keycloak client, OpenFGA tuples,
    `organizations` and `agent_clients` rows, business rules and re-baselined evals, and a
    courier card adds about 1.5k model tokens to every run. The 14-agent roster stays; only
    display names and descriptions change. Agent ids stay, because eval cases and Keycloak
    clients use them.
*   **A plugin system or capability discovery.** Adding a vendor is the written checklist in
    §12, not a runtime mechanism.
*   **Square OAuth** ("connect your POS"). Square tokens stay in the environment.
*   **Payments.** A placed Square order is an unpaid `OPEN` order. Square Payments, Toast
    and Clover are out of scope.
*   **Live vendor calls in CI or the loop.** Tests replay recordings. Only the owner runs the
    recorder and the live check. This keeps the CLAUDE.md rule that tests never switch to
    real integrations.
*   **A courier price check at booking.** Quotes stay zone-level. Re-pricing with the
    address is defined when the first real courier is connected (Future Improvements).

## Open Questions

*   **Square sandbox behaviour not yet checked.** Build step 0 (an extended spike the owner
    runs) answers each of these. Each has a fallback already fixed in this design, so no
    other build step waits on the answer:

    | Unknown | If it works | Fallback |
    |---|---|---|
    | A second location in one sandbox account | Both tokens hold the same value; the two cards differ by location id | Two sandbox accounts, one token each. Same code (§7). |
    | An order of ad-hoc line items (name, quantity, price), replayed with the same key | As §7 describes | Create one catalog item per order line in the same call batch (no stored ids), then the order. Recorded as an amendment to this design. |
    | Taxes applied to ad-hoc lines | None are applied: our total equals Square's | `quote` calls `CalculateOrder` and returns Square's total. |
    | Two creates with the same key at the same moment | One order, both responses equal | The same-run takeover (§9) applies only after the first attempt has failed: on `AdapterFailure` the guard sets the key's `updated_at` back by 60 s instead. |
    | The same key with a different body | A 4xx Square error | Mapped to `vendor_key_reused` (not retryable) in either case. |

*   **The fictional grocery name.** This design uses "Hillcrest Market". The owner may change
    the string before build step 5 lands. Only display text depends on it.

## Critical User Journeys (CUJs)

### CUJ A1: Weekly order with the Square sandbox

*   **Given**: the VM runs with `MODE=real`, and `mcp` has `SQUARE_RETAIL_TOKEN` and
    `SQUARE_RETAIL_LOCATION_ID`.
*   **When**: the household agent runs the weekly grocery order and picks the Square store.
*   **Then**: Square's sandbox holds one `OPEN` order whose lines and total match the
    authorised quote. The courier step runs on the DoorDash Drive card, which is simulated.
    For this run the panel shows "Square Grocery (sandbox)" and "DoorDash Drive
    (simulated)". Replaying `place_order` with the same key returns the same Square order
    id.

### CUJ A2: A simulated vendor under `MODE=real`

*   **Given**: `MODE=real`, with no real DoorDash, Uber Direct or Kroger adapter.
*   **When**: a run starts.
*   **Then**: `runs.modes` has `doordash`, `kroger_catalog` and `kroger_ordering` as
    `mock`, and the run completes. Setting `doordash` to `real` on the panel is refused
    with `doordash is always mock`.

### CUJ A3: Square returns a 5xx during `place_order`

*   **Given**: a run with `square_retail` pinned to `real`.
*   **When**: Square returns a 503 to the create-order call.
*   **Then**: the adapter raises `AdapterFailure("adapter_5xx", retryable=True)`. The
    client retries once with the same key. The guard lets the retry take over the key,
    because the same run claimed it, and the adapter calls Square again with the same
    vendor key. Square either creates the order or returns the one it already made. The run
    does not end with `idempotency_conflict` (#156).

### CUJ A4: An amend request reaches a real Square card

*   **Given**: a run with `square_retail` pinned to `real`, and a seller agent that calls
    `amend_order` on its order (for example after a bad model choice; no shortfall can be
    opened on this order, see §8).
*   **When**: the call reaches the grocery server.
*   **Then**: it returns `refused / unsupported / retryable=false`, no idempotency key is
    claimed, and one `tool_result` row with `refused=true` is written.

## Technical Plan (Architecture)

### 1. Changes to approved designs

Each change below is written into the named document as a dated note ("Amended
2026-11-02 (adapters, #146)") in the same PR that lands this design. Nothing else in those
documents changes.

| Document | Section | Today | After |
|---|---|---|---|
| architecture | §1 brief decisions superseded | — | Adds the brief's D15, §4.7 and §7 (real DoorDash and Kroger). The brief is not in the repository, so the supersession is recorded here. |
| architecture | §3 roster | Kroger: real catalog; DoorDash: real sandbox | Agent ids unchanged. Display names and descriptions as in §6 below. |
| architecture | §5 adapter table | Kroger real catalog; DoorDash real sandbox | Square retail and restaurant real (sandbox). Every other seller and courier is mock. |
| architecture | §5 mode resolution | `kroger_ordering` is always mock | Every vendor key with no real class is always mock. A key missing from a non-empty `runs.modes` means `mock`; an empty `runs.modes` raises. Eval households' runs pin every key to `mock`. |
| architecture | §5 guard step 4 | Takeover only after 60 s | Also take over a same-hash `in_progress` key that the same run claimed (§9). |
| architecture | §5 guard step 7 | Store the result, apply, trace | The store succeeds only while the key is `in_progress`; a later completer returns the stored result and skips the state change (§9). |
| architecture | §8 courier status table | DoorDash names, "to be verified, Q6" | Mock names only. The DoorDash column moves to Future Improvements. |
| architecture | §9 failure matrix | Adapter timeout 15 s | One 10 s deadline per adapter call, 3 s to connect. The client timeout stays 15 s. |
| architecture | Q6 | Open; blocks build step 7 | Restated: "closes when the first real courier key exists." Blocks nothing. |
| architecture | build step 7 | Real Square, DoorDash and Kroger; blocked by Q6 | Square adapters and the vendor labels, built by this design's build steps 2–7. Blocked by this design only. |
| architecture | items 44, 55 | `KrogerCatalogAdapter`, `DoorDashDriveAdapter`, the Kroger catalog sync | Not built. See adapters §12. |
| architecture | `/webhooks/doordash/<secret>` | DoorDash status webhook | Reserved; returns 404 until a real courier exists. |
| architecture | §13 | MCP schemas provisional | Fixed by adapters §10. |
| architecture | CUJ 5, Alternative V | DoorDash real; Uber rejected as a second real courier | CUJ 5 uses the Square card for the vendor failure. Alternative V stands: Uber Direct is simulated on the existing mock courier card, not a new card. |
| architecture | item 47 (panel) | Events allowed on any card | A `shortfall` event is refused when the order's run pinned the seller's vendor key to `real` (§8). |
| agent-protocol | P27 | Real Square and Kroger adapters refuse amend with `unsupported` | Unchanged in substance. Note added: there is no real Kroger adapter, and the Square refusal is the §8 mechanism. |
| SITEMAP | adapters row | Planned, real DoorDash and Kroger | This design. |

### 2. Components

```text
            agents (seller, courier)
                     │ MCP tool call (run_id, agent_id, token)
                     ▼
  services/mcp/<area>/server.py ──► services/safety/guard.py (guard steps 0-7)
                     │
       vendors.card_vendor_key(area, card, tool)
                     │
                     ▼
  services/common/modes.py  adapter_for(vendor_key, runs.modes)   _ADAPTERS, ALWAYS_MOCK
                     │
     ┌───────────────┴────────────────────┐
     │ real                               │ mock
     ▼                                    ▼
  SquareRetailAdapter            MockStoreAdapter / MockRestaurantAdapter /
  SquareRestaurantAdapter        MockCourierAdapter (services/mocks/*, mock_* tables)
     │ services/mcp/square_client.py (httpx, Square-Version pinned)
     ▼
  connect.squareupsandbox.com/v2

  api: panel, run view, WhatsApp text ──► services/common/vendors.py vendor_label(card, runs.modes)
```

### 3. Vendor keys and modes (`services/common/modes.py`)

The vendor keys stay as they are: `kroger_catalog`, `kroger_ordering`, `square_retail`,
`square_restaurant` and `doordash`. None is renamed or removed, so runs pinned before this
design and existing `adapter_modes` rows still parse.

*   **Real classes.** `_ADAPTERS` gains `("square_retail", "real")` and
    `("square_restaurant", "real")` in build step 6. `ALWAYS_MOCK` becomes
    `VENDORS - {key for (key, mode) in _ADAPTERS if mode == "real"}`. Today that is
    `kroger_catalog`, `kroger_ordering` and `doordash`.
*   **Always mock first.** A key in `ALWAYS_MOCK` resolves to `mock` before the panel row,
    `MODE_<VENDOR>` or `MODE` is read. That fixes Problem 3. `control.set_mode()` already
    refuses `real` for these keys.
*   **A missing key means mock.** `adapter_for` treats a key in `VENDORS` that is missing
    from a non-empty `runs.modes` as `mock`. A run pinned before a deploy that adds a key
    keeps working, with the new vendor simulated. An empty or absent `runs.modes` raises
    `adapter_not_configured`, so a run with no pinned modes cannot silently become all
    simulated. A key not in `VENDORS` still raises.
*   **Eval runs are pinned to mock.** `runs.start()` pins every key to `mock` when the
    household's `env` is `eval`, whatever the panel or environment says. The mock tables
    are split by `env`; the Square sandbox is not, so evals must never write to it. This
    amends #36's run start.
*   **Empty values are unset.** #155 makes an empty `MODE_<VENDOR>` fall through to `MODE`.
    It is ready for the loop and does not wait for this design.
*   **Storage stays `real` / `mock`.** "Simulated" and "sandbox" are labels only (§6). The
    `adapter_modes` CHECK does not change.
*   **The next-run mode** of a key is what `resolve_modes` would return for a run started
    now. **The pinned mode** is the value in a run's `runs.modes`. Labels use the pinned
    mode. The panel's mode toggle shows the next-run mode.

### 4. Settings and configuration

`services/common/config.py` already has `mode_<key>` fields for every vendor key,
`square_retail_token`, `square_restaurant_token` and `square_env` (all read by `mcp`). This
design adds `square_retail_location_id` and `square_restaurant_location_id` (`mcp`). The
Square adapters refuse to start a call unless `square_env == "sandbox"`, raising
`AdapterFailure("adapter_not_configured", retryable=False)`. A missing token or location id
raises the same failure on the first call. That fails closed and appears in the trace.

`infra/.env.example` already lists `SQUARE_RETAIL_TOKEN`, `SQUARE_RESTAURANT_TOKEN` and
`SQUARE_ENV=sandbox`. It gains the two location ids, and its unused `SQUARE_ACCESS_TOKEN`
line is removed. The `KROGER_*` and `DOORDASH_*` lines are marked "unused until a real
adapter exists (adapters.md §12)".

A unit test checks that every vendor key has a `mode_<key>` Settings field and a
`MODE_<KEY>` entry in both `infra/docker-compose.yml` and `infra/.env.example`, so a PR that
adds a key must add all three.

### 5. From card to vendor key

Each seller server today passes `vendor_keys`, a map from a card's `vendor` to the vendor
key of each tool, and the delivery server has `_VENDOR_KEYS`. A card with `vendor: mock`
always gets the area's mock. This design moves those maps into one function that both `mcp`
and `api` import, and adds two rules.

*   `vendors.card_vendor_key(area, card, tool) -> str | None` returns the key for a card's
    tool, or `None` for a `mock` card. It holds the maps that the servers hold today.
*   **One key per card.** Every tool of a card maps to the same key, `amend_order`
    included. The `kroger` entry changes from the catalog/ordering split to
    `kroger_catalog` for every tool. The `square` entries gain `amend_order` (today they map
    only `search_items`, `quote` and `place_order`). Without that entry, a Square card in
    mock mode cannot amend, and a real one would fail with `adapter_not_configured`
    instead of `unsupported`.
*   **The seed fails** on a business card whose `vendor` has no entry (other than `mock`).

`agent_cards.vendor` values do not change (`kroger`, `square`, `doordash`, `mock`).

### 6. Names, labels and the disclaimer

*   **Display names.** `organizations.name` and the card description carry the label. The
    `org_id` values are ids and are never shown, so they do not change.

    | Card | Organization name | Description |
    |---|---|---|
    | `grocery:kroger-us` | Hillcrest Market (simulated) | Simulated grocery store. Catalog modelled on Kroger's public product data. Not affiliated with Kroger. |
    | `grocery:square-us` | Square Grocery | Grocery store on the Square sandbox. Simulated when the platform runs in mock mode. |
    | `restaurant:square-us` | Square Kitchen | Restaurant on the Square sandbox. Simulated when the platform runs in mock mode. |
    | `delivery:doordash-us` | DoorDash Drive (simulated) | Simulated courier. Shows where a DoorDash Drive connection plugs in. Not affiliated with DoorDash. |
    | `delivery:mock-us` | Uber Direct (simulated) | Simulated courier. Shows where an Uber Direct connection plugs in. Not affiliated with Uber. |
    | `grocery:mock-us`, `restaurant:mock-us` | Mock Grocery US (simulated), Mock Kitchen US (simulated) | unchanged |

*   **The label.** `vendors.vendor_label(area, card, org_name, run_modes) -> VendorLabel`
    returns `badge` and `text`:
    *   `badge` is `sandbox` when the card's key is `real` in `run_modes` and the key has a
        real class; otherwise `simulated`. A `mock` card is always `simulated`.
    *   `text` is `org_name` when it already ends in "(simulated)", and
        `"{org_name} ({badge})"` otherwise. For example "Square Grocery (sandbox)", "Square
        Grocery (simulated)" in a mock-mode run, and "DoorDash Drive (simulated)".

    The run view, the panel and WhatsApp order text show `text`.
*   **Surfaces with no run** (A2A cards, the directory) show the organization name and the
    description above.
*   **The disclaimer** is one constant, `vendors.DISCLAIMER`: "Square orders go to Square's
    sandbox. Hillcrest Market, DoorDash Drive and Uber Direct are simulated by Kitchenless,
    this platform. No real orders, payments or deliveries are made." The panel footer and
    the A2A demo page show it. The owner may reword it; there is only one copy.
*   **Timing.** Simulated vendors keep the mocks' current timing. No delays are added.

### 7. The Square adapters

`services/mcp/grocery/adapters/square.py` and `services/mcp/restaurant/adapters/square.py`
share `services/mcp/square_client.py`. The client uses raw `httpx.AsyncClient`, not the
Square SDK, because of the `mcp` container's 320 MB memory cap.

*   **Configuration.** Base URL `https://connect.squareupsandbox.com/v2`. `Square-Version`
    is the constant `SQUARE_VERSION = "2025-01-23"` in `square_client.py`, the version the
    recordings use. Token and location id from the Settings in §4.
*   **Accounts.** If one sandbox account can hold two locations, both tokens hold the same
    value and the cards differ by location. If not, they are two sandbox accounts. The code
    is the same either way.
*   **`search_items` and `quote`** read our `catalog_items` (retail) or `menu_items`
    (restaurant) tables exactly as the mocks do, because Square has no allergen or diet
    codes and holds none of our items. Stock is `in_stock` from those tables. `quote` prices
    lines from `price_minor`. If the spike shows taxes apply to ad-hoc lines, `quote` calls
    `CalculateOrder` with the same lines and returns Square's total.
*   **`place_order`** makes one call, `POST /orders`, with:
    *   `idempotency_key`: the `idempotency_key` argument the adapter already receives,
        which is `ids.vendor_key(<guard key>)`;
    *   `location_id` from Settings, `reference_id` = our `order_id`;
    *   one ad-hoc line item per order line: `name` (our item name), `quantity` (integer as
        a string), `base_price_money` (`unit_minor`, currency) and `note` = our SKU;
    *   one `PICKUP` fulfilment whose recipient display name is the order id. No household
        name, phone or address is sent.

    Every field is a function of the guarded request and our stored rows, so a retry sends
    the same body. The result maps `total_money.amount` to `total_minor` and checks the
    currency. If Square's total differs from the authorised total, the adapter raises
    `AdapterFailure("total_mismatch", retryable=False)`; the order stays as an unpaid `OPEN`
    order in the sandbox. "Placed" means an unpaid `OPEN` order.
*   **`amend_order`** is not implemented (§8).
*   **One deadline per call.** Each Square call runs inside `asyncio.timeout(10)`, with the
    httpx connect timeout at 3 s. httpx timeouts apply to each phase separately, so the
    outer deadline is what keeps the first attempt shorter than the client's 15 s.
*   **Error mapping.** Every failure is an `AdapterFailure` with `vendor="square"` and, when
    known, `http_status` and `vendor_code` (§11):

    | Square response | `reason` | `retryable` |
    |---|---|---|
    | timeout or connection error | `adapter_timeout` | true |
    | 5xx | `adapter_5xx` | true |
    | 429 | `adapter_rate_limited` | true |
    | 401 / 403 | `adapter_not_configured` | false |
    | idempotency key reused with a different body | `vendor_key_reused` | false |
    | other 4xx | `adapter_4xx` | false |
    | currency differs from the card | `currency_mismatch` | false |

    A 429 gets the client's one normal retry; there is no extra backoff.

### 8. Operations a seller cannot do

*   Each seller adapter class declares `OPERATIONS: ClassVar[frozenset[str]]`, the guarded
    seller tools it supports. The mocks declare `place_order` and `amend_order`. The Square
    classes declare `place_order` only.
*   The guarded seller tools (`place_order`, `amend_order` in `services/mcp/seller.py`)
    resolve the adapter class first, before `run_guarded`. Their `limits` function then
    checks the tool against the class's `OPERATIONS` before anything else. Limits run at
    guard step 3, before the key claim at step 4. An unsupported tool raises
    `GuardRefused("unsupported")`, which reaches the client as `kind=refused,
    reason=unsupported, retryable=false` and writes a `tool_result` row with
    `refused=true`. The guard itself does not change for this.
*   `search_items` and `quote` are supported by every class, so they need no check.
*   **Shortfall events on real orders are refused.** `control.inject_event()` refuses a
    `shortfall` event whose order's run pinned the seller's vendor key to `real`
    (`ControlRefused`, "shortfall events need a mock-mode order"). Other panel events only
    change mock tables. On a card that is real in a run, they have no effect on that run,
    and the panel says so next to the event buttons.

### 9. Retrying after an adapter failure (#156)

Guard step 4 gains one rule: a key with the same request hash that is `in_progress` and
was claimed by **the same run** is taken over, whatever its age. A key claimed by a
different run, `in_progress` and under 60 s old, still returns `idempotency_conflict`. This
is safe because the retry sends the same vendor key, and Square and the mocks deduplicate
on it. The 10 s adapter deadline (§7) is below the client's 15 s timeout, so the first
attempt has finished before the retry starts.

The same rule lets two calls from one run that are in flight together both reach the
vendor; the vendor still makes one order. So guard step 7 becomes "first complete wins":
`idempotency.complete()` stores the result only while the key is `in_progress`. A second
call that reaches step 7 gets the stored result back, skips the tool's order-state change
(the `apply` function) and returns the stored result. The owner chose this over releasing
the key on failure (2026-11-02). If the spike shows Square does not deduplicate two
simultaneous creates, the fallback in Open Questions applies.

### 10. Tool schemas

The tools, names and arguments in architecture item 43, with the #47 and #48 amendments,
are fixed as they are. This design adds no argument and removes nothing. It adds:

*   the refusal reason `unsupported` (guarded seller tools);
*   the failure reasons in §7's table (`adapter_rate_limited`, `vendor_key_reused`,
    `total_mismatch`, `currency_mismatch`).

`tests/unit/test_tool_schemas.py` checks, for every tool on all five servers, that each
argument in architecture item 43 is accepted with the same type and that no new argument is
required. The household and knowledge servers have no adapters with modes and are
unchanged. Open Food Facts and Neo4j have no mode: they are seed data and the knowledge
store.

### 11. Contract tests and traces

*   **Interface contract.** `tests/contract/grocery/`, `restaurant/` and `delivery/` run
    the same cases against every adapter class of the area, the Square classes included,
    with Square on a replay transport. Add `OPERATIONS` cases.
*   **Square wire contract.** `tests/contract/grocery/test_square_wire.py` and
    `tests/contract/restaurant/test_square_wire.py` replay recorded Square responses from
    `tests/contract/fixtures/square_*.json` through `httpx.MockTransport`, with a fixture
    that fails any real network call. `make test-contract SERVICE=<area>` collects them.
    Recordings are scrubbed of tokens, merchant ids, location ids and addresses.
*   **The recorder,** `scripts/record_square.py`, is run only by the owner, by hand, with
    the token exported in their own terminal. The loop and CI never run it.
*   Only Square has recordings. Hand-written recordings for vendors this project cannot
    reach would be fabricated evidence.
*   A contract test checks that no recorded request contains household fields (names,
    phones, addresses).
*   **Trace fields.** `AdapterFailure` gains optional `vendor`, `http_status` and
    `vendor_code`. `meta.tool_errors()` copies them into the `error` row's payload when
    set. Nothing else is added; `latency_ms` is already a trace column.

### 12. Connecting a real vendor later (checklist)

Worked example: Uber Direct.

1.  Add `uber_direct` to `VENDORS`, with its real class in `_ADAPTERS`.
2.  Add the `mode_uber_direct` Settings field and the `MODE_UBER_DIRECT` entries in
    `infra/docker-compose.yml` and `infra/.env.example`, plus its credential settings (the
    §4 test fails until the three mode entries exist).
3.  Write `services/mcp/delivery/adapters/uber_direct.py`. It passes the courier interface
    contract and has recorded wire tests.
4.  Change `delivery:mock-us` to `vendor: uber_direct` in `agents.yaml`, add it to
    `card_vendor_key`, and update its name and description.
5.  The owner runs the recorder and the live check by hand.

No agent code, tool, guard or eval change is needed. Kroger and DoorDash follow the same
steps. DoorDash also needs Q6 answered if its webhook is used, and a real courier needs the
address re-pricing rule in Future Improvements.

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Square changes behaviour under the pinned `Square-Version` | Low | Replay tests pass while the live sandbox fails | The owner's live check (`REAL=1 make test-contract`) before the demo; the recorder refreshes fixtures. |
| Square sandbox data is reset | Medium | None for us | Square holds no copy of our catalog and we store no Square ids (§7). |
| Rate limit (429) during the demo | Low | One seller unavailable for that run | Retryable; the client's one retry; other sellers remain. |
| Orphaned unpaid `OPEN` orders pile up in the sandbox | High | Clutter only | Sandbox only; payments out of scope. |
| An eval run writes to the Square sandbox | Low | Shared sandbox state, flaky evals | Eval runs pin every key to `mock` (§3), with a unit test. |
| A simulated brand is mistaken for the real service, for example in a screenshot | Medium | Misleading demo | "(simulated)" in the organization name, so it appears on every surface; the disclaimer; a fictional name for the grocery store. |
| Two simultaneous creates with one key make two Square orders | Low | A duplicate sandbox order | Build step 0 checks it; the fallback in Open Questions narrows the takeover. |
| A real Square card is shown as "sandbox" in a mock-mode run | Low | Wrong label | The label is computed from the run's pinned modes (§6), with a unit test. |

## Non-Functional Requirements

*   **Security and privacy.** Square tokens live only in the `mcp` environment. They are
    never logged or traced. No household name, phone or address is sent to Square (§7), and
    a contract test checks the recordings for that. Recordings are scrubbed before commit.
*   **Memory.** No SDK. One shared `httpx.AsyncClient` per process.
*   **Rollout and rollback.** No schema change. Each build step is its own PR, and rolling
    one back means reverting it. `MODE=mock` (the default) keeps every path on the mocks.
    The vendor keys and `adapter_modes` values do not change, so old runs and old panel rows
    still parse.

## Alternatives Considered

### Alternative AD1: Keep DoorDash and Kroger real, as the architecture says

*   **Pros**: No amendments. The demo would show three real platforms.
*   **Cons**: Kroger's developer site does not load from Sri Lanka, and DoorDash's sign-up
    needs a US phone number. Architecture build step 7, and through it identity-authz build
    step 13, stay blocked on architecture Q6, which nobody can answer.
*   **Reason for Rejection**: It cannot be built from here. The adapters would be untested
    code, and two build steps would never start.

### Alternative AD2: Uber Direct as the real courier

*   **Pros**: A second well-known platform, and an alternative to DoorDash.
*   **Cons**: Uber Direct needs a store in a country it serves, and the customer id is tied
    to that region. Architecture Alternative V had already rejected it as a second courier.
*   **Reason for Rejection**: The same access problem as DoorDash. Uber Direct is simulated
    on the existing mock courier card instead.

### Alternative AD3: Vendor-shaped fake servers for each simulated platform

*   **Pros**: Simulated vendors would use something like the real wire format, and the real
    adapter code would run against them.
*   **Cons**: This project cannot read those vendors' current APIs, so the fakes would be
    guesses presented as vendor behaviour. Each fake is more code to build and maintain.
    Vendor-style ids and status names in storage would break
    `uuid5(UUID(external_delivery_id), …)` and the four-state CHECK on `mock_deliveries`,
    and changing a CHECK is not additive.
*   **Reason for Rejection**: The owner chose the "level 1" approach: the vendor-neutral
    interfaces and existing mocks, named after the platforms and labelled "simulated".

### Alternative AD4: New agent cards per brand

*   **Pros**: Each platform gets its own card next to the generic mocks.
*   **Cons**: Each card needs a Keycloak client, OpenFGA tuples, `organizations` and
    `agent_clients` rows, business rules and re-baselined evals, and a courier card adds
    about 1.5k model tokens to every run.
*   **Reason for Rejection**: Relabelling the existing cards gives the same demo without
    that work. Agent ids stay because eval cases and Keycloak clients use them.

### Alternative AD5: Real retailer names on simulated stores

*   **Pros**: "Kroger (simulated)" is recognisable to judges.
*   **Cons**: A real retailer's name on invented prices and stock presents those prices as
    the retailer's. A label can be cropped out of a screenshot.
*   **Reason for Rejection**: The grocery store gets a fictional name, with "catalog
    modelled on Kroger's" in its description. Platform names (DoorDash Drive, Uber Direct)
    are kept with "(simulated)" in the name itself, because they name a connection type,
    not a shop's prices, and the word travels with the name to every surface.

### Alternative AD6: A `simulated` mode value in storage

*   **Pros**: Storage would match the label.
*   **Cons**: `adapter_modes` and the mode checks accept `real` and `mock` only. A new value
    means replacing a CHECK, which is not additive, and touching every mode check.
*   **Reason for Rejection**: Storage stays `real` / `mock`. "Simulated" is a label.

### Alternative AD7: A separate per-vendor list or a vendors table

*   **Pros**: One place that lists vendors, their real classes and their credentials; a
    table would let the panel list vendors from the database.
*   **Cons**: `modes._ADAPTERS` already says which keys have real classes, and nothing reads
    a credentials list. A table of credentials would fall under identity-authz's encryption
    rules.
*   **Reason for Rejection**: Derive `ALWAYS_MOCK` from `_ADAPTERS`, and test the Settings,
    Compose and `.env.example` entries.

### Alternative AD8: A `vendor_key` field in `agent_cards.config`

*   **Pros**: The mapping would sit next to the card.
*   **Cons**: The servers already map a card's `vendor` to a key per tool. A second mapping
    in the database could disagree with the code.
*   **Reason for Rejection**: Keep one map in code (`vendors.card_vendor_key`), require one
    key per card, and make the seed fail on an unmapped vendor.

### Alternative AD9: Supported operations per vendor

*   **Pros**: One table answers "can this vendor amend?".
*   **Cons**: The mock and the real class of one vendor differ: the mock amends, and the
    Square class does not.
*   **Reason for Rejection**: Each adapter class declares `OPERATIONS`.

### Alternative AD10: Raise `unsupported` inside the adapter

*   **Pros**: No change to the tool's limits.
*   **Cons**: The adapter runs at guard step 6, after the key claim. It would raise an
    `AdapterFailure`, which reaches the client as `unavailable`. The client would retry and
    waste a call, the refusal would not be traced as a refusal, and the key would stay
    `in_progress`.
*   **Reason for Rejection**: Check in `limits` (guard step 3) and refuse with
    `unsupported`.

### Alternative AD11: Separate catalog and ordering modes per card

*   **Pros**: It supported "real Kroger catalog, mock ordering".
*   **Cons**: With Kroger simulated, it only allows a real catalog to meet mock ordering on
    one card, which fails part way through an order.
*   **Reason for Rejection**: One vendor key per card.

### Alternative AD12: A courier webhook for status

*   **Pros**: Faster status updates.
*   **Cons**: It needs Q6 (signing and status names) answered, which needs a DoorDash
    account.
*   **Reason for Rejection**: A real courier polls status in the mock courier's existing
    progression loop (architecture item 46). The webhook route stays reserved.

### Alternative AD13: A guarded `address_ref` on the courier quote

*   **Pros**: A real courier could quote with the exact address.
*   **Cons**: It would share personal data before a courier is chosen, against architecture
    G5 (the address reaches only the chosen courier, at booking), and needs a new
    data-share scope.
*   **Reason for Rejection**: Quotes stay zone-level. Re-pricing at booking is left to the
    first real courier.

### Alternative AD14: Live Square calls in CI

*   **Pros**: It would catch sandbox changes on every PR.
*   **Cons**: It puts a secret in CI, makes CI depend on Square's uptime, and breaks the rule
    that tests never switch to real integrations.
*   **Reason for Rejection**: Recorded replays in CI; the recorder and the live check are
    run by the owner.

### Alternative AD15: The Square Python SDK

*   **Pros**: Typed models and less request code.
*   **Cons**: Extra memory in the `mcp` container (320 MB cap), and the SDK pins its own API
    version.
*   **Reason for Rejection**: Raw `httpx` with `Square-Version` pinned in one constant.

### Alternative AD16: Fault injection for panel events on real cards

*   **Pros**: Judges could trigger "out of stock" on the real Square card.
*   **Cons**: It would show invented failures as the real vendor's behaviour, and it is
    another code path.
*   **Reason for Rejection**: Panel events drive only the mocks; shortfalls on real-mode
    orders are refused.

### Alternative AD17: Instacart as a simulated shopping-list handoff

*   **Pros**: One more recognisable platform.
*   **Cons**: No journey, demo step, goal or eval uses it. It would send diet and allergy
    data off the platform, needing a guarded data share and scope. A simulated link sent to
    real WhatsApp phones would look like a fake instacart.com link.
*   **Reason for Rejection**: Deferred to Future Improvements.

### Alternative AD18: Keep the 60-second rule for retries (#156)

*   **Pros**: No change to guard step 4.
*   **Cons**: Every Square 5xx or timeout, and every `adapter_down` panel event on
    `place_order`, ends the run with `idempotency_conflict`, because the retry arrives
    within 60 s.
*   **Reason for Rejection**: Same-run takeover, with "first complete wins" and the adapter
    deadline below the client's timeout.

### Alternative AD19: Release the key on failure (#156)

*   **Pros**: Two calls from one run in flight together still conflict.
*   **Cons**: A crash between the failure and the release leaves the key stuck for 60 s, and
    the guard needs a delete path for keys.
*   **Reason for Rejection**: The owner chose same-run takeover (2026-11-02). Setting
    `updated_at` back on failure stays as the fallback in Open Questions.

### Alternative AD20: Push our catalog into Square and store Square's item ids

*   **Pros**: Square orders would reference real Square catalog items, and Square would
    track stock.
*   **Cons**: It needs a new column, an owner-run seed command with network access and the
    Square tokens, a hand-built seed key outside `ids.idempotency_key`, and price syncing
    between Square and our tables. Without the sync, a total mismatch is likely, which
    leaves orphaned orders. Restaurant items would need the same in `menu_items`.
*   **Reason for Rejection**: Ad-hoc line items priced from our tables (§7).

### Alternative AD21: Amending Square orders through `UpdateOrder`

*   **Pros**: The shortfall flow would reach the real vendor.
*   **Cons**: Agent-protocol makes real-vendor shortfalls a Non-Goal, and the panel cannot
    open one on a real-mode order. It would need Square's order `version` (not in the
    guarded request, so a retry could send a different body under the same key), a second
    vendor key per tool, and a changed `amend_order` signature.
*   **Reason for Rejection**: The Square classes leave `amend_order` out of `OPERATIONS`.

## Additional Resources

*   Square spike: `docs/spikes/vendors/square_check.py` and its record in
    `docs/spikes/vendors.md` (build step 0).
*   Square Orders API: <https://developer.squareup.com/reference/square/orders-api>.
*   #155 (empty `MODE_<VENDOR>`), #156 (retry after an adapter failure), #128
    (identity-authz build step 13, waiting on architecture build step 7).

## Future Improvements

*   **Connect a real courier.** DoorDash Drive or Uber Direct once an account in a served
    region exists, following §12. Q6 (DoorDash status names and webhook signing) closes
    then, and the DoorDash status column returns to architecture §8. The courier adapter
    re-prices with the address inside `book_delivery` and refuses (`price_changed`) if the
    price rose by more than a set amount over the zone-level quote.
*   **A real grocery catalog.** The Kroger catalog sync (architecture item 55) if the
    developer site becomes reachable.
*   **Instacart shopping-list handoff,** through Instacart's own MCP server, as a guarded
    data share with its own scope row.
*   **Square OAuth,** so a restaurant can connect its own POS.

--------------------------------------------------------------------------------

## Detailed Implementation Plan

### Build steps (one PR each; C = touches a critical path and needs human review)

| Build step | Work | Files (below) | Blocked by | C |
|---|---|---|---|---|
| 0 | Extended Square spike, run by the owner | 1 | — | — |
| 1 | This design and the amendment notes | 2–4, 6 | — | C |
| 2 | Modes: `ALWAYS_MOCK` derived, missing key, eval pin; `vendors.py`; shortfall refusal | 7–12 | build step 1 | — |
| 3 | Guard: same-run takeover, first complete wins | 13–15 | build step 1 | C |
| 4 | `OPERATIONS` and `unsupported` | 16–18 | build step 2 | — |
| 5 | Seed: names, descriptions, Kroger card on the mock catalog, unmapped-vendor check | 19–21 | build step 2 | — |
| 6 | Square adapters, client, settings, Compose, wire tests, recorder, trace fields | 22–31 | build steps 0, 3, 4 | C |
| 7 | Labels on the run view, panel and WhatsApp text; disclaimer | 32–33 | build steps 2, 5 | — |

Build steps 2–7 together are architecture build step 7 as narrowed by §1. When build step 7
merges, architecture build step 7 is done and identity-authz build step 13 (#128) no longer
waits on it. #156 is implemented by build step 3. #155 is separate and already ready.

**Existing tests that change.** These are rewrites to match the amended design, not
weakened checks. Each PR names them in its body:

| Test | Build step | Change |
|---|---|---|
| `tests/unit/test_modes.py::test_vendor_env_beats_global_mode` | 2 | `kroger_catalog` under `MODE=real` is now `mock` (always mock). |
| `tests/unit/test_modes.py::test_kroger_ordering_always_mock` | 2 | Same: `kroger_catalog` is `mock`; the test also asserts `doordash` is `mock`. |
| `tests/unit/test_modes.py::test_adapter_for_real_without_adapter_not_configured` | 2 | The "no mode for the run" case uses a non-empty `run_modes` without the key and now expects the mock; `{}` still raises. The `real` loop covers only keys with no real class. |
| `tests/unit/test_idempotency.py::test_stale_in_progress_reclaimable` | 3 | The "still in flight" check uses a different run (`retry_run`) for the under-60 s claim, because the same run now takes over. |
| `tests/unit/test_idempotency.py::test_concurrent_claim_one_wins` | 3 | The two claims use two different runs. A new test covers the same-run case. |
| `tests/unit/test_seed.py` (`_seed` helper and the `kroger` location assertion) | 5 | `kroger_location_id` is no longer passed or asserted. |

### Target Files to Create/Modify

#### 1. `docs/spikes/vendors/square_check.py` and `docs/spikes/vendors.md` (Modify, Create)

*   **Type / Scope**: Spike script and record (build step 0).
*   **Proposed Changes**: Add one check per row of the Open Questions table: a second
    location (or a second account); an order of ad-hoc line items, replayed with the same
    key; taxes on those lines; two simultaneous creates with one key (two concurrent
    requests); the same key with a different body. `vendors.md` records the first run
    (2026-11-02), the extended run, and which fallback each answer selects. The script
    still prints ids, states and amounts only.

#### 2. `docs/designs/adapters.md` (Create)

*   **Type / Scope**: This design.

#### 3. `docs/designs/architecture.md` (Modify)

*   **Type / Scope**: Dated amendment notes (critical).
*   **Proposed Changes**: Every architecture row of §1's table, each as an "Amended
    2026-11-02 (adapters, #146)" note next to the text it changes.

#### 4. `docs/designs/agent-protocol.md` (Modify)

*   **Type / Scope**: Dated amendment notes (critical).
*   **Proposed Changes**: A note under P27: there is no real Kroger adapter, and the Square
    classes refuse `amend_order` through §8.

#### 5. `docs/designs/identity-authz.md` (No change)

*   **Type / Scope**: None. Its build step 13 names "architecture build step 7", which
    build step 6 here completes; its Q6 is the Expo redirect URI, outside this design.

#### 6. `docs/designs/SITEMAP.md` (Modify)

*   **Type / Scope**: Registry (critical).
*   **Proposed Changes**: The adapters row becomes "Approved" with its date. The
    architecture and agent-protocol rows gain the amendment date.

#### 7. `services/common/modes.py` (Modify)

*   **Type / Scope**: Python module.
*   **Proposed Changes**:
    *   `ALWAYS_MOCK = VENDORS - {key for (key, mode) in _ADAPTERS if mode == "real"}`.
    *   `resolve_modes`: keys in `ALWAYS_MOCK` are `mock` before any other source (today
        that check already comes first for `kroger_ordering`; it now covers all of them).
    *   `adapter_for(vendor, run_modes)`: if `run_modes` is empty, raise
        `adapter_not_configured`; if `vendor` is in `VENDORS` but not in `run_modes`, use
        `mock`; otherwise as today.
    *   New `pin_for_env(modes: dict[str, Mode], env: str) -> dict[str, Mode]`: returns
        `{key: "mock" for key in modes}` when `env == "eval"`, else `modes` unchanged.

#### 8. `services/common/runs.py` (Modify)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: `start()` reads the household's `env` and stores
    `modes.pin_for_env(await resolve_modes(conn), env)` in `runs.modes` and the `run_start`
    trace row.

#### 9. `services/common/vendors.py` (Create)

*   **Type / Scope**: Python module, imported by `mcp` and `api`.
*   **Proposed Changes**:
    *   `card_vendor_key(area: str, card: AgentCard, tool: str) -> str | None`: `None` for a
        `mock` card; otherwise the key from `_VENDOR_KEYS[area][card.vendor]`, raising
        `AdapterFailure("adapter_not_configured", retryable=False)` when the vendor has no
        entry. `_VENDOR_KEYS` holds the maps from the three servers, with `kroger` mapped to
        `kroger_catalog` and `square` to `square_retail` / `square_restaurant`, each for all
        four seller tools; `delivery` maps `doordash` to `doordash`.
    *   `VendorLabel` (frozen dataclass: `badge: Literal["sandbox", "simulated"]`,
        `text: str`) and `vendor_label(area, card, org_name, run_modes) -> VendorLabel` per
        §6.
    *   `DISCLAIMER: str` (§6 text).

#### 10. `services/mcp/seller.py`, `services/mcp/grocery/server.py`, `services/mcp/restaurant/server.py`, `services/mcp/delivery/server.py` (Modify)

*   **Type / Scope**: Python modules.
*   **Proposed Changes**: `SellerTools` drops its `vendor_keys` field, and its `adapter()`
    calls `vendors.card_vendor_key(self.area, card, tool)`, then the mock class for `None`
    or `modes.adapter_for(key, ctx.modes)` otherwise. The grocery and restaurant servers
    stop passing `vendor_keys`. The delivery server's `_VENDOR_KEYS` lookup is replaced by
    the same call.

#### 11. `services/mocks/control.py` (Modify)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: In `_validate_shortfall`, after the order is loaded, read the
    order's run's `runs.modes` and the seller card. If
    `vendors.card_vendor_key(area, card, "amend_order")` is pinned to `real`, raise
    `ControlRefused("shortfall events need a mock-mode order")`. `set_mode()` already
    refuses `real` for `ALWAYS_MOCK` keys; no change there.

#### 12. `tests/unit/test_modes.py`, `tests/unit/test_vendors.py`, `tests/unit/test_mock_control.py` (Modify, Create)

*   **Type / Scope**: Unit tests.
*   **Proposed Changes**: The rewrites in the table above, plus:
    *   `test_always_mock_ignores_mode_real` (`MODE=real`: `doordash`, `kroger_catalog` and
        `kroger_ordering` are `mock`; `square_*` are `real`).
    *   `test_always_mock_ignores_panel_real` (an `adapter_modes` row `doordash=real` is
        ignored).
    *   `test_missing_key_in_run_modes_is_mock` and `test_empty_run_modes_raises`.
    *   `test_eval_env_pins_mock` (`pin_for_env`) and a `runs.start` database test for an
        eval household under `MODE=real`.
    *   `test_every_vendor_has_setting_compose_and_env_example` (reads
        `infra/docker-compose.yml` and `infra/.env.example` as text).
    *   `test_one_vendor_key_per_card` (for every area and vendor, all four seller tools map
        to one key) and `test_square_maps_amend_order`.
    *   `test_vendor_label` (one case per row of §6's examples, plus `real` mode with no
        real class → `simulated`).
    *   `test_shortfall_refused_on_real_mode_order` and
        `test_shortfall_allowed_on_mock_mode_order` (database tests on `control`).

#### 13. `services/safety/idempotency.py` (Modify)

*   **Type / Scope**: Python module (critical).
*   **Proposed Changes**:
    *   `_SELECT_SQL` also returns `run_id`. In `claim()`, after the hash check and the
        `done` check: if `status == 'in_progress'` and the stored `run_id` equals the
        caller's `run_id`, run `_TAKE_OVER_SQL` and return `New()`, at any age. The stale
        rule for other runs is unchanged.
    *   `complete(conn, key, result) -> Done | None`: the update gains
        `AND status = 'in_progress'`. If it updates one row, return `None`. If it updates
        none, select the row: if it is `done`, return `Done(stored_result)`; if it does not
        exist, raise as today.

#### 14. `services/safety/guard.py` (Modify)

*   **Type / Scope**: Python module (critical).
*   **Proposed Changes**: At guard step 7, if `idempotency.complete()` returns `Done`,
    skip `apply`, write the `tool_result` row with the stored result, and return the stored
    result. The module docstring's step 4 and step 7 describe the new rules. No other step
    changes.

#### 15. `tests/unit/test_idempotency.py`, `tests/unit/test_guard.py` (Modify)

*   **Type / Scope**: Unit tests (database tests).
*   **Proposed Changes**: The rewrites in the table above, plus:
    *   `test_same_run_in_progress_is_taken_over` (under 60 s).
    *   `test_other_run_in_progress_conflicts` (under 60 s).
    *   `test_concurrent_claims_same_run_both_new`.
    *   `test_second_complete_returns_first_result`.
    *   `test_retry_after_adapter_failure_reaches_adapter` (the first `call()` raises
        `AdapterFailure(retryable=True)`; a second `run_guarded` with the same key and run
        calls the adapter again and succeeds).
    *   `test_concurrent_same_run_calls_apply_once` (two `run_guarded` calls with one key:
        `apply` runs once and both return the same result).

#### 16. `services/mcp/seller.py` (Modify)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: `SellerAdapter` gains `OPERATIONS: ClassVar[frozenset[str]]`.
    `SellerTools.place_order` and `SellerTools.amend_order` resolve
    `adapter = self.adapter(card, tool, ctx)` before `run_guarded` (an
    `AdapterFailure` there reaches the client through `meta.tool_errors()` as today). The
    first line of each tool's `check` (its `limits` function) is: if the tool is not in
    `type(adapter).OPERATIONS`, raise `GuardRefused("unsupported")`. The tool's `call()`
    uses the resolved `adapter`. Add `UNSUPPORTED = "unsupported"` next to the other reason
    constants.

#### 17. `services/mcp/grocery/adapters/mock_store.py`, `services/mcp/restaurant/adapters/mock_restaurant.py` (Modify)

*   **Type / Scope**: Python modules.
*   **Proposed Changes**: `OPERATIONS = frozenset({"place_order", "amend_order"})`.

#### 18. `tests/unit/test_operations.py`, `tests/unit/test_tool_schemas.py` (Create)

*   **Type / Scope**: Unit tests.
*   **Proposed Changes**:
    *   `test_operations_match_methods`: for every seller adapter class, a guarded tool is in
        `OPERATIONS` if and only if the class defines that method.
    *   `test_unsupported_refused_before_claim` (database test): a fake adapter without
        `amend_order` gives the wire result `refused/unsupported/false`, no
        `idempotency_keys` row, and one `tool_result` row with `refused=true`.
    *   `test_item_43_arguments_still_accepted`: for each tool on all five servers, the
        architecture item 43 argument names are accepted with their types, and no new
        argument is required. The item 43 list is written into the test as a literal.

#### 19. `services/seed/data/agents.yaml` (Modify)

*   **Type / Scope**: Seed data.
*   **Proposed Changes**: Organization names and card descriptions per §6. `grocery:kroger-us`
    loses `location_id_from_env: KROGER_LOCATION_ID`. Agent ids, vendors and org ids do not
    change.

#### 20. `services/seed/__main__.py` (Modify)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: Remove `_kroger_catalog_mode` and its check in `main()`, and the
    `kroger_location_id` parameter of `seed()`: the Kroger card always seeds from the mock
    catalog. In `_validate`, fail when a business card's `vendor` is not `mock` and
    `vendors.card_vendor_key` has no entry for it. The existing check that every seller
    SKU has a product in `off_subset.json` stays as it is.

#### 21. `tests/unit/test_seed.py` (Modify)

*   **Type / Scope**: Unit tests.
*   **Proposed Changes**: The rewrite in the table above, plus
    `test_seed_fails_on_unmapped_vendor` and `test_display_names_carry_simulated` (every
    card whose key is in `ALWAYS_MOCK`, and every `mock` card, has an organization name
    ending in "(simulated)").

#### 22. `services/common/config.py` (Modify)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: `square_retail_location_id: str | None = _var({"mcp"})` and
    `square_restaurant_location_id: str | None = _var({"mcp"})`.

#### 23. `infra/.env.example` and `infra/docker-compose.yml` (Modify)

*   **Type / Scope**: Config template and Compose (critical: `infra/`).
*   **Proposed Changes**: `.env.example`: add `SQUARE_RETAIL_LOCATION_ID=` and
    `SQUARE_RESTAURANT_LOCATION_ID=`, each with a one-line comment; remove the unused
    `SQUARE_ACCESS_TOKEN=` line; mark the `KROGER_*` and `DOORDASH_*` lines "unused until a
    real adapter exists (adapters.md §12)". Compose: pass the two location ids to `mcp`.

#### 24. `services/mcp/square_client.py` (Create)

*   **Type / Scope**: Python module.
*   **Proposed Changes**:
    *   `SQUARE_VERSION = "2025-01-23"`.
    *   `SquareClient(token: str, *, transport: httpx.AsyncBaseTransport | None = None)`
        around one `httpx.AsyncClient` (base URL, `Square-Version`, bearer token, connect
        timeout 3 s). Each method runs inside `asyncio.timeout(10)`.
    *   `async create_order(*, idempotency_key, location_id, reference_id, lines, currency)
        -> dict` and `async calculate_order(*, location_id, lines, currency) -> dict`.
    *   `_raise_for(response)` applies the §7 error table and sets `vendor="square"`,
        `http_status` and `vendor_code`. It never logs bodies or headers.
    *   `client_for(token, transport=None)` returns one cached client per token; tests pass a
        `MockTransport`.

#### 25. `services/mcp/meta.py` (Modify)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: `AdapterFailure.__init__(self, reason, retryable, *, vendor=None,
    http_status=None, vendor_code=None)`. `tool_errors()` adds each non-`None` one to the
    `error` row's payload.

#### 26. `services/mcp/grocery/adapters/square.py`, `services/mcp/restaurant/adapters/square.py` (Create)

*   **Type / Scope**: Python modules.
*   **Proposed Changes**: `SquareRetailAdapter(card, env)` and
    `SquareRestaurantAdapter(card, env)`, same constructor as the mocks.
    `OPERATIONS = frozenset({"place_order"})`.
    *   `search_items` and `quote`: the same queries as the mocks over `catalog_items` or
        `menu_items` (shared helper, no `mock_stock`); `quote` uses `calculate_order` only
        if build step 0 selected that fallback.
    *   `place_order(*, idempotency_key, order_id, lines, currency)`: checks
        `square_env == "sandbox"`, the token and the location id; builds the §7 body from
        `lines` and the item names in our tables; calls `create_order`; checks currency and
        total; returns the same minimal result as the mock (`external_order_id` = Square's
        order id, `state`, `total_minor`, `currency`).
    *   No `amend_order` method.

#### 27. `services/common/modes.py` (Modify)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: `_ADAPTERS` gains
    `("square_retail", "real"): "services.mcp.grocery.adapters.square:SquareRetailAdapter"`
    and `("square_restaurant", "real"):
    "services.mcp.restaurant.adapters.square:SquareRestaurantAdapter"`.
    `test_modes.py` gains `test_square_real_resolves_square_class`.

#### 28. `tests/contract/grocery/test_grocery_contract.py`, `tests/contract/restaurant/test_restaurant_contract.py`, `tests/contract/shop.py` (Modify)

*   **Type / Scope**: Contract tests (interface layer).
*   **Proposed Changes**: Parametrise the existing cases over the mock and the Square class
    of the area, with Square on the replay transport. Cases marked "mock only" in
    architecture stay mock only. Add `test_operations_declared`.

#### 29. `tests/contract/grocery/test_square_wire.py`, `tests/contract/restaurant/test_square_wire.py`, `tests/contract/fixtures/square_*.json` (Create)

*   **Type / Scope**: Contract tests (wire layer).
*   **Proposed Changes**: A fixture that fails any real network call. Cases: order create,
    replay with the same key (same order id), total mismatch, 5xx, 429, key reused with a
    different body, 401, currency mismatch. `test_recorded_requests_have_no_household_fields`
    checks every recorded request body for the seed households' names, phones and
    addresses.

#### 30. `scripts/record_square.py` (Create)

*   **Type / Scope**: Owner-run recorder (critical: `scripts/`).
*   **Proposed Changes**: Reads the token and location id from the environment, runs the
    wire cases against the sandbox, scrubs tokens, merchant ids, location ids and
    addresses, and writes the fixtures. A header comment says CI and the loop never run it.

#### 31. `tests/unit/test_square_client.py` (Create)

*   **Type / Scope**: Unit tests.
*   **Proposed Changes**: Each row of the §7 error table through a `MockTransport`; the
    10 s deadline (a transport that sleeps past it raises `adapter_timeout`); the
    `AdapterFailure` fields; no token in any raised message.

#### 32. Run view, panel and WhatsApp order text (Modify)

*   **Type / Scope**: `services/api/routes/runs.py`, `services/api/whatsapp/sender.py`, and
    the panel page.
*   **Proposed Changes**: Wherever a seller or courier is named for a run, show
    `vendors.vendor_label(area, card, org_name, runs.modes).text`. The panel footer and the
    A2A demo page show `vendors.DISCLAIMER`. A surface that does not exist yet takes this
    rule from §6 in its own issue, which cites this section.

#### 33. `tests/unit/test_vendor_labels_surfaces.py` (Create)

*   **Type / Scope**: Unit tests.
*   **Proposed Changes**: For each surface in item 32 that exists, a run pinned with
    `square_retail=real` shows "Square Grocery (sandbox)", and one pinned with `mock` shows
    "Square Grocery (simulated)".

### Verification Plan

*   **Static analysis & linting**: `make lint`.
*   **Unit tests**: `make test-unit` (database tests via `uv run --with pgserver`).
*   **Contract tests**: `make test-contract SERVICE=grocery`, `SERVICE=restaurant` and
    `SERVICE=delivery`, with the network blocked.
*   **Evals**: no build step touches `services/agents/`. Every existing eval case passes
    unchanged; `make eval-fast` runs on any step that turns out to.
*   **Manual (owner only)**: the extended `square_check.py` (build step 0); after build step
    6, `scripts/record_square.py`, then `REAL=1 make test-contract SERVICE=grocery` and
    `SERVICE=restaurant`; then on the VM with `MODE=real`, CUJ A1 and A2 by hand.

--------------------------------------------------------------------------------

*Drafted with EGM*
