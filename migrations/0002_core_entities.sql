-- Core entities (architecture item 8). Additive only. Every table has created_at.
CREATE TABLE users (
  user_id      uuid        PRIMARY KEY,
  display_name text        NOT NULL,
  keycloak_sub text        UNIQUE NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE organizations (
  org_id     text        PRIMARY KEY,
  name       text        NOT NULL,
  region     text        NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE households (
  household_id text        PRIMARY KEY,
  name         text        NOT NULL,
  env          text        NOT NULL DEFAULT 'live' CHECK (env IN ('live', 'eval')),
  region       text        NOT NULL,
  zone_id      text        NOT NULL,
  timezone     text        NOT NULL,
  currency     char(3)     NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE household_members (
  household_id text        NOT NULL REFERENCES households,
  user_id      uuid        NOT NULL REFERENCES users,
  role         text        NOT NULL CHECK (role IN ('admin', 'member', 'child')),
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (household_id, user_id)
);

-- Allergy and diet values use the fixed code lists of architecture §5.
CREATE TABLE member_constraints (
  id         bigserial   PRIMARY KEY,
  user_id    uuid        NOT NULL REFERENCES users,
  kind       text        NOT NULL CHECK (kind IN ('allergy', 'diet', 'dislike')),
  value      text        NOT NULL,
  source     text        NOT NULL DEFAULT 'seed',
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT member_constraints_code_check CHECK (
    (kind = 'allergy' AND value IN ('celery', 'gluten', 'crustaceans', 'eggs', 'fish', 'lupin',
                                    'milk', 'molluscs', 'mustard', 'tree_nuts', 'peanuts',
                                    'sesame', 'soy', 'sulphites'))
    OR (kind = 'diet' AND value IN ('vegetarian', 'vegan', 'pescatarian', 'halal', 'kosher',
                                    'keto', 'high_protein'))
    OR kind = 'dislike'
  )
);

CREATE TABLE budgets (
  household_id text        NOT NULL REFERENCES households,
  period       text        NOT NULL CHECK (period = 'weekly'),
  amount_minor bigint      NOT NULL CHECK (amount_minor >= 0),
  currency     char(3)     NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (household_id, period)
);

CREATE TABLE approval_rules (
  household_id     text        PRIMARY KEY REFERENCES households,
  approver_user_id uuid        NOT NULL REFERENCES users,
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE household_addresses (
  address_ref  uuid        PRIMARY KEY,
  household_id text        NOT NULL REFERENCES households,
  line1        text        NOT NULL,
  line2        text        NULL,
  city         text        NOT NULL,
  region       text        NOT NULL,
  postal_code  text        NOT NULL,
  country      char(2)     NOT NULL,
  lat          numeric     NULL,
  lng          numeric     NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE channel_identities (
  phone_e164   text        PRIMARY KEY,
  user_id      uuid        NOT NULL REFERENCES users,
  channel_mode text        NOT NULL CHECK (channel_mode IN ('real', 'sim')),
  created_at   timestamptz NOT NULL DEFAULT now()
);
