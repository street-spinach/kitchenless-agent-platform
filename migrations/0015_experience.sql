-- Experience (experience design §13, EX-I4). Additive only: new columns, new tables, indexes
-- and two views. No new constraint on statuses or kinds; they are validated in code.
-- Every new table has created_at.

-- When the person last wrote (the 24-hour window) and when they turned reminders off.
ALTER TABLE channel_identities ADD COLUMN last_inbound_at timestamptz NULL;
ALTER TABLE channel_identities ADD COLUMN reminders_off_at timestamptz NULL;

-- Who recorded a pantry correction.
ALTER TABLE pantry_events ADD COLUMN actor_user_id uuid NULL REFERENCES users;

-- One row per WhatsApp send attempt and its outcome (sent, template, held, skipped, refused).
CREATE TABLE whatsapp_sends (
  message_id       uuid        PRIMARY KEY REFERENCES agent_messages,
  user_id          uuid        NOT NULL REFERENCES users,
  outcome          text        NOT NULL,
  reason           text        NULL,
  graph_message_id text        NULL UNIQUE,
  status           text        NULL,
  status_at        timestamptz NULL,
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX whatsapp_sends_user_created ON whatsapp_sends (user_id, created_at);

-- Messages held until the person writes (outside the window, holdable kinds).
CREATE TABLE held_messages (
  held_id    bigserial   PRIMARY KEY,
  message_id uuid        NOT NULL UNIQUE REFERENCES agent_messages,
  user_id    uuid        NOT NULL REFERENCES users,
  body       jsonb       NOT NULL,
  ref_kind   text        NOT NULL,
  ref_id     text        NOT NULL,
  status     text        NOT NULL DEFAULT 'pending',
  decided_at timestamptz NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX held_messages_user_status ON held_messages (user_id, status, held_id);

-- Each reminder the sweeper handled, sent or skipped, once per kind, ref and person.
CREATE TABLE reminders (
  reminder_id  bigserial   PRIMARY KEY,
  household_id text        NOT NULL REFERENCES households,
  user_id      uuid        NOT NULL REFERENCES users,
  kind         text        NOT NULL,
  ref_id       text        NOT NULL,
  status       text        NOT NULL,
  reason       text        NULL,
  message_id   uuid        NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (kind, ref_id, user_id)
);

CREATE INDEX reminders_household_kind ON reminders (household_id, kind);

-- A setup answer waiting for the admin's tap.
CREATE TABLE onboarding_proposals (
  proposal_id  uuid        PRIMARY KEY,
  household_id text        NOT NULL REFERENCES households,
  proposed_by  uuid        NOT NULL REFERENCES users,
  answer       jsonb       NOT NULL,
  status       text        NOT NULL DEFAULT 'pending',
  expires_at   timestamptz NOT NULL,
  decided_at   timestamptz NULL,
  decided_by   uuid        NULL REFERENCES users,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX onboarding_proposals_household_status
  ON onboarding_proposals (household_id, status);

-- A code an adult types to join; pending holds the allergy and diet codes set for them.
CREATE TABLE join_codes (
  code         text        PRIMARY KEY,
  household_id text        NOT NULL REFERENCES households,
  phone_e164   text        NOT NULL,
  display_name text        NOT NULL,
  pending      jsonb       NOT NULL,
  created_by   uuid        NOT NULL REFERENCES users,
  expires_at   timestamptz NOT NULL,
  used_at      timestamptz NULL,
  used_by      uuid        NULL REFERENCES users,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX join_codes_household_open ON join_codes (household_id) WHERE used_at IS NULL;

-- One reaction per person and order.
CREATE TABLE meal_reactions (
  order_id   uuid        REFERENCES orders,
  user_id    uuid        REFERENCES users,
  value      smallint    NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (order_id, user_id)
);

-- Decisions asked (XG1): delivered sends with a decision button, per household, user and
-- ISO week in the household's time zone. The household comes from household_members (a
-- person is in one household), so sends from household_id = NULL runs still count.
CREATE VIEW experience_weekly_asks AS
SELECT hm.household_id, s.user_id,
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
GROUP BY 1, 2, 3;

-- Guard metrics, per household and the same week. Approvals are dated by requested_at (their
-- own creation time); per-person rows take the household from household_members.
CREATE VIEW experience_weekly_guards AS
WITH events AS (
  SELECT household_id, requested_at AS at, 'approvals_expired' AS metric
  FROM approvals WHERE status = 'expired'
  UNION ALL
  SELECT household_id, updated_at, 'orders_ended'
  FROM orders WHERE state IN ('cancelled', 'expired')
  UNION ALL
  SELECT household_id, requested_at, 'rejects'
  FROM approvals WHERE status = 'rejected'
  UNION ALL
  SELECT r.household_id, t.ts, 'safety_refusals'
  FROM agent_traces t JOIN runs r ON r.run_id = t.run_id
  WHERE t.kind = 'tool_result' AND t.payload->>'refused' = 'true'
  UNION ALL
  SELECT hm.household_id, e.ts, 'revokes'
  FROM audit_events e JOIN household_members hm ON hm.user_id = e.subject_user_id
  WHERE e.kind = 'consent_revoked'
  UNION ALL
  SELECT hm.household_id, x.created_at, 'held_dropped'
  FROM held_messages x JOIN household_members hm ON hm.user_id = x.user_id
  WHERE x.status = 'dropped'
  UNION ALL
  SELECT hm.household_id, s.created_at, 'sends_not_delivered'
  FROM whatsapp_sends s JOIN household_members hm ON hm.user_id = s.user_id
  WHERE s.outcome IN ('skipped', 'refused')
)
SELECT e.household_id,
       date_trunc('week', e.at AT TIME ZONE h.timezone)::date AS week,
       count(*) FILTER (WHERE e.metric = 'approvals_expired') AS approvals_expired,
       count(*) FILTER (WHERE e.metric = 'orders_ended') AS orders_ended,
       count(*) FILTER (WHERE e.metric = 'rejects') AS rejects,
       count(*) FILTER (WHERE e.metric = 'safety_refusals') AS safety_refusals,
       count(*) FILTER (WHERE e.metric = 'revokes') AS revokes,
       count(*) FILTER (WHERE e.metric = 'held_dropped') AS held_dropped,
       count(*) FILTER (WHERE e.metric = 'sends_not_delivered') AS sends_not_delivered
FROM events e
JOIN households h ON h.household_id = e.household_id
GROUP BY 1, 2;
