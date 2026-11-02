# Kitchenless pantry: estimated inventory and the code-only skip

*   **Author:** madushankaf
*   **Status:** Approved
*   **Date:** 2026-11-02
*   **Path:** `docs/designs/pantry.md`
*   **Issue:** #251
*   **Builds on:** [architecture.md](architecture.md), [agent-protocol.md](agent-protocol.md),
    [identity-authz.md](identity-authz.md), [adapters.md](adapters.md),
    [agents.md](agents.md) and [evals.md](evals.md), all approved. Where this design needs one
    of them to change, the table in Technical Plan §1 lists the change. Nothing here relies on
    a change that table doesn't list.
*   **Input:** the pantry brainstorm brief (two review rounds; owner decisions PD1–PD15 and
    RQ1–RQ8), the EGM review of this design on 2026-11-02, and the code on `main` at
    `63a52bc`. The brief lives in `.loop-logs/`, which is not in the repository, so every
    decision this design relies on is written out in full below.

**Conventions:**

*   **Ids.** Owner decisions are PT-OD1…, goals PG1…, journeys PT-CUJ 1…, changes to other
    designs PT-A1…, alternatives PT-ALT1…. "&lt;design&gt; §N" or "&lt;design&gt; item N" means
    a section or item of that design. A bare "§N" means section N of this design's Technical
    Plan.
*   **Pantry item.** A name from the pantry vocabulary (§2), for example `milk` or `rice`. It
    is not a SKU and not a list item's free text.
*   **Event kinds.** `add`: a delivery brought it. `verify`: someone confirmed it is at home.
    `out`: someone said it ran out. In this design, deliveries write `add`, and only the eval
    setup key writes `verify` and `out`. The deferred pantry check (Future Improvements) will
    write them for real households.
*   **Present.** For one household and one pantry item: its latest event (highest
    `event_id`) is `add` or `verify`.
*   **Trust window.** Each vocabulary item has a window of `window_days` days. An item is
    **trusted** when it is present and the latest event is less than `window_days × 24`
    hours old. **Stale** means present but not trusted.
*   **Pantry on.** The skip applies to a household only when `households.env = 'eval'`
    (PT-OD4). For every other household the pantry is **off**: events are still recorded,
    but nothing is ever skipped.
*   **Weekly order.** A `weekly` run whose order has `kind = 'grocery'` (architecture §8).
    Dinner and instant orders never use the pantry.

--------------------------------------------------------------------------------

## Overview

The project brief (§2.9) describes the pantry as an estimated inventory built from events,
whose confidence decays over time, so that the weekly order can skip what is already at home.
On `main` today the pantry exists but never changes an order:

*   `record_delivery` writes the SKU (for example `MOCK-MILK-WHOLE`) into
    `pantry_events.item`, always with unit `each` (`services/mcp/household/server.py:597-628`).
*   `build_list` lower-cases the list's item names (`milk`, `eggs`) and drops those found in the
    pantry (`services/agents/household/graph.py:739-775`). A name never equals a SKU, so nothing
    is ever dropped.
*   The `build_list` prompt (`services/agents/household/prompts.py:8-9`) tells the model "Skip
    what the pantry has" and sends it the whole pantry, so the model may leave items off on
    its own, with no trace.
*   Nothing ever removes stock, so anything delivered once counts as at home forever.

This design makes the skip work, explainable and safe, on eval households first:

1.  **One vocabulary** of 11 pantry items, with catalog SKUs and list names mapped to it in
    code.
2.  **Presence, not quantity:** each item is present or not, decided by its latest event.
3.  **Trust that runs out:** each item stays trusted for a fixed window after its last `add`
    or `verify`.
4.  **A code-only skip:** on eval households, the weekly list drops trusted items, writes one
    trace row per item, names the skips in the summary, and never asks the model to do it.
5.  **Live households are unchanged in effect:** their deliveries now record pantry items, so
    history builds up, but nothing is skipped until a way to correct a wrong skip exists
    (PT-OD7).
6.  **Eval cases and an eval-household demo** that make all of this repeatable.

The pantry check (asking a member about stale items), photos, receipts, typed notes and
cooked-meal deductions are left for later (PT-OD6, PT-OD8).

### Owner decisions

The brainstorm's decisions (PD, RQ) were accepted by the owner on 2026-11-02. The review of
this design on the same day changed the scope (marked "review"): the skip is limited to eval
households, the pantry check is deferred, and the demo runs on an eval household.

| ID | Decision | From |
|---|---|---|
| PT-OD1 | **Presence-only event log.** `pantry_events` stays the one store. Each event has a `kind` (`add`, `verify`, `out`). The latest event per household and item, by `event_id`, decides presence. No quantities, no "low". | PD3 |
| PT-OD2 | **A fixed vocabulary.** 11 names, the ones the mock catalog supports, in seed YAML. Catalog SKUs map through `catalog_items.pantry_item`. List names match a vocabulary name or alias exactly, in code. An unmatched list item is never skipped. Old rows without a pantry item are never matched. | PD12, PD14, PD15, RQ7 |
| PT-OD3 | **Trust window.** The skip rule is "present and inside its window". A confidence is computed alongside, from a constant in code, and written into traces; no rule reads it. | PD2, RQ4 |
| PT-OD4 | **Code-only skip on weekly grocery orders of eval households.** The `build_list` prompt no longer receives the pantry or a skip instruction, for every household. On an eval household, code drops trusted items after the model answers. On any other household, code keeps every item with reason `pantry_off`. | brief, revision item 3; review |
| PT-OD5 | **Everything at home.** When the skip empties the list, the draft order is cancelled, no quote request is sent, and the run ends `completed` with reason `nothing_needed`. | PD11 |
| PT-OD6 | **The pantry check is deferred.** No message asks a member about the pantry in this design. The check's shape, and the fixes the review found it needs, are recorded under Future Improvements. | review; supersedes PD1, PD4, PD5, PD8, PD9 and RQ2, RQ3, RQ5, RQ8 here |
| PT-OD7 | **Lifting the gate needs a correction.** The skip is turned on for live households only by a later design that gives a household a way to correct a wrong skip. The candidate is a "Ran out" button on the order summary, built like the take-turns button. | review |
| PT-OD8 | **Deferred sources.** Photos, receipts, typed notes ("we're out of sugar"), voice, barcodes and cooked-meal deductions are not in this design. This changes the brief's MVP (§2.8, §2.9). | PD6 |
| PT-OD9 | **No undo.** There is no undo window. With PT-OD4, a wrong skip can only happen on an eval household. | PD10, RQ1 |
| PT-OD10 | **Evals.** Pantry state for eval cases comes from an opt-in `pantry` setup key, undone by the next reset. The base eval seed has no pantry events. | PD13 |
| PT-OD11 | **Demo on `us-1-eval`.** The demo sets the pantry through `POST /panel/reset` with the `pantry` setup key, then runs a weekly order. There is no live demo household, no seed refresh flag and no live-household exception. | review; supersedes PD13's demo part and RQ6 |
| PT-OD12 | **Rollback is safe.** The migration is additive and adds no CHECK constraint. Events that aren't deliveries write `delta 0`, and `get_pantry` keeps its current `items` field, so older code reading new rows never skips anything. | round 2 |

## The Problem

1.  **The skip never fires.** Deliveries store SKUs and the list compares names, so the
    weekly order re-buys what the household has. The feature the brief puts first ("skip what
    is already at home") does nothing.
2.  **Stock only goes up.** Nothing removes an item, so once problem 1 is fixed, a single
    delivery would make an item count as at home forever, and it would be skipped long after
    it ran out.
3.  **The model skips without a trace.** The prompt asks the model to skip and gives it the
    whole pantry. Its omissions aren't recorded, can't be explained to the household, and
    break the rule that code, not the model, decides what is safe to leave out.
4.  **There is no shared name for an item.** A delivery knows `MOCK-MILK-2PCT` and a list says
    "milk". Nothing maps them to each other, and nothing stops "almond milk" counting as
    "milk".
5.  **An all-at-home order would crash.** If the pantry emptied the list, `build_list` raises
    `InvalidOutput("build_list returned no items")` (`graph.py:775`), which ends the run as a
    model failure.
6.  **A wrong skip can't be corrected.** No message, button or tool lets a household say "we
    ran out". Turning the skip on for real families before that exists would trade a
    duplicate item for a missing one.
7.  **No eval or demo can show it.** The base eval seed has no pantry, and adding one would
    change existing cases (for example the shortfall fixture's milk).

This design solves 1–5 and 7. It solves 6 by not skipping on live households (PT-OD4,
PT-OD7).

## Goals

Every goal names the test or check that verifies it. Unit tests run with `MODE=mock` and
`MODEL_MODE=stub` (CLAUDE.md).

*   **PG1. A weekly order on an eval household skips exactly the trusted items.** An item on
    the list is dropped when, and only when, it matches a vocabulary item that is trusted when
    `build_list` runs. *Checked by:* `tests/unit/test_pantry.py` (the rule),
    `tests/unit/test_household_graph.py::test_build_list_skips_trusted_items`, and the eval
    case `task-weekly-order-skips-pantry-items`.
*   **PG2. A duplicate is always preferred to a missing item.** An unmatched list item, an
    item with no events, an old row without a pantry item, a stale item, an `out` item and
    any pantry read failure all lead to ordering. *Checked by:* `tests/unit/test_pantry.py`,
    `test_household_graph.py::test_build_list_keeps_stale_out_absent`,
    `::test_build_list_unmatched_kept` and `::test_build_list_pantry_unavailable_keeps_all`.
*   **PG3. Only code skips.** The `build_list` prompt contains no pantry data and no
    instruction to skip, on every household. *Checked by:*
    `test_household_graph.py::test_build_list_prompt_has_no_pantry`.
*   **PG4. Every skip can be explained.** Each list item on a weekly grocery order writes one
    decision row (`pantry_skipped`, `pantry_kept` with a reason, or `pantry_unmatched`) whose
    `facts` hold the event, its age, the window, the confidence and the vocabulary version.
    The order summary lists the skipped items. *Checked by:*
    `test_household_graph.py::test_build_list_decision_facts`, `::test_summary_names_skipped_items`
    and the eval check `pantry_decisions_traced`.
*   **PG5. An order with everything at home ends cleanly.** The order is cancelled, no
    `quote_request` is sent, the requester is told, and the run ends `completed` with reason
    `nothing_needed`. *Checked by:* `test_household_graph.py::test_build_list_all_skipped_nothing_needed`
    and the eval case `task-weekly-order-nothing-needed`.
*   **PG6. A live household's order never loses an item to the pantry.** On a household whose
    `env` isn't `eval`, `get_pantry` reports the pantry off and `build_list` keeps every item.
    *Checked by:* `tests/unit/test_household_tools.py::test_get_pantry_off_for_live_household`
    and `test_household_graph.py::test_build_list_pantry_off_keeps_all`.
*   **PG7. Business agents never see the pantry.** No `quote_request` or other message to a
    business agent carries a pantry field or the skip list. *Checked by:*
    `test_household_graph.py::test_quote_request_has_no_pantry_fields`.
*   **PG8. Rollback never causes a skip.** The current `get_pantry` query, run over rows the new
    code writes, returns no item that wasn't delivered. *Checked by:*
    `test_household_tools.py::test_get_pantry_items_unchanged_by_new_rows`.
*   **PG9. Evals and the demo start from a known pantry.** The `pantry` setup key sets an eval
    household's pantry for one case or one demo run, the next reset removes it, and the base
    eval seed stays empty. *Checked by:* `tests/unit/test_panel_reset.py::test_pantry_setup_key`
    and `::test_pantry_setup_undone_by_reset`.

## Non-Goals

*   **No pantry check** or any other message that asks about the pantry (PT-OD6).
*   **No skip on live households** (PT-OD4, PT-OD7).
*   **No photos, receipts, voice, barcodes, typed notes or cooked-meal deductions** (PT-OD8).
    `source` and `kind` stay unconstrained text, so they can be added later.
*   **No quantities,** "low", consumption rates, expiry dates, shelf locations or restock
    rules (PT-OD1).
*   **No per-household settings.** Windows are per item and platform-wide.
*   **No new order state, run kind, timer kind, table, projection or cache.**
*   **No change to agent-protocol message schemas,** `ingest`, `POST /runs` or `runs.py`.
*   **No pantry for dinner or instant orders.**
*   **No model calls** anywhere in the pantry logic.

## Critical User Journeys (CUJs)

### PT-CUJ 1: The weekly order skips what is at home

*   **Given**: on `us-1-eval`, an eval case's `pantry` setup gives milk an `add` 1 hour old
    (window 3 days), rice an `add` 400 hours old (window 14 days = 336 hours), and eggs an
    `add` 5 hours old followed by an `out` 1 hour old.
*   **When**: a weekly order runs and the stub model lists milk, eggs, bread, bananas and rice.
*   **Then**:
    1.  Code drops milk (trusted). It keeps eggs (reason `out`), rice (`stale`), and bread and
        bananas (`absent`, no events).
    2.  Five decision rows are written: one `pantry_skipped` and four `pantry_kept`.
    3.  The quote request lists eggs, bread, bananas and rice only and carries no pantry data.
    4.  The summary sent after delivery says "Skipped, already at home: milk."

A real household ordering weekly sees fewer skips than this: a delivery a week ago leaves
milk (3 days), bread (3), bananas (4) and chicken (2) stale. §2 explains why.

### PT-CUJ 2: Everything is at home

*   **Given**: every item the stub model lists for `us-1-eval` has an `add` 1 hour old.
*   **When**: a weekly order runs.
*   **Then**: no `quote_request` is sent, the draft order is cancelled, the run ends
    `completed` with reason `nothing_needed`, and the requester gets "Nothing to order this
    week. Already at home: milk, eggs, bread, bananas, rice."

### PT-CUJ 3: A live household's weekly order

*   **Given**: `us-1` (env `live`) had a delivery yesterday, which `record_delivery` wrote with
    pantry items.
*   **When**: its weekly order runs.
*   **Then**: `get_pantry` reports the pantry off. `build_list` keeps every item and writes
    `pantry_kept` rows with reason `pantry_off`. The order goes ahead as today, and the
    summary has no "Skipped" line.

### PT-CUJ 4: The demo

*   **Given**: the operator calls `POST /panel/reset` for `us-1-eval` with the `pantry` setup
    of PT-CUJ 1, but with eggs given only an `add` 1 hour old.
*   **When**: the operator starts a weekly order on `us-1-eval` and watches the live view.
*   **Then**: the order is for bread, bananas and rice; the summary on the simulated phone
    says "Skipped, already at home: milk, eggs."; the trace rows show each decision and its
    facts. A second reset with every item fresh shows the `nothing_needed` ending.

--------------------------------------------------------------------------------

## Technical Plan (Architecture)

### 1. Changes to approved designs

Rows PT-A1–PT-A12 each become a dated "*Amended 2026-11-02 (pantry, #251):*"
note at its **Amends** location, written in build step P1. PT-A13 is §11 of this plan, and
PT-A14 is the SITEMAP row.

| # | Amends | Change | Why |
|---|---|---|---|
| PT-A1 | architecture Non-Goals ("the pantry event model, confidence decay, photos and receipts") and R9 (media) | The event model, the trust window and confidence are owned by pantry.md. Photos, receipts, WhatsApp media and the pantry check stay out of scope, for a later design. | PT-OD6, PT-OD8 |
| PT-A2 | architecture §5 tool table and item 43 (`/mcp/household`) | `record_delivery` also writes `pantry_item`, `kind` and `run_id`. `get_pantry` keeps `items` unchanged and adds `enabled`, `as_of` and `estimates`. | §3, §4 |
| PT-A3 | architecture §8 (weekly-order flow), item 29, and the lines saying a `completed` run placed an order (architecture.md:153, :307) | On eval households, `build_list` skips in code only (§5). When the skip empties the list, the order goes `draft → cancelled` and the run ends `completed`, reason `nothing_needed`, with no order placed. | PT-OD4, PT-OD5 |
| PT-A4 | architecture §9 (failure handling) | A failed vocabulary load or `get_pantry` call means no skip. | PG2 |
| PT-A5 | architecture §13 (provisional `pantry_events`) and the migrations list | `0014_pantry.sql` (§3). `item` keeps the SKU; `pantry_updated_once` keeps its meaning. | PT-OD12 |
| PT-A6 | architecture item 55 (seed) | `catalog_items.pantry_item` values in `catalog_mock.yaml`, the vocabulary file `pantry_items.yaml`, and a seed check that every `pantry_item` is in the vocabulary. | §2 |
| PT-A7 | agents.md Non-Goals (pantry), §6 `build_list`, prompt 5 | The prompt loses the pantry and the skip instruction and gets "Use these names for the items you choose, where they fit: {pantry_names}". Code skips after the item-name check and before the dislike rule (§5). | PT-OD4 |
| PT-A8 | agents.md §11 (`order_summary`, texts) | The summary gains "Skipped, already at home: …". New text `nothing_needed`. | PG4, PG5 |
| PT-A9 | agents.md §15 (traces) | New `build_list` decision choices `pantry_skipped`, `pantry_kept` and `pantry_unmatched`, using the `facts` payload key from evals E-A12 (already on `main`). | PG4 |
| PT-A10 | agents.md §17 (state) | New state fields `pantry_skipped` (list) and `pantry_as_of`, defaulted when absent; `STATE_VERSION` stays 1. | §5 |
| PT-A11 | evals.md Non-Goal "No pantry eval cases", §4 (setup keys), §5 (checks), coverage prefixes | Setup key `pantry`; checks `pantry_decisions_traced` and `no_quote_requests`; the cases of §8, with prefix entries and a `pantry` goal section in `evals/coverage.yaml`. | PT-OD10 |
| PT-A12 | identity-authz household-scoped table list and the unscoped allowlist | No new household-scoped table. The `pantry` setup key, which writes eval `pantry_events` inside the reset, joins the unscoped allowlist. | §8 |
| PT-A13 | PROJECT_BRIEF §2.8, §2.9 and the §4 stack row for SAM 3.1 (photo segmentation) | §11 records each deviation. The brief itself is not edited. | PT-OD6, PT-OD8 |
| PT-A14 | SITEMAP.md | The pantry row. | Index |

CLAUDE.md is unchanged: no CHECK constraint is added or widened, and nothing is dropped or
renamed.

### 2. Vocabulary and matching

*   **File:** `services/seed/data/pantry_items.yaml`. Each entry has `name`, `aliases` (plurals
    and common variants, listed explicitly) and `window_days`. Launch list, from the mock
    catalog and the stub grocery list:

    | Name | Aliases | Window (days) |
    |---|---|---|
    | `milk` | `whole milk`, `2% milk`, `reduced fat milk` | 3 |
    | `almond milk` | | 7 |
    | `eggs` | `egg`, `large eggs` | 7 |
    | `bread` | `wheat bread`, `white bread`, `sandwich bread` | 3 |
    | `bananas` | `banana` | 4 |
    | `apples` | `apple` | 10 |
    | `chicken breast` | `chicken` | 2 |
    | `rice` | `white rice` | 14 |
    | `pasta` | `penne` | 14 |
    | `granola` | | 14 |
    | `almonds` | `almond` | 14 |

*   **Weekly cadence.** A weekly order sees last week's delivery about 7 days old. So from
    deliveries alone, only items with windows over 7 days (apples, rice, pasta, granola,
    almonds) are skipped; eggs sit on the edge. This is deliberate: the windows fail toward
    ordering. Longer windows for staples become safe only with a correction path (PT-OD7).
*   **No allergen or diet data** is in the vocabulary. `almond milk` is its own item, never an
    alias of `milk`.
*   **Catalog link:** `catalog_items.pantry_item` names the vocabulary item for a SKU (for
    example `MOCK-MILK-2PCT → milk`). A SKU with no `pantry_item` writes events that are never
    matched.
*   **Matching** (`services/common/pantry.py`, pure): lower-case, trim and collapse spaces, then
    look up the exact name or alias. No stemming, no fuzzy match, no model. No hit means
    `unmatched`. This is deliberately stricter than `dislikes.matches`, which drops items: a
    looser pantry match would skip items wrongly.
*   **Version:** the vocabulary's version is the sha256 of the YAML file's bytes, computed when
    it is loaded. It goes into every pantry decision row, so a trace shows which list was used.
    `mcp` and `agents` each load the file. A mismatch during a deploy can only make a name
    unmatched, which keeps the item.

### 3. Data model (`migrations/0014_pantry.sql`, additive)

*   **`pantry_events`** gains:
    *   `pantry_item text NULL`: the vocabulary name. `NULL` marks a legacy or unmapped row,
        which is never matched (PT-OD2).
    *   `kind text NOT NULL DEFAULT 'add'`. Code accepts `add`, `verify` and `out`
        (`pantry.KINDS`). Old code that inserts without `kind` still writes a correct `add`. A
        CHECK constraint is left out so that a later source (the pantry check, receipts) needs
        no migration to add a kind.
    *   `run_id uuid NULL REFERENCES runs`: the run that wrote the event, so triage can join an
        event to its traces.
    *   index `pantry_events_item_latest (household_id, pantry_item, event_id DESC)`.
*   **`catalog_items`** gains `pantry_item text NULL`.
*   No new table.

**Writing rules.**

| Writer | `item` | `pantry_item` | `kind` | `delta` | `unit` | `source` | `ts` |
|---|---|---|---|---|---|---|---|
| `record_delivery` | SKU | from `catalog_items.pantry_item` | `add` | quantity | `each` | `delivery` | now |
| `pantry` eval setup key | vocabulary name | vocabulary name | as given | 0 | `each` | `eval_setup` | now − `age_hours` |

The setup-key row has `delta = 0`, so the current `get_pantry` query (`HAVING sum(delta) > 0`)
never returns it (PT-OD12).

### 4. The estimate

`get_pantry` (household MCP, `@traced`, read-only, unguarded as today) returns:

*   `items`: exactly as today (rollback safety);
*   `enabled`: `true` only when the run's household has `env = 'eval'` (PT-OD4);
*   `as_of`: the database's `now()` for this read, as an ISO-8601 UTC string;
*   `estimates`: when `enabled`, one entry per vocabulary item with at least one event, each
    with `pantry_item`, `latest_event_id`, `kind`, `age_hours`, `window_hours`, `present`,
    `trusted` and `confidence`; when not `enabled`, `[]`.

Rules (`services/common/pantry.py`, pure, `as_of` passed in):

*   the latest event is the one with the highest `event_id` for `(household_id, pantry_item)`
    among rows with `pantry_item IS NOT NULL`; `age = as_of − ts`;
*   `present = kind in (add, verify)`;
*   `trusted = present and age < window`;
*   `confidence = max(0, 1 − (1 − θ) · age / window)` when present, else 0, with `θ = 0.7` a
    code constant. It reaches θ exactly when the window ends. No rule reads it; it is written
    into decision rows so that the brief's "confidence decays" is visible in traces (PT-OD3).

### 5. The weekly order

`build_list` (household graph) on a weekly order with order kind `grocery`, after the model
answers and the existing item-name check (`item_name_ok`), and before the AG5 dislike rule:

1.  Read `get_pantry` (already called at the top of `build_list`). If the call failed, keep
    every item with reason `pantry_unavailable`. If `enabled` is false, or the field is
    missing (older `mcp`), keep every item with reason `pantry_off`.
2.  Otherwise, for each item: match it against the vocabulary, then
    *   unmatched → keep, decision `pantry_unmatched`;
    *   matched and trusted → skip, decision `pantry_skipped`;
    *   matched and not trusted → keep, decision `pantry_kept` with reason `absent` (no events
        for the item), `stale` or `out`.
3.  If the vocabulary fails to load, keep every item with reason `pantry_unavailable`.
4.  Each decision row carries `facts`: `list_name`, `pantry_item`, `latest_event_id`, `kind`,
    `age_hours`, `window_hours`, `confidence`, `as_of`, `vocabulary_version` and, on
    `pantry_kept`, `reason`. Fields that don't apply are `null`.
5.  Store the skipped names in `pantry_skipped` and `as_of` in `pantry_as_of`.
6.  If the skip emptied the list: route to a new `nothing_needed` node instead of
    `request_quotes`. It moves the order `draft → cancelled`, sends the requester
    `nothing_needed` with the skipped names, and ends the run `completed` with reason
    `nothing_needed`. No `quote_request` is sent. This replaces today's
    `InvalidOutput("build_list returned no items")` after the pantry filter. An empty or
    invalid model list is still `InvalidOutput`, as today. The dislike rule runs after the
    skip and never empties a list (`apply_dislike_rule` never drops the last item).

**The reason set** is closed: `absent | stale | out | pantry_unavailable | pantry_off`
(`pantry.KEPT_REASONS`). Dinner and instant orders write no pantry rows and skip nothing.

**Recipients and texts.** `nothing_needed` goes to the same recipient as `order_summary` (the
run's `user_id`, if set). The run is `completed`, not `cancelled`, because nothing failed and
no one declined: the household has what it needs. The skip notice reaches the household only
in the summary, which is sent after delivery; an order that ends `no_offers` or is cancelled
at approval carries no notice.

**Business agents.** `quote_request` bodies and every message to a business agent are
unchanged: no pantry field and no skip list (PG7). The order summary names no member.

### 6. Texts

`services/api/whatsapp/sender.py` `TEXTS` gains `nothing_needed`: "Nothing to order this
week. Already at home: {items}." The skip line rides inside the existing `order_summary`
text (`"{text}"`): `_summary` (`graph.py:438`) appends "Skipped, already at home: {names}."
when `pantry_skipped` is not empty. Both are free-form texts, so they arrive only inside
WhatsApp's customer-service window, like every other text.

### 7. The demo

On `us-1-eval`, with its simulated phones and mock vendors:

1.  `POST /panel/reset {env: "eval", household: "us-1-eval", setup: {pantry: {milk: [{kind:
    add, age_hours: 1}], eggs: [{kind: add, age_hours: 1}], rice: [{kind: add, age_hours:
    400}]}}}`.
2.  `POST /runs {scenario: "weekly_grocery_order", household_id: "us-1-eval"}`.
3.  The stub list is milk, eggs, bread, bananas and rice. The order is for bread, bananas and
    rice. The live view shows the five decision rows, and the simulated phone shows "Skipped,
    already at home: milk, eggs."
4.  Optional: reset with all five items `add` 1 hour old and run again for `nothing_needed`.

The demo needs `MODEL_MODE=stub` for a fixed list. With the real model, the list varies, and
prompt 5's vocabulary names make matches likely but not certain.

### 8. Evals

Built after evals.md steps 3 (setup registry, #224), 5a (runner format, #228) and 5b
(invariant checks, #229).

*   **Setup key** `pantry` in `SETUP_KEYS` (`services/mocks/control.py`), with the evals.md §4
    `SetupKey(type, apply, undo)` shape:
    *   `type`: a mapping from vocabulary name to a non-empty list of `{kind: str, age_hours:
        number}`;
    *   `apply`: for the case's household only, appends one `pantry_events` row per entry in
        list order, so a later entry is the latest. Within a list, `age_hours` must not
        increase, so the latest event is also the newest;
    *   `undo`: a no-op, because the reset already deletes eval `pantry_events`
        (`control.py:70-74`);
    *   `apply` raises `ControlRefused`, naming the item, for a name not in the vocabulary, a
        `kind` not in `KINDS`, an `age_hours` that is not a number ≥ 0 or that increases, an
        entry with keys other than `kind` and `age_hours`, or an empty list.
*   **Checks** (`evals/checks.py`):
    *   `pantry_decisions_traced` (invariant). A pantry row is a `build_list` decision row
        whose `choice` is `pantry_skipped`, `pantry_kept` or `pantry_unmatched` (the model's
        own `build_list` row is not one). The check passes when: (a) the pantry rows have
        distinct `facts.list_name`; (b) every `items[].item` of every `quote_request` has
        exactly one pantry row; (c) no `pantry_skipped` name appears in any `quote_request`;
        (d) every kept or unmatched name appears in a `quote_request` or has a
        `dislike_dropped` row with explanation `item=<name>`. With no `quote_request`, (b)–(d)
        hold trivially.
    *   `no_quote_requests` (invariant): the run sent no `quote_request` message.
    *   `run_status(status, reason)` (architecture item 66, built by #62) checks the ending,
        and `reply_template(name)` (evals.md §12, built by #235) checks the `nothing_needed`
        text.
*   **Cases:**

    | # | Case id | Category | Tags | Modes | Run | Setup | Checks | Covers |
    |---|---|---|---|---|---|---|---|---|
    | 1 | `task-weekly-order-skips-pantry-items` | task | fast | stub | `weekly_grocery_order` | `pantry` of PT-CUJ 1 | `pantry_decisions_traced`, `run_status(status=completed)` | PG1, PG2, PG4 |
    | 2 | `task-weekly-order-nothing-needed` | task | fast | stub | `weekly_grocery_order` | `pantry`: milk, eggs, bread, bananas, rice each `[{kind: add, age_hours: 1}]` | `no_quote_requests`, `run_status(status=completed, reason=nothing_needed)`, `reply_template(name=nothing_needed)` | PG5 |

    Both are stub-only: the skip is code, and a real-mode run would test the model's list,
    not the pantry.

### 9. Failure handling

| Where | Failure | Behaviour |
|---|---|---|
| `build_list` | `get_pantry` fails | Nothing is skipped; reason `pantry_unavailable`. |
| `build_list` | The vocabulary doesn't load | Nothing is skipped; reason `pantry_unavailable`. |
| `build_list` | `enabled` missing (older `mcp`) | Nothing is skipped; reason `pantry_off`. |
| Rollback | Old `get_pantry` reads new rows | Setup-key rows have `delta 0`; nothing new is returned. |
| Rollback | Old `build_list` reads new `get_pantry` | It reads `items` only, as today, and skips nothing (names never equal SKUs). |

### 10. What this design depends on

*   evals.md steps 3, 5a and 5b (#224, #228, #229) for the setup key and checks.
*   The evals migration `0013` (#226), so the migrations apply in number order.
*   agents.md's `build_list` with the item-name check and the dislike rule (#203, on `main`),
    and evals E-A12's `facts` key on decision rows (on `main`, `graph.py:584-590`).

### 11. What this design changes in the project brief

The brief is not edited; this table records where pantry.md decides differently (PT-A13).

| Brief | Brief says | pantry.md |
|---|---|---|
| §2.8 Pantry recalibration | Before a big shop, a member photographs the fridge; the agent confirms what it saw with quick confirmations | No photos and no confirmations yet (PT-OD6, PT-OD8). |
| §2.9 item record | Quantity, confidence, location, consumption rate, expiry, restock rule | Presence, a trust window and a computed confidence only (PT-OD1, PT-OD3). |
| §2.9 signals | Receipts, cooked meals, consumption patterns, photos, barcode, voice | Deliveries only, plus the eval setup key (PT-OD8). |
| §2.9 photos | Rotate the photo task between members | No photo task (PT-OD8). |
| §2.9 MVP | One fridge photo before each weekly order; cooked meals deduct | No photo, no deductions, and the skip only on eval households (PT-OD4, PT-OD8). |
| §4 stack, SAM 3.1 row | Photos to SAM 3.1 (image segmentation) plus a labelling model | Not used by this design. |

--------------------------------------------------------------------------------

## Alternatives Considered

### Alternative PT-ALT1: Key the pantry on SKUs only, with no vocabulary

*   **Pros**: No vocabulary file; deliveries already write SKUs.
*   **Cons**: List items have no SKU until quotes arrive, so the list can't be compared with
    the pantry. Two stores' SKUs for the same milk never match each other.
*   **Reason for Rejection**: It can't drive the skip.

### Alternative PT-ALT2: Overwrite `item` with the vocabulary name

*   **Pros**: No new column.
*   **Cons**: Two lines that map to one name (whole milk and a 2% substitute) collide on the
    `(order_id, item)` unique index, and the second is silently dropped. It breaks
    `pantry_updated_once`, `pantry_matches_final_lines` and architecture §13, and older code
    would read vocabulary names after a rollback and skip with no trust check.
*   **Reason for Rejection**: Data loss and an unsafe rollback. `pantry_item` is a separate
    nullable column (§3).

### Alternative PT-ALT3: Quantities with an exponential decay and a threshold

*   **Pros**: Closer to the brief's "4 of 12, 60% confidence" record.
*   **Cons**: Units differ between catalog lines, and no consumption rate is measured. With one
    fixed threshold, any decay curve is the same rule as a window, only harder to explain.
*   **Reason for Rejection**: PT-OD1 and PT-OD3.

### Alternative PT-ALT4: Let the model match names or skip items

*   **Pros**: Handles "whole milk" and spelling variants without an alias list.
*   **Cons**: Its matches and omissions can't be checked or explained; code must decide what is
    safe to leave out (Problem 3).
*   **Reason for Rejection**: PT-OD2, PT-OD4. The model only sees vocabulary names as a hint.

### Alternative PT-ALT5: Open Food Facts categories or the knowledge graph as the vocabulary

*   **Pros**: A large, ready-made list of foods.
*   **Cons**: Categories are too coarse (bananas and apples are both "fruit"; almond milk is
    "milk"). A graph lookup would make every weekly order depend on Neo4j.
*   **Reason for Rejection**: A small owned YAML file that grows with the catalog.

### Alternative PT-ALT6: A stored pantry projection table or a Redis cache

*   **Pros**: Faster reads at scale.
*   **Cons**: A second copy that drifts from the events; the data is about 20–30 rows per
    household per week.
*   **Reason for Rejection**: The estimate is computed from events on read (§4).

### Alternative PT-ALT7: Backfill `pantry_item` on existing delivery rows

*   **Pros**: Live households would have usable history on day one.
*   **Cons**: A data migration (CLAUDE.md allows SQL-only, additive migrations), and with the
    skip off for live households, nothing would read the result.
*   **Reason for Rejection**: New deliveries fill the column; no backfill.

### Alternative PT-ALT8: Build the pantry check in this design

*   **Pros**: Matches the brainstorm (PD1, PD4) and the brief's "quick confirmations"; gives a
    way to write `verify` and `out`.
*   **Cons**: About half of the build (a table, two guarded tools, two limits, a `pc:` path in
    `ingest`, an `api` module, run-bookkeeping exemptions, config and a safety case). The
    review found 4 blockers in it against the guard and `runs.py`. Nothing would start it for
    live households, and under the gate (PT-OD4) it would only serve evals and the demo.
*   **Reason for Rejection**: PT-OD6. Recorded under Future Improvements with the review's
    fixes.

### Alternative PT-ALT9: A "Ran out" button on the summary now, with the skip on for everyone

*   **Pros**: Live households get the skip and a correction in one design; it reuses the
    `tt:` take-turns pattern.
*   **Cons**: One more guarded tool, limit (critical), `ingest` branch and eval case, and a
    wrong skip still costs one order.
*   **Reason for Rejection**: The owner chose the smaller scope (review 2026-11-02). It is the
    condition for lifting the gate (PT-OD7).

### Alternative PT-ALT10: Demo on the live family household with a refresh flag

*   **Pros**: Real WhatsApp phones on stage.
*   **Cons**: It writes made-up stock into a real family's pantry, and their next real orders
    would skip those items. It needs a live-household exception and an extra config variable.
*   **Reason for Rejection**: PT-OD11. The demo uses `us-1-eval`.

### Alternative PT-ALT11: Typed notes in this design

*   **Pros**: "We're out of sugar" is the cheapest correction for a member.
*   **Cons**: The consumer agent would need a pantry intent and a `pantry:write` grant, with
    amendments to three designs, and children's notes need their own rule.
*   **Reason for Rejection**: PT-OD8.

### Alternative PT-ALT12: Seed pantry events in the base eval seed

*   **Pros**: Every eval case would exercise the pantry.
*   **Cons**: It changes existing cases (the shortfall fixture's milk would be skipped).
*   **Reason for Rejection**: PT-OD10. An opt-in `pantry` setup key.

--------------------------------------------------------------------------------

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| A wrong skip leaves an eval household without an item. | Only trusted, matched items are skipped; every skip has a decision row; eval households have no real customers. Live households never skip (PG6). |
| The real model uses names outside the vocabulary ("organic whole milk"), so a skip doesn't fire. | It fails toward ordering. Prompt 5 lists vocabulary names; `pantry_unmatched` rows show the gaps; aliases are added in seed YAML. |
| The gate is lifted without a correction path. | PT-OD7: lifting it needs a later design; `enabled` is computed in one place (`get_pantry`), tested by PG6. |
| A mixed-version deploy misreads rows. | Deploy `mcp` before `agents`; new rows that aren't deliveries have `delta 0`; `build_list` treats a missing `enabled` as off; PG8's test runs the old query over new rows. |
| Pantry facts in traces are read by more people. | Decision rows sit in `agent_traces` with the rest of the run, readable by the same operators; they hold item names and ages, nothing about a member. |
| A business agent infers stock from week-to-week list changes. | Unchanged from today: the list shape was already visible to sellers. No pantry field is sent (PG7). |
| Live households gain no benefit from this design. | Accepted (PT-OD4). Their history builds up from deliveries, ready for PT-OD7. |

## Non-Functional Requirements

*   **Safety.** Allergies, diets, budgets and approval rules never come from the pantry; the
    pantry only removes items before the quote request, and allergens stay enforced in the
    quote constraints and in `authorize_spend`. The pantry never removes an item on a live
    household.
*   **Privacy.** No pantry data reaches a business agent. Pantry decision rows name no member.
*   **Cost.** No model calls. Prompt 5 gains 11 names. Storage grows by about 20–30 rows per
    household per week. Each weekly order writes one decision row per list item (5 for the
    stub list).
*   **Rollout and rollback.** Additive migration; deploy `mcp` before `agents`; the old
    `get_pantry` query returns no row that isn't a delivery.

## Additional Resources

*   [PROJECT_BRIEF.md](../../PROJECT_BRIEF.md) §2.8, §2.9.
*   [architecture.md](architecture.md): §5, §8, §9, §13; items 29, 43, 55.
*   [agents.md](agents.md): §6, §10 (the `tt:` button, the model for PT-OD7), §11, §15, §17.
*   [agent-protocol.md](agent-protocol.md): G7, `pantry_matches_final_lines`.
*   [evals.md](evals.md): §4 case format and setup registry, §5 checks, E-A12.
*   Brainstorm brief and review reports: `.loop-logs/reviews/concepts_brief_pantry.md` and
    `*_pantry.md` (outside the repository).

## Future Improvements

*   **A correction path, then the skip for live households** (PT-OD7). A "Ran out" button on
    the order summary and the `nothing_needed` message, built like `tt:` (agents.md §10, issue
    #244's commit-then-call helper). Each tap writes `out` events for the skipped items. Then
    `get_pantry` enables live households.
*   **The pantry check** (PT-OD6). A short run, separate from the order, that asks the
    household's approver about up to 3 stale items, one message per item with "Still have it"
    and "Ran out", answered by button taps. The review of 2026-11-02 found what a future design
    must settle:
    *   one exemption in `runs.py` for pantry runs: no `trigger.user_id` needed, no retry
        prompt, skipped by `retry_target`;
    *   guarded tools that do their work in `call()` with `apply=None`, return pinned shapes,
        refuse any caller but `system:api`, and take the household from the run;
    *   traces as `decision` rows (trace kinds are fixed);
    *   a tap that commits its run before the tool call (#244), and `POST /runs` handling
        outside the request transaction;
    *   ids from `ids.py` for check messages and button payloads;
    *   a recipient rule checked on role, a reply to members who weren't asked (as approval
        buttons do), "Ran out" always counted, and the newest check winning instead of a
        status column;
    *   a trigger that real households actually get (an automatic schedule), or the check
        stays an eval feature.
*   **Photos and receipts** through WhatsApp media, owned by a later design.
*   **Typed notes** ("we're out of sugar") through a consumer pantry intent.
*   **Cooked-meal deductions** once a meal-to-ingredients mapping exists.
*   **Quantities and consumption rates,** once consumption is measured.

--------------------------------------------------------------------------------

## Detailed Implementation Plan

### Build steps

Each step is one issue. Every step except P9 (an owner step) is also one PR. **Critical**
means the PR touches a path in `.github/critical-paths.txt` and needs human review. **Demo**
marks a step on the demo's path; every pantry step is on it. An issue's `## Depends on`
lists only issues, never design PRs.

| Step | Scope | Depends on | Critical | Demo |
|---|---|---|---|---|
| P1 | This design, the amendment notes PT-A1–PT-A12, the SITEMAP row (PT-A14) | none | yes | demo |
| P2 | Migration `0014_pantry.sql` | #226 (evals step 4, migration `0013`) | yes | demo |
| P3 | Vocabulary, `services/common/pantry.py`, catalog links, seed check | P2 | no | demo |
| P4 | `record_delivery` writes the new columns; `get_pantry` returns `enabled`, `as_of`, `estimates` | P3 | no | demo |
| P5 | Weekly order: the code-only skip, decision rows, `nothing_needed`, summary line | P4 | no | demo |
| P6 | The `pantry` setup key | P3, #224 (evals step 3, setup registry) | no | demo |
| P7 | Eval checks `pantry_decisions_traced`, `no_quote_requests` | P5, #229 (evals step 5b, invariant checks) | yes | demo |
| P8 | Eval cases (2) and their coverage entries | P6, P7, #62 (`run_status`), #228 (evals step 5a, case format), #231 (evals step 6, `evals/coverage.yaml`), #235 (evals step 10b, `reply_template`) | no | demo |
| P9 | Owner: rehearse the demo script (§7) | P5, P6 | n/a | demo |

P9 is an owner step: its issue gets no `ready` label, so the build loop never picks it.

Deploy order for every step that changes `services/mcp/` and also `services/agents/`:
`mcp` first.

### P1. Design and amendments

| File | Change | Why |
|---|---|---|
| `docs/designs/pantry.md` | This document, Status Approved, with the issue number filled in | Source of truth |
| `docs/designs/architecture.md` | Amendment notes PT-A1–PT-A6 at their locations | §1 |
| `docs/designs/agents.md` | Amendment notes PT-A7–PT-A10 | §1 |
| `docs/designs/evals.md` | Amendment note PT-A11 | §1 |
| `docs/designs/identity-authz.md` | Amendment note PT-A12 | §1 |
| `docs/designs/SITEMAP.md` | Pantry row to Approved; each amended design's row gains ", 2026-11-02, pantry" | PT-A14 |

Notes use the format `*Amended 2026-11-02 (pantry, #251):*`. No code.

### P2. Migration

| File | Change | Why |
|---|---|---|
| `migrations/0014_pantry.sql` (new) | `ALTER TABLE pantry_events ADD COLUMN pantry_item text NULL`, `ADD COLUMN kind text NOT NULL DEFAULT 'add'`, `ADD COLUMN run_id uuid NULL REFERENCES runs`; `CREATE INDEX pantry_events_item_latest ON pantry_events (household_id, pantry_item, event_id DESC)`; `ALTER TABLE catalog_items ADD COLUMN pantry_item text NULL` | §3 |
| `tests/unit/test_migrations.py` | `test_0014_additive` (the file has no `DROP`, `RENAME` or `CHECK`); add `0014` to the apply-order assertion and update the docstring's range | Migration rules |

### P3. Vocabulary and matching

| File | Change | Why |
|---|---|---|
| `services/seed/data/pantry_items.yaml` (new) | The 11 entries of §2, as a top-level `pantry_items: [{name, aliases, window_days}]` list | §2 |
| `services/seed/data/catalog_mock.yaml` | Each SKU gets the `pantry_item` in the table below | §2 |
| `services/seed/__main__.py` | `load_data` loads `pantry_items.yaml`. `_validate` refuses a `pantry_item` not in the vocabulary, a duplicate name or alias, and an alias equal to another item's name. `_seed_business` writes `pantry_item` on `catalog_items` rows only (not `menu_items`) | PT-A6 |
| `services/common/pantry.py` (new) | Listed below | §2, §4, §5 |
| `tests/unit/test_pantry.py` (new) | Listed below | PG1, PG2 |
| `tests/unit/test_seed.py` | `test_catalog_pantry_items_in_vocabulary`, `test_vocabulary_rejects_duplicate_alias` | Seed check |

SKU to `pantry_item` in `catalog_mock.yaml`:

| SKU | `pantry_item` |
|---|---|
| `MOCK-MILK-WHOLE`, `MOCK-MILK-2PCT`, `SQ-MILK-WHOLE` | `milk` |
| `MOCK-MILK-ALMOND` | `almond milk` |
| `MOCK-EGGS-12`, `SQ-EGGS-12` | `eggs` |
| `MOCK-BREAD-WHEAT`, `SQ-BREAD-WHITE` | `bread` |
| `MOCK-BANANAS-2LB`, `SQ-BANANAS-2LB` | `bananas` |
| `MOCK-APPLES-3LB` | `apples` |
| `MOCK-CHICKEN-BREAST` | `chicken breast` |
| `MOCK-RICE-2LB` | `rice` |
| `MOCK-GRANOLA-NUT` | `granola` |
| `SQ-PASTA-1LB` | `pasta` |
| `SQ-ALMONDS-1LB` | `almonds` |

`services/common/pantry.py` holds, with no I/O other than `load_vocabulary` reading the YAML:

1.  `KINDS = ("add", "verify", "out")`, `KEPT_REASONS = ("absent", "stale", "out",
    "pantry_unavailable", "pantry_off")`, `THETA = 0.7`, and `VOCABULARY_PATH` (the seed YAML).
2.  `Vocabulary`: names, alias map, `window_days` per name, and `version` (sha256 of the file
    bytes). `load_vocabulary(path=VOCABULARY_PATH)`.
3.  `normalize(text)` (lower-case, trim, collapse spaces) and `match(vocab, text) -> str | None`.
4.  `Estimate` (`pantry_item`, `latest_event_id`, `kind`, `age_hours`, `window_hours`,
    `present`, `trusted`, `confidence`) and `estimate(latest, window_days, as_of) -> Estimate`.
5.  `decide(vocab, estimates, list_name) -> (choice, reason, facts)`: the §5 step 2 rule.
    `estimates` is `get_pantry`'s JSON list keyed by `pantry_item`; `decide` uses its
    `present` and `trusted` as given and recomputes nothing. Under `pantry_off` or
    `pantry_unavailable`, `facts.pantry_item` is the match if the vocabulary loaded, else
    `null`, and the estimate fields are `null`.

`tests/unit/test_pantry.py` covers: names, aliases, case and spaces; `almond milk` never
matches `milk`; an unknown name gives `None`; an estimate at age 0, just inside the window, at
the window edge (confidence exactly θ, not trusted) and past it; `out` and `verify`; the
latest event wins; `decide` for each choice and reason.

### P4. Household MCP

| File | Change | Why |
|---|---|---|
| `services/mcp/household/server.py` | `record_delivery` and `get_pantry` as listed below; the module docstring names the new fields | §3, §4, PT-A2 |
| `tests/unit/test_household_tools.py` | Listed below | PG6, PG8 |

1.  **`record_delivery`** (`server.py:597`): the insert becomes `INSERT INTO pantry_events
    (household_id, item, pantry_item, kind, delta, unit, source, order_id, run_id) SELECT %s,
    oi.sku, ci.pantry_item, 'add', sum(oi.qty), 'each', 'delivery', oi.order_id, %s FROM
    order_items oi JOIN orders o ON o.order_id = oi.order_id LEFT JOIN catalog_items ci ON
    ci.agent_id = o.seller_agent_id AND ci.sku = oi.sku WHERE oi.order_id = %s GROUP BY
    oi.order_id, oi.sku, ci.pantry_item ON CONFLICT (order_id, item) WHERE order_id IS NOT
    NULL DO NOTHING`, bound as `(household, ctx.run_id, order_id)`.
2.  **`get_pantry`** (`server.py:1049`): keeps its `items` query unchanged. It adds:
    *   `as_of` from `SELECT now()`, as an ISO-8601 UTC string (the result goes through
        `json.dumps` with no `default`); `age_hours` is a float rounded to 2 places;
    *   `enabled = ctx.env == "eval"` (`CallContext.env`, `services/mcp/meta.py:95`, already
        read from `households.env`); this is the one place the gate is computed;
    *   when `enabled`, `estimates` from `SELECT DISTINCT ON (pantry_item) pantry_item,
        event_id, kind, ts FROM pantry_events WHERE household_id = %s AND pantry_item IS NOT
        NULL ORDER BY pantry_item, event_id DESC`, then `pantry.estimate` per row with the
        vocabulary's window; otherwise `[]`. An item missing from the vocabulary is left out.
        If the vocabulary fails to load, `get_pantry` raises, and the agent records
        `pantry_unavailable`.

Tests:
*   `test_record_delivery_writes_pantry_item` (`db`): the vocabulary name, `kind 'add'` and
    `run_id`.
*   `test_record_delivery_unmapped_sku_null_pantry_item` (`db`).
*   `test_get_pantry_items_unchanged_by_new_rows` (`db`): insert setup-key-style rows (`delta
    0`, kinds `verify` and `out`), and the `items` result is unchanged (PG8).
*   `test_get_pantry_estimates_latest_event_wins` (`db`).
*   `test_get_pantry_ignores_legacy_rows` (`db`): rows with `pantry_item IS NULL`.
*   `test_get_pantry_off_for_live_household` (`db`): `enabled` false and `estimates` empty for
    an `env = 'live'` household (PG6).
*   The result shape: `items`, `enabled`, `as_of`, `estimates` and their keys.

### P5. The weekly order

| File | Change | Why |
|---|---|---|
| `services/agents/household/prompts.py` | `BUILD_LIST_PROMPT` drops "Skip what the pantry has." and gains "Use these names for the items you choose, where they fit: {pantry_names}." | PT-A7 |
| `services/agents/household/graph.py` | Listed below | §5, PT-A3, PT-A7–PT-A10 |
| `services/api/whatsapp/sender.py` | `TEXTS["nothing_needed"]` | §6 |
| `tests/unit/test_household_graph.py` | Listed below | PG1–PG7 |
| `tests/unit/test_household_checkpoint_compat.py` | A checkpoint saved without `pantry_skipped` or `pantry_as_of` resumes, and both default | Checkpoint compatibility |

Changes to `graph.py`:

1.  `build_graph` and `HouseholdGraph.__init__` gain `load_vocabulary: Callable[[],
    Vocabulary] = pantry.load_vocabulary`, injected like `clock` and `label_seller`.
    `build_list` (`graph.py:739`) calls it once per run inside `try/except Exception`, and
    wraps the existing `get_pantry` call the same way; either failure means
    `pantry_unavailable` for every item. `BUILD_LIST_PROMPT.format` gains `pantry_names` (the
    vocabulary names, sorted and comma-joined, or `NO_CODES` when the load failed), for every
    order kind; the model input loses `pantry`. Replace the name filter at `:772-775` with §5
    steps 1–5 for weekly orders. A **weekly order** in code is `state["kind"] == "grocery"
    and (state.get("scope") or HOUSEHOLD_SCOPE) == HOUSEHOLD_SCOPE`; other orders skip
    nothing and write no pantry rows.
2.  Each pantry decision is written with the existing `trace_decision(state, "build_list",
    choice, explanation, facts)` (`graph.py:584`); the explanation is `item=<list name>`.
3.  The existing `InvalidOutput("build_list returned no items")` stays for a list that is
    empty after `item_name_ok`. If the pantry skip emptied the list, return `{"items": [],
    "pantry_skipped": …, "pantry_as_of": …, "decision": "nothing_needed"}` and skip the
    dislike rule. (`decision` is the key every router reads; `HouseholdGraph.__call__` resets
    it on each message, `graph.py:1572`.)
4.  New `after_build_list(state)` returns `"nothing_needed"` when `state.get("decision") ==
    "nothing_needed"`, else `"request_quotes"`. Replace `g.add_edge("build_list",
    "request_quotes")` (`graph.py:1438`) with `g.add_conditional_edges("build_list",
    after_build_list, ["request_quotes", "nothing_needed"])`, and add
    `g.add_edge("nothing_needed", "send")`.
5.  New node `nothing_needed`, modelled on `no_offers` (`graph.py:927-938`): `out =
    list(state.get("outgoing") or [])`; `tool(state, "set_order_state", {"order_id": …,
    "expected": "draft", "new": "cancelled"})`; when `state.get("user_id")` is set, `_add(state,
    out, WHATSAPP, "whatsapp_out", {"user_id": state["user_id"], "template": "nothing_needed",
    "params": {"items": ", ".join(pantry_skipped)}, "buttons": []})`; return `{"phase": DONE,
    "outgoing": out, "finish": {"status": "completed", "reason": "nothing_needed"}}`. If
    `set_order_state` is refused (`ToolRefused`), the run ends `error` with the refusal
    reason, as `authorize` does (`graph.py:1060`). `phase: DONE` makes a replayed message a
    no-op.
6.  `HouseholdState` gains `pantry_skipped: list[str]` and `pantry_as_of: str`, read with
    defaults; `STATE_VERSION` stays 1.
7.  `_summary` (`graph.py:438`) appends "Skipped, already at home: {names}." when
    `pantry_skipped` is not empty.
8.  The module docstring's `build_list` line describes the new order (item names, pantry skip,
    dislike rule).

Tests (`test_household_graph.py`), with a fake `get_pantry` returning `enabled` and
`estimates`:
*   `test_build_list_skips_trusted_items` (PG1);
*   `test_build_list_keeps_stale_out_absent` (one row per reason);
*   `test_build_list_unmatched_kept`;
*   `test_build_list_pantry_unavailable_keeps_all` (the tool raises; and the vocabulary fails to
    load);
*   `test_build_list_pantry_off_keeps_all` (`enabled` false, and `enabled` missing) (PG6);
*   `test_build_list_decision_facts` (every facts key) (PG4);
*   `test_build_list_all_skipped_nothing_needed` (order cancelled, no `quote_request`, the
    text, the run's reason) (PG5);
*   `test_build_list_pantry_then_dislikes` (a skipped item is never also a dislike row; the
    dislike rule sees only kept items);
*   `test_build_list_prompt_has_no_pantry` (PG3);
*   `test_quote_request_has_no_pantry_fields` (PG7);
*   `test_summary_names_skipped_items`;
*   `test_dinner_order_never_skips`.

Existing tests that change, each keeping its intent; none is removed:
*   `test_quotes_requested_once_every_member_answered` (`test_household_graph.py:354-366`)
    asserts the old rice skip. Its fake `get_pantry` gains `enabled: True` and a trusted `rice`
    estimate.
*   Tests that count `build_list` decision rows see one extra pantry row per item on a
    household grocery order (5 for the stub list); their expected counts are updated.

### P6. The `pantry` setup key

| File | Change | Why |
|---|---|---|
| `services/mocks/control.py` | `SETUP_KEYS["pantry"]` as defined in §8 (`type`, `apply`, no-op `undo`, the `ControlRefused` cases); each row has `item = pantry_item = name`, `delta 0`, `unit 'each'`, `source 'eval_setup'`, `ts = now() - age_hours` | §8, PT-A11 |
| `tests/unit/test_panel_reset.py` | `test_pantry_setup_key` (`db`: rows written, in order, only for the case's household); `test_pantry_setup_refusals` (one per `ControlRefused` case); `test_pantry_setup_undone_by_reset` | PG9 |

### P7. Eval checks

| File | Change | Why |
|---|---|---|
| `evals/checks.py` | `pantry_decisions_traced` and `no_quote_requests` as defined in §8, both `invariant` | §8 |
| `tests/unit/test_eval_checks.py` | Pass and fail fixtures for each | Check tests |

### P8. Eval cases

| File | Change | Why |
|---|---|---|
| `evals/cases/task/task-weekly-order-skips-pantry-items.yaml` (new) | Case 1 of §8 | PG1, PG2, PG4 |
| `evals/cases/task/task-weekly-order-nothing-needed.yaml` (new) | Case 2 of §8 | PG5 |
| `evals/coverage.yaml` | Prefix entries `task-weekly-order-skips-pantry` and `task-weekly-order-nothing-needed`, both mapping to `pantry` (longest prefix wins, so they don't fall under architecture's `task-weekly-order-`); a goal section `pantry: {PG1: [task-weekly-order-skips-pantry-items], PG2: [task-weekly-order-skips-pantry-items], PG4: [task-weekly-order-skips-pantry-items], PG5: [task-weekly-order-nothing-needed]}` | Coverage report |

Case 1 in full, in the evals.md §4 format:

```yaml
id: task-weekly-order-skips-pantry-items
category: task
tags: [fast]
model_modes: [stub]
household: us-1-eval
run: weekly_grocery_order
setup:
  pantry:
    milk: [{kind: add, age_hours: 1}]
    eggs: [{kind: add, age_hours: 5}, {kind: out, age_hours: 1}]
    rice: [{kind: add, age_hours: 400}]
expect:
  - {check: pantry_decisions_traced}
  - {check: run_status, args: {status: completed}}
```

`evals/thresholds.yaml` is not edited.

### P9. Owner rehearsal

No files. The owner runs §7 against the VM in mock mode with `MODEL_MODE=stub`, checks the
live view and the simulated phone, and records anything the script gets wrong as an issue.

### Verification

Every code step: `make lint test-unit` with `DATABASE_URL` set, so the `db` tests run rather
than skip. P5 (touches `services/agents/`), P7 and P8: also `make eval-fast`. P2: also apply
the migrations to an empty database and to one with today's rows.
