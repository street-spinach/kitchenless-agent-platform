# Kitchenless showcase: the Spinach theme and visible agent work

*   **Author:** madushankaf
*   **Status:** Draft
*   **Date:** 2026-11-04
*   **Path:** `docs/designs/showcase.md`
*   **Issue:** none yet (product owner request, 2026-11-04, demo week)
*   **Builds on:** [experience.md](experience.md), [agent-protocol.md](agent-protocol.md),
    [agents.md](agents.md), [identity-authz.md](identity-authz.md) and
    [architecture.md](architecture.md), all approved. §1 lists every change this design
    makes to them; nothing here relies on a change that table doesn't list.
*   **Input:** the product owner's answers on 2026-11-04 ("show the depth": all four views;
    "theme": the `/me` pages, Keycloak sign-in and consent, the owner app; "use the theme
    colour at streetspinach.com"), the Spinach WordPress theme stylesheet
    (`streetspinach.com/wp-content/themes/spinach/style.css`, version 1.6.0), and the code on
    `main` at `dfda2e1`.

**Conventions.** Ids: goals SG1…, journeys SC-CUJ 1…, changes to other designs SC-A1…,
alternatives SC-ALT1…, open questions SC-Q1…, build steps SC1…, implementation items SC-I1….
Roles, surfaces (S1–S6), "text key", "window", "decision asked" and "Spark" mean what
experience.md "Conventions" says. **Story** means the facts of one order's agent work, shown
to people. **Brand tokens** means the colour, radius, shadow and type values in §2.

--------------------------------------------------------------------------------

## Overview

Two changes for the demo. First, every human-facing screen takes the Spinach brand: the
`/me` pages, the Keycloak sign-in and consent screens, and the owner app share one set of
brand tokens copied from streetspinach.com. Second, the work the agents already do (asking
several sellers, comparing quotes, one counter-offer, allergen, diet and budget checks in
code, approvals, booking a courier) becomes visible in four places: a **story page** per
order on `/me`, a **progress line and a story line** in the household's WhatsApp messages,
a **negotiation sheet** in the owner app, and a full-screen **presenter view** on the panel.

Every view reads rows the platform already writes (`agent_messages`, `agent_traces`,
`quotes`, `orders`, `approvals`). Nothing new is traced, no model is called to describe
anything (experience EX-ALT22 still holds), and every story sentence is built in code from
those rows.

## The Problem

The demo shows a WhatsApp chat that ends "Your groceries are ordered", a plain `/me` page
and the stock Keycloak login. The audience can't see that three sellers were asked, that
one was countered, that every line was checked against Kavi's peanut allergy in code, or
that the total stayed under the budget without the model deciding it. That work exists
only in traces the panel shows as raw rows. The screens also don't look like Spinach:
`/me` uses a generic grey-green stylesheet, Keycloak its stock PatternFly theme and the
owner app its own palette.

## Goals

*   **SG1.** One brand: the `/me` pages, every server-rendered status page, the Keycloak
    login, magic-link and consent screens, and the owner app use the §2 brand tokens and the
    Spinach logo. A unit test fails when the three copies of the tokens differ.
*   **SG2.** An adult can open the story of any of their household's orders and see, in
    under 30 seconds of reading, who was asked, what each seller offered, the counter-offer,
    which checks ran in code and what they found, the approval, and the courier.
*   **SG3.** The household's WhatsApp chat says what the agents are doing: one progress line
    when quotes go out, one story line in the order summary, and a **See how** button that
    opens the story page.
*   **SG4.** A business owner sees each negotiation with their own business turn by turn in
    the owner app, refreshed live, without any household identity.
*   **SG5.** The operator can put a full-screen live view on a projector that shows agents
    exchanging messages, model choices and code checks as they happen, with running totals.
*   **SG6.** No new model calls, no new trace rows, no new migrations, no new personal data
    on any surface, and no extra decisions asked.

## Non-Goals

*   Changing what agents do, negotiate or decide. This design only shows existing work.
*   Native app builds; the owner app stays a web export (experience §11).
*   A Keycloak account console theme or admin console theme; only the login theme.
*   Showing one household's story to another household or to a business, or showing a
    member's name next to an allergy, diet or dislike anywhere new.
*   Streaming (WebSocket or SSE); every live view polls, as `/me` and the owner app already do.

## Open Questions

*   **SC-Q1. The Satoshi font.** The Spinach site uses Satoshi (Indian Type Foundry, Free
    Font License). This design ships the system font stack and leaves a `--brand-font`
    token. If the product owner confirms the licence covers self-hosting in this product
    and adds the `woff2` files, SC-I3 adds the `@font-face` rules. Decided default: system
    fonts.

## Critical User Journeys (CUJs)

### SC-CUJ 1: Maya watches her weekly order happen

*   **Given**: Maya's household is set up (experience EX-CUJ 1), inside the window.
*   **When**: She writes "do the weekly shop".
*   **Then**: Within seconds she gets `order_progress`: "On it. Asking 3 stores for 18 items.
    Every line is checked for peanuts and sesame, vegetarian, and your $200.00 weekly
    budget." When the order is ready she gets `approval_request` as today, plus a **See how**
    button. After she approves, `order_summary` ends with the story line: "I asked 3 stores.
    Fresh Market won at $182.40 after one counter-offer (was $191.90). 18 of 18 lines passed
    the allergy and diet checks. Spent $182.40 of $200.00 this week."

### SC-CUJ 2: Maya opens the story

*   **When**: She taps **See how**.
*   **Then**: She gets `me_link` for `/me/orders/{ref}`. The page shows, top to bottom: a
    summary card (seller, total, budget bar); **Who we asked** (one row per seller: offer,
    lines it couldn't supply, outcome); **Negotiation** (the counter and the reply, with the
    seller's checked rationale); **Checks in code** (allergens, diet, budget, approval, each
    with what it checked and the result); **Delivery** (courier quotes and the choice);
    **Behind the scenes** (agents involved, messages exchanged, model decisions and whether
    each used the model or the rule default, elapsed time). No member names appear next to
    any code.

*Amended 2026-11-05 (product owner, #432):* the **Behind the scenes** card shows only
agents involved, messages exchanged and elapsed time, in plain words. The token count and
the model or rule-default split are not shown on `/me` pages; the live screen keeps them for
the demo audience. `/me/history` shows each audit row as a plain sentence and `/me/agents`
names agents ("Your household agent", "Your personal agent"), never by id.

### SC-CUJ 3: The restaurant owner watches a dinner negotiation

*   **Given**: The owner of Mock Restaurant is signed in to the owner app and has set
    "Most discount you'll give" to 20% (experience EX-CUJ 9).
*   **When**: A household asks for dinner and the household agent counters her offer.
*   **Then**: Orders shows the row with a live "Negotiating" badge. She taps it; a sheet
    lists the turns: "Request: 2 dinners, nut-free, vegetarian", "You offered $48.00",
    "They countered at $40.80", "You accepted at $40.80 (your floor was $38.40)", "Courier
    booked". It refreshes every 5 seconds while open.

### SC-CUJ 4: The presenter view

*   **When**: The operator opens `/panel/screen` on the projector and Maya starts an order.
*   **Then**: The page shows one column per agent taking part (Maya's household agent, three
    stores, a courier), with message cards sliding in as they're sent ("Quote request: 18
    items", "Offer: $191.90", "Counter: $182.40"), a check card for each code check ("Allergy
    check: 18 lines, passed"), a model card for each model decision ("Spark chose Fresh
    Market" or "Rule default"), and a footer with running totals: agents, messages, model
    calls, tokens, elapsed time.

--------------------------------------------------------------------------------

## Technical Plan (Architecture)

### 1. Changes to the approved designs

| Id | Design | Change |
|---|---|---|
| SC-A1 | experience §6 | New text key `order_progress` (in-window only; never held, never a Meta template; dropped when outside the window). `order_summary`'s `{text}` gains the story line (§5). `approval_request` and `order_summary` gain a **See how** reply button with payload `me:story` when sent as text (not as a Meta template). `me:` is not a decision prefix, so decisions asked don't change. |
| SC-A2 | experience §8 | New page `/me/orders/{ref}` (§4) and the `/me/household` order card links to it. The `/me` stylesheet is replaced by the branded one (§3). `_page()` status pages render through `base.html` without tabs. |
| SC-A3 | experience §11 | Owner app theme values become the brand tokens. Orders rows open a negotiation sheet backed by `GET /app/orders/{ref}/turns` (§6). |
| SC-A4 | experience §12, agent-protocol "live view" | New panel page `/panel/screen` and JSON `GET /panel/screen/feed` (§7). `/panel/runs/live` is unchanged. |
| SC-A7 | identity-authz (links, EX-A15's closed set) | `links.NEXT_PAGES` gains `"story"`: after sign-in it opens `/me/orders/latest`, which redirects to the household's newest order's story. The set stays closed and literal. |
| SC-A5 | identity-authz §4 (realm), I15 (image) | The realm's `loginTheme` is `kitchenless`; the Keycloak image copies `services/identity/realm/theme/kitchenless/`. The household and consumer class scopes get `consent.screen.text` (§3.3). |
| SC-A6 | agents §11 | The household graph sends `order_progress` once per order, when the first `quote_request` goes out, to the person who started the run, only if they are an adult. |

### 2. Brand tokens

From the Spinach theme stylesheet (version 1.6.0). One file is the source,
`services/api/static/brand.css`, as CSS custom properties; the owner app's `theme.ts` and
the Keycloak theme's CSS hold copies, and `tests/unit/test_brand_tokens.py` parses all three
and fails on any difference.

| Token | Value | Use |
|---|---|---|
| `--brand-green-900` | `#123710` | Dark surfaces (presenter view background, header on Keycloak) |
| `--brand-green-700` | `#1e6b1c` | Primary text accent, links, gradient start |
| `--brand-green-600` | `#268923` | Hover, focus ring |
| `--brand-green-500` | `#2a9727` | Primary, gradient end, "passed" |
| `--brand-green-50` | `#eaf5e9` | Tinted card, selected tab background |
| `--brand-lime` | `#8ecc06` | Highlight: live badge, budget bar fill, hero glow |
| `--brand-orange` | `#db5f00` | Attention: "needs your approval", "couldn't supply" |
| `--brand-ink` | `#262b25` | Body text |
| `--brand-ink-soft` | `#323831` | Headings on tinted surfaces |
| `--brand-muted` | `#5a6559` | Secondary text |
| `--brand-muted-2` | `#7b847a` | Captions, timestamps |
| `--brand-line` | `#cccccc` at 40% (`rgba(38, 43, 37, 0.08)` on cards) | Borders |
| `--brand-bg` | `#f8fbf7` | Page background |
| `--brand-bg-2` | `#efefee` | Secondary background |
| `--brand-white` | `#ffffff` | Cards |
| `--brand-danger` | `#e03030` | Errors, "refused" |
| `--brand-radius-card` | `24px` | Cards |
| `--brand-radius-control` | `12px` | Buttons, inputs, pills use `999px` |
| `--brand-shadow` | `0 10px 30px rgba(38, 43, 37, 0.06)` | Cards |
| `--brand-shadow-lift` | `0 22px 70px rgba(18, 55, 16, 0.14)` | Hero card, sheets |
| `--brand-gradient` | `linear-gradient(91deg, #1e6b1c, #2a9727)` | Primary buttons |
| `--brand-font` | `system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif` | All text (SC-Q1) |

**Logo.** The Spinach icon (`spinach_icon.png`, 305×302, from the Spinach site, owned by the
product owner) is committed once as `services/api/static/brand/spinach-icon.png` and copied
into the Keycloak theme and the owner app's `assets/`. Shown at 32 px in headers, with the
word "Kitchenless" beside it in `--brand-green-900`, 700 weight.

### 3. Theming the three surfaces

#### 3.1 `/me` pages

`me.css` is rewritten on top of `brand.css` (imported first; both served from
`/me/static/`). No JavaScript; the CSP is unchanged.

*   **Header.** White bar, logo and "Kitchenless", the person's name and household as a pill
    on the right. Tabs below as a segmented control: selected tab white on
    `--brand-green-50` track, `--brand-green-700` text.
*   **Page background** `--brand-bg` with the site's soft hero glow
    (`radial-gradient(circle at 80% 0%, rgba(142, 204, 6, 0.14), transparent 40%)`) behind
    the first card only.
*   **Cards** white, `--brand-radius-card`, `--brand-shadow`, 20 px padding. A **hero card**
    (the week's order on `/me/household`, the summary on the story page) uses
    `--brand-green-900` background, white text, a lime budget bar.
*   **Buttons.** Primary: `--brand-gradient`, white, 700, 48 px tall,
    `--brand-radius-control`. Secondary: white with a 1 px `rgba(30, 107, 28, 0.16)` inset
    ring, `--brand-green-700` text. Destructive ("Ran out"): white, `--brand-orange` text.
*   **Status pills** (999 px radius, 13 px, 700): green-50/green-700 "At home", "Passed",
    "Won"; `#fdf1e6`/orange "Not sure", "Needs approval", "Couldn't supply"; `#fdeaea`/danger
    "Out", "Refused"; bg-2/muted "Expired", "No reply".
*   **Empty and error states** get the logo at 48 px, a one-line heading and the existing
    copy.
*   **`_page()`** (sign-in results, consent done, errors) renders `base.html` with
    `tab = none` and a `hide_tabs` flag instead of its inline HTML, so every page a person
    sees during sign-in is branded.

#### 3.2 Owner app

`apps/owner/src/ui/theme.ts` takes the brand values: `primary` `#2a9727` (pressed
`#1e6b1c`), `background` `#f8fbf7`, `text` `#262b25`, `muted` `#5a6559`, `border`
`rgba(38, 43, 37, 0.08)`, `info` `#eaf5e9`/`#1e6b1c`, `warning` `#fdf1e6`/`#db5f00`,
`danger` `#e03030`; `radius` `{sm: 12, md: 16, lg: 24}`; a new `accent` `#8ecc06` for the
live badge and the discount card's ring. The Home discount card becomes the hero card
(green-900, white text). The tab bar uses green-700 for the selected tab. The web export's
`app.json` gets the Spinach icon as `web.favicon` and `name: "Spinach Business"` (the
Spinach site's name for the business app). Screens still use only `src/ui/` components
(experience S5).

#### 3.3 Keycloak login theme

A login theme `kitchenless` in `services/identity/realm/theme/kitchenless/login/`:

*   `theme.properties`: `parent=keycloak.v2`, `import=common/keycloak`,
    `styles=css/styles.css css/kitchenless.css`, `locales=en`.
*   `resources/css/kitchenless.css`: sets the PatternFly 5 variables the v2 theme reads
    (`--pf-v5-global--primary-color--100: #2a9727`, `--100` hover `#1e6b1c`,
    `--pf-v5-global--link--Color: #1e6b1c`, `--pf-v5-global--BorderRadius--sm: 12px`,
    `--pf-v5-global--FontFamily--text` to `--brand-font`) plus the brand tokens; page
    background `--brand-bg` with the hero glow; the card with `--brand-radius-card` and
    `--brand-shadow-lift`; primary buttons with `--brand-gradient`.
*   `resources/img/spinach-icon.png` and the realm display name
    `"displayNameHtml": "<img src=…> Kitchenless"` are not used (HTML in the realm file is
    fragile); instead `kitchenless.css` shows the logo with
    `#kc-header-wrapper::before { content: url(../img/spinach-icon.png) }` at 40 px.
*   No `.ftl` template overrides: only CSS and images, so a Keycloak upgrade can't break a
    copied template.

The consent screen lists each requested scope by its `consent.screen.text`. The realm file
sets these on the class scopes (§1 SC-A5):

| Scope | Consent text |
|---|---|
| `constraints:read` | Read your household's allergies, diets and dislikes |
| `constraints:write` | Save allergies, diets and dislikes you tell it |
| `pantry:read` | See what's at home |
| `pantry:write` | Record what's at home or run out |
| `meals:plan` | Plan meals for you |
| `orders:place` | Place orders within your budget and approval rules |
| `approvals:request` | Ask your approver before spending above the band |

The build step lists every scope in `grants.CLASS_SCOPES` and gives each a text in this
style; a unit test fails if a class scope has none.

**Deploying the theme.** The image change (SC-A5) ships the files. The realm import is
skipped when the realm already exists, so on the VM the operator runs
`kcadm.sh update realms/kitchenless -s loginTheme=kitchenless` once, plus the scope texts;
SC-I8 adds `infra/keycloak-sync.sh` that applies `loginTheme`, the scope consent texts and
the `https://<DOMAIN>` redirect URIs and web origins to an existing realm, idempotently.

### 4. The story page `/me/orders/{ref}`

**Route.** `GET /me/orders/{ref}`, where `ref = ids.org_order_ref(household_id, order_id)`
(the same opaque 8-character form the owner app uses, keyed on the household so it is
unique per household). The route resolves `ref` against `orders` through `scope.scoped`
for the session's household; an unknown ref is 404 "We couldn't find that order." Adults
only (role `admin` or `member`); a child gets the existing "not available" page
(experience Conventions: a child never gets `/me` data about others).

**Data** (`services/api/story.py`, one function `build_story(q, order_id) -> Story`, pure
reads through `scope.scoped`; no writes, no tool calls):

| Section | Source | Shown |
|---|---|---|
| Summary | `orders` (state, total, seller, kind, delivery_window), `budgets`; seller name from `organizations.name` through `agent_cards.owner_ref = 'org:<id>'` | Seller display name, total, "of $200.00 this week" and a bar, state as a pill |
| Who we asked | `quotes` for the order, grouped by `seller_agent_id`, named as in Summary | Per seller: first offer total, final total, lines unavailable count, outcome pill (`accepted` Won, `rejected` Not chosen, `expired` No reply in time, missing No offer) |
| Negotiation | `agent_messages` of `type IN ('counter_offer', 'offer', 'accept', 'reject')` for the order's conversations, ordered by `created_at` | Each turn as a line: who, what, amount; the seller's `rationale` (already checked by agents `check_rationale`) under its offer |
| Checks in code | `agent_traces` rows of the order's run with `kind = 'tool_result'` for the guard tools and `kind = 'decision'` with `payload.check` | One row per check: allergens (codes checked, lines checked, result), diet, budget (limit, total, result), approval rule (needed or not, who approved, when) |
| Delivery | `agent_messages` `delivery_quote` / `delivery_booked`; `orders.delivery_agent_id` | Courier quotes (fee, window) and the chosen one |
| Behind the scenes | `agent_traces` for the order's run | Distinct `agent_id` count, `message` rows, `decision` rows split by `payload.source` (`model` / `rule_default`), sum of `tokens_in + tokens_out`, `max(ts) - min(ts)` |

The checks section shows codes as words (`peanut` → "peanuts", `vegetarian`) from a new
`codes.LABELS` mapping (one plain word per `AllergenCode` and `DietCode`, unit-tested to
cover every code), the union across the household, never a member's name.
Agent ids show as display names; a household's own agents show as "Your household agent".
Where the build step finds a guard check that doesn't write a `tool_result` or `decision`
row the story can read, that check is shown from `orders`/`approvals` state instead (budget
from `orders.total_minor` against `budgets`; approval from `approvals`) and the PR records
it under Decisions; no new trace rows are added (SG6).

**Template** `services/api/templates/story.html` extends `base.html` with
`tab = "household"`. The sections are cards; Negotiation and Behind the scenes are a
vertical timeline (a 2 px green-50 line with 10 px dots: green passed/accepted, orange
countered, muted expired).

**Links in.** `/me/household`'s week order card, and each delivered meal row, gain "See
how this order was made →" to `/me/orders/{ref}`. The **See how** button (§5) arrives as
`me:story`; ingest handles it like the existing `me:pantry` button: it sends `me_link`
with `next = "story"` (SC-A7). `GET /me/orders/latest` redirects (303) to the newest
order of the session's household that has left `draft`, or shows "No orders yet."

### 5. WhatsApp: progress, story line, See how

*   **`order_progress`** (new text key): "On it. Asking {sellers} for {items}. Every line is
    checked for {checks}, and your {budget} {period} budget." `{sellers}` is "3 stores" or "2
    restaurants"; `{items}` "18 items" or "2 dinners"; `{checks}` the household's allergen and
    diet words joined with commas ("peanuts and sesame, vegetarian"), or "your rules" when
    there are none; `{budget}` and `{period}` from `budgets`. Sent by the household graph
    when the first `quote_request` of an order goes out (SC-A6), only inside the window (it
    is never held: a stale progress line is noise), only to the adult who started the run.
*   **Story line**, appended to `order_summary`'s `{text}` by `_summary` in the household
    graph: "I asked {n} {kind}. {seller} won at {total}{counter}. {checked} of {lines} lines
    passed the allergy and diet checks. Spent {spent} of {budget} this {period}." where
    `{counter}` is " after one counter-offer (was {first})" when a counter was accepted, else
    "". Built from graph state the household graph already holds (offers, the counter, the
    merged constraints, the budget); one line, no newlines (template parameters can't hold
    them).
*   **See how button** on `order_summary` and in-window `approval_request`: title "See how",
    payload `me:story`, which opens the newest order's story: the order the message is about
    is always the household's newest one when it is sent. Not added to Meta templates (their
    buttons are fixed).

### 6. Owner app: the negotiation sheet

**API.** `GET /app/orders/{ref}/turns` (through `scope.org_scoped`, like every `/app`
route). `ref` is the org's order ref; the route finds the order by matching
`ids.org_order_ref(org_id, order_id)` over the org's quotes. Response:

```json
{"ref": "K7Q2M9XD", "state": "negotiating", "live": true,
 "turns": [
   {"at": "…", "by": "them", "kind": "request", "text": "2 dinners · nut-free, vegetarian"},
   {"at": "…", "by": "you", "kind": "offer", "amount_minor": 4800, "rationale": "…"},
   {"at": "…", "by": "them", "kind": "counter", "amount_minor": 4080},
   {"at": "…", "by": "you", "kind": "accept", "amount_minor": 4080, "floor_minor": 3840},
   {"at": "…", "by": "system", "kind": "courier", "text": "Courier booked"}]}
```

Sources: the org agent's conversations in `agent_messages` for that order (`quote_request`,
`offer`, `counter_offer`, `accept`, `reject`, `cancel`, `order_confirm`, `order_status`);
`floor_minor` from the owner rule at the time (identity-authz I33's decision row) when it
bound. Constraint tags are the request's codes as words, as in insights. **Never**: a
household id, name, address, member, or another seller's offer. `live` is true while the
order state is `quoting` or `negotiating`.

**Screen.** Orders rows show a lime "Live" badge while `live`. Tapping a row opens a bottom
sheet (the existing `sheet.tsx`) with the turns as a timeline, refreshing every 5 s while
open. Loading, empty and error states as every screen (experience S5).

### 7. The presenter view `/panel/screen`

**Page.** `GET /panel/screen` (behind the panel's basic auth) returns one HTML page plus
`/panel/static/screen.js` and `screen.css` (no inline script, so the page can carry
`Content-Security-Policy: default-src 'self'`). Dark theme: `--brand-green-900` background,
white cards, lime accents; sized for a 1920×1080 projector and readable at 3 m (18 px
minimum).

**Feed.** `GET /panel/screen/feed?after=<ts>&run_id=` returns rows newer than `after`
(default: the latest run's start), at most 200, as:

| Row | From | Fields |
|---|---|---|
| `message` | `agent_traces` `kind = 'message'` | from, to (display names), type, summary (agent-protocol summary line) |
| `decision` | `agent_traces` `kind = 'decision'` | agent, `payload.source` (model or rule default), chosen option label, `model`, `latency_ms` |
| `check` | `agent_traces` `kind = 'tool_result'` for guard tools | tool, result (`ok` or the refusal code) |
| `approval` | `agent_traces` `kind = 'approval'` | requested / approved / rejected |
| `totals` | aggregate over the run | agents, messages, model calls, tokens, elapsed |

No payload bodies, phone numbers, names or addresses: the projector is public. Household
agents show as "Household agent", people as "Maya's household" only by the household's
display name, which the operator chose.

**Layout.** A header with the logo, "Live: agents at work", the run kind and elapsed
timer. One column per agent in the run (household first, then sellers, then courier), each
headed by an icon and display name. Message cards appear under the sender with an arrow
label to the receiver; decision cards show "Model: Spark" or "Rule default" with a small
latency; check cards show a green tick or an orange stop. A footer bar shows the totals.
`screen.js` polls the feed every 1.5 s, appends cards with a 200 ms slide-in, and follows the
newest run unless `run_id` is pinned in the URL.

### 8. Tests and evals

*   Unit: `test_brand_tokens.py` (three copies equal), `test_story.py` (each section from
    seeded rows; no member names; child refused; other household 404), `test_me_story_route.py`,
    `test_order_progress.py` (sent once, only in window, only adults, wording), the story
    line in `test_household_summary.py`, `test_app_turns.py` (no household fields; other
    org 404), `test_panel_screen.py` (feed fields, no bodies), `test_realm_file.py` (login
    theme set, every class scope has consent text), owner app jest tests for the sheet.
*   Evals: no case or threshold changes. `order_summary` stays the same key; cases that
    check it look at the key and buttons, and the new button's payload has no decision
    prefix. The build step runs `make eval-fast` to confirm.

## Alternatives Considered

### SC-ALT1: A model-written recap of the order

Rejected for the same reasons as experience EX-ALT22: cost, latency and a model describing
checks it didn't run. Code already knows every fact.

### SC-ALT2: Streaming the presenter view over SSE

Rejected: `api` has no streaming path today and polling every 1.5 s over a few hundred rows
is cheap at demo scale (experience EX-ALT21 for the owner app).

### SC-ALT3: One shared CSS file served to all three surfaces at runtime

Rejected: Keycloak themes are packaged in the image and the owner app is a React Native
web export with JS style objects. Three copies with one test is simpler than a build step
that generates them.

### SC-ALT4: Overriding Keycloak `.ftl` templates

Rejected: copied templates break on upgrades. CSS variables and the logo cover the look.

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| The story shows something a member shouldn't see | Only household-level facts that `/me/household` already shows to adults, plus seller names and codes as words; never names next to codes; tests for child and cross-household access |
| `order_progress` adds noise | One per order, in-window only, never held, never a decision |
| The presenter view leaks data on a projector | Summaries and display names only; test asserts no body fields |
| The realm theme isn't applied on an existing realm | `infra/keycloak-sync.sh` (SC-I8) and the operator step in §3.3 |

## Detailed Implementation Plan

### Build order

| Step | Items | Depends on | Critical |
|---|---|---|---|
| SC1 | SC-I1 brand tokens, logo, token test | — | no |
| SC2 | SC-I2, SC-I3 `/me` restyle and `_page()` through `base.html` | SC1 | no |
| SC3 | SC-I4 owner app theme | SC1 | no |
| SC4 | SC-I5 Keycloak login theme, realm `loginTheme`, scope consent texts | SC1 | yes (`services/identity/`, `infra/`) |
| SC5 | SC-I6 story builder and `/me/orders/{ref}` | SC2 | no |
| SC6 | SC-I7 `order_progress`, story line, See how button and `me:story` ingest | SC5 | yes (`services/identity/links.py`) |
| SC7 | SC-I8 `infra/keycloak-sync.sh` | SC4 | yes |
| SC8 | SC-I9 `/app/orders/{ref}/turns` and the owner negotiation sheet | SC3 | no |
| SC9 | SC-I10 `/panel/screen` and its feed | SC1 | no |

### Target Files to Create/Modify

*   **SC-I1.** `services/api/static/brand.css` (Create), `services/api/static/brand/spinach-icon.png`
    (Create), `tests/unit/test_brand_tokens.py` (Create).
*   **SC-I2.** `services/api/static/me.css` (Modify), `services/api/templates/base.html`,
    `household.html`, `pantry.html`, `history.html`, `agents.html`, `band.html`, `error.html`
    (Modify: classes only, no copy changes).
*   **SC-I3.** `services/api/routes/me.py` `_page()` (Modify), its tests (Modify).
*   **SC-I4.** `apps/owner/src/ui/theme.ts`, the components in `apps/owner/src/ui/` that hard-code
    radius or colour, `apps/owner/app.json`, `apps/owner/assets/spinach-icon.png` (Modify/Create).
*   **SC-I5.** `services/identity/realm/theme/kitchenless/login/` (Create),
    `services/identity/realm/kitchenless.json` (Modify: `loginTheme`, scope attributes),
    `infra/keycloak/Dockerfile` (Modify: copy the theme to `/opt/keycloak/themes/`),
    `tests/unit/test_realm_file.py` (Modify).
*   **SC-I6.** `services/api/story.py` (Create), `services/common/codes.py` (Modify: `LABELS`), `services/api/templates/story.html` (Create),
    `services/api/routes/me.py` (Modify: route, next-page set), `household.html` (Modify: link),
    tests (Create).
*   **SC-I7.** `services/api/whatsapp/sender.py` (Modify: `order_progress` text, See how
    button rendering), `services/agents/household/graph.py` (Modify: progress send, story
    line), `services/api/ingest.py` (Modify: `me:story`), `services/identity/links.py` (Modify:
    `NEXT_PAGES` gains `story`; critical), tests.
*   **SC-I8.** `infra/keycloak-sync.sh` (Create), `tests/unit/test_keycloak_sync.py` (Create).
*   **SC-I9.** `services/api/routes/app.py` (Modify), `apps/owner/app/(tabs)/orders.tsx`,
    `apps/owner/src/api.ts` (Modify), tests.
*   **SC-I10.** `services/api/routes/panel.py` (Modify), `services/api/templates/screen.html`,
    `services/api/static/panel/screen.js`, `screen.css` (Create), tests.

### Out of scope (implementation)

Native builds, a Satoshi font without SC-Q1's answer, account and admin console themes,
streaming, any change to agent behaviour or prompts beyond sending `order_progress`.

### Verification Plan

`make lint test-unit` with `DATABASE_URL` set; `make app-lint app-test` for SC3 and SC8;
`make eval-fast` for SC6; on the VM after deploy: run `infra/keycloak-sync.sh`, sign in
through a Renew link (branded login and consent), run "do the weekly shop" and follow
SC-CUJ 1–4 with `/panel/screen` open.
