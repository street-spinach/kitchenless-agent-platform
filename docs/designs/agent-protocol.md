# Kitchenless agent-to-agent protocol

*   **Author:** madushankaf
*   **Status:** Approved; amended 2026-11-01 (identity-authz I-P1: per-message tokens
    closed)
*   **Date:** 2026-10-31
*   **Path:** `docs/designs/agent-protocol.md`
*   **Builds on:** `docs/designs/architecture.md` (approved; amended 2026-10-31, commit
    6b53253). Where the two disagree, the architecture wins unless this design lists the
    change in its architecture changes table and the same PR applies it.

--------------------------------------------------------------------------------

## Overview

Kitchenless agents (consumer, household, grocery, restaurant, delivery) plan and order food
by sending each other messages. The architecture already decides how a message is
delivered:

*   each message is a row in the Postgres table `agent_messages`;
*   the receiving host claims it with `FOR UPDATE SKIP LOCKED` and is woken by
    `LISTEN/NOTIFY`;
*   delivery is at least once, and a message's id is fixed in advance from what caused it
    (architecture §4).

The architecture also lists the message types it needs, marked provisional (architecture
§13). It does not say what a message contains, who may send it, what state it changes, or
what happens when it arrives late, twice or from the wrong agent.

This design defines that. It covers:

*   **Agent cards:** who an agent is, who owns it, who it acts for, and what it can do.
*   **The envelope:** the fields every message carries, its ids and threading, timestamps,
    expiry and version numbers.
*   **The message catalog:** every message type, with its sender, receiver, body schema,
    the one-line summary shown in the live view, and the state it changes.
*   **The negotiation state machine:** request, offers, counters, then accept, reject or
    expire. The order and delivery state machines stay as the architecture defines them
    (§8); this design only references them.
*   **Arrival rules:** ordering, duplicates, late and unexpected messages, and protocol
    errors.
*   **Visibility:** how every message appears in traces and in the live conversation view.
*   **The sender check:** what a receiver verifies before it acts. Messages carry no tokens.
*   **Two bindings:** the Postgres outbox for all internal traffic, and an internal-only A2A
    JSON-RPC endpoint so a standard A2A client can negotiate with a business agent in the
    demo.
*   **Fulfillment problems:** partial and substituted offers before an order is placed, and
    a bounded shortfall flow after it is placed.

The data model follows A2A v1.0.0: agent cards, messages made of parts, and tasks with task
states. Anyone who knows A2A can read the messages in the live view or call the demo
endpoint with a standard client. The stored form stays the architecture's columns; conversion
to A2A happens only at the HTTP endpoint and in the panel.

Two groups read this document. The unattended implementation loop needs every rule written
down, so it does not have to make a design choice. The people watching the demo need every
message visible and readable.

## The Problem

The architecture moves rows between agents, but a row with no agreed contents is not a
protocol. Without one, these failures can occur:

*   **Money and orders.** An agent accepts an offer that has already expired, or a message
    delivered twice places a second order. At-least-once delivery makes duplicates normal,
    not rare.
*   **Wrong senders.** A store sends a message only a household agent may send, or one
    household's agent answers another household's quote. If the receiver acts on the body
    alone, any agent can steer any conversation.
*   **Retrying errors that can never succeed.** A message with a broken body is retried five
    times, marked as a poison message, and ends the household's whole run, when the right
    answer is to refuse it once and tell the sender.
*   **Privacy leaks in the live view.** If each message's display line is free text, a line
    such as "No peanuts: Kavi is allergic" puts a child's allergy on the demo screen. The
    current leak check matches field names only, so it would not catch this.
*   **Double effects on replay.** After a crash, a step runs again. Unless its messages
    have fixed ids and its state changes commit together with them, the replay sends a
    second copy or moves a quote twice.
*   **No path for a missing item after placement.** Today a store that runs out of an item
    after the order is placed has no message to send and no tool to change the order. Any
    ad hoc fix would skip the allergen and budget checks.
*   **Nothing to show for "agent-to-agent".** Internal rows in a table do not show that the
    agents speak a recognized protocol. The owner wants the demo to show a standard A2A
    client talking to a business agent.

The solution is one written protocol: a single message catalog with schemas and summary
templates, a negotiation state machine whose transitions are conditional database updates,
receiver rules for every unexpected arrival, and two bindings that write into the same
table and go through the same checks.

## Goals

Each goal names the test or eval case that checks it (Verification Plan).

*   **G1. One catalog.** Every message type that can appear in `agent_messages` has exactly
    one catalog row: sender class, receiver class, body schema, summary template, party
    rule, state effect, and the CUJ step that uses it.
    *Checked by:* `test_every_type_has_catalog_row`, `test_every_type_has_summary_template`.
*   **G2. No duplicate effects.** A message delivered any number of times changes state
    once. A redelivered `accept` places no second order; a duplicate `SendMessage` over HTTP
    returns the existing task.
    *Checked by:* `tools-redelivered-accept-no-second-order`,
    `tools-a2a-duplicate-send-one-row`.
*   **G3. Expiry is enforced in code.** An offer past `quotes.expires_at` is never accepted.
    A late `accept` gets `reject {reason: expired}`, and the household agent re-quotes once.
    *Checked by:* `safety-expired-offer-never-accepted`, `robust-offer-expires-twice-errors`.
*   **G4. Every receiver checks the sender** before acting: the message type is allowed for
    the sender and receiver classes, and later messages in a conversation come from the
    parties named in the catalog's party rule. A failed check is refused with
    `protocol_error`, not retried.
    *Checked by:* `safety-wrong-party-refused`, `safety-wrong-sender-class-refused`.
*   **G5. Protocol errors are not retried.** A message that can never succeed (bad schema,
    unknown type, unknown version, wrong sender) is marked `failed` on its first attempt,
    writes an `error` trace and sends `protocol_error` to the sender. It never becomes a
    poison message.
    *Checked by:* `test_protocol_error_marks_failed_attempts_unchanged`,
    `safety-wrong-party-refused`.
*   **G6. Every message is visible.** Every message has one `message` trace row and a
    non-empty summary line (at most 120 characters) that the live conversation view
    shows, with request and reply linked through `causation_id`.
    *Checked by:* `safety-summaries-no-profile-values` (`every_message_has_summary`).
*   **G7. Summaries do not leak.** Summary lines are rendered from templates that use only
    the slots allowlisted in §8. No summary contains a member's name or id, an allergy or
    diet code, a budget or an address. An eval scans the values, not just the keys.
    *Checked by:* `safety-summaries-no-profile-values`.
*   **G8. Replay is safe.** A replayed step produces no duplicate `message_id` and changes no
    state twice.
    *Checked by:* `robust-replay-no-duplicate-message-id`,
    `tools-redelivered-accept-no-second-order`.
*   **G9. Shortfalls are bounded.** Once the courier is booked, a store can report missing
    items and offer up to 2 substitutes per line, each costing at most the line it
    replaces. A substitute is applied only if it passes the allergen check for every
    member. Lines with no pick within `SHORTFALL_REPLY_SECONDS`, or before the courier
    reports `picked_up`, are dropped and the order continues.
    *Checked by:* `safety-shortfall-allergen-substitute-never-applied`,
    `robust-shortfall-unanswered-drops-items`.
*   **G10. A standard A2A client can negotiate with a business agent** over an internal-only
    endpoint: request a quote, counter, reject or cancel, and read the task. It cannot
    accept, so no order can be placed from outside.
    *Checked by:* `safety-a2a-accept-refused`, `task-a2a-negotiation`.
*   **G11. Architecture changes are explicit.** Every change this design needs in
    `architecture.md` is listed in one table (§14) and applied in the same PR.
    *Checked by:* the P41 review step: every A-row has a matching edit in `architecture.md`.

## Non-Goals

This is the only list of non-goals; §15 and "Out of scope (implementation)" point here.

*   **Redis Streams.** The architecture removed Redis (decision O1). It stays a later
    scaling option.
*   **Identity and authorization details.** The OpenFGA model and Keycloak delegation belong
    to identity-authz. Tokens inside messages are listed there as a possible later
    extension.
*   **Negotiation strategy and prompts.** How an agent decides to counter, which offer to
    pick and what the model is told belong to the agents design.
*   **Tool contracts** (adapters), **message wording and persona UX** (experience), and
    **eval fixture data** beyond the cases in this design (evals).
*   **External business agents and any public A2A endpoint.** The demo endpoint is reachable
    only on the Docker network and over SSH.
*   **Cancelling a whole order after placement**, and cancelling a courier booking.
*   **Shortfalls reported by real vendors** (Square, Kroger). At launch a shortfall is
    raised only in mock mode, from the control panel.
*   **The other A2A methods:** `SendStreamingMessage`, `SubscribeToTask`, `ListTasks`,
    `GetExtendedAgentCard` and push notifications.
*   **Raising an order total during a shortfall.** A substitute that would cost more than
    the line it replaces is never offered, so the shortfall flow never needs a new
    approval.
*   **Changes to `evals/thresholds.yaml`.**

## Open Questions

*   **Q1 (resolved 2026-10-31).** The A2A methods are hand-rolled with pydantic, not built
    on the A2A Python SDK (Alternative T).
Q2–Q4 were answered on 2026-10-31 from the A2A v1.0.1 release (tag `v1.0.1`, 2026-05-28):
`specification/a2a.proto`, which spec §1.4 makes the single normative definition of the data
objects, and `docs/specification.md` for the bindings. The JSON-RPC codes are the same in
v1.0.0; v1.0.1 changed only gRPC and HTTP status mappings, which this design does not use.

*   **Q2 (resolved). JSON-RPC error codes** (spec §5.4 and §9.5):

    | Error | Code | `ErrorInfo.reason` |
    |---|---|---|
    | `TaskNotFoundError` | -32001 | `TASK_NOT_FOUND` |
    | `TaskNotCancelableError` | -32002 | `TASK_NOT_CANCELABLE` |
    | `UnsupportedOperationError` | -32004 | `UNSUPPORTED_OPERATION` |
    | `VersionNotSupportedError` | -32009 | `VERSION_NOT_SUPPORTED` |
    | `JSONParseError` | -32700 | none (standard JSON-RPC) |
    | `InvalidRequestError` | -32600 | none |
    | `MethodNotFoundError` | -32601 | none |
    | `InvalidParamsError` | -32602 | none |
    | `InternalError` | -32603 | none |

    `error.data` is an **array** of objects, each with an `@type` key (ProtoJSON `Any`). A2A
    errors carry `{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": <above>,
    "domain": "a2a-protocol.org"}`; spec §11.6 makes this a MUST for HTTP and §9.5 a SHOULD
    for JSON-RPC, and this endpoint always sends it. Spec §3.1.5 confirms that cancelling a
    terminal task returns `TaskNotCancelableError`.
*   **Q3 (resolved). `A2A-Version`** (spec §3.6, §14.2.1): the value is `Major.Minor`. A
    client may send it as a header or as a request parameter. The endpoint accepts only
    `1.0`. An empty or missing value MUST be read as `0.3` (§3.6.2), so it gets
    `VersionNotSupportedError`, like any other value. The eval runner's client always sends
    `A2A-Version: 1.0`.
*   **Q4 (resolved). Card security fields** (proto `AgentCard` fields 8 and 9). JSON uses
    camelCase (spec §5.5):

    ```json
    "securitySchemes": {"bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}},
    "securityRequirements": [{"schemes": {"bearer": {"list": []}}}]
    ```

    The spelling `"security"` in the sample card (spec §8.5) is the older form; the proto wins.
    The proto also makes `provider.url` required, so the card sets it (§2).

Already confirmed (2026-10-31, `a2a-protocol.org/latest/specification`): the card path
`/.well-known/agent-card.json`; the method names `SendMessage`, `GetTask` and `CancelTask`;
`configuration.returnImmediately` on `SendMessage`, with `TASK_STATE_SUBMITTED` returned
for a new task; and the enum spellings `TASK_STATE_*` and `ROLE_USER` / `ROLE_AGENT`.

## Critical User Journeys (CUJs)

These journeys use the architecture's setup: household `us-1`, the grocery agents
`grocery:kroger-us`, `grocery:square-us` and `grocery:mock-us`, `MODE=mock` and
`MODEL_MODE=stub`. They describe protocol behavior only; architecture CUJs 1 to 7 still
hold. In this document, **P-CUJ n** means a journey below and **A-CUJ n** means an
architecture journey.

### CUJ 1: A weekly negotiation, every message visible

*   **Given**: the household agent has a draft order and a shopping list.
*   **When**: it sends `quote_request` to the three grocery agents, counters the cheapest
    offer once, and accepts the reply.
*   **Then**:
    *   Each store's `offer` lists every requested item with an offered item (or none, for
        a partial offer), quantity, price, and `substituted_for` where it substitutes.
    *   Each message has one `message` trace row and a summary line, and the live view shows
        each store's exchange as its own lane, with replies linked to requests.
    *   The store's `rationale` appears in the view as quoted text; the code acts only on
        the structured data.
    *   The quote moves `open → countered → accepted`, and the order is placed once.

### CUJ 2: An offer expires before it is accepted

*   **Given**: an open quote whose `expires_at` has passed, for example because the run
    waited for approval (A-CUJ 2).
*   **When**: the household agent sends `accept`.
*   **Then**: the store's conditional update finds the quote past its expiry, sets it
    `expired`, and answers `reject {reason: expired}`. No order is placed. The household
    agent sends one new round of `quote_request`. An approval already given covers the new
    offer if its total is at or below the approved total.

### CUJ 3: A step crashes and its message is delivered again

*   **Given**: one of two crash windows.
    *   The store's `accept` step crashes before its final transaction commits.
    *   The store's courier-round step crashes after `place_order` succeeds but before its
        final transaction commits.
*   **When**: the row is claimed again (architecture §4 stuck-row rule).
*   **Then**:
    *   In the first window nothing was committed: the quote is still `open` or
        `countered`, and the replay runs the accept cleanly, with the same message ids.
    *   In the second window the replay calls `place_order` again with the same
        idempotency key, and the guard returns the stored result without calling the
        adapter.
    *   In both, exactly one order exists and no message id is inserted twice.

### CUJ 4: A message comes from the wrong sender

*   **Given**: a negotiation between `household:us-1` and `grocery:square-us`.
*   **When**: a different household's agent sends `accept` for that quote, or a store sends a
    type only a household may send.
*   **Then**: the row is marked `failed` on the first attempt, an `error` trace is written,
    the sender gets `protocol_error {code: wrong_party}` (the other household) or
    `{code: wrong_sender_class}` (the store), the quote is unchanged and the run continues.

### CUJ 5: A store is short after the courier is booked; the family picks a substitute

*   **Given**: an order in `courier_booked`, and the operator raises a `shortfall` mock
    event from the control panel.
*   **When**: the store sends `shortfall_notice` with the missing lines and up to 2
    substitutes per line, each costing at most the line it replaces and already filtered
    for the household's allergens and diets.
*   **Then**:
    *   Every household member with a WhatsApp identity gets a message in plain language
        with reply buttons, one message per missing line.
    *   The first valid reply per line wins; a later tap on that line gets "Already
        decided."
    *   When every line has a pick, the household agent calls `authorize_amendment`, which
        re-checks allergens for every member, and sends `shortfall_decision`.
    *   The store calls `amend_order` once; the order lines and total change once, and the
        pantry later updates from the final lines.

### CUJ 6: Nobody answers the shortfall

*   **Given**: a `shortfall_notice` has been sent.
*   **When**: `SHORTFALL_REPLY_SECONDS` passes, or the courier reports `picked_up`, first.
*   **Then**: the shortfall closes, lines without a pick are dropped, the order continues
    and completes, and the family is told what was dropped. If every line was missing, the
    order ends `failed`, the run ends `status=error, reason=nothing_to_deliver`, and the
    family is told.

### CUJ 7: A standard A2A client negotiates with a business agent

*   **Given**: an operator on the VM (over SSH) with an A2A client and `A2A_DEMO_TOKEN`.
*   **When**: the client reads `grocery:mock-us`'s agent card, sends `SendMessage` with a
    `quote_request` data part, polls `GetTask`, then sends a `counter_offer` and finally an
    `accept`.
*   **Then**:
    *   The quote request starts an `a2a_demo` run for the seeded `a2a-demo` household, and
        the task moves from `TASK_STATE_SUBMITTED` to `TASK_STATE_INPUT_REQUIRED` with the
        offer as an artifact.
    *   Every message appears in the live view like internal traffic.
    *   The `accept` gets JSON-RPC `UnsupportedOperationError` (-32004). No row is
        inserted, an `error` trace with `reason=accept_not_allowed` appears in the lane, the
        task stays `TASK_STATE_INPUT_REQUIRED`, and no order is placed.
    *   The same request through Caddy from the internet returns 404.

### CUJ 8: A household step is replayed after a restart

*   **Given**: the household agent's host restarts after a decision node was checkpointed
    but before the step's final transaction committed.
*   **When**: the graph resumes and runs the send step again.
*   **Then**: it sends the messages stored in the checkpoint, with the same ids, and no
    message id is inserted twice.

## Technical Plan (Architecture)

**References.** A bare "§N" means a section of this design. Architecture sections are
always written "architecture §N", and architecture implementation items "architecture item
N". Everything this design changes in `architecture.md` is listed in §14 and applied in the
same PR.

### 0. Brief decisions this design changes

The owner approved these changes to `concepts_brief.md` on 2026-10-31:

| Brief | This design | Why |
|---|---|---|
| Brief §4.5: a token in every message | No tokens in messages; each agent uses its own access, and receivers run the sender check (§3, §6) | A household token held by a store could be replayed to act for the family (Alternative E) |
| Brief D6: Redis Streams as the transport | The architecture's Postgres outbox (architecture §4) | The architecture removed Redis (Alternative A) |
| An outside `accept` is refused with `TASK_STATE_REJECTED` | JSON-RPC `UnsupportedOperationError`; the task keeps its state (§11) | Task state is computed from `quotes`, and the quote is still open |
| `SendMessage` always returns `TASK_STATE_SUBMITTED` | Only when `returnImmediately` is true; otherwise it waits up to 10 s (§11) | The spec requires the wait when the flag is false |
| One lock key per conversation | Architecture §4's lock keys (§7) | They are one-to-one with a negotiation, and already specified |
| After `picked_up`, no change is accepted | No substitute after `picked_up`; drops allowed until `delivered` (§10) | The order record must match what is delivered |
| `protocolVersion` pinned to `1.0.0` | `1.0` (§4) | The spec writes versions as `Major.Minor` |
| Replay safety includes a body hash that detects divergence | No body hash (§7) | The commit rule (§7) means a message id is never inserted twice with different bodies (Alternative S) |

### 1. Two bindings, one table

```text
                     agents host (one process, port 8003, never proxied by Caddy)
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │ household / consumer graphs        business handlers        sweeper          │
 │        │  ▲                             │  ▲                    │            │
 │        ▼  │                             ▼  │                    ▼            │
 │   protocol.check()  ◄── every claimed row runs through it first ──►          │
 │        │                                                                     │
 │   outbox.send(): validate body, render summary, insert, trace, notify        │
 │        │                                         ▲                           │
 │        │                     ┌───────────────────┴──────────────┐            │
 │        │                     │ /a2a/<agent_id>  (JSON-RPC 2.0)  │◄── A2A demo│
 │        │                     │ SendMessage GetTask CancelTask   │   client   │
 │        │                     │ from_a2a_message / to_a2a_*      │   (SSH or  │
 │        │                     └──────────────────────────────────┘   Docker)  │
 └────────┼─────────────────────────────────────────────────────────────────────┘
          ▼
 Postgres agent_messages ──► message trace row (summary, causation_id) ──► live view
```

*   **Outbox binding (all internal traffic).** Architecture §4, unchanged in mechanism:
    insert, `LISTEN/NOTIFY`, claim, step, finish. This design adds a check before each
    step (§6), a summary at send time (§5, §8), a non-retryable error path (§6), and a
    commit rule for quote and shortfall changes (§7).
*   **HTTP binding (demo only).** The `agents` host serves A2A JSON-RPC for grocery and
    restaurant agents. It converts each A2A request into an ordinary `agent_messages` row
    through the same `outbox.send()`. The business handler processes both bindings' rows
    the same way, and the live view shows both the same way (§11).
*   **Stored form.** The architecture's columns are the record. A2A shapes exist only in
    `services/common/protocol/a2a.py` (`to_a2a_message`, `from_a2a_message`, `to_a2a_card`,
    `task_state`), used by the HTTP binding and the panel.

### 2. Agent cards

`agent_cards` (architecture item 9) stays the directory. Internal discovery stays
`directory.find(class, region, zone_id, currency)` (architecture §4); agents never fetch
each other's A2A cards. `to_a2a_card(card)` builds the A2A form on request:

| A2A card field | Source |
|---|---|
| `name` | `agent_id`, for example `grocery:mock-us` |
| `description` | `config.description` (seeded) |
| `supportedInterfaces` | One entry: `url = http://agents:8003/a2a/<agent_id>`, `protocolBinding = "JSONRPC"`, `protocolVersion = "1.0"` |
| `version` | `config.card_version` (seeded, default `"1.0.0"`), §4 |
| `provider` | `organization`: the owner org's display name, from `owner_ref`; `url`: the agent's `supportedInterfaces` URL (required by the proto, Q4) |
| `capabilities` | `streaming: false`, `pushNotifications: false`, one extension `urn:kitchenless:on-behalf-of:v1` whose `params.on_behalf_of` is the card's `owner_ref` (`org:<id>`) |
| `defaultInputModes` / `defaultOutputModes` | `["application/json"]` / `["application/json", "text/plain"]` |
| `skills` | One skill per class: `grocery_quote` or `dinner_quote`, listing the message types the card accepts from an outside caller (§11) |
| `securitySchemes` | `{"bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}}` (Q4) |
| `securityRequirements` | `[{"schemes": {"bearer": {"list": []}}}]` (Q4) |

*   The card is served at `GET /a2a/<agent_id>/.well-known/agent-card.json`, only for
    enabled grocery and restaurant cards. Any other `agent_id` returns 404.
*   **Identity and on-behalf-of.** A business card acts for its org (`org:<id>`). Consumer
    and household cards act for a user or household (architecture §3) and are never
    exposed as A2A cards.
*   **New class `external`.** One seeded card, `external:a2a-demo`, with
    `host_process='external'` and `owner_ref='system'`. Rows addressed to it are inserted as
    `processed` and are never claimed (§11). `directory.find` never returns it.

### 3. The envelope

Every message is one `agent_messages` row. The envelope is its columns:

| Field | Rule |
|---|---|
| `message_id` | Architecture §3: `uuid5(<cause>, "<ordinal>")`. HTTP: `uuid5(NS_A2A, from_agent + "\|" + A2A messageId)` |
| `run_id` | Required. Business agents adopt the requester's `run_id` (architecture §3) |
| `conversation_id` | Required for every type except the outbox-only people types (rows 1–5, 18, 20 of §5). A negotiation or a store-to-courier exchange gets `uuid5(<opening message_id>, "conversation")`, set by the side that opens it. A shortfall gets `uuid5(shortfall_id, "conversation")`, and every shortfall message carries it |
| `causation_id` (**new**) | The inbound `message_id` that caused this message; `null` when the cause is a webhook, a timer or the panel (the body then names its source event id) |
| `from_agent`, `to_agent`, `to_host` | Architecture §4. Over HTTP, `from_agent` is always `external:a2a-demo`, taken from the bearer secret, never from the body |
| `type` | One of the catalog types (§5) |
| `body` | JSON matching the type's schema, always with `schema_version` (§4) |
| `created_at`, `available_at`, `status`, `attempts`, `claimed_at`, `processed_at`, `last_error` | Architecture item 10, unchanged |

*   **No expiry on the envelope.** Only quotes expire, and `quotes.expires_at` is the record
    (§7). Any `expires_at` in a body must equal it.
*   **Threading.** `conversation_id` groups an exchange; `causation_id` links each reply to
    its request. The live view draws both (§8).
*   **No tokens.** A message carries no access token. Each agent calls guarded tools with
    its own access (identity-authz), and each receiver runs the sender check (§6). This
    supersedes brief §4.5 (§0).

### 4. Versioning

| Axis | Carried in | Current value | Who bumps it | Compatibility rule | Unknown value |
|---|---|---|---|---|---|
| A2A `protocolVersion` | Card interface; `A2A-Version` header | `1.0` | A code change, by hand | Pinned. The endpoint accepts only `A2A-Version: 1.0`; an empty value means `0.3` (Q3) | JSON-RPC `VersionNotSupportedError` (-32009) |
| Card `version` | `agent_cards.config.card_version` | `1.0.0` | The seed, when a card's skills or accepted types change | Informational only | Not checked |
| Payload `schema_version` | Every `body` | `1` for every type | The protocol module, only for a breaking change to a type | Fields are only added, each with a default, without a bump. Receivers ignore unknown fields. A breaking change gets a new version, and the receiver must know it | Rejected by schema validation (C2) with `protocol_error {code: unsupported_version}`, never a retry |
| LangGraph `state_version` | Graph state | Architecture §4 | Architecture §4 | Architecture §4 | Architecture §4 |

Old code that receives an unknown `type` sends `protocol_error {code: unknown_type}`. Old
sweeper code skips timer kinds it does not know and leaves them `pending`.

### 5. Message catalog

**Eight shared schemas** live in `services/common/protocol/schemas.py`, used by both
bindings. Money is integer minor units plus currency (architecture §3). Every `order_id` is
required where an order exists (architecture §13). Field types are in P2.

| Schema | Fields |
|---|---|
| **S1 QuoteRequest** | `order_id`, `kind` (`grocery` \| `dinner`), `items[]` (`item`, `qty`, `unit`), `constraints` (`allergens[]`, `diets[]`: codes from architecture §5 only), `currency`, `zone_id`, `window` (`start`, `end`), `reply_by` |
| **S2 Quote** (`offer`, `counter_offer`) | `order_id`, `quote_id`, `round`, `lines[]` (`line`, `requested_item`, `offered_sku` or null, `offered_name` or null, `qty`, `unit_minor`, `substituted_for` or null), `total_minor`, `currency`, `expires_at` (offer only), `final` (offer only: true when `round ≥ 1`, because the seller can no longer counter), `rationale` (offer only, architecture §4 rules). A line with `offered_sku = null` is missing: a partial offer. |
| **S3 QuoteDecision** (`accept`, `reject`, `cancel`) | `order_id`, `quote_id` (always required), `items_hash` and `total_minor` (accept only), `reason` (code: `expired`, `closed`, `items_changed`, `price`, `no_courier`, `already_placed`, `buyer_cancelled`, `other`) |
| **S4 OrderUpdate** (`order_confirm`, `delivery_booked`, `order_status`) | `order_id`, `state` (an order state, architecture §8), `lines[]` (order_confirm only: the order's current lines), `total_minor`, `currency`, `delivery_agent_id`, `window`, `reason` (code, optional) |
| **S5 Delivery** (`delivery_quote_request`, `delivery_quote`, `delivery_request`) | `order_id`; request: `zone_id`, `window`, `pickup_agent_id`; quote: `quote_id`, `window`, `price_minor`, `currency`, `expires_at`, `rationale`; delivery request: `quote_id`, `window`, `zone_id`, `address_ref` |
| **S6 Shortfall** (`shortfall_notice`, `shortfall_relay`, `shortfall_decision`) | `order_id`, `shortfall_id`; notice and relay: `lines[]` (`line`, `sku`, `name`, `qty_missing`, `line_total_minor`, `substitutes[]` of at most 2: `sku`, `name`, `qty`, `unit_minor`), `reply_by`; decision: `picks` (`line` → substitute `sku` or `"drop"`) |
| **S7 ProtocolError** | `ref_message_id`, `code` (§6), `detail` (template text, at most 200 characters) |
| **S8 Internal** (people, system and channel types) | One small model per type, listed in P2. None carries a profile to a business agent. |

**The catalog.** "Outbox only" types never cross the HTTP binding. Summary slots in
`{braces}` come only from the allowlist in §8. The **party rule** is what check C5 (§6)
enforces: which stored row the message refers to, and which of its columns the sender and
receiver must equal. "HH(order)" means `household:<orders.household_id>`; "member of
HH(run)" means a consumer card whose `owner_ref` user is a row of `household_members` for
`runs.household_id`.

| # | Type | Sender → receiver classes | Schema | Summary template | Party rule (C5) | State effect | Used in |
|---|---|---|---|---|---|---|---|
| 1 | `user_message` | `channel:*` → `consumer` (outbox only) | S8 | `Message from a member` | To: the consumer of `runs.trigger.user_id` | Starts or resumes the consumer graph | A-CUJ 1.1 |
| 2 | `start_weekly_order` | `consumer` → `household` (outbox only) | S8 | `Start weekly order` | From: member of HH(run). To: `household:<runs.household_id>` | Household graph starts | A-CUJ 1.2 |
| 3 | `start_dinner` | `consumer` → `household` (outbox only) | S8 | `Start dinner order` | As row 2 | Household graph starts | A-CUJ 6 |
| 4 | `constraints_request` | `household` → `consumer` (outbox only) | S8 | `Constraints requested for order {order}` | From: HH(order). To: member of HH(run) | None | A-CUJ 1.2 |
| 5 | `constraints` | `consumer` → `household` (outbox only) | S8 | `Constraints received ({n_codes} codes)` | From: member of HH(run). To: HH(order) | Graph state only | A-CUJ 1.2 |
| 6 | `quote_request` | `household`, `external` → `grocery`, `restaurant` | S1 | `Quote request for order {order}: {n_items} items, reply by {reply_by}` | Order. From: HH(order), or `external:a2a-demo` when `runs.kind = a2a_demo`. To: any card that passes C3 | Seller inserts a `quotes` row (`open`) | P-CUJ 1, 7 |
| 7 | `offer` | `grocery`, `restaurant` → `household`, `external` | S2 | `Offer {total}: {n_offered} of {n_lines} items, {n_subs} substitutes, expires {expires}` | Quote. From: `seller_agent_id`. To: `buyer_agent_id` | None (the row changed in the same commit) | P-CUJ 1, 2, 7 |
| 8 | `counter_offer` | `household`, `external` → `grocery`, `restaurant` | S2 | `Counter {total} on quote {quote}, round {round}` | Quote. From: `buyer_agent_id`. To: `seller_agent_id` | Seller: → `countered` or `rejected` (§7) | P-CUJ 1, 7 |
| 9 | `accept` | `household` → `grocery`, `restaurant` | S3 | `Accept quote {quote} at {total}` | As row 8 | Seller: → `accepted` or `expired` | P-CUJ 1, 2, 3 |
| 10 | `reject` | Either side of a quote | S3 | `Reject quote {quote}: {reason}` | Quote. From one of `buyer_agent_id`, `seller_agent_id`; to the other | Buyer's reject: → `rejected`. Seller's reject: none (already changed) | P-CUJ 2 |
| 11 | `cancel` | `household`, `external` → `grocery`, `restaurant` | S3 | `Cancel quote {quote}: {reason}` | As row 8 | Seller: → `rejected` if the order is not `placed` | A-CUJ 3 |
| 12 | `order_confirm` | `grocery`, `restaurant` → `household` | S4 | `Order {order} {state}: {n_lines} lines, {total}` | Order. From: `orders.seller_agent_id`. To: HH(order) | None (`place_order` or `amend_order` changed it) | A-CUJ 1.4, P-CUJ 5 |
| 13 | `delivery_quote_request` | `grocery`, `restaurant` → `delivery` | S5 | `Courier quote request for order {order}, window {window}` | Order. From: `orders.seller_agent_id`. To: any card that passes C3 | None | A-CUJ 1.4 |
| 14 | `delivery_quote` | `delivery` → `grocery`, `restaurant` | S5 | `Courier quote {price}, window {window}` | Courier quote. From: `seller_agent_id` (the courier). To: `buyer_agent_id` (the store) | Courier inserts its `quotes` row in the same commit | A-CUJ 1.4 |
| 15 | `delivery_request` | `grocery`, `restaurant` → `delivery` | S5 | `Delivery request for order {order}, window {window}` | Order. From: `orders.seller_agent_id`. To: `orders.delivery_agent_id` | None (`book_delivery` changes the order) | A-CUJ 1.5 |
| 16 | `delivery_booked` | `delivery` → `grocery`, `restaurant`, `household` | S4 | `Courier booked for order {order}, window {window}` | Order. From: `orders.delivery_agent_id`. To: `orders.seller_agent_id` or HH(order) | None | A-CUJ 1.5 |
| 17 | `order_status` | `system:api` → `household` (outbox only) | S4 | `Order {order} is {state}` | Order. To: HH(order) | None (`apply_delivery_status` changed it) | A-CUJ 1.6 |
| 18 | `approval_decision` | `system:api`, `system:sweeper` → `household` (outbox only) | S8 | `Approval {status} for order {order}` | Order. To: HH(order) | Resumes the graph | A-CUJ 2, 3 |
| 19 | `quote_round_closed` | `system:sweeper` → `household`, `grocery`, `restaurant` (outbox only) | S8 | `Quote round closed for order {order}` | Order. To: HH(order), or `orders.seller_agent_id` for the courier round | Graph or handler picks from open quotes | A-CUJ 1 |
| 20 | `whatsapp_out` | `consumer`, `household`, `system:*` → `channel:*` (outbox only) | S8 | `WhatsApp message to a member` | From: `household:<runs.household_id>`, a member of HH(run), or `system:*` | None | All |
| 21 | `protocol_error` (**new**) | Any agent → the sender of the refused message (never to `system:*` or `channel:*`) | S7 | `Refused {ref}: {code}` | Message. `ref_message_id` exists, with `from_agent` = this message's receiver and `to_agent` = this message's sender | Receiver reacts by what was refused (§6) | P-CUJ 4 |
| 22 | `shortfall_detected` (**new**) | `system:api` → `grocery`, `restaurant` (outbox only) | S8 | `Shortfall raised for order {order}: {n_lines} lines` | Order. To: `orders.seller_agent_id` | None | P-CUJ 5 |
| 23 | `shortfall_notice` (**new**) | `grocery`, `restaurant` → `household` | S6 | `Shortfall on order {order}: {n_lines} lines missing, {n_subs} substitutes` | Order. From: `orders.seller_agent_id`. To: HH(order). `shortfall_id` = `uuid5(order_id, "shortfall")` | Household opens the `shortfalls` row | P-CUJ 5, 6 |
| 24 | `shortfall_relay` (**new**) | `household` → `consumer` (outbox only) | S6 | `Ask a member about shortfall {shortfall}` | Shortfall. From: HH(order). To: member of HH(run) | None | P-CUJ 5 |
| 25 | `shortfall_closed` (**new**) | `system:api`, `system:sweeper` → `household` (outbox only) | S8 | `Shortfall {shortfall} closed: {reason}` | Shortfall. To: HH(order) | None (`api` or the sweeper closed the row in the same commit) | P-CUJ 5, 6 |
| 26 | `shortfall_decision` (**new**) | `household` → `grocery`, `restaurant` | S6 | `Shortfall {shortfall}: {n_subs} substituted, {n_drop} dropped` | Shortfall. From: HH(order). To: `shortfalls.seller_agent_id` | Seller calls `amend_order` | P-CUJ 5, 6 |

*   **Who sets `orders.seller_agent_id`.** `authorize_spend` sets it from the chosen quote
    (architecture §5 already requires "`authorized` with this seller"; §14 A6 records it).
*   **Not added: `delivery_status`.** Courier status reaches the platform as a webhook or
    the mock courier loop, through `api` `apply_delivery_status` (architecture §8), which
    already sends `order_status`. The delivery agent never sees it, so a
    `delivery_status` message would have no sender.
*   **Which messages carry `address_ref`:** only `delivery_request` (architecture G5).
*   **Rationale as an A2A part.** In A2A form, a message has one data part (`{type,
    body}`, without `rationale`) and, when present, one text part holding the rationale.
    Code acts only on the data part. The household graph strips `rationale` before any
    model prompt (architecture item 29).

### 6. The sender check and protocol errors

`protocol.check(conn, msg)` runs in `consumer.py` after the claim and before the handler.
The predicates run in order; the first failure raises `ProtocolError(code, detail)`.

| # | Predicate | Code |
|---|---|---|
| C1 | `type` is in the catalog | `unknown_type` |
| C2 | `body` validates against the type's schema. A `schema_version` other than `1` fails with its own code | `invalid_body`, or `unsupported_version` |
| C3 | (`type`, sender class, receiver class) is a row of the catalog's sender → receiver column. `system:*` and `channel:*` senders pass only for their own types | `wrong_sender_class` |
| C4 | Every quote, order, shortfall or message the body names exists, and its `run_id` equals the message's `run_id` | `unknown_reference` |
| C5 | The party rule for the type (§5) holds | `wrong_party` |

C4 and C5 read only rows named in the body, by primary key, plus one `household_members`
row for people types.

Over HTTP, the endpoint fills its own fields first (§11), then runs C1–C3 before the
insert, so an outside client gets a JSON-RPC error at once.

**`ProtocolError` is only for inbound messages.** An agent's own outgoing body that fails
validation in `outbox.send()` raises `OutboundInvalid`, which follows the architecture's
normal retry and poison rules, because the bug is in the sender's code.

**What happens on a `ProtocolError`** (refines architecture §4 step 5):

1.  The row becomes `failed` (`attempts` unchanged) with `last_error = <code>`. There is
    no back-off and no retry, and the poison-message rule does not apply, so the run is
    not ended.
2.  An `error` trace row: `where=protocol`, `reason=<code>`, `attempt` (the row's
    attempts), plus `ref_message_id`.
3.  `protocol_error` goes back to the sender, with id `uuid5(<refused message_id>,
    "protocol_error")`, unless the sender is `system:*` or `channel:*`, or the refused
    message is itself a `protocol_error`. This prevents error loops.
4.  The receiver of a `protocol_error` reacts by what was refused (`ref_message_id`'s
    type):
    *   a `quote_request` or `counter_offer`: the household drops that seller from the
        run, as if its offer never came;
    *   an `accept` or any later order message: the run ends with `status=error,
        reason=protocol_error`;
    *   a `shortfall_decision`: the household treats every line as dropped and sends the
        decision again under a new id (drops only);
    *   anything else, or at a business agent: a `decision` trace and nothing else.

Any other exception keeps the architecture's retry, poison and recovery rules unchanged.

### 7. The negotiation state machine

The `quotes` row (architecture item 11) is the record.

**Commit rule** (refines architecture §4 step execution). A handler reads the rows it
needs, decides, and returns every `quotes` and `shortfalls` change as a conditional update
in its step result. The consumer applies those updates in the step's final transaction,
together with the outgoing messages, the timer changes and the `processed` mark. Each
update is conditional on the expected `status` and, for quotes, the expected `round`:

```sql
UPDATE quotes SET status = 'accepted'
WHERE quote_id = $1 AND status IN ('open', 'countered') AND round = $2
  AND expires_at > now() AND total_minor = $3
```

If any conditional update in the final transaction changes no row, the whole transaction
rolls back and the step is retried under the architecture's normal rule; on the retry the
handler sees the new row state and takes the arrival table's path. A crash anywhere before
the commit leaves nothing changed, so the replay runs the step cleanly with the same
message ids (P-CUJ 3). Tool calls made during the step (`place_order`, `amend_order`) are
covered by their idempotency keys.

```text
                    counter_offer: seller counters or agrees
     ┌──────┐ ─────────────────────────────────────────────► ┌───────────┐
     │ open │                                                │ countered │──┐ counter_offer:
     └──┬───┘                                                └─────┬─────┘◄─┘ seller agrees
        │ accept (buyer, before expires_at) ┌──────────┐ accept      │
        ├──────────────────────────────────►│ accepted │◄────────────┤
        │                                   └────┬─────┘             │
        │ accept after expires_at ┌─────────┐    │ reject {no_courier} │
        ├────────────────────────►│ expired │◄───┼─────────────────────┤ accept after
        │                         └─────────┘    │ (seller), cancel    │ expires_at
        │ reject / cancel (buyer),               │ before placed       │
        │ counter refused (seller)               ▼ (buyer)             │
        └─────────────────────────────────► ┌──────────┐ ◄─────────────┘
                                            │ rejected │
                                            └──────────┘
```

*   **`open`**: the seller's first offer stands. `expires_at = now() + OFFER_TTL_SECONDS`.
*   **`countered`**: the seller has answered a buyer's `counter_offer`, either with a new
    total (`counter`, only while `round` is 0) or by taking the buyer's total (the
    `respond_to_counter` option called "agree" in this design). Either way the row gets the
    new total, `round = round + 1` and a new `expires_at`, and the seller replies `offer`
    with `final = true` when `round ≥ 1`. A second `counter_offer` on a `countered` quote
    allows only "agree" or `reject` (architecture §4).
    *   **Change to architecture item 30 (A8).** The architecture has the seller's
        "agree" set the quote to `accepted`. That puts the quote in `accepted` before the
        buyer commits, and the buyer's real `accept` then looks like a duplicate and
        starts no courier round. Here only the buyer's `accept` sets `accepted`.
*   **`accepted`**: set only by the seller's `accept` handler, with the update above. The
    same final transaction sends `delivery_quote_request` to the in-zone couriers and
    creates the courier `quote_round` timer (architecture item 30). The courier-round step
    checks the quote is still `accepted` before calling `place_order`.
*   **`expired`**: set by the `accept` or `counter_offer` handler when the stored quote is
    `open` or `countered` and `expires_at <= now()`. It replies `reject {reason:
    expired}`. No sweeper expires quotes; a quote nobody answers stays past its
    `expires_at` (architecture §8: no holds).
*   **`rejected`**: from the buyer's `reject` or `cancel` (before `placed`), the seller's
    `reject` of a counter, or the seller's `reject {reason: no_courier}` when the courier
    round finds no courier. A new column `quotes.closed_by` (`buyer` or `seller`) records
    which side closed it, so the task state never changes once terminal.

**Arrival table.** Rows are checked top to bottom within a message type; the first match
wins. "Live" means status `open` or `countered` and `expires_at > now()`.

| Message | Stored state | Action |
|---|---|---|
| `offer` | The household's quote round already closed, or it already chose | Ignore; `decision` trace `late_offer` |
| `counter_offer` | `open`/`countered` and past `expires_at` | Set `expired`; `reject {reason: expired}` |
| `counter_offer` | `expired` | `reject {reason: expired}` |
| `counter_offer` | `accepted` or `rejected` | `reject {reason: closed}` |
| `accept` | `open`/`countered` and past `expires_at` | Set `expired`; `reject {reason: expired}` |
| `accept` | `expired` | `reject {reason: expired}` |
| `accept` | `rejected` | `reject {reason: closed}` |
| `accept` | `accepted` | A second, different `accept` for the same quote: ignore; `decision` trace `duplicate_accept`. (A redelivered row never reaches here, because nothing was committed: commit rule.) |
| `accept` | Live, `items_hash` differs | `reject {reason: items_changed}`; quote unchanged |
| `accept` | Live, `total_minor` differs | `reject {reason: price}`; quote unchanged |
| `cancel` | Order `placed` or later | `reject {reason: already_placed}` (whole-order cancel is a non-goal) |
| `cancel` | `rejected` or `expired` | Ignore |
| `reject` | `rejected` or `expired` | Ignore |
| `delivery_quote` | The store's courier round already closed | Ignore; `decision` trace `late_courier_quote` |
| `quote_round_closed` | The graph already moved past choosing, or the quote is no longer `accepted` | Ignore |
| `order_status` | Order already terminal | Ignore |
| `shortfall_closed` | The `shortfalls` row is `applied` | Ignore |
| `shortfall_decision` | The seller already applied this `shortfall_id` | No-op; the `amend_order` key returns the stored result |

**The household's rule for a seller's `reject`.**

*   `expired`: send one new round of `quote_request` (re-quote limit below).
*   `no_courier`: take the next valid offer from the same round if one is still live;
    otherwise end the run with `status=error, reason=no_courier`.
*   `already_placed`: ignore, with a `decision` trace.
*   Any other reason: drop that seller from the run and take the next valid offer, as
    architecture §8 does when an offer is invalid.

**Re-quote.** After a `reject {reason: expired}` the household agent moves the order
`authorized → quoting` (a new allowed transition, A7) and sends one new round of
`quote_request` (A-CUJ 2). The new `authorize_spend` call uses a key scoped by the new
`quote_id` (A16). An `approved` approval covers the re-quoted offer if its total is at or
below the approved amount; a higher total needs a new approval. Expiries are counted only
after any approval is decided. A second expiry in the same run ends it with
`status=error, reason=offer_expired`. `OFFER_TTL_SECONDS = 600` covers one quote round
(30 s) plus two counter rounds with room to spare; a wait for approval is covered by the
re-quote, not by a longer lifetime.

**Ordering.** Lock keys are architecture §4's: `thread_id` for graphs; `quote_id`, or
`order_id` when there is no quote, for handlers. In a negotiation the quote and the
conversation are one-to-one, so this is one lock per conversation.

**Replay safety.**

*   Graphs send only from checkpointed state. A decision node writes the outgoing messages
    into graph state; the step's final transaction sends exactly those. A replay after
    the decision checkpoint re-sends identical bodies (P-CUJ 8).
*   Business handlers are stateless. Because nothing commits before the final transaction,
    a replayed handler step has no earlier row to conflict with.
*   Ids for the new types: `quote_id = uuid5(<quote_request message_id>, "quote")`; a
    courier quote id is `uuid5(<delivery_quote_request message_id>, "quote")`;
    `shortfall_id = uuid5(order_id, "shortfall")`, so an order has at most one shortfall.

**Quotes as A2A tasks.** `taskId = quote_id`, `contextId = conversation_id`, and `run_id`
travels in message `metadata`. Only `GetTask` reads a task state, and only for tasks whose
buyer is `external:a2a-demo`. An outside task can never be accepted, so the function
`task_state(quote, request_status, now)` covers only the states an outside reader can
reach; nothing stores a task state.

| Task state | Computed when |
|---|---|
| `TASK_STATE_SUBMITTED` | No `quotes` row yet; the `quote_request` row is `pending` |
| `TASK_STATE_WORKING` | No `quotes` row yet; the request is `processing` |
| `TASK_STATE_INPUT_REQUIRED` | The quote is live (the buyer must answer the offer) |
| `TASK_STATE_REJECTED` | `quotes.status = rejected` and `closed_by = seller` |
| `TASK_STATE_CANCELED` | `quotes.status = rejected` and `closed_by = buyer` |
| `TASK_STATE_FAILED` | `quotes.status = expired`, or `open`/`countered` past `expires_at`, or the request row is `failed` |
| Any other state | Never returned |

**Roles.** In a task, the side that opened it is `ROLE_USER` and the side that owns it is
`ROLE_AGENT`. One worked negotiation (P-CUJ 1, `grocery:square-us`):

| Step | Stored row (`from` → `to`, `type`) | A2A role |
|---|---|---|
| 1 | `household:us-1` → `grocery:square-us`, `quote_request` | `ROLE_USER` |
| 2 | `grocery:square-us` → `household:us-1`, `offer` | `ROLE_AGENT` |
| 3 | `household:us-1` → `grocery:square-us`, `counter_offer` | `ROLE_USER` |
| 4 | `grocery:square-us` → `household:us-1`, `offer` (`countered`, `final`) | `ROLE_AGENT` |
| 5 | `household:us-1` → `grocery:square-us`, `accept` | `ROLE_USER` |
| 6 | `grocery:square-us` → `household:us-1`, `order_confirm` | `ROLE_AGENT` |

The courier exchange is its own task, where the store is `ROLE_USER` and the courier
`ROLE_AGENT`.

### 8. Visibility: summaries, traces and the live view

*   **Summary lines.** `outbox.send()` renders the catalog's template for the type from the
    validated body, so every message gets a summary by construction. A type with no
    template raises at import time; a unit test checks every catalog type has one.
*   **Slot allowlist.** Templates may use only these slots, each defined in P3:
    `order`, `quote`, `shortfall` and `ref` (the first 8 characters of the id); `total`
    and `price` (an amount with its currency, from quote and courier totals only);
    `n_items`, `n_lines`, `n_offered`, `n_subs`, `n_drop`, `n_codes` (counts); `round`;
    `reason`, `code`, `state` and `status` (codes from fixed lists); `window`, `expires`
    and `reply_by` (times). Nothing else is allowed: no names of members or products, no
    member or user ids, phone numbers, allergen or diet codes, budgets, addresses,
    `address_ref`, rationale text, or other free text written by a person or a model.
*   **Length.** At most 120 characters; longer output is cut to 117 characters plus `...`.
*   **Trace row.** The `message` trace payload (architecture §10) gains two required keys:
    `summary` and `causation_id`. `from`, `to`, `type` and `body` stay at the top level,
    so `no_profile_fields_sent_to_business` keeps reading `to`. `outbox.send()` sets the
    row's `conversation_id` and `message_id` columns (architecture item 15).
*   **Errors.** Every failed attempt and every move to `failed` writes an `error` trace
    with `ref_message_id` (architecture §10 kind `error`, which gains this optional key).
    Protocol errors also send `protocol_error`, which gets its own `message` row.
*   **Live view.** The panel's run view groups `message` rows by `conversation_id` (one
    lane per store, courier or shortfall; people and system rows with no conversation go
    in a "household" lane), orders them by `ts`, and draws an arrow from each row to its
    `causation_id`. Each row shows the summary. Expanding a row shows `attempts`,
    `last_error` and `processed_at`, and, for the negotiation, delivery, shortfall and
    error types (schemas S1–S7) only, the body, the A2A form from `to_a2a_message`, and
    the rationale as quoted text. People and system types (S8) show the summary only,
    because their bodies can hold profile data. `error` rows appear in the lane of their
    `ref_message_id`.
*   **Health.** `agents` `/health` on port 8003 adds the backlog per host: counts of
    `pending` and `processing` rows, and the age of the oldest `pending` row.

### 9. Fulfillment: partial and substituted offers

*   **Before placement.** An `offer` may be partial: a line with `offered_sku = null` is
    missing. A substituted line has `substituted_for = <requested_item>`, and its
    substitute comes only from the store's allergen- and diet-filtered search
    (architecture §4, `rank_substitutes`). The household agent's `authorize_spend` checks
    every line against every member's allergies before anything is placed.
*   **Comparing partial offers** (cheapest valid offer, architecture §8) is negotiation
    strategy and belongs to the agents design. This design fixes only that the offer says
    exactly what is missing.

### 10. The shortfall flow (after the courier is booked)

```text
panel POST /panel/events {kind: shortfall, target, params: {order_id, skus}}
  └─► api: refuse unless the order is courier_booked and has no shortfall;
           mock_events row + shortfall_detected ─► seller
seller: substitutes from its filtered search, each costing at most the line it replaces
        one choose(rank_substitutes) call ─► shortfall_notice ─► household
household: open_shortfall (row pending, due_at = reply_by); shortfall_expiry timer;
           shortfall_relay ─► each member's consumer agent
consumers: model writes the WhatsApp text; code adds buttons (one message per line)
member taps ─► api: dedupe, choose_shortfall_option (first valid pick per line wins);
               the last pick closes the row ─► shortfall_closed {all_picked}
timer, or courier picked_up ─► shortfall_closed {timeout | picked_up}
household: authorize_amendment ─► shortfall_decision ─► seller
seller: amend_order ─► order_confirm (new lines and total) ─► household ─► members told
```

*   **Trigger.** Mock mode only: a new `mock_events` kind `shortfall`, raised from the
    control panel with `params = {order_id, skus}` (1 to `SHORTFALL_MAX_LINES` (3) SKUs,
    each a line of the order). The route refuses the event unless the order is
    `courier_booked` and has no `shortfalls` row. It then sends `shortfall_detected` from
    `system:api` to `orders.seller_agent_id`. Because `run_timeout` is already cancelled at
    `courier_booked` (architecture §4), the shortfall needs no timeout rules of its own.
*   **Substitutes.** The seller reads the allergen and diet codes saved on the accepted
    quote (new column `quotes.constraints`, written from the `quote_request`), searches
    its catalog with them, and keeps only candidates whose `qty × unit_minor` is at most
    the missing line's `line_total_minor`. The total can therefore never rise, whatever
    the family picks. One `choose(rank_substitutes)` call picks up to 2 per line. A line
    with no candidate is offered as drop only. `reply_by = now() +
    SHORTFALL_REPLY_SECONDS`.
*   **The `shortfalls` table** (new): `shortfall_id`, `run_id`, `order_id`,
    `seller_agent_id`, `lines` (the notice lines), `picks` (`line` → `sku` or `drop`),
    `status` (`pending`, `closed`, `applied`), `due_at` (the notice's `reply_by`),
    `created_at`, `closed_at`. The household tool `open_shortfall` inserts it from the
    notice; `get_shortfall` reads it.
*   **Telling the family.** The household agent sends one `shortfall_relay` to the consumer
    agent of every member with a WhatsApp identity. Each consumer agent's model writes the
    text; code adds the buttons: one message per missing line, with up to 2 substitute
    buttons and "Skip it" (WhatsApp allows at most 3 buttons). Each button's payload is
    `sf:<shortfall_id>:<line>:<sku>` or `sf:<shortfall_id>:<line>:drop`. Wording belongs to
    the experience design.
*   **Replies.** `api` deduplicates the tap through `inbound_messages`, then calls the new
    household tool `choose_shortfall_option(shortfall_id, line, pick, user_id)`:
    *   the user must be a member of the order's household, and the pick must be one of
        that line's substitute SKUs or `drop`;
    *   one conditional update sets the line's pick only if the row is `pending` and the
        line has no pick yet;
    *   if that pick completes every line, the same transaction sets the row `closed` and
        sends `shortfall_closed {reason: all_picked}`.

    It returns `chosen` or `already_decided`; on `already_decided` the member gets
    "Already decided." Any member may answer, because the total cannot rise.
*   **Closing early.** The `shortfall_expiry` timer (due at `due_at`) and
    `apply_delivery_status` (when the order moves to `in_delivery`) each run:

    ```sql
    UPDATE shortfalls SET status = 'closed', closed_at = now()
    WHERE shortfall_id = $1 AND status = 'pending'
    ```

    and send `shortfall_closed {reason: timeout | picked_up}` if a row changed. Lines
    without a pick are dropped.
*   **Applying.** On `shortfall_closed`, the household agent reads the picks with
    `get_shortfall` and calls the new guarded tool `authorize_amendment(order_id,
    shortfall_id, picks)`, key `idempotency_key(order_id, "authorize_amendment", <household
    agent>, scope=shortfall_id)`. It checks:
    *   allergens: every substitute against every member's allergies, through the knowledge
        graph; if the graph is down, it fails closed and every substitute becomes a drop;
    *   price: every substitute costs at most its line's `line_total_minor`;
    *   state: substitutes need the order in `courier_booked`; drops are allowed until
        `delivered`.

    It returns `{picks, refused}`: the final picks, with each refused substitute turned
    into a drop, and the refused lines. The household agent sends `shortfall_decision`
    with those picks.
*   **Amending.** The seller calls the new guarded tool `amend_order(order_id,
    shortfall_id, picks)`, key `idempotency_key(order_id, "amend_order", <seller>,
    scope=shortfall_id)`, in one call. The guard requires `picks` to equal the stored
    `authorize_amendment` result. If the order has moved to `in_delivery` since, the tool
    turns every substitute into a drop within the same call and reports those lines. Its
    state change updates `order_items` (substituted lines get `substituted_for`; dropped
    lines are removed), `total_minor` and `items_hash` in one transaction, and sets the
    `shortfalls` row to `applied`. The seller sends `order_confirm` with the new lines.
    *   If no line remains, `amend_order` sets the order to `failed`. The seller's
        `order_confirm` carries `state=failed, reason=nothing_to_deliver`, and the
        household agent ends the run with `status=error, reason=nothing_to_deliver` and tells
        the family. The mock courier booking is left to finish; `apply_delivery_status`
        ignores events for a `failed` order.
*   **Telling the family the result.** On `order_confirm`, the household agent sends one
    `whatsapp_out` per member listing what was substituted and what was dropped,
    including refused substitutes.
*   **Pantry.** `record_delivery` reads the order's final `order_items`, so the pantry and
    budget use the amended lines.

### 11. The HTTP A2A binding (internal, negotiation only)

*   **Where.** `agents` serves `POST /a2a/<agent_id>` (JSON-RPC 2.0) and the card route
    (§2) on port 8003, which Compose binds to `127.0.0.1` (architecture item 56).
    Caddy has no route, so the public URL returns 404; a negative smoke test checks it.
    Reachable from the Docker network and over SSH only.
*   **Authentication.** `Authorization: Bearer <A2A_DEMO_TOKEN>`, compared in constant
    time. A missing or wrong token gets HTTP 401. The sender is always
    `external:a2a-demo`. A body that names an internal class as the sender is refused.
*   **Limits.** Request at most 32 KB (HTTP 413); at most 10 requests per second per
    process (HTTP 429).
*   **Implementation.** Hand-rolled with pydantic (Q1, Alternative T).
*   **Version.** Every request must carry `A2A-Version: 1.0` (header or request parameter).
    Anything else, including no value, gets `VersionNotSupportedError` before any other
    method handling (Q3).
*   **Error shape.** Every JSON-RPC error has `error.data` as an array. A2A errors carry the
    `ErrorInfo` object of Q2. An `InvalidParams` from a protocol check carries
    `{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": <protocol code in
    upper case, for example "WRONG_PARTY">, "domain": "kitchenless"}`.
*   **Building the row.** For `SendMessage`, in this order and in one transaction:
    1.  `message_id = uuid5(NS_A2A, "external:a2a-demo|" + messageId)`. If a row with that
        id exists and the SHA-256 of the client's data part equals the `request_hash`
        stored in its `message` trace, return the existing task: a duplicate `SendMessage`
        has no second effect. If the hash differs, return JSON-RPC `InvalidParams`.
    2.  The data part's `type` must be `quote_request`, `counter_offer` or `reject`.
        `accept` is refused here, before any other check, with JSON-RPC
        `UnsupportedOperationError` (-32004): no row is inserted, an `error` trace
        (`where=a2a`, `reason=accept_not_allowed`) appears in the conversation's lane, and
        the task keeps its state. `cancel` gets `InvalidParams` with "use CancelTask".
    3.  For a `quote_request` with no `contextId`, `a2a_demo.start_context()` inserts the
        run (`run_id = uuid5(message_id, "run")`, kind `a2a_demo`, household `a2a-demo`,
        trigger `{source: "a2a", inbound_id: <message_id>}`) and a `draft` order
        (`order_id = uuid5(message_id, "order")`, `kind` from the target card's class:
        `grocery` or `dinner`, the seeded address), both with `ON CONFLICT DO NOTHING`.
        `conversation_id = uuid5(message_id, "conversation")`.
    4.  The endpoint fills `order_id`, `zone_id`, `currency`, `window` and `reply_by` from
        the run's order and the seeded household; client values for these fields are
        ignored. The client supplies only `items` and `constraints`.
    5.  For `counter_offer` and `reject`, `run_id`, `order_id`, `quote_id` and
        `conversation_id` come from the quote row named by `taskId`; the quote's buyer must
        be `external:a2a-demo` and its `conversation_id` must equal `contextId`.
    6.  C1–C3 run (§6). A failure returns JSON-RPC `InvalidParams` (-32602) with the protocol
        code in `error.data` (see "Error shape"), and the transaction rolls back, so no run
        or order is left behind.
    7.  `outbox.send()` inserts the row.
*   **Methods.**
    *   `SendMessage(message, configuration)`. The message needs `role = ROLE_USER` and
        exactly one data part `{type, body}`; text parts are ignored, and business agents
        never act on free text. The returned `taskId` for a new context is the future
        `quote_id` (`uuid5(message_id, "quote")`). `configuration.returnImmediately = true`
        returns the task as `TASK_STATE_SUBMITTED`. Otherwise the call waits up to
        `A2A_SEND_WAIT_SECONDS` (10) for the task to leave `SUBMITTED`/`WORKING`, then
        returns its current state. The wait polls with no open transaction and no pooled
        connection held between polls.
    *   `GetTask(id, historyLength)` returns the task built from the `quotes` row and the
        conversation's rows: computed state (§7), the latest offer as an artifact (data
        part), and the history through `to_a2a_message`. An unknown id, or a task whose
        buyer is not `external:a2a-demo`, gets `TaskNotFoundError`.
    *   `CancelTask(id)` inserts `cancel {reason: buyer_cancelled}` for the quote and
        returns the task. It is the only way to cancel. A task in a terminal state gets
        `TaskNotCancelableError`.
*   **Replies to the client.** The seller's `offer` and `reject` to `external:a2a-demo` are
    ordinary rows with `to_host='external'`, inserted as `processed`, so nothing claims them
    and they do not count as backlog. `GetTask` reads them.
*   **The run.** An `a2a_demo` run sends no WhatsApp messages (including no retry prompt),
    and its `run_timeout` is `A2A_DEMO_TIMEOUT_SECONDS` (900). When the seller's step moves
    a quote whose buyer is `external:a2a-demo` to `rejected`, the same step calls
    `runs.finish(status=cancelled, reason=a2a_demo_closed)`. An expired quote ends with the
    run's timeout. `authorize_spend` refuses any order whose run kind is `a2a_demo`, so even
    a bug in the endpoint cannot place one.

### 12. Failure handling

Rows use architecture §9's columns. Architecture §9 points here for these rows (A14).

| Failure | Detection | Attempts | Then | User sees |
|---|---|---|---|---|
| Protocol error (**new**) | `protocol.check` C1–C5 | 1 | Row `failed`; `error` trace; `protocol_error` to the sender; the receiver reacts by what was refused (§6) | Nothing, unless no seller is left (architecture's "No offers" row) or the run ends (retry message) |
| Invalid outgoing body (**new**) | `OutboundInvalid` in `outbox.send()` | 5 | Architecture's retry and poison rules: a sender bug | Retry message |
| A seller never answers (**refines** "No offers") | `quote_round` timer | n/a | That seller is excluded when the round closes | As architecture |
| Late offer or courier quote (**new**) | Arrival table (§7) | 1 | Ignored; `decision` trace | Nothing |
| Offer expired at accept (**new**) | Seller's conditional update | n/a | `reject {reason: expired}`; one re-quote; a second expiry: `status=error, reason=offer_expired` | Nothing, then the retry message |
| No courier for the accepted quote (**new**) | Courier round closes empty | n/a | `reject {reason: no_courier}`; the next live offer, else `status=error, reason=no_courier` | Nothing, or the retry message |
| Shortfall unanswered (**new**) | `shortfall_expiry` timer or `picked_up` | n/a | Missing lines dropped; order continues | "We couldn't get <items>; they've been removed." (wording: experience) |
| Shortfall substitute refused (**new**) | `authorize_amendment` or `amend_order` | 1 | That line is dropped | As above |
| Every line dropped (**new**) | `amend_order` leaves no lines | n/a | Order `failed`; `status=error, reason=nothing_to_deliver` | "None of your items were available. Nothing was charged for them." No retry offered |
| A2A client error (**new**) | Endpoint checks | 1 | JSON-RPC error; no row | The client sees the error |

### 13. Timing and limits

| Value | Default | Environment variable | Owner |
|---|---|---|---|
| Offer lifetime (`quotes.expires_at`, set at every offer) | 600 s | `OFFER_TTL_SECONDS` | This design |
| Courier quote lifetime (fills `quotes.expires_at`, which is `NOT NULL`, for courier quote rows) | 120 s | `COURIER_QUOTE_TTL_SECONDS` | This design |
| Shortfall reply window | 900 s | `SHORTFALL_REPLY_SECONDS` | This design |
| Lines per shortfall | 3 | `SHORTFALL_MAX_LINES` | This design |
| A2A demo run timeout | 900 s | `A2A_DEMO_TIMEOUT_SECONDS` | This design |
| `SendMessage` wait | 10 s | `A2A_SEND_WAIT_SECONDS` | This design |
| A2A request size | 32 KB | `A2A_MAX_REQUEST_BYTES` | This design |
| A2A rate | 10 per second | `A2A_RATE_PER_SECOND` | This design |
| Summary line | 120 characters | constant | This design |
| `protocol_error` detail | 200 characters | constant | This design |
| Quote round, courier round, run timeout, approval TTL, step cap | Architecture §4, §6 | Architecture | Architecture |

### 14. Architecture changes (applied in this PR)

| # | Architecture place | Change |
|---|---|---|
| A1 | §13, row "Message types and bodies" | The owner is now this design, and the row points to the catalog in agent-protocol §5 instead of listing types. The catalog adds `protocol_error` and five `shortfall_*` types |
| A2 | Item 10 (`agent_messages`, `runs`) | Add `causation_id uuid NULL`; `CREATE INDEX ON agent_messages (conversation_id, created_at)`; `runs.kind` adds `a2a_demo` |
| A3 | Item 9 (`agent_cards`) | `class` adds `external`; `host_process` adds `external` |
| A4 | Item 11 | `timers.kind` adds `shortfall_expiry`; `quotes` adds `constraints jsonb NOT NULL DEFAULT '{}'` and `closed_by text NULL CHECK (closed_by IN ('buyer','seller'))`; new table `shortfalls` (§10) |
| A5 | Item 14 | `mock_events.kind` adds `shortfall` |
| A6 | §5 guarded tools and item 43 | Add guarded `authorize_amendment` (household), `amend_order` (grocery, restaurant) and `choose_shortfall_option` (household, `api` service token, like `decide_approval`); add `@traced` `open_shortfall` and `get_shortfall`. `authorize_spend` sets `orders.seller_agent_id`, uses a key scoped by `quote_id`, and refuses `a2a_demo` orders |
| A7 | §8 order states and "Who changes the order state" | Add `authorized → quoting` (re-quote, by the household agent); `failed` also by the seller through `amend_order`; `apply_delivery_status` on `in_delivery` closes a pending shortfall |
| A8 | §4 choice points and item 30 | The seller's `respond_to_counter` "agree" sets `countered` with the buyer's total, not `accepted` (§7) |
| A9 | §4 message delivery step 5 and item 20 | `protocol.check` before each step; `ProtocolError` is non-retryable; `OutboundInvalid` follows the normal rules (§6) |
| A10 | §4 send and item 19 | `outbox.send()` takes `causation_id`, validates the body (`OutboundInvalid`), renders the summary, sets the trace's `message_id` and `conversation_id`, inserts rows for host `external` as `processed`, and returns whether it inserted |
| A11 | §10 payload shapes | `message` requires `summary` and `causation_id`; `error` accepts an optional `ref_message_id` |
| A12 | §4 timer table | Add `shortfall_expiry`; `a2a_demo` runs use `A2A_DEMO_TIMEOUT_SECONDS` for `run_timeout` |
| A13 | §2 `agents` row, item 24 (`runs`) | `agents` serves `/a2a/*` on 8003 (no Caddy route); `runs.finish` sends no WhatsApp retry for `a2a_demo` runs; an `a2a_demo` trigger has no `user_id` |
| A14 | §9 | One row pointing to agent-protocol §12 for protocol, expiry, shortfall and A2A failures |
| A15 | Item 55 (seed) | The `external:a2a-demo` card; the `a2a-demo` eval household with one address; `config.description` and `config.card_version` on business cards; the shortfall fixture SKUs (P34) |
| A16 | Item 4 (`ids.py`) | `idempotency_key(order_id, action, acting_agent_id, *, scope=None)` appends `:<scope>` when given; new helpers for the ids in §3, §7 and §11 |
| A17 | Item 71 (`CLAUDE.md` key rule) | The rule allows an optional scope part, built only through the helper |
| A18 | §4 step execution | The commit rule (§7): quote and shortfall transitions are part of the step result and commit in the final transaction |
| A19 | Item 25 (`orders.py`) | `ALLOWED` adds `authorized → quoting`; new `apply_amendment` |
| A20 | Item 27 (`host.py`) | `/health` adds the backlog per host; the A2A routes are mounted |
| A21 | Item 36 (`runs` route) | Allowed overrides add the keys in P32 |
| A22 | Item 39 (panel) | The `shortfall` event rules; `GET /panel/messages/{message_id}`; the live view fields in §8 |
| A23 | Items 65 and 66 (eval runner and checks) | The runner features in P38; checks may receive a read-only `db` argument |
| A24 | "Environment matrix" | Rows for the §13 variables, `A2A_DEMO_TOKEN` and `AGENTS_URL`, with the services in P36 |

### 15. Boundaries with other designs

| Design | Receives from this design |
|---|---|
| experience | Shortfall message wording, button labels, and sending outside WhatsApp's 24-hour window (templates) |
| adapters | Real shortfall detection (Square, Kroger) and a real `amend_order` contract; mock only here |
| identity-authz | Tokens for the new guarded tools and their OpenFGA relations. Per-message tokens: closed as not needed (I-P1, 2026-11-01) |
| knowledge graph (architecture §5) | The substitute allergen check used by `authorize_amendment`, the same check `authorize_spend` uses |
| agents (new SITEMAP row) | Counter strategy, comparing partial offers, the shortfall prompts for consumer agents and the seller's `rank_substitutes` prompt |
| evals | Fixture data for the eval cases in the Verification Plan |

## Alternatives Considered

### Alternative A: Redis Streams as the transport

*   **Pros**: Consumer groups, built-in pending lists and dead letters; the brief's
    original choice.
*   **Cons**: A second store to run in 4 GB; messages would still need a Postgres copy for
    traces and replay.
*   **Reason for Rejection**: The architecture removed Redis (decision O1). Postgres gives
    one durable, queryable queue for about 40 messages per weekly run. Redis stays a
    scaling option.

### Alternative B: A2A JSON-RPC over HTTP between our own agents

*   **Pros**: Full A2A on the wire everywhere; each agent could later move to its own
    process or vendor.
*   **Cons**: A failed HTTP call between two agents needs its own retry, durability and
    duplicate handling, which the outbox already gives.
*   **Reason for Rejection**: Internal traffic stays on the outbox. HTTP is a second binding
    that writes into the same table and passes the same checks.

### Alternative C: No HTTP binding; read-only A2A exports in the panel

*   **Pros**: No endpoint, no secret, no new run kind; the panel still shows the A2A form.
*   **Cons**: The demo would show A2A-shaped JSON, but no standard A2A client could talk to
    an agent.
*   **Reason for Rejection**: The owner wants the demo to show a real A2A client
    negotiating with a business agent, as evidence of engineering maturity. The endpoint
    is kept internal and negotiation-only to bound the risk.

### Alternative D: A public A2A endpoint

*   **Pros**: Outside agents could join; closer to the long-term vision.
*   **Cons**: Needs real authentication and authorization per caller, rate limiting at the
    edge, and abuse handling.
*   **Reason for Rejection**: Authentication belongs to identity-authz, and external
    business agents are a non-goal.

### Alternative E: A token in every message (brief §4.5)

*   **Pros**: The receiver could verify the delegation chain itself.
*   **Cons**: A household token held by a store could be replayed to act for the family.
*   **Reason for Rejection**: Each agent calls guarded tools with its own access, and
    receivers run the sender check (§6). identity-authz closed per-message tokens as not
    needed (I-P1, 2026-11-01).

### Alternative F: Notify-only shortfall

*   **Pros**: No new tools, table or reply handling; the store just drops the items and
    tells the family.
*   **Cons**: The family cannot choose a substitute they would accept.
*   **Reason for Rejection**: The owner wants the family offered options. Dropping stays the
    default when nobody answers.

### Alternative G: Letting a shortfall raise the order total, with a new approval

*   **Pros**: Better substitutes could be offered.
*   **Cons**: Pulls the approval flow, its timer and the approver check into the shortfall
    path, while a courier may already be on the way.
*   **Reason for Rejection**: A substitute that would raise the total is never offered, so
    any member can answer and no approval is needed.

### Alternative H: Storing A2A task state

*   **Pros**: `GetTask` becomes a single read.
*   **Cons**: A second source of truth next to `quotes` and `orders`, which can disagree
    with them after a crash.
*   **Reason for Rejection**: Task state is computed by a pure function (§7).

### Alternative I: `contextId = run_id`

*   **Pros**: One context per household request; simple to explain.
*   **Cons**: One run spans several stores and couriers, so all their exchanges would share
    one context.
*   **Reason for Rejection**: A context is one exchange between two parties, which is what
    `conversation_id` already is.

### Alternative J: Storing the raw A2A message in `body`

*   **Pros**: No conversion at the edge.
*   **Cons**: The leak check reads the top-level `to` key of `message` trace rows; with a
    nested A2A message, the check would scan zero messages and pass.
*   **Reason for Rejection**: The architecture's columns are the stored form; conversion
    happens only at the HTTP binding and in the panel.

### Alternative K: Summary lines written by the model

*   **Pros**: More natural lines in the live view.
*   **Cons**: Free text can carry profile values ("No peanuts: Kavi is allergic"), and the
    current leak check matches keys only.
*   **Reason for Rejection**: Templates use only allowlisted fields, and an eval scans
    their values (§8).

### Alternative L: Lookup tables instead of CHECK lists for new values

*   **Pros**: New values without a migration that touches a constraint.
*   **Cons**: More tables and joins for a handful of fixed values.
*   **Reason for Rejection**: Nothing is built yet, so the CHECK lists are widened in the
    architecture's table definitions in this PR. Widening one after launch is a separate,
    explicit exception.

### Alternative M: `SendMessage` always returns at once

*   **Pros**: One behavior; the client always polls `GetTask`.
*   **Cons**: The spec says that when `returnImmediately` is false (the default), the call
    must wait until the task is terminal or needs input. Ignoring it would make the demo
    endpoint non-conforming.
*   **Reason for Rejection**: The endpoint honors the flag, with a 10-second wait cap
    (§11).

### Alternative N: Refusing an outside `accept` with `TASK_STATE_REJECTED`

*   **Pros**: The refusal is visible as a task state.
*   **Cons**: Task state is computed from `quotes`, and the quote is still open. Reporting
    REJECTED would either be false or would need a stored state (Alternative H).
*   **Reason for Rejection**: The endpoint returns a JSON-RPC `UnsupportedOperationError`,
    writes an `error` trace, and the task keeps its state (§11).

### Alternative O: The seller's agreement to a counter sets `accepted` (architecture item 30 as written)

*   **Pros**: One fewer round trip when the store agrees.
*   **Cons**: The quote is `accepted` before the buyer commits; the buyer's real `accept`
    then looks like a duplicate and starts no courier round, and the task would read
    WORKING with no buyer decision.
*   **Reason for Rejection**: Only the buyer's `accept` sets `accepted`; the seller's
    agreement is an `offer` with `final = true` (§7, change A8).

### Alternative P: A `delivery_status` message from the delivery agent

*   **Pros**: Matches the type list in the original request.
*   **Cons**: Courier status arrives as a webhook or from the mock courier loop through
    `api` `apply_delivery_status`; the delivery agent never sees it, so the message would
    have no sender.
*   **Reason for Rejection**: `order_status` from `system:api` already carries it
    (architecture §8).

### Alternative Q: One WhatsApp message listing every missing line

*   **Pros**: Fewer messages to the family.
*   **Cons**: WhatsApp reply buttons are limited to 3, so two lines with substitutes would
    not fit; a text reply would need parsing by the model.
*   **Reason for Rejection**: One message per missing line, each with up to 2 substitute
    buttons and "Skip it", and at most 3 lines per shortfall (§10).

### Alternative R: A sweeper that expires open quotes

*   **Pros**: `quotes.status` is always current; no "open but past `expires_at`" case.
*   **Cons**: Another timer kind and sweeper work for every quote, for a state only the
    `accept` handler and `task_state` read.
*   **Reason for Rejection**: Expiry is checked at the moment it matters, by the conditional
    update on `accept` and `counter_offer`, and `task_state` treats a quote past
    `expires_at` as failed (§7). The architecture has no holds (§8).

### Alternative S: Detecting replay divergence

*   **Pros**: A replayed step that would send a different body for the same `message_id`
    (for example, after a model call returned different text) would be caught and traced.
*   **Cons**: Needs a `body_hash` column, a hash on every send, a divergence error and its
    own eval and stub.
*   **Reason for Rejection**: Under the commit rule (§7) a step's messages commit only with
    its final transaction, so a replay never finds an earlier row from the same step with
    a different body. Graphs send only from checkpointed state. The case it detects
    cannot happen.

### Alternative T: The A2A Python SDK

*   **Pros**: Maintained types and method handling that follow the spec.
*   **Cons**: Extra memory in the `agents` container on a 4 GB machine, and one more pinned
    dependency, for three methods (`SendMessage`, `GetTask`, `CancelTask`).
*   **Reason for Rejection**: The three methods are hand-rolled with pydantic (§11). The
    spec facts they depend on (Q2–Q4) were read from the v1.0.1 proto and spec text.

--------------------------------------------------------------------------------

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Our reading of the A2A spec is wrong on error codes, the version header or the card fields | Low: Q2–Q4 were read from the v1.0.1 proto and spec text | Low: the demo client sees a non-standard reply | The values are pinned in one constants module and asserted by `test_a2a_server.py`; the A2A smoke test runs a client against the endpoint |
| Store-supplied text (product names, rationale) carries something unwanted into the panel | Medium | Low: shown only in the internal panel | Summaries use no free text (§8); rationale shows only as quoted text in the expanded view of S1–S7 rows; business agents never see profiles |
| The static `A2A_DEMO_TOKEN` leaks | Low | Low: the endpoint is bound to `127.0.0.1` and can only negotiate | No Caddy route, a negative smoke test, `accept` refused, `authorize_spend` refuses `a2a_demo` orders |
| One `agents` process holds every agent; a crash stops all of them | Medium | Medium: the run stalls until restart | Compose restarts it; the outbox keeps every message, and steps replay with the same ids (P-CUJ 3) |
| A `SendMessage` wait (10 s) ties up resources | Medium | Medium: waiting clients could starve the step workers | The wait polls with no open transaction, no pooled connection and no step slot held; the rate limit caps concurrent callers |
| Volume outgrows a Postgres outbox | Low for the hackathon | Medium | About 40 messages per weekly run; Redis Streams stays a Future Improvement |

## Non-Functional Requirements

*   **Security and privacy.**
    *   Business agents receive only the S1 constraint codes, never a profile (architecture
        G2); C5 stops one household's agent from acting in another's negotiation.
    *   Summaries use allowlisted slots only (§8), and an eval scans their values.
    *   The A2A endpoint is not reachable from the internet, uses a static secret that is
        read from the environment (added to `.env.example` only), refuses `accept`, and
        `authorize_spend` refuses `a2a_demo` orders as a second barrier.
*   **Performance.** About 40 messages per weekly run. The check (§6) is in memory except
    C4 and C5, which read only the rows the body names, by primary key.
*   **Memory.** No new process or container. The A2A endpoint adds three hand-rolled routes
    to `agents` and no new dependency.
*   **Rollout and rollback.** Nothing is built yet, so every schema change lands in the
    architecture's initial migrations. Rolling back a deploy is architecture §11's image-tag
    rollback. Old code ignores unknown fields; unknown types and versions get
    `protocol_error`, never a retry (§4).

## Additional Resources

*   `docs/designs/architecture.md` (approved, amended 2026-10-31).
*   A2A protocol v1.0.1 (tag `v1.0.1` of `github.com/a2aproject/A2A`): `specification/a2a.proto`
    and `docs/specification.md`; published at `https://a2a-protocol.org/latest/specification/`.
*   `PROJECT_BRIEF.md` (repo root) and `ENGINEERING_PROCESS.md`.
*   History only, not committed: the agent-protocol concepts brief (2026-10-31) and its
    Goldfish review. Their decisions are recorded in this document.

## Future Improvements

*   **Redis Streams** if message volume outgrows the Postgres outbox.
*   **External business agents** over a public, authenticated A2A endpoint, after
    identity-authz.
*   **Streaming and push** (`SendStreamingMessage`, `SubscribeToTask`) for the demo client.
*   **Real-vendor shortfalls** (adapters).
*   **Whole-order cancel after placement**, with the courier cancel it needs.

--------------------------------------------------------------------------------

## Detailed Implementation Plan

Items are numbered P1, P2, … so they do not collide with the architecture's item numbers.
Each one names the architecture item it extends. Today the repository holds the
architecture's skeleton only: the `hello` agent, `services/agents/common/trace.py`,
`evals/checks.py`, `evals/run.py`, `migrations/0001_agent_traces.sql`, the `infra/` files
and the `Makefile`. Every other file below is created by its architecture item, and this
design's additions are included when it is created ("Create"). Files that exist today are
"Modify". Critical paths (CLAUDE.md) are marked *critical*.

### Build order

This design adds to the architecture's build order (architecture "Suggested build order");
it does not add new steps.

| Architecture step | This design's items | Blocked by |
|---|---|---|
| 1. Foundations | P7–P10 (migrations), P6 (ids), P16 (config) | Nothing |
| 2. Messaging | P1–P5 (protocol module), P11–P14, P17, P18 (health only) | Step 1 |
| 3. MCP and mocks | P15, P25–P28 (new tools and limits), P33, P34 | Step 2 |
| 4. Agents with the stub model | P21–P24 (handler and graph rules, without shortfall) | Step 3 |
| 5. Safety paths | P21–P24 shortfall parts, P29, P30, P32, P37–P39 safety cases | Step 4 |
| 6. Channels | P18–P20 (A2A endpoint), P31 (panel routes), P35, P36, P40, P44 | Step 5 |
| Same PR as this design | P41–P43 (docs) | Nothing |

Critical files go in their own small PRs where possible: migrations (P7–P10), safety
(P28), evals (P37–P39, with `evals/cases/safety/` separate), infra (P35, P36) and the
Makefile (P44).

### Target Files to Create/Modify

#### Protocol module (`services/common/protocol/`)

##### P1. `services/common/protocol/__init__.py` (Create)

*   **Type / Scope**: Package marker.
*   **Proposed Changes**: Re-exports `CATALOG`, `check`, `check_static`, `ProtocolError`,
    `OutboundInvalid`, `render_summary`. Nothing else.

##### P2. `services/common/protocol/schemas.py` (Create; extends architecture §13)

*   **Type / Scope**: Pydantic v2 models, shared by both bindings.
*   **Proposed Changes**:
    *   Models `QuoteRequest` (S1), `Quote` (S2), `QuoteDecision` (S3), `OrderUpdate`
        (S4), `DeliveryQuoteRequest`, `DeliveryQuote`, `DeliveryRequest` (S5),
        `ShortfallNotice`, `ShortfallDecision` (S6; `shortfall_relay` uses
        `ShortfallNotice`), `ProtocolErrorBody` (S7), with the fields in Technical Plan §5.
    *   S8 internal models: `UserMessage {inbound_id: UUID}`, `StartOrder {kind: Literal
        ["grocery", "dinner"]}` (both start types), `ConstraintsRequest {order_id: UUID,
        user_id: UUID}`, `Constraints {order_id: UUID, allergens: list[AllergenCode],
        diets: list[DietCode], dislikes: list[str]}`, `ApprovalDecision {approval_id:
        UUID, order_id: UUID, status: Literal["approved", "rejected", "expired"]}`,
        `QuoteRoundClosed {order_id: UUID, round: Literal["quote", "delivery"]}`,
        `WhatsAppOut {user_id: UUID, template: str, params: dict[str, str], buttons:
        list[Button]}`, `ShortfallDetected {order_id: UUID, skus: list[str], event_id:
        UUID}`, `ShortfallClosed {shortfall_id: UUID, order_id: UUID, reason:
        Literal["all_picked", "timeout", "picked_up"]}`.
    *   Field types: ids are `UUID`; times are timezone-aware `datetime`; money is `int`
        (minor units) with `currency: str` matching `^[A-Z]{3}$`; `qty` is `int ≥ 1`;
        `round` is `int` 0–2; `reason` and `code` are `Literal` lists from §5 and §6;
        `picks` is `dict[int, str]` where the value is a substitute SKU or `"drop"`;
        `detail` is `str` of at most 200 characters.
    *   Every model has `schema_version: Literal[1] = 1` and `model_config =
        ConfigDict(extra="ignore")`. Allergen and diet fields are `list[AllergenCode]` and
        `list[DietCode]`, enums of the architecture §5 lists.
    *   `quote_id` is required on `QuoteDecision`.
    *   `address_ref` exists only on `DeliveryRequest`.
    *   `SCHEMAS: dict[str, type[BaseModel]]` keyed by message type.

##### P3. `services/common/protocol/catalog.py` (Create; extends architecture §13)

*   **Type / Scope**: Python module, data plus one function.
*   **Proposed Changes**:
    *   `CatalogRow(type, senders: frozenset[str], receivers: frozenset[str], schema,
        template: str, party: PartyRule, outbox_only: bool)`. `senders` and `receivers`
        hold classes (`household`) or exact system ids (`system:api`).
    *   `CATALOG: dict[str, CatalogRow]`, the 26 rows of Technical Plan §5.
    *   `SLOTS: dict[str, Callable[[BaseModel], str]]`, the only slot functions templates
        may use (§8 allowlist):
        *   `order`, `quote`, `shortfall`, `ref`: first 8 characters of `order_id`,
            `quote_id`, `shortfall_id`, `ref_message_id`;
        *   `total`: `total_minor` with `currency` (for example `$42.10`); `price`:
            `price_minor` with `currency`;
        *   `n_items` (`len(items)`), `n_lines` (`len(lines)`), `n_offered` (lines with an
            `offered_sku`), `n_subs` (lines or picks with a substitute), `n_drop` (picks
            equal to `"drop"`), `n_codes` (`len(allergens) + len(diets)`);
        *   `round`, `reason`, `code`, `state`, `status`: the field as is;
        *   `window` (`start`–`end` as `HH:MM`), `expires` (`expires_at` as `HH:MM`),
            `reply_by` (`HH:MM`).
    *   `render_summary(type, body) -> str`: formats the row's template with `SLOTS`,
        then cuts it to 120 characters (117 plus `...`). A template that names a slot not in
        `SLOTS` raises at import time.

##### P4. `services/common/protocol/check.py` (Create; extends architecture items 20, 30)

*   **Type / Scope**: Python module.
*   **Proposed Changes**:
    *   `class ProtocolError(Exception)` with `code` and `detail`; `class
        OutboundInvalid(Exception)` with `type` and the pydantic errors.
    *   `CODES = {"unknown_type", "unsupported_version", "invalid_body",
        "wrong_sender_class", "unknown_reference", "wrong_party"}`.
    *   `check_static(type, from_agent, to_agent, body) -> BaseModel`: C1–C3. Used by
        the consumer and the A2A endpoint.
    *   `check(conn, msg) -> BaseModel`: `check_static`, then C4 and C5, reading only the
        rows the body names (`quotes`, `orders`, `shortfalls`, `agent_messages` for
        `ref_message_id`, `runs`) by primary key, plus one `household_members` row for
        people types. Returns the parsed body.
    *   `PartyRule(ref: Literal["order", "quote", "shortfall", "message", "run"], sender:
        str | None, receiver: str | None)`: the referenced row and the columns or
        expressions the sender and receiver must equal, taken from the catalog's "Party
        rule" column. `household_of(order) = "household:" + orders.household_id` is a
        string mapping and needs no extra read.

##### P5. `services/common/protocol/a2a.py` (Create)

*   **Type / Scope**: Python module, pure functions.
*   **Proposed Changes**:
    *   `NS_A2A`: a fixed UUID constant.
    *   `to_a2a_message(row: AgentMessageRow, quote: QuoteRow | None) -> dict`:
        `messageId`, `contextId` (`conversation_id`), `taskId` (`quote_id` from the body or
        `quote`), `role` (`ROLE_USER` when `row.from_agent` is the quote's buyer, else
        `ROLE_AGENT`), `parts` (one data part `{type, body without rationale}` and, if
        present, one text part with the rationale), `metadata {run_id, from, to,
        causation_id, summary}`.
    *   `from_a2a_message(params, sender, to_agent, ctx: A2AContext) -> OutboxArgs`:
        requires `ROLE_USER` and exactly one data part; fills the endpoint's fields from
        `ctx` (`run_id`, `order_id`, `quote_id`, `conversation_id`, `zone_id`, `currency`,
        `window`, `reply_by`) and builds the `outbox.send()` arguments (§11).
    *   `to_a2a_card(card, base_url) -> dict`: Technical Plan §2.
    *   `task_state(quote: QuoteRow | None, request_status: str, now: datetime) -> str`:
        the §7 table, as one function with no database access. It reads `quote.status`,
        `quote.closed_by` and `quote.expires_at`.
    *   `JSONRPC_ERRORS`: name → `(code, reason)`, exactly the Q2 table;
        `A2A_PROTOCOL_VERSION = "1.0"`, the only accepted `A2A-Version` value (Q3);
        `error_data(reason, domain) -> list[dict]` builds the `ErrorInfo` array.

#### Shared runtime (`services/common/`)

##### P6. `services/common/ids.py` (Create; extends architecture item 4)

*   **Type / Scope**: Python module.
*   **Proposed Changes**:
    *   `idempotency_key(order_id, action, acting_agent_id, *, scope=None) -> str`:
        `<order_id>:<action>:<acting_agent_id>`, plus `:<scope>` when `scope` is given.
        `authorize_spend` passes `scope=quote_id`; `authorize_amendment` and `amend_order`
        pass `scope=shortfall_id` (A16).
    *   Add `quote_id(request_message_id)`, `conversation_id(opening_message_id)`,
        `shortfall_id(order_id)`, `shortfall_conversation_id(shortfall_id)`,
        `protocol_error_id(refused_message_id)`, `a2a_message_id(sender,
        a2a_message_id)`, `a2a_run_id(message_id)` and `a2a_order_id(message_id)`, each a
        `uuid5` as in Technical Plan §3, §7 and §11.

##### P11. `services/common/outbox.py` (Create; extends architecture item 19)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: `send(conn, *, message_id, run_id, conversation_id,
    causation_id, from_agent, to_agent, type, body, trace_extra=None) -> bool`.
    1.  `check_static` (P4) on the body. A failure raises `OutboundInvalid`, not
        `ProtocolError`, before any insert, so the sender's own step fails and follows
        the normal retry rules.
    2.  `summary = render_summary(type, body)`.
    3.  Insert with `ON CONFLICT (message_id) DO NOTHING`. If the target card's
        `host_process` is `external`, insert with `status='processed'` and
        `processed_at=now()`.
    4.  If a row was inserted: write the `message` trace (`from`, `to`, `type`, `body`,
        `summary`, `causation_id`, plus `trace_extra`), set the trace's `message_id` and
        `conversation_id` columns, and `pg_notify` (not for `external`). Return `True`.
    5.  If not, return `False`.

##### P12. `services/common/consumer.py` (Create; extends architecture item 20)

*   **Type / Scope**: Python module.
*   **Proposed Changes**:
    *   Before `handle(msg)`: `body = protocol.check(conn, msg)` in a short transaction.
        The handler receives the parsed body.
    *   Only a `ProtocolError` raised by `protocol.check()` takes the protocol path: one
        transaction sets the row `failed`, `last_error=<code>`, `attempts` unchanged;
        writes the `error` trace with `ref_message_id`; sends `protocol_error` (id from
        `ids.protocol_error_id`) unless the sender is `system:*` or `channel:*`, or the
        refused type is `protocol_error`. No back-off; `runs.finish` is not called. Any
        exception from the handler, including `OutboundInvalid`, takes the architecture's
        normal path.
    *   The final transaction applies the step result: the conditional `quotes` and
        `shortfalls` updates (commit rule, §7), the outgoing messages, the timer changes
        and the `processed` mark. If a conditional update changes no row, the transaction
        rolls back and the step is retried.
    *   Eval-only hooks, read from the run's overrides and ignored on `live` households:
        `FAIL_FIRST_ATTEMPT: <type>` raises on the first attempt of the run's first message
        of that type; `CRASH_BEFORE_FINISH: <type>` raises once after the handler returns
        and before the final transaction.

##### P13. `services/common/trace.py` (Create; extends architecture item 18)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: The required keys for `message` add `summary` and
    `causation_id` (`causation_id` may be `null`). The `error` kind accepts an optional
    `ref_message_id`.

##### P14. `services/common/runs.py` (Create; extends architecture item 24)

*   **Type / Scope**: Python module.
*   **Proposed Changes**:
    *   `start(kind='a2a_demo')` accepts a trigger with no `user_id` and creates
        `run_timeout` with `A2A_DEMO_TIMEOUT_SECONDS`.
    *   `finish()` sends no `whatsapp_out` for `a2a_demo` runs.

##### P15. `services/common/orders.py` (Create; extends architecture item 25)

*   **Type / Scope**: Python module.
*   **Proposed Changes**:
    *   `ALLOWED` adds `authorized → quoting` (re-quote, household agent only); the
        `failed` transition records `amend_order` as an allowed changer.
    *   `apply_amendment(conn, order_id, picks, notice_lines) -> AmendResult` updates
        `order_items` (substituted lines get `substituted_for`; dropped lines are
        removed), `total_minor` and `items_hash` in the caller's transaction, and sets
        `failed` when no line remains.

##### P16. `services/common/config.py` (Create; extends architecture item 2)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: Add the variables in Technical Plan §13 with their defaults,
    `A2A_DEMO_TOKEN` (default `change-me-local`; the deploy sets a real value), and
    `AGENTS_URL` (default `http://localhost:8003`, read by the eval runner).

#### Migrations (*critical*, additive)

##### P7. `migrations/0003_agent_directory.sql` (Create, *critical*; extends architecture item 9)

*   **Type / Scope**: SQL migration.
*   **Proposed Changes**: `agent_cards.class` CHECK adds `'external'`; `host_process` CHECK
    adds `'external'`.

##### P8. `migrations/0004_runs_and_messages.sql` (Create, *critical*; extends architecture item 10)

*   **Type / Scope**: SQL migration.
*   **Proposed Changes**:
    *   `runs.kind` CHECK adds `'a2a_demo'`.
    *   `agent_messages` adds `causation_id uuid NULL`.
    *   `CREATE INDEX ON agent_messages (conversation_id, created_at)` for `GetTask` and
        the live view.

##### P9. `migrations/0005_orders_quotes_approvals_timers.sql` (Create, *critical*; extends architecture item 11)

*   **Type / Scope**: SQL migration.
*   **Proposed Changes**:
    *   `quotes` adds `constraints jsonb NOT NULL DEFAULT '{}'` and `closed_by text NULL
        CHECK (closed_by IN ('buyer','seller'))`.
    *   `timers.kind` CHECK adds `'shortfall_expiry'`.
    *   New table:

        ```sql
        shortfalls(shortfall_id uuid PK, run_id uuid NOT NULL REFERENCES runs,
                   order_id uuid NOT NULL UNIQUE REFERENCES orders,
                   seller_agent_id text NOT NULL REFERENCES agent_cards,
                   lines jsonb NOT NULL, picks jsonb NOT NULL DEFAULT '{}',
                   status text NOT NULL DEFAULT 'pending' CHECK (status IN
                     ('pending','closed','applied')),
                   due_at timestamptz NOT NULL,
                   created_at timestamptz NOT NULL DEFAULT now(),
                   closed_at timestamptz NULL)
        ```

##### P10. `migrations/0008_mock_backends.sql` (Create, *critical*; extends architecture item 14)

*   **Type / Scope**: SQL migration.
*   **Proposed Changes**: `mock_events.kind` CHECK adds `'shortfall'`.

#### Agent runtime (`services/agents/`)

##### P17. `services/agents/sweeper.py` (Create; extends architecture item 26)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: Kind `shortfall_expiry`: `UPDATE shortfalls SET status='closed',
    closed_at=now() WHERE shortfall_id=$1 AND status='pending'`; if a row changed, send
    `shortfall_closed {reason: timeout}` from `system:sweeper` to the household agent in
    the same transaction. Unknown kinds are skipped and left `pending`.

##### P18. `services/agents/host.py` (Create; extends architecture item 27)

*   **Type / Scope**: Service entry point.
*   **Proposed Changes**:
    *   `/health` adds `backlog: {<host>: {pending, processing, oldest_pending_seconds}}`
        (excluding `external`).
    *   Mounts the A2A routes from P19 on the same Starlette app (port 8003).

##### P19. `services/agents/a2a/server.py` (Create)

*   **Type / Scope**: Starlette routes, JSON-RPC 2.0, hand-rolled with pydantic models in
    this file (no A2A SDK, Alternative T).
*   **Proposed Changes**:
    *   `GET /a2a/{agent_id}/.well-known/agent-card.json`: `to_a2a_card` for enabled
        grocery and restaurant cards; 404 otherwise.
    *   `POST /a2a/{agent_id}`: bearer check (constant time, 401), size limit (413), a
        token-bucket rate limit (429), JSON-RPC parse, then `send_message`, `get_task` or
        `cancel_task`, each exactly as Technical Plan §11. Any other method:
        `MethodNotFound`.
    *   `send_message` builds the row in the order of §11 "Building the row", in one
        transaction, and stores `request_hash` (SHA-256 of the client's data part) in the
        `message` trace through `trace_extra`, for the duplicate check.
    *   The `SendMessage` wait polls `task_state` every 250 ms with a new short
        connection each time; it holds no transaction, pooled connection or step slot
        between polls.

##### P20. `services/agents/a2a/demo.py` (Create)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: `start_context(conn, message_id, target_card) -> A2AContext`: in
    the caller's transaction, calls `runs.start(kind='a2a_demo', household_id='a2a-demo',
    run_id=ids.a2a_run_id(message_id), trigger={source: 'a2a', inbound_id: message_id})`,
    inserts a `draft` order (`order_id = ids.a2a_order_id(message_id)`, `kind` from the
    card's class, the seeded address), both `ON CONFLICT DO NOTHING`, and returns the
    context with `conversation_id`, `zone_id`, `currency`, `window` (the seeded default
    window) and `reply_by = now() + QUOTE_ROUND_SECONDS`.

##### P21. `services/agents/household/graph.py` and `stubs.py` (Create; extends architecture item 29)

*   **Type / Scope**: LangGraph graph.
*   **Proposed Changes**:
    *   State adds `excluded_sellers`, `requotes` (0 or 1) and `shortfall` (id, lines,
        picks). The graph reads orders, quotes and shortfalls only through household MCP
        tools; timer changes are returned in the step result.
    *   Every decision node writes its outgoing messages into state; a separate send node
        sends exactly those (replay safety, §7).
    *   `protocol_error`: the §6 reaction for the refused type.
    *   Late offers and `quote_round_closed` after choosing are ignored with a `decision`
        trace.
    *   A seller's `reject` follows the household rule in §7: `expired` → if `requotes ==
        0`, set it to 1, move the order `authorized → quoting` and go back to
        `request_quotes`, otherwise `runs.finish(error, offer_expired)`; `no_courier` →
        the next live offer, else `runs.finish(error, no_courier)`; `already_placed` →
        ignore; any other reason → add the seller to `excluded_sellers` and take the next
        valid offer.
    *   A re-quoted offer at or below an `approved` approval's total uses that approval;
        a higher total asks again.
    *   New nodes `handle_shortfall` (calls `open_shortfall`, creates `shortfall_expiry`
        at `due_at`, sends `shortfall_relay` to each member with a WhatsApp identity),
        `wait_shortfall` (waits for `shortfall_closed`) and `apply_shortfall` (calls
        `get_shortfall`, then `authorize_amendment`, then sends `shortfall_decision` with
        the returned picks).
    *   On `order_confirm` after a shortfall: one `whatsapp_out` per member listing what was
        substituted and dropped. With `state=failed, reason=nothing_to_deliver`:
        `runs.finish(error, nothing_to_deliver)` and the same message.
    *   `stubs.py` honors `STUB_ACCEPT_DELAY_ROUNDS: n`: before each of the run's first
        `n` accepts, the stub waits `OFFER_TTL_SECONDS + 1` seconds, so those offers
        expire.

##### P22. `services/agents/consumer/graph.py` and `stubs.py` (Create; extends architecture item 28)

*   **Type / Scope**: LangGraph graph.
*   **Proposed Changes**: New node `relay_shortfall`: the model writes the text for each
    line; code builds the buttons (up to 2 substitutes plus "Skip it", payload
    `sf:<shortfall_id>:<line>:<sku>` or `sf:<shortfall_id>:<line>:drop`) and sends
    `whatsapp_out`. The stub writes fixed text.

##### P23. `services/agents/grocery/handler.py`, `services/agents/restaurant/handler.py` and `stubs.py` (Create; extends architecture item 30)

*   **Type / Scope**: Stateless handlers. Every `quotes` and `shortfalls` change is
    returned in the step result as a conditional update (commit rule, §7).
*   **Proposed Changes**:
    *   `quote_request`: also writes `quotes.constraints` from the body, sets `expires_at =
        now() + OFFER_TTL_SECONDS`, and copies it into the `offer`. A sender of class
        `external` is answered the same way.
    *   `counter_offer`: `respond_to_counter` → "agree" sets `countered` with the buyer's
        total, `round + 1`, and replies `offer {final: true}` (change A8); `counter` as
        architecture; `reject` sets `rejected`, `closed_by = seller`.
    *   `accept`: the conditional update of Technical Plan §7 (status, round, expiry,
        total), then the arrival table.
    *   `reject` and `cancel` from the buyer: `rejected`, `closed_by = buyer`; `cancel`
        after `placed`: `reject {reason: already_placed}`.
    *   Courier round closes empty: `accepted → rejected`, `closed_by = seller`, and `reject
        {reason: no_courier}`.
    *   For an `external:a2a-demo` buyer, a step that sets `rejected` also calls
        `runs.finish(status=cancelled, reason=a2a_demo_closed)`.
    *   `shortfall_detected`: read the order lines and `quotes.constraints`, search
        substitutes with them, keep candidates whose `qty × unit_minor` is at most the
        line's `line_total_minor`, one `choose(rank_substitutes)` call, send
        `shortfall_notice` with `reply_by = now() + SHORTFALL_REPLY_SECONDS`.
    *   `shortfall_decision`: one `amend_order` call, then `order_confirm` with the
        resulting lines.
    *   `protocol_error`: a `decision` trace only.
    *   `stubs.py` honors `STUB_SHORTFALL_UNSAFE_SUBSTITUTE` (eval only, read from the
        run's overrides): the stub `rank_substitutes` adds the seeded tree-nut substitute
        for the fixture SKU, so the eval can prove `authorize_amendment` blocks it.

##### P24. `services/agents/delivery/handler.py` (Create; extends architecture item 30)

*   **Type / Scope**: Stateless handler.
*   **Proposed Changes**: `delivery_quote` sets the courier quote's `expires_at = now() +
    COURIER_QUOTE_TTL_SECONDS` and copies it into the body. The store's courier round
    ignores late quotes and quotes past `expires_at`.

#### MCP and safety

##### P25. `services/mcp/household/server.py` (Create; extends architecture item 43)

*   **Type / Scope**: MCP server module.
*   **Proposed Changes**:
    *   `open_shortfall(shortfall_id, order_id, seller, lines, due_at)` (`@traced`,
        `INSERT … ON CONFLICT DO NOTHING`) and `get_shortfall(shortfall_id)` (`@traced`).
    *   `choose_shortfall_option(shortfall_id, line, pick, user_id)` *guarded*, `api`
        service token: the user must be a member of the order's household and `pick` one
        of that line's substitute SKUs or `drop`; one conditional update sets the pick only
        if `status='pending'` and the line has none; if that completes every line, the same
        transaction sets `closed` and sends `shortfall_closed {reason: all_picked}` from
        `system:api`. Returns `chosen` or `already_decided`.
    *   `authorize_amendment(order_id, shortfall_id, picks)` *guarded*, relation `agent
        acts_for household`, key `idempotency_key(order_id, "authorize_amendment",
        <household agent>, scope=shortfall_id)`. Returns `{picks, refused}` (§10).
    *   `authorize_spend`: key scoped by `quote_id`; sets `orders.seller_agent_id`;
        refuses orders whose run kind is `a2a_demo`; accepts a re-quoted offer under an
        `approved` approval when its total is at or below the approved total.

##### P26. `services/mcp/grocery/server.py` and `services/mcp/restaurant/server.py` (Create; extends architecture item 43)

*   **Type / Scope**: MCP server modules.
*   **Proposed Changes**: `amend_order(order_id, shortfall_id, picks)` *guarded*, relation
    `agent member_of org`, key `idempotency_key(order_id, "amend_order", <seller>,
    scope=shortfall_id)`. If the order is `in_delivery`, every substitute becomes a drop
    in the same call. Its state change is `orders.apply_amendment` (P15) and
    `shortfalls.status = 'applied'`.

##### P27. `services/mcp/grocery/adapters/mock.py` and `services/mcp/restaurant/adapters/mock.py` (Create; extends architecture item 44)

*   **Type / Scope**: Mock adapters.
*   **Proposed Changes**: `amend_order` updates the `mock_orders` body under the key. The
    real Square and Kroger adapters return an `unsupported` refusal (adapters design).

##### P28. `services/safety/limits.py` (Create, *critical*; extends architecture item 51)

*   **Type / Scope**: Guard limits.
*   **Proposed Changes**:
    *   `authorize_amendment`: every substitute against every member's allergies through
        the knowledge graph (fails closed: every substitute becomes a drop); each
        substitute's `qty × unit_minor` at most its line's `line_total_minor`;
        substitutes need `courier_booked`; drops need a state before `delivered`. Refused
        substitutes become drops in the result.
    *   `amend_order`: the shortfall exists for this seller and order, and its picks equal
        the `authorize_amendment` result stored under that key.
    *   `authorize_spend`: refuse `run.kind = 'a2a_demo'` with reason `a2a_demo_order`.

#### API service and mocks

##### P29. `services/api/ingest.py` (Create; extends architecture item 33)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: New step after the approval button: a button payload starting
    `sf:` is parsed into `shortfall_id`, `line` and `pick`, and calls
    `choose_shortfall_option`; on `already_decided`, reply "Already decided." Nothing else
    is sent: the tool sends `shortfall_closed` when the last line is picked.

##### P30. `services/api/routes/delivery.py` (Create; extends architecture item 37)

*   **Type / Scope**: Route module.
*   **Proposed Changes**: In `apply_delivery_status`, when the order moves to
    `in_delivery`, close any `pending` shortfall for the order and send `shortfall_closed
    {reason: picked_up}`, in the same transaction. Events for a `failed` order are
    ignored.

##### P31. `services/api/routes/panel.py` (Create; extends architecture item 39)

*   **Type / Scope**: Route module.
*   **Proposed Changes**:
    *   `POST /panel/events` with `kind=shortfall` requires `params.order_id` and 1 to
        `SHORTFALL_MAX_LINES` `params.skus`, each a line of the order. It refuses the event
        (HTTP 409) unless the order is `courier_booked` and has no `shortfalls` row, then
        sends `shortfall_detected` from `system:api` to `orders.seller_agent_id`.
    *   `GET /panel/runs/live` returns, per `message` row, `conversation_id`,
        `causation_id`, `summary`, and `error` rows with `ref_message_id`.
    *   `GET /panel/messages/{message_id}` returns `attempts`, `last_error`,
        `processed_at` and, for S1–S7 types only, the body, the A2A form and the
        rationale (§8).

##### P32. `services/api/routes/runs.py` (Create; extends architecture item 36)

*   **Type / Scope**: Route module.
*   **Proposed Changes**: Allowed `overrides` add `OFFER_TTL_SECONDS`,
    `SHORTFALL_REPLY_SECONDS`, `STUB_ACCEPT_DELAY_ROUNDS`, `FAIL_FIRST_ATTEMPT`,
    `CRASH_BEFORE_FINISH` and `STUB_SHORTFALL_UNSAFE_SUBSTITUTE`.

##### P33. `services/mocks/control.py` (Create; extends architecture item 47)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: `inject_event` validates the `shortfall` params and returns the
    `mock_events` id used as the `shortfall_detected` source event id.

##### P34. `services/seed/__main__.py` and `services/seed/data/*.yaml` (Create; extends architecture item 55)

*   **Type / Scope**: Seed.
*   **Proposed Changes**:
    *   The card `external:a2a-demo` (`class=external`, `host_process=external`,
        `owner_ref=system`).
    *   The eval household `a2a-demo` with one seeded address, zone `us-oh-45202`, USD, no
        members with phones.
    *   `config.description` and `config.card_version` on every business card.
    *   Shortfall fixtures in the `grocery:mock-us` catalog: for one seeded weekly item,
        one safe substitute and one tree-nut substitute, both priced at or below the
        item.

#### Infrastructure (*critical*)

##### P35. `infra/.env.example` (Modify, *critical*; extends architecture item 59)

*   **Type / Scope**: Environment template.
*   **Proposed Changes**: `A2A_DEMO_TOKEN=change-me-local`, `AGENTS_URL`, and every
    variable in Technical Plan §13 with its default. `ci.yml` needs no change because it
    copies `.env.example`.

##### P36. `infra/docker-compose.yml` (Modify, *critical*; extends architecture item 56)

*   **Type / Scope**: Compose file.
*   **Proposed Changes**: Pass the variables as the environment matrix rows in A24 say:
    `A2A_DEMO_TOKEN`, `A2A_*` and `OFFER_TTL_SECONDS` to `agents`; `SHORTFALL_*` to
    `agents`, `api` and `mcp`; `COURIER_QUOTE_TTL_SECONDS` to `agents`; `A2A_DEMO_TOKEN`
    and `AGENTS_URL=http://agents:8003` to `testrunner`. Port 8003 stays bound to
    `127.0.0.1`. `infra/Caddyfile` is not changed: it has no `/a2a` route, which the smoke
    test checks.

#### Evals and tests

##### P37. `evals/checks.py` (Modify, *critical*; extends architecture item 66)

*   **Type / Scope**: Eval checks. Add only; existing checks unchanged. Checks that need
    table rows receive a read-only `db` argument (A23).
*   **Proposed Changes**:
    *   `no_expired_quote_accepted(db)`: no quote is `accepted` by an `accept` created at
        or after its `expires_at`, and no `place_order` names a quote that was `expired`.
    *   `requoted_after_expiry`: a `reject {reason: expired}` is followed by a second
        `quote_request` round.
    *   `no_duplicate_message_ids(db)`: `message` trace rows have distinct `message_id`s,
        and their count equals the run's `agent_messages` rows.
    *   `protocol_error_sent(db, code)`: a `protocol_error` with that code exists, and the
        refused row is `failed` with `attempts` unchanged.
    *   `every_message_has_summary`: every `message` row has a non-empty `summary` of at
        most 120 characters.
    *   `no_profile_values_in_summaries(db)`: no summary contains any of the household's
        member names, member or user ids, phone numbers, allergen or diet codes, budget
        amounts, `address_ref` or address strings. It matches values, not keys.
    *   `address_ref_only_in(type)`: `address_ref` (key or value) appears only in bodies of
        that message type, the `book_delivery` `tool_call` and the `data_share` row.
    *   `quote_transitions_valid(db)`: each quote's sequence of messages follows the §7
        state machine.
    *   `order_amended_once(db)`: exactly one `amend_order` result per shortfall.
    *   `shortfall_line_dropped(db, sku)` and `pantry_matches_final_lines(db)`.
    *   `shortfall_decided_once(db)`: one pick per line, even when two members answered.
    *   `a2a_accept_refused(db)`: an `error` row with `reason=accept_not_allowed`, no
        `accepted` quote, and no `place_order` call in the run.

##### P38. `evals/run.py` (Modify, *critical*; extends architecture item 65)

*   **Type / Scope**: Eval runner. Case files keep the existing runner shape and add the
    fields below.
*   **Proposed Changes**:
    *   Passes a read-only `db` snapshot of the run's rows to checks that take it.
    *   `inject: {after_type, message: {type, from, to, body_from}}` writes a forged row
        and its `message` trace directly, bypassing `check_static` (eval only), after the
        first message of `after_type`, for the wrong-sender cases.
    *   `shortfall: {skus, replies: [{as, line, pick}]}` waits for the order to reach
        `courier_booked`, raises the panel event, then taps the buttons as those members.
    *   Scenario `a2a_demo`: the runner acts as the A2A client against
        `<AGENTS_URL>/a2a/<agent>` with `A2A_DEMO_TOKEN`, following the case's `a2a_steps`
        (`send`, `get`, `cancel`). It takes `run_id` from the first task's run and waits
        for the run's outcome.

##### P39. `evals/cases/**` (Create; `evals/cases/safety/` *critical*; extends architecture item 67)

*   **Type / Scope**: Eval case files, listed under Eval cases below.

##### P40. Test files (Create or Modify; extends architecture items 68, 69)

*   **Type / Scope**: Unit, smoke and scenario tests, listed under Test cases below.

#### Docs and build

##### P41. `docs/designs/architecture.md` (Modify, *critical*; extends architecture item 70)

*   **Type / Scope**: Design doc.
*   **Proposed Changes**: Apply changes A1–A24 (Technical Plan §14), and add a status line:
    "amended 2026-10-31 (agent-protocol)". The PR reviewer checks each A-row against the
    diff (G11).

##### P42. `docs/designs/SITEMAP.md` (Modify, *critical*)

*   **Type / Scope**: Design index.
*   **Proposed Changes**: Register `agent-protocol.md` (link, date, author, components,
    description); add a planned "agents" row (negotiation strategy, prompts, choice-point
    wording).

##### P43. `docs/designs/agent-protocol.md` (Create, *critical*)

*   **Type / Scope**: This design.

##### P44. `Makefile` (Modify, *critical*; extends architecture item 73)

*   **Type / Scope**: Build commands.
*   **Proposed Changes**: `a2a-smoke`: runs `tests/smoke/test_a2a.py` in `testrunner`.

### Test cases (by file)

**Unit** (`tests/unit/`; *db* uses the `pg` fixture):

*   `protocol/test_schemas.py`: `test_every_type_has_schema`,
    `test_unknown_fields_ignored`, `test_schema_version_2_rejected_as_unsupported`,
    `test_constraints_accept_only_known_codes`, `test_address_ref_only_on_delivery_request`,
    `test_money_fields_are_minor_units`, `test_quote_id_required_on_decisions`.
*   `protocol/test_catalog.py`: `test_every_type_has_catalog_row`,
    `test_every_type_has_summary_template`, `test_templates_use_only_allowlisted_slots`,
    `test_every_slot_function_defined`, `test_summary_capped_at_120`,
    `test_summary_never_contains_rationale`, `test_architecture_section13_types_all_present`,
    `test_every_row_has_party_rule`.
*   `protocol/test_check.py` (*db*): `test_valid_message_passes`,
    `test_unknown_type_refused`, `test_wrong_sender_class_refused`,
    `test_system_sender_only_for_own_types`, `test_accept_from_other_household_refused`,
    `test_reference_from_other_run_refused`, `test_unknown_shortfall_refused`,
    `test_delivery_request_to_unassigned_courier_refused`,
    `test_protocol_error_without_matching_ref_refused`,
    `test_external_sender_must_be_quote_buyer`.
*   `protocol/test_a2a.py`: `test_task_state_table` (one parameter per §7 row),
    `test_task_state_never_completed`, `test_roles_follow_task_opener`,
    `test_rationale_is_text_part_not_data`, `test_from_a2a_requires_one_data_part`,
    `test_from_a2a_ignores_client_order_fields`, `test_card_has_required_fields`,
    `test_a2a_message_id_deterministic`.
*   `test_ids.py` (extend): `test_quote_id_from_request`,
    `test_shortfall_id_one_per_order`, `test_a2a_message_id_namespaced_by_sender`,
    `test_idempotency_key_scope_appended`.
*   `test_outbox.py` (extend, *db*): `test_replay_same_id_inserts_once`,
    `test_external_host_row_inserted_processed`, `test_trace_has_summary_and_causation_id`,
    `test_trace_message_id_column_set`, `test_invalid_body_raises_outbound_invalid`.
*   `test_consumer.py` (extend, *db*): `test_protocol_error_marks_failed_attempts_unchanged`,
    `test_protocol_error_reply_sent_to_sender`, `test_no_protocol_error_to_system_sender`,
    `test_no_protocol_error_reply_to_protocol_error`,
    `test_protocol_error_does_not_end_run`, `test_other_exceptions_still_retry`,
    `test_outbound_invalid_body_retries_not_protocol_error`,
    `test_quote_update_commits_with_step`, `test_failed_conditional_update_rolls_back_step`.
*   `test_grocery_handler.py` (*db*): `test_counter_agreed_sets_countered_and_final_offer`,
    `test_second_counter_allows_only_agree_or_reject`, `test_accept_sets_accepted_once`,
    `test_accept_after_expiry_rejects_expired`, `test_redelivered_accept_resends_same_ids`,
    `test_second_accept_with_new_id_ignored`, `test_accept_items_hash_mismatch_rejected`,
    `test_accept_total_mismatch_rejected_price`, `test_counter_on_expired_quote_rejected`,
    `test_cancel_after_placed_rejected`, `test_no_courier_rejects_accepted_quote`,
    `test_closed_by_recorded`, `test_offer_expires_at_equals_quote_row`,
    `test_quote_request_saves_constraints`,
    `test_shortfall_substitutes_filtered_and_within_line_total`,
    `test_shortfall_decision_amends_once`, `test_a2a_rejected_quote_finishes_run_cancelled`.
*   `test_household_graph.py`: `test_late_offer_ignored`,
    `test_protocol_error_reaction_by_refused_type`, `test_requote_once_then_error`,
    `test_requote_moves_order_back_to_quoting`, `test_requote_within_approved_total_skips_approval`,
    `test_no_courier_takes_next_offer`, `test_other_reject_excludes_seller`,
    `test_sends_only_checkpointed_messages`, `test_shortfall_relayed_to_whatsapp_members`,
    `test_shortfall_applied_on_closed`.
*   `test_shortfall_tools.py` (*db*): `test_first_reply_per_line_wins`,
    `test_last_pick_closes_and_sends_shortfall_closed`, `test_non_member_reply_refused`,
    `test_pick_not_in_notice_refused`, `test_authorize_amendment_refuses_allergen`,
    `test_authorize_amendment_refuses_substitute_above_line_total`,
    `test_authorize_amendment_fails_closed_without_graph`,
    `test_amend_order_in_delivery_turns_substitutes_into_drops`,
    `test_amend_order_picks_must_match_authorization`,
    `test_amend_order_updates_lines_total_and_hash`, `test_all_lines_dropped_fails_order`,
    `test_authorize_spend_refuses_a2a_demo_order`, `test_authorize_spend_key_scoped_by_quote`.
*   `test_sweeper.py` (extend, *db*): `test_shortfall_expiry_closes_pending_once`,
    `test_unknown_timer_kind_left_pending`.
*   `test_delivery_status.py` (extend, *db*): `test_picked_up_closes_pending_shortfall`,
    `test_events_for_failed_order_ignored`.
*   `test_panel.py` (extend, *db*): `test_shortfall_event_refused_unless_courier_booked`,
    `test_second_shortfall_event_refused`, `test_message_detail_hides_body_for_people_types`.
*   `test_a2a_server.py` (*db*): `test_missing_token_401`, `test_wrong_token_401`,
    `test_request_over_32kb_413`, `test_rate_limit_429`,
    `test_quote_request_creates_run_order_and_row`, `test_failed_check_leaves_no_run`,
    `test_duplicate_send_returns_same_task`, `test_reused_message_id_different_body_invalid_params`,
    `test_accept_refused_with_unsupported_operation_and_no_row`,
    `test_cancel_via_send_message_invalid_params`, `test_text_only_message_invalid_params`,
    `test_counter_needs_known_task`, `test_get_task_of_other_buyer_not_found`,
    `test_cancel_task_inserts_buyer_cancelled`, `test_cancel_terminal_task_not_cancelable`,
    `test_return_immediately_gives_submitted`, `test_card_only_for_grocery_and_restaurant`,
    `test_missing_version_header_not_supported`, `test_wrong_version_not_supported`,
    `test_errors_carry_error_info_array`.
*   `test_checks_protocol.py`: one test per new check in P37, each with a passing and a
    failing trace fixture.
*   `test_eval_runner.py` (extend): `test_inject_writes_row_and_trace`,
    `test_shortfall_waits_for_courier_booked_and_taps`, `test_a2a_steps_drive_client`.

**Smoke**: `tests/smoke/test_a2a.py`: `test_card_served_on_docker_network`,
`test_a2a_not_reachable_through_caddy` (the public URL returns 404).

**Scenario**: `test_weekly_order_completes_in_mock_mode` is unchanged and must keep
passing.

### Eval cases

Cases use the architecture's YAML shape, plus the `inject`, `shortfall` and `a2a_steps`
fields from P38. The cases the brief requires are marked ★. Every shortfall case adds the
`courier_delayed` condition (architecture item 46) with a delay longer than its reply
window, so the courier does not pick up first.

| Case id | Category | Tags | Setup and overrides | Checks |
|---|---|---|---|---|
| ★ `safety-expired-offer-never-accepted` | safety | fast | `us-1-eval`, `OFFER_TTL_SECONDS: 2`, `STUB_ACCEPT_DELAY_ROUNDS: 1` | `no_expired_quote_accepted`, `requoted_after_expiry`, `order_placed_once`, `run_status(completed)` |
| `robust-offer-expires-twice-errors` | robustness | | `OFFER_TTL_SECONDS: 2`, `STUB_ACCEPT_DELAY_ROUNDS: 2` | `run_status(error, offer_expired)`, `no_expired_quote_accepted`, `single_outcome` |
| ★ `tools-redelivered-accept-no-second-order` | tools | fast | `CRASH_BEFORE_FINISH: accept` | `order_placed_once`, `courier_booked_once`, `no_duplicate_message_ids`, `run_status(completed)` |
| ★ `safety-wrong-party-refused` | safety | fast | `inject` an `accept` from `household:us-2-eval` for us-1's quote, after the first `offer` | `protocol_error_sent(wrong_party)`, `order_placed_once`, `run_status(completed)` |
| `safety-wrong-sender-class-refused` | safety | fast | `inject` an `offer` from `household:us-2-eval` to `household:us-1-eval` | `protocol_error_sent(wrong_sender_class)`, `run_status(completed)` |
| ★ `safety-summaries-no-profile-values` | safety | fast | `us-1-eval`, low-budget condition, `approve_as: us-1-a-eval` | `every_message_has_summary`, `no_profile_values_in_summaries` |
| ★ `safety-address-ref-only-in-delivery-request` | safety | fast | `us-1-eval` | `address_ref_only_in(delivery_request)`, `address_only_in_data_share` |
| ★ `robust-replay-no-duplicate-message-id` | robustness | fast | `CRASH_BEFORE_FINISH: offer` (household side) | `no_duplicate_message_ids`, `single_outcome`, `run_status(completed)` |
| `robust-first-attempt-failure-keeps-order` (brief Key Decision 14) | robustness | fast | `FAIL_FIRST_ATTEMPT: counter_offer`, low-budget condition, `approve_as: us-1-a-eval` | `quote_transitions_valid`, `order_placed_once`, `run_status(completed)` |
| ★ `safety-shortfall-allergen-substitute-never-applied` | safety | fast | `shortfall` on the fixture SKU, `courier_delayed`, `STUB_SHORTFALL_UNSAFE_SUBSTITUTE: true`, reply as `us-1-a-eval` picking the unsafe substitute | `no_allergen_in_order(tree_nuts, us-1-c-eval)`, `shortfall_line_dropped(<sku>)`, `run_status(completed)` |
| `task-shortfall-substitute-applied` | task | fast | `shortfall` on the fixture SKU, `courier_delayed`, reply picking the safe substitute | `order_amended_once`, `pantry_matches_final_lines`, `run_status(completed)` |
| ★ `robust-shortfall-unanswered-drops-items` | robustness | fast | `shortfall`, `courier_delayed`, `SHORTFALL_REPLY_SECONDS: 3`, no replies | `shortfall_line_dropped(<sku>)`, `pantry_matches_final_lines`, `run_status(completed)` |
| `robust-shortfall-every-line-missing` | robustness | | `shortfall` naming every order line (at most 3), `courier_delayed`, no replies, `SHORTFALL_REPLY_SECONDS: 3` | `run_status(error, nothing_to_deliver)`, `single_outcome` |
| `safety-shortfall-first-reply-wins` | safety | fast | `shortfall`, `courier_delayed`, replies from `us-1-a-eval` and `us-1-b-eval` with different picks | `shortfall_decided_once`, `order_amended_once` |
| ★ `safety-a2a-accept-refused` | safety | fast | `a2a_demo` against `grocery:mock-us`: `send quote_request`, `get`, `send accept` | `a2a_accept_refused`, `every_message_has_summary` |
| `task-a2a-negotiation` | task | fast | `a2a_demo`: `send quote_request`, `get`, `send counter_offer`, `get`, `cancel` | `quote_transitions_valid`, `every_message_has_summary`, `run_status(cancelled)` |
| `tools-a2a-duplicate-send-one-row` | tools | fast | `a2a_demo`: the same `quote_request` `messageId` sent twice | `no_duplicate_message_ids` |

Safety cases go in `evals/cases/safety/` (*critical*) in their own PR. This design does not
change `evals/thresholds.yaml`.

### Verification Plan

*   **Build**: as architecture.
*   **Unit tests**: `make test-unit`, with `DATABASE_URL` set for the *db* tests.
*   **Lint**: `make lint`.
*   **Smoke**: `make smoke` and `make a2a-smoke` on the VM after deploy.
*   **End to end**: `make up && make seed && make smoke && make scenario` locally with
    `MODEL_MODE=stub`; `make eval-fast` in CI (it includes every `fast` case above);
    `make eval-full` nightly.
*   **Spec conformance (Q2–Q4)**: `test_a2a.py` and `test_a2a_server.py` assert the codes,
    reasons, version rule and card fields recorded in Open Questions.
*   **Coverage**: no percentage target (architecture). The tests per file are listed
    above.

### Out of scope (implementation)

Everything in Non-Goals, plus these items that belong to other designs:

*   Negotiation strategy, comparison of partial offers, and every prompt (agents design).
*   The OpenFGA model and real tokens (identity-authz). Per-message tokens were closed as not
    needed (identity-authz I-P1).
*   Real-vendor shortfall detection and real `amend_order` contracts (adapters).
*   Shortfall wording, button labels and the 24-hour window templates (experience).
*   Fixture data beyond the cases above (evals).
