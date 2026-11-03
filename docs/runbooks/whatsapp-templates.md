# Runbook: submitting the WhatsApp Meta templates

Experience design §5 and EX-I14; the operator step is X30. The `api` sender uses a Meta
template only to reach a person outside the 24-hour WhatsApp window. Until a template is
approved and its name configured, its message is held (approvals, order summaries,
take-turns offers, consent notices) or skipped (reminders). Nothing else changes.

## Steps

1.  In Meta Business Manager, open WhatsApp Manager → Message templates → Create template.
2.  For each template below, enter the name, category **Utility** and language
    **English (US)** (`en_US`). Paste the body exactly as written, with `{{n}}` for each
    parameter, and enter the sample values for Meta's review. Add each button as a
    **Quick reply** button with the title shown, in the order shown.
3.  Submit, then wait for Meta's decision.
4.  When a template is approved **as Utility**, write its approved name into the variable
    shown, in the VM's `infra/.env`, then restart `api`. A template that Meta files as
    **Marketing** is not configured: leave its variable empty.
5.  Record each outcome, including any Marketing filing, on the X30 issue (EX-Q1).

An empty variable means "not approved". If Meta later pauses or rejects a template, the
send ends `refused` with reason `template_refused`. Clear the variable until it is
approved again.

## Templates

### `approval_needed_v1`

*   Variable: `WA_TPL_APPROVAL_REQUEST` (text key `approval_request`)
*   Category: Utility. Language: `en_US`
*   Body: `Your agent wants to spend {{1}} and needs your approval. Reply by {{2}}.`
*   Samples: `{{1}}` = `$76.00`; `{{2}}` = `6:30 pm`
*   Buttons (quick reply): `Approve`, `Reject`

### `take_turns_offer_v1`

*   Variable: `WA_TPL_TAKE_TURNS_OFFER` (text key `take_turns_offer`)
*   Category: Utility. Language: `en_US`
*   Body: `Tonight's dinner had something one person doesn't like. Want me to take turns on whose dislikes come first?`
*   Samples: none (no parameters)
*   Buttons (quick reply): `Take turns`

### `order_delivered_v1`

*   Variable: `WA_TPL_ORDER_SUMMARY` (text key `order_summary`, on delivery)
*   Category: Utility. Language: `en_US`
*   Body: `Your order from {{1}} was delivered: {{2}} items, {{3}}.`
*   Samples: `{{1}}` = `Fresh Market`; `{{2}}` = `12`; `{{3}}` = `$42.50`
*   Buttons: none

### `consent_renewal_v1`

*   Variable: `WA_TPL_CONSENT_RENEWAL` (text key `consent_renewal`)
*   Category: Utility. Language: `en_US`
*   Body: `Your agents' access has ended. Tap Renew and I'll send you a sign-in link.`
*   Samples: none (no parameters)
*   Buttons (quick reply): `Renew`

### `consent_revoked_v1`

*   Variable: `WA_TPL_CONSENT_REVOKED` (text key `consent_revoked`)
*   Category: Utility. Language: `en_US`
*   Body: `{{1}} stopped. Orders not yet placed won't go ahead. Tap Renew and I'll send you a sign-in link.`
*   Samples: `{{1}}` = `Your agent` (the other value sent is `The household agent`)
*   Buttons (quick reply): `Renew`

### `delivery_today_v1`

*   Variable: `WA_TPL_REMINDER_DELIVERY_TODAY` (text key `reminder_delivery_today`)
*   Category: Utility. Language: `en_US`
*   Body: `Groceries from {{1}} arrive today, {{2}}. Nothing needed from you.`
*   Samples: `{{1}}` = `Fresh Market`; `{{2}}` = `5:00 pm-7:00 pm`
*   Buttons (quick reply): `Stop these`

### `pantry_summary_v1`

*   Variable: `WA_TPL_REMINDER_PANTRY_SUMMARY` (text key `reminder_pantry_summary`)
*   Category: Utility. Language: `en_US`
*   Body: `Pantry check: {{1}} items at home by our records, {{2}} we're not sure about.`
*   Samples: `{{1}}` = `14`; `{{2}}` = `3`
*   Buttons (quick reply): `Open pantry`, `Stop these`

## What the parameters are

Parameters are built by code (`services/api/whatsapp/templates.py`): amounts, times in
the household's time zone, a store's name, counts, and the two fixed phrases above. None
is an allergy or diet code or a member's name (XG10). A button sends the same payload as
the in-window message.
