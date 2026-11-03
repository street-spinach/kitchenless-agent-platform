# Kitchenless experience: WhatsApp, the /me pages, onboarding, reminders and the owner app

*   **Author:** madushankaf
*   **Status:** Approved; amended 2026-11-03 (owner app CSP allows inline styles, EX-I45)
*   **Date:** 2026-11-03
*   **Path:** `docs/designs/experience.md`
*   **Issue:** #271
*   **Builds on:** [architecture.md](architecture.md), [agent-protocol.md](agent-protocol.md),
    [identity-authz.md](identity-authz.md), [adapters.md](adapters.md),
    [agents.md](agents.md), [evals.md](evals.md) and [pantry.md](pantry.md), all approved.
    Where this design needs one of them to change, the table in Technical Plan §1 lists the
    change. Nothing here relies on a change that table doesn't list.
*   **Input:** the experience brainstorm brief, the EGM review of this design on 2026-11-03
    (five stages, consolidated report) and the product owner's rulings on it (D1–D5, recorded
    in the decisions below), and the code on `main` at `d77d9c2`. The brief and reports live
    in `.loop-logs/`, which is not in the repository, so every decision this design relies on
    is written out in full below.

**Conventions:**

*   **Ids.** Product-owner decisions are EX-OD1…, goals XG1…, journeys EX-CUJ 1…, changes to
    other designs EX-A1…, alternatives EX-ALT1…, open questions EX-Q1…, build steps X1…,
    and implementation items EX-I1…. An id from another design always carries that design's
    name ("identity-authz I24", "pantry P2", "agents AG-A1"). "&lt;design&gt; §N" means a
    section of that design; a bare "§N" means section N of this design's Technical Plan.
*   **People.** The **product owner** makes the decisions in this design. A **business
    owner** runs a store, restaurant or courier and uses the owner app. The **operator** runs
    the VM and the Meta account (today the same person as the product owner).
*   **Roles.** Every rule keys on `household_members.role`, never on whether a phone exists.
    **Admin** is role `admin`; **parent** always means admin. **Adult** is role `admin` or
    `member`; a teenager with a phone is a member. **Child** is role `child`. A child may
    have a phone (the seed gives `us-1-c` and `us-1-c-eval` one): a child gets replies to
    its own messages, but never decision buttons, reminders or `/me` data about others.
    **Approver** is `approval_rules.approver_user_id` (agents §10). **Guardian** is
    identity-authz's relation: every admin of the child's household.
*   **Window.** WhatsApp's customer-service window: the 24 hours after a person's last
    message to the platform number. Inside it, any text or reply buttons may be sent.
    Outside it, only an approved Meta template is delivered; anything else is refused by
    Meta with error 131047 (Meta Cloud API error codes).
*   **Three kinds of "template".**
    *   A **text key**: an entry in `TEXTS` in `services/api/whatsapp/sender.py`, for
        example `approval_request`. Evals grade these keys (evals.md), so keys never change.
    *   A **Meta template**: a message Meta has approved for sending outside the window, for
        example `approval_needed_v1`. Each Meta template has its own fixed body (§5) and
        maps to exactly one text key.
    *   A **page template**: a Jinja2 file that renders a `/me` page (§9).
*   **Utility template.** A Meta template in Meta's "utility" category: about a specific
    order, delivery or account event. The "marketing" category is never used (EX-OD5).
*   **Held item.** A message that could not be sent because the person was outside the
    window and no approved Meta template exists for it. It is stored as the unrendered
    `WhatsAppOut` (text key and typed params) plus a reference to the item it is about, and
    re-checked before it is sent (§5).
*   **Outbox.** `agent_messages`, through `services/common/outbox.py` (architecture §4).
*   **Decision asked.** One message delivered to one person that has at least one button
    whose payload has a decision prefix: `ap:` (approval), `tt:` (take-turns), `ob:` (setup)
    or `sf:` (shortfall pick) (§4).
*   **Spark.** The `people` model profile, Muse Spark (agents.md §2).
*   **Band.** identity-authz's per-order spend band (`approval_band`, identity-authz §9).
*   **Surfaces.** S1 WhatsApp chat, S2 `/me` web pages, S3 onboarding chat, S4 reminders,
    S5 the owner app, S6 the demo panel.
*   **Critical path.** A path listed in CLAUDE.md "Critical paths" and
    `.github/critical-paths.txt`; a PR touching one waits for human review.

--------------------------------------------------------------------------------

## Overview

Kitchenless makes a household's routine food decisions for it, within limits the household
sets. People reach it through one WhatsApp number, small web pages opened from chat, and,
for business owners, one app. The other approved designs define what the agents do. This
design defines every place a person touches the system: what each message says and which
buttons it has, how a message reaches someone who hasn't written for a day, how a new
household tells the agent about allergies and budgets, what the household sees on its web
pages, which reminders it gets, and what a business owner sees in the app.

The product owner's aim is a product that people come back to often without spending time
in it, the way they use WhatsApp itself. The measure of success is therefore not time in
the app. It is the number of decisions the system asks of each person each week (§4).

Today none of these surfaces exist beyond fixed message texts. `/me/*` and `/app/*` return
`501 {"owner": "experience design"}` (`services/api/routes/reserved.py`), and identity-authz
steps 8 and 13 add the first minimal pages. Messages to someone outside the window are
retried and then fail. Onboarding happens only through the seed and the panel.

### Owner decisions

The brainstorm's decisions were accepted by the product owner on 2026-11-03. The review of
this design on the same day led to the rulings marked "D1"–"D5" and to the fixes marked
"review".

| ID | Decision | From |
|---|---|---|
| EX-OD1 | **One design for every human-facing surface.** It extends identity-authz's `/me` (identity-authz I24) and `/app` (identity-authz I32) routes and pages, and never redefines them. Every build step names the identity-authz step it waits on. | brief; review |
| EX-OD2 | **Success is fewer decisions asked.** The weekly count of decisions asked per person (§4) should fall over the weeks, while the guard metrics listed in §4 must not rise. No code path may change a budget, band, approver, allergy or diet to lower the count; only a person's explicit tap does. Time in the app is not measured. | brief; review |
| EX-OD3 | **Social stays inside one household; nothing about one household leaves it.** Allergies, diets, budgets, spending, schedules, location and anything about a child never reach another household, and reach a business only as an order's constraints (agent-protocol) or inside weekly counts over at least 5 distinct households (EX-OD12). Inside the household, the visibility table (§4) decides who sees what, enforced in queries. Parents (admins) see their children's data. There are no extra consent toggles beyond identity-authz's per-agent grants. Social features between households are a Non-Goal. | brief; D2 |
| EX-OD4 | **Inside the window: text and reply buttons. Outside: utility templates, then held items.** Inside the window every message is text with at most 3 reply buttons. Outside, a message is sent as its Meta template if one is approved; otherwise a holdable message is held and re-checked when the person next writes (§5). Model-written text is never sent outside the window. No list messages, no WhatsApp Flows, no links in Meta templates. | brief; review |
| EX-OD5 | **Two reminders, built from facts.** `delivery_today` and the weekly `pantry_summary`. Each is derived each minute from current state by the sweeper, carries information, and has no decision button. At most 1 per person per day, never in quiet hours, never to children, with a "Stop these" button. Utility templates only. Text is built by code. The approval-closing, consent-before-expiry and budget-used reminders are dropped: identity-authz allows one message per approval and per re-consent (identity-authz I-D6). | brief; D1 |
| EX-OD6 | **The pantry summary comes with a correction page.** The weekly summary ("At home by our records: rice, milk. Not sure about: eggs, bread.") has an "Open pantry" button that sends a fresh `/me/pantry` link. There an adult taps "Still have it" or "Ran out" per item, writing pantry.md's `verify` or `out` events. This is the correction path pantry PT-OD7 asks for. The summary goes only to households where the pantry is on (`env = 'eval'`, pantry PT-OD4); lifting that gate stays with pantry.md. | brief; D1 |
| EX-OD7 | **Onboarding is a chat.** The admin answers in their own words. Spark may only pick codes from the fixed allergy and diet lists; code validates them, cross-checks the message for words Spark didn't use, stores the proposal as a row, and restates exactly what will be saved. Nothing is saved without the admin's "Yes" tap; removing an allergy or diet needs a second tap. | brief; reconcile |
| EX-OD8 | **The household page is the social surface.** `/me/household` shows the current plan, what the agent did and why, spending against the budget, and whether take-turns is on. Each delivered meal has an optional 👍/👎 reaction shown only as a total, and hidden in households with fewer than 3 adults. Reactions are never read by an agent, prompt or rule. | brief; D5 |
| EX-OD9 | **The take-turns switch lives on the household page.** Only the approver can change it; others see it read-only (EX-A6). | review |
| EX-OD10 | **The owner app is an Expo web export.** It is built in CI into the api image and served at `/owner/` (answers identity-authz Q6). Its quality bar: changing the discount rule takes at most 2 taps from the first screen, one shared design system, loading, empty and error states on every screen, and pickers wherever a choice is fixed. | brief |
| EX-OD11 | **The owner app shows what business owners can act on.** Sign-in; the `max_discount_pct` rule (the only approved owner rule, identity-authz I-D8); live orders and negotiations; read-only notices; read-only connection status; insights. No approving single quotes (identity-authz Non-Goal), no connecting a POS (adapters Non-Goal). | review |
| EX-OD12 | **Insights are weekly aggregates; the demo uses labelled sample data.** Busiest delivery hours, the most-asked constraint tags (allergy and diet codes) and quotes won, per ISO week, each cell shown only when it covers at least 5 distinct households. No cumulative totals, so cells can't be subtracted from each other. With `INSIGHTS_SAMPLE=1` the screen shows a fixed sample marked "Sample data". Text is built by code. | brief; D2 |
| EX-OD13 | **Final wording, stable keys.** This design writes the final wording of every text key and every Meta template body. Keys never change. Every action a person takes gets a visible reply, including refusals. Approvals keep Approve and Reject only. | review |
| EX-OD14 | **Meta templates are submitted by hand.** The operator submits them in WhatsApp Manager from a runbook and writes each approved name into the VM's `infra/.env`. Code sends a Meta template only when its name is configured; otherwise it holds or skips. | review |
| EX-OD15 | **New adults join with a phone-bound code.** The admin types each adult's phone during setup. A join code (40 bits, 7 days, single use) is created for that exact phone; only a message from that phone with that code joins. The adult's allergies and diets are stored on the code and applied to household orders at once, then moved to the person when they join. | D3 |
| EX-OD16 | **A member's allergy goes through the admin.** A member who states an allergy or diet is told to ask the admin (`dislike_not_saved`); no request is forwarded. | D4 |

## The Problem

1.  **Messages to people who haven't written for a day are lost.** The sender posts text
    and buttons to Meta whatever the window, and retries every failure 3 times, 20 seconds
    apart (`sender.py:80-83`, `:200-211`). Meta reports an outside-window failure in a later
    status callback, which nothing reads. An approval request to a parent who last wrote
    yesterday never arrives, and the order expires after 4 hours (`approval_ttl_seconds`,
    `config.py:76`). This is a risk agents.md hands to this design (agents.md:1338).
2.  **The system has no record of the window.** `inbound_messages` stores no phone or user,
    so "when did this person last write" can't be answered.
3.  **The mock path hides real failures.** In `MODE=mock` the sim sender never runs Meta's
    payload rules (at most 3 buttons, titles of at most 20 characters) or a window check.
4.  **Wording is unfinished.** Some texts are placeholders, `nothing_needed` is missing, and
    some refusals send nothing, so a person who taps a button sometimes sees no reply.
5.  **Onboarding exists only in the seed.** No message, page or tool lets a household say
    who its members are, what they are allergic to, or what it can spend.
6.  **The household can't see what its agent did.** There is no plan, spending or history
    view, and the take-turns switch exists only as a button after a dinner.
7.  **A wrong pantry skip can't be corrected,** which keeps the pantry off for real
    households (pantry PT-OD7).
8.  **Business owners have no app.** identity-authz adds one rule page. There is no build
    path for an Expo app, no view of orders, and no insights.
9.  **Nothing measures whether the experience works.** There is no count of what the system
    asks of people.

This design addresses 1–6, 8 and 9 fully, and 7 for households where the pantry is on
(EX-OD6).

## Goals

Every goal names the test or check that verifies it. Unit tests run with `MODE=mock` and
`MODEL_MODE=stub` (CLAUDE.md). Test file locations are the ones the build items name.

*   **XG1. Decisions asked per person per week are counted, next to the guard metrics.** The
    view `experience_weekly_asks` counts them from delivered sends only (§4). The success
    target is a falling count over weeks with no guard metric rising; there is no numeric
    target. *Checked by:* `tests/unit/test_experience_metric.py::test_counts_delivered_decisions_only`,
    `::test_held_counted_once`, `::test_stop_and_open_buttons_not_counted`, `::test_guard_metrics`,
    `::test_falls_across_weeks`.
*   **XG2. No free-form message is sent outside the window, in mock mode too.** *Checked by:*
    `tests/unit/test_whatsapp_window.py::test_outside_window_holds`, `::test_sim_applies_window`,
    and the safety case `safety-no-freeform-outside-window`.
*   **XG3. A held item is sent only while it can still be acted on.** On the person's next
    message, an expired approval is dropped, never sent; at most 3 items are sent, oldest
    first. *Checked by:* `tests/unit/test_held_items.py::test_expired_approval_dropped`,
    `::test_release_cap`, and the safety case `safety-expired-held-approval-never-sent`.
*   **XG4. Meta refusals are not retried, and late refusals are caught.** An outside-window
    refusal, at send time or in a status callback, leads to holding; a template refusal ends
    with its own reason. *Checked by:* `tests/unit/test_whatsapp_sender.py::test_window_refusal_not_retried`,
    `tests/unit/test_whatsapp_status.py::test_status_131047_holds`.
*   **XG5. Every message fits WhatsApp's limits and every key renders.** *Checked by:*
    `tests/unit/test_whatsapp_texts.py::test_every_key_renders`, `::test_button_limits`.
*   **XG6. Every action gets a visible reply.** *Checked by:*
    `tests/unit/test_whatsapp_texts.py::test_every_refusal_has_text`.
*   **XG7. Onboarding saves nothing without a tap and never drops a word silently.** A
    constraint is written only after the admin's "Yes"; any allergy or diet word Spark
    didn't use is named in the restatement; a non-admin can't save allergies or diets.
    *Checked by:* `tests/unit/test_household_setup_tools.py::test_apply_nothing_saved_without_yes`,
    `::test_apply_member_refused`, `tests/unit/test_consumer_graph.py::test_unmatched_word_quoted_back`,
    `::test_cross_check_names_missed_word`, and the safety cases `safety-no-allergy-without-tap`
    and `safety-unknown-allergen-refused`.
*   **XG8. A joining adult's allergies protect them before they join.** Household orders
    exclude the allergens stored on an open join code. *Checked by:*
    `tests/unit/test_household_tools.py::test_open_join_code_allergens_merged` and the safety
    case `safety-pending-joiner-allergy-enforced`.
*   **XG9. Nobody sees data the visibility table doesn't give them.** *Checked by:*
    `tests/unit/test_visibility.py::test_matrix` (one assertion per §4 cell) and
    `tests/unit/test_me_household.py::test_member_sees_no_child_constraint`.
*   **XG10. Reminders are capped, factual and safe.** *Checked by:*
    `tests/unit/test_reminders.py::test_daily_cap`, `::test_quiet_hours`, `::test_stop_these`,
    `::test_no_children`, and `tests/unit/test_template_send.py::test_params_have_no_sensitive_codes`
    (every Meta template key).
*   **XG11. A household can correct its pantry.** *Checked by:*
    `tests/unit/test_pantry_correction.py` and the eval case `task-pantry-ran-out-reorders`.
*   **XG12. The approver can switch take-turns from the household page.** *Checked by:*
    `tests/unit/test_me_household.py::test_take_turns_switch` and the eval case
    `task-take-turns-switch-from-page`.
*   **XG13. A business owner changes the discount rule in at most 2 taps.** *Checked by:*
    `apps/owner/__tests__/discount.test.tsx` and identity-authz's `test_owner_rule_raises_floor`.
*   **XG14. Business owners see only their own organisation and never one household.**
    *Checked by:* `tests/unit/test_app_routes.py::test_org_scoped`, `::test_no_household_fields`,
    `::test_insights_suppressed_below_k` (seeded live-like rows; §14 says why this is not an
    eval case).

## Non-Goals

*   **No social features between households,** no WhatsApp groups, no public sharing
    (EX-OD3).
*   **No "your turn to pick" and no "weekly plan ready".** Take-turns decides whose dislikes
    come first (agents.md §10); no design has a plan-review step; both would add decisions.
*   **No general scheduler.** Reminders are derived each minute from current state (§10);
    no row schedules future work.
*   **No per-person ratings and no recap message** (EX-OD8).
*   **No list messages, no WhatsApp Flows, no links inside Meta templates** (EX-OD4).
*   **No marketing templates and no nudges** (EX-OD5).
*   **No approval-closing, consent-before-expiry or budget reminders** (EX-OD5).
*   **No quantity or expiry reminders.** The pantry has neither (pantry PT-OD1).
*   **No change to the pantry gate** (pantry PT-OD4).
*   **No approving from a web page.**
*   **No forwarding of a member's allergy to the admin** (EX-OD16).
*   **No live owner approval, no Square OAuth, no new owner rule kinds** (EX-OD11).
*   **No push notifications and no streaming** to the owner app; it polls.
*   **No native consumer app** and no single-page app for `/me`.
*   **No teenager role.**
*   **No model-written text outside the window, in reminders, notices or insights.**
*   **No change to agent behaviour** beyond the rows of §1. This design owns how messages
    look, not what agents decide (agents.md:298).

## Open Questions

**Resolved by this design:**

*   **architecture Q5** (templates for agent-initiated messages): utility templates
    submitted by hand, held items as the fallback (EX-OD4, EX-OD14). Groups stay out
    (EX-ALT4).
*   **identity-authz Q5** (renew and revoke notices outside the window): the utility
    templates `consent_renewal_v1` and `consent_revoked_v1`, holdable as `grant`, with a
    "Renew" button that sends a fresh link (§5).
*   **identity-authz Q6** (Expo redirect URI): `https://<host>/owner/auth` (EX-OD10).

**Still open:**

*   **EX-Q1. Will Meta accept every template as utility?** Meta decides the category when a
    template is submitted. A template filed as marketing is not configured, and its message
    is held (holdable kinds) or skipped (reminders). The operator step X30 records the
    outcome.
*   **EX-Q2. Should the pantry summary go to every adult or only the approver?** This design
    sends it to the approver.

## Critical User Journeys (CUJs)

The demo household is `us-1-eval` where the pantry is involved (pantry PT-OD11), and the
seeded US family otherwise.

### EX-CUJ 1: An admin sets up the household by chat

*   **Given**: Maya is registered by the panel (identity-authz §5) as the first admin of a
    new household and has consented through her link. The household has no budget.
*   **When**: Maya writes "hi" and gets `setup_ask`. She answers "Me, my husband Sam
    +1 555 0102, and Kavi who is 8. Kavi can't have peanuts or sesame. Sam is vegetarian.
    Budget 200 a week."
*   **Then**: She gets `setup_restate`: "I'll save: Sam (+1 555 0102): vegetarian, applied
    to household orders now; he joins when he writes from that number. Kavi (child):
    allergic to peanuts, sesame. Weekly budget $200.00. Nothing else is saved." with "Yes,
    save" and "No". She taps "Yes, save". `api` creates Kavi with `create_child`; the tool
    writes Kavi's constraints, the budget, and a join code for Sam's phone holding his diet;
    Maya gets `join_link` to forward. Decisions asked: 1.

### EX-CUJ 2: A word the agent doesn't recognise

*   **Given**: As EX-CUJ 1, after setup.
*   **When**: Maya writes "Kavi is allergic to kiwi and nuts".
*   **Then**: The restatement reads "I'll save: Kavi: allergic to tree nuts, peanuts. Not
    saved: 'kiwi' (I can record: celery, gluten, crustaceans, …). Nothing else is saved."
    Had Spark chosen only one nut code, the cross-check would add "I also saw 'nuts' but
    didn't save it. Say 'add nuts for Kavi' if it applies." "No" writes nothing and replies
    `setup_discarded`.

### EX-CUJ 3: Sam joins

*   **Given**: Sam's join code is open. A household dinner is ordered before he joins.
*   **When**: The dinner's constraints are merged; later Sam taps Maya's forwarded link and
    sends "JOIN K7Q2M9XD" from +1 555 0102.
*   **Then**: The dinner already respects Sam's vegetarian diet (XG8). On his message,
    ingest checks the code against his phone, calls `create_person`, moves his diet onto
    his user, sends `joined` and then his consent link. The same message from any other
    phone gets `unknown_number`.

### EX-CUJ 4: An approval for a parent who hasn't written since yesterday

*   **Given**: Maya last wrote 30 hours ago. The weekly order needs her approval.
    `approval_needed_v1` is approved and configured.
*   **When**: The household agent requests approval.
*   **Then**: The sender finds Maya outside the window and sends `approval_needed_v1` with
    the amount and reply-by time and the same Approve and Reject buttons.
*   **Variant**: no Meta template is configured. The item is held. When Maya next writes,
    the approval is still pending with more than 5 minutes left, so it is sent; if it had
    expired, it would be dropped and she would get `held_cancelled_line`.

### EX-CUJ 5: A member opens the household page

*   **Given**: Sam has joined and consented.
*   **When**: Sam writes "my household" and taps the link in `me_link`.
*   **Then**: `/me/household` shows the week's order and its explanation, the spending total
    against the budget, whether take-turns is on, and the last delivered meals with reaction
    totals (the household has 2 adults, so totals are hidden and only Sam's own reaction
    shows). No member's dislikes, no per-order spending. Decisions asked: 0.

### EX-CUJ 6: The approver switches take-turns on

*   **When**: On `/me/household` Maya turns the switch on.
*   **Then**: `set_household_take_turns` writes `household_rules.take_turns = true`, `set_by
    = Maya`; the page shows "Take turns is on". Sam sees the switch read-only.

### EX-CUJ 7: The weekly pantry summary and a correction

*   **Given**: `us-1-eval`, pantry set by the `pantry` setup key: rice and milk trusted,
    eggs and bread stale. It is Saturday after 10:00 household time.
*   **When**: The sweeper's reminder pass runs.
*   **Then**: Maya gets `reminder_pantry_summary` with "Open pantry" and "Stop these". She
    taps "Open pantry", gets a fresh `me_link`, and on `/me/pantry` taps "Ran out" on rice.
    One `out` event is written. The next weekly order keeps rice with reason `out`.
    Decisions asked: 0.

### EX-CUJ 8: A delivery reminder and "Stop these"

*   **Given**: Sam is outside the window; his household's grocery delivery window is today
    18:00–19:00; `delivery_today_v1` is configured.
*   **When**: The reminder pass runs after 08:00 household time.
*   **Then**: Sam gets "Groceries from Store B arrive today, 6–7 pm. Nothing needed from
    you." with "Stop these". He taps it; he gets `reminders_off`; no reminder reaches him
    again until he writes "reminders on".

### EX-CUJ 9: A business owner changes the discount rule (demo moment)

*   **Given**: The restaurant's business owner is signed in to the owner app.
*   **When**: Home shows "Most discount you'll give: 10%". She taps it, then taps "20%".
*   **Then**: `POST /app/rules {max_discount_pct: 20}` (identity-authz I32) saves it; the
    card shows the new floor on a sample dish. The next dinner quote uses it, and the
    family's chat shows the cheaper offer. Taps: 2.

### EX-CUJ 10: A business owner reads insights

*   **Given**: Fewer than 5 households ordered from her restaurant this week.
*   **When**: She opens Insights.
*   **Then**: Real cells are hidden ("Not enough orders yet"). With `INSIGHTS_SAMPLE=1` the
    screen shows the labelled sample: "Sample data. Busiest: Thursday 18:00–19:00. Most
    asked: nut-free. Quotes won: 7 of 12."

--------------------------------------------------------------------------------

## Technical Plan (Architecture)

### 1. Changes to the approved designs

Each row is applied by this design's approval PR (X1) as one note
`*Amended 2026-11-03 (experience, #271):*` in the target design, one note per row.

| ID | Target | Change | Why |
|---|---|---|---|
| EX-A1 | architecture Q5; §13 item 38; line 1199 | Q5 resolved by EX-OD4 and EX-OD14. `/me/*` and `/app/*` are served by identity-authz I24/I32 and this design; "return 501" holds only until those land. | Problem 1 |
| EX-A2 | architecture §4 (sweeper) | The sweeper runs a reminder pass every 60 s (§10). `timers` is unchanged. | EX-OD5 |
| EX-A3 | architecture §11 (Caddy, image) | `handle /owner/*` reverse-proxies to `api`, which serves the Expo export from its image. The image build gains a `node:22` stage that produces the export; the runtime image has no Node. | EX-OD10 |
| EX-A4 | architecture §9 and the sim channel (line 1209) | The sim sender applies the same payload validation and window check as the real sender. Seeded and sim phones start inside the window (`last_inbound_at = now()` at seed and at `POST /panel/reset`). | XG2 |
| EX-A5 | agent-protocol P2 (`WhatsAppOut` in `schemas.py`) | An optional `ref: {kind: "approval" \| "order" \| "grant" \| "reminder", id: str} \| None`. Old code ignores it (`extra="ignore"`). | §5 |
| EX-A6 | agents AG-A1, §5 consumer graph | `INTENTS` adds `onboard`, handled by a new node `propose_setup` (§8). Code routes an admin of a household with no budget to it before the classifier runs. Spark extracts codes from the fixed lists and code plus the admin's tap decide, the same split as `learn_dislike` (agents.md:427, :543), so agents OD-14 ("code makes every decision") holds. | EX-OD7 |
| EX-A7 | agents §10, AG-A5 | `set_take_turns` gains an order-less form, `set_household_take_turns(on: bool, user_id: UUID)`: `system:api` only, the household from the run, `user_id` checked by `check_approver` as `set_take_turns` does (`server.py:797-845`), written to `set_by`. The tap refusal for `wrong_approver` sends `take_turns_not_approver`. | EX-OD9 |
| EX-A8 | agents §11, item 33 | Final wording of every text key (§6). `approval_request` adds `{reply_by}`; `dislike_not_saved` adds `{admin}`. Keys are unchanged. | EX-OD13 |
| EX-A9 | agents §6 (constraint merge) | The household's allergen and diet merge (`_member_allergens`, `get_household_allergens`, `get_constraints`) also includes the codes stored on the household's open join codes. | XG8 |
| EX-A10 | identity-authz Q5, Q6; §5 "Who calls it" | Q5: `consent_renewal_v1` and `consent_revoked_v1` (§5). Q6: redirect `https://<host>/owner/auth`. §5: the onboarding chat calls `create_child` (on a "Yes" tap, in `api`) and `create_person` (on a matching JOIN message, in `api`). | EX-OD7, EX-OD15 |
| EX-A11 | identity-authz §8 (scope, `TOOL_SCOPES`) | The new tables `join_codes`, `onboarding_proposals`, `held_messages`, `whatsapp_sends`, `reminders` and `meal_reactions` join the household-scoped list; the `unscoped` allowlist gains: `join_lookup` (ingest's join-code lookup by code), `held_release` (ingest, by user), `setup_phone_check` (`propose_setup`, yes/no only), `reminder_pass` (the sweeper), and `control.reset`. `scope.org_scoped(conn, org_id)` is added with the same scan test. `TOOL_SCOPES` gains: `get_setup_state` (delegated, consumer agent, the same scope and relation as `get_budget`); `propose_setup` (delegated, consumer agent, `constraints:write`, relation `acts_for user`); `apply_setup`, `complete_join`, `set_household_take_turns`, `react_to_meal`, `record_pantry_check` (service, `svc.api`, checks in `limits`). | review |
| EX-A12 | pantry PT-OD7, Future Improvements | The correction path is `/me/pantry` and `record_pantry_check` (§9), reached from the weekly summary. The gate (PT-OD4) is unchanged. | EX-OD6 |
| EX-A13 | adapters Non-Goals (:151) | The owner app shows connection status read-only; no OAuth. | EX-OD11 |
| EX-A14 | evals §4–5 | New cases (§14) and setup keys `last_inbound_hours_ago`, `setup_incomplete`, `join_code`, each undone by the next reset. | XG2, XG3, XG7, XG8, XG11, XG12 |
| EX-A15 | identity-authz §6, §12, I26 | The renew text becomes `consent_renewal` and the revoke notice becomes `consent_revoked` (§6 wording, a Renew button `me:renew`, Meta templates outside the window, holdable as `grant`), replacing "outside it, the panel shows re-consent needed". The approval prompt is neutral (amount and reply-by time, EX-A8): `test_approval_prompt_names_both_limits` becomes `test_approval_prompt_has_amount_and_reply_by`. `links.issue` gains `next: Literal["household", "pantry", "history", "agents"] \| None`, stored on the link and used by the callback's redirect (default `/me/agents` as today). | EX-OD4, §7 |
| EX-A16 | architecture §4 (`runs`) | `runs.retry_target` never returns a run with `household_id IS NULL` or `trigger.source` in (`code`, `join`, `tap`, `me`, `reminder`). | §3 |
| EX-A17 | pantry §4, item for `get_pantry` | `enabled` is computed by one pure function `pantry.is_enabled(env: str) -> bool` in `services/common/pantry.py`; `get_pantry`, the reminder pass and `GET /me/pantry` all call it. The rule (PT-OD4) is unchanged. | §8, §10 |

### 2. Dependencies

| Needs | From | Issue | On `main` at `d77d9c2` |
|---|---|---|---|
| Guard, `scope.scoped`, mock grants, `grants.py` | identity-authz step 5 | #115 | Not built |
| Links, sessions, `routes/me.py` | identity-authz step 8 | #123 | Not built |
| `create_person`, `create_child`, `unknown_number` | identity-authz step 9 | #124 | Not built |
| Band page, `approvals.limits` | identity-authz step 12 | #127 | Not built |
| `routes/app.py`, owner tokens, `max_discount_pct` | identity-authz step 13 | #128 | Not built |
| Migration 0014, `verify`/`out` | pantry P2, P3 | #254, #255 | Not built |
| `get_pantry` `enabled`, `nothing_needed` sender | pantry P4, P5 | #256, #257 | Not built |
| `links.py` (identity-authz I21) | identity-authz step 8 | #123 | Not built |
| `pantry` setup key | pantry P6 | #258 | Not built |
| Eval case format, checks, coverage, `reply_template` | evals 5a, 5b, 6, 10b | #228, #229, #231, #235 | Not built |
| Migration 0013, setup-key registry | evals | #226, #224 | Merged |

The build order (Detailed Implementation Plan) names which steps need none of these: X2,
X3, X5, X6 and X19.

### 3. Components, runs and where the code lives

```text
 WhatsApp (Meta) ──webhook──► api /webhooks/whatsapp ──► ingest
        │  messages: last_inbound_at, held release, ob:/rs:/me: taps, JOIN
        │  statuses: delivered/failed → whatsapp_sends (131047 → hold)
        ▼
 agents (consumer graph: onboard; household graph) ──► outbox (agent_messages)
 sweeper (agents): timers + reminder pass (60 s) ────────┤
                                                         ▼
           api sender: validate → window? ─► text + buttons      (inside)
                                         ─► Meta template      (outside, configured)
                                         ─► held_messages      (outside, holdable)
                                         ─► skipped            (outside, otherwise)
           every outcome → whatsapp_sends + a whatsapp_window decision row
 Browser  ─► Caddy /me/*    ─► api routes/me.py  ─► household MCP tools as system:api
 Business ─► Caddy /owner/* ─► api static export; /app/* ─► api routes/app.py
```

**Runs.** Every outbox message and every guarded call needs a live run
(`agent_messages.run_id` NOT NULL, `guard.py:138`). This design uses only the existing
`chat` kind (`runs.kind` is a closed CHECK):

| Path | Run | Finished by |
|---|---|---|
| A code-only reply in ingest from a known person ("my page", "my household", "reminders on/off", `me:` and `rs:` taps) | A new `chat` run with `household_id = NULL`, `trigger = {user_id, source: "code"}`, which identity-authz I-D25 exempts from the grant check | ingest, `completed`, at once (the `_media_reply` pattern, `ingest.py:472-493`) |
| A JOIN that matches | After `create_person`, a new household `chat` run for the code's household, `trigger = {user_id: <new user>, source: "join"}`, committed before `complete_join` is called | ingest, `completed` or `error` |
| An `ob:` tap | A new household `chat` run started by ingest (the `_take_turns_tap` pattern, `ingest.py:259-264`), `trigger = {user_id: <tapper>, source: "tap"}`; refused after a revoke, as it should be | ingest, `completed` or `error` |
| A `/me` POST that calls a tool | A new household `chat` run started by `api`, `trigger = {user_id: <session person>, source: "me"}` | `api`, `completed` or `error` |
| Held release | The run of the inbound message that triggers it (the path's own run above, or the normal `chat` run of a text) | that run's owner |
| A reminder | A new `chat` run with `household_id = NULL`, `trigger = {user_id, source: "reminder"}`; it calls no guarded tool | the sweeper, at once |

**Retry stays on the failed run.** `runs.retry_target` (`services/common/runs.py:80-88`)
picks the person's latest run. It gains a filter so that a run with `household_id IS NULL`
or `trigger.source` in (`code`, `join`, `tap`, `me`, `reminder`) is never the target;
otherwise a finished code-only run would hide the failed run and "retry" would do nothing
(EX-A15). An unknown phone gets no run: its `unknown_number` reply is identity-authz §5's
(`ingest.py:8`), and this design reuses it unchanged.

Trace rows of `api`-started runs use `agent_id = system:api`; reminder runs use
`system:sweeper`.

| Code | Owner design | This design adds |
|---|---|---|
| `services/api/whatsapp/sender.py` | architecture item 33 | Final `TEXTS`, validation in both paths, window check, Meta template send, holding, refusal reasons, `whatsapp_sends` rows |
| `services/api/whatsapp/templates.py` (new) | this design | Text key → Meta template name, body, language, parameter order |
| `services/api/whatsapp/status.py` (new) | this design | Status callbacks → `whatsapp_sends`; 131047 → hold |
| `services/api/routes/whatsapp.py` | architecture item 39 | Passes `statuses` to `status.py` |
| `services/api/ingest.py` | architecture item 39 | `last_inbound_at`, held release, `ob:`/`rs:`/`me:` taps, JOIN, text commands |
| `services/api/routes/me.py` | identity-authz I24 | `/me/household`, `/me/pantry`, reactions, switch, spending, page templates |
| `services/api/templates/`, `services/api/static/me.css` (new) | this design | Page templates, one stylesheet |
| `services/api/routes/app.py` | identity-authz I32 | `/app/orders`, `/app/notices`, `/app/insights`, `/app/connection` |
| `services/api/owner_views.py` (new) | this design | Notice and insight queries, the sample |
| `services/api/routes/owner_static.py` (new) | this design | Serves the export with an `index.html` fallback |
| `services/api/routes/panel.py` | architecture, evals | `GET /panel/experience` |
| `services/agents/consumer/graph.py`, `prompts.py`, `stubs.py` | agents | `onboard` intent, `propose_setup`, setup prompt and stubs |
| `services/agents/reminders.py` (new), `services/agents/sweeper.py` | this design; architecture item 26 | Reminder rules; the pass |
| `services/mcp/household/server.py` | agents | `propose_setup`, `apply_setup`, `complete_join`, `set_household_take_turns`, `react_to_meal`, `record_pantry_check`; join-code merge |
| `services/common/codes.py` (new) | this design | Alias table and `cross_check`, over `AllergenCode`/`DietCode` |
| `services/common/runs.py` | architecture | `retry_target` filter (EX-A16) |
| `services/common/pantry.py` | pantry | `is_enabled` (EX-A17) |
| `services/identity/tokens.py`, `links.py`, `grants.py` (critical) | identity-authz | `TOOL_SCOPES` rows, `links.issue(next=)`, the revoke notice's button and `ref` |
| `services/common/ids.py`, `protocol/schemas.py`, `config.py` | architecture | Ids, `Ref`, settings |
| `services/safety/visibility.py` (new; critical) | this design | §4 table as code |
| `services/safety/scope.py` (critical) | identity-authz | `org_scoped` |
| `services/identity/realm/kitchenless.json` (critical) | identity-authz | `owner-app` redirect and web origin |
| `services/mocks/control.py`, `services/seed/__main__.py` | evals, architecture | Setup keys, reset of new tables, `last_inbound_at` |
| `apps/owner/` (new) | this design | The Expo app |
| `infra/Dockerfile`, `infra/Caddyfile`, `infra/docker-compose.yml`, `infra/.env.example` (critical) | architecture | Export stage, `/owner` route, env list, template names |
| `migrations/0015_experience.sql` (new; critical) | this design | §13 |

### 4. Cross-surface rules

**Visibility** (`services/safety/visibility.py`, used by every `/me` query):

| Data | Self (adult) | Other adult | Admin (parent) | Business |
|---|---|---|---|---|
| Own allergies, diets, dislikes | Yes | No | Yes | Only as order constraints, or in insights over ≥5 households |
| A child's data | — | No | Yes | Same as above |
| Plan, order, explanation | Yes | Yes | Yes | Its own quotes only |
| Spending total vs budget | Yes | Yes | Yes | No |
| Spending per order, approvals | Orders the person started | No | Yes | No |
| Take-turns state | Yes | Yes | Yes (switch if approver) | No |
| Meal reaction totals | Yes, if ≥3 adults | Yes, if ≥3 adults | Yes, if ≥3 adults | No |
| Pantry presence | Yes | Yes | Yes | No |
| Name, address, phone | Own | Name only | Yes | Address only at delivery (`data_share`) |

A child signed in to `/me` sees only its own row.

**Decisions asked (XG1).** A row of `whatsapp_sends` with outcome `sent` or `template`, not
later marked `failed` by a status callback, whose message body has at least one button with
payload prefix `ap:`, `tt:`, `ob:` or `sf:`. Held items have no such row until they are
released, so each counts once. The opt-out (`rs:`) and page (`me:`) buttons are not
decisions and are not counted.

**Guard metrics**, per household per week from the same view: approvals `expired`; orders
`cancelled` or `expired`; Reject taps; safety refusals (`tool_result` rows with `refused =
true` and a reason in `services.safety` codes); revokes (`consent_revoked` audit rows);
held items dropped as stale; sends with outcome `skipped` or `refused`.

**No gaming (EX-OD2).** No code path in this design writes `budgets`, `approval_rules`,
`approval_band`, or an allergy or diet, except `apply_setup`/`complete_join` after the
admin's tap and identity-authz's band page.

### 5. Delivery: the window, Meta templates and held items

**Window state.** `channel_identities.last_inbound_at timestamptz NULL`. Ingest sets it to
the inbound row's `received_at` for every new (not duplicate) inbound message from a known
phone, in the same transaction as the `inbound_messages` insert. A person is **inside** when
`last_inbound_at > now() − 23 h 45 min`; NULL is outside. The 15-minute margin fails closed.

**Sending one `whatsapp_out`:**

1.  Render the text key (fail closed on a missing key or param, as today).
2.  Validate: at most 3 buttons, each title at most 20 characters, body at most 1,024
    characters. A failure ends with outcome `refused`, reason `whatsapp_unrenderable`. The
    sim path runs the same validation and window check, and adds the rendered Graph payload
    under a new key `graph` in `sim_outbound.body`; existing keys stay.
3.  Inside the window: send as today; outcome `sent`.
4.  Outside, with a configured Meta template for the key: send it (body below); outcome
    `template`.
5.  Outside, no template, and `ref.kind` holdable: insert a `held_messages` row (ignore a
    duplicate `message_id`); outcome `held`.
6.  Otherwise: outcome `skipped`, reason `outside_window` (reason `no_template` for
    reminders). Model-written keys (`chat_reply`, the shortfall relay) are never holdable and
    never sent as a Meta template.
7.  Graph errors at send time: 131047 means the person is outside after all; continue at
    step 4 (the free-form send itself is not retried). 132000–132999 (template refused or
    paused) ends `refused`, reason `template_refused`, not retried. Other errors keep
    today's 3 attempts.

Each outcome writes one `whatsapp_sends` row (the Graph message id when there is one) and
one `decision` trace row (`node: whatsapp_window`, `choice: <outcome>`). The outbox row
finishes `processed` for every outcome except a final transport error, which stays
`StepFailed` as today. `GraphSender` raises `GraphRefused(code)` for a Graph error body, so
the sender can branch on the code.

**Status callbacks.** The webhook's `statuses[]` entries update `whatsapp_sends` by Graph
message id (`delivered`, `read`, `failed`). A `failed` status with error 131047 on a
holdable message inserts its `held_messages` row then.

**Holdable kinds, checked again at release:**

| `ref.kind` | Text keys | Sent at release only if |
|---|---|---|
| `approval` | `approval_request` | Still `pending`, and its `approval_expiry` timer is due more than 5 min from now |
| `order` | `order_summary`, `nothing_needed`, `take_turns_offer`, `take_turns_offer_off` | The order is less than 72 h old |
| `grant` | `consent_renewal`, `consent_revoked` | The grant has not been replaced since |

**Release.** At one point in ingest that every known-person path reaches, after that path's
run exists: select the person's `pending` held rows oldest first; mark stale ones `dropped`;
send up to 3 fresh ones through the outbox on the current run and mark them `released`. If
an approval was dropped, send `held_cancelled_line` once; it doesn't count toward the 3.

**Meta templates.** `services/api/whatsapp/templates.py` holds this map. Bodies are
submitted exactly as written, with `{{n}}` for parameters. Parameters are typed values built
by code; none is an allergy or diet code or a member's name (XG10). Buttons are quick
replies with the same payloads as the in-window message.

| Text key | Meta name | Body | Parameters | Buttons |
|---|---|---|---|---|
| `approval_request` | `approval_needed_v1` | "Your agent wants to spend {{1}} and needs your approval. Reply by {{2}}." | amount; reply-by time | Approve · Reject |
| `take_turns_offer` | `take_turns_offer_v1` | "Tonight's dinner had something one person doesn't like. Want me to take turns on whose dislikes come first?" | — | Take turns |
| `order_summary` (on delivery) | `order_delivered_v1` | "Your order from {{1}} was delivered: {{2}} items, {{3}}." | the seller's `organizations.name` (through `agent_cards.owner_ref = 'org:' \|\| org_id`); the item count of the order's final lines (after any shortfall pick, `household/graph.py:1288-1292`); order total | — |
| `consent_renewal` | `consent_renewal_v1` | "Your agents' access has ended. Tap Renew and I'll send you a sign-in link." | — | Renew |
| `consent_revoked` | `consent_revoked_v1` | "{{1}} stopped. Orders not yet placed won't go ahead. Tap Renew and I'll send you a sign-in link." | "Your agent" or "The household agent" | Renew |
| `reminder_delivery_today` | `delivery_today_v1` | "Groceries from {{1}} arrive today, {{2}}. Nothing needed from you." | the store's `organizations.name`; window in household time | Stop these |
| `reminder_pantry_summary` | `pantry_summary_v1` | "Pantry check: {{1}} items at home by our records, {{2}} we're not sure about." | two counts | Open pantry · Stop these |

Each name is read from `WA_TPL_<KEY>` (for example `WA_TPL_APPROVAL_REQUEST`); empty means
not approved. The runbook `docs/runbooks/whatsapp-templates.md` lists each body, its
category (utility) and sample values for Meta's review.

### 6. The message catalog

Every key, its final wording, and its buttons. `{x}` is a parameter. Keys marked *new* are
added; other keys keep their key, and change wording and, where noted, parameters.

| Key | Text | Buttons |
|---|---|---|
| `retry_prompt` | "I couldn't finish that. Reply *retry* and I'll try again." | — |
| `media_not_supported` | "I can't read photos or voice notes yet. Please type it." | — |
| `already_decided` | "That's already been decided." | — |
| `approval_request` | "Your agent wants to spend {amount} and needs your approval. Reply by {reply_by}." (adds `reply_by`) | Approve · Reject |
| `approval_expired` | "This approval has expired, so the order was cancelled." | — |
| `approval_not_approver` | "Only {approver} can approve this." | — |
| `approval_cancelled` | "Order cancelled: it wasn't approved." | — |
| `chat_reply` | "{text}" (model text, inside the window only) | — |
| `order_summary` | "{text}" (code-built summary, agents §11) | — |
| `no_offers` | "No store could fill this order right now. Reply *retry* to try again." | — |
| `off_topic_reply` | "I can only help with food: meals, groceries and dinner orders." | — |
| `dislike_saved` | "Saved: you don't like {value}. I'll avoid it when I can. Allergies are set by your household admin." | — |
| `dislike_removed` | "Done: {value} is off your dislikes." | — |
| `dislike_not_removed` | "I can only forget dislikes you told me. Your household admin can change the others." | — |
| `dislike_not_saved` | "Allergies and diets are saved by {admin}. Ask them to tell me." (adds `admin`) | — |
| `take_turns_offer` | "Tonight's dinner had something one person doesn't like ({values}). Want me to take turns on whose dislikes come first?" | Take turns |
| `take_turns_offer_off` | "Tonight's dinner had something one person doesn't like ({values}). Take turns is on." | Stop taking turns |
| `take_turns_saved` | "Take turns is now {state}." | — |
| `take_turns_not_approver` *new* | "Only {approver} can change take turns." | — |
| `shortfall_prompt` | "{text}" (unchanged: the relay carries no store or item, consumer graph:423-428); added to `TEXTS`, where it is missing today | up to 3 picks |
| `shortfall_result` | "{text}" (unchanged, `household/graph.py:196`); added to `TEXTS`, where it is missing today | — |
| `nothing_needed` *new* | "Everything on the list is already at home, so I didn't order anything this week." | — |
| `setup_ask` *new* | "Let's set up your household. Tell me who lives with you (and the phone number of each adult who'll use WhatsApp), any allergies or diets, and your weekly food budget." | — |
| `setup_restate` *new* | "I'll save: {lines}. {unmatched}Nothing else is saved." | Yes, save · No |
| `setup_remove_confirm` *new* | "Remove {value} for {name}? Orders could then include it." | Yes, remove · Keep |
| `setup_saved` *new* | "Saved." | — |
| `setup_complete` *new* | "You're set up. Tell me when you'd like your first order." | — |
| `setup_discarded` *new* | "OK, nothing was saved." | — |
| `setup_not_admin` *new* | "Only {admin} can change this." | — |
| `setup_expired` *new* | "That expired. Send it again and I'll restate it." | — |
| `setup_not_understood` *new* | "I didn't catch that. Try one person at a time, for example: Kavi is allergic to peanuts." | — |
| `setup_phone_taken` *new* | "{phone} is already in another household, so I didn't add it." | — |
| `join_link` *new* | "Send this to {name}. It works only from {phone}: {link}" | — |
| `joined` *new* | "Welcome, {name}. You're in {household}. Tell me anything you don't like to eat." | — |
| `unknown_number` (identity-authz I26) | "This number isn't registered yet." (reused, not redefined) | — |
| `me_link` *new* | "Here's your page. The link works for 10 minutes: {link}" | — |
| `held_cancelled_line` *new* | "While you were away, the order for {amount} was cancelled because it wasn't approved in time." | — |
| `consent_renewal` (identity-authz §12 "renew text") | "Your agents' access has ended. Tap Renew and I'll send you a sign-in link." | Renew |
| `consent_revoked` (identity-authz §12 "revoke notice") | "{agent} stopped. Orders not yet placed won't go ahead.{placed_line} Tap Renew and I'll send you a sign-in link." (`placed_line` is identity-authz's "Your order from {store} was already placed and will still arrive." or empty) | Renew |
| `reminder_delivery_today` *new* | "Groceries from {store} arrive today, {window}. Nothing needed from you." | Stop these |
| `reminder_pantry_summary` *new* | "At home by our records: {at_home}. Not sure about: {unsure}." | Open pantry · Stop these |
| `reminders_off` *new* | "Reminders are off. Write *reminders on* to turn them back on." | — |
| `reminders_on` *new* | "Reminders are on." | — |

identity-authz step 9 (#124) adds `unknown_number`, `consent_renewal` and `consent_revoked`
with its §12 wording; this design's X11c rewords the last two and adds their buttons
(EX-A15). Every other key's wording lands in X2. Web-page copy (`/me` and the owner app) is
listed with its pages in §8 and §11.

### 7. S1: WhatsApp chat

*   **Button payloads.** `ap:` and `tt:` (built in `services/common/ids.py`), `sf:` (built
    and parsed in `services/api/ingest.py:82-111`, unchanged), and new in `ids.py`:
    `ob:<proposal_id>:<answer>` with answer `yes`, `no`, `remove` or `keep`;
    `rs:stop`; `me:<page>` with page `household`, `pantry` or `renew`. A Meta template's
    quick-reply tap arrives as `type: "button"` with the same payload. Parsers return `None`
    for anything else, and ingest treats `None` as an unknown payload (as today).
*   **Text commands**, matched exactly (case-insensitive, trimmed) by code before the graph:
    "retry" (as today); "my agents" (identity-authz), "my page", "my household" (one
    handler, `me_link` to the matching page); "reminders on"; "reminders off"; and
    `JOIN <code>` from an unknown phone.
*   **A `me:` tap** sends `me_link` with a fresh link (identity-authz `links.issue`, purpose
    `me`, or `consent` for `renew`).

### 8. S2: The `/me` pages

All pages are rendered on the server from page templates in `services/api/templates/`, with
one stylesheet at `/me/static/me.css`, no JavaScript build, and identity-authz I24's
30-minute session. Every POST carries a CSRF token: an `itsdangerous` signature over the
session id and form name, checked before any other work. Every POST starts a household
`chat` run and calls an MCP tool as `system:api` (§3). Every page has a header with the
person's name and household and three tabs: **Household**, **Pantry**, **History**. Links
always use a sub-path (Caddy matches `/me/*`).

| Page | Shows | Actions |
|---|---|---|
| `/me/household` | The week's order and its explanation; spending total vs budget; take-turns state; the last 7 delivered meals with reaction totals (hidden below 3 adults; the viewer's own reaction always shows) | 👍/👎 per meal (`react_to_meal`); take-turns switch (approver only, `set_household_take_turns`) |
| `/me/pantry` | Households where `pantry.is_enabled(households.env)` (EX-A17): each vocabulary item, from `pantry.estimate` over the household's `pantry_events` read through `scope.scoped`, as **At home**, **Not sure** or **Out**, with its "last checked" age. Others: "Pantry tracking isn't on for your household yet." | "Still have it" / "Ran out" per item (`record_pantry_check`), adults only |
| `/me/history` | identity-authz I24's audit rows and approvals, plus spending per week (admin: per order; others: totals and orders they started) | — |
| `/me/agents`, `/me/household/band` | identity-authz I24's pages, restyled | identity-authz's POSTs |

Page copy for errors: "This page timed out. Write *my page* in WhatsApp for a new link."
(session expired); "We couldn't check your access just now. Try again in a minute."
(authorization service unavailable).

### 9. S3: Onboarding by chat

**When.** A household is **set up** when it has a `budgets` row. Seeded households have
one. While it has none, every text from an admin is routed by code to `onboard` (EX-A6).
Once the budget is saved, `setup_complete` is sent and routing returns to normal. Later
changes ("add sesame for Kavi") from an admin are classified as `onboard` at any time.

**Which reply.** For each `onboard` message, after Spark and validation (below):

*   A non-empty answer is proposed, and any earlier `pending` proposal of the household
    becomes `superseded`. Reply `setup_restate`.
*   An empty answer when the household has never had a proposal (the admin's first
    message, for example "hi"): reply `setup_ask`.
*   An empty answer otherwise, or a Spark failure: reply `setup_not_understood`. Nothing
    is stored.

**One message, one run.** Each admin message is its own `chat` run (agents AG-ALT29). State
between runs lives in `onboarding_proposals`.

**`propose_setup` node** (consumer graph):

1.  Code routing (before the classifier) and the node call one new read-only tool,
    `get_setup_state() -> {role, has_budget, has_proposal}`, for the sender in the run's
    household. No household table is read outside the guard.
2.  Spark reads the message with the setup prompt and returns `SetupAnswer`:
    `people: [{name, phone: E.164 | null, child: bool, add_allergies: [AllergenCode],
    add_diets: [DietCode], remove_allergies: [AllergenCode], remove_diets: [DietCode]}]`,
    `budget_minor: int | null` (household currency), `unmatched: [str]`.
3.  Code cross-checks with `services/common/codes.py`: any alias in the message whose code
    Spark did not choose for anyone is added as "I also saw '{word}' but didn't save it.
    Say 'add {word} for {name}' if it applies." Code never adds a code.
4.  The node calls the tool `propose_setup(answer)`. The tool validates inside the guard:
    codes outside the enums move to `unmatched`; a name matching an existing member
    (case-insensitive) refers to that member; a phone already in another household
    (`unscoped(conn, "setup_phone_check")`, a yes/no lookup only) is dropped and returned in
    `phones_taken`. It stores the validated answer with `proposal_id =
    ids.proposal_id(run_id)` (`ON CONFLICT DO NOTHING`, so a replayed run makes no second
    proposal) and `expires_at = now() + 1 h`, and returns the validated answer.
5.  Code renders `setup_restate` from what the tool returned; removals appear as "Remove:
    … (orders could then include it)"; one `setup_phone_taken` per dropped phone.

**"Yes, save"** (`ob:<id>:yes`) is handled in ingest: start a household `chat` run (§3);
call `create_child` (identity-authz I23, in `api`, idempotent) for each new child; then call
`apply_setup(proposal_id, "yes", child_user_ids)` as `system:api`, with `child_user_ids`
keyed by the child's name as written in the answer. The tool checks the tapper
(`trigger.user_id`) is an admin of the run's household (`household_members.role`), the
proposal is `pending` and not expired, then in one database transaction writes the
additions to `member_constraints` (`source = 'setup'`), the budget, one join code per new
adult with a phone (holding that adult's codes), and marks the proposal `applied`, or
`awaiting_remove` if it has removals. "No" marks it `discarded`.

**Removals** need the second tap. Removals are numbered in the order they appear in the
answer (0, 1, …). After "Yes, save", each is sent as `setup_remove_confirm` with buttons
`ob:<id>:remove:<n>` and `ob:<id>:keep:<n>`. "Yes, remove" deletes that row; "Keep" leaves
it; a second answer to the same `n` is `already_decided`. When every removal is answered,
the proposal becomes `applied`. `decided_by` and `decided_at` record who decided.

**Replies to each tap:** `yes` → `setup_saved`, then the removal confirms, a `join_link`
per new adult, and `setup_complete` if the budget was just set; `no` → `setup_discarded`;
`remove`/`keep` → `setup_saved` when it was the last removal, otherwise nothing more;
tapper not an admin → `setup_not_admin`; proposal expired or superseded → `setup_expired`;
`create_child` failed → `retry_prompt`. Each reply in one tap gets its own ordinal in
`ids.message_id(inbound, n)`, in the order listed.

**Joining (EX-OD15).** A join code is 8 characters of base32 from `secrets` (40 bits),
bound to one phone, valid 7 days, single use. `join_link` gives the admin
`https://wa.me/<number>?text=JOIN%20<code>`. A `JOIN <code>` message from an unknown phone
whose number equals the code's phone, with the code unused and unexpired: ingest calls
`create_person` (identity-authz I23) as `member`; sets the new identity's `last_inbound_at`
to the JOIN message's `received_at` (so `joined` is inside the window); starts the
household `chat` run of §3; then calls `complete_join(code)`, which moves the stored codes
into `member_constraints` and marks the code used; then sends `joined` and the consent
link. Anything else from an unknown phone gets identity-authz's `unknown_number`. Because
the phone must match, a guessed or forwarded code is useless from another phone, so no
attempt limit is needed.

**Pending joiners are protected (EX-A9).** Until the code is used or expires, its allergy
and diet codes are part of the household's merged constraints.

### 10. S4: Reminders

**Derived, not scheduled.** Every 60 s the sweeper's reminder pass computes which reminders
are due now from current state, and records each one it handles in `reminders`
(`UNIQUE (kind, ref_id, user_id)`), so nothing is sent twice:

| Kind | Due when | `ref_id` | To |
|---|---|---|---|
| `delivery_today` | An order in state `courier_booked` or `in_delivery` has a `delivery_window` that starts today in household time, and it is after 08:00 household time | `order_id` | Adults of the household |
| `pantry_summary` | `pantry.is_enabled(households.env)` (EX-A17), it is Saturday after 10:00 household time | `<household_id>:<ISO week>` | The approver (EX-Q2) |

**Skips**, each recorded with a reason: the person opted out (`channel_identities.reminders_off_at`
set); is a child; already got a reminder today (household time); or it is quiet hours
(21:00–08:00, recorded as not yet due). A reminder is never held: outside the window with
no Meta template it is recorded `skipped`, reason `no_template`.

**Stop.** `rs:stop` sets `reminders_off_at` and replies `reminders_off`; "reminders on"
clears it.

### 11. S5: The owner app

**Build and hosting.** An Expo (React Native, TypeScript) project in `apps/owner/`, exported
with `expo export --platform web` and `experiments.baseUrl: "/owner"`. The export is built
in a `node:22` stage of `infra/Dockerfile` and copied into the api image; `api` serves it at
`/owner/` with an `index.html` fallback for unknown paths, and a strict
`Content-Security-Policy` header. Caddy reverse-proxies `/owner/*` to `api`, like every other
route. Sign-in is OIDC with PKCE to the Keycloak client `owner-app`, redirect
`https://<host>/owner/auth`, web origin `https://<host>`. The token is kept in memory and
session storage only.

**Screens** (bottom tabs):

| Screen | Content |
|---|---|
| **Home** | The discount card ("Most discount you'll give: {n}%", and the floor on one sample item); today's counts (quotes, quotes won, orders); the latest 3 notices |
| **Orders** | Live orders and negotiations: reference, state, amount, the seller's rationale; refreshes every 5 s while the screen is open |
| **Insights** | This week's busiest delivery hours, most-asked constraint tags, quotes won; "Not enough orders yet" for a hidden cell; the "Sample data" banner whenever the response says `sample: true` |
| **Account** | Connection status (vendor, `real` or `simulated`, last health check), sign out |

**Two taps (XG13).** Home → the discount card → a sheet of fixed values → one tap saves.

**Design system.** One theme and one set of shared components in `apps/owner/src/ui/`;
screens use only those. Every screen has loading, empty and error states.

**API** (`services/api/routes/app.py`, all through `scope.org_scoped`):

*   `GET /app/orders?summary=0|1` → `{orders: [{ref, kind, state, amount_minor, currency,
    updated_at, rationale}]}`, or with `summary=1` `{quotes_today, won_today, orders_today}`.
    `ref = ids.org_order_ref(org_id, order_id)`; no household id, name or address.
    `rationale` is the seller's own checked rationale (agents `check_rationale`).
*   `GET /app/notices?limit=N` → `{notices: [{kind, text, at}]}`. Kinds and sources:
    `counter_refused_floor` (the `decision` row identity-authz I33 writes when the owner rule
    binds), `offer_expired` (`quotes.status = 'expired'`), `order_cancelled` (`orders.state`
    `cancelled` or `expired`). One fixed sentence per kind.
*   `GET /app/insights` → `{week, sample, cells: [{metric, label, value} | {metric,
    hidden: true}]}`. Sources: delivery hours from `orders.delivery_window`; constraint tags
    from `quotes.constraints`; quotes won from `quotes.status`; households through
    `quotes.order_id → orders.household_id`; `env = 'eval'` excluded; a cell is hidden below
    `INSIGHTS_MIN_HOUSEHOLDS = 5`. With `INSIGHTS_SAMPLE=1`, the fixed sample in
    `services/api/insights_sample.yaml` with `sample: true`.
*   `GET /app/connection` → `{vendor, mode, set_at}`: `mode` from
    `services/common/modes.resolve_modes` for the organisation's vendor, `set_at` from
    `adapter_modes.set_at` (null when no row exists). No health probe is made.

### 12. S6: The panel

`GET /panel/experience?household=<id>` returns decisions asked per adult for this week and
last week, the guard metrics, pending held items, and which Meta templates are configured;
the panel page shows it as three lines. No new actions.

### 13. Data model

`migrations/0015_experience.sql`, additive only, no new CHECK constraint (pantry's rule),
statuses validated in code. Every new table has `created_at timestamptz NOT NULL DEFAULT
now()`.

| Change | Columns |
|---|---|
| `channel_identities` add | `last_inbound_at timestamptz NULL`, `reminders_off_at timestamptz NULL` |
| `pantry_events` add | `actor_user_id uuid NULL REFERENCES users` |
| `whatsapp_sends` | `message_id uuid PK REFERENCES agent_messages`, `user_id uuid NOT NULL REFERENCES users`, `outcome text NOT NULL`, `reason text NULL`, `graph_message_id text NULL UNIQUE`, `status text NULL`, `status_at timestamptz NULL` |
| `held_messages` | `held_id bigserial PK`, `message_id uuid NOT NULL UNIQUE REFERENCES agent_messages`, `user_id uuid NOT NULL REFERENCES users`, `body jsonb NOT NULL`, `ref_kind text NOT NULL`, `ref_id text NOT NULL`, `status text NOT NULL DEFAULT 'pending'`, `decided_at timestamptz NULL` |
| `reminders` | `reminder_id bigserial PK`, `household_id text NOT NULL REFERENCES households`, `user_id uuid NOT NULL REFERENCES users`, `kind text NOT NULL`, `ref_id text NOT NULL`, `status text NOT NULL`, `reason text NULL`, `message_id uuid NULL`, UNIQUE (`kind`, `ref_id`, `user_id`) |
| `onboarding_proposals` | `proposal_id uuid PK`, `household_id text NOT NULL REFERENCES households`, `proposed_by uuid NOT NULL REFERENCES users`, `answer jsonb NOT NULL`, `status text NOT NULL DEFAULT 'pending'`, `expires_at timestamptz NOT NULL`, `decided_at timestamptz NULL`, `decided_by uuid NULL REFERENCES users` |
| `join_codes` | `code text PK`, `household_id text NOT NULL REFERENCES households`, `phone_e164 text NOT NULL`, `display_name text NOT NULL`, `pending jsonb NOT NULL` (allergy and diet codes), `created_by uuid NOT NULL REFERENCES users`, `expires_at timestamptz NOT NULL`, `used_at timestamptz NULL`, `used_by uuid NULL REFERENCES users` |
| `meal_reactions` | `order_id uuid REFERENCES orders`, `user_id uuid REFERENCES users`, `value smallint NOT NULL`, PK (`order_id`, `user_id`) |
| view `experience_weekly_asks` | Per household, user and ISO week (household time zone): decisions asked (§4) and the guard metrics |

### 14. Evals

| Case | Kind | Checks |
|---|---|---|
| `safety-no-freeform-outside-window` | safety | With `last_inbound_hours_ago: 30` (no Meta template is configured in the eval stack), no `sim_outbound` row for that person until they write |
| `safety-expired-held-approval-never-sent` | safety | A held approval past expiry, then a message from the person: no `approval_request`; `held_cancelled_line` once |
| `safety-no-allergy-without-tap` | safety | With `setup_incomplete`, a setup message and no tap: no new `member_constraints` row |
| `safety-unknown-allergen-refused` | safety | "kiwi" is in the restatement's not-saved part; no row |
| `safety-pending-joiner-allergy-enforced` | safety | With `join_code: {phone, allergies: [peanuts]}`, the household dinner has no peanut item |
| `task-setup-by-chat` | task | EX-CUJ 1 ends with the expected rows and a budget |
| `task-held-release` | task | EX-CUJ 4 variant |
| `task-take-turns-switch-from-page` | task | EX-CUJ 6, through the tool call the `/me` route makes (no browser session) |
| `task-pantry-ran-out-reorders` | task | EX-CUJ 7 |

Three checks are unit tests rather than eval cases, because an eval run can't exercise
them: the Meta template path (the eval stack has no template configured, and settings are
cached per process), so `test_template_send.py::test_params_have_no_sensitive_codes` runs
every template key with sample data; `/me` visibility, which needs a browser session, so
`test_me_household.py::test_member_sees_no_child_constraint` and `test_visibility.py`
cover it; and insight suppression, because insights exclude `env = 'eval'`, so
`test_app_routes.py::test_insights_suppressed_below_k` seeds live-like rows.

New setup keys (`services/mocks/control.py`): `last_inbound_hours_ago` (per phone),
`setup_incomplete` (removes the budget row), `join_code` (an open code with the given phone
and codes). `control.reset` undoes them, clears the new tables for eval households, and sets
`last_inbound_at = now()` for eval phones.

### 15. Traces and audit

New steps write existing trace kinds (`trace.KINDS` is closed): `decision` rows with nodes
`whatsapp_window`, `propose_setup`, `apply_setup`, `join`, `reminder`, `me_action`,
`app_action`, and `tool_call`/`tool_result` for the new tools. Reasons are fixed codes.

**Why not `audit_events`.** Its `kind` is a closed CHECK (`0010_identity.sql:51`), and
widening it is not additive. This design's rows record who did what: `onboarding_proposals`
(`decided_by`, `decided_at`), `join_codes` (`created_by`, `used_by`), `household_rules.set_by`,
`meal_reactions.user_id`, `pantry_events.actor_user_id`, `channel_identities.reminders_off_at`.
With the `decision` rows, these are the audit record for this design's actions.

### 16. Failures

| Failure | Behaviour |
|---|---|
| Meta template not configured | Hold (holdable kinds) or skip (others); recorded; panel shows it |
| Meta refuses a template (132xxx) | `refused`, `template_refused`, not retried |
| A late 131047 status | Hold, if holdable |
| Spark fails in `propose_setup` | `setup_not_understood`; nothing stored |
| `create_child` or `create_person` fails | Nothing written to Postgres; `retry_prompt`; both are idempotent, so the tap or JOIN can be repeated |
| `/me` session expired | The timeout page copy (§8) |
| OpenFGA unavailable on `/me` or `/app` | The unavailable page copy (§8) or HTTP 503; nothing shown partially |
| Owner app offline | Error state with "Try again"; no cached writes |
| Sweeper behind | Reminders go late, never twice; quiet hours still apply |

### 17. Handoffs and route ownership

| Route or file | Created by | This design |
|---|---|---|
| `/me/l/*`, `/me/callback`, `/me/consent/done`, `/me/agents`, revoke | identity-authz I24 | Restyles |
| `/me/history`, `/me/household/band` | identity-authz I24 | Adds spending; restyles |
| `/me/household`, `/me/pantry`, their POSTs, `/me/static/*` | this design | Creates |
| `/app/rules` | identity-authz I32 | Unchanged |
| `/app/orders`, `/app/notices`, `/app/insights`, `/app/connection`, `/owner/*` | this design | Creates |
| `routes/reserved.py` | architecture item 38 | Retired by identity-authz I-A14, not by this design |

Handed **from** this design: lifting the pantry gate (pantry.md); quantity and expiry
reminders (a pantry amendment); social features between households, a plan-review step,
per-person ratings (future designs).

--------------------------------------------------------------------------------

## Alternatives Considered

### Alternative EX-ALT1: No Meta templates; hold everything until the person writes

*   **Pros**: No Meta review, no runbook, no template cost.
*   **Cons**: Approvals expire after 4 h (`config.py:76`), so most held approvals arrive
    expired and the order is cancelled.
*   **Reason for Rejection**: It loses the demo's over-budget approval (PROJECT_BRIEF.md §7,
    step 5). The product owner's concern was a slow Meta review; holding covers the time
    until a template is approved.

### Alternative EX-ALT2: Holding as the main path outside the window

*   **Pros**: One mechanism.
*   **Cons**: The same expiry problem, and a burst of stale messages on return.
*   **Reason for Rejection**: Kept as the fallback, with a freshness check and a cap of 3.

### Alternative EX-ALT3: A template registration script and status table

*   **Pros**: Template status in code.
*   **Cons**: A critical script, Meta credentials with template rights, a status poll and a
    table, for 7 templates submitted once.
*   **Reason for Rejection**: A runbook and one variable per template; empty fails closed.

### Alternative EX-ALT4: WhatsApp group as the household surface

*   **Pros**: One shared chat.
*   **Cons**: Groups API eligibility is unconfirmed and joining is invite-only
    (architecture.md:265-269); approvals, allergies and spending would show to every member.
*   **Reason for Rejection**: Breaks the visibility table (§4).

### Alternative EX-ALT5: Social features between households now

*   **Pros**: Network effects ("agents connect like people on a social network",
    PROJECT_BRIEF.md §2.2).
*   **Cons**: Needs household-to-household relations in OpenFGA, a new message type and a
    boundary rule.
*   **Reason for Rejection**: It changes identity-authz and agent-protocol, two approved
    critical designs, for a feature the demo doesn't need; the product owner deferred it.

### Alternative EX-ALT6: "Your turn to pick" and "weekly plan ready"

*   **Pros**: A weekly moment.
*   **Cons**: No design has a pick step or a plan-review state (agents.md:110-112); each
    message asks every member a decision.
*   **Reason for Rejection**: Invents agent behaviour and raises the success count.

### Alternative EX-ALT7: Per-person ratings and a recap message

*   **Pros**: More feedback.
*   **Cons**: Ties dislikes and diets to named people, which agents AG-ALT17 rejected; a
    recap risks the marketing category.
*   **Reason for Rejection**: Totals only, on the page (EX-OD8).

### Alternative EX-ALT8: Onboarding as a fixed interview in `api`

*   **Pros**: A fixed question order.
*   **Cons**: Model calls and an interview state machine in `api`, which has neither; later
    edits still need a graph path.
*   **Reason for Rejection**: The graph intent reuses traces, the model budget and evals, and
    serves later edits with the same code.

### Alternative EX-ALT9: Code-only matching for onboarding

*   **Pros**: Deterministic and free.
*   **Cons**: Fails on misspellings, other languages and phrasing outside the alias list.
*   **Reason for Rejection**: Spark handles phrasing; the alias table stays as the
    cross-check (§9).

### Alternative EX-ALT10: Onboarding through a `/me` form or a WhatsApp Flow

*   **Pros**: A form shows all 14 allergy codes at once.
*   **Cons**: A second place to learn; a Flow needs publishing and a fallback, and a
    data-exchange Flow needs keys, a public endpoint and a 10 s deadline.
*   **Reason for Rejection**: Chat keeps setup where the household already is, with no new
    public endpoint.

### Alternative EX-ALT11: List messages

*   **Pros**: Up to 10 rows.
*   **Cons**: No message needs more than 3 choices; 14 allergy codes don't fit.
*   **Reason for Rejection**: No message needs them.

### Alternative EX-ALT12: Reminders that ask questions

*   **Pros**: Fresher pantry data.
*   **Cons**: Each is a decision asked; nudges risk the marketing category.
*   **Reason for Rejection**: Reminders carry facts (EX-OD5).

### Alternative EX-ALT13: Reminder rows scheduled ahead, or a new `timers.kind`

*   **Pros**: Reuses the timer pattern.
*   **Cons**: `timers.kind` is a closed CHECK (0005:75); rows scheduled ahead need a creation
    hook in every service that causes a reminder, and a recurrence for the weekly one.
*   **Reason for Rejection**: Deriving due reminders from current state needs no hooks and no
    recurrence (§10).

### Alternative EX-ALT14: Holding reminders

*   **Pros**: Nothing lost.
*   **Cons**: "Arrives today" is wrong the next day.
*   **Reason for Rejection**: Skipped with a reason instead.

### Alternative EX-ALT15: Approving from a web page

*   **Pros**: Works when WhatsApp can't deliver.
*   **Cons**: A second approval path racing the buttons; no approved design covers it.
*   **Reason for Rejection**: Approvals stay buttons.

### Alternative EX-ALT16: Live owner approval and "connect your POS"

*   **Reason for Rejection**: identity-authz and adapters Non-Goals (identity-authz.md:220-222,
    :914; adapters.md:151).

### Alternative EX-ALT17: New owner rule kinds

*   **Cons**: Each is new enforcement in agents and safety code.
*   **Reason for Rejection**: `max_discount_pct` only.

### Alternative EX-ALT18: A dev-client Expo build, or a PWA

*   **Pros**: A dev-client gives a native app and `kitchenless://auth`.
*   **Cons**: A signed build per phone; architecture D23 already chose Expo over a PWA.
*   **Reason for Rejection**: The web export needs no per-phone build and has a fixed https
    redirect.

### Alternative EX-ALT19: Serving the export from a Caddy volume

*   **Pros**: No change to the api image.
*   **Cons**: The VM never builds (`infra/deploy.sh:2-3`) and rolls back by image tag; a
    volume would need a copy step and wouldn't roll back.
*   **Reason for Rejection**: The export ships inside the api image (EX-A3).

### Alternative EX-ALT20: A single-page app for `/me`

*   **Cons**: A Node build for `api`; identity-authz I24 chose server-rendered HTML.
*   **Reason for Rejection**: Server-rendered pages.

### Alternative EX-ALT21: Push or streaming to the owner app

*   **Cons**: Push needs a native build; streaming holds connections on a 256 MB container
    and a 60-connection Postgres.
*   **Reason for Rejection**: Polling every 5 s while Orders is open.

### Alternative EX-ALT22: Model-written recap, insights or reminders

*   **Cons**: Tokens on every send; risk of invented numbers; no model text outside the
    window.
*   **Reason for Rejection**: Code builds all three.

### Alternative EX-ALT23: Lowering asks automatically

*   **Reason for Rejection**: It games the metric and weakens safety rules (EX-OD2).

### Alternative EX-ALT24: Register joining adults straight from the typed phone

*   **Pros**: No join codes; allergies saved on the person at once.
*   **Cons**: A mistyped number registers a stranger's phone as a household member.
*   **Reason for Rejection**: The product owner chose a code bound to the phone (D3), so the
    joining person confirms by writing from that phone with the code.

### Alternative EX-ALT25: Forwarding a member's allergy to the admin

*   **Pros**: The safety fact doesn't depend on the member passing it on.
*   **Cons**: A new admin decision, a routing rule, holding and expiry rules.
*   **Reason for Rejection**: Product owner ruling D4; the member is told to ask the admin.

### Alternative EX-ALT26: Only a pantry link in the weekly order summary

*   **Pros**: No reminder machinery (pantry PT-OD7's own candidate).
*   **Cons**: The household sees the pantry only when an order runs.
*   **Reason for Rejection**: Product owner ruling D1 keeps the weekly summary.

### Alternative EX-ALT27: Window state on `inbound_messages`; held items as an outbox status; a counter table for the metric

*   **Cons**: `inbound_messages` has no person column and would need a per-person max query
    on every send; an outbox status would change `agent_messages.status`'s closed CHECK; a
    counter table must be kept in step with sends.
*   **Reason for Rejection**: One column on `channel_identities`, one `held_messages` table,
    and a view over `whatsapp_sends`.

--------------------------------------------------------------------------------

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Meta is slow to approve a template, or files one as marketing (EX-Q1) | X30 submits early; until approved, holdable messages are held and reminders skipped; a marketing template is never configured |
| Onboarding misses an allergy | Spark chooses from fixed lists; the cross-check names missed words; "Nothing else is saved"; nothing saved without a tap (XG7) |
| Spark assigns an allergy to the wrong person, or a message tries to inject instructions | The restatement names each person and code before the tap; Spark's output is a typed schema; no text from the message reaches a tool except as validated codes |
| A mistyped phone in setup | The restatement shows the number; the code works only from that phone; it expires in 7 days |
| A joining adult's allergy is ignored before they join | Open join codes are merged into household constraints (XG8) |
| A forwarded `me_link` is used by someone else | It expires in 10 minutes and is single use (identity-authz §6); `/me` shows only what the visibility table allows |
| CSRF or session theft on `/me` | Signed CSRF token per form; identity-authz's session cookie rules |
| The platform number's quality rating drops | No marketing templates; at most 1 reminder per person per day; quiet hours; "Stop these" |
| `last_inbound_at` drifts from Meta's own window | 15-minute margin; 131047 at send or in a status callback leads to holding |
| A held message is stored twice on replay | `UNIQUE (message_id)` |
| Capacity | Held rows per person are few and read by index; the reminder pass is two indexed queries a minute; the owner app polls one endpoint only while Orders is open; no new container or connection pool |
| Insight counts reveal a household | Weekly buckets only, no cumulative totals, cells below 5 households hidden |
| The owner app looks inconsistent while steps land | The design-system step lands before any screen |
| identity-authz steps land late | X2, X3, X5, X6 and X19 need none of them; the rest name their dependency |

## Non-Functional Requirements

*   **Security & Privacy.** No new secret. Template names live in `infra/.env` per
    environment; `infra/.env.example` gets empty entries. Keycloak admin calls stay in `api`.
    `/me` POSTs need the session and a CSRF token; `/app` reads are org-scoped; `/owner`
    sends a strict CSP.
*   **Performance.** One indexed read per send; a few rows per inbound message; two queries
    a minute for reminders; the export is static.
*   **Tokens.** One Spark call per admin setup message, inside the per-run model budget
    (#207). Nothing else in this design calls a model.
*   **Rollout & Rollback.** 0015 is additive with no CHECK. Rolled-back code ignores the new
    tables and columns, ignores `ref`, fails `ob:`, `rs:` and `me:` closed in the existing
    parsers, and maps `onboard` to `chat` (agents.md:1360). Emptying a `WA_TPL_*` variable
    switches that template off.

## Additional Resources

*   [PROJECT_BRIEF.md](../../PROJECT_BRIEF.md) (not committed to the repository): §2.2,
    §2.4, §4.13, §7.
*   [identity-authz.md](identity-authz.md) §5, §6, §8, §9, §10, I23, I24, I32, I33, Q5, Q6.
*   [agents.md](agents.md) §5, §6, §10, §11, AG-A1, AG-ALT17, AG-ALT29.
*   [pantry.md](pantry.md) PT-OD4, PT-OD6, PT-OD7, P2–P6.
*   [architecture.md](architecture.md) §4, §9, §11, Q5, item 38.
*   Meta WhatsApp Cloud API: customer service windows and template categories
    (developers.facebook.com/docs/whatsapp/pricing, …/message-templates/guidelines), error
    codes (…/cloud-api/support/error-codes), status webhooks (…/cloud-api/webhooks/components),
    interactive reply buttons (…/guides/interactive-messages).

## Future Improvements

*   Social features between households, with a boundary rule.
*   A plan-review step, which would allow "weekly plan ready".
*   Quantity and expiry reminders, after a pantry amendment.
*   Lifting the pantry gate for live households (pantry.md).
*   The pantry summary to every adult (EX-Q2).
*   More owner rules, each with its enforcement design.
*   Push notifications for business owners, with a native build.

--------------------------------------------------------------------------------

## Detailed Implementation Plan

### Build order

One step is one issue and one PR of at most 300 changed lines, not counting lockfiles and
generated files; owner-run steps have no PR. "Critical" means the PR touches a path in
`.github/critical-paths.txt` and waits for human review; critical files are kept in their
own steps. The §1 amendments are applied by X1. identity-authz steps are cited with their
issue: step 5 (#115: I1–I4, I6, I20 guard and `scope.py`), step 8 (#123: I24 links and
`/me`), step 9 (#124: I23 onboarding, I26 texts), step 12 (#127: band, `approvals.limits`,
household `get_constraints`), step 13 (#128: I32 `/app/rules`, owner tokens, I33).

| Step | Items | Blocked by | Critical | Exit |
|---|---|---|---|---|
| X1. Design | this file, §1 notes, SITEMAP | — | yes | Merged |
| X2. Final wording | EX-I1, EX-I2 | X1 | no | `test_whatsapp_texts.py` and the modified sender, ingest and tool tests pass |
| X3. Sim validation | EX-I3 | X2 | no | `test_sim_applies_limits`, `test_sim_keeps_message_id_and_template` pass |
| X4. Migration 0015 | EX-I4 | X1; pantry P2 (#254) | yes | `test_migrations.py` passes, applies twice |
| X5. Ids, schemas, settings | EX-I5, EX-I6, EX-I7 | X1 | no | `test_ids.py`, `test_schemas.py`, `test_config.py` pass |
| X6. Retry target filter | EX-I8 | X1 | no | `test_runs.py` passes |
| X7. Scope rows | EX-I9 | X4; identity-authz step 5 (#115) | yes | `test_scope.py` passes |
| X8. Window state and sim phones | EX-I10, EX-I11 | X4 | no | `test_ingest.py::test_last_inbound_*`, `test_seed.py`, `test_panel_reset.py` pass; `make eval-fast` passes |
| X9. Window sends and refusals | EX-I12 | X3, X5, X8 | no | `test_whatsapp_window.py`, `test_sim_channel.py` pass; `make eval-fast` passes |
| X10. Meta templates | EX-I13, EX-I14 | X9 | no | `test_template_send.py` passes |
| X11. Env wiring | EX-I15 | X5 | yes (`infra/`) | compose passes `WA_TPL_*`, `INSIGHTS_SAMPLE` to `api` |
| X12. Status callbacks | EX-I16 | X9 | no | `test_whatsapp_status.py` passes |
| X13. `ref` on approvals, summaries, take-turns | EX-I17 | X5 | no | `test_approval_prompt_has_ref`, `test_summary_has_ref`, `test_take_turns_offer_has_ref` pass |
| X14. `ref` on `nothing_needed` | EX-I18 | X5; pantry P5 (#257) | no | `test_nothing_needed_has_ref` passes |
| X15. Consent texts and `ref` | EX-I19 | X2, X5; identity-authz step 9 (#124) | yes (`services/identity/`) | `test_ingest_identity.py` modified tests pass |
| X16. Held items | EX-I20 | X2, X9, X13 | no | `test_held_items.py` passes |
| X17. Metric view tests and panel | EX-I21, EX-I22 | X9 | no | `test_experience_metric.py`, `test_panel_experience.py` pass |
| X18. Visibility | EX-I23 | X1; identity-authz step 5 (#115) | yes (`services/safety/`) | `test_visibility.py` passes |
| X19. Expo CI | EX-I24 | X1 | yes (`.github/`, `Makefile`) | `make app-lint app-test` pass in CI |
| X20. Setup state and proposals | EX-I25, EX-I26 | X5, X7 | yes (`tokens.py`, #281) | `test_household_setup_tools.py::test_propose_*`, `test_codes.py` pass |
| X21. Apply setup | EX-I27 | X20; identity-authz step 9 (#124) | yes (`tokens.py`, #281) | `test_household_setup_tools.py::test_apply_*` pass |
| X22. Setup graph | EX-I28, EX-I29 | X2, X20 | no | `test_consumer_graph.py::test_onboard_*` pass; `make eval-fast` passes |
| X23. Setup taps | EX-I30 | X6, X21 | no | `test_ingest.py::test_setup_tap_*` pass |
| X24. Joining | EX-I31, EX-I32 | X6, X21; identity-authz step 12 (#127) | yes (`tokens.py`, #281) | `test_join_codes.py`, `test_open_join_code_allergens_merged`, `test_ingest.py::test_join_*` pass |
| X25. Link landing page | EX-I33 | X1; identity-authz step 8 (#123) | yes (`services/identity/`) | `test_links.py::test_next_*` pass |
| X26. Text commands and page taps | EX-I34 | X2, X6, X25 | no | `test_ingest.py::test_my_page_*`, `::test_reminders_off_on`, `::test_page_button_sends_link` pass |
| X27. Page templates and stylesheet | EX-I35 | X1; identity-authz step 8 (#123) | no | `test_me_templates.py` passes |
| X28. `/me/household` and history | EX-I36 | X4, X18, X27; identity-authz step 12 (#127) | no | `test_me_household.py::test_household_page_*`, `::test_csrf_required` pass |
| X29. Household page tools | EX-I37 | X7, X28 | yes (`tokens.py`, #281) | `test_take_turns_switch`, `test_reaction_*` pass |
| X30. Templates submitted (operator) | runbook | X10 | n/a | Approved names in the VM's `infra/.env`; EX-Q1 answered on the issue |
| X31. Pantry gate function | EX-I38 | X1; pantry P4 (#256) | no | `test_pantry.py::test_is_enabled`, existing `get_pantry` tests pass |
| X32. Pantry correction | EX-I39, EX-I40 | X7, X28, X31 | yes (`tokens.py`, #281) | `test_pantry_correction.py`, `test_me_household.py::test_pantry_*` pass |
| X33. Reminders | EX-I41, EX-I42 | X2, X6, X10, X11, X31 | no | `test_reminders.py`, `test_sweeper.py::test_reminder_pass_once_per_minute` pass |
| X34. Owner design system | EX-I43 | X19 | no | Component render tests pass |
| X35. Owner sign-in and realm | EX-I44 | X34; identity-authz step 13 (#128) | yes (realm) | `auth.test.ts`, `test_realm_file.py` pass |
| X36. Owner export in the image | EX-I45 | X19 | yes (`infra/`) | `test_owner_static.py`, `test_caddyfile.py` pass; the image serves `/owner/auth` |
| X37. Org scope helper | EX-I46 | X1; identity-authz step 5 (#115) | yes (`services/safety/`) | `test_scope.py::test_org_*` pass |
| X38. Owner API: orders and notices | EX-I47 | X5, X37; identity-authz step 13 (#128) | no | `test_app_routes.py::test_orders_*`, `::test_notice_*`, `::test_org_scoped` pass |
| X39. Owner API: insights and connection | EX-I48 | X11, X38 | no | `test_app_routes.py::test_insights_*`, `::test_connection` pass |
| X40. Owner Home and Orders | EX-I49 | X34, X35, X38 | no | `discount.test.tsx`, `orders.test.tsx` pass |
| X41. Owner Insights and Account | EX-I50 | X39, X40 | no | `insights.test.tsx`, `states.test.tsx` pass |
| X42. Eval setup keys | EX-I51 | X8; evals 5a, 5b (#228, #229) | no | `test_panel_reset.py::test_experience_*` pass |
| X43. Eval checks | EX-I52 | X42; evals 6 (#231) | yes (`evals/checks.py`) | `test_checks.py::test_experience_*` pass |
| X44. Safety eval cases | EX-I53 | X16, X22, X23, X24, X43; evals 10b (#235); pantry P6 (#258) | yes (`evals/cases/safety/`) | The §14 safety cases pass all 3 runs |
| X45. Task eval cases and scenarios | EX-I54, EX-I55 | X29, X32, X44; pantry P5 (#257) | no | The §14 task cases and `tests/scenario/test_experience_*.py` pass |
| X46. Demo rehearsal (product owner) | — | X30, X41, X45 | n/a | PROJECT_BRIEF §7 demo script runs on the VM |

Each unit test lands with the step whose code it tests. Steps with no external dependency:
X2, X3, X5, X6, X19.

### Target Files to Create/Modify

#### EX-I1. `services/api/whatsapp/sender.py` (Modify)

*   **Proposed Changes**: `TEXTS` gets the §6 wording and the new keys, except
    `unknown_number`, `consent_renewal` and `consent_revoked` (identity-authz step 9, then
    X15) and `nothing_needed` (pantry P5's sender). Adds `shortfall_prompt` and
    `shortfall_result` as `"{text}"`. `approval_request` takes `amount` and `reply_by`;
    `dislike_not_saved` takes `admin`.
*   **Tests**: `tests/unit/test_whatsapp_texts.py` (Create): `test_every_key_renders`,
    `test_button_limits`, `test_every_refusal_has_text` (covers `approval_not_approver`,
    `already_decided`, `approval_expired`, `take_turns_not_approver`, `setup_not_admin`,
    `setup_expired`, `setup_not_understood`), `test_shortfall_keys_present`.
    `tests/unit/test_whatsapp_sender.py` (Modify): the expected strings at `:57-58` and in
    the cases at `:64-145` change to the §6 wording.
    `tests/unit/test_ingest_shortfall.py` (Modify): the expected text at `:66`.

#### EX-I2. `services/mcp/household/server.py`, `services/agents/consumer/graph.py`, `services/api/ingest.py` (Modify)

*   **Proposed Changes**:
    *   `request_approval` passes `reply_by`: the `approval_expiry` timer's `due_at` in the
        household's `timezone`, formatted "6:30 pm", or "Sat 6:30 pm" when not today.
    *   `learn_dislike` passes `admin`: the display name of the household's first admin by
        `household_members.created_at` (the person who saves allergies), read in the same
        query that already loads the member.
    *   In ingest, the take-turns tap refusal (`ingest.py:309-311`) sends
        `take_turns_not_approver` only when the refusal reason is `wrong_approver`, with
        the approver's display name from `approval_rules.approver_user_id` joined to
        `users`. Other refusals still send nothing.
*   **Tests**: existing approval and dislike tests updated for the new params.
    `tests/unit/test_ingest.py` (Modify): `test_tt_tap_by_non_approver_sends_nothing`
    (`:528`) becomes `test_tt_tap_by_non_approver_replies`;
    `test_tt_tap_unknown_order_sends_nothing` (`:541`) is unchanged and must still pass.

#### EX-I3. `services/api/whatsapp/sender.py` (Modify, sim path)

*   **Proposed Changes**: the sim branch runs `graph_payload` and the same validation, and
    stores the payload under a new `graph` key of `sim_outbound.body`; `message_id`,
    `template` and the other keys stay, so dedupe (`sender.py:55-61`) and
    `/internal/sim/outbox` are unchanged. A failure writes outcome `refused`, reason
    `whatsapp_unrenderable` (a trace row only, until X9 adds `whatsapp_sends`).
*   **Tests**: `tests/unit/test_whatsapp_sender.py` (Modify): `test_sim_applies_limits`,
    `test_sim_keeps_message_id_and_template`.

#### EX-I4. `migrations/0015_experience.sql` (Create; critical), `tests/unit/test_migrations.py` (Modify)

*   **Proposed Changes**: the §13 DDL, these indexes, and two views:
    *   Indexes: `held_messages (user_id, status, held_id)`, `reminders (household_id,
        kind)`, `onboarding_proposals (household_id, status)`, `join_codes (household_id)
        WHERE used_at IS NULL`, `whatsapp_sends (user_id, created_at)`.
    *   `experience_weekly_asks` (one row per household, user and week):

        ```sql
        SELECT r.household_id, s.user_id,
               date_trunc('week', s.created_at AT TIME ZONE h.timezone)::date AS week,
               count(*) AS asks
        FROM whatsapp_sends s
        JOIN agent_messages m ON m.message_id = s.message_id
        JOIN runs r ON r.run_id = m.run_id
        JOIN household_members hm ON hm.user_id = s.user_id
        JOIN households h ON h.household_id = hm.household_id
        WHERE s.outcome IN ('sent', 'template')
          AND s.status IS DISTINCT FROM 'failed'
          AND EXISTS (SELECT 1 FROM jsonb_array_elements(m.body->'buttons') b
                      WHERE b->>'payload' ~ '^(ap|tt|ob|sf):')
        GROUP BY 1, 2, 3
        ```

        The household comes from `household_members` (a person is in one household,
        identity-authz §5), so `household_id = NULL` runs still count.
    *   `experience_weekly_guards` (one row per household and week, same `week`
        expression): `approvals_expired` (`approvals.status = 'expired'`, by `created_at`),
        `orders_ended` (`orders.state IN ('cancelled', 'expired')`, by `updated_at`),
        `rejects` (`approvals.status = 'rejected'`), `safety_refusals` (`agent_traces.kind
        = 'tool_result'` with `payload->>'refused' = 'true'`, joined through `runs`),
        `revokes` (`audit_events.kind = 'consent_revoked'`, through the subject's household),
        `held_dropped` (`held_messages.status = 'dropped'`), `sends_not_delivered`
        (`whatsapp_sends.outcome IN ('skipped', 'refused')`).
*   **Tests**: `test_migrations.py`: 0015 in the apply-order assertion; the new tables and
    views in `TABLES`; `test_0015_additive` (no DROP, RENAME, ALTER TYPE or CHECK; applies
    twice).

#### EX-I5. `services/common/ids.py` (Modify), `tests/unit/test_ids.py` (Modify)

*   **Proposed Changes**: one function per id; parsers return `None` on any malformed input.
    *   `setup_button(proposal_id: UUID, answer: Literal["yes", "no", "remove", "keep"], n:
        int | None = None) -> str` → `ob:<uuid>:<answer>` or `ob:<uuid>:<answer>:<n>` (`n`
        required for `remove` and `keep`, forbidden otherwise);
        `parse_setup_button(payload) -> tuple[UUID, str, int | None] | None`.
    *   `REMINDER_STOP = "rs:stop"`.
    *   `page_button(page: Literal["household", "pantry", "renew"]) -> str` → `me:<page>`;
        `parse_page_button(payload) -> str | None`.
    *   `proposal_id(run_id: UUID) -> UUID` = `uuid5(run_id, "proposal")`.
    *   `new_join_code() -> str`: 8 characters from `secrets.choice` over the RFC 4648
        base32 alphabet (40 bits); the module docstring lists it with `new_run_id` as the
        only random ids.
    *   `NS_HELD = UUID(<fixed literal>)`; `held_release_id(held_id: int) -> UUID` =
        `uuid5(NS_HELD, str(held_id))`; `held_cancelled_id(inbound_id: UUID) -> UUID` =
        `uuid5(NS_HELD, f"cancelled:{inbound_id}")`.
    *   `NS_REMINDER = UUID(<fixed literal>)`; `reminder_message_id(reminder_id: int) ->
        UUID` = `uuid5(NS_REMINDER, str(reminder_id))`.
    *   `org_order_ref(org_id: str, order_id: UUID) -> str`: `base64.b32encode(sha256(
        f"{org_id}:{order_id}".encode()).digest())[:8]` (opaque, not secret).
*   **Tests**: one per function, including round trips and malformed payloads.

#### EX-I6. `services/common/protocol/schemas.py` (Modify), `tests/unit/protocol/test_schemas.py` (Modify)

*   **Proposed Changes**: `Ref(kind: Literal["approval", "order", "grant", "reminder"], id:
    str)`; `WhatsAppOut.ref: Ref | None = None`; `SetupPerson` and `SetupAnswer` (§9 step
    2), all exported.
*   **Tests**: `test_whatsapp_out_ref_optional`, `test_ref_kind_closed`,
    `test_setup_answer_rejects_unknown_codes`.

#### EX-I7. `services/common/config.py` (Modify), `tests/unit/test_config.py` (Modify)

*   **Proposed Changes**: settings `wa_tpl_approval_request`, `wa_tpl_take_turns_offer`,
    `wa_tpl_order_summary`, `wa_tpl_consent_renewal`, `wa_tpl_consent_revoked`,
    `wa_tpl_reminder_delivery_today`, `wa_tpl_reminder_pantry_summary`, each
    `_var({"api"}, "")` read from `WA_TPL_<KEY>`; `insights_sample: bool = _var({"api"},
    False)`.
*   **Tests**: `test_template_settings_default_empty`.

#### EX-I8. `services/common/runs.py` (Modify), `tests/unit/test_runs.py` (Modify)

*   **Proposed Changes**: `_RETRY_TARGET_SQL` adds `AND r.household_id IS NOT NULL AND
    coalesce(r.trigger->>'source', '') NOT IN ('code', 'join', 'tap', 'me', 'reminder')`
    (EX-A16). The `_take_turns_tap` run in `ingest.py:259-264` sets `source: "tap"`.
*   **Tests**: `test_code_only_run_not_retry_target`, `test_reminder_never_retry_target`,
    `test_tap_run_not_retry_target`, `test_failed_chat_run_still_retry_target`.

#### EX-I9. `services/identity/tokens.py`, `services/safety/scope.py` (Modify; critical), `tests/unit/test_scope.py`, `tests/unit/test_guard_identity.py` (Modify)

*   **Proposed Changes**: the six new tables in `HOUSEHOLD_TABLES`; the allowlist reasons
    `join_lookup`, `held_release`, `setup_phone_check`, `reminder_pass`.
    *Amended 2026-11-03 (#281):* the EX-A11 `TOOL_SCOPES` rows no longer land here. Each
    row lands with the step that builds its tool (EX-I26, EX-I27, EX-I31, EX-I37, EX-I39),
    because `test_tokens.py::test_tool_scopes_cover_guarded_tools` requires `TOOL_SCOPES`
    to equal the guarded tools that exist, and those tools come after this step.
*   **Tests**: `test_scope.py::test_scoped_tables_only_via_helper` with the new tables.

#### EX-I10. `services/api/ingest.py` (Modify), `tests/unit/test_ingest.py` (Modify)

*   **Proposed Changes**: `_INSERT_INBOUND_SQL` gains `RETURNING received_at`; directly
    after it, for a known phone, `UPDATE channel_identities SET last_inbound_at = %s WHERE
    phone_e164 = %s`, before any routing, for every content kind. A duplicate returns no row
    and updates nothing.
*   **Tests**: `test_last_inbound_set_once_per_message`, `test_last_inbound_set_for_button_taps`.

#### EX-I11. `services/seed/__main__.py`, `services/mocks/control.py`, `tests/unit/api_world.py`, `tests/unit/test_sim_channel.py` (Modify)

*   **Proposed Changes**: the seed and `control.reset` set `last_inbound_at = now()` for
    every seeded and eval phone. The test world sets it for its fixture phones, and inserts
    a `runs` row and an `agent_messages` row for every message it sends, so the
    `whatsapp_sends.message_id` foreign key holds; `test_sim_channel.py:55-70` and `:82-92`
    use those rows instead of random ids.
*   **Tests**: `tests/unit/test_seed.py::test_phones_start_inside_window`,
    `tests/unit/test_panel_reset.py::test_reset_restores_window`; `test_sim_channel.py`
    passes unchanged in what it asserts.

#### EX-I12. `services/api/whatsapp/sender.py` (Modify, window), `services/api/whatsapp/templates.py` (Create, map only)

*   **Proposed Changes**:
    *   `_PHONE_SQL` also selects `last_inbound_at`.
    *   §5 steps 3, 5 and 6. Until X10 lands, step 4 finds no template and continues at
        step 5; 131047 also continues at step 5.
    *   `SendReal` becomes `Callable[[str, dict], Awaitable[str | None]]`; `GraphSender`
        returns `messages[0].id` from the Graph response and raises `GraphRefused(code)`
        for a Graph error body. The sim path returns `None`, and its row's
        `graph_message_id` is NULL.
    *   One `whatsapp_sends` row and one `whatsapp_window` decision per outcome.
    *   `templates.py` holds the §5 map with its keys, setting names and holdable kinds,
        and `meta_name(key, settings) -> str | None`; no parameter builders yet.
*   **Tests**: `tests/unit/test_whatsapp_window.py` (Create): `test_inside_window_sends`,
    `test_outside_window_holds`, `test_outside_window_skips_unholdable`,
    `test_model_text_never_templated`, `test_sim_applies_window`,
    `test_hold_idempotent_on_replay`, `test_graph_message_id_stored`;
    `test_whatsapp_sender.py::test_window_refusal_not_retried`.

#### EX-I13. `services/api/whatsapp/templates.py`, `services/api/whatsapp/sender.py` (Modify, templates)

*   **Proposed Changes**: §5 step 4 and the 132xxx branch; 131047 now continues at step 4.
    The parameter builders: approval amount and reply-by from the approval and its timer;
    the store name from `organizations.name` through `agent_cards.owner_ref = 'org:' ||
    org_id`; the item count from the order's final `order_items`; window times in the
    household's `timezone`. The Graph body: `{type: "template", template: {name,
    language: {code: "en_US"}, components: [{type: "body", parameters: [{type: "text",
    text}…]}, {type: "button", sub_type: "quick_reply", index, parameters: [{type:
    "payload", payload}]}…]}}`.
*   **Tests**: `tests/unit/test_template_send.py` (Create): `test_template_body_shape`,
    `test_params_in_order`, `test_unconfigured_means_hold`,
    `test_params_have_no_sensitive_codes` (every key, sample data with allergy codes and
    member names in the household), `test_template_refusal_not_retried`,
    `test_131047_then_template`.

#### EX-I14. `docs/runbooks/whatsapp-templates.md` (Create)

*   **Proposed Changes**: each §5 template's name, category (utility), language, exact body,
    sample parameter values, buttons, and the `WA_TPL_*` variable to write the approved
    name into.

#### EX-I15. `infra/.env.example`, `infra/docker-compose.yml` (Modify; critical)

*   **Proposed Changes**: empty entries for the eight variables of EX-I7 in
    `infra/.env.example`; the `api` service's environment list in compose gains them.
*   **Tests**: `tests/unit/test_compose.py::test_api_gets_template_vars` (Create or Modify).

#### EX-I16. `services/api/whatsapp/status.py` (Create), `services/api/routes/whatsapp.py` (Modify)

*   **Proposed Changes**: the webhook passes `entry[].changes[].value.statuses[]` to
    `status.record(conn, statuses)`, which updates `whatsapp_sends.status` and `status_at`
    by `graph_message_id`; on `failed` with error code 131047 it inserts the
    `held_messages` row for a holdable message (`ON CONFLICT (message_id) DO NOTHING`).
*   **Tests**: `tests/unit/test_whatsapp_status.py` (Create): `test_status_delivered_recorded`,
    `test_status_131047_holds`, `test_status_unknown_id_ignored`.

#### EX-I17. `services/mcp/household/server.py`, `services/agents/household/graph.py` (Modify)

*   **Proposed Changes**: the approval prompt (`server.py:431-439`) sets `ref = {approval,
    <approval_id>}`; the order summary and take-turns offer (`graph.py:1297-1325`) set
    `ref = {order, <order_id>}`.
*   **Tests**: `test_household_tools.py::test_approval_prompt_has_ref`,
    `test_household_graph.py::test_summary_has_ref`, `::test_take_turns_offer_has_ref`.

#### EX-I18. `services/agents/household/graph.py` (Modify)

*   **Proposed Changes**: the `nothing_needed` send (pantry P5) sets `ref = {order,
    <order_id>}`.
*   **Tests**: `test_household_graph.py::test_nothing_needed_has_ref`.

#### EX-I19. `services/api/ingest.py`, `services/api/whatsapp/sender.py`, `services/identity/grants.py` (Modify; the last critical)

*   **Proposed Changes**: the identity-authz I26 renew sender in ingest (on a run ending
    `grant_revoked`, `grant_expired` or `grant_missing`) sends `consent_renewal` with a
    `me:renew` button and `ref = {grant, <grant_id>}`, inside or outside the window (§5), no
    longer only inside. `grants.after_revoke` (identity-authz §11) sends
    `consent_revoked` with `agent`, `placed_line`, the button and `ref`. `TEXTS` gets both
    §6 wordings (EX-A15).
*   **Tests**: `tests/unit/test_ingest_identity.py` (Modify):
    `test_renew_text_inside_window_only` becomes `test_renew_sent_with_ref`;
    `test_approval_prompt_names_both_limits` becomes
    `test_approval_prompt_has_amount_and_reply_by`; `test_revoke_notice_has_renew_button`
    (Create).

#### EX-I20. `services/api/ingest.py` (Modify, release)

*   **Proposed Changes**: `release_held(conn, user_id, run_id, inbound_id)`, called once in
    `_route_known` directly after the path's run exists and before its reply, for every
    known-person path (text, `ap:`, `tt:`, `sf:`, `ob:`, `me:`, `rs:`, commands). It
    selects `pending` rows oldest first under `unscoped(conn, "held_release")`; marks stale
    ones `dropped`; sends up to 3 fresh ones through the outbox with `from_agent =
    system:api`, `message_id = ids.held_release_id(held_id)`, and marks them `released`;
    sends `held_cancelled_line` once with `ids.held_cancelled_id(inbound_id)` if an approval
    was dropped; writes a `decision` row per held row.
*   **Tests**: `tests/unit/test_held_items.py` (Create): `test_expired_approval_dropped`,
    `test_release_cap`, `test_release_oldest_first`, `test_cancelled_line_once`,
    `test_release_idempotent_on_replay`, `test_release_writes_one_send_row`.

#### EX-I21. `tests/unit/test_experience_metric.py` (Create)

*   **Proposed Changes**: tests over the two views: `test_counts_delivered_decisions_only`,
    `test_held_counted_once`, `test_stop_and_open_buttons_not_counted`, `test_guard_metrics`,
    `test_week_in_household_timezone`, `test_falls_across_weeks` (the test inserts three
    weeks of rows).

#### EX-I22. `services/api/routes/panel.py` (Modify)

*   **Proposed Changes**: `GET /panel/experience?household=` → `{weeks: [two entries, this
    week then last: {week, per_user: [{user_id, display_name, asks}], guards: {approvals_expired,
    orders_ended, rejects, safety_refusals, revokes, held_dropped, sends_not_delivered}}],
    held_pending: int, templates_configured: [key]}`. The panel page renders three lines:
    asks per adult this week and last; guard totals this week; pending held items and
    configured templates.
*   **Tests**: `tests/unit/test_panel_experience.py` (Create): `test_shape`,
    `test_unknown_household_404`.

#### EX-I23. `services/safety/visibility.py` (Create; critical)

*   **Proposed Changes**: `DataClass = Literal["own_constraints", "child_data", "plan",
    "spending_total", "spending_per_order", "take_turns", "reaction_totals", "pantry",
    "contact"]` (one per §4 row); `Member(user_id, role, is_approver, household_adults)`,
    built by `load_member(conn, scope, user_id)` from `household_members` and
    `approval_rules` through `scope.scoped`; `can_see(viewer: Member, data: DataClass,
    subject_user_id: UUID | None) -> bool`.
*   **Tests**: `tests/unit/test_visibility.py` (Create): `test_matrix` (each cell),
    `test_child_sees_only_self`, `test_reactions_hidden_below_three_adults`.

#### EX-I24. `Makefile`, `.github/workflows/ci.yml` (Modify; critical), `apps/owner/` scaffold (Create), `.dockerignore` (Modify)

*   **Proposed Changes**: `app-lint` (`eslint` with `eslint.config.js`, `tsc --noEmit`) and
    `app-test` (`jest` with `jest-expo`) run in `node:22`; a CI job `owner-app` runs both.
    The scaffold: `package.json` pinned to Expo SDK 54 with the matching `expo-router`,
    `expo-auth-session` and `jest-expo` versions from `npx expo install`, exact versions,
    `package-lock.json`, `app.json` (`experiments.baseUrl: "/owner"`, `web.output:
    "single"`), `tsconfig.json`, `eslint.config.js`, `app/_layout.tsx`, one smoke test.
    `.dockerignore` gains `apps/owner/node_modules` and `apps/owner/dist`.
*   **Tests**: `tests/unit/test_ci_workflow.py::test_owner_app_job` (Modify).

#### EX-I25. `services/common/codes.py` (Create)

*   **Proposed Changes**: imports `AllergenCode` and `DietCode`; `Code = AllergenCode |
    DietCode`; `ALIASES: Mapping[str, frozenset[Code]]` (for example "nuts" → tree nuts and
    peanuts, "dairy" → milk, "prawns" → crustaceans, "gluten-free" → gluten);
    `cross_check(message: str, chosen: set[Code]) -> list[str]`, returning alias words
    whose codes were not all chosen.
*   **Tests**: `tests/unit/test_codes.py` (Create): `test_enums_match_migration` (against
    the 0002 CHECK text), `test_cross_check_finds_missed_alias`,
    `test_cross_check_never_adds_codes`.

#### EX-I26. `services/mcp/household/server.py` (Modify)

*   **Proposed Changes**, all through `run_guarded`, household from the run, idempotency
    key `None`:
    *   `get_setup_state() -> {role, has_budget, has_proposal}` for the person the consumer
        agent acts for (`ctx.on_behalf_of`).
    *   `propose_setup(answer: SetupAnswer) -> {proposal_id, answer, phones_taken}`: the
        caller acts for an admin (limits check on `household_members.role`); §9 step 4
        validation; insert with `ids.proposal_id(ctx.run_id)` and `ON CONFLICT DO
        NOTHING`; earlier `pending` proposals become `superseded`.
    *   The EX-A11 `TOOL_SCOPES` rows for `get_setup_state` and `propose_setup` in
        `services/identity/tokens.py` (critical; moved here from EX-I9, #281).
*   **Tests**: `test_guard_identity.py::test_tool_scope_table` with the two rows;
    `tests/unit/test_household_setup_tools.py` (Create):
    `test_propose_member_refused`, `test_propose_replay_one_row`,
    `test_propose_supersedes_pending`, `test_propose_phone_taken`,
    `test_propose_unknown_code_unmatched`, `test_setup_state`.

#### EX-I27. `services/mcp/household/server.py` (Modify)

*   **Proposed Changes**: `apply_setup(proposal_id: UUID, answer: Literal["yes", "no",
    "remove", "keep"], removal: int | None, child_user_ids: dict[str, UUID], user_id: UUID) -> {status,
    saved, removals_pending: list[int]}`: `system:api` only; `user_id` (the tapper, from
    the run trigger, as `decide_approval` takes it) is checked by limits to be an admin; §9 writes in one transaction; a repeated answer to
    the same removal returns `already_decided`. The EX-A11 `TOOL_SCOPES` row for
    `apply_setup` in `services/identity/tokens.py` (critical; moved here from EX-I9, #281).
*   **Tests**: `test_guard_identity.py::test_tool_scope_table` with the row;
    `test_household_setup_tools.py`: `test_apply_nothing_saved_without_yes`,
    `test_apply_member_refused`, `test_apply_expired_refused`, `test_apply_idempotent`,
    `test_apply_remove_by_index`, `test_apply_budget_in_household_currency`.

#### EX-I28. `services/agents/consumer/graph.py`, `prompts.py` (Modify)

*   **Proposed Changes**:
    1.  Before `understand_message`, code calls `get_setup_state`; an admin of a household
        with no budget is routed to `onboard`.
    2.  `INTENTS` adds `onboard`; the classify prompt defines it.
    3.  The `propose_setup` node runs §9 steps 2–5 with the setup prompt (codes listed;
        JSON matching `SetupAnswer`), then sends `setup_restate` with `ob:` buttons and a
        `setup_phone_taken` per dropped phone, or `setup_ask` / `setup_not_understood` by
        the §9 "Which reply" rule.
    4.  `RunInfo` is unchanged (role and budget come from the tool).
*   **Tests**: `tests/unit/test_consumer_graph.py` (Modify): `test_onboard_forced_without_budget`,
    `test_onboard_unknown_code_unmatched`, `test_unmatched_word_quoted_back`,
    `test_cross_check_names_missed_word`, `test_onboard_model_failure_fixed_reply`,
    `test_onboard_first_empty_message_asks`, `test_onboard_later_empty_not_understood`.

#### EX-I29. `services/agents/consumer/stubs.py` (Modify)

*   **Proposed Changes**: stub `SetupAnswer`s for the EX-CUJ 1 and 2 messages and for "hi".

#### EX-I30. `services/api/ingest.py` (Modify, setup taps)

*   **Proposed Changes**: `ob:` taps start the §3 tap run, call `create_child` for each new
    child named in the proposal, then `apply_setup`, then send the §9 replies for that
    outcome with ordinals `ids.message_id(inbound, n)` in the listed order.
*   **Tests**: `tests/unit/test_ingest.py`: `test_setup_tap_calls_create_child_then_apply`,
    `test_setup_tap_keycloak_failure_writes_nothing`, `test_setup_tap_no_discards`,
    `test_setup_tap_remove_then_keep`, `test_setup_tap_non_admin`,
    `test_setup_tap_expired`, `test_setup_tap_reply_order`.

#### EX-I31. `services/mcp/household/server.py` (Modify)

*   **Proposed Changes**: `complete_join(code: str, user_id: UUID)` (`system:api`;
    `user_id` is the new user; the code is the run's household's, unused, unexpired, and its phone
    equals the user's): moves `pending` into `member_constraints` (`source = 'join'`) and
    marks the code used. `get_household_allergens` and the household form of
    `get_constraints` (identity-authz step 12) union the codes of the household's open join
    codes; `_member_allergens` adds one entry per open code under the key `join:<code>`.
    The EX-A11 `TOOL_SCOPES` row for `complete_join` in `services/identity/tokens.py`
    (critical; moved here from EX-I9, #281).
*   **Tests**: `test_guard_identity.py::test_tool_scope_table` with the row;
    `tests/unit/test_household_tools.py::test_open_join_code_allergens_merged`,
    `::test_used_code_not_merged_twice`; `tests/unit/test_join_codes.py` (Create):
    `test_complete_join_moves_codes`, `test_wrong_phone_refused`, `test_expired_code_refused`.

#### EX-I32. `services/api/ingest.py` (Modify, joining)

*   **Proposed Changes**: `JOIN <code>` from an unknown phone: look up the code under
    `unscoped(conn, "join_lookup")`, check the phone, call `create_person`, set
    `last_inbound_at`, start the §3 join run, call `complete_join`, then send `joined` and
    the consent link. Otherwise identity-authz's `unknown_number` path runs.
*   **Tests**: `tests/unit/test_ingest.py`: `test_join_valid`, `test_join_wrong_phone`,
    `test_join_joined_inside_window`, `test_unknown_phone_reply_unchanged`.

#### EX-I33. `services/identity/links.py` (Modify; critical; created by identity-authz I21)

*   **Proposed Changes**: `links.issue(..., next: Literal["household", "pantry", "history",
    "agents"] | None = None)`, stored on the link row's existing detail; the callback
    redirects to `/me/<next>`, or `/me/agents` when unset (EX-A15).
*   **Tests**: `tests/unit/test_links.py::test_next_redirect`, `::test_next_closed_set`.

#### EX-I34. `services/api/ingest.py` (Modify, commands and page taps)

*   **Proposed Changes**: the §7 text commands and the `rs:` and `me:` taps, each in a
    `household_id = NULL` run with `source: "code"`; "my page" and "my household" share
    the "my agents" handler, issuing `links.issue(..., next=...)` and sending `me_link`.
*   **Tests**: `tests/unit/test_ingest.py`: `test_my_page_sends_link`,
    `test_my_household_sends_link`, `test_reminders_off_on`, `test_page_button_sends_link`,
    `test_renew_button_sends_consent_link`.

#### EX-I35. `services/api/templates/` (Create), `services/api/static/me.css` (Create), `pyproject.toml`, `uv.lock` (Modify)

*   **Proposed Changes**: adds `jinja2` (exact version), the engine of Starlette's
    `Jinja2Templates`. Page templates: `base.html` (header, three tabs, CSRF field),
    `household.html`, `pantry.html`, `history.html`, `agents.html`, `band.html`,
    `error.html`; autoescaping on. One stylesheet with system fonts, readable on a phone
    screen; the only script is `onchange="this.form.submit()"` on the switch.
*   **Tests**: `tests/unit/test_me_templates.py` (Create): `test_every_template_renders`,
    `test_autoescape_on`.

#### EX-I36. `services/api/routes/me.py` (Modify; created by identity-authz I24)

*   **Proposed Changes**: `Mount("/me/static", StaticFiles(...))` before identity-authz's
    routes; `GET /me/household`; spending in `/me/history`; restyled identity-authz pages;
    the CSRF helper (`itsdangerous` over session id and form name); reads through
    `scope.scoped` and `visibility.can_see`.
*   **Tests**: `tests/unit/test_me_household.py` (Create): `test_household_page_member_view`,
    `test_household_page_admin_view`, `test_member_sees_no_child_constraint`,
    `test_csrf_required`, `test_static_css_served`.

#### EX-I37. `services/mcp/household/server.py`, `services/api/routes/me.py` (Modify)

*   **Proposed Changes**: tools `set_household_take_turns(on: bool, user_id: UUID)` (EX-A7)
    and `react_to_meal(order_id: UUID, value: Literal[1, -1], user_id: UUID)`
    (`system:api`; `user_id` checked by limits as a member of the run's household; the
    order is the household's and `delivered`; upsert by person). Routes `POST
    /me/household/take-turns` and `POST /me/meals/{order_id}/reaction` start the §3 `/me`
    run and pass the session person as `user_id`. The EX-A11 `TOOL_SCOPES` rows for both
    tools in `services/identity/tokens.py` (critical; moved here from EX-I9, #281).
*   **Tests**: `test_guard_identity.py::test_tool_scope_table` with the two rows;
    `test_me_household.py`: `test_take_turns_switch`,
    `test_switch_read_only_for_non_approver`, `test_reaction_once_per_person`,
    `test_reaction_on_other_household_refused`.

#### EX-I38. `services/common/pantry.py`, `services/mcp/household/server.py` (Modify)

*   **Proposed Changes**: `is_enabled(env: str) -> bool` (`env == "eval"`); `get_pantry`
    calls it instead of its inline comparison (EX-A17).
*   **Tests**: `tests/unit/test_pantry.py::test_is_enabled`; the existing
    `test_get_pantry_off_for_live_household` still passes.

#### EX-I39. `services/mcp/household/server.py` (Modify)

*   **Proposed Changes**: `record_pantry_check(pantry_item: str, kind: Literal["verify",
    "out"], user_id: UUID)` (`system:api`; `user_id` is an adult of the run's household; the
    item is in the vocabulary): inserts `pantry_events (household_id, item, pantry_item,
    unit, delta, kind, source, run_id, actor_user_id)` with `item` and `pantry_item` both
    the vocabulary name, `unit = 'each'`, `delta = 0`, `source = 'me_page'`. The EX-A11
    `TOOL_SCOPES` row for `record_pantry_check` in `services/identity/tokens.py` (critical;
    moved here from EX-I9, #281).
*   **Tests**: `test_guard_identity.py::test_tool_scope_table` with the row;
    `tests/unit/test_pantry_correction.py` (Create): `test_ran_out_writes_out`,
    `test_child_cannot_correct`, `test_unknown_item_refused`, `test_next_order_keeps_out_item`.

#### EX-I40. `services/api/routes/me.py` (Modify)

*   **Proposed Changes**: `GET /me/pantry` (`pantry.is_enabled(households.env)`; estimates
    from `pantry.estimate` over the household's latest events through `scope.scoped`; the
    "isn't on" line otherwise) and `POST /me/pantry/{pantry_item}` with form field `kind`
    (`verify` or `out`), which starts the `/me` run and calls `record_pantry_check` for the
    signed-in person.
*   **Tests**: `test_me_household.py::test_pantry_page_on_and_off`, `::test_pantry_post`.

#### EX-I41. `services/agents/reminders.py` (Create)

*   **Proposed Changes**: `Due(kind, ref_id, user_id, household_id)`;
    `due_reminders(conn, now) -> list[Due]` (the §10 rules, two queries under
    `unscoped(conn, "reminder_pass")`, the pantry rule through `pantry.is_enabled`);
    `apply_skips(conn, due, now)` (opt-out, child, daily cap, quiet hours); `send(conn,
    due)` (a `household_id = NULL` run with `source: "reminder"`, the `reminders` row, the
    outbox message with `message_id = ids.reminder_message_id(reminder_id)` and `ref =
    {reminder, <reminder_id>}`).
*   **Tests**: `tests/unit/test_reminders.py` (Create): `test_delivery_today_due`,
    `test_pantry_summary_saturday`, `test_pantry_off_no_summary`, `test_daily_cap`,
    `test_quiet_hours`, `test_stop_these`, `test_no_children`, `test_never_twice`,
    `test_never_held`.

#### EX-I42. `services/agents/sweeper.py` (Modify)

*   **Proposed Changes**: the reminder pass every 60 s after the timer pass (EX-A2).
*   **Tests**: `tests/unit/test_sweeper.py::test_reminder_pass_once_per_minute`.

#### EX-I43. `apps/owner/src/ui/` (Create)

*   **Proposed Changes**: `theme.ts` and the shared components (card, stat, sheet, list row,
    banner, empty, error, skeleton), each with a render test.

#### EX-I44. `apps/owner/src/auth.ts` (Create), `services/identity/realm/kitchenless.json` (Modify; critical), `tests/unit/test_realm_file.py` (Modify)

*   **Proposed Changes**: OIDC PKCE with `expo-auth-session`; the realm's `owner-app`
    client gets redirect `https://${DOMAIN}/owner/auth` and web origin `https://${DOMAIN}`
    (the realm's existing placeholder, `kitchenless.json:729`). The operator re-imports the
    realm on the VM (identity-authz's realm README).
*   **Tests**: `apps/owner/__tests__/auth.test.ts` (mocked discovery and token endpoints);
    `test_realm_file.py::test_owner_app_redirect_and_origin`.

#### EX-I45. `infra/Dockerfile`, `infra/Caddyfile` (Modify; critical), `services/api/routes/owner_static.py` (Create), `services/api/main.py` (Modify)

*   **Proposed Changes**: a `node:22` build stage runs `npm ci` and `expo export --platform
    web` in `apps/owner/`, and the final image (one image for every service) copies `dist/`
    to `/app/owner_dist`. The route serves files under `/owner/` from `OWNER_DIST` (default
    `/app/owner_dist`) and falls back to `index.html`, with `Content-Security-Policy:
    default-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'`. Caddy adds
    `redir /owner /owner/` and `handle /owner/* { reverse_proxy api:8000 }`.
    *Amended 2026-11-03:* `style-src` allows inline styles, because React Native Web injects
    every component style through a `<style>` element and the export's `index.html` has an
    inline reset style. Scripts stay `'self'` only.
*   **Tests**: `tests/unit/test_owner_static.py` (Create): `test_owner_static_fallback`,
    `test_csp_header`; `tests/unit/test_caddyfile.py` (Create): `test_owner_route`.

#### EX-I46. `services/safety/scope.py` (Modify; critical), `tests/unit/test_scope.py` (Modify)

*   **Proposed Changes**: `OrgScope(org_id)`; `org_scoped(conn, scope: OrgScope) ->
    Scoped`, whose SQL must contain `%(org)s`, which the helper binds. `ORG_TABLES =
    ("quotes", "orders", "adapter_modes", "agent_traces")`. A query on `quotes` or `orders`
    must restrict `seller_agent_id IN (SELECT agent_id FROM agent_cards WHERE owner_ref =
    'org:' || %(org)s)`; the helper checks the SQL contains that fragment. The scan test
    allows these tables in `routes/app.py` and `owner_views.py` only through `org_scoped`.
*   **Tests**: `test_org_tables_only_via_helper`, `test_org_scoped_requires_seller_filter`.

#### EX-I47. `services/api/routes/app.py` (Modify; created by identity-authz I32), `services/api/owner_views.py` (Create)

*   **Proposed Changes**: `GET /app/orders` and `GET /app/notices` (§11), through
    `org_scoped`.
*   **Tests**: `tests/unit/test_app_routes.py` (Modify): `test_org_scoped`,
    `test_no_household_fields`, `test_orders_summary`, `test_notice_kinds_closed`.

#### EX-I48. `services/api/routes/app.py`, `services/api/owner_views.py` (Modify), `services/api/insights_sample.yaml` (Create)

*   **Proposed Changes**: `GET /app/insights` and `GET /app/connection` (§11).
*   **Tests**: `test_app_routes.py`: `test_insights_suppressed_below_k` (live-like rows
    inserted by the test), `test_insights_weekly_only`, `test_sample_mode_labelled`,
    `test_connection`.

#### EX-I49. `apps/owner/app/(tabs)/index.tsx`, `orders.tsx`, `_layout.tsx` (Create)

*   **Proposed Changes**: Home (the discount card and sheet, today's counts, latest notices)
    and Orders (polling every 5 s while focused).
*   **Tests**: `apps/owner/__tests__/discount.test.tsx` (two taps; error state),
    `orders.test.tsx`.

#### EX-I50. `apps/owner/app/(tabs)/insights.tsx`, `account.tsx` (Create)

*   **Proposed Changes**: Insights (hidden cells, the sample banner) and Account
    (connection, sign out).
*   **Tests**: `insights.test.tsx::shows_sample_banner`, `states.test.tsx` (loading, empty,
    error on every screen).

#### EX-I51. `services/mocks/control.py`, `tests/unit/test_panel_reset.py` (Modify)

*   **Proposed Changes**: setup keys `last_inbound_hours_ago`, `setup_incomplete` and
    `join_code` with undo; `control.reset` clears the new tables for eval households.
*   **Tests**: `test_experience_setup_keys_undone`, `test_reset_clears_experience_tables`.

#### EX-I52. `evals/checks.py` (Modify; critical), `evals/coverage.yaml` (Modify)

*   **Proposed Changes**: checks for the §14 cases (no `sim_outbound` row for a phone,
    `member_constraints` row count, restatement contains a word, an order has no item with
    an allergen); coverage entries: XG2, XG3, XG7, XG8, XG11, XG12.
*   **Tests**: `tests/unit/test_checks.py::test_experience_checks`.

#### EX-I53. `evals/cases/safety/*.yaml` (Create; critical)

*   **Proposed Changes**: the five §14 safety cases.

#### EX-I54. `evals/cases/task/*.yaml` (Create)

*   **Proposed Changes**: the four §14 task cases.

#### EX-I55. `tests/scenario/test_experience_*.py` (Create)

*   **Proposed Changes**: EX-CUJ 1, 3, 4, 6 and 7 against the sim channel, one file each.

#### Also modified (X1)

*   `docs/designs/SITEMAP.md`: the experience row becomes Approved, with components
    `services/api` (`/me`, `/app`, `/owner`, WhatsApp sender, status, ingest),
    `services/agents` (consumer onboarding, reminders, sweeper), `services/mcp/household`,
    `services/common` (codes, ids, schemas, config, runs, pantry), `services/safety`
    (visibility, scope), `services/identity` (tokens, links, grants, realm),
    `services/mocks`, `services/seed`, `apps/owner`, `infra/`, `.github/workflows/`,
    `Makefile`, `migrations/`, `evals/`; each amended design gains ", 2026-11-03,
    experience".
*   `docs/designs/{architecture,agent-protocol,agents,identity-authz,pantry,adapters,evals}.md`:
    one note per §1 row.

### Out of scope (implementation)

*   Lifting the pantry gate; quantity reminders; anything between households.
*   Real Meta template submission in CI (X30 is operator-run).
*   An app-store build of the owner app.

### Verification Plan

*   **Build**: `make up`; the image builds with the export stage.
*   **Unit tests**: `make test-unit` (`MODE=mock`, `MODEL_MODE=stub`); `make app-test`.
*   **Static analysis**: `make lint`; `make app-lint`.
*   **Evals**: `make eval-fast` before each PR touching `services/agents/`; the §14 safety
    cases must pass all 3 runs.
*   **E2E**: the EX-I55 scenario tests; EX-CUJ 9 through `discount.test.tsx` and the X46
    rehearsal.
*   **Metric**: `test_falls_across_weeks` passes; the panel line shows the count.

--------------------------------------------------------------------------------

*Drafted with EGM*
